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
    parsed = _parse_json(_fixture("driver_claude.json"))
    assert parsed.session_id == "ccf35261-fbc5-4a16-b48c-db4b1b66e966"
    assert parsed.text == "hi"
    assert parsed.is_error is False
    assert parsed.subtype == "success"
    assert parsed.permission_denials == ()
    assert parsed.total_cost_usd == 0.204492
    assert parsed.num_turns == 1
    assert parsed.usage is not None


def test_claude_parser_handles_list_legacy() -> None:
    payload = (
        '[{"session_id":"sess-1","type":"assistant","message":{"content":"think"}},'
        '{"session_id":"sess-1","type":"result","result":"final answer"}]'
    )
    parsed = _parse_json(payload)
    assert parsed.session_id == "sess-1"
    assert parsed.text == "final answer"


def test_claude_parser_non_json_returns_stdout() -> None:
    parsed = _parse_json("plain text output")
    assert parsed.session_id is None
    assert parsed.text == "plain text output"


def test_claude_parser_captures_permission_denials() -> None:
    payload = (
        '{"session_id":"sess-x","type":"result","result":"blocked",'
        '"is_error":true,"subtype":"error_during_execution",'
        '"permission_denials":["Edit:/etc/passwd"],"total_cost_usd":0.5,"num_turns":3}'
    )
    parsed = _parse_json(payload)
    assert parsed.is_error is True
    assert parsed.subtype == "error_during_execution"
    assert parsed.permission_denials == ("Edit:/etc/passwd",)
    assert parsed.total_cost_usd == 0.5
    assert parsed.num_turns == 3


def test_codex_parser_handles_real_jsonl() -> None:
    """Real codex JSONL: thread.started.thread_id + item.completed.agent_message."""
    session_id, text, usage = _parse_codex_jsonl(_fixture("driver_codex.jsonl"))
    assert session_id == "019fcac4-11cb-74f3-9b6c-9a08c2ea6730"
    assert text == "hi"
    assert usage == {
        "input_tokens": 24626,
        "cached_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_output_tokens": 0,
    }


def test_codex_parser_keeps_legacy_result_payload() -> None:
    payload = (
        '{"session_id":"thr-1","type":"result","payload":{"status":"completed","text":"answer"}}'
    )
    session_id, text, _ = _parse_codex_jsonl(payload)
    assert session_id == "thr-1"
    assert text == "answer"


def test_codex_parser_ignores_failed_result() -> None:
    payload = '{"session_id":"thr-2","type":"result","payload":{"status":"failed","text":"x"}}'
    session_id, text, _ = _parse_codex_jsonl(payload)


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


def _capture_argv(
    monkeypatch, module: str, driver, request: DriverRequest
) -> list[str]:
    """Run ``driver.invoke(request)`` with run_cli stubbed and return the argv.

    The ``_is_git_repo`` hook exists only on the codex module, so it is patched
    automatically when present instead of being a caller-supplied flag.
    """
    captured: dict[str, list[str]] = {}

    def fake_run(command, cwd, timeout, agent):
        captured["command"] = command
        return _fake_run(command, cwd, timeout, agent)

    monkeypatch.setattr(f"{module}.find_binary", lambda *a, **k: "binary")
    monkeypatch.setattr(f"{module}.run_cli", fake_run)
    if hasattr(__import__(module, fromlist=["x"]), "_is_git_repo"):
        monkeypatch.setattr(f"{module}._is_git_repo", lambda cwd: True)
    driver.invoke(request)
    return captured["command"]


def _value_at(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_claude_argv_json_output_format(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(question="q", cwd=Path(".")),
    )
    assert argv[:6] == ["binary", "-p", "--output-format", "json", "--permission-mode", "plan"]


def test_claude_read_only_defaults_plan(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(question="q", cwd=Path(".")),
    )
    assert _value_at(argv, "--permission-mode") == "plan"
    assert "--allowedTools" not in argv


def test_claude_writable_defaults_accept_edits(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(question="q", cwd=Path("."), writable_docs=("a.c",)),
    )
    assert _value_at(argv, "--permission-mode") == "acceptEdits"
    assert "--allowedTools" in argv


def test_claude_explicit_permission_always_wins(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(question="q", cwd=Path("."), permission_mode="bypassPermissions"),
    )
    assert _value_at(argv, "--permission-mode") == "bypassPermissions"


def test_codex_argv_exec_json(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=Path(".")),
    )
    assert argv[:4] == ["binary", "exec", "--json", "-s"]


def test_codex_read_only_defaults_read_only_sandbox(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=Path(".")),
    )
    assert argv[4] == "read-only"


def test_codex_writable_defaults_workspace_write(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=Path("."), writable_docs=("a.c",)),
    )
    assert argv[4] == "workspace-write"


def test_codex_explicit_sandbox_always_wins(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=Path("."), sandbox="danger-full-access"),
    )
    assert argv[4] == "danger-full-access"


def test_opencode_argv_run_format_json(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.opencode",
        OpenCodeDriver(),
        DriverRequest(question="q", cwd=Path(".")),
    )
    assert argv[:4] == ["binary", "run", "--format", "json"]


def test_opencode_read_only_uses_plan_agent(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.opencode",
        OpenCodeDriver(),
        DriverRequest(question="q", cwd=Path(".")),
    )
    assert _value_at(argv, "--agent") == "plan"
