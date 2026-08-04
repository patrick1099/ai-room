"""OpenCode (opencode) headless driver.

v1 uses plain-text print mode; session_id is unknown because opencode's JSON
event schema is not stable across versions. A later pass can parse --format json.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .protocol import Driver, DriverError, DriverResult


class OpenCodeDriver(Driver):
    name = "opencode"

    def _binary(self) -> str:
        for name in ("opencode", "opencode.cmd", "opencode.ps1"):
            path = shutil.which(name)
            if path:
                return path
        raise DriverError("opencode CLI not found on PATH")

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
        del sandbox
        del allowed_write
        base = _binary_command(self._binary())
        command: list[str] = list(base)
        if model:
            command += ["--model", model]
        command.append(question)

        proc = _run(command, cwd=cwd, timeout=timeout)
        return DriverResult(
            agent=self.name,
            session_id=None,
            text=proc.stdout.strip(),
            exit_code=proc.returncode,
            stderr=proc.stderr.strip(),
        )


def _binary_command(binary: str) -> list[str]:
    if os.name != "nt" or not binary.lower().endswith((".ps1", ".cmd")):
        return [binary]
    return ["cmd", "/c", binary]


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
            f"opencode sub-agent timed out after {timeout:g}s"
        ) from error
    except OSError as error:
        raise DriverError(f"failed to run opencode CLI: {error}") from error