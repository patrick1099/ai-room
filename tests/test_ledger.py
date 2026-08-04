from __future__ import annotations

from pathlib import Path

from ai_room.ledger import LEDGER_FILENAME, LedgerEntry, append_ledger


def test_append_ledger_creates_file_with_header(tmp_path: Path) -> None:
    entry = LedgerEntry(
        agent="claude",
        question="review OTA risk",
        session_id="abc123",
        related_docs=("Code/protocol.c",),
        model=None,
        exit_code=0,
        status="ok",
        timestamp="2026-08-04T14:00:00+08:00",
    )

    path = append_ledger(tmp_path, entry)

    assert path == tmp_path / ".ai-room" / LEDGER_FILENAME
    content = path.read_text(encoding="utf-8")
    assert "ai-room dispatch ledger" in content
    assert "### 2026-08-04T14:00:00+08:00 - claude [`ok`]" in content
    assert "sub-agent session id: `abc123`" in content
    assert "resume: `claude -r abc123`" in content


def test_append_ledger_appends_without_duplicate_header(tmp_path: Path) -> None:
    first = LedgerEntry(
        agent="codex",
        question="question one",
        session_id="sid-1",
        related_docs=(),
        model=None,
        exit_code=0,
        status="ok",
        timestamp="2026-08-04T14:00:00+08:00",
    )
    second = LedgerEntry(
        agent="claude",
        question="question two",
        session_id="sid-2",
        related_docs=(),
        model=None,
        exit_code=1,
        status="error",
        timestamp="2026-08-04T14:05:00+08:00",
    )

    append_ledger(tmp_path, first)
    path = append_ledger(tmp_path, second)

    content = path.read_text(encoding="utf-8")
    assert content.count("ai-room dispatch ledger") == 1
    assert "sid-1" in content
    assert "sid-2" in content
    assert "`error`" in content


def test_resume_hint_unknown_session_id(tmp_path: Path) -> None:
    entry = LedgerEntry(
        agent="opencode",
        question="question",
        session_id=None,
        related_docs=(),
        model=None,
        exit_code=0,
        status="ok",
        timestamp="2026-08-04T14:00:00+08:00",
    )

    path = append_ledger(tmp_path, entry)

    content = path.read_text(encoding="utf-8")
    assert "sub-agent session id: `unknown`" in content
    assert "no session id, cannot resume" in content