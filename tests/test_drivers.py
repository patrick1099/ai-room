from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_room.drivers.claude import ClaudeDriver, _parse_json
from ai_room.drivers.codex import CodexDriver, _parse_jsonl as _parse_codex_jsonl
from ai_room.drivers.opencode import (
    OpenCodeDriver,
    _binary_command,
    _parse_jsonl as _parse_opencode_jsonl,
)
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
    request = DriverRequest(question="q", cwd=Path("."), permission="workspace-write")
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


def test_compose_prompt_writable_omits_read_only_notice() -> None:
    request = DriverRequest(
        question="edit this",
        cwd=Path("."),
        permission="workspace-write",
    )
    prompt = compose_prompt(request)
    assert "read-only" not in prompt
    assert prompt == "edit this"


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


def _all_values(argv: list[str], flag: str) -> list[str]:
    """Return every value that follows *flag* in *argv* (for repeatable flags)."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


# ---- claude ----


def test_claude_argv_json_output_format(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(question="q", cwd=Path(".")),
    )
    assert argv[0] == "binary"
    assert argv[1] == "-p"
    assert _value_at(argv, "--output-format") == "json"


def test_claude_prompt_is_value_of_p_not_trailing(monkeypatch) -> None:
    """The prompt must be the value of -p, not the last positional arg.

    --allowedTools is variadic and would eat a trailing prompt as a tool name.
    """
    request = DriverRequest(question="do stuff", cwd=Path("."))
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        request,
    )
    assert argv[1] == "-p"
    assert argv[2] == compose_prompt(request)
    assert argv[-1] != compose_prompt(request)


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
        DriverRequest(question="q", cwd=Path("."), permission="workspace-write"),
    )
    assert _value_at(argv, "--permission-mode") == "acceptEdits"
    assert "--allowedTools" in argv
    assert _value_at(argv, "--allowedTools") == "Edit,Write"


def test_claude_full_access_emits_dangerously_skip(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(question="q", cwd=Path("."), permission="full-access"),
    )
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode" not in argv


def test_claude_explicit_permission_always_wins(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(question="q", cwd=Path("."), permission_mode="bypassPermissions"),
    )
    assert _value_at(argv, "--permission-mode") == "bypassPermissions"


def test_claude_add_dir_for_cwd(monkeypatch) -> None:
    project = Path("/tmp/project")
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(question="q", cwd=project),
    )
    assert str(project) in _all_values(argv, "--add-dir")


def test_claude_add_dir_for_extra_dirs(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(
            question="q",
            cwd=Path("."),
            permission="workspace-write",
            extra_dirs=("/a", "/b"),
        ),
    )
    assert _all_values(argv, "--add-dir")[-2:] == ["/a", "/b"]


def test_claude_model_session_id_agent(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.claude",
        ClaudeDriver(),
        DriverRequest(
            question="q",
            cwd=Path("."),
            model="claude-3",
            session_id="sess-123",
            agent_name="builder",
        ),
    )
    assert _value_at(argv, "--model") == "claude-3"
    assert _value_at(argv, "--session-id") == "sess-123"
    assert _value_at(argv, "--agent") == "builder"


# ---- codex ----


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
        DriverRequest(question="q", cwd=Path("."), permission="workspace-write"),
    )
    assert argv[4] == "workspace-write"


def test_codex_full_access_sandbox(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=Path("."), permission="full-access"),
    )
    assert argv[4] == "danger-full-access"


def test_codex_explicit_sandbox_always_wins(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=Path("."), sandbox="danger-full-access"),
    )
    assert argv[4] == "danger-full-access"


def test_codex_c_flag_pins_cwd(monkeypatch) -> None:
    project = Path("/tmp/project")
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=project),
    )
    assert "-C" in argv
    assert _value_at(argv, "-C") == str(project)


def test_codex_read_only_turns_approvals_off(monkeypatch) -> None:
    """-s read-only is not a boundary on its own.

    An approving reviewer escalates a sandbox denial into a run outside the
    sandbox. Verified against the real CLI: with the machine configured for
    auto_review, a read-only dispatch wrote both files it was told not to. The
    driver therefore pins the approval policy instead of inheriting it.
    """
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=Path(".")),
    )
    assert 'approval_policy="never"' in _all_values(argv, "-c")
    assert "approvals_reviewer=auto_review" not in _all_values(argv, "-c")


def test_codex_writable_allows_escalation_after_a_sandbox_refusal(
    monkeypatch,
) -> None:
    """Without escalation a workspace-write produces files nobody can read.

    When the sandbox helper is broken the sandboxed write fails and the
    fallback leaves the file owned by the sandbox principal -- exit 0, and the
    caller cannot open it. on-failure keeps the escalation narrow: only after
    the sandbox has actually refused.
    """
    values = _all_values(
        _capture_argv(
            monkeypatch,
            "ai_room.drivers.codex",
            CodexDriver(),
            DriverRequest(
                question="q", cwd=Path("."), permission="workspace-write"
            ),
        ),
        "-c",
    )
    assert 'approval_policy="on-failure"' in values
    assert "approvals_reviewer=auto_review" in values


def test_codex_full_access_needs_no_approvals(monkeypatch) -> None:
    """danger-full-access has no sandbox to refuse anything to escalate past."""
    values = _all_values(
        _capture_argv(
            monkeypatch,
            "ai_room.drivers.codex",
            CodexDriver(),
            DriverRequest(question="q", cwd=Path("."), permission="full-access"),
        ),
        "-c",
    )
    assert 'approval_policy="never"' in values


def test_codex_add_dir_for_extra_dirs(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(
            question="q",
            cwd=Path("."),
            permission="workspace-write",
            extra_dirs=("/a", "/b"),
        ),
    )
    assert _all_values(argv, "--add-dir") == ["/a", "/b"]


def test_codex_model_flag(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.codex",
        CodexDriver(),
        DriverRequest(question="q", cwd=Path("."), model="gpt-5"),
    )
    assert _value_at(argv, "-m") == "gpt-5"


# ---- opencode ----


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


def test_opencode_dir_present_read_only(monkeypatch) -> None:
    project = Path("/tmp/project")
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.opencode",
        OpenCodeDriver(),
        DriverRequest(question="q", cwd=project),
    )
    assert "--dir" in argv
    assert _value_at(argv, "--dir") == str(project)


def test_opencode_dir_present_workspace_write(monkeypatch) -> None:
    project = Path("/tmp/project")
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.opencode",
        OpenCodeDriver(),
        DriverRequest(question="q", cwd=project, permission="workspace-write"),
    )
    assert _value_at(argv, "--dir") == str(project)
    assert "--auto" in argv


def test_opencode_dir_present_full_access(monkeypatch) -> None:
    project = Path("/tmp/project")
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.opencode",
        OpenCodeDriver(),
        DriverRequest(question="q", cwd=project, permission="full-access"),
    )
    assert _value_at(argv, "--dir") == str(project)
    assert "--auto" in argv


def test_opencode_agent_name_overrides_plan(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.opencode",
        OpenCodeDriver(),
        DriverRequest(question="q", cwd=Path("."), agent_name="builder"),
    )
    assert _value_at(argv, "--agent") == "builder"


def test_opencode_agent_name_does_not_cost_write_access(monkeypatch) -> None:
    """Naming an agent says who does the work, not how much they may do.

    --agent and --auto answer different questions, so a workspace-write
    dispatch that also names an agent must still receive --auto.
    """
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.opencode",
        OpenCodeDriver(),
        DriverRequest(
            question="q",
            cwd=Path("."),
            permission="workspace-write",
            agent_name="builder",
        ),
    )
    assert _value_at(argv, "--agent") == "builder"
    assert "--auto" in argv


def test_binary_command_passes_a_real_executable_through() -> None:
    assert _binary_command(r"C:\tools\opencode.exe") == [r"C:\tools\opencode.exe"]


@pytest.mark.skipif(os.name != "nt", reason="npm shim resolution is Windows-only")
def test_binary_command_resolves_an_npm_shim_to_the_real_exe(tmp_path: Path) -> None:
    """The shim must be bypassed, not invoked through cmd.

    Launching the .cmd via ``cmd /c`` truncates the prompt at its first
    newline, so this is a correctness fix, not a tidiness one.
    """
    shim = tmp_path / "opencode.cmd"
    shim.write_text('@ECHO off\r\n"%dp0%\\node_modules\\x" %*\r\n', encoding="utf-8")
    real = tmp_path / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"MZ")

    assert _binary_command(str(shim)) == [str(real)]


@pytest.mark.skipif(os.name != "nt", reason="npm shim resolution is Windows-only")
def test_binary_command_reads_the_target_out_of_an_unfamiliar_shim(
    tmp_path: Path,
) -> None:
    real = tmp_path / "elsewhere.exe"
    real.write_bytes(b"MZ")
    shim = tmp_path / "opencode.cmd"
    shim.write_text(f'@ECHO off\r\n"{real}" %*\r\n', encoding="utf-8")

    assert _binary_command(str(shim)) == [str(real)]


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe argument parsing is Windows-only")
def test_cmd_shim_would_have_truncated_a_multiline_prompt() -> None:
    """Pins the reason the shim is bypassed, so nobody reinstates ``cmd /c``.

    Every real prompt is multi-line -- compose_prompt appends a block -- and
    the truncated run still exits 0 with a plausible answer, which is why this
    went unnoticed.
    """
    show = [sys.executable, "-c", "import sys; print(repr(sys.argv[1]))"]
    prompt = "line one\nline two"

    direct = subprocess.run(show + [prompt], capture_output=True, text=True)
    through_cmd = subprocess.run(
        ["cmd", "/c"] + show + [prompt], capture_output=True, text=True
    )

    assert "line two" in direct.stdout
    assert "line two" not in through_cmd.stdout


def test_opencode_model_flag(monkeypatch) -> None:
    argv = _capture_argv(
        monkeypatch,
        "ai_room.drivers.opencode",
        OpenCodeDriver(),
        DriverRequest(question="q", cwd=Path("."), model="opencode-1"),
    )
    assert _value_at(argv, "--model") == "opencode-1"
