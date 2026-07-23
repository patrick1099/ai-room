"""Immutable domain contracts shared by ai-room components."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AgentName(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class TaskKind(StrEnum):
    DECISION = "decision"
    REQUIREMENTS_REVIEW = "requirements_review"
    DESIGN_REVIEW = "design_review"
    PLAN_REVIEW = "plan_review"
    CONTEXT_CHECK = "context_check"


class TaskState(StrEnum):
    QUEUED = "queued"
    WORKING = "working"
    WAITING_CHECKPOINT = "waiting_checkpoint"
    DONE = "done"
    BLOCKED = "blocked"
    COMPACT_READY = "compact_ready"


class TaskOutcome(StrEnum):
    DONE = "done"
    BLOCKED = "blocked"
    COMPACT_READY = "compact_ready"
    CHECKPOINT_NEEDED = "checkpoint_needed"


class ContextSource(StrEnum):
    UNKNOWN = "unknown"
    CODEX_TOKEN_COUNT = "codex_token_count"
    CLAUDE_USAGE = "claude_usage"


class MemberStatus(StrEnum):
    NEVER_JOINED = "never_joined"
    JOINED_NOT_WAITING = "joined_not_waiting"
    WAITING = "waiting"


@dataclass(frozen=True)
class RoomRef:
    room_id: str
    root: Path
    explicit_name: str | None = None


@dataclass(frozen=True)
class ContextSample:
    input_tokens: int | None
    context_window: int | None
    source: ContextSource
    session_id: str | None
    unknown_reason: str | None = None


@dataclass(frozen=True)
class TaskRequest:
    room_id: str
    sender: AgentName
    recipient: AgentName
    kind: TaskKind
    question: str
    related_docs: tuple[str, ...]
    writable_docs: tuple[str, ...]
    context: ContextSample
    checkpoint_docs: tuple[str, ...]
    next_entry: str | None
    idempotency_key: str
    reply_to: str | None = None


@dataclass(frozen=True)
class TaskView:
    task_id: str
    request: TaskRequest
    state: TaskState
    round_no: int = 1
    blocked_reason: str | None = None

    @property
    def room_id(self) -> str:
        return self.request.room_id


@dataclass(frozen=True)
class Delivery:
    message_id: str
    task_id: str
    room_id: str
    sender: AgentName
    recipient: AgentName
    body: str
    lease_token: str | None = None


@dataclass(frozen=True)
class GuardResult:
    allowed_changes: tuple[str, ...]
    violations: tuple[str, ...]


@dataclass(frozen=True)
class MemberView:
    agent: AgentName
    is_joined: bool
    is_waiting: bool
    last_heartbeat: float | None

    @property
    def status(self) -> MemberStatus:
        if self.is_waiting:
            return MemberStatus.WAITING
        if self.is_joined or self.last_heartbeat is not None:
            return MemberStatus.JOINED_NOT_WAITING
        return MemberStatus.NEVER_JOINED
