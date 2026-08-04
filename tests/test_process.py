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