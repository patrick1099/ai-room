"""Durable SQLite storage and task state-machine tests."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from ai_room.domain import (
    AgentName,
    ContextSample,
    ContextSource,
    RoomRef,
    TaskKind,
    TaskOutcome,
    TaskRequest,
    TaskState,
)
from ai_room.storage import (
    DatabaseBusyError,
    DatabaseOpenError,
    MalformedMessageError,
    PeerNotJoinedError,
    SQLiteStore,
    SchemaVersionError,
    TaskConflictError,
)


class FakeClock:
    def __init__(self, current: float = 1_000.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def room(tmp_path: Path) -> RoomRef:
    return RoomRef("room-a", tmp_path / "中文项目", "评审室")


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> SQLiteStore:
    opened = SQLiteStore.open(tmp_path / "运行数据.sqlite3", clock)
    yield opened
    opened.close()


@pytest.fixture
def joined_store(store: SQLiteStore, room: RoomRef) -> SQLiteStore:
    store.join_member(room, AgentName.CODEX)
    store.join_member(room, AgentName.CLAUDE)
    return store


@pytest.fixture
def task_request(room: RoomRef) -> TaskRequest:
    return TaskRequest(
        room_id=room.room_id,
        sender=AgentName.CODEX,
        recipient=AgentName.CLAUDE,
        kind=TaskKind.DESIGN_REVIEW,
        question="这里应该采用方案 A 还是方案 B？",
        related_docs=(r"docs\架构 设计.md",),
        writable_docs=(r"docs\评审结果.md",),
        context=ContextSample(
            input_tokens=150_000,
            context_window=258_000,
            source=ContextSource.CODEX_TOKEN_COUNT,
            session_id="线程-一",
        ),
        checkpoint_docs=(r"docs\工作节点.md",),
        next_entry="继续实现存储层",
        idempotency_key="request-一",
    )


def join_both(store: SQLiteStore, room: RoomRef) -> None:
    store.join_member(room, AgentName.CODEX)
    store.join_member(room, AgentName.CLAUDE)


def test_new_database_uses_required_sqlite_settings(
    store: SQLiteStore,
) -> None:
    assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_contains_required_tables_and_indexes(store: SQLiteStore) -> None:
    names = {
        row[0]
        for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        )
    }

    assert {
        "schema_meta",
        "rooms",
        "members",
        "tasks",
        "messages",
        "context_samples",
        "quarantined_messages",
        "uq_messages_terminal_reply",
    } <= names


def test_store_reopens_with_tasks_intact(
    tmp_path: Path,
    clock: FakeClock,
    room: RoomRef,
    task_request: TaskRequest,
) -> None:
    path = tmp_path / "重启.sqlite3"
    first_store = SQLiteStore.open(path, clock)
    join_both(first_store, room)
    created = first_store.enqueue_task(task_request)
    first_store.close()

    reopened = SQLiteStore.open(path, clock)
    try:
        restored = reopened.get_task(created.task_id)
        assert restored.request == task_request
        assert restored.state is TaskState.WORKING
    finally:
        reopened.close()


def test_non_database_file_is_refused_without_mutation(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "不可读.sqlite3"
    original = b"not a sqlite database\x00\xff"
    path.write_bytes(original)

    with pytest.raises(DatabaseOpenError, match="不可读.sqlite3") as caught:
        SQLiteStore.open(path, clock)

    assert str(tmp_path) not in str(caught.value)
    assert path.read_bytes() == original
    assert not path.with_name(path.name + "-wal").exists()


def test_schema_version_mismatch_is_refused_without_mutation(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta (singleton INTEGER PRIMARY KEY, schema_version INTEGER)"
    )
    connection.execute("INSERT INTO schema_meta VALUES (1, 99)")
    connection.commit()
    connection.close()
    original = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="99"):
        SQLiteStore.open(path, clock)

    assert path.read_bytes() == original
    assert not path.with_name(path.name + "-wal").exists()


def test_real_write_lock_maps_to_database_busy_error(
    tmp_path: Path,
    clock: FakeClock,
    room: RoomRef,
) -> None:
    path = tmp_path / "locked.sqlite3"
    store = SQLiteStore.open(path, clock)
    lock = sqlite3.connect(path, timeout=0, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(DatabaseBusyError):
            store.join_member(room, AgentName.CODEX)
        elapsed = time.monotonic() - started
        assert elapsed >= 4.5
    finally:
        lock.rollback()
        lock.close()
        store.close()


def test_rooms_keep_members_and_tasks_distinct(
    store: SQLiteStore,
    tmp_path: Path,
    task_request: TaskRequest,
) -> None:
    first = RoomRef("room-a", tmp_path / "一")
    second = RoomRef("room-b", tmp_path / "二")
    join_both(store, first)
    join_both(store, second)

    first_task = store.enqueue_task(replace(task_request, room_id=first.room_id))
    second_task = store.enqueue_task(
        replace(task_request, room_id=second.room_id, idempotency_key="request-二")
    )

    assert store.status(first.room_id).active_task.task_id == first_task.task_id
    assert store.status(second.room_id).active_task.task_id == second_task.task_id


def test_member_status_distinguishes_join_wait_stale_and_leave(
    store: SQLiteStore,
    room: RoomRef,
    clock: FakeClock,
) -> None:
    before = store.status(room.room_id, stale_after_seconds=15)
    assert before.members[AgentName.CODEX].is_joined is False

    store.join_member(room, AgentName.CODEX)
    joined = store.status(room.room_id, stale_after_seconds=15)
    assert joined.members[AgentName.CODEX].is_joined is True
    assert joined.members[AgentName.CODEX].is_waiting is False

    store.heartbeat(room.room_id, AgentName.CODEX, is_waiting=True)
    waiting = store.status(room.room_id, stale_after_seconds=15)
    assert waiting.members[AgentName.CODEX].is_waiting is True

    clock.advance(16)
    stale = store.status(room.room_id, stale_after_seconds=15)
    assert stale.members[AgentName.CODEX].is_joined is True
    assert stale.members[AgentName.CODEX].is_waiting is False

    store.leave_member(room.room_id, AgentName.CODEX)
    left = store.status(room.room_id, stale_after_seconds=15)
    assert left.members[AgentName.CODEX].is_joined is False


def test_enqueue_rejects_a_peer_that_has_never_joined(
    store: SQLiteStore,
    room: RoomRef,
    task_request: TaskRequest,
) -> None:
    store.join_member(room, AgentName.CODEX)

    with pytest.raises(PeerNotJoinedError):
        store.enqueue_task(task_request)


def test_task_and_message_ids_are_uuid4_strings(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    task = joined_store.enqueue_task(task_request)
    delivery = joined_store.claim_next_message(
        task.room_id, AgentName.CLAUDE, lease_seconds=60
    )

    assert uuid.UUID(task.task_id).version == 4
    assert delivery is not None
    assert uuid.UUID(delivery.message_id).version == 4


def test_duplicate_task_idempotency_key_returns_existing_task(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    first = joined_store.enqueue_task(task_request)
    second = joined_store.enqueue_task(task_request)

    assert second.task_id == first.task_id


def test_duplicate_task_key_with_different_payload_conflicts(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    joined_store.enqueue_task(task_request)

    with pytest.raises(TaskConflictError):
        joined_store.enqueue_task(
            replace(task_request, question="同一个键却是另一个问题")
        )


def test_second_task_stays_queued_until_first_finishes(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    first = joined_store.enqueue_task(task_request)
    second = joined_store.enqueue_task(
        replace(task_request, idempotency_key="request-二", question="第二个问题")
    )

    assert joined_store.get_task(first.task_id).state is TaskState.WORKING
    assert joined_store.get_task(second.task_id).state is TaskState.QUEUED


def test_terminal_reply_atomically_activates_next_fifo_task(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    first = joined_store.enqueue_task(task_request)
    second = joined_store.enqueue_task(
        replace(task_request, idempotency_key="request-二", question="第二个问题")
    )
    third = joined_store.enqueue_task(
        replace(task_request, idempotency_key="request-三", question="第三个问题")
    )
    delivery = joined_store.claim_next_message(
        first.room_id, AgentName.CLAUDE, lease_seconds=60
    )

    joined_store.reply(
        delivery.task_id, AgentName.CLAUDE, TaskOutcome.DONE, "采用方案 A"
    )

    assert joined_store.get_task(first.task_id).state is TaskState.DONE
    assert joined_store.get_task(second.task_id).state is TaskState.WORKING
    assert joined_store.get_task(third.task_id).state is TaskState.QUEUED
    next_delivery = joined_store.claim_next_message(
        first.room_id, AgentName.CLAUDE, lease_seconds=60
    )
    assert next_delivery is not None
    assert next_delivery.task_id == second.task_id


def test_checkpoint_reply_keeps_next_task_queued(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    first = joined_store.enqueue_task(task_request)
    second = joined_store.enqueue_task(
        replace(task_request, idempotency_key="request-二")
    )
    delivery = joined_store.claim_next_message(
        first.room_id, AgentName.CLAUDE, lease_seconds=60
    )

    joined_store.reply(
        delivery.task_id,
        AgentName.CLAUDE,
        TaskOutcome.CHECKPOINT_NEEDED,
        "请先补齐工作节点",
    )

    assert joined_store.get_task(first.task_id).state is TaskState.WAITING_CHECKPOINT
    assert joined_store.get_task(second.task_id).state is TaskState.QUEUED


def test_unacknowledged_delivery_is_redelivered_only_after_lease_expiry(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
    clock: FakeClock,
) -> None:
    task = joined_store.enqueue_task(task_request)
    first = joined_store.claim_next_message(
        task.room_id, AgentName.CLAUDE, lease_seconds=60
    )
    assert first is not None
    assert joined_store.claim_next_message(
        task.room_id, AgentName.CLAUDE, lease_seconds=60
    ) is None

    clock.advance(61)
    second = joined_store.claim_next_message(
        task.room_id, AgentName.CLAUDE, lease_seconds=60
    )

    assert second is not None
    assert second.message_id == first.message_id
    assert second.lease_token != first.lease_token


def test_reply_is_idempotent(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    task = joined_store.enqueue_task(task_request)
    delivery = joined_store.claim_next_message(
        task.room_id, AgentName.CLAUDE, lease_seconds=60
    )
    first = joined_store.reply(
        delivery.task_id, AgentName.CLAUDE, TaskOutcome.DONE, "采用方案 A"
    )
    second = joined_store.reply(
        delivery.task_id, AgentName.CLAUDE, TaskOutcome.DONE, "采用方案 A"
    )

    assert second.reply_message_id == first.reply_message_id


def test_conflicting_second_reply_is_rejected(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    task = joined_store.enqueue_task(task_request)
    delivery = joined_store.claim_next_message(
        task.room_id, AgentName.CLAUDE, lease_seconds=60
    )
    joined_store.reply(
        delivery.task_id, AgentName.CLAUDE, TaskOutcome.DONE, "采用方案 A"
    )

    with pytest.raises(TaskConflictError):
        joined_store.reply(
            delivery.task_id, AgentName.CLAUDE, TaskOutcome.BLOCKED, "无法决定"
        )


def test_wrong_recipient_cannot_reply(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    task = joined_store.enqueue_task(task_request)

    with pytest.raises(TaskConflictError):
        joined_store.reply(
            task.task_id, AgentName.CODEX, TaskOutcome.DONE, "越权回复"
        )


def test_reply_acknowledges_request_and_is_deliverable_to_sender(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    task = joined_store.enqueue_task(task_request)
    request = joined_store.claim_next_message(
        task.room_id, AgentName.CLAUDE, lease_seconds=60
    )
    result = joined_store.reply(
        task.task_id, AgentName.CLAUDE, TaskOutcome.DONE, "采用方案 A"
    )

    acknowledged_at = joined_store._connection.execute(
        "SELECT acknowledged_at FROM messages WHERE message_id = ?",
        (request.message_id,),
    ).fetchone()[0]
    reply = joined_store.claim_next_message(
        task.room_id, AgentName.CODEX, lease_seconds=60
    )

    assert acknowledged_at is not None
    assert reply is not None
    assert reply.message_id == result.reply_message_id
    assert reply.body == "采用方案 A"


def test_json_paths_and_context_round_trip_strictly(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    created = joined_store.enqueue_task(task_request)

    restored = joined_store.get_task(created.task_id)

    assert restored.request.related_docs == (r"docs\架构 设计.md",)
    assert restored.request.writable_docs == (r"docs\评审结果.md",)
    assert restored.request.context == task_request.context
    raw_payload = joined_store._connection.execute(
        "SELECT payload_json FROM messages WHERE task_id = ? AND message_type = 'request'",
        (created.task_id,),
    ).fetchone()[0]
    assert "架构" in raw_payload
    assert "\\u67b6" not in raw_payload


def test_malformed_request_is_quarantined_and_next_task_activates(
    joined_store: SQLiteStore,
    task_request: TaskRequest,
) -> None:
    first = joined_store.enqueue_task(task_request)
    second = joined_store.enqueue_task(
        replace(task_request, idempotency_key="request-二", question="仍然有效")
    )
    joined_store._connection.execute(
        "UPDATE messages SET payload_json = ? WHERE task_id = ? AND message_type = 'request'",
        (json.dumps({"related_docs": "not-a-list"}), first.task_id),
    )

    with pytest.raises(MalformedMessageError) as caught:
        joined_store.claim_next_message(
            first.room_id, AgentName.CLAUDE, lease_seconds=60
        )

    assert caught.value.message_id
    assert joined_store.get_task(first.task_id).state is TaskState.BLOCKED
    assert joined_store.get_task(first.task_id).blocked_reason == "internal_corruption"
    assert joined_store.get_task(second.task_id).state is TaskState.WORKING
    quarantined = joined_store._connection.execute(
        "SELECT original_message_id, reason FROM quarantined_messages"
    ).fetchone()
    assert quarantined[0] == caught.value.message_id
    valid = joined_store.claim_next_message(
        second.room_id, AgentName.CLAUDE, lease_seconds=60
    )
    assert valid is not None
    assert valid.task_id == second.task_id
