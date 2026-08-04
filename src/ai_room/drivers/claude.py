"""Claude Code (claude) headless driver."""

from __future__ import annotations

import json

from .process import find_binary, run_cli
from .protocol import Driver, DriverRequest, DriverResult, compose_prompt


class ClaudeDriver(Driver):
    name = "claude"
    _BINARY_CANDIDATES = ("claude", "claude.exe")

    def invoke(self, request: DriverRequest) -> DriverResult:
        binary = find_binary(self.name, self._BINARY_CANDIDATES)
        # An explicit --permission-mode always wins; otherwise read-only defaults
        # to plan and any writable grant defaults to acceptEdits.
        permission = request.permission_mode or (
            "acceptEdits" if not request.read_only else "plan"
        )
        command = [
            binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            permission,
        ]
        if not request.read_only:
            command += ["--allowedTools", "Edit", "Write"]
        if request.model:
            command += ["--model", request.model]
        command.append(compose_prompt(request))

        run = run_cli(command, cwd=request.cwd, timeout=request.timeout, agent=self.name)
        session_id, text = _parse_json(run.stdout)
        return DriverResult(
            agent=self.name,
            session_id=session_id,
            text=text,
            exit_code=run.returncode,
            stderr=run.stderr.strip(),
        )


def _parse_json(stdout: str) -> tuple[str | None, str]:
    """Extract session_id and the final result text from claude json output.

    ``--output-format json`` returns a single object (not a list), so the parser
    accepts both a dict and a list.
    """
    session_id: str | None = None
    parts: list[str] = []
    try:
        payload = json.loads(stdout)
    except ValueError:
        return session_id, stdout.strip()
    events = payload if isinstance(payload, list) else [payload]
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