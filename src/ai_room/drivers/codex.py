"""OpenAI Codex (codex) headless driver."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .protocol import Driver, DriverError, DriverResult


class CodexDriver(Driver):
    name = "codex"
    _BINARY_CANDIDATES = ("codex", "codex.exe")

    def _binary(self) -> str:
        for name in self._BINARY_CANDIDATES:
            path = shutil.which(name)
            if path:
                return path
        raise DriverError("codex CLI not found on PATH")

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
        del permission_mode
        del allowed_write
        binary = self._binary()
        sandbox_mode = sandbox or "read-only"
        command = [binary, "exec", "--json", "-s", sandbox_mode]
        if model:
            command += ["-m", model]
        command.append(question)

        proc = _run(command, cwd=cwd, timeout=timeout)
        session_id, text = _parse_jsonl(proc.stdout)
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
        raise DriverError(f"codex sub-agent timed out after {timeout:g}s") from error
    except OSError as error:
        raise DriverError(f"failed to run codex CLI: {error}") from error


def _parse_jsonl(stdout: str) -> tuple[str | None, str]:
    """Extract session_id and the completed result text from codex JSONL output."""
    session_id: str | None = None
    parts: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("session_id"):
            session_id = event["session_id"]
        if event.get("type") == "result":
            payload = event.get("payload") or {}
            if payload.get("status") == "completed":
                text = payload.get("text") or payload.get("final_text") or ""
                if text.strip():
                    parts.append(text)
    return session_id, "\n".join(parts)