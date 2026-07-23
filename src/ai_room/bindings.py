"""Durable exact-session bindings from worktree roots to room names."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .domain import AgentName


BINDING_SCHEMA_VERSION = 1


class BindingState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"


@dataclass(frozen=True)
class RoomBinding:
    root_key: str
    agent: AgentName
    session_id: str
    explicit_name: str | None
    state: BindingState
    updated_at: float


class BindingError(RuntimeError):
    """Base class for binding-registry failures."""


class RoomBindingMissingError(BindingError):
    """The exact root/agent/session has never joined a room."""


class RoomJoinIncompleteError(BindingError):
    """A prior join reserved its room but did not activate membership."""


class RoomBindingConflictError(BindingError):
    """The exact session is already bound to a different room name."""


class RoomDatabaseMissingError(BindingError):
    """An active binding points to a room database that no longer exists."""


class BindingDatabaseOpenError(BindingError):
    """The fixed binding registry cannot be opened safely."""


class BindingSchemaVersionError(BindingError):
    """The fixed binding registry uses an unsupported schema."""


class BindingDatabaseBusyError(BindingError):
    """A binding mutation could not acquire its short transaction."""


_SCHEMA = """
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
);

CREATE TABLE room_bindings (
    root_key TEXT NOT NULL,
    agent TEXT NOT NULL CHECK (agent IN ('codex', 'claude')),
    session_id TEXT NOT NULL,
    explicit_name TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'active')),
    updated_at REAL NOT NULL,
    PRIMARY KEY (root_key, agent, session_id)
);
"""


class BindingRegistry:
    """SQLite adapter for exact current-session room bindings."""

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
    def open(
        cls,
        path: Path,
        clock: Callable[[], float],
    ) -> BindingRegistry:
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
                    f"VALUES (1, {BINDING_SCHEMA_VERSION});\n"
                    "COMMIT;\n"
                )
            return cls(connection, path, clock)
        except BindingSchemaVersionError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error as error:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            raise BindingDatabaseOpenError(
                f"cannot open binding registry {path.name}: "
                f"{type(error).__name__}"
            ) from error

    @staticmethod
    def _verify_existing_schema(connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT schema_version FROM schema_meta WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error as error:
            if "no such table" in str(error).lower():
                raise BindingSchemaVersionError(
                    "unsupported binding schema version: missing"
                ) from error
            raise
        if row is None or row[0] != BINDING_SCHEMA_VERSION:
            found = "missing" if row is None else row[0]
            raise BindingSchemaVersionError(
                f"unsupported binding schema version: {found}"
            )
        connection.execute(
            """
            SELECT root_key, agent, session_id, explicit_name, state, updated_at
            FROM room_bindings
            LIMIT 0
            """
        )

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        with self._mutation_lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if _is_busy(error):
                    raise BindingDatabaseBusyError(
                        "binding registry remained busy for 5000 ms"
                    ) from error
                raise
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def reserve_binding(
        self,
        root_key: str,
        agent: AgentName,
        session_id: str,
        explicit_name: str | None,
    ) -> RoomBinding:
        _validate_key(root_key, session_id)
        now = self._clock()
        with self._mutation():
            existing = self._select(root_key, agent, session_id)
            if existing is not None:
                binding = _binding_from_row(existing)
                if binding.explicit_name != explicit_name:
                    raise RoomBindingConflictError(
                        "this session is already bound to a different room; "
                        "leave it before joining another room"
                    )
                return binding
            self._connection.execute(
                """
                INSERT INTO room_bindings (
                    root_key, agent, session_id, explicit_name, state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    root_key,
                    agent.value,
                    session_id,
                    explicit_name,
                    BindingState.PENDING.value,
                    now,
                ),
            )
            row = self._select(root_key, agent, session_id)
            return _binding_from_row(row)

    def activate_binding(
        self,
        root_key: str,
        agent: AgentName,
        session_id: str,
        explicit_name: str | None,
    ) -> RoomBinding:
        _validate_key(root_key, session_id)
        now = self._clock()
        with self._mutation():
            row = self._select(root_key, agent, session_id)
            if row is None:
                raise RoomBindingMissingError(
                    "room binding disappeared before join activation"
                )
            binding = _binding_from_row(row)
            if binding.explicit_name != explicit_name:
                raise RoomBindingConflictError(
                    "reserved room does not match join activation"
                )
            self._connection.execute(
                """
                UPDATE room_bindings
                SET state = ?, updated_at = ?
                WHERE root_key = ? AND agent = ? AND session_id = ?
                """,
                (
                    BindingState.ACTIVE.value,
                    now,
                    root_key,
                    agent.value,
                    session_id,
                ),
            )
            updated = self._select(root_key, agent, session_id)
            return _binding_from_row(updated)

    def resolve_active_binding(
        self,
        root_key: str,
        agent: AgentName,
        session_id: str,
    ) -> RoomBinding:
        binding = self._resolve(root_key, agent, session_id)
        if binding.state is BindingState.PENDING:
            raise RoomJoinIncompleteError(
                "room join is incomplete; repeat the same ai-room join command"
            )
        return binding

    def resolve_binding_for_leave(
        self,
        root_key: str,
        agent: AgentName,
        session_id: str,
    ) -> RoomBinding:
        return self._resolve(root_key, agent, session_id)

    def delete_binding(
        self,
        root_key: str,
        agent: AgentName,
        session_id: str,
    ) -> bool:
        with self._mutation():
            cursor = self._connection.execute(
                """
                DELETE FROM room_bindings
                WHERE root_key = ? AND agent = ? AND session_id = ?
                """,
                (root_key, agent.value, session_id),
            )
        return cursor.rowcount == 1

    def _resolve(
        self,
        root_key: str,
        agent: AgentName,
        session_id: str,
    ) -> RoomBinding:
        _validate_key(root_key, session_id)
        row = self._select(root_key, agent, session_id)
        if row is None:
            raise RoomBindingMissingError(
                "no room binding exists for this session"
            )
        return _binding_from_row(row)

    def _select(
        self,
        root_key: str,
        agent: AgentName,
        session_id: str,
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT root_key, agent, session_id, explicit_name, state, updated_at
            FROM room_bindings
            WHERE root_key = ? AND agent = ? AND session_id = ?
            """,
            (root_key, agent.value, session_id),
        ).fetchone()


def binding_registry_path(runtime_directory: Path) -> Path:
    """Return the fixed registry path below the approved runtime root."""
    return Path(runtime_directory) / "bindings" / "index.sqlite3"


def _binding_from_row(row: sqlite3.Row | None) -> RoomBinding:
    if row is None:
        raise BindingDatabaseOpenError("binding registry row is missing")
    return RoomBinding(
        root_key=row["root_key"],
        agent=AgentName(row["agent"]),
        session_id=row["session_id"],
        explicit_name=row["explicit_name"],
        state=BindingState(row["state"]),
        updated_at=row["updated_at"],
    )


def _validate_key(root_key: str, session_id: str) -> None:
    if not root_key:
        raise ValueError("root_key must not be empty")
    if not session_id:
        raise ValueError("session_id must not be empty")


def _is_busy(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message
