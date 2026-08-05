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

Output is consumed as it arrives rather than collected after the process exits.
That is not a performance choice.  ``ask`` is synchronous, so the caller's own
shell timeout can kill it mid-run -- codex's default is 10 seconds, below any
real dispatch -- and when that happens no ``except`` branch here ever runs.  If
the output were only parsed at the end, the session id would die with the
process and the turn that was already paid for could not be resumed.  The
``on_line`` callback exists so a driver can pull the session id out of the first
event and persist it seconds into the run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

from .protocol import DriverError


class DriverTimeout(DriverError):
    """Raised when a headless vendor CLI exceeds its timeout budget.

    Carries whatever the sub-agent managed to emit before it was killed, so the
    caller can still recover the session id and bill from a timed-out run.
    """

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class CompletedRun:
    """A finished subprocess with UTF-8 decoded text output."""

    returncode: int
    stdout: str
    stderr: str
    #: Set when ``on_line`` raised.  A failing callback must not abort a run
    #: that is already burning tokens, but it must not vanish either.
    callback_error: BaseException | None = None


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
    on_line: Callable[[str], None] | None = None,
) -> CompletedRun:
    """Run ``command`` once, streaming its UTF-8 output as it arrives.

    ``on_line`` is called with each stdout line, newline stripped, while the
    sub-agent is still running.  Use it to persist anything that must survive
    the process being killed from outside -- above all the session id, which
    every vendor emits in its first event.

    Raises :class:`DriverTimeout` when the subprocess exceeds ``timeout``
    seconds and :class:`DriverError` when the process cannot be started.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        raise DriverError(f"failed to run {agent} CLI: {error}") from error

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    callback_error: BaseException | None = None

    def pump(stream, sink: list[str], notify: bool) -> None:
        # Both pipes must be drained concurrently or a chatty stderr fills its
        # buffer and deadlocks the child while we wait on stdout.
        nonlocal callback_error
        try:
            for line in stream:
                sink.append(line)
                if notify and on_line is not None and callback_error is None:
                    try:
                        on_line(line.rstrip("\r\n"))
                    except BaseException as error:  # noqa: BLE001
                        # A failing ledger write must not abort a run that is
                        # already spending money; report it on the result.
                        callback_error = error
        finally:
            stream.close()

    pumps = [
        Thread(target=pump, args=(process.stdout, stdout_chunks, True), daemon=True),
        Thread(target=pump, args=(process.stderr, stderr_chunks, False), daemon=True),
    ]
    for thread in pumps:
        thread.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _kill_tree(process)
        for thread in pumps:
            thread.join(timeout=_DRAIN_SECONDS)
        raise DriverTimeout(
            f"{agent} sub-agent timed out after {timeout:g}s",
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
        ) from error

    for thread in pumps:
        thread.join(timeout=_DRAIN_SECONDS)
    return CompletedRun(
        returncode=process.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        callback_error=callback_error,
    )


#: How long to wait for the reader threads to finish after the child is gone.
_DRAIN_SECONDS = 5.0


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the sub-agent and its children.

    The vendor CLIs are launcher shims around a node process that spawns more
    of its own, so killing only the direct child leaves the real work running
    and holding the pipes open.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=_DRAIN_SECONDS,
                check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass
