"""Blocking service orchestration over durable ai-room storage."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from threading import Event

from .context.base import ContextAdapter
from .context.policy import (
    CompactionAction,
    checkpoint_fingerprint,
    evaluate_compaction,
)
from .domain import (
    AgentName,
    ContextSample,
    Delivery,
    MemberView,
    MemberStatus,
    RoomRef,
    TaskKind,
    TaskOutcome,
    TaskRequest,
    TaskView,
)
from .storage import (
    PeerNotJoinedError,
    ReplyResult,
    RoomStatus,
    SQLiteStore,
    TaskConflictError,
)
from .workspace_guard import (
    WorkspaceGuardError,
    capture_workspace,
    compare_workspace,
    normalize_exact_paths,
)


class AiRoomService:
    """Coordinate one registered agent session within a room."""

    def __init__(
        self,
        store: SQLiteStore,
        room: RoomRef,
        agent: AgentName,
        *,
        session_id: str,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = 0.5,
        heartbeat_seconds: float = 5.0,
        stale_seconds: float = 15.0,
        process_id: int | None = None,
        waiter_token_factory: Callable[[], str] | None = None,
        context_adapter: ContextAdapter | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if stale_seconds <= 0:
            raise ValueError("stale_seconds must be positive")
        if not session_id:
            raise ValueError("session_id must not be empty")

        self._store = store
        self._room = room
        self._agent = agent
        self._session_id = session_id
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_seconds = stale_seconds
        self._process_id = os.getpid() if process_id is None else process_id
        self._waiter_token_factory = waiter_token_factory or (
            lambda: str(uuid.uuid4())
        )
        self._context_adapter = context_adapter
        self._unacknowledged_reply = False

    def join(self) -> MemberView:
        return self._store.join_member(
            self._room,
            self._agent,
            session_id=self._session_id,
        )

    def send(self, request: TaskRequest) -> TaskView:
        self._acknowledge_prior_reply()
        if request.room_id != self._room.room_id:
            raise TaskConflictError("request belongs to a different room")
        if request.sender is not self._agent:
            raise TaskConflictError("request sender does not match this session")
        if request.recipient is self._agent:
            raise TaskConflictError("cannot send a task to self")
        return self._store.enqueue_task(request)

    def wait(
        self,
        cancel_event: Event,
        checkpoint_docs: tuple[Path, ...] = (),
        *,
        next_entry: str | None = None,
    ) -> Delivery | None:
        self._acknowledge_prior_reply()
        if cancel_event.is_set():
            return None

        waiter_token = self._waiter_token_factory()
        self._store.begin_wait(
            self._room.room_id,
            self._agent,
            session_id=self._session_id,
            waiter_pid=self._process_id,
            waiter_token=waiter_token,
        )
        next_heartbeat = self._clock() + self._heartbeat_seconds
        try:
            delivery = self._claim_for_waiter(waiter_token)
            if delivery is not None:
                return delivery

            waiting_check = self._store.waiting_context_check(
                self._room.room_id,
                self._agent,
            )
            if waiting_check is not None and checkpoint_docs:
                self.resume_context_check(
                    checkpoint_docs,
                    next_entry=next_entry,
                )
            elif waiting_check is None:
                self._maybe_request_context_check(
                    checkpoint_docs,
                    next_entry=next_entry,
                )

            while True:
                if cancel_event.is_set():
                    return None

                now = self._clock()
                if now >= next_heartbeat:
                    is_current = self._store.refresh_waiter(
                        self._room.room_id,
                        self._agent,
                        session_id=self._session_id,
                        waiter_pid=self._process_id,
                        waiter_token=waiter_token,
                    )
                    if not is_current:
                        return None
                    next_heartbeat = now + self._heartbeat_seconds

                delivery = self._claim_for_waiter(waiter_token)
                if delivery is not None:
                    return delivery

                cancel_event.wait(self._poll_seconds)
        finally:
            self._store.clear_waiter(
                self._room.room_id,
                self._agent,
                session_id=self._session_id,
                waiter_pid=self._process_id,
                waiter_token=waiter_token,
            )

    def reply(
        self,
        task_id: str,
        outcome: TaskOutcome,
        body: str,
    ) -> ReplyResult:
        self._acknowledge_prior_reply()
        task = self._store.get_task(task_id)
        if task.request.recipient is self._agent:
            baseline = self._store.get_workspace_baseline(task_id, task.round_no)
            if baseline is None:
                raise TaskConflictError("task round has no workspace baseline")
            after = capture_workspace(self._room.root)
            writable_docs = self._writable_docs_for(task)
            result = compare_workspace(baseline, after, writable_docs)
            if result.violations:
                raise WorkspaceGuardError(result.violations)
        if (
            task.request.kind is TaskKind.CONTEXT_CHECK
            and outcome is TaskOutcome.COMPACT_READY
        ):
            body = self._manual_compaction_reminder(task, body)
        return self._store.reply(task_id, self._agent, outcome, body)

    def resume_context_check(
        self,
        checkpoint_docs: tuple[Path, ...],
        *,
        next_entry: str | None = None,
    ) -> TaskView:
        self._acknowledge_prior_reply()
        task = self._store.waiting_context_check(
            self._room.room_id,
            self._agent,
        )
        if task is None:
            raise TaskConflictError(
                "no context check is waiting for checkpoint records"
            )

        normalized = normalize_exact_paths(self._room.root, checkpoint_docs)
        fingerprint = checkpoint_fingerprint(
            self._room.root,
            tuple(Path(path) for path in normalized),
        )
        next_round = task.round_no + 1
        recovery_entry = next_entry or task.request.next_entry
        question = (
            f"Context checkpoint follow-up round {next_round} from "
            f"{self._agent.value}. Re-check the exact checkpoint documents: "
            f"{', '.join(normalized) if normalized else '(none)'}. "
            f"Next entry: {recovery_entry or 'resume ai-room after compaction'}. "
            "Reply CHECKPOINT_NEEDED with the remaining missing records, or "
            "COMPACT_READY only when a manual compaction reminder is safe."
        )
        resumed = self._store.resume_context_check(
            task.task_id,
            self._agent,
            checkpoint_docs=normalized,
            question=question,
        )
        tokens = task.request.context.input_tokens
        if tokens is None:
            raise TaskConflictError("context-check task has no token baseline")
        self._store.record_context_check(
            self._room.room_id,
            self._agent,
            input_tokens=tokens,
            checkpoint_fingerprint=fingerprint,
            task_id=task.task_id,
        )
        return resumed

    def status(self) -> RoomStatus:
        return self._store.status(
            self._room.room_id,
            stale_after_seconds=self._stale_seconds,
        )

    def get_task(self, task_id: str) -> TaskView:
        """Return one task so presentation layers can format a delivery."""
        return self._store.get_task(task_id)

    def leave(self) -> None:
        self._store.leave_member(
            self._room.room_id,
            self._agent,
            session_id=self._session_id,
        )

    def acknowledge_delivered_reply(self) -> None:
        """Acknowledge a reply once its recipient has actually received it.

        Callers invoke this after the delivery reached the agent, so a reply
        stays redeliverable while the process could still fail before that.
        """
        if not self._unacknowledged_reply:
            return
        self._acknowledge_prior_reply()
        self._unacknowledged_reply = False

    def _acknowledge_prior_reply(self) -> None:
        self._store.acknowledge_informational_replies(
            self._room.room_id,
            self._agent,
            session_id=self._session_id,
        )

    def _capture_task_baseline(self, delivery: Delivery) -> None:
        task = self._store.get_task(delivery.task_id)
        if task.request.recipient is not self._agent:
            return
        self._writable_docs_for(task)
        snapshot = capture_workspace(self._room.root)
        self._store.record_workspace_baseline(
            task.task_id,
            task.round_no,
            snapshot,
        )

    def _claim_for_waiter(self, waiter_token: str) -> Delivery | None:
        delivery = self._store.claim_next_message(
            self._room.room_id,
            self._agent,
            lease_seconds=self._stale_seconds,
            session_id=self._session_id,
            waiter_pid=self._process_id,
            waiter_token=waiter_token,
        )
        if delivery is not None:
            self._capture_task_baseline(delivery)
            if delivery.outcome is not None:
                self._unacknowledged_reply = True
        return delivery

    def _maybe_request_context_check(
        self,
        checkpoint_docs: tuple[Path, ...],
        *,
        next_entry: str | None = None,
    ) -> TaskView | None:
        if self._context_adapter is None:
            return None

        sample = self._context_adapter.sample()
        tokens = sample.input_tokens
        previous = self._store.get_previous_context_check(
            self._room.room_id,
            self._agent,
        )
        if tokens is None:
            return None
        if previous is not None and tokens < previous.input_tokens:
            self._store.clear_context_check(self._room.room_id, self._agent)
            previous = None
        if tokens < 150_000:
            return None

        normalized = normalize_exact_paths(self._room.root, checkpoint_docs)
        fingerprint = checkpoint_fingerprint(
            self._room.root,
            tuple(Path(path) for path in normalized),
        )
        action = evaluate_compaction(sample, previous, fingerprint)
        if action is CompactionAction.NONE:
            return None

        peer = (
            AgentName.CLAUDE
            if self._agent is AgentName.CODEX
            else AgentName.CODEX
        )
        if (
            self.status().members[peer].status
            is MemberStatus.NEVER_JOINED
        ):
            raise PeerNotJoinedError(
                f"{peer.value} must join the room before a context check "
                "can be requested"
            )

        question = self._context_check_question(
            sample,
            normalized,
            action,
            next_entry=next_entry,
        )
        request = TaskRequest(
            room_id=self._room.room_id,
            sender=self._agent,
            recipient=peer,
            kind=TaskKind.CONTEXT_CHECK,
            question=question,
            related_docs=normalized,
            writable_docs=(),
            context=sample,
            checkpoint_docs=normalized,
            next_entry=next_entry or "resume ai-room after compaction",
            idempotency_key=(
                f"context-check:{self._agent.value}:{tokens}:{fingerprint}"
            ),
        )
        task = self._store.enqueue_task(request)
        self._store.record_context_check(
            self._room.room_id,
            self._agent,
            input_tokens=tokens,
            checkpoint_fingerprint=fingerprint,
            task_id=task.task_id,
        )
        return task

    def _context_check_question(
        self,
        sample: ContextSample,
        checkpoint_docs: tuple[str, ...],
        action: CompactionAction,
        *,
        next_entry: str | None = None,
    ) -> str:
        tokens = (
            "unknown"
            if sample.input_tokens is None
            else str(sample.input_tokens)
        )
        context_window = (
            "unknown"
            if sample.context_window is None
            else str(sample.context_window)
        )
        urgency = (
            "URGENT: the primary must finish or pause the smallest safe unit "
            "and record its checkpoint before any reminder. "
            if action is CompactionAction.URGENT_CHECK
            else ""
        )
        recovery_entry = next_entry or "resume ai-room after compaction"
        return (
            f"{urgency}Evaluate the safe checkpoint boundary for "
            f"{self._agent.value}. Input tokens: {tokens}. Context window: "
            f"{context_window}. Sample source: {sample.source.value}. "
            "Exact checkpoint documents: "
            f"{', '.join(checkpoint_docs) if checkpoint_docs else '(none)'}. "
            f"Next entry: {recovery_entry}. Reply "
            "CHECKPOINT_NEEDED with exact missing records, or COMPACT_READY "
            "only to request a manual compaction reminder. Never compact "
            "automatically."
        )

    @staticmethod
    def _manual_compaction_reminder(task: TaskView, advisor_body: str) -> str:
        tokens = (
            "unknown"
            if task.request.context.input_tokens is None
            else str(task.request.context.input_tokens)
        )
        documents = (
            ", ".join(task.request.checkpoint_docs)
            if task.request.checkpoint_docs
            else "(none)"
        )
        recovery = task.request.next_entry or "resume ai-room after compaction"
        return (
            f"Manual compaction reminder for {task.request.sender.value}. "
            f"Tokens: {tokens}. Checkpoint documents: {documents}. "
            f"Recovery entry: {recovery}. Advisor: {advisor_body}"
        )

    def _writable_docs_for(self, task: TaskView) -> tuple[str, ...]:
        if task.request.kind in (TaskKind.DECISION, TaskKind.CONTEXT_CHECK):
            return ()
        return normalize_exact_paths(
            self._room.root,
            (Path(path) for path in task.request.writable_docs),
        )
