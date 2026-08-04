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
    "# ai-room \u6d3e\u53d1\u53f0\u8d26\n\n"
    "> \u81ea\u52a8\u751f\u6210\uff0c\u52ff\u624b\u6539\u3002\u6bcf\u6b21 "
    "`ai-room ask` \u65e0\u5934\u6d3e\u53d1\u540e\u8ffd\u52a0\u4e00\u6761\u3002\n"
    "> \u5b50 agent session id \u7528\u4e8e "
    "`-r/--resume` \u7eed\u63a5\u8be5\u6b21\u4f1a\u8bdd\u3002\n"
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
    violations: tuple[str, ...] = ()


def append_ledger(root: Path, entry: LedgerEntry) -> Path:
    """Append ``entry`` to ``<root>/.ai-room/ledger.md`` and return its path."""
    path = root / LEDGER_DIRECTORY / LEDGER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_self_gitignore(path.parent)
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


def _write_self_gitignore(directory: Path) -> None:
    """Ignore the whole ``.ai-room`` ledger directory so it is never committed.

    ``.ai-room`` in a project root is used only for the ledger; the room state
    lives under LOCALAPPDATA.  Without this, dispatching against a company
    firmware repo would accidentally commit the ledger.
    """
    ignore = directory / ".gitignore"
    if not ignore.exists():
        with ignore.open("w", encoding="utf-8") as stream:
            stream.write("*\n")


def _single_line(text: str, limit: int = 200) -> str:
    """Collapse whitespace and truncate so a question cannot break the ledger."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "..."
    return cleaned


def _format_entry(entry: LedgerEntry) -> str:
    timestamp = entry.timestamp or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    docs = ", ".join(entry.related_docs) if entry.related_docs else "(none)"
    resume = _resume_hint(entry.agent, entry.session_id)
    lines = [
        f"\n### {timestamp} - {entry.agent} [`{entry.status}`]",
        f"- \u72b6\u6001: {entry.status} (exit {entry.exit_code})",
        f"- \u6a21\u578b: {entry.model or '(default)'}",
        f"- \u95ee\u9898: {_single_line(entry.question)}",
        f"- \u76f8\u5173\u6587\u6863: {docs}",
        f"- \u5b50 agent session id: `{entry.session_id or 'unknown'}`",
        f"- \u7eed\u63a5: {resume}",
    ]
    if entry.violations:
        lines.append(
            f"- \u8d8a\u754c\u6587\u4ef6: {', '.join(entry.violations)}"
        )
    return "\n".join(lines) + "\n"


def _resume_hint(agent: str, session_id: str | None) -> str:
    if not session_id:
        return "(\u65e0\u4f1a\u8bdd id\uff0c\u65e0\u6cd5\u7eed\u63a5)"
    if agent == "claude":
        return f"`claude -r {session_id}`"
    if agent == "codex":
        return f"`codex exec resume {session_id}`"
    if agent == "opencode":
        return f"`opencode run --session {session_id}`"
    return session_id