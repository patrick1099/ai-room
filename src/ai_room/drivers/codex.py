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

#: codex's effective permission is not decided by ``-s`` alone.  An approving
#: reviewer turns a sandbox denial into an escalation that runs *outside* the
#: sandbox, so both axes must be set together and neither may be inherited from
#: whatever the machine's config.toml happens to say.  Both directions were
#: verified against the real CLI:
#:
#: - ``-s read-only`` on a machine configured with ``auto_review`` wrote both
#:   files it was told it could not write.  Read-only is not a boundary while
#:   approvals are on.
#: - ``-s workspace-write`` with ``approval_policy=never`` produced a file the
#:   caller could not read at all.  This machine's sandbox helper is broken, so
#:   the sandboxed write failed and the fallback left the file owned by the
#:   sandbox principal: exit 0, the receipt lists it, nobody can open it.
#:   Permitting escalation is what makes the write land as a normal file.
#:
#: ``on-failure`` rather than ``on-request``: escalate only once the sandbox has
#: actually refused, not whenever the model asks.
_TIER_TO_APPROVAL = {
    "read-only": ('approval_policy="never"',),
    "workspace-write": (
        'approval_policy="on-failure"',
        "approvals_reviewer=auto_review",
    ),
    # danger-full-access has no sandbox to refuse anything, so there is nothing
    # to escalate and no reason to leave approvals on.
    "full-access": ('approval_policy="never"',),
}


class CodexDriver(Driver):
    name = "codex"
    _BINARY_CANDIDATES = ("codex", "codex.exe")

    def invoke(self, request: DriverRequest) -> DriverResult:
        binary = find_binary(self.name, self._BINARY_CANDIDATES)
        sandbox_mode = request.sandbox or _TIER_TO_SANDBOX[request.permission]
        command = [binary, "exec", "--json", "-s", sandbox_mode]
        for override in _TIER_TO_APPROVAL[request.permission]:
            command += ["-c", override]
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
