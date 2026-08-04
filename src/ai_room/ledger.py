"""Append-only dispatch ledger written into the project folder.

Each ``ai-room ask`` headless dispatch appends one block to
``<root>/.ai-room/ledger.md``, recording which agent received the task, the
question, and the sub-agent session id so the conversation can be resumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LEDGER_DIRECTORY = ".ai-room"
LEDGER_FILENAME = "ledger.md"

_HEADER = (
    "# ai-room dispatch ledger\n\n"
    "> Auto-generated. Do not edit by hand. One block is appended per "
    "`ai-room ask` headless dispatch.\n"
    "> The sub-agent session id allows resuming that conversation with "
    "`-r/--resume`.\n"
)


@dataclass(frozen=True)
class LedgerEntry:
    """One recorded headless dispatch."""

    agent: str
    question: str
    session_id: str | None
    related_docs: tuple[str, ...]
    model: str | None
    exit_code: int
    status: str
    timestamp: str | None = None


def append_ledger(root: Path, entry: LedgerEntry) -> Path:
    """Append ``entry`` to ``<root>/.ai-room/ledger.md`` and return its path."""
    path = root / LEDGER_DIRECTORY / LEDGER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    block = _format_entry(entry)
    if path.exists():
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
            stream.write(block)
    else:
        with path.open("w", encoding="utf-8") as stream:
            stream.write(_HEADER)
            stream.write("\n")
            stream.write(block)
    return path


def _format_entry(entry: LedgerEntry) -> str:
    timestamp = entry.timestamp or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    docs = ", ".join(entry.related_docs) if entry.related_docs else "(none)"
    resume = _resume_hint(entry.agent, entry.session_id)
    return (
        f"\n### {timestamp} - {entry.agent} [`{entry.status}`]\n"
        f"- status: {entry.status} (exit {entry.exit_code})\n"
        f"- model: {entry.model or '(default)'}\n"
        f"- question: {entry.question}\n"
        f"- related docs: {docs}\n"
        f"- sub-agent session id: `{entry.session_id or 'unknown'}`\n"
        f"- resume: {resume}\n"
    )


def _resume_hint(agent: str, session_id: str | None) -> str:
    if not session_id:
        return "(no session id, cannot resume)"
    if agent == "claude":
        return f"`claude -r {session_id}`"
    if agent == "codex":
        return f"`codex exec --resume {session_id}`"
    if agent == "opencode":
        return f"`opencode run --session {session_id}`"
    return session_id