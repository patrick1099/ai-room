"""OpenCode (opencode) headless driver."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .process import find_binary, run_cli
from .protocol import Driver, DriverRequest, DriverResult, compose_prompt


class OpenCodeDriver(Driver):
    name = "opencode"
    _BINARY_CANDIDATES = ("opencode", "opencode.cmd", "opencode.ps1")

    def invoke(self, request: DriverRequest) -> DriverResult:
        binary = find_binary(self.name, self._BINARY_CANDIDATES)
        base = _binary_command(binary)
        command: list[str] = list(base) + ["run", "--format", "json"]
        # opencode run ignores the process cwd entirely and reuses whatever
        # project it last remembered, so --dir must be passed on every tier.
        command += ["--dir", str(request.cwd)]
        # The agent name and the permission tier are independent: naming an
        # agent must not quietly cost the sub-agent its write access.
        if request.agent_name:
            command += ["--agent", request.agent_name]
        elif request.read_only:
            command += ["--agent", "plan"]
        if not request.read_only:
            # opencode has no third permission level; full-access maps to the
            # same --auto flag as workspace-write.
            command += ["--auto"]
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
    """Return the argv prefix that runs opencode without mangling the prompt.

    On Windows ``opencode`` on PATH is an npm shim whose body is
    ``"...\\opencode.exe" %*``.  Routing through ``cmd /c`` to run it **cuts
    every argument at its first newline**: cmd re-parses the command line, and
    a prompt is one argument containing newlines.  Since ``compose_prompt``
    appends a "Relevant documents" / read-only block, essentially every
    dispatch is multi-line, and the sub-agent would receive only the first line
    while still exiting 0 with a plausible-looking answer.  Verified directly:
    an argument of ``"line one\\nline two"`` arrives as ``"line one"``.

    So resolve the shim to the real executable and launch that instead.
    """
    if os.name != "nt" or not binary.lower().endswith((".ps1", ".cmd")):
        return [binary]
    real = _resolve_shim(Path(binary))
    if real is not None:
        return [str(real)]
    # Last resort: the prompt may be truncated, but a collapsed prompt still
    # beats not running at all.
    return ["cmd", "/c", binary]


def _resolve_shim(shim: Path) -> Path | None:
    """Find the .exe an npm .cmd/.ps1 shim delegates to, if we can."""
    npm_layout = shim.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    if npm_layout.exists():
        return npm_layout
    # Fall back to reading the shim: npm writes the target as a quoted path.
    try:
        body = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for match in _EXE_IN_SHIM.finditer(body):
        candidate = Path(match.group(1).replace("%~dp0", str(shim.parent) + "\\"))
        candidate = Path(str(candidate).replace("%dp0%", str(shim.parent)))
        if candidate.exists():
            return candidate
    return None


_EXE_IN_SHIM = re.compile(r'"([^"]+\.exe)"', re.IGNORECASE)


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
