"""Service orchestration tests for blocking delivery and recovery."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ai_room.domain import (
    AgentName,
    ContextSample,
    ContextSource,
    MemberStatus,
    RoomRef,
    TaskKind,
    TaskOutcome,
    TaskRequest,
)
from ai_room.service import AiRoomService
from ai_room.storage import PeerNotJoinedError, SQLiteStore, TaskConflictError


class FakeClock:
    def __init__(self, current: float = 1_000.0) -> None:
        self.current = current
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.current

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.current += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def room(tmp_path: Path) -> RoomRef:
    return RoomRef("room-a", tmp_path / "中文项目", "评审室")


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


@pytest.fixture
def service_pair(
    tmp_path: Path,
    room: RoomRef,
    fake_clock: FakeClock,
):
    database = tmp_path / "service.sqlite3"
    primary_store = SQLiteStore.open(database, fake_clock)
    advisor_store = SQLiteStore.open(database, fake_clock)
    primary = AiRoomService(
        primary_store,
        room,
        AgentName.CODEX,
        session_id="codex-session",
        clock=fake_clock,
        poll_seconds=0.01,
        heartbeat_seconds=5.0,
        stale_seconds=15.0,
        process_id=101,
    )
    advisor = AiRoomService(
        advisor_store,
        room,
        AgentName.CLAUDE,
        session_id="claude-session",
        clock=fake_clock,
        poll_seconds=0.01,
        heartbeat_seconds=5.0,
        stale_seconds=15.0,
        process_id=202,
    )
    primary.join()
    advisor.join()
    yield primary, advisor
    primary_store.close()
    advisor_store.close()


def _wait_in_thread(
    service: AiRoomService,
    cancel_event: threading.Event | None = None,
) -> tuple[threading.Thread, list[object]]:
    cancel = cancel_event or threading.Event()
    results: list[object] = []

    def run() -> None:
        try:
            results.append(service.wait(cancel))
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, results


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition was not reached before timeout")
        time.sleep(0.005)


def test_join_wait_send_reply_wait_round_trip(
    service_pair,
    task_request: TaskRequest,
) -> None:
    primary, advisor = service_pair
    advisor_wait, advisor_results = _wait_in_thread(advisor)
    _wait_until(
        lambda: primary.status().members[AgentName.CLAUDE].status
        is MemberStatus.WAITING
    )

    sent = primary.send(task_request)
    advisor_wait.join(timeout=2)

    assert not advisor_wait.is_alive()
    delivery = advisor_results[0]
    assert delivery.task_id == sent.task_id
    assert delivery.body == task_request.question

    advisor.reply(sent.task_id, TaskOutcome.DONE, "采用方案 A")
    reply = primary.wait(threading.Event())

    assert reply is not None
    assert reply.task_id == sent.task_id
    assert reply.body == "采用方案 A"


def test_cancel_before_claim_does_not_acknowledge_request(
    service_pair,
    task_request: TaskRequest,
) -> None:
    primary, advisor = service_pair
    sent = primary.send(task_request)
    cancelled = threading.Event()
    cancelled.set()

    assert advisor.wait(cancelled) is None
    delivery = advisor.wait(threading.Event())

    assert delivery is not None
    assert delivery.task_id == sent.task_id


def test_cancel_after_request_delivery_does_not_acknowledge_it(
    service_pair,
    task_request: TaskRequest,
    fake_clock: FakeClock,
) -> None:
    primary, advisor = service_pair
    sent = primary.send(task_request)
    first = advisor.wait(threading.Event())
    cancelled = threading.Event()
    cancelled.set()

    assert advisor.wait(cancelled) is None
    fake_clock.advance(16.0)
    redelivered = advisor.wait(threading.Event())

    assert redelivered is not None
    assert redelivered.task_id == sent.task_id
    assert redelivered.message_id == first.message_id
    assert redelivered.lease_token != first.lease_token


def test_process_style_restart_redelivers_expired_reply(
    tmp_path: Path,
    room,
    task_request: TaskRequest,
    fake_clock: FakeClock,
) -> None:
    database = tmp_path / "restart.sqlite3"
    primary_store = SQLiteStore.open(database, fake_clock)
    advisor_store = SQLiteStore.open(database, fake_clock)
    primary = AiRoomService(
        primary_store,
        room,
        AgentName.CODEX,
        session_id="codex-session",
        clock=fake_clock,
        stale_seconds=15.0,
    )
    advisor = AiRoomService(
        advisor_store,
        room,
        AgentName.CLAUDE,
        session_id="claude-session",
        clock=fake_clock,
        stale_seconds=15.0,
    )
    primary.join()
    advisor.join()
    sent = primary.send(task_request)
    request = advisor.wait(threading.Event())
    advisor.reply(sent.task_id, TaskOutcome.DONE, "采用方案 A")
    first_reply = primary.wait(threading.Event())
    assert request is not None
    assert first_reply is not None
    primary_store.close()

    fake_clock.advance(16.0)
    restarted_store = SQLiteStore.open(database, fake_clock)
    restarted = AiRoomService(
        restarted_store,
        room,
        AgentName.CODEX,
        session_id="codex-session",
        clock=fake_clock,
        stale_seconds=15.0,
    )
    try:
        redelivered = restarted.wait(threading.Event())
        assert redelivered is not None
        assert redelivered.message_id == first_reply.message_id
        assert redelivered.lease_token != first_reply.lease_token
    finally:
        restarted_store.close()
        advisor_store.close()


def test_next_command_acknowledges_unexpired_informational_reply(
    service_pair,
    task_request: TaskRequest,
    fake_clock: FakeClock,
) -> None:
    primary, advisor = service_pair
    sent = primary.send(task_request)
    advisor.wait(threading.Event())
    advisor.reply(sent.task_id, TaskOutcome.DONE, "采用方案 A")
    delivered = primary.wait(threading.Event())
    assert delivered is not None

    primary.send(
        replace(
            task_request,
            recipient=AgentName.CLAUDE,
            idempotency_key="ack-trigger",
            question="下一项任务",
        )
    )

    fake_clock.advance(16.0)
    cancelled = threading.Event()
    cancelled.set()
    assert primary.wait(cancelled) is None
    row = primary._store._connection.execute(
        "SELECT acknowledged_at FROM messages WHERE message_id = ?",
        (delivered.message_id,),
    ).fetchone()
    assert row[0] is not None


def test_send_rejects_self_and_peer_that_never_joined(
    tmp_path: Path,
    room,
    task_request: TaskRequest,
    fake_clock: FakeClock,
) -> None:
    store = SQLiteStore.open(tmp_path / "membership.sqlite3", fake_clock)
    primary = AiRoomService(
        store,
        room,
        AgentName.CODEX,
        session_id="codex-session",
        clock=fake_clock,
    )
    primary.join()
    try:
        with pytest.raises(TaskConflictError, match="self"):
            primary.send(
                replace(task_request, recipient=AgentName.CODEX)
            )
        with pytest.raises(PeerNotJoinedError):
            primary.send(task_request)
    finally:
        store.close()


def test_send_permits_peer_that_joined_but_is_not_waiting(
    service_pair,
    task_request: TaskRequest,
) -> None:
    primary, _ = service_pair

    sent = primary.send(task_request)

    assert sent.task_id == primary.status().active_task.task_id
    assert (
        primary.status().members[AgentName.CLAUDE].status
        is MemberStatus.JOINED_NOT_WAITING
    )


def test_wait_updates_heartbeat_and_stale_waiter_is_not_waiting(
    service_pair,
    fake_clock: FakeClock,
) -> None:
    primary, advisor = service_pair
    cancelled = threading.Event()
    wait_thread, results = _wait_in_thread(advisor, cancelled)
    _wait_until(
        lambda: primary.status().members[AgentName.CLAUDE].status
        is MemberStatus.WAITING
    )

    fake_clock.advance(5.0)
    _wait_until(
        lambda: primary.status().members[AgentName.CLAUDE].last_heartbeat
        == fake_clock()
    )
    assert (
        primary.status().members[AgentName.CLAUDE].status
        is MemberStatus.WAITING
    )

    fake_clock.advance(16.0)
    assert (
        primary.status().members[AgentName.CLAUDE].status
        is MemberStatus.JOINED_NOT_WAITING
    )
    cancelled.set()
    wait_thread.join(timeout=2)
    assert results == [None]


def test_old_wait_finally_cannot_clear_new_waiter(
    service_pair,
) -> None:
    primary, advisor = service_pair
    first_cancel = threading.Event()
    second_cancel = threading.Event()
    first_thread, first_results = _wait_in_thread(advisor, first_cancel)
    _wait_until(
        lambda: primary.status().members[AgentName.CLAUDE].status
        is MemberStatus.WAITING
    )
    second_thread, second_results = _wait_in_thread(advisor, second_cancel)
    time.sleep(0.03)

    first_cancel.set()
    first_thread.join(timeout=2)

    assert (
        primary.status().members[AgentName.CLAUDE].status
        is MemberStatus.WAITING
    )
    second_cancel.set()
    second_thread.join(timeout=2)
    assert first_results == [None]
    assert second_results == [None]


def test_keyboard_interrupt_propagates_and_clears_only_wait_state(
    service_pair,
) -> None:
    primary, advisor = service_pair

    class InterruptingEvent:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float) -> bool:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        advisor.wait(InterruptingEvent())

    member = primary.status().members[AgentName.CLAUDE]
    assert member.status is MemberStatus.JOINED_NOT_WAITING
    assert member.is_joined is True


def test_concurrent_sends_are_delivered_in_database_fifo_order(
    tmp_path: Path,
    room,
    task_request: TaskRequest,
    fake_clock: FakeClock,
) -> None:
    database = tmp_path / "concurrent.sqlite3"
    join_store = SQLiteStore.open(database, fake_clock)
    join_primary = AiRoomService(
        join_store,
        room,
        AgentName.CODEX,
        session_id="codex-session",
        clock=fake_clock,
    )
    advisor_store = SQLiteStore.open(database, fake_clock)
    advisor = AiRoomService(
        advisor_store,
        room,
        AgentName.CLAUDE,
        session_id="claude-session",
        clock=fake_clock,
    )
    join_primary.join()
    advisor.join()
    sender_stores = [
        SQLiteStore.open(database, fake_clock),
        SQLiteStore.open(database, fake_clock),
    ]
    senders = [
        AiRoomService(
            opened,
            room,
            AgentName.CODEX,
            session_id="codex-session",
            clock=fake_clock,
        )
        for opened in sender_stores
    ]
    requests = [
        replace(task_request, idempotency_key=f"concurrent-{index}", question=str(index))
        for index in range(2)
    ]
    sent: list[object] = []

    def send_request(index: int) -> None:
        try:
            sent.append(senders[index].send(requests[index]))
        except BaseException as error:
            sent.append(error)

    threads = [
        threading.Thread(target=send_request, args=(index,))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    try:
        assert all(not thread.is_alive() for thread in threads)
        assert len(sent) == 2
        assert not any(isinstance(result, BaseException) for result in sent)
        fifo_ids = [
            row[0]
            for row in join_store._connection.execute(
                "SELECT task_id FROM tasks ORDER BY fifo_sequence"
            )
        ]
        delivered_ids: list[str] = []
        for _ in range(2):
            delivery = advisor.wait(threading.Event())
            assert delivery is not None
            delivered_ids.append(delivery.task_id)
            advisor.reply(delivery.task_id, TaskOutcome.DONE, "完成")
        assert delivered_ids == fifo_ids
    finally:
        for opened in sender_stores:
            opened.close()
        advisor_store.close()
        join_store.close()


def test_leave_preserves_tasks_and_messages(
    service_pair,
    task_request: TaskRequest,
) -> None:
    primary, advisor = service_pair
    sent = primary.send(task_request)

    advisor.leave()

    status = primary.status()
    assert status.members[AgentName.CLAUDE].status is MemberStatus.JOINED_NOT_WAITING
    assert status.active_task.task_id == sent.task_id
    message_count = primary._store._connection.execute(
        "SELECT COUNT(*) FROM messages WHERE task_id = ?",
        (sent.task_id,),
    ).fetchone()[0]
    assert message_count == 1
