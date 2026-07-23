"""Durable room-binding registry contracts."""

from __future__ import annotations

import importlib
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import ai_room.paths as paths
from ai_room.domain import AgentName


class FakeClock:
    def __init__(self, current: float = 1_000.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current


def _bindings():
    return importlib.import_module("ai_room.bindings")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def registry(tmp_path: Path, clock: FakeClock):
    bindings = _bindings()
    opened = bindings.BindingRegistry.open(
        tmp_path / "bindings" / "index.sqlite3",
        clock,
    )
    yield opened
    opened.close()


def test_registry_uses_independent_v1_schema_and_sqlite_settings(
    registry,
) -> None:
    connection = registry._connection

    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute(
        "SELECT schema_version FROM schema_meta WHERE singleton = 1"
    ).fetchone()[0] == 1


def test_concurrent_first_registry_opens_share_one_created_schema(
    tmp_path: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = _bindings()
    path = tmp_path / "bindings" / "index.sqlite3"
    real_connect = sqlite3.connect
    connect_barrier = threading.Barrier(2)
    opened = []
    errors: list[BaseException] = []

    def synchronized_connect(*args, **kwargs):
        connect_barrier.wait(timeout=5)
        return real_connect(*args, **kwargs)

    def open_registry() -> None:
        try:
            opened.append(bindings.BindingRegistry.open(path, clock))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(sqlite3, "connect", synchronized_connect)
    workers = [threading.Thread(target=open_registry) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    try:
        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(opened) == 2
        for registry in opened:
            assert registry._connection.execute(
                "SELECT schema_version FROM schema_meta WHERE singleton = 1"
            ).fetchone()[0] == 1
    finally:
        for registry in opened:
            registry.close()


def test_reserve_pending_activate_and_resolve_exact_binding(registry) -> None:
    bindings = _bindings()
    pending = registry.reserve_binding(
        "c:/项目/工作树",
        AgentName.CODEX,
        "线程-一",
        "评审室",
    )

    assert pending.state is bindings.BindingState.PENDING
    with pytest.raises(bindings.RoomJoinIncompleteError):
        registry.resolve_active_binding(
            "c:/项目/工作树",
            AgentName.CODEX,
            "线程-一",
        )

    active = registry.activate_binding(
        "c:/项目/工作树",
        AgentName.CODEX,
        "线程-一",
        "评审室",
    )

    assert active.state is bindings.BindingState.ACTIVE
    assert registry.resolve_active_binding(
        "c:/项目/工作树",
        AgentName.CODEX,
        "线程-一",
    ) == active


def test_same_reservation_repairs_pending_but_different_room_conflicts(
    registry,
) -> None:
    bindings = _bindings()
    first = registry.reserve_binding(
        "c:/repo",
        AgentName.CODEX,
        "session",
        "review",
    )
    repeated = registry.reserve_binding(
        "c:/repo",
        AgentName.CODEX,
        "session",
        "review",
    )

    assert repeated == first
    with pytest.raises(bindings.RoomBindingConflictError):
        registry.reserve_binding(
            "c:/repo",
            AgentName.CODEX,
            "session",
            "another-room",
        )


def test_fresh_session_is_missing_and_sessions_can_bind_distinct_rooms(
    registry,
) -> None:
    bindings = _bindings()
    first = registry.reserve_binding(
        "c:/repo",
        AgentName.CODEX,
        "session-a",
        "room-a",
    )
    second = registry.reserve_binding(
        "c:/repo",
        AgentName.CODEX,
        "session-b",
        "room-b",
    )

    assert first.explicit_name == "room-a"
    assert second.explicit_name == "room-b"
    with pytest.raises(bindings.RoomBindingMissingError):
        registry.resolve_active_binding(
            "c:/repo",
            AgentName.CODEX,
            "fresh-session",
        )


def test_leave_lookup_accepts_pending_and_delete_allows_rejoin(registry) -> None:
    bindings = _bindings()
    registry.reserve_binding(
        "c:/repo",
        AgentName.CLAUDE,
        "session",
        "old-room",
    )

    pending = registry.resolve_binding_for_leave(
        "c:/repo",
        AgentName.CLAUDE,
        "session",
    )
    assert pending.state is bindings.BindingState.PENDING
    assert registry.delete_binding(
        "c:/repo",
        AgentName.CLAUDE,
        "session",
    )

    replacement = registry.reserve_binding(
        "c:/repo",
        AgentName.CLAUDE,
        "session",
        "new-room",
    )
    assert replacement.explicit_name == "new-room"


def test_root_key_reuses_one_normalizer_for_chinese_and_case_variants(
    tmp_path: Path,
) -> None:
    first = paths.normalize_root(Path(r"C:\Users\Coder\中文项目\WorkTree"))
    second = paths.normalize_root(Path(r"c:\users\coder\中文项目\worktree"))

    assert first == second
    assert "中文项目" in paths.normalize_root(tmp_path / "中文项目")


def test_corrupt_registry_fails_closed_without_mutation(
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    bindings = _bindings()
    path = tmp_path / "bindings" / "index.sqlite3"
    path.parent.mkdir()
    original = b"not sqlite\x00\xff"
    path.write_bytes(original)

    with pytest.raises(
        bindings.BindingDatabaseOpenError,
        match="index.sqlite3",
    ) as caught:
        bindings.BindingRegistry.open(path, clock)

    assert str(tmp_path) not in str(caught.value)
    assert path.read_bytes() == original


def test_unsupported_registry_schema_fails_closed_without_mutation(
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    bindings = _bindings()
    path = tmp_path / "bindings" / "index.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta "
        "(singleton INTEGER PRIMARY KEY, schema_version INTEGER)"
    )
    connection.execute("INSERT INTO schema_meta VALUES (1, 99)")
    connection.commit()
    connection.close()
    original = path.read_bytes()

    with pytest.raises(bindings.BindingSchemaVersionError, match="99"):
        bindings.BindingRegistry.open(path, clock)

    assert path.read_bytes() == original
    assert not path.with_name("index.sqlite3-wal").exists()


def test_v1_registry_missing_binding_table_is_corrupt_and_not_mutated(
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    bindings = _bindings()
    path = tmp_path / "bindings" / "index.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta "
        "(singleton INTEGER PRIMARY KEY, schema_version INTEGER)"
    )
    connection.execute("INSERT INTO schema_meta VALUES (1, 1)")
    connection.commit()
    connection.close()
    original = path.read_bytes()

    with pytest.raises(bindings.BindingDatabaseOpenError):
        bindings.BindingRegistry.open(path, clock)

    assert path.read_bytes() == original
    assert not path.with_name("index.sqlite3-wal").exists()


@pytest.mark.parametrize(
    ("schema_meta_sql", "room_bindings_sql"),
    [
        (
            """
            CREATE TABLE schema_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE room_bindings (
                root_key TEXT NOT NULL,
                agent TEXT NOT NULL CHECK (agent IN ('codex', 'claude')),
                session_id TEXT NOT NULL,
                explicit_name TEXT,
                state TEXT NOT NULL CHECK (state IN ('pending', 'active')),
                updated_at REAL NOT NULL
            )
            """,
        ),
        (
            """
            CREATE TABLE schema_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE room_bindings (
                root_key TEXT NOT NULL,
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                explicit_name TEXT,
                state TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (root_key, agent, session_id)
            )
            """,
        ),
        (
            """
            CREATE TABLE schema_meta (
                singleton INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE room_bindings (
                root_key TEXT NOT NULL,
                agent TEXT NOT NULL CHECK (agent IN ('codex', 'claude')),
                session_id TEXT NOT NULL,
                explicit_name TEXT,
                state TEXT NOT NULL CHECK (state IN ('pending', 'active')),
                updated_at REAL NOT NULL,
                PRIMARY KEY (root_key, agent, session_id)
            )
            """,
        ),
    ],
    ids=("missing-primary-key", "missing-enum-checks", "weak-schema-meta"),
)
def test_v1_registry_rejects_weakened_schema_constraints(
    tmp_path: Path,
    clock: FakeClock,
    schema_meta_sql: str,
    room_bindings_sql: str,
) -> None:
    bindings = _bindings()
    path = tmp_path / "bindings" / "index.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute(schema_meta_sql)
    connection.execute(room_bindings_sql)
    connection.execute("INSERT INTO schema_meta VALUES (1, 1)")
    connection.commit()
    connection.close()

    with pytest.raises(bindings.BindingDatabaseOpenError):
        bindings.BindingRegistry.open(path, clock)


@pytest.mark.parametrize(
    ("agent", "state"),
    [
        ("unknown-agent", "active"),
        ("codex", "unknown-state"),
    ],
)
def test_row_decoder_classifies_invalid_persisted_enums(
    agent: str,
    state: str,
) -> None:
    bindings = _bindings()
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT
            'c:/repo' AS root_key,
            ? AS agent,
            'session' AS session_id,
            NULL AS explicit_name,
            ? AS state,
            1000.0 AS updated_at
        """,
        (agent, state),
    ).fetchone()
    try:
        with pytest.raises(bindings.BindingDatabaseOpenError):
            bindings._binding_from_row(row)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("root_key", None),
        ("session_id", 7),
        ("explicit_name", 7),
        ("updated_at", "not-a-number"),
    ],
)
def test_row_decoder_classifies_invalid_persisted_types(
    column: str,
    value: object,
) -> None:
    bindings = _bindings()
    values = {
        "root_key": "c:/repo",
        "agent": "codex",
        "session_id": "session",
        "explicit_name": None,
        "state": "active",
        "updated_at": 1000.0,
    }
    values[column] = value
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT
            ? AS root_key,
            ? AS agent,
            ? AS session_id,
            ? AS explicit_name,
            ? AS state,
            ? AS updated_at
        """,
        tuple(values.values()),
    ).fetchone()
    try:
        with pytest.raises(bindings.BindingDatabaseOpenError):
            bindings._binding_from_row(row)
    finally:
        connection.close()


def test_busy_registry_mutation_fails_after_configured_timeout(
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    bindings = _bindings()
    path = tmp_path / "bindings" / "index.sqlite3"
    registry = bindings.BindingRegistry.open(path, clock)
    lock = sqlite3.connect(path, timeout=0, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(bindings.BindingDatabaseBusyError):
            registry.reserve_binding(
                "c:/repo",
                AgentName.CODEX,
                "session",
                None,
            )
        assert time.monotonic() - started >= 4.5
    finally:
        lock.rollback()
        lock.close()
        registry.close()
