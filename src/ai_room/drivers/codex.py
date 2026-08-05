"""OpenAI Codex (codex) headless driver."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .process import find_binary, run_cli
from .protocol import Driver, DriverRequest, DriverResult, compose_prompt


_TIER_TO_SANDBOX = {
    "read-only": "read-only",
    "workspace-write": "workspace-write",
    "full-access": "danger-full-access",
}


class CodexDriver(Driver):
    name = "codex"
    _BINARY_CANDIDATES = ("codex", "codex.exe")

    def invoke(self, request: DriverRequest) -> DriverResult:
        binary = find_binary(self.name, self._BINARY_CANDIDATES)
        sandbox_mode = request.sandbox or _TIER_TO_SANDBOX[request.permission]
        command = [binary, "exec", "--json", "-s", sandbox_mode]
        command += ["-C", str(request.cwd)]
        for d in request.extra_dirs:
            command += ["--add-dir", d]
        if request.model:
            command += ["-m", request.model]
        if not _is_git_repo(request.cwd):
            command += ["--skip-git-repo-check"]
        command.append(compose_prompt(request))

        run = run_cli(command, cwd=request.cwd, timeout=request.timeout, agent=self.name)
        session_id, text, usage = _parse_jsonl(run.stdout)
        return DriverResult(
            agent=self.name,
            session_id=session_id,
            text=text,
            exit_code=run.returncode,
            stderr=run.stderr.strip(),
            usage=usage,
        )


def _is_git_repo(cwd: Path) -> bool:
    """Return True when ``cwd`` is inside a git work tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            shell=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"true"


def _parse_jsonl(stdout: str) -> tuple[str | None, str, dict | None]:
    """Extract the thread id, final agent text, and usage from codex JSONL.

    Real ``codex exec --json`` events carry the session handle on
    ``thread.started.thread_id``, the answer on ``item.completed`` with
    ``item.type == "agent_message"``, and token usage on ``turn.completed``.
    The legacy ``session_id`` / ``result`` branches are kept for older output.
    """
    session_id: str | None = None
    parts: list[str] = []
    usage: dict | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        sid = event.get("thread_id") or event.get("session_id")
        if sid:
            session_id = sid
        if event.get("type") == "turn.completed":
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text") or ""
                if text.strip():
                    parts.append(text)
        elif event.get("type") == "result":
            payload = event.get("payload") or {}
            if isinstance(payload, dict) and payload.get("status") == "completed":
                text = payload.get("text") or payload.get("final_text") or ""
                if text.strip():
                    parts.append(text)
    return session_id, "\n".join(parts), usage
