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
    assert "ai-room \u6d3e\u53d1\u53f0\u8d26" in content
    assert "### 2026-08-04T14:00:00+08:00 - claude [`ok`]" in content
    assert "\u5b50 agent session id: `abc123`" in content
    assert "\u7eed\u63a5: `claude -r abc123`" in content


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
    assert content.count("ai-room \u6d3e\u53d1\u53f0\u8d26") == 1
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
    assert "\u5b50 agent session id: `unknown`" in content
    assert "\u65e0\u4f1a\u8bdd id\uff0c\u65e0\u6cd5\u7eed\u63a5" in content