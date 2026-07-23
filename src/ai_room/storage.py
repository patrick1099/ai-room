"""Durable SQLite storage and task state transitions for ai-room."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .domain import (
    AgentName,
    ContextSample,
    ContextSource,
    Delivery,
    MemberView,
    RoomRef,
    TaskKind,
    TaskOutcome,
    TaskRequest,
    TaskState,
    TaskView,
)
from .context.policy import PreviousContextCheck
from .workspace_guard import WorkspaceSnapshot


SCHEMA_VERSION = 2
_ACTIVE_STATES = (TaskState.WORKING.value, TaskState.WAITING_CHECKPOINT.value)
_TERMINAL_STATES = {
    TaskOutcome.DONE: TaskState.DONE,
    TaskOutcome.BLOCKED: TaskState.BLOCKED,
    TaskOutcome.COMPACT_READY: TaskState.COMPACT_READY,
}


class StorageError(RuntimeError):
    """Base class for durable storage failures."""


class PeerNotJoinedError(StorageError):
    """Raised when a task targets a peer that has never joined the room."""


class TaskConflictError(StorageError):
    """Raised when an idempotency key or task transition conflicts."""


class SchemaVersionError(StorageError):
    """Raised when an existing database uses an unsupported schema."""


class MalformedMessageError(StorageError):
    """Raised after a malformed message has been quarantined."""

    def __init__(self, message_id: str, reason: str) -> None:
        self.message_id = message_id
        super().__init__(f"malformed message {message_id}: {reason}")


class DatabaseOpenError(StorageError):
    """Raised when SQLite cannot safely open an existing database."""


class DatabaseBusyError(StorageError):
    """Raised when a write lock remains unavailable after the busy timeout."""


@dataclass(frozen=True)
class ReplyResult:
    """Result of an atomic task reply."""

    reply_message_id: str
    task_id: str
    state: TaskState


@dataclass(frozen=True)
class RoomStatus:
    """Current room membership and active task."""

    room_id: str
    members: dict[AgentName, MemberView]
    active_task: TaskView | None


_SCHEMA = """
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
);

CREATE TABLE rooms (
    room_id TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    explicit_name TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE members (
    room_id TEXT NOT NULL REFERENCES rooms(room_id),
    agent TEXT NOT NULL,
    session_id TEXT,
    joined_at REAL NOT NULL,
    left_at REAL,
    last_heartbeat REAL NOT NULL,
    is_waiting INTEGER NOT NULL CHECK (is_waiting IN (0, 1)),
    waiter_pid INTEGER,
    waiter_token TEXT,
    PRIMARY KEY (room_id, agent)
);

CREATE TABLE tasks (
    fifo_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id),
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    related_docs_json TEXT NOT NULL,
    writable_docs_json TEXT NOT NULL,
    checkpoint_docs_json TEXT NOT NULL,
    next_entry TEXT,
    reply_to TEXT,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    blocked_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (room_id, idempotency_key)
);

CREATE TABLE context_samples (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    input_tokens INTEGER,
    context_window INTEGER,
    source TEXT NOT NULL,
    session_id TEXT,
    unknown_reason TEXT
);

CREATE TABLE messages (
    fifo_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id),
    sequence INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    message_type TEXT NOT NULL CHECK (message_type IN ('request', 'reply')),
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    body TEXT NOT NULL,
    outcome TEXT,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    delivered_at REAL,
    lease_token TEXT,
    lease_expires_at REAL,
    delivered_session_id TEXT,
    delivered_pid INTEGER,
    acknowledged_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (room_id, idempotency_key),
    UNIQUE (task_id, sequence)
);

CREATE UNIQUE INDEX uq_messages_terminal_reply
ON messages(task_id)
WHERE message_type = 'reply' AND is_terminal = 1;

CREATE TABLE quarantined_messages (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_message_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    quarantined_at REAL NOT NULL
);

CREATE TABLE workspace_baselines (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    round_no INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, round_no)
);

CREATE TABLE context_check_state (
    room_id TEXT NOT NULL REFERENCES rooms(room_id),
    agent TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    checkpoint_fingerprint TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    awaiting_reset INTEGER NOT NULL CHECK (awaiting_reset IN (0, 1)),
    updated_at REAL NOT NULL,
    PRIMARY KEY (room_id, agent)
);
"""


def _uuid4() -> str:
    return str(uuid.uuid4())


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_busy(error: sqlite3.Error) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _path_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def _request_payload(request: TaskRequest) -> dict[str, object]:
    return {
        "related_docs": list(request.related_docs),
        "writable_docs": list(request.writable_docs),
        "checkpoint_docs": list(request.checkpoint_docs),
        "context": {
            "input_tokens": request.context.input_tokens,
            "context_window": request.context.context_window,
            "source": request.context.source.value,
            "session_id": request.context.session_id,
            "unknown_reason": request.context.unknown_reason,
        },
        "next_entry": request.next_entry,
        "reply_to": request.reply_to,
    }


def _validate_optional_int(value: object, field: str) -> int | None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{field} must be an integer or null")
    return value


def _validate_optional_str(value: object, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _validate_request_payload(raw: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("payload must be an object")

    _path_list(value.get("related_docs"), "related_docs")
    _path_list(value.get("writable_docs"), "writable_docs")
    _path_list(value.get("checkpoint_docs"), "checkpoint_docs")
    context = value.get("context")
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    _validate_optional_int(context.get("input_tokens"), "input_tokens")
    _validate_optional_int(context.get("context_window"), "context_window")
    source = context.get("source")
    if not isinstance(source, str):
        raise ValueError("source must be a string")
    ContextSource(source)
    _validate_optional_str(context.get("session_id"), "session_id")
    _validate_optional_str(context.get("unknown_reason"), "unknown_reason")
    _validate_optional_str(value.get("next_entry"), "next_entry")
    _validate_optional_str(value.get("reply_to"), "reply_to")
    return value


def _validate_message_row(
    row: sqlite3.Row,
) -> tuple[AgentName, AgentName]:
    message_type = row["message_type"]
    if message_type not in ("request", "reply"):
        raise ValueError("message_type must be request or reply")
    sender = AgentName(row["sender"])
    recipient = AgentName(row["recipient"])

    if message_type == "request":
        _validate_request_payload(row["payload_json"])
        if row["outcome"] is not None:
            raise ValueError("request outcome must be null")
        return sender, recipient

    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    payload_outcome = payload.get("outcome")
    if not isinstance(payload_outcome, str):
        raise ValueError("outcome must be a string")
    stored_outcome = row["outcome"]
    if not isinstance(stored_outcome, str):
        raise ValueError("stored outcome must be a string")
    if TaskOutcome(payload_outcome) is not TaskOutcome(stored_outcome):
        raise ValueError("payload outcome does not match stored outcome")
    payload_body = payload.get("body")
    if not isinstance(payload_body, str):
        raise ValueError("body must be a string")
    if payload_body != row["body"]:
        raise ValueError("payload body does not match stored body")
    return sender, recipient


def _workspace_snapshot_from_json(raw: str) -> WorkspaceSnapshot:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("workspace snapshot must be a list")
    files: list[tuple[str, str]] = []
    for entry in value:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(item, str) for item in entry)
        ):
            raise ValueError("workspace snapshot entry must contain path and hash")
        files.append((entry[0], entry[1]))
    return WorkspaceSnapshot(tuple(files))


class SQLiteStore:
    """Room-scoped state persisted in a versioned SQLite database."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        path: Path,
        clock: Callable[[], float],
    ) -> None:
        self._connection = connection
        self._path = path
        self._clock = clock
        self._mutation_lock = threading.RLock()

    @classmethod
    def open(cls, path: Path, clock: Callable[[], float]) -> SQLiteStore:
        path = Path(path)
        is_new = not path.exists()
        connection: sqlite3.Connection | None = None
        try:
            if is_new:
                path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row

            if not is_new:
                cls._verify_existing_schema(connection)

            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

            if is_new:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    f"{_SCHEMA}\n"
                    "INSERT INTO schema_meta (singleton, schema_version) "
                    f"VALUES (1, {SCHEMA_VERSION});\n"
                    "COMMIT;\n"
                )
            return cls(connection, path, clock)
        except SchemaVersionError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error as error:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            raise DatabaseOpenError(
                f"cannot open database {path.name}: {type(error).__name__}"
            ) from error

    @staticmethod
    def _verify_existing_schema(connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT schema_version FROM schema_meta WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error as error:
            if "no such table" in str(error).lower():
                raise SchemaVersionError("unsupported schema version: missing") from error
            raise
        if row is None or row[0] != SCHEMA_VERSION:
            found = "missing" if row is None else row[0]
            raise SchemaVersionError(f"unsupported schema version: {found}")

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        with self._mutation_lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if _is_busy(error):
                    raise DatabaseBusyError(
                        "database remained busy for 5000 ms"
                    ) from error
                raise
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def join_member(
        self,
        room: RoomRef,
        agent: AgentName,
        *,
        session_id: str | None = None,
    ) -> MemberView:
        now = self._clock()
        try:
            with self._mutation():
                existing_room = self._connection.execute(
                    "SELECT root, explicit_name FROM rooms WHERE room_id = ?",
                    (room.room_id,),
                ).fetchone()
                if existing_room is None:
                    self._connection.execute(
                        """
                        INSERT INTO rooms (room_id, root, explicit_name, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (room.room_id, str(room.root), room.explicit_name, now),
                    )
                elif (
                    existing_room["root"] != str(room.root)
                    or existing_room["explicit_name"] != room.explicit_name
                ):
                    raise TaskConflictError(
                        f"room {room.room_id!r} already has different identity data"
                    )
                self._connection.execute(
                    """
                    INSERT INTO members (
                        room_id, agent, session_id, joined_at, left_at,
                        last_heartbeat, is_waiting, waiter_pid, waiter_token
                    ) VALUES (?, ?, ?, ?, NULL, ?, 0, NULL, NULL)
                    ON CONFLICT(room_id, agent) DO UPDATE SET
                        session_id = excluded.session_id,
                        joined_at = excluded.joined_at,
                        left_at = NULL,
                        last_heartbeat = excluded.last_heartbeat,
                        is_waiting = 0,
                        waiter_pid = NULL,
                        waiter_token = NULL
                    """,
                    (room.room_id, agent.value, session_id, now, now),
                )
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise DatabaseBusyError("database remained busy for 5000 ms") from error
            raise
        return MemberView(agent, True, False, now)

    def leave_member(
        self,
        room_id: str,
        agent: AgentName,
        *,
        session_id: str | None = None,
    ) -> None:
        now = self._clock()
        with self._mutation():
            if session_id is None:
                self._connection.execute(
                    """
                    UPDATE members
                    SET left_at = ?, last_heartbeat = ?, is_waiting = 0,
                        waiter_pid = NULL, waiter_token = NULL
                    WHERE room_id = ? AND agent = ?
                    """,
                    (now, now, room_id, agent.value),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE members
                    SET left_at = ?, last_heartbeat = ?, is_waiting = 0,
                        waiter_pid = NULL, waiter_token = NULL
                    WHERE room_id = ? AND agent = ? AND session_id = ?
                    """,
                    (now, now, room_id, agent.value, session_id),
                )

    def heartbeat(
        self, room_id: str, agent: AgentName, *, is_waiting: bool
    ) -> MemberView:
        now = self._clock()
        with self._mutation():
            cursor = self._connection.execute(
                """
                UPDATE members
                SET last_heartbeat = ?, is_waiting = ?
                WHERE room_id = ? AND agent = ? AND left_at IS NULL
                """,
                (now, int(is_waiting), room_id, agent.value),
            )
            if cursor.rowcount != 1:
                raise PeerNotJoinedError(
                    f"{agent.value} is not joined to room {room_id!r}"
                )
        return MemberView(agent, True, is_waiting, now)

    def begin_wait(
        self,
        room_id: str,
        agent: AgentName,
        *,
        session_id: str,
        waiter_pid: int,
        waiter_token: str,
    ) -> MemberView:
        now = self._clock()
        with self._mutation():
            cursor = self._connection.execute(
                """
                UPDATE members
                SET last_heartbeat = ?, is_waiting = 1,
                    waiter_pid = ?, waiter_token = ?
                WHERE room_id = ? AND agent = ? AND session_id = ?
                  AND left_at IS NULL
                """,
                (
                    now,
                    waiter_pid,
                    waiter_token,
                    room_id,
                    agent.value,
                    session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PeerNotJoinedError(
                    f"{agent.value} session is not joined to room {room_id!r}"
                )
        return MemberView(agent, True, True, now)

    def refresh_waiter(
        self,
        room_id: str,
        agent: AgentName,
        *,
        session_id: str,
        waiter_pid: int,
        waiter_token: str,
    ) -> bool:
        now = self._clock()
        with self._mutation():
            cursor = self._connection.execute(
                """
                UPDATE members
                SET last_heartbeat = ?
                WHERE room_id = ? AND agent = ? AND session_id = ?
                  AND left_at IS NULL AND is_waiting = 1
                  AND waiter_pid = ? AND waiter_token = ?
                """,
                (
                    now,
                    room_id,
                    agent.value,
                    session_id,
                    waiter_pid,
                    waiter_token,
                ),
            )
        return cursor.rowcount == 1

    def clear_waiter(
        self,
        room_id: str,
        agent: AgentName,
        *,
        session_id: str,
        waiter_pid: int,
        waiter_token: str,
    ) -> bool:
        now = self._clock()
        with self._mutation():
            cursor = self._connection.execute(
                """
                UPDATE members
                SET last_heartbeat = ?, is_waiting = 0,
                    waiter_pid = NULL, waiter_token = NULL
                WHERE room_id = ? AND agent = ? AND session_id = ?
                  AND waiter_pid = ? AND waiter_token = ?
                """,
                (
                    now,
                    room_id,
                    agent.value,
                    session_id,
                    waiter_pid,
                    waiter_token,
                ),
            )
        return cursor.rowcount == 1

    def acknowledge_informational_replies(
        self,
        room_id: str,
        recipient: AgentName,
        *,
        session_id: str,
    ) -> int:
        now = self._clock()
        with self._mutation():
            member = self._connection.execute(
                """
                SELECT 1 FROM members
                WHERE room_id = ? AND agent = ? AND session_id = ?
                  AND left_at IS NULL
                """,
                (room_id, recipient.value, session_id),
            ).fetchone()
            if member is None:
                raise PeerNotJoinedError(
                    f"{recipient.value} session is not joined to room {room_id!r}"
                )
            cursor = self._connection.execute(
                """
                UPDATE messages
                SET acknowledged_at = ?
                WHERE room_id = ? AND recipient = ?
                  AND message_type = 'reply'
                  AND acknowledged_at IS NULL
                  AND delivered_session_id = ?
                  AND lease_expires_at > ?
                """,
                (now, room_id, recipient.value, session_id, now),
            )
        return cursor.rowcount

    def enqueue_task(self, request: TaskRequest) -> TaskView:
        now = self._clock()
        try:
            with self._mutation():
                existing = self._connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE room_id = ? AND idempotency_key = ?
                    """,
                    (request.room_id, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    task = self._task_from_row(existing)
                    if task.request != request:
                        raise TaskConflictError(
                            "task idempotency key has a different payload"
                        )
                    return task

                peer = self._connection.execute(
                    """
                    SELECT 1 FROM members
                    WHERE room_id = ? AND agent = ?
                    """,
                    (request.room_id, request.recipient.value),
                ).fetchone()
                if peer is None:
                    raise PeerNotJoinedError(
                        f"{request.recipient.value} has not joined room "
                        f"{request.room_id!r}"
                    )

                task_id = _uuid4()
                self._connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, room_id, sender, recipient, kind, question,
                        related_docs_json, writable_docs_json, checkpoint_docs_json,
                        next_entry, reply_to, idempotency_key, state, round_no,
                        blocked_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
                    """,
                    (
                        task_id,
                        request.room_id,
                        request.sender.value,
                        request.recipient.value,
                        request.kind.value,
                        request.question,
                        _json_dump(list(request.related_docs)),
                        _json_dump(list(request.writable_docs)),
                        _json_dump(list(request.checkpoint_docs)),
                        request.next_entry,
                        request.reply_to,
                        request.idempotency_key,
                        TaskState.QUEUED.value,
                        now,
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO context_samples (
                        task_id, input_tokens, context_window, source,
                        session_id, unknown_reason
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        request.context.input_tokens,
                        request.context.context_window,
                        request.context.source.value,
                        request.context.session_id,
                        request.context.unknown_reason,
                    ),
                )
                self._activate_next(request.room_id, now)
                row = self._connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                return self._task_from_row(row)
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise DatabaseBusyError("database remained busy for 5000 ms") from error
            raise

    def get_previous_context_check(
        self,
        room_id: str,
        agent: AgentName,
    ) -> PreviousContextCheck | None:
        row = self._connection.execute(
            """
            SELECT input_tokens, checkpoint_fingerprint, awaiting_reset
            FROM context_check_state
            WHERE room_id = ? AND agent = ?
            """,
            (room_id, agent.value),
        ).fetchone()
        if row is None:
            return None
        return PreviousContextCheck(
            input_tokens=row["input_tokens"],
            checkpoint_fingerprint=row["checkpoint_fingerprint"],
            awaiting_reset=bool(row["awaiting_reset"]),
        )

    def record_context_check(
        self,
        room_id: str,
        agent: AgentName,
        *,
        input_tokens: int,
        checkpoint_fingerprint: str,
        task_id: str,
    ) -> None:
        now = self._clock()
        with self._mutation():
            task = self._connection.execute(
                """
                SELECT 1 FROM tasks
                WHERE task_id = ? AND room_id = ? AND sender = ?
                  AND kind = ?
                """,
                (
                    task_id,
                    room_id,
                    agent.value,
                    TaskKind.CONTEXT_CHECK.value,
                ),
            ).fetchone()
            if task is None:
                raise TaskConflictError(
                    "context check state requires a matching context-check task"
                )
            self._connection.execute(
                """
                INSERT INTO context_check_state (
                    room_id, agent, input_tokens, checkpoint_fingerprint,
                    task_id, awaiting_reset, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(room_id, agent) DO UPDATE SET
                    input_tokens = excluded.input_tokens,
                    checkpoint_fingerprint = excluded.checkpoint_fingerprint,
                    task_id = excluded.task_id,
                    awaiting_reset = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    room_id,
                    agent.value,
                    input_tokens,
                    checkpoint_fingerprint,
                    task_id,
                    now,
                ),
            )

    def clear_context_check(
        self,
        room_id: str,
        agent: AgentName,
    ) -> None:
        with self._mutation():
            self._connection.execute(
                "DELETE FROM context_check_state WHERE room_id = ? AND agent = ?",
                (room_id, agent.value),
            )

    def waiting_context_check(
        self,
        room_id: str,
        sender: AgentName,
    ) -> TaskView | None:
        row = self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE room_id = ? AND sender = ? AND kind = ? AND state = ?
            ORDER BY fifo_sequence DESC
            LIMIT 1
            """,
            (
                room_id,
                sender.value,
                TaskKind.CONTEXT_CHECK.value,
                TaskState.WAITING_CHECKPOINT.value,
            ),
        ).fetchone()
        return None if row is None else self._task_from_row(row)

    def resume_context_check(
        self,
        task_id: str,
        sender: AgentName,
        *,
        checkpoint_docs: tuple[str, ...],
        question: str,
    ) -> TaskView:
        now = self._clock()
        with self._mutation():
            task = self._connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TaskConflictError(f"unknown task {task_id}")
            if (
                task["sender"] != sender.value
                or task["kind"] != TaskKind.CONTEXT_CHECK.value
            ):
                raise TaskConflictError(
                    "only the context-check sender may continue the task"
                )
            if task["state"] != TaskState.WAITING_CHECKPOINT.value:
                raise TaskConflictError(
                    "context check is not waiting for checkpoint records"
                )

            round_no = task["round_no"] + 1
            self._connection.execute(
                """
                UPDATE tasks
                SET checkpoint_docs_json = ?, question = ?, state = ?,
                    round_no = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    _json_dump(list(checkpoint_docs)),
                    question,
                    TaskState.WORKING.value,
                    round_no,
                    now,
                    task_id,
                ),
            )
            updated = self._connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            self._insert_request_message(updated, now)
            return self._task_from_row(updated)

    def _activate_next(self, room_id: str, now: float) -> None:
        active = self._connection.execute(
            """
            SELECT 1 FROM tasks
            WHERE room_id = ? AND state IN (?, ?)
            LIMIT 1
            """,
            (room_id, *_ACTIVE_STATES),
        ).fetchone()
        if active is not None:
            return

        row = self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE room_id = ? AND state = ?
            ORDER BY fifo_sequence
            LIMIT 1
            """,
            (room_id, TaskState.QUEUED.value),
        ).fetchone()
        if row is None:
            return
        self._connection.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
            (TaskState.WORKING.value, now, row["task_id"]),
        )
        self._insert_request_message(row, now)

    def _insert_request_message(self, task_row: sqlite3.Row, now: float) -> None:
        request = self._request_from_row(task_row)
        sequence = self._next_message_sequence(task_row["task_id"])
        self._connection.execute(
            """
            INSERT INTO messages (
                message_id, task_id, room_id, sequence, round_no, message_type,
                sender, recipient, body, outcome, payload_json, idempotency_key,
                is_terminal, created_at
            ) VALUES (?, ?, ?, ?, ?, 'request', ?, ?, ?, NULL, ?, ?, 0, ?)
            """,
            (
                _uuid4(),
                task_row["task_id"],
                task_row["room_id"],
                sequence,
                task_row["round_no"],
                task_row["sender"],
                task_row["recipient"],
                task_row["question"],
                _json_dump(_request_payload(request)),
                f"{task_row['task_id']}:round:{task_row['round_no']}:request",
                now,
            ),
        )

    def _next_message_sequence(self, task_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0])

    def get_task(self, task_id: str) -> TaskView:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskConflictError(f"unknown task {task_id}")
        return self._task_from_row(row)

    def record_workspace_baseline(
        self,
        task_id: str,
        round_no: int,
        snapshot: WorkspaceSnapshot,
    ) -> WorkspaceSnapshot:
        """Persist the first baseline for a round and return the durable value."""
        payload = _json_dump(list(snapshot.files))
        now = self._clock()
        try:
            with self._mutation():
                task = self._connection.execute(
                    "SELECT round_no FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if task is None:
                    raise TaskConflictError(f"unknown task {task_id}")
                if task["round_no"] != round_no:
                    raise TaskConflictError("task round changed before baseline capture")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO workspace_baselines (
                        task_id, round_no, snapshot_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, round_no, payload, now),
                )
                row = self._connection.execute(
                    """
                    SELECT snapshot_json FROM workspace_baselines
                    WHERE task_id = ? AND round_no = ?
                    """,
                    (task_id, round_no),
                ).fetchone()
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise DatabaseBusyError("database remained busy for 5000 ms") from error
            raise
        return _workspace_snapshot_from_json(row["snapshot_json"])

    def get_workspace_baseline(
        self,
        task_id: str,
        round_no: int,
    ) -> WorkspaceSnapshot | None:
        """Load a task-round baseline without opening a write transaction."""
        row = self._connection.execute(
            """
            SELECT snapshot_json FROM workspace_baselines
            WHERE task_id = ? AND round_no = ?
            """,
            (task_id, round_no),
        ).fetchone()
        if row is None:
            return None
        return _workspace_snapshot_from_json(row["snapshot_json"])

    def _task_from_row(self, row: sqlite3.Row) -> TaskView:
        return TaskView(
            task_id=row["task_id"],
            request=self._request_from_row(row),
            state=TaskState(row["state"]),
            round_no=row["round_no"],
            blocked_reason=row["blocked_reason"],
        )

    def _request_from_row(self, row: sqlite3.Row) -> TaskRequest:
        related_docs = _path_list(
            json.loads(row["related_docs_json"]), "related_docs"
        )
        writable_docs = _path_list(
            json.loads(row["writable_docs_json"]), "writable_docs"
        )
        checkpoint_docs = _path_list(
            json.loads(row["checkpoint_docs_json"]), "checkpoint_docs"
        )
        context = self._connection.execute(
            "SELECT * FROM context_samples WHERE task_id = ?", (row["task_id"],)
        ).fetchone()
        if context is None:
            raise ValueError("task context sample is missing")
        input_tokens = _validate_optional_int(context["input_tokens"], "input_tokens")
        context_window = _validate_optional_int(
            context["context_window"], "context_window"
        )
        session_id = _validate_optional_str(context["session_id"], "session_id")
        unknown_reason = _validate_optional_str(
            context["unknown_reason"], "unknown_reason"
        )
        return TaskRequest(
            room_id=row["room_id"],
            sender=AgentName(row["sender"]),
            recipient=AgentName(row["recipient"]),
            kind=TaskKind(row["kind"]),
            question=row["question"],
            related_docs=related_docs,
            writable_docs=writable_docs,
            context=ContextSample(
                input_tokens=input_tokens,
                context_window=context_window,
                source=ContextSource(context["source"]),
                session_id=session_id,
                unknown_reason=unknown_reason,
            ),
            checkpoint_docs=checkpoint_docs,
            next_entry=_validate_optional_str(row["next_entry"], "next_entry"),
            idempotency_key=row["idempotency_key"],
            reply_to=_validate_optional_str(row["reply_to"], "reply_to"),
        )

    def claim_next_message(
        self,
        room_id: str,
        recipient: AgentName,
        *,
        lease_seconds: float,
        session_id: str | None = None,
        waiter_pid: int | None = None,
        waiter_token: str | None = None,
    ) -> Delivery | None:
        waiter_identity = (session_id, waiter_pid, waiter_token)
        has_waiter_identity = any(value is not None for value in waiter_identity)
        if has_waiter_identity and not all(
            value is not None for value in waiter_identity
        ):
            raise ValueError(
                "session_id, waiter_pid, and waiter_token must form "
                "a complete waiter identity"
            )

        now = self._clock()
        malformed: tuple[str, str] | None = None
        delivery: Delivery | None = None
        try:
            with self._mutation():
                if has_waiter_identity:
                    current_waiter = self._connection.execute(
                        """
                        SELECT 1 FROM members
                        WHERE room_id = ? AND agent = ? AND session_id = ?
                          AND left_at IS NULL AND is_waiting = 1
                          AND waiter_pid = ? AND waiter_token = ?
                        """,
                        (
                            room_id,
                            recipient.value,
                            session_id,
                            waiter_pid,
                            waiter_token,
                        ),
                    ).fetchone()
                    if current_waiter is None:
                        return None

                row = self._connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE room_id = ?
                      AND (
                          recipient = ?
                          OR recipient NOT IN (?, ?)
                      )
                      AND acknowledged_at IS NULL
                      AND (delivered_at IS NULL OR lease_expires_at <= ?)
                    ORDER BY fifo_sequence
                    LIMIT 1
                    """,
                    (
                        room_id,
                        recipient.value,
                        AgentName.CODEX.value,
                        AgentName.CLAUDE.value,
                        now,
                    ),
                ).fetchone()
                if row is None:
                    return None

                try:
                    message_sender, message_recipient = _validate_message_row(row)
                except (json.JSONDecodeError, TypeError, ValueError) as error:
                    reason = str(error)
                    self._quarantine_message(row, reason, now)
                    malformed = (row["message_id"], reason)
                else:
                    lease_token = _uuid4()
                    self._connection.execute(
                        """
                        UPDATE messages
                        SET delivered_at = ?, lease_token = ?, lease_expires_at = ?,
                            delivered_session_id = ?, delivered_pid = ?
                        WHERE message_id = ?
                        """,
                        (
                            now,
                            lease_token,
                            now + lease_seconds,
                            session_id,
                            waiter_pid,
                            row["message_id"],
                        ),
                    )
                    delivery = Delivery(
                        message_id=row["message_id"],
                        task_id=row["task_id"],
                        room_id=row["room_id"],
                        sender=message_sender,
                        recipient=message_recipient,
                        body=row["body"],
                        outcome=(
                            None
                            if row["outcome"] is None
                            else TaskOutcome(row["outcome"])
                        ),
                        lease_token=lease_token,
                    )
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise DatabaseBusyError("database remained busy for 5000 ms") from error
            raise
        if malformed is not None:
            raise MalformedMessageError(*malformed)
        return delivery

    def _quarantine_message(
        self, row: sqlite3.Row, reason: str, now: float
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO quarantined_messages (
                original_message_id, task_id, room_id, payload_json,
                reason, quarantined_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["message_id"],
                row["task_id"],
                row["room_id"],
                row["payload_json"],
                reason,
                now,
            ),
        )
        self._connection.execute(
            "DELETE FROM messages WHERE message_id = ?", (row["message_id"],)
        )
        self._connection.execute(
            """
            UPDATE tasks
            SET state = ?, blocked_reason = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                TaskState.BLOCKED.value,
                "internal_corruption",
                now,
                row["task_id"],
            ),
        )
        self._activate_next(row["room_id"], now)

    def reply(
        self,
        task_id: str,
        agent: AgentName,
        outcome: TaskOutcome,
        body: str,
    ) -> ReplyResult:
        now = self._clock()
        try:
            with self._mutation():
                task = self._connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise TaskConflictError(f"unknown task {task_id}")
                if task["recipient"] != agent.value:
                    raise TaskConflictError("only the task recipient may reply")

                existing = self._connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE task_id = ? AND round_no = ? AND message_type = 'reply'
                    ORDER BY sequence
                    LIMIT 1
                    """,
                    (task_id, task["round_no"]),
                ).fetchone()
                if existing is not None:
                    if existing["outcome"] == outcome.value and existing["body"] == body:
                        return ReplyResult(
                            existing["message_id"],
                            task_id,
                            TaskState(task["state"]),
                        )
                    raise TaskConflictError("task round already has a different reply")

                if task["state"] != TaskState.WORKING.value:
                    raise TaskConflictError(
                        f"task cannot be replied to while {task['state']}"
                    )
                request = self._connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE task_id = ? AND round_no = ? AND message_type = 'request'
                    """,
                    (task_id, task["round_no"]),
                ).fetchone()
                if request is None:
                    raise TaskConflictError("task request is missing")

                is_terminal = outcome is not TaskOutcome.CHECKPOINT_NEEDED
                next_state = (
                    _TERMINAL_STATES[outcome]
                    if is_terminal
                    else TaskState.WAITING_CHECKPOINT
                )
                message_id = _uuid4()
                self._connection.execute(
                    """
                    UPDATE messages
                    SET acknowledged_at = ?
                    WHERE message_id = ?
                    """,
                    (now, request["message_id"]),
                )
                self._connection.execute(
                    """
                    INSERT INTO messages (
                        message_id, task_id, room_id, sequence, round_no,
                        message_type, sender, recipient, body, outcome,
                        payload_json, idempotency_key, is_terminal, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'reply', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        task_id,
                        task["room_id"],
                        self._next_message_sequence(task_id),
                        task["round_no"],
                        agent.value,
                        task["sender"],
                        body,
                        outcome.value,
                        _json_dump({"outcome": outcome.value, "body": body}),
                        f"{task_id}:round:{task['round_no']}:reply",
                        int(is_terminal),
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE tasks
                    SET state = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (next_state.value, now, task_id),
                )
                if (
                    task["kind"] == TaskKind.CONTEXT_CHECK.value
                    and outcome is TaskOutcome.COMPACT_READY
                ):
                    self._connection.execute(
                        """
                        UPDATE context_check_state
                        SET awaiting_reset = 1, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (now, task_id),
                    )
                if is_terminal:
                    self._activate_next(task["room_id"], now)
                return ReplyResult(message_id, task_id, next_state)
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise DatabaseBusyError("database remained busy for 5000 ms") from error
            raise

    def status(
        self, room_id: str, *, stale_after_seconds: float = 15.0
    ) -> RoomStatus:
        now = self._clock()
        rows = {
            AgentName(row["agent"]): row
            for row in self._connection.execute(
                "SELECT * FROM members WHERE room_id = ?", (room_id,)
            )
        }
        members: dict[AgentName, MemberView] = {}
        for agent in AgentName:
            row = rows.get(agent)
            if row is None:
                members[agent] = MemberView(agent, False, False, None)
                continue
            is_joined = row["left_at"] is None
            is_fresh = (now - row["last_heartbeat"]) <= stale_after_seconds
            members[agent] = MemberView(
                agent=agent,
                is_joined=is_joined,
                is_waiting=bool(row["is_waiting"]) and is_joined and is_fresh,
                last_heartbeat=row["last_heartbeat"],
            )

        active = self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE room_id = ? AND state IN (?, ?)
            ORDER BY fifo_sequence
            LIMIT 1
            """,
            (room_id, *_ACTIVE_STATES),
        ).fetchone()
        return RoomStatus(
            room_id=room_id,
            members=members,
            active_task=None if active is None else self._task_from_row(active),
        )
