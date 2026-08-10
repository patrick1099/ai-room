"""Append-only dispatch ledger written into the project folder.

Each ``ai-room ask`` headless dispatch appends one block to
``<root>/.ai-room/ledger.md``, recording which agent received the task, the
question, and the sub-agent session id so the conversation can be resumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LEDGER_DIRECTORY = ".ai-room"
LEDGER_FILENAME = "ledger.md"

_HEADER = (
    "# ai-room \u6d3e\u53d1\u53f0\u8d26\n\n"
    "> \u81ea\u52a8\u751f\u6210\uff0c\u52ff\u624b\u6539\u3002\u6bcf\u6b21 "
    "`ai-room ask` \u65e0\u5934\u6d3e\u53d1\u540e\u8ffd\u52a0\u4e00\u6761\u3002\n"
    "> \u5b50 agent session id \u7528\u4e8e\u7eed\u63a5\u8be5\u6b21\u4f1a\u8bdd\uff0c"
    "\u7eed\u63a5\u547d\u4ee4\u89c1\u5404\u6761\u8bb0\u5f55\u3002\n"
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
    #: The dispatch receipt: which files the sub-agent moved.  Recorded for
    #: review, never for enforcement -- see :mod:`ai_room.receipt`.
    changed_files: tuple[str, ...] = ()
    sender: str | None = None
    is_error: bool | None = None
    subtype: str | None = None
    permission_denials: tuple[str, ...] = ()
    total_cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict | None = None


def append_ledger(root: Path, entry: LedgerEntry) -> Path:
    """Append ``entry`` to ``<root>/.ai-room/ledger.md`` and return its path."""
    path = root / LEDGER_DIRECTORY / LEDGER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    write_self_gitignore(path.parent)
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


def write_self_gitignore(directory: Path) -> None:
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


def _single_line_json(value: dict, limit: int = 200) -> str:
    """Render a usage dict as one collapsed, truncated line."""
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return _single_line(rendered, limit)


def _format_entry(entry: LedgerEntry) -> str:
    timestamp = entry.timestamp or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    docs = ", ".join(entry.related_docs) if entry.related_docs else "(none)"
    command = resume_hint(entry.agent, entry.session_id)
    resume = (
        f"`{command}`"
        if command
        else "(无会话 id，无法续接)"
    )
    lines = [
        f"\n### {timestamp} - {entry.agent} [`{entry.status}`]",
        f"- \u72b6\u6001: {entry.status} (exit {entry.exit_code})",
        f"- \u53d1\u8d77\u8005: {entry.sender or '(unknown)'}",
        f"- \u6a21\u578b: {entry.model or '(default)'}",
        f"- \u95ee\u9898: {_single_line(entry.question)}",
        f"- \u76f8\u5173\u6587\u6863: {docs}",
        f"- \u5b50 agent session id: `{entry.session_id or 'unknown'}`",
        f"- \u7eed\u63a5: {resume}",
    ]
    if entry.is_error is not None:
        lines.append(
            f"- \u4f9b\u5e94\u5546 is_error: {entry.is_error}"
            + (f" ({entry.subtype})" if entry.subtype else "")
        )
    if entry.permission_denials:
        lines.append(
            f"- \u88ab\u62e6\u4e0b\u7684\u8d8a\u754c\u5c1d\u8bd5: "
            f"{', '.join(entry.permission_denials)}"
        )
    if entry.total_cost_usd is not None or entry.num_turns is not None:
        cost = f"{entry.total_cost_usd:.6f}" if entry.total_cost_usd is not None else "?"
        turns = entry.num_turns if entry.num_turns is not None else "?"
        lines.append(f"- \u8d39\u7528: ${cost} USD, {turns} turn(s)")
    if entry.usage:
        lines.append(f"- usage: {_single_line_json(entry.usage)}")
    if entry.changed_files:
        lines.append(
            f"- \u6539\u52a8\u56de\u6267: {', '.join(entry.changed_files)}"
        )
    return "\n".join(lines) + "\n"


def resume_hint(agent: str, session_id: str | None) -> str | None:
    """The vendor's own command for continuing ``session_id``, if there is one.

    Kept alongside ai-room's ``resume`` for the case ai-room cannot serve: the
    handle is still usable by hand long after the room, the ledger or ai-room
    itself is out of the picture.
    """
    if not session_id:
        return None
    if agent == "claude":
        return f"claude -r {session_id}"
    if agent == "codex":
        return f"codex exec resume {session_id}"
    if agent == "opencode":
        return f"opencode run --session {session_id}"
    return session_id
