"""Shared subprocess plumbing for the headless vendor-CLI drivers.

All three drivers start one vendor CLI as a one-shot sub-agent and parse its
text output.  Everything that is common to that path lives here so the drivers
do not each keep a subtly different copy.

Two environment bugs are fixed once, here:

- ``text=True`` decodes with the locale default (cp936 on this machine), which
  crashes as soon as a sub-agent emits a non-ASCII character.  We therefore
  call ``subprocess`` with ``encoding="utf-8"`` and ``errors="replace"`` so the
  output is always decoded as UTF-8 and a bad byte degrades to a replacement
  character instead of raising.
- ``stdin`` is ``DEVNULL`` so an interactive CLI never blocks waiting for input
  on a pipe (codex prints ``Reading additional input from stdin...`` and can
  hang when the caller is not a TTY).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .protocol import DriverError


class DriverTimeout(DriverError):
    """Raised when a headless vendor CLI exceeds its timeout budget."""


@dataclass(frozen=True)
class CompletedRun:
    """A finished subprocess with UTF-8 decoded text output."""

    returncode: int
    stdout: str
    stderr: str


def find_binary(agent: str, candidates: tuple[str, ...]) -> str:
    """Return the first available candidate binary or raise DriverError."""
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    listing = ", ".join(candidates)
    raise DriverError(f"{agent} CLI not found on PATH (looked for {listing})")


def run_cli(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    agent: str,
) -> CompletedRun:
    """Run ``command`` once and decode its output as UTF-8.

    Raises :class:`DriverTimeout` when the subprocess exceeds ``timeout``
    seconds and :class:`DriverError` when the process cannot be started.
    """
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise DriverTimeout(
            f"{agent} sub-agent timed out after {timeout:g}s"
        ) from error
    except OSError as error:
        raise DriverError(f"failed to run {agent} CLI: {error}") from error
    return CompletedRun(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
