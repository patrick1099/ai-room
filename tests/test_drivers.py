from __future__ import annotations

from pathlib import Path

from ai_room.drivers.claude import ClaudeDriver, _parse_json
from ai_room.drivers.codex import CodexDriver, _parse_jsonl as _parse_codex_jsonl
from ai_room.drivers.opencode import OpenCodeDriver, _parse_jsonl as _parse_opencode_jsonl
from ai_room.drivers.protocol import (
    DriverError,
    DriverRequest,
    DriverResult,
    compose_prompt,
)
from ai_room.drivers.registry import driver_for, list_drivers

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_driver_for_returns_known_drivers() -> None:
    assert driver_for("claude").name == "claude"
    assert driver_for("codex").name == "codex"
    assert driver_for("opencode").name == "opencode"


def test_list_drivers_matches_expected() -> None:
    assert list_drivers() == ("claude", "codex", "opencode")


def test_unknown_driver_raises() -> None:
    try:
        driver_for("gemini")
    except DriverError as error:
        assert "no driver for agent" in str(error)
    else:
        raise AssertionError("expected DriverError")


def test_claude_parser_handles_real_single_object() -> None:
    """--output-format json returns one object, not a list."""
    session_id, text = _parse_json(_fixture("driver_claude.json"))
    assert session_id == "ccf35261-fbc5-4a16-b48c-db4b1b66e966"
    assert text == "hi"


def test_claude_parser_handles_list_legacy() -> None:
    payload = (
        '[{"session_id":"sess-1","type":"assistant","message":{"content":"think"}},'
        '{"session_id":"sess-1","type":"result","result":"final answer"}]'
    )
    session_id, text = _parse_json(payload)
    assert session_id == "sess-1"
    assert text == "final answer"


def test_claude_parser_non_json_returns_stdout() -> None:
    session_id, text = _parse_json("plain text output")
    assert session_id is None
    assert text == "plain text output"


def test_codex_parser_handles_real_jsonl() -> None:
    """Real codex JSONL: thread.started.thread_id + item.completed.agent_message."""
    session_id, text = _parse_codex_jsonl(_fixture("driver_codex.jsonl"))
    assert session_id == "019fcac4-11cb-74f3-9b6c-9a08c2ea6730"
    assert text == "hi"


def test_codex_parser_keeps_legacy_result_payload() -> None:
    payload = (
        '{"session_id":"thr-1","type":"result","payload":{"status":"completed","text":"answer"}}'
    )
    session_id, text = _parse_codex_jsonl(payload)
    assert session_id == "thr-1"
    assert text == "answer"


def test_codex_parser_ignores_failed_result() -> None:
    payload = '{"session_id":"thr-2","type":"result","payload":{"status":"failed","text":"x"}}'
    session_id, text = _parse_codex_jsonl(payload)
    assert session_id == "thr-2"
    assert text == ""


def test_opencode_parser_handles_real_jsonl() -> None:
    session_id, text = _parse_opencode_jsonl(_fixture("driver_opencode.jsonl"))
    assert session_id == "ses_0353abc38ffejSaQa8Vs5zYQ82"
    assert text == "hi"


def test_opencode_parser_dedupes_streamed_fragments() -> None:
    payload = (
        '{"type":"text","sessionID":"ses-1","part":{"id":"p1","type":"text","text":"hel"}}\n'
        '{"type":"text","sessionID":"ses-1","part":{"id":"p1","type":"text","text":"hello"}}\n'
        '{"type":"text","sessionID":"ses-1","part":{"id":"p2","type":"text","text":" world"}}\n'
    )
    session_id, text = _parse_opencode_jsonl(payload)
    assert session_id == "ses-1"
    assert text == "hello\n world"


def test_driver_request_read_only_defaults_true() -> None:
    request = DriverRequest(question="q", cwd=Path("."))
    assert request.read_only is True


def test_driver_request_read_only_false_with_writable() -> None:
    request = DriverRequest(question="q", cwd=Path("."), writable_docs=("a.c",))
    assert request.read_only is False


def test_compose_prompt_read_only_bans_writes() -> None:
    request = DriverRequest(
        question="review this",
        cwd=Path("."),
        related_docs=("a.c", "b.h"),
    )
    prompt = compose_prompt(request)
    assert "Relevant documents" in prompt
    assert "a.c" in prompt and "b.h" in prompt
    assert "read-only" in prompt


def test_compose_prompt_writable_lists_only_those_files() -> None:
    request = DriverRequest(
        question="edit this",
        cwd=Path("."),
        writable_docs=("a.c",),
    )
    prompt = compose_prompt(request)
    assert "may write only these files" in prompt
    assert "a.c" in prompt
    assert "read-only" not in prompt


# --- argv-shape tests: each driver must build the exact vendor CLI contract. ---


def _fake_run(command, cwd, timeout, agent):
    """Return a canned CompletedRun so invoke() does not touch the real CLI."""
    from ai_room.drivers.process import CompletedRun

    return CompletedRun(returncode=0, stdout="{}", stderr="")


def test_claude_argv_json_output_format(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.claude.find_binary", lambda *a, **k: "claude")
    monkeypatch.setattr("ai_room.drivers.claude.run_cli", fake_run)
    request = DriverRequest(question="q", cwd=Path("."))
    ClaudeDriver().invoke(request)
    assert captured["command"][:5] == ["claude", "-p", "--output-format", "json", "--permission-mode"]


def test_claude_read_only_defaults_plan(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.claude.find_binary", lambda *a, **k: "claude")
    monkeypatch.setattr("ai_room.drivers.claude.run_cli", fake_run)
    request = DriverRequest(question="q", cwd=Path("."))
    ClaudeDriver().invoke(request)
    assert "--permission-mode" in captured["command"]
    assert captured["command"][captured["command"].index("--permission-mode") + 1] == "plan"
    assert "--allowedTools" not in captured["command"]


def test_claude_writable_defaults_accept_edits(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.claude.find_binary", lambda *a, **k: "claude")
    monkeypatch.setattr("ai_room.drivers.claude.run_cli", fake_run)
    request = DriverRequest(question="q", cwd=Path("."), writable_docs=("a.c",))
    ClaudeDriver().invoke(request)
    assert "--permission-mode" in captured["command"]
    assert captured["command"][captured["command"].index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" in captured["command"]


def test_claude_explicit_permission_always_wins(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.claude.find_binary", lambda *a, **k: "claude")
    monkeypatch.setattr("ai_room.drivers.claude.run_cli", fake_run)
    request = DriverRequest(
        question="q", cwd=Path("."), permission_mode="bypassPermissions"
    )
    ClaudeDriver().invoke(request)
    assert "--permission-mode" in captured["command"]
    assert captured["command"][captured["command"].index("--permission-mode") + 1] == "bypassPermissions"


def test_codex_argv_exec_json(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.codex.find_binary", lambda *a, **k: "codex")
    monkeypatch.setattr("ai_room.drivers.codex.run_cli", fake_run)
    monkeypatch.setattr("ai_room.drivers.codex._is_git_repo", lambda cwd: True)
    request = DriverRequest(question="q", cwd=Path("."))
    CodexDriver().invoke(request)
    assert captured["command"][:4] == ["codex", "exec", "--json", "-s"]


def test_codex_read_only_defaults_read_only_sandbox(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.codex.find_binary", lambda *a, **k: "codex")
    monkeypatch.setattr("ai_room.drivers.codex.run_cli", fake_run)
    monkeypatch.setattr("ai_room.drivers.codex._is_git_repo", lambda cwd: True)
    request = DriverRequest(question="q", cwd=Path("."))
    CodexDriver().invoke(request)
    assert captured["command"][4] == "read-only"


def test_codex_writable_defaults_workspace_write(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.codex.find_binary", lambda *a, **k: "codex")
    monkeypatch.setattr("ai_room.drivers.codex.run_cli", fake_run)
    monkeypatch.setattr("ai_room.drivers.codex._is_git_repo", lambda cwd: True)
    request = DriverRequest(question="q", cwd=Path("."), writable_docs=("a.c",))
    CodexDriver().invoke(request)
    assert captured["command"][4] == "workspace-write"


def test_codex_explicit_sandbox_always_wins(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.codex.find_binary", lambda *a, **k: "codex")
    monkeypatch.setattr("ai_room.drivers.codex.run_cli", fake_run)
    monkeypatch.setattr("ai_room.drivers.codex._is_git_repo", lambda cwd: True)
    request = DriverRequest(question="q", cwd=Path("."), sandbox="danger-full-access")
    CodexDriver().invoke(request)
    assert captured["command"][4] == "danger-full-access"


def test_opencode_argv_run_format_json(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.opencode.find_binary", lambda *a, **k: "opencode")
    monkeypatch.setattr("ai_room.drivers.opencode.run_cli", fake_run)
    request = DriverRequest(question="q", cwd=Path("."))
    OpenCodeDriver().invoke(request)
    assert captured["command"][:4] == ["opencode", "run", "--format", "json"]


def test_opencode_read_only_uses_plan_agent(monkeypatch) -> None:
    captured = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr("ai_room.drivers.opencode.find_binary", lambda *a, **k: "opencode")
    monkeypatch.setattr("ai_room.drivers.opencode.run_cli", fake_run)
    request = DriverRequest(question="q", cwd=Path("."))
    OpenCodeDriver().invoke(request)
    assert "--agent" in captured["command"]
    assert captured["command"][captured["command"].index("--agent") + 1] == "plan"