"""Durable exact-session bindings from worktree roots to room names."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path

from .domain import AgentName
from .paths import validate_explicit_room_name


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


_SCHEMA_META_SQL = """
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
)
"""

_ROOM_BINDINGS_SQL = """
CREATE TABLE room_bindings (
    root_key TEXT NOT NULL,
    agent TEXT NOT NULL CHECK (agent IN ('codex', 'claude')),
    session_id TEXT NOT NULL,
    explicit_name TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'active')),
    updated_at REAL NOT NULL,
    PRIMARY KEY (root_key, agent, session_id)
)
"""

_EXPECTED_TABLE_SQL = {
    "schema_meta": _SCHEMA_META_SQL,
    "room_bindings": _ROOM_BINDINGS_SQL,
}


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
        connection: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            cls._initialize_or_verify_schema(connection)
            connection.execute("PRAGMA journal_mode=WAL")
            return cls(connection, path, clock)
        except BindingError:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            raise
        except sqlite3.Error as error:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            if _is_busy(error):
                raise BindingDatabaseBusyError(
                    "binding registry remained busy for 5000 ms"
                ) from error
            raise BindingDatabaseOpenError(
                f"cannot open binding registry {path.name}: "
                f"{type(error).__name__}"
            ) from error

    @classmethod
    def _initialize_or_verify_schema(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            schema_objects = connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%'
                LIMIT 1
                """
            ).fetchone()
            if schema_objects is None:
                connection.execute(_SCHEMA_META_SQL)
                connection.execute(_ROOM_BINDINGS_SQL)
                connection.execute(
                    """
                    INSERT INTO schema_meta (singleton, schema_version)
                    VALUES (1, ?)
                    """,
                    (BINDING_SCHEMA_VERSION,),
                )
            else:
                cls._verify_existing_schema(connection)
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def _verify_existing_schema(connection: sqlite3.Connection) -> None:
        try:
            rows = connection.execute(
                "SELECT singleton, schema_version FROM schema_meta"
            ).fetchall()
        except sqlite3.Error as error:
            if "no such table" in str(error).lower():
                raise BindingSchemaVersionError(
                    "unsupported binding schema version: missing"
                ) from error
            raise
        version_row = next(
            (
                row
                for row in rows
                if type(row["singleton"]) is int and row["singleton"] == 1
            ),
            None,
        )
        if (
            version_row is None
            or type(version_row["schema_version"]) is not int
            or version_row["schema_version"] != BINDING_SCHEMA_VERSION
        ):
            found = (
                "missing"
                if version_row is None
                else version_row["schema_version"]
            )
            raise BindingSchemaVersionError(
                f"unsupported binding schema version: {found}"
            )
        if len(rows) != 1:
            raise BindingDatabaseOpenError(
                "binding registry schema metadata is invalid"
            )

        schema_rows = connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
        schema_objects = {
            (row["type"], row["name"]): row["sql"] for row in schema_rows
        }
        expected_objects = {
            ("table", table_name) for table_name in _EXPECTED_TABLE_SQL
        }
        if set(schema_objects) != expected_objects:
            raise BindingDatabaseOpenError(
                "binding registry contains unexpected schema objects"
            )
        for table_name, expected_sql in _EXPECTED_TABLE_SQL.items():
            actual_sql = schema_objects[("table", table_name)]
            if (
                not isinstance(actual_sql, str)
                or _normalize_schema_sql(actual_sql)
                != _normalize_schema_sql(expected_sql)
            ):
                raise BindingDatabaseOpenError(
                    f"binding registry table {table_name} is invalid"
                )

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        with self._mutation_lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                _raise_binding_sqlite_error(error)
            try:
                yield
                self._connection.commit()
            except BaseException as error:
                if self._connection.in_transaction:
                    try:
                        self._connection.rollback()
                    except sqlite3.Error:
                        pass
                if isinstance(error, sqlite3.Error):
                    _raise_binding_sqlite_error(error)
                raise

    def reserve_binding(
        self,
        root_key: str,
        agent: AgentName,
        session_id: str,
        explicit_name: str | None,
    ) -> RoomBinding:
        _validate_key(root_key, session_id)
        validate_explicit_room_name(explicit_name)
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
        validate_explicit_room_name(explicit_name)
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
        try:
            return self._connection.execute(
                """
                SELECT root_key, agent, session_id, explicit_name, state, updated_at
                FROM room_bindings
                WHERE root_key = ? AND agent = ? AND session_id = ?
                """,
                (root_key, agent.value, session_id),
            ).fetchone()
        except sqlite3.Error as error:
            _raise_binding_sqlite_error(error)


def binding_registry_path(runtime_directory: Path) -> Path:
    """Return the fixed registry path below the approved runtime root."""
    return Path(runtime_directory) / "bindings" / "index.sqlite3"


def _binding_from_row(row: sqlite3.Row | None) -> RoomBinding:
    if row is None:
        raise BindingDatabaseOpenError("binding registry row is missing")
    try:
        root_key = row["root_key"]
        agent = row["agent"]
        session_id = row["session_id"]
        explicit_name = row["explicit_name"]
        state = row["state"]
        updated_at = row["updated_at"]
        if not isinstance(root_key, str) or not root_key:
            raise TypeError("invalid root key")
        if not isinstance(agent, str):
            raise TypeError("invalid agent")
        if not isinstance(session_id, str) or not session_id:
            raise TypeError("invalid session ID")
        if explicit_name is not None and not isinstance(explicit_name, str):
            raise TypeError("invalid room name")
        validate_explicit_room_name(explicit_name)
        if not isinstance(state, str):
            raise TypeError("invalid binding state")
        if (
            isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not isfinite(float(updated_at))
        ):
            raise TypeError("invalid update time")
        return RoomBinding(
            root_key=root_key,
            agent=AgentName(agent),
            session_id=session_id,
            explicit_name=explicit_name,
            state=BindingState(state),
            updated_at=float(updated_at),
        )
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise BindingDatabaseOpenError(
            "binding registry contains invalid row data"
        ) from error


def _validate_key(root_key: str, session_id: str) -> None:
    if not root_key:
        raise ValueError("root_key must not be empty")
    if not session_id:
        raise ValueError("session_id must not be empty")


def _is_busy(error: sqlite3.Error) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _raise_binding_sqlite_error(error: sqlite3.Error) -> None:
    if _is_busy(error):
        raise BindingDatabaseBusyError(
            "binding registry remained busy for 5000 ms"
        ) from error
    raise BindingDatabaseOpenError(
        "binding registry operation failed: "
        f"{type(error).__name__}"
    ) from error


def _normalize_schema_sql(sql: str) -> str:
    return "".join(sql.lower().split())
