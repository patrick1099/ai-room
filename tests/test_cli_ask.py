"""CLI-level tests for the ``ask`` sub-command.

These exercise the real ``main()`` -> ``_command_ask`` path with a fake driver,
so the exit-code, receipt and ledger behaviours are verified without spending a
real vendor CLI turn.

Note what is deliberately *not* here any more: there is no test that a
sub-agent writing an unexpected file fails the dispatch.  It does not.  ``ask``
hands out work, and you cannot know in advance which files a job will touch, so
the changed files are reported to the caller as a receipt and the exit code is
the vendor's verdict alone.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

from ai_room.cli import EXIT_OPERATIONAL, EXIT_SUCCESS, main
from ai_room.drivers.process import DriverTimeout
from ai_room.drivers.protocol import DriverResult


class _FakeDriver:
    def __init__(self, result: DriverResult) -> None:
        self._result = result
        self.request = None

    def invoke(self, request):
        self.request = request
        return self._result


class _RaisingDriver:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def invoke(self, request):
        raise self._error


class _WritingDriver:
    """A fake driver that actually writes a file, to exercise the receipt."""

    def __init__(self, result: DriverResult, file_path: Path, content: str) -> None:
        self._result = result
        self._file_path = file_path
        self._content = content

    def invoke(self, request):
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(self._content, encoding="utf-8")
        return self._result


def _ok_result(session_id: str = "sess-1", text: str = "done") -> DriverResult:
    return DriverResult(
        agent="claude", session_id=session_id, text=text, exit_code=0, stderr=""
    )


def _init_git_repo(tmp_path: Path) -> None:
    """Create a minimal git repo with one committed file ``known.txt``."""
    sub = subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True)
    assert sub.returncode == 0, sub.stderr.decode()
    (tmp_path / "known.txt").write_text("base", encoding="utf-8")
    sub = subprocess.run(["git", "add", "known.txt"], cwd=tmp_path, capture_output=True)
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


def _run_ask(monkeypatch, *, driver, extra: list[str] | None = None):
    """Run ``ai-room ask`` against a fake driver and return (code, json, stderr)."""
    from ai_room import cli as cli_module

    monkeypatch.setattr(cli_module, "driver_for", lambda name: driver)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["ask", "--to", "claude", "--question", "review this"] + (extra or []),
        stdout=out,
        stderr=err,
    )
    raw = out.getvalue()
    return code, (json.loads(raw) if raw else {}), err.getvalue()


def _ledger(tmp_path: Path) -> str:
    return (tmp_path / ".ai-room" / "ledger.md").read_text(encoding="utf-8")


def test_ask_success_exits_zero_and_writes_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, payload, _ = _run_ask(monkeypatch, driver=_FakeDriver(_ok_result("sess-1")))
    assert code == EXIT_SUCCESS
    assert payload["ok"] is True
    assert payload["result"]["session_id"] == "sess-1"
    ledger = _ledger(tmp_path)
    assert "sess-1" in ledger
    assert "`ok`" in ledger


def test_ask_failure_exits_operational(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = DriverResult(
        agent="claude", session_id=None, text="", exit_code=1, stderr="boom"
    )
    code, payload, _ = _run_ask(monkeypatch, driver=_FakeDriver(result))
    assert code == EXIT_OPERATIONAL
    assert payload["ok"] is False
    assert "`error`" in _ledger(tmp_path)


def test_ask_timeout_writes_error_ledger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    driver = _RaisingDriver(DriverTimeout("claude sub-agent timed out after 1s"))
    code, payload, err = _run_ask(monkeypatch, driver=driver)
    assert code == EXIT_OPERATIONAL
    # A raised DriverError is reported on stderr, not the stdout result line.
    assert payload == {}
    assert err
    assert "`timeout`" in _ledger(tmp_path)


def test_ask_timeout_recovers_the_session_id_from_partial_output(
    tmp_path: Path, monkeypatch
) -> None:
    """A timed-out turn was still paid for, so its handle must reach the ledger.

    Without this the most expensive failure mode -- a long dispatch killed part
    way -- is also the one you cannot resume.
    """
    monkeypatch.chdir(tmp_path)
    partial = '{"type":"thread.started","thread_id":"thr-abc"}\n{"type":"item.st'
    driver = _RaisingDriver(DriverTimeout("timed out", stdout=partial))
    code, _, _ = _run_ask(monkeypatch, driver=driver)
    assert code == EXIT_OPERATIONAL
    ledger = _ledger(tmp_path)
    assert "thr-abc" in ledger
    assert "`timeout`" in ledger


def test_ask_reports_changed_files_without_failing_the_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    """A sub-agent that writes an unlisted file is reported, not blocked.

    This is the whole difference from the advisor role: dispatched work has no
    allow-list to violate.
    """
    monkeypatch.chdir(tmp_path)
    _init_git_repo(tmp_path)
    driver = _WritingDriver(_ok_result("sess-77"), tmp_path / "secret.txt", "leak")

    code, payload, _ = _run_ask(monkeypatch, driver=driver)

    assert code == EXIT_SUCCESS
    assert payload["ok"] is True
    assert any("secret.txt" in line for line in payload["result"]["changed_files"])
    ledger = _ledger(tmp_path)
    assert "`ok`" in ledger
    assert "secret.txt" in ledger
    assert "改动回执" in ledger


def test_ask_receipt_ignores_changes_that_predate_the_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    """The receipt is a delta, not a dump of everything already dirty."""
    monkeypatch.chdir(tmp_path)
    _init_git_repo(tmp_path)
    (tmp_path / "already-dirty.txt").write_text("before", encoding="utf-8")

    code, payload, _ = _run_ask(monkeypatch, driver=_FakeDriver(_ok_result()))

    assert code == EXIT_SUCCESS
    assert payload["result"]["changed_files"] == []


def test_ask_outside_a_git_repo_reports_no_receipt_and_still_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    """No git means the receipt is unknown; that must not fail the dispatch."""
    monkeypatch.chdir(tmp_path)
    driver = _WritingDriver(_ok_result(), tmp_path / "made.txt", "x")
    code, payload, _ = _run_ask(monkeypatch, driver=driver)
    assert code == EXIT_SUCCESS
    assert payload["result"]["changed_files"] == []


def test_ask_defaults_to_read_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    driver = _FakeDriver(_ok_result())
    _run_ask(monkeypatch, driver=driver)
    assert driver.request.permission == "read-only"
    assert driver.request.read_only is True


def test_ask_permission_tier_reaches_the_driver(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    driver = _FakeDriver(_ok_result())
    _run_ask(monkeypatch, driver=driver, extra=["--permission", "workspace-write"])
    assert driver.request.permission == "workspace-write"
    assert driver.request.read_only is False


def test_ask_rejects_an_unknown_permission_tier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    driver = _FakeDriver(_ok_result())
    from ai_room import cli as cli_module

    monkeypatch.setattr(cli_module, "driver_for", lambda name: driver)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["ask", "--to", "claude", "--question", "q", "--permission", "root"],
        stdout=out,
        stderr=err,
    )
    assert code != EXIT_SUCCESS


def test_ask_no_ledger_skips_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, _ = _run_ask(
        monkeypatch, driver=_FakeDriver(_ok_result()), extra=["--no-ledger"]
    )
    assert code == EXIT_SUCCESS
    assert not (tmp_path / ".ai-room" / "ledger.md").exists()
