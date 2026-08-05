"""Tests for the shared subprocess plumbing (process.py).

These cover the two Windows environment bugs that broke every driver: a
sub-agent that emits non-ASCII UTF-8 must not crash under the cp936 locale, and
a subprocess must never block reading stdin from a pipe.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ai_room.drivers.process import DriverError, DriverTimeout, find_binary, run_cli


def test_run_cli_decodes_utf8_chinese(tmp_path: Path) -> None:
    """A sub-agent writing non-ASCII UTF-8 must not blow up under cp936."""
    script = tmp_path / "writer.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write('\u4f60\u597d\uff0c\u4e16\u754c\\n'.encode('utf-8'))\n",
        encoding="utf-8",
    )
    run = run_cli([sys.executable, str(script)], cwd=tmp_path, timeout=30, agent="test")
    assert run.stdout.strip() == "\u4f60\u597d\uff0c\u4e16\u754c"


def test_run_cli_does_not_require_stdin(tmp_path: Path) -> None:
    """A child that reads stdin must see EOF immediately (DEVNULL), not hang."""
    script = tmp_path / "reader.py"
    script.write_text(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write('got:' + str(len(data)))\n",
        encoding="utf-8",
    )
    run = run_cli([sys.executable, str(script)], cwd=tmp_path, timeout=30, agent="test")
    assert run.stdout.strip() == "got:0"


def test_run_cli_replaces_bad_bytes(tmp_path: Path) -> None:
    script = tmp_path / "bad.py"
    script.write_bytes(b"import sys\nsys.stdout.buffer.write(b'\\xff\\xfe bad')\n")
    run = run_cli([sys.executable, str(script)], cwd=tmp_path, timeout=30, agent="test")
    assert "bad" in run.stdout


def test_run_cli_times_out(tmp_path: Path) -> None:
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    with pytest.raises(DriverTimeout):
        run_cli([sys.executable, str(script)], cwd=tmp_path, timeout=0.3, agent="test")


def _slow_talker(tmp_path: Path) -> Path:
    """A child that speaks once, then outlives any sane timeout."""
    script = tmp_path / "slow.py"
    script.write_text(
        "import sys, time\n"
        "print('{\"thread_id\": \"abc-123\"}', flush=True)\n"
        "time.sleep(30)\n"
        "print('never', flush=True)\n",
        encoding="utf-8",
    )
    return script


def test_on_line_fires_while_the_child_is_still_running(tmp_path: Path) -> None:
    """The session id must be recoverable from a run that never finishes.

    This is the whole point of streaming: the caller's shell can kill ask long
    before the sub-agent is done, and the id of the turn already paid for has
    to have reached us by then.
    """
    seen: list[str] = []
    with pytest.raises(DriverTimeout):
        run_cli(
            [sys.executable, str(_slow_talker(tmp_path))],
            cwd=tmp_path,
            timeout=1.5,
            agent="test",
            on_line=seen.append,
        )
    assert any("abc-123" in line for line in seen)


def test_timeout_carries_the_partial_output(tmp_path: Path) -> None:
    with pytest.raises(DriverTimeout) as caught:
        run_cli(
            [sys.executable, str(_slow_talker(tmp_path))],
            cwd=tmp_path,
            timeout=1.5,
            agent="test",
        )
    assert "abc-123" in caught.value.stdout


def test_failing_callback_is_reported_but_does_not_abort_the_run(
    tmp_path: Path,
) -> None:
    script = tmp_path / "chatty.py"
    script.write_text(
        "print('one')\nprint('two')\n",
        encoding="utf-8",
    )

    def explode(_line: str) -> None:
        raise RuntimeError("ledger is on fire")

    run = run_cli(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout=30,
        agent="test",
        on_line=explode,
    )
    assert run.returncode == 0
    assert "two" in run.stdout
    assert isinstance(run.callback_error, RuntimeError)


def test_large_stderr_does_not_deadlock(tmp_path: Path) -> None:
    """Both pipes are drained concurrently, so a noisy stderr cannot wedge us."""
    script = tmp_path / "noisy.py"
    script.write_text(
        "import sys\n"
        "for i in range(4000):\n"
        "    sys.stderr.write('warning %d: something happened\\n' % i)\n"
        "sys.stdout.write('done\\n')\n",
        encoding="utf-8",
    )
    run = run_cli([sys.executable, str(script)], cwd=tmp_path, timeout=60, agent="test")
    assert run.stdout.strip() == "done"
    assert run.stderr.count("warning") == 4000


def test_run_cli_missing_executable_raises_driver_error(tmp_path: Path) -> None:
    with pytest.raises(DriverError):
        run_cli(
            [str(tmp_path / "no-such-exe-xyz.exe")],
            cwd=tmp_path,
            timeout=5,
            agent="test",
        )


def test_find_binary_returns_existing_path() -> None:
    assert find_binary("test", (sys.executable,)) == sys.executable


def test_find_binary_missing_raises() -> None:
    with pytest.raises(DriverError):
        find_binary("test", ("definitely-not-a-real-binary-xyz-123",))