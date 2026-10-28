"""DeepSeek Harness (dsh) headless driver."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .process import find_binary, run_cli
from .protocol import (
    Driver,
    DriverRequest,
    DriverResult,
    compose_prompt,
    session_id_watcher,
)


class DshDriver(Driver):
    name = "dsh"
    _BINARY_CANDIDATES = ("dsh", "dsh.cmd", "dsh.ps1")

    def invoke(self, request: DriverRequest) -> DriverResult:
        binary = find_binary(self.name, self._BINARY_CANDIDATES)
        base = _binary_command(binary)
        command = base + ["--profile", "dsh-agent", "--format", "jsonl"]
        if request.resume_session:
            command += ["--session", request.resume_session]
        command += ["--cwd", str(request.cwd)]
        command += ["--permission", request.permission]
        if request.model:
            command += ["--model", request.model]
        command.append(compose_prompt(request))

        run = run_cli(
            command,
            cwd=request.cwd,
            timeout=request.timeout,
            agent=self.name,
            on_line=session_id_watcher(request),
            max_runtime=request.max_runtime,
        )
        session_id, text = _parse_jsonl(run.stdout)
        return DriverResult(
            agent=self.name,
            session_id=session_id or request.resume_session,
            text=text,
            exit_code=run.returncode,
            stderr=run.stderr.strip(),
        )

    def parse_partial(self, stdout: str) -> DriverResult:
        session_id, text = _parse_jsonl(stdout)
        return DriverResult(
            agent=self.name,
            session_id=session_id,
            text=text,
            exit_code=-1,
            stderr="",
        )


def _binary_command(binary: str) -> list[str]:
    """Return the argv prefix that runs dsh without mangling the prompt.

    On Windows ``dsh`` on PATH is an npm shim whose body launches
    ``node.exe .../@deepseek-ai/dsh/lib/bin.js %*``.  Routing through
    ``cmd /c`` to run it cuts every argument at its first newline: cmd
    re-parses the command line, and a prompt is one argument containing
    newlines.  Resolve the shim to the real node executable and script and
    launch those directly instead.
    """
    if os.name != "nt" or not binary.lower().endswith((".cmd", ".ps1")):
        return [binary]
    resolved = _resolve_shim(Path(binary))
    if resolved is not None:
        return resolved
    # Last resort: the prompt may be truncated, but a collapsed prompt still
    # beats not running at all.
    return ["cmd", "/c", binary]


def _resolve_shim(shim: Path) -> list[str] | None:
    """Find the node executable and dsh bin script an npm shim delegates to."""
    node = shim.parent / "node.exe"
    bin_js = (
        shim.parent
        / "node_modules"
        / "@deepseek-ai"
        / "dsh"
        / "lib"
        / "bin.js"
    )
    if node.exists() and bin_js.exists():
        return [str(node), str(bin_js)]

    # Fall back to reading the shim: npm writes the targets as quoted paths.
    try:
        body = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    node_match = _NODE_EXE_IN_SHIM.search(body)
    bin_js_match = _BIN_JS_IN_SHIM.search(body)
    if node_match and bin_js_match:
        node = _expand_shim_path(shim, node_match.group(1))
        bin_js = _expand_shim_path(shim, bin_js_match.group(1))
        if node.exists() and bin_js.exists():
            return [str(node), str(bin_js)]
    return None


def _expand_shim_path(shim: Path, raw: str) -> Path:
    """Resolve an npm shim's ``%dp0%``/``%~dp0`` placeholders to a real path."""
    if raw.startswith("%~dp0"):
        return shim.parent / raw[len("%~dp0") :].lstrip("\\/")
    if raw.startswith("%dp0%"):
        return shim.parent / raw[len("%dp0%") :].lstrip("\\/")
    return Path(raw)


_NODE_EXE_IN_SHIM = re.compile(r'"([^"]*node\.exe)"', re.IGNORECASE)
_BIN_JS_IN_SHIM = re.compile(
    r'"([^"]*@deepseek-ai[\\/]dsh[\\/]lib[\\/]bin\.js)"',
    re.IGNORECASE,
)


def _parse_jsonl(stdout: str) -> tuple[str | None, str]:
    """Extract the session id and final text from dsh JSONL output.

    dsh's JSONL protocol announces the session on the top-level ``session_id``
    of every event, streams reply fragments as ``assistant.delta`` events, and
    closes with an optional ``run.completed`` event carrying the final text.
    The completed text is authoritative when present; otherwise deltas are
    joined in arrival order.
    """
    session_id: str | None = None
    deltas: list[str] = []
    final_text: str | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        sid = event.get("session_id")
        if sid:
            session_id = sid
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event_type == "assistant.delta":
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                deltas.append(text)
        elif event_type == "run.completed":
            text = payload.get("text")
            if isinstance(text, str):
                final_text = text
    if final_text is not None:
        return session_id, final_text
    return session_id, "".join(deltas)
