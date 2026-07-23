"""Subprocess end-to-end and operator-document contracts for ai-room."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
CLI = (sys.executable, "-m", "ai_room")
PROCESS_TIMEOUT = 10
REDELIVERY_TIMEOUT = 22


@dataclass(frozen=True)
class E2EWorkspace:
    root: Path
    local_app_data: Path
    home: Path
    codex_env: dict[str, str]
    claude_env: dict[str, str]


def _session_environment(
    local_app_data: Path,
    home: Path,
    *,
    agent: str,
    session_id: str,
    transcript: Path | None = None,
) -> dict[str, str]:
    environ = os.environ.copy()
    environ.update(
        {
            "LOCALAPPDATA": str(local_app_data),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONUTF8": "1",
        }
    )
    environ.pop("CODEX_THREAD_ID", None)
    environ.pop("AI_ROOM_CLAUDE_SESSION_ID", None)
    environ.pop("AI_ROOM_CLAUDE_TRANSCRIPT_PATH", None)
    if agent == "codex":
        environ["CODEX_THREAD_ID"] = session_id
    else:
        if transcript is None:
            raise ValueError("Claude subprocess requires a transcript")
        environ["AI_ROOM_CLAUDE_SESSION_ID"] = session_id
        environ["AI_ROOM_CLAUDE_TRANSCRIPT_PATH"] = str(transcript)
    return environ


def _workspace(
    tmp_path: Path,
    name: str = "中文工作树",
    *,
    codex_session: str = "codex-e2e",
    claude_session: str = "claude-e2e",
    local_app_data: Path | None = None,
    home: Path | None = None,
) -> E2EWorkspace:
    root = tmp_path / name
    root.mkdir(parents=True)
    isolated_runtime = local_app_data or tmp_path / "isolated-local-app-data"
    isolated_home = home or tmp_path / "isolated-home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / f"{name}-claude.jsonl"
    transcript.write_text("", encoding="utf-8")
    return E2EWorkspace(
        root=root,
        local_app_data=isolated_runtime,
        home=isolated_home,
        codex_env=_session_environment(
            isolated_runtime,
            isolated_home,
            agent="codex",
            session_id=codex_session,
        ),
        claude_env=_session_environment(
            isolated_runtime,
            isolated_home,
            agent="claude",
            session_id=claude_session,
            transcript=transcript,
        ),
    )


def _parse_compact_json(text: str) -> dict[str, object]:
    assert text.endswith("\n")
    assert len(text.splitlines()) == 1
    value = json.loads(text)
    assert isinstance(value, dict)
    assert (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        == text
    )
    return value


def _run(
    workspace: E2EWorkspace,
    agent: str,
    *arguments: str,
    timeout: float = PROCESS_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    environ = workspace.codex_env if agent == "codex" else workspace.claude_env
    return subprocess.run(
        [*CLI, *arguments],
        cwd=workspace.root,
        env=environ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _run_ok(
    workspace: E2EWorkspace,
    agent: str,
    *arguments: str,
    timeout: float = PROCESS_TIMEOUT,
) -> dict[str, object]:
    completed = _run(
        workspace,
        agent,
        *arguments,
        timeout=timeout,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return _parse_compact_json(completed.stdout)


def _start(
    workspace: E2EWorkspace,
    agent: str,
    *arguments: str,
) -> subprocess.Popen[str]:
    environ = workspace.codex_env if agent == "codex" else workspace.claude_env
    return subprocess.Popen(
        [*CLI, *arguments],
        cwd=workspace.root,
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _finish(
    process: subprocess.Popen[str],
    *,
    timeout: float = PROCESS_TIMEOUT,
) -> dict[str, object]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=PROCESS_TIMEOUT)
        raise
    assert process.returncode == 0, stderr
    assert stderr == ""
    return _parse_compact_json(stdout)


def _stop(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=PROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=PROCESS_TIMEOUT)


def _join_both(workspace: E2EWorkspace) -> None:
    _run_ok(workspace, "codex", "join", "codex")
    _run_ok(workspace, "claude", "join", "claude")


def test_two_agent_chinese_round_trip_uses_real_subprocesses(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _join_both(workspace)
    advisor_wait = _start(workspace, "claude", "wait")
    try:
        sent = _run_ok(
            workspace,
            "codex",
            "send",
            "--to",
            "claude",
            "--type",
            "decision",
            "--question",
            "这里应该选方案 A 还是 B？",
        )
        delivered = _finish(advisor_wait)
    finally:
        _stop(advisor_wait)

    task_id = sent["result"]["task_id"]  # type: ignore[index]
    assert delivered["result"]["task_id"] == task_id  # type: ignore[index]
    assert delivered["result"]["question"] == "这里应该选方案 A 还是 B？"  # type: ignore[index]
    _run_ok(
        workspace,
        "claude",
        "reply",
        str(task_id),
        "--outcome",
        "done",
        "--message",
        "采用方案 A，并记录取舍。",
    )
    response = _run_ok(workspace, "codex", "wait")
    assert response["result"]["task_id"] == task_id  # type: ignore[index]
    assert response["result"]["message"] == "采用方案 A，并记录取舍。"  # type: ignore[index]


def test_waiter_restart_redelivers_before_acknowledgement(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _join_both(workspace)
    sent = _run_ok(
        workspace,
        "codex",
        "send",
        "--to",
        "claude",
        "--type",
        "decision",
        "--question",
        "未确认前进程结束后还能收到吗？",
    )
    first_wait = _start(workspace, "claude", "wait")
    first = _finish(first_wait)
    _stop(first_wait)

    restarted_wait = _start(workspace, "claude", "wait")
    try:
        redelivered = _finish(
            restarted_wait,
            timeout=REDELIVERY_TIMEOUT,
        )
    finally:
        _stop(restarted_wait)

    task_id = sent["result"]["task_id"]  # type: ignore[index]
    assert first["result"]["task_id"] == task_id  # type: ignore[index]
    assert redelivered["result"]["task_id"] == task_id  # type: ignore[index]
    assert (
        redelivered["result"]["message_id"]  # type: ignore[index]
        == first["result"]["message_id"]  # type: ignore[index]
    )


def test_concurrent_sends_deliver_working_then_fifo_queued_task(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _join_both(workspace)

    def send(index: int) -> dict[str, object]:
        return _run_ok(
            workspace,
            "codex",
            "send",
            "--to",
            "claude",
            "--type",
            "decision",
            "--question",
            f"并发问题 {index}",
            "--idempotency-key",
            f"e2e-concurrent-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = list(pool.map(send, (1, 2)))

    by_state = {
        item["result"]["state"]: item["result"]  # type: ignore[index]
        for item in sent
    }
    assert set(by_state) == {"working", "queued"}
    first = _run_ok(workspace, "claude", "wait")
    assert first["result"]["task_id"] == by_state["working"]["task_id"]  # type: ignore[index]
    _run_ok(
        workspace,
        "claude",
        "reply",
        str(by_state["working"]["task_id"]),
        "--outcome",
        "done",
        "--message",
        "先完成第一项",
    )
    second = _run_ok(workspace, "claude", "wait")
    assert second["result"]["task_id"] == by_state["queued"]["task_id"]  # type: ignore[index]


def test_different_worktree_roots_use_isolated_rooms(tmp_path: Path) -> None:
    local_app_data = tmp_path / "shared-isolated-runtime"
    home = tmp_path / "shared-isolated-home"
    first = _workspace(
        tmp_path,
        "worktree-a",
        codex_session="codex-a",
        claude_session="claude-a",
        local_app_data=local_app_data,
        home=home,
    )
    second = _workspace(
        tmp_path,
        "worktree-b",
        codex_session="codex-b",
        claude_session="claude-b",
        local_app_data=local_app_data,
        home=home,
    )
    _join_both(first)
    _join_both(second)

    sent = _run_ok(
        first,
        "codex",
        "send",
        "--to",
        "claude",
        "--type",
        "decision",
        "--question",
        "只属于 worktree-a",
    )
    first_delivery = _run_ok(first, "claude", "wait")
    second_status = _run_ok(second, "claude", "status")

    assert first_delivery["room"] == sent["room"]
    assert second_status["room"] != sent["room"]
    assert second_status["result"]["active_task"] is None  # type: ignore[index]


def test_advisor_reply_is_blocked_after_source_change(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = workspace.root / "src" / "module.py"
    document = workspace.root / "docs" / "design.md"
    source.parent.mkdir()
    document.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    document.write_text("# Design\n", encoding="utf-8")
    _join_both(workspace)
    sent = _run_ok(
        workspace,
        "codex",
        "send",
        "--to",
        "claude",
        "--type",
        "design-review",
        "--question",
        "只允许修改设计文档",
        "--related-doc",
        "docs/design.md",
        "--writable-doc",
        "docs/design.md",
    )
    _run_ok(workspace, "claude", "wait")
    source.write_text("VALUE = 2\n", encoding="utf-8")

    blocked = _run(
        workspace,
        "claude",
        "reply",
        str(sent["result"]["task_id"]),  # type: ignore[index]
        "--outcome",
        "done",
        "--message",
        "完成",
    )

    assert blocked.returncode == 4
    assert blocked.stdout == ""
    error = _parse_compact_json(blocked.stderr)
    assert error["error"]["code"] == "workspace_guard_violation"  # type: ignore[index]
    assert error["error"]["violations"] == ["src/module.py"]  # type: ignore[index]
    assert source.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_156k_codex_fixture_triggers_context_check(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        codex_session="codex-156k",
    )
    sessions = workspace.home / ".codex" / "sessions" / "2026" / "07" / "23"
    sessions.mkdir(parents=True)
    transcript = sessions / "rollout.jsonl"
    records = (
        {"type": "session_meta", "payload": {"id": "codex-156k"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 156_000},
                    "model_context_window": 258_000,
                },
            },
        },
    )
    transcript.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    checkpoint = workspace.root / "docs" / "checkpoint.md"
    checkpoint.parent.mkdir()
    checkpoint.write_text("当前小任务已完成；下一步：继续验证。\n", encoding="utf-8")
    _join_both(workspace)

    primary_wait = _start(
        workspace,
        "codex",
        "wait",
        "--checkpoint",
        "docs/checkpoint.md",
        "--next-entry",
        "继续 Task 9",
    )
    try:
        request = _run_ok(workspace, "claude", "wait")
    finally:
        stdout, stderr = _stop(primary_wait)

    assert stdout == ""
    assert stderr == ""
    result = request["result"]
    assert result["kind"] == "context_check"  # type: ignore[index]
    assert result["context"] == {  # type: ignore[index]
        "input_tokens": 156_000,
        "context_window": 258_000,
        "source": "codex_token_count",
    }
    assert result["checkpoint_docs"] == ["docs/checkpoint.md"]  # type: ignore[index]
    assert "156000" in result["question"]  # type: ignore[index]


def test_subprocesses_use_only_isolated_runtime_and_home(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _join_both(workspace)
    status = _run_ok(workspace, "codex", "status")

    assert status["ok"] is True
    assert (workspace.local_app_data / "ai-room").is_dir()
    assert str(workspace.local_app_data) not in json.dumps(status)
    assert str(workspace.home) not in json.dumps(status)
    assert not (workspace.home / ".claude").exists()


def test_operator_documents_keep_real_acceptance_pending() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    design = (
        PROJECT_ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-07-23-ai-room-design.md"
    ).read_text(encoding="utf-8")
    acceptance_path = PROJECT_ROOT / "docs" / "acceptance" / "dual-window.md"

    assert "python -m ai_room.install --check" in readme
    assert "另行获得用户明确批准" in readme
    for command in ("join", "wait", "send", "reply", "status", "leave"):
        assert f"ai-room {command}" in readme
    assert "%LOCALAPPDATA%/ai-room" in readme
    assert "不会删除" in readme
    assert "manual" in readme.casefold()
    assert "implemented, awaiting dual-window acceptance" in design
    assert acceptance_path.is_file()
    acceptance = acceptance_path.read_text(encoding="utf-8")
    assert "- [x]" not in acceptance.casefold()
    assert "真实双窗口验收尚未执行" in acceptance
    for field in ("日期", "Codex 版本", "Claude Code 版本", "工作树根目录", "测试人"):
        assert field in acceptance
