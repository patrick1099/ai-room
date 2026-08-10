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

import pytest

from ai_room.cli import EXIT_OPERATIONAL, EXIT_SUCCESS, main
from ai_room.drivers.process import DriverTimeout
from ai_room.drivers.protocol import Driver, DriverResult


class _FakeDriver(Driver):
    name = "claude"

    def __init__(self, result: DriverResult) -> None:
        self._result = result
        self.request = None

    def invoke(self, request):
        self.request = request
        if request.on_session_id and self._result.session_id:
            request.on_session_id(self._result.session_id)
        return self._result


class _RaisingDriver(Driver):
    name = "claude"

    def __init__(self, error: Exception) -> None:
        self._error = error

    def invoke(self, request):
        raise self._error


class _WritingDriver(Driver):
    """A fake driver that actually writes a file, to exercise the receipt."""

    name = "claude"

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


def _run_resume(monkeypatch, *, driver, extra: list[str] | None = None):
    """Run ``ai-room resume`` against a fake driver and return (code, json, err)."""
    from ai_room import cli as cli_module

    monkeypatch.setattr(cli_module, "driver_for", lambda name: driver)
    out, err = io.StringIO(), io.StringIO()
    code = main(["resume"] + (extra or []), stdout=out, stderr=err)
    raw = out.getvalue()
    return code, (json.loads(raw) if raw else {}), err.getvalue()


def _ledger(tmp_path: Path) -> str:
    return (tmp_path / ".ai-room" / "ledger.md").read_text(encoding="utf-8")


def _inflight_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / ".ai-room" / "inflight").glob("*.json"))


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
    code, payload, _ = _run_ask(monkeypatch, driver=driver)
    assert code == EXIT_OPERATIONAL
    assert payload["result"]["status"] == "timeout"
    assert "`timeout`" in _ledger(tmp_path)


def test_ask_timeout_hands_back_a_way_to_continue(
    tmp_path: Path, monkeypatch
) -> None:
    """A timeout must reach the caller as a resumable result, not a bare error.

    Reporting only "timed out" leaves re-dispatching the same task as the only
    available move, which bills the whole turn a second time -- so the handle,
    the partial answer and the resume command all travel on the result.
    """
    monkeypatch.chdir(tmp_path)
    partial = '{"type":"result","session_id":"sess-cut","result":"half done"}'
    driver = _RaisingDriver(
        DriverTimeout("timed out", stdout=partial, reason="idle")
    )
    code, payload, _ = _run_ask(monkeypatch, driver=driver)
    result = payload["result"]
    assert code == EXIT_OPERATIONAL
    assert result["ok"] is False
    assert result["timeout_reason"] == "idle"
    assert result["session_id"] == "sess-cut"
    assert "ai-room resume" in result["resume_command"]
    assert "sess-cut" in result["resume_command"]
    assert "do not re-send" in result["hint"]


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


def test_ask_preassigns_a_session_id_for_claude(tmp_path: Path, monkeypatch) -> None:
    """claude accepts a chosen handle, so use one -- it is the only vendor
    whose run stays resumable even if it dies before saying anything."""
    monkeypatch.chdir(tmp_path)
    driver = _FakeDriver(_ok_result())
    _run_ask(monkeypatch, driver=driver)
    assert driver.request.session_id


def test_ask_timeout_falls_back_to_the_preassigned_id(
    tmp_path: Path, monkeypatch
) -> None:
    """A claude run killed before it emits anything is still resumable."""
    monkeypatch.chdir(tmp_path)
    driver = _RaisingDriver(DriverTimeout("timed out", stdout=""))
    code, _, _ = _run_ask(monkeypatch, driver=driver)
    assert code == EXIT_OPERATIONAL
    ledger = _ledger(tmp_path)
    assert "无会话 id" not in ledger
    assert "claude -r " in ledger


def test_ask_does_not_invent_an_id_when_the_cli_never_ran(
    tmp_path: Path, monkeypatch
) -> None:
    """A handle that resumes nothing is worse than admitting there is none."""
    from ai_room.drivers import DriverError

    monkeypatch.chdir(tmp_path)
    driver = _RaisingDriver(DriverError("claude CLI not found on PATH"))
    code, _, _ = _run_ask(monkeypatch, driver=driver)
    assert code == EXIT_OPERATIONAL
    assert "unknown" in _ledger(tmp_path)


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


def test_budgets_come_from_the_environment_when_set(
    tmp_path: Path, monkeypatch
) -> None:
    """The useful budget depends on the caller's own shell timeout.

    That is a property of which CLI is driving ai-room, not of ai-room, so it
    has to be settable per machine without editing flags at every call site.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_ROOM_TIMEOUT", "900")
    monkeypatch.setenv("AI_ROOM_MAX_RUNTIME", "1500")
    driver = _FakeDriver(_ok_result())
    _run_ask(monkeypatch, driver=driver)
    assert driver.request.timeout == 900.0
    assert driver.request.max_runtime == 1500.0


def test_an_explicit_flag_still_beats_the_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_ROOM_TIMEOUT", "900")
    driver = _FakeDriver(_ok_result())
    _run_ask(monkeypatch, driver=driver, extra=["--timeout", "42"])
    assert driver.request.timeout == 42.0


@pytest.mark.parametrize("value", ("", "not-a-number", "0", "-5"))
def test_a_broken_budget_variable_falls_back_instead_of_failing(
    tmp_path: Path, monkeypatch, value: str
) -> None:
    """A typo in a machine-wide variable must not break every dispatch."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_ROOM_TIMEOUT", value)
    driver = _FakeDriver(_ok_result())
    _run_ask(monkeypatch, driver=driver)
    assert driver.request.timeout == 300.0


def test_ask_no_ledger_skips_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, _ = _run_ask(
        monkeypatch, driver=_FakeDriver(_ok_result()), extra=["--no-ledger"]
    )
    assert code == EXIT_SUCCESS
    assert not (tmp_path / ".ai-room" / "ledger.md").exists()


# --- surviving a kill that happens outside this process ---


class _WatchingDriver(Driver):
    """Announces a handle mid-run and records what was on disk at that moment."""

    name = "claude"

    def __init__(self, result: DriverResult, root: Path) -> None:
        self._result = result
        self._root = root
        #: Read during the run: by the time the dispatch returns the record is
        #: gone, which is the point -- only killed runs leave one behind.
        self.written_when_announced: list[dict] = []

    def invoke(self, request):
        request.on_session_id(self._result.session_id)
        for path in sorted((self._root / ".ai-room" / "inflight").glob("*.json")):
            self.written_when_announced.append(
                json.loads(path.read_text(encoding="utf-8"))
            )
        return self._result


def test_the_handle_is_on_disk_before_the_dispatch_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """The killing blow can land between the first event and the last.

    ``ask`` is synchronous, so the caller's shell timeout can take down this
    whole process mid-run; nothing written at the end would survive that. The
    handle therefore has to be durable while the sub-agent is still talking.
    """
    monkeypatch.chdir(tmp_path)
    driver = _WatchingDriver(_ok_result("sess-live"), tmp_path)
    code, _, _ = _run_ask(monkeypatch, driver=driver)
    assert code == EXIT_SUCCESS
    assert [run["session_id"] for run in driver.written_when_announced] == [
        "sess-live"
    ]


def test_a_dispatch_that_reported_back_is_not_left_looking_orphaned(
    tmp_path: Path, monkeypatch
) -> None:
    """Only runs killed from outside may remain, or resume picks a finished one."""
    monkeypatch.chdir(tmp_path)
    code, _, _ = _run_ask(monkeypatch, driver=_FakeDriver(_ok_result("sess-done")))
    assert code == EXIT_SUCCESS
    assert _inflight_files(tmp_path) == []


# --- resume ---


def _orphan(tmp_path: Path, *, agent: str = "claude", session_id: str = "sess-x"):
    from ai_room.inflight import record_inflight

    return record_inflight(
        tmp_path,
        agent=agent,
        session_id=session_id,
        question="the original task",
        cwd=tmp_path,
    )


def test_resume_continues_the_killed_dispatch_without_being_told_which(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _orphan(tmp_path, session_id="sess-orphan")
    driver = _FakeDriver(_ok_result("sess-orphan", text="finished the rest"))
    code, payload, _ = _run_resume(monkeypatch, driver=driver)
    assert code == EXIT_SUCCESS
    assert driver.request.resume_session == "sess-orphan"
    assert payload["result"]["resumed_from"] == "sess-orphan"


def test_resume_tells_the_sub_agent_to_carry_on_not_to_restart(
    tmp_path: Path, monkeypatch
) -> None:
    """Restating the task is how a resume turns back into a full re-run."""
    monkeypatch.chdir(tmp_path)
    _orphan(tmp_path)
    driver = _FakeDriver(_ok_result())
    _run_resume(monkeypatch, driver=driver)
    assert "Do not start over" in driver.request.question


def test_resume_clears_the_record_it_consumed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _orphan(tmp_path)
    _run_resume(monkeypatch, driver=_FakeDriver(_ok_result()))
    assert _inflight_files(tmp_path) == []


def test_resume_refuses_a_bare_handle_without_its_vendor(
    tmp_path: Path, monkeypatch
) -> None:
    """A session id does not say who issued it, and guessing resumes nothing."""
    monkeypatch.chdir(tmp_path)
    code, _, err = _run_resume(
        monkeypatch, driver=_FakeDriver(_ok_result()), extra=["--session", "sess-1"]
    )
    assert code == EXIT_OPERATIONAL
    assert "resume_agent_required" in err


def test_resume_with_nothing_to_resume_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = _run_resume(monkeypatch, driver=_FakeDriver(_ok_result()))
    assert code == EXIT_OPERATIONAL
    assert "no_inflight_run" in err
