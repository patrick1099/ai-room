"""CLI-level tests for the ``ask`` sub-command.

These exercise the real ``main()`` -> ``_command_ask`` path with a fake driver
and a stubbed workspace guard, so the exit-code and ledger behaviours are
verified without spending a real vendor CLI turn.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

from ai_room.cli import EXIT_OPERATIONAL, EXIT_SUCCESS, main
from ai_room.drivers.process import DriverTimeout
from ai_room.drivers.protocol import DriverResult
from ai_room.domain import GuardResult
from ai_room.workspace_guard import WorkspaceSnapshot


class _FakeDriver:
    def __init__(self, result: DriverResult) -> None:
        self._result = result

    def invoke(self, request):
        return self._result


class _RaisingDriver:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def invoke(self, request):
        raise self._error


def _run_ask(
    tmp_path: Path,
    monkeypatch,
    *,
    driver,
    guard: GuardResult | None = None,
    use_capture: bool = True,
) -> tuple[int, dict, str]:
    """Run ``ai-room ask`` against a fake driver and return (code, json, stderr)."""
    from ai_room import cli as cli_module

    monkeypatch.setattr(cli_module, "driver_for", lambda name: driver)
    if use_capture:
        monkeypatch.setattr(
            cli_module,
            "capture_workspace",
            lambda root: WorkspaceSnapshot(()),
        )
        monkeypatch.setattr(
            cli_module,
            "compare_workspace",
            lambda before, after, writable: guard
            or GuardResult(allowed_changes=(), violations=()),
        )

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["ask", "--to", "claude", "--question", "review this"],
        stdout=out,
        stderr=err,
    )
    raw = out.getvalue()
    payload = json.loads(raw) if raw else {}
    return code, payload, err.getvalue()


def test_ask_success_exits_zero_and_writes_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = DriverResult(
        agent="claude",
        session_id="sess-1",
        text="looks safe",
        exit_code=0,
        stderr="",
    )
    code, payload, _ = _run_ask(tmp_path, monkeypatch, driver=_FakeDriver(result))
    assert code == EXIT_SUCCESS
    assert payload["ok"] is True
    assert payload["result"]["session_id"] == "sess-1"
    assert payload["result"]["ok"] is True
    ledger = (tmp_path / ".ai-room" / "ledger.md").read_text(encoding="utf-8")
    assert "sess-1" in ledger
    assert "`ok`" in ledger


def test_ask_failure_exits_operational(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = DriverResult(
        agent="claude",
        session_id=None,
        text="",
        exit_code=1,
        stderr="boom",
    )
    code, payload, _ = _run_ask(tmp_path, monkeypatch, driver=_FakeDriver(result))
    assert code == EXIT_OPERATIONAL
    assert payload["ok"] is False
    assert payload["result"]["ok"] is False
    ledger = (tmp_path / ".ai-room" / "ledger.md").read_text(encoding="utf-8")
    assert "`error`" in ledger


def test_ask_timeout_writes_error_ledger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    driver = _RaisingDriver(DriverTimeout("claude sub-agent timed out after 1s"))
    code, payload, err = _run_ask(tmp_path, monkeypatch, driver=driver)
    assert code == EXIT_OPERATIONAL
    # A raised DriverError is reported on stderr, not the stdout result line.
    assert payload == {}
    assert err
    ledger = (tmp_path / ".ai-room" / "ledger.md").read_text(encoding="utf-8")
    assert "`timeout`" in ledger


def test_ask_guard_blocked_exits_operational(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = DriverResult(
        agent="claude",
        session_id="sess-9",
        text="done",
        exit_code=0,
        stderr="",
    )
    guard = GuardResult(allowed_changes=(), violations=("secret.txt",))
    code, payload, _ = _run_ask(
        tmp_path, monkeypatch, driver=_FakeDriver(result), guard=guard
    )
    assert code == EXIT_OPERATIONAL
    assert payload["ok"] is False
    assert payload["result"]["guard_violations"] == ["secret.txt"]
    ledger = (tmp_path / ".ai-room" / "ledger.md").read_text(encoding="utf-8")
    assert "`guard-blocked`" in ledger
    assert "secret.txt" in ledger


def test_ask_no_ledger_skips_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = DriverResult(
        agent="claude",
        session_id="sess-1",
        text="ok",
        exit_code=0,
        stderr="",
    )
    from ai_room import cli as cli_module

    monkeypatch.setattr(cli_module, "driver_for", lambda name: _FakeDriver(result))
    monkeypatch.setattr(
        cli_module, "capture_workspace", lambda root: WorkspaceSnapshot(())
    )
    monkeypatch.setattr(
        cli_module,
        "compare_workspace",
        lambda before, after, writable: GuardResult(allowed_changes=(), violations=()),
    )
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["ask", "--to", "claude", "--question", "q", "--no-ledger"],
        stdout=out,
        stderr=err,
    )
    assert code == EXIT_SUCCESS
    assert not (tmp_path / ".ai-room" / "ledger.md").exists()


class _WritingDriver:
    """A fake driver that actually writes a file (real-wiring guard test)."""

    def __init__(self, result: DriverResult, file_path: Path, content: str) -> None:
        self._result = result
        self._file_path = file_path
        self._content = content

    def invoke(self, request):
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(self._content, encoding="utf-8")
        return self._result


def test_ask_guard_real_wiring_detects_untracked_change(
    tmp_path: Path, monkeypatch
) -> None:
    """Real capture_workspace/compare_workspace must catch a file the driver
    wrote outside the writable allow-list, even when the driver reports ok."""
    from ai_room import cli as cli_module
    from ai_room.workspace_guard import compare_workspace

    monkeypatch.chdir(tmp_path)
    # A real git repo so capture_workspace uses the tracked/untracked paths.
    sub = subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, capture_output=True
    )
    assert sub.returncode == 0, sub.stderr.decode()
    (tmp_path / "known.txt").write_text("base", encoding="utf-8")
    sub = subprocess.run(
        ["git", "add", "known.txt"], cwd=tmp_path, capture_output=True
    )
    assert sub.returncode == 0
    name = subprocess.run(
        ["git", "config", "user.email"], cwd=tmp_path, capture_output=True
    )
    if not name.stdout.strip():
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True
        )
    sub = subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, capture_output=True
    )
    assert sub.returncode == 0, sub.stderr.decode()

    result = DriverResult(
        agent="claude",
        session_id="sess-77",
        text="done",
        exit_code=0,
        stderr="",
    )
    # The driver writes secret.txt, which is NOT in writable_docs. Only
    # driver_for is stubbed; the real guard runs end to end.
    driver = _WritingDriver(result, tmp_path / "secret.txt", "leak")
    monkeypatch.setattr(cli_module, "driver_for", lambda name: driver)

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["ask", "--to", "claude", "--question", "q", "--writable-doc", "known.txt"],
        stdout=out,
        stderr=err,
    )
    assert code == EXIT_OPERATIONAL
    payload = json.loads(out.getvalue())
    assert payload["ok"] is False
    assert payload["result"]["guard_violations"] == ["secret.txt"]
    ledger = (tmp_path / ".ai-room" / "ledger.md").read_text(encoding="utf-8")
    assert "`guard-blocked`" in ledger
    assert "secret.txt" in ledger