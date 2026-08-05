"""Claude Code (claude) headless driver."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .process import find_binary, run_cli
from .protocol import Driver, DriverRequest, DriverResult, compose_prompt


class ClaudeDriver(Driver):
    name = "claude"
    _BINARY_CANDIDATES = ("claude", "claude.exe")

    def invoke(self, request: DriverRequest) -> DriverResult:
        binary = find_binary(self.name, self._BINARY_CANDIDATES)
        command = [
            binary,
            "-p",
            compose_prompt(request),
            "--output-format",
            "json",
        ]
        if request.permission == "full-access":
            command += ["--dangerously-skip-permissions"]
        else:
            mode = request.permission_mode or (
                "acceptEdits"
                if request.permission == "workspace-write"
                else "plan"
            )
            command += ["--permission-mode", mode]
        if not request.read_only:
            command += ["--allowedTools", "Edit,Write"]
        if request.model:
            command += ["--model", request.model]
        if request.session_id:
            command += ["--session-id", request.session_id]
        if request.agent_name:
            command += ["--agent", request.agent_name]
        command += ["--add-dir", str(request.cwd)]
        if request.extra_dirs:
            for d in request.extra_dirs:
                command += ["--add-dir", str(d)]

        run = run_cli(command, cwd=request.cwd, timeout=request.timeout, agent=self.name)
        parsed = _parse_json(run.stdout)
        return DriverResult(
            agent=self.name,
            session_id=parsed.session_id,
            text=parsed.text,
            exit_code=run.returncode,
            stderr=run.stderr.strip(),
            is_error=parsed.is_error,
            subtype=parsed.subtype,
            permission_denials=parsed.permission_denials,
            total_cost_usd=parsed.total_cost_usd,
            num_turns=parsed.num_turns,
            usage=parsed.usage,
        )


@dataclass(frozen=True)
class ClaudeParsed:
    """The authoritative fields claude's ``--output-format json`` reports."""

    session_id: str | None
    text: str
    is_error: bool | None = None
    subtype: str | None = None
    permission_denials: tuple[str, ...] = ()
    total_cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict | None = None


def _parse_json(stdout: str) -> ClaudeParsed:
    """Extract the authoritative outcome fields from claude json output.

    ``--output-format json`` returns a single object (not a list), so the parser
    accepts both a dict and a list.  Besides the session id and result text it
    keeps the vendor's own ``is_error`` / ``subtype`` / ``permission_denials`` /
    ``total_cost_usd`` / ``num_turns`` so the caller does not guess success.
    """
    session_id: str | None = None
    parts: list[str] = []
    is_error: bool | None = None
    subtype: str | None = None
    permission_denials: tuple[str, ...] = ()
    total_cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict | None = None
    try:
        payload = json.loads(stdout)
    except ValueError:
        return ClaudeParsed(session_id=None, text=stdout.strip())
    events = payload if isinstance(payload, list) else [payload]
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("session_id"):
            session_id = event["session_id"]
        if "is_error" in event:
            is_error = bool(event["is_error"])
        if event.get("subtype"):
            subtype = event["subtype"]
        if event.get("permission_denials"):
            denials = event["permission_denials"]
            if isinstance(denials, list):
                permission_denials = tuple(str(item) for item in denials)
        if event.get("total_cost_usd") is not None:
            total_cost_usd = event["total_cost_usd"]
        if event.get("num_turns") is not None:
            num_turns = event["num_turns"]
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if event.get("type") == "result":
            result = event.get("result")
            if isinstance(result, str) and result.strip():
                parts.append(result)
    return ClaudeParsed(
        session_id=session_id,
        text="\n".join(parts),
        is_error=is_error,
        subtype=subtype,
        permission_denials=permission_denials,
        total_cost_usd=total_cost_usd,
        num_turns=num_turns,
        usage=usage,
    )
