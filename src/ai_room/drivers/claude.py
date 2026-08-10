"""Claude Code (claude) headless driver."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .process import find_binary, run_cli
from .protocol import (
    Driver,
    DriverRequest,
    DriverResult,
    compose_prompt,
    session_id_watcher,
)


class ClaudeDriver(Driver):
    name = "claude"
    _BINARY_CANDIDATES = ("claude", "claude.exe")

    def invoke(self, request: DriverRequest) -> DriverResult:
        binary = find_binary(self.name, self._BINARY_CANDIDATES)
        # stream-json rather than json: with the single-object format claude
        # says nothing at all until it is finished, so there is no way to tell
        # a sub-agent that is thinking from one that has hung, and the silence
        # budget would degenerate back into a wall clock.  The streamed form
        # emits progress events throughout, and carries the same result object
        # as its final event.  ``--verbose`` is required for it under ``-p``.
        command = [
            binary,
            "-p",
            compose_prompt(request),
            "--output-format",
            "stream-json",
            "--verbose",
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
        if request.resume_session:
            # -r continues the recorded conversation; --session-id would demand
            # a fresh handle and the two cannot both be given.
            command += ["-r", request.resume_session]
        elif request.session_id:
            command += ["--session-id", request.session_id]
        if request.agent_name:
            command += ["--agent", request.agent_name]
        command += ["--add-dir", str(request.cwd)]
        if request.extra_dirs:
            for d in request.extra_dirs:
                command += ["--add-dir", str(d)]

        run = run_cli(
            command,
            cwd=request.cwd,
            timeout=request.timeout,
            agent=self.name,
            on_line=session_id_watcher(request),
            max_runtime=request.max_runtime,
        )
        parsed = _parse_stream(run.stdout)
        return DriverResult(
            agent=self.name,
            session_id=parsed.session_id or request.resume_session,
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

    def parse_partial(self, stdout: str) -> DriverResult:
        parsed = _parse_stream(stdout)
        return DriverResult(
            agent=self.name,
            session_id=parsed.session_id,
            text=parsed.text,
            exit_code=-1,
            stderr="",
            subtype=parsed.subtype,
            permission_denials=parsed.permission_denials,
            total_cost_usd=parsed.total_cost_usd,
            num_turns=parsed.num_turns,
            usage=parsed.usage,
        )


def _decode_events(stdout: str) -> list[dict]:
    """Decode vendor output into events, whatever framing it arrived in."""
    text = stdout.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(payload, list):
            return [event for event in payload if isinstance(event, dict)]
        if isinstance(payload, dict):
            return [payload]
        return []
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A stream cut mid-line, or a stray non-JSON banner.  Everything
            # already decoded still counts.
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


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


def _parse_stream(stdout: str) -> ClaudeParsed:
    """Extract the authoritative outcome fields from claude's output.

    Handles all three shapes the CLI can produce: the ``stream-json`` JSONL we
    ask for, the single object of the older ``--output-format json``, and a
    list.  Besides the session id and result text it keeps the vendor's own
    ``is_error`` / ``subtype`` / ``permission_denials`` / ``total_cost_usd`` /
    ``num_turns`` so the caller does not guess success.

    A truncated stream must degrade rather than raise: this is also the parser
    for a run that was killed, where the last line is routinely half-written.
    """
    session_id: str | None = None
    parts: list[str] = []
    is_error: bool | None = None
    subtype: str | None = None
    permission_denials: tuple[str, ...] = ()
    total_cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict | None = None
    events = _decode_events(stdout)
    if not events:
        return ClaudeParsed(session_id=None, text=stdout.strip())
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
