"""Command-line contract tests for ai-room."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

import ai_room.cli as cli
from ai_room.domain import AgentName
from ai_room.domain import RoomRef
from ai_room.service import AiRoomService


PROJECT_ROOT = Path(__file__).parents[1]


def _session_environment(
    local_app_data: Path,
    agent: AgentName,
    *,
    transcript: Path | None = None,
) -> dict[str, str]:
    environ = os.environ.copy()
    environ["LOCALAPPDATA"] = str(local_app_data)
    environ["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    environ["PYTHONUTF8"] = "1"
    environ.pop("CODEX_THREAD_ID", None)
    environ.pop("AI_ROOM_CLAUDE_SESSION_ID", None)
    environ.pop("AI_ROOM_CLAUDE_TRANSCRIPT_PATH", None)
    if agent is AgentName.CODEX:
        environ["CODEX_THREAD_ID"] = "codex-线程"
    else:
        if transcript is None:
            raise ValueError("Claude tests require a transcript")
        environ["AI_ROOM_CLAUDE_SESSION_ID"] = "claude-会话"
        environ["AI_ROOM_CLAUDE_TRANSCRIPT_PATH"] = str(transcript)
    return environ


def _run_cli(
    cwd: Path,
    environ: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ai_room", *arguments],
        cwd=cwd,
        env=environ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _assert_one_json_object(text: str) -> dict[str, object]:
    assert text.endswith("\n")
    assert len(text.splitlines()) == 1
    value = json.loads(text)
    assert isinstance(value, dict)
    return value


@pytest.fixture
def cli_workspace(tmp_path: Path) -> tuple[Path, Path, dict[str, str], dict[str, str]]:
    workspace = tmp_path / "C盘 工作区" / "中文项目"
    workspace.mkdir(parents=True)
    local_app_data = tmp_path / "Local AppData"
    transcript = tmp_path / "会话记录" / "Claude 记录.jsonl"
    transcript.parent.mkdir()
    transcript.write_text("", encoding="utf-8")
    codex_env = _session_environment(local_app_data, AgentName.CODEX)
    claude_env = _session_environment(
        local_app_data,
        AgentName.CLAUDE,
        transcript=transcript,
    )
    return workspace, local_app_data, codex_env, claude_env


def test_help_lists_exactly_the_six_public_commands(cli_workspace) -> None:
    workspace, _, codex_env, _ = cli_workspace

    completed = _run_cli(workspace, codex_env, "--help")

    assert completed.returncode == 0
    assert completed.stderr == ""
    for command in ("join", "wait", "send", "reply", "status", "leave"):
        assert command in completed.stdout
    assert "install" not in completed.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ("join", "other"),
        ("send", "--to", "codex", "--type", "other", "--question", "Q"),
        ("reply", "task", "--outcome", "other", "--message", "M"),
    ],
)
def test_invalid_choice_is_json_argument_error(
    cli_workspace,
    arguments: tuple[str, ...],
) -> None:
    workspace, _, codex_env, _ = cli_workspace

    completed = _run_cli(workspace, codex_env, *arguments)

    assert completed.returncode == 2
    assert completed.stdout == ""
    error = _assert_one_json_object(completed.stderr)
    assert error["ok"] is False
    assert error["error"]["code"] == "argument_error"
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(
    "kind",
    ("requirements-review", "design-review", "plan-review"),
)
def test_document_review_requires_related_document(
    cli_workspace,
    kind: str,
) -> None:
    workspace, _, codex_env, _ = cli_workspace

    completed = _run_cli(
        workspace,
        codex_env,
        "send",
        "--to",
        "claude",
        "--type",
        kind,
        "--question",
        "请评审",
    )

    assert completed.returncode == 2
    error = _assert_one_json_object(completed.stderr)
    assert error["error"]["code"] == "argument_error"
    assert "--related-doc" in error["error"]["message"]


@pytest.mark.parametrize("kind", ("decision", "context-check"))
def test_non_document_kind_rejects_writable_document(
    cli_workspace,
    kind: str,
) -> None:
    workspace, _, codex_env, _ = cli_workspace

    completed = _run_cli(
        workspace,
        codex_env,
        "send",
        "--to",
        "claude",
        "--type",
        kind,
        "--question",
        "请判断",
        "--writable-doc",
        r"docs\不应允许.md",
    )

    assert completed.returncode == 2
    error = _assert_one_json_object(completed.stderr)
    assert error["error"]["code"] == "argument_error"
    assert "--writable-doc" in error["error"]["message"]


def test_join_status_and_leave_emit_stable_status_wording(cli_workspace) -> None:
    workspace, _, codex_env, _ = cli_workspace

    joined = _run_cli(workspace, codex_env, "join", "codex")
    status = _run_cli(workspace, codex_env, "status")
    left = _run_cli(workspace, codex_env, "leave")

    assert joined.returncode == status.returncode == left.returncode == 0
    joined_json = _assert_one_json_object(joined.stdout)
    status_json = _assert_one_json_object(status.stdout)
    left_json = _assert_one_json_object(left.stdout)
    assert joined_json["command"] == "join"
    assert joined_json["result"]["agent"] == "codex"
    assert status_json["result"]["members"] == {
        "codex": {
            "status": "joined_not_waiting",
            "message": "Codex has joined but is not waiting.",
        },
        "claude": {
            "status": "never_joined",
            "message": "Claude has not joined this room.",
        },
    }
    assert left_json["result"] == {
        "agent": "codex",
        "status": "left",
        "message": "Codex left this room; messages were preserved.",
    }
    assert joined.stderr == status.stderr == left.stderr == ""


def test_peer_not_joined_is_stable_actionable_error(cli_workspace) -> None:
    workspace, _, codex_env, _ = cli_workspace
    assert _run_cli(workspace, codex_env, "join", "codex").returncode == 0

    completed = _run_cli(
        workspace,
        codex_env,
        "send",
        "--to",
        "claude",
        "--type",
        "decision",
        "--question",
        "选 A 还是 B？",
    )

    assert completed.returncode == 3
    assert completed.stdout == ""
    assert _assert_one_json_object(completed.stderr) == {
        "ok": False,
        "error": {
            "code": "peer_not_joined",
            "message": "Claude has not joined this room.",
        },
    }


def test_unicode_and_repeated_exact_paths_round_trip_through_subprocess(
    cli_workspace,
) -> None:
    workspace, _, codex_env, claude_env = cli_workspace
    assert _run_cli(workspace, codex_env, "join", "codex").returncode == 0
    assert _run_cli(workspace, claude_env, "join", "claude").returncode == 0

    sent = _run_cli(
        workspace,
        codex_env,
        "send",
        "--to",
        "claude",
        "--type",
        "design-review",
        "--question",
        "请判断：方案甲还是方案乙？",
        "--related-doc",
        r"docs\需求 说明.md",
        "--related-doc",
        r"docs\架构 设计.md",
        "--writable-doc",
        r"docs\评审 结果.md",
        "--checkpoint-doc",
        r"docs\任务 现场.md",
        "--next-entry",
        "继续实现命令行",
        "--idempotency-key",
        "中文-幂等键",
    )
    waited = _run_cli(workspace, claude_env, "wait")

    assert sent.returncode == waited.returncode == 0
    sent_json = _assert_one_json_object(sent.stdout)
    waited_json = _assert_one_json_object(waited.stdout)
    assert sent_json["result"]["kind"] == "design_review"
    assert waited_json["result"] == {
        "message_id": waited_json["result"]["message_id"],
        "task_id": sent_json["result"]["task_id"],
        "kind": "design_review",
        "sender": "codex",
        "recipient": "claude",
        "question": "请判断：方案甲还是方案乙？",
        "message": "请判断：方案甲还是方案乙？",
        "outcome": None,
        "related_docs": ["docs/需求 说明.md", "docs/架构 设计.md"],
        "writable_docs": ["docs/评审 结果.md"],
        "checkpoint_docs": ["docs/任务 现场.md"],
        "context": {
            "input_tokens": "unknown",
            "context_window": "unknown",
            "source": "unknown",
        },
        "next_entry": "继续实现命令行",
        "reply": {
            "task_id": sent_json["result"]["task_id"],
            "command": (
                "ai-room reply "
                f"{sent_json['result']['task_id']} --outcome "
                "done|blocked|compact-ready|checkpoint-needed --message TEXT"
            ),
        },
    }
    assert codex_env["LOCALAPPDATA"] not in waited.stdout
    assert sent.stderr == waited.stderr == ""


def test_guard_violation_is_json_and_exit_four(cli_workspace) -> None:
    workspace, _, codex_env, claude_env = cli_workspace
    assert _run_cli(workspace, codex_env, "join", "codex").returncode == 0
    assert _run_cli(workspace, claude_env, "join", "claude").returncode == 0
    sent = _run_cli(
        workspace,
        codex_env,
        "send",
        "--to",
        "claude",
        "--type",
        "decision",
        "--question",
        "只做决定",
    )
    task_id = _assert_one_json_object(sent.stdout)["result"]["task_id"]
    assert _run_cli(workspace, claude_env, "wait").returncode == 0
    (workspace / "source.py").write_text("not allowed\n", encoding="utf-8")

    completed = _run_cli(
        workspace,
        claude_env,
        "reply",
        task_id,
        "--outcome",
        "done",
        "--message",
        "采用方案甲",
    )

    assert completed.returncode == 4
    assert completed.stdout == ""
    error = _assert_one_json_object(completed.stderr)
    assert error == {
        "ok": False,
        "error": {
            "code": "workspace_guard_violation",
            "message": "Reply blocked by workspace guard: source.py",
            "violations": ["source.py"],
        },
    }
    assert "Traceback" not in completed.stderr


def test_keyboard_interrupt_during_wait_returns_130_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    environ = _session_environment(tmp_path / "runtime", AgentName.CODEX)
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert (
        cli.main(
            ["join", "codex"],
            environ=environ,
            cwd=workspace,
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    stdout.seek(0)
    stdout.truncate(0)

    def interrupt(self, cancel_event, checkpoint_docs=(), *, next_entry=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(AiRoomService, "wait", interrupt)

    result = cli.main(
        ["wait"],
        environ=environ,
        cwd=workspace,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 130
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "ai-room wait interrupted; room membership and messages were "
        "preserved.\n"
    )


def test_keyboard_interrupt_outside_wait_is_not_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    environ = _session_environment(tmp_path / "runtime", AgentName.CODEX)

    def interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(AiRoomService, "join", interrupt)

    with pytest.raises(KeyboardInterrupt):
        cli.main(
            ["join", "codex"],
            environ=environ,
            cwd=workspace,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


def test_wait_rejects_non_exact_checkpoint_before_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    environ = _session_environment(tmp_path / "runtime", AgentName.CODEX)
    assert (
        cli.main(
            ["join", "codex"],
            environ=environ,
            cwd=workspace,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        == 0
    )

    def must_not_wait(self, cancel_event, checkpoint_docs=(), *, next_entry=None):
        raise AssertionError("invalid checkpoint reached blocking service")

    monkeypatch.setattr(AiRoomService, "wait", must_not_wait)
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = cli.main(
        ["wait", "--checkpoint", r"..\escape.md"],
        environ=environ,
        cwd=workspace,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 2
    assert stdout.getvalue() == ""
    error = _assert_one_json_object(stderr.getvalue())
    assert error["error"]["code"] == "argument_error"


def test_path_argument_error_redacts_the_user_home(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    environ = _session_environment(tmp_path / "runtime", AgentName.CODEX)
    assert (
        cli.main(
            ["join", "codex"],
            environ=environ,
            cwd=workspace,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        == 0
    )
    stderr = io.StringIO()

    result = cli.main(
        [
            "send",
            "--to",
            "claude",
            "--type",
            "design-review",
            "--question",
            "review",
            "--related-doc",
            "docs/review.md",
            "--writable-doc",
            str(Path.home() / "outside.md"),
        ],
        environ=environ,
        cwd=workspace,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert result == 2
    error = _assert_one_json_object(stderr.getvalue())
    assert error["error"]["code"] == "argument_error"
    assert str(Path.home()) not in error["error"]["message"]


class _WaitStore:
    def __init__(self, waiting_context_check: object | None = None) -> None:
        self.waiting = waiting_context_check

    def acknowledge_informational_replies(self, *args, **kwargs) -> int:
        return 0

    def begin_wait(self, *args, **kwargs) -> None:
        return None

    def waiting_context_check(self, *args, **kwargs) -> object | None:
        return self.waiting

    def clear_waiter(self, *args, **kwargs) -> bool:
        return True


@pytest.mark.parametrize("has_waiting_check", (False, True))
def test_wait_carries_next_entry_to_context_service_paths(
    tmp_path: Path,
    has_waiting_check: bool,
) -> None:
    store = _WaitStore(object() if has_waiting_check else None)
    service = AiRoomService(
        store,  # type: ignore[arg-type]
        RoomRef("room", tmp_path),
        AgentName.CODEX,
        session_id="session",
        poll_seconds=0.001,
    )
    cancel_event = threading.Event()
    observed: list[tuple[str, str | None]] = []

    def no_delivery(self, waiter_token):
        return None

    def maybe_context(self, checkpoint_docs, next_entry=None):
        observed.append(("request", next_entry))
        cancel_event.set()
        return None

    def resume_context(self, checkpoint_docs, next_entry=None):
        observed.append(("resume", next_entry))
        cancel_event.set()
        return store.waiting

    service._claim_for_waiter = types.MethodType(no_delivery, service)
    service._maybe_request_context_check = types.MethodType(maybe_context, service)
    service.resume_context_check = types.MethodType(resume_context, service)

    assert (
        service.wait(
            cancel_event,
            (Path(r"docs\节点.md"),),
            next_entry="继续 Task 8",
        )
        is None
    )
    assert observed == [
        ("resume" if has_waiting_check else "request", "继续 Task 8")
    ]
