"""Blocking service orchestration over durable ai-room storage."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from threading import Event

from .domain import (
    AgentName,
    Delivery,
    MemberView,
    RoomRef,
    TaskOutcome,
    TaskRequest,
    TaskView,
)
from .storage import ReplyResult, RoomStatus, SQLiteStore, TaskConflictError


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
    ) -> Delivery | None:
        del checkpoint_docs
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

                delivery = self._store.claim_next_message(
                    self._room.room_id,
                    self._agent,
                    lease_seconds=self._stale_seconds,
                    session_id=self._session_id,
                    waiter_pid=self._process_id,
                    waiter_token=waiter_token,
                )
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
        return self._store.reply(task_id, self._agent, outcome, body)

    def status(self) -> RoomStatus:
        return self._store.status(
            self._room.room_id,
            stale_after_seconds=self._stale_seconds,
        )

    def leave(self) -> None:
        self._store.leave_member(
            self._room.room_id,
            self._agent,
            session_id=self._session_id,
        )

    def _acknowledge_prior_reply(self) -> None:
        self._store.acknowledge_informational_replies(
            self._room.room_id,
            self._agent,
            session_id=self._session_id,
        )
