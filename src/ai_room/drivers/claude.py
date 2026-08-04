"""Claude Code (claude) headless driver."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .protocol import Driver, DriverError, DriverResult


class ClaudeDriver(Driver):
    name = "claude"
    _BINARY_CANDIDATES = ("claude", "claude.exe")

    def _binary(self) -> str:
        for name in self._BINARY_CANDIDATES:
            path = shutil.which(name)
            if path:
                return path
        raise DriverError("claude CLI not found on PATH")

    def invoke(
        self,
        question: str,
        *,
        cwd: Path,
        model: str | None = None,
        timeout: float = 300.0,
        permission_mode: str | None = None,
        sandbox: str | None = None,
        allowed_write: tuple[str, ...] = (),
    ) -> DriverResult:
        del sandbox
        binary = self._binary()
        permission = permission_mode or "plan"
        command = [
            binary,
            "-p",
            "--output-format",
            "json-1",
            "--permission-mode",
            permission,
        ]
        if model:
            command += ["--model", model]
        if allowed_write:
            command += ["--allowedTools", "Edit", "Write"]
        command.append(question)

        proc = _run(command, cwd=cwd, timeout=timeout)
        session_id, text = _parse_json(proc.stdout)
        return DriverResult(
            agent=self.name,
            session_id=session_id,
            text=text,
            exit_code=proc.returncode,
            stderr=proc.stderr.strip(),
        )


def _run(command: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise DriverError(
            f"claude sub-agent timed out after {timeout:g}s"
        ) from error
    except OSError as error:
        raise DriverError(f"failed to run claude CLI: {error}") from error


def _parse_json(stdout: str) -> tuple[str | None, str]:
    """Extract session_id and the final result text from claude json-1 output."""
    session_id: str | None = None
    parts: list[str] = []
    try:
        events = json.loads(stdout)
    except ValueError:
        return session_id, stdout.strip()
    if not isinstance(events, list):
        return session_id, stdout.strip()
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("session_id"):
            session_id = event["session_id"]
        if event.get("type") == "result":
            result = event.get("result")
            if isinstance(result, str) and result.strip():
                parts.append(result)
    return session_id, "\n".join(parts)