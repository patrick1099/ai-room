"""OpenCode (opencode) headless driver."""

from __future__ import annotations

import json
import os

from .process import find_binary, run_cli
from .protocol import Driver, DriverRequest, DriverResult, compose_prompt


class OpenCodeDriver(Driver):
    name = "opencode"
    _BINARY_CANDIDATES = ("opencode", "opencode.cmd", "opencode.ps1")

    def invoke(self, request: DriverRequest) -> DriverResult:
        binary = find_binary(self.name, self._BINARY_CANDIDATES)
        base = _binary_command(binary)
        command: list[str] = list(base) + ["run", "--format", "json"]
        if request.read_only:
            command += ["--agent", "plan"]
        if request.model:
            command += ["--model", request.model]
        command.append(compose_prompt(request))

        run = run_cli(command, cwd=request.cwd, timeout=request.timeout, agent=self.name)
        session_id, text = _parse_jsonl(run.stdout)
        return DriverResult(
            agent=self.name,
            session_id=session_id,
            text=text,
            exit_code=run.returncode,
            stderr=run.stderr.strip(),
        )


def _binary_command(binary: str) -> list[str]:
    if os.name != "nt" or not binary.lower().endswith((".ps1", ".cmd")):
        return [binary]
    return ["cmd", "/c", binary]


def _parse_jsonl(stdout: str) -> tuple[str | None, str]:
    """Extract the session id and final text from opencode JSON output.

    ``opencode run --format json`` emits one JSON object per line.  The session
    handle lives on the top-level ``sessionID``; the reply is the ``text`` part
    which may arrive in several fragments, so the last value per ``part.id`` is
    kept and concatenated.  Fragments are joined with ``\n``; this is a guess
    from a single-part capture, so multi-fragment replies may carry extra
    newlines until the real stream shape is captured.
    """
    session_id: str | None = None
    fragments: dict[str, str] = {}
    order: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("sessionID"):
            session_id = event["sessionID"]
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        key = part.get("id") or f"#fragment{len(order)}"
        if key not in fragments:
            order.append(key)
        fragments[key] = text
    return session_id, "\n".join(fragments[key] for key in order)