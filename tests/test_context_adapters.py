"""Contract tests for real Codex and Claude context sampling."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from ai_room.context import (
    SessionDetectionError,
    adapter_for,
    detect_current_session,
)
from ai_room.context.claude import (
    ClaudeContextAdapter,
    parse_claude_transcript,
)
from ai_room.context.codex import CodexContextAdapter, parse_codex_transcript
from ai_room.domain import AgentName, ContextSource
from ai_room.hooks import claude_session_start


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def test_codex_reads_latest_input_tokens(fixture_dir: Path) -> None:
    sample = parse_codex_transcript(
        fixture_dir / "codex_session.jsonl",
        "thread-123",
    )

    assert sample.input_tokens == 156_000
    assert sample.context_window == 258_400
    assert sample.source is ContextSource.CODEX_TOKEN_COUNT
    assert sample.session_id == "thread-123"


def test_claude_sums_current_context_components(fixture_dir: Path) -> None:
    sample = parse_claude_transcript(
        fixture_dir / "claude_session.jsonl",
        "session-123",
    )

    assert sample.input_tokens == 156_000
    assert sample.context_window is None
    assert sample.source is ContextSource.CLAUDE_USAGE
    assert sample.session_id == "session-123"


@pytest.mark.parametrize(
    ("fixture", "parser", "session_id"),
    [
        ("codex_session_changed.jsonl", parse_codex_transcript, "thread-123"),
        ("claude_session_changed.jsonl", parse_claude_transcript, "session-123"),
    ],
)
def test_format_drift_returns_unknown(
    fixture_dir: Path,
    fixture: str,
    parser: object,
    session_id: str,
) -> None:
    sample = parser(fixture_dir / fixture, session_id)  # type: ignore[operator]

    assert sample.input_tokens is None
    assert sample.source is ContextSource.UNKNOWN
    assert sample.unknown_reason is not None
    assert "format" in sample.unknown_reason.lower()


@pytest.mark.parametrize(
    ("parser", "session_id"),
    [
        (parse_codex_transcript, "thread-123"),
        (parse_claude_transcript, "session-123"),
    ],
)
def test_inaccessible_transcript_returns_unknown(
    tmp_path: Path,
    parser: object,
    session_id: str,
) -> None:
    sample = parser(tmp_path / "missing.jsonl", session_id)  # type: ignore[operator]

    assert sample.input_tokens is None
    assert sample.source is ContextSource.UNKNOWN
    assert "read" in (sample.unknown_reason or "").lower()


def test_invalid_lines_are_reported_when_no_usage_exists(tmp_path: Path) -> None:
    transcript = tmp_path / "broken.jsonl"
    transcript.write_text(
        '{"type":"session_meta","payload":{"id":"thread-123"}}\n'
        "{not-json}\n",
        encoding="utf-8",
    )

    sample = parse_codex_transcript(transcript, "thread-123")

    assert sample.input_tokens is None
    assert "invalid json" in (sample.unknown_reason or "").lower()


def test_codex_rejects_mismatched_transcript_identity(
    fixture_dir: Path,
) -> None:
    sample = parse_codex_transcript(
        fixture_dir / "codex_session.jsonl",
        "another-thread",
    )

    assert sample.input_tokens is None
    assert "identity" in (sample.unknown_reason or "").lower()


def test_codex_rejects_contradictory_record_identities(
    fixture_dir: Path,
) -> None:
    sample = parse_codex_transcript(
        fixture_dir / "codex_session_contradictory.jsonl",
        "thread-123",
    )

    assert sample.input_tokens is None
    assert "contradictory" in (sample.unknown_reason or "").lower()


def test_codex_discovery_rejects_contradictory_record_identities(
    tmp_path: Path,
    fixture_dir: Path,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "contradictory.jsonl").write_text(
        (
            fixture_dir / "codex_session_contradictory.jsonl"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    adapter = CodexContextAdapter(
        environ={
            "CODEX_THREAD_ID": "thread-123",
            "CODEX_HOME": str(tmp_path),
        }
    )

    sample = adapter.sample()

    assert sample.input_tokens is None
    assert "contradictory" in (sample.unknown_reason or "").lower()


def test_codex_discovery_ignores_unrelated_contradictory_transcript(
    tmp_path: Path,
    fixture_dir: Path,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for fixture in (
        "codex_session_contradictory_unrelated.jsonl",
        "codex_session.jsonl",
    ):
        (sessions / fixture).write_text(
            (fixture_dir / fixture).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    adapter = CodexContextAdapter(
        environ={
            "CODEX_THREAD_ID": "thread-123",
            "CODEX_HOME": str(tmp_path),
        }
    )

    sample = adapter.sample()

    assert sample.input_tokens == 156_000
    assert sample.source is ContextSource.CODEX_TOKEN_COUNT


def test_claude_uses_latest_assistant_event_even_when_it_drifted(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        '{"type":"assistant","sessionId":"session-123","message":{"usage":'
        '{"input_tokens":1,"cache_read_input_tokens":2,'
        '"cache_creation_input_tokens":3}}}\n'
        '{"type":"assistant","sessionId":"session-123","message":{}}\n',
        encoding="utf-8",
    )

    sample = parse_claude_transcript(transcript, "session-123")

    assert sample.input_tokens is None
    assert "format" in (sample.unknown_reason or "").lower()


def test_detect_current_codex_session() -> None:
    identity = detect_current_session({"CODEX_THREAD_ID": "thread-123"})

    assert identity.agent is AgentName.CODEX
    assert identity.session_id == "thread-123"
    assert identity.transcript_path is None


def test_detect_current_claude_session() -> None:
    identity = detect_current_session(
        {
            "AI_ROOM_CLAUDE_SESSION_ID": "session-123",
            "AI_ROOM_CLAUDE_TRANSCRIPT_PATH": r"C:\记录\会话 123.jsonl",
        }
    )

    assert identity.agent is AgentName.CLAUDE
    assert identity.session_id == "session-123"
    assert identity.transcript_path == Path(r"C:\记录\会话 123.jsonl")


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({}, "no current"),
        (
            {
                "CODEX_THREAD_ID": "thread-123",
                "AI_ROOM_CLAUDE_SESSION_ID": "session-123",
                "AI_ROOM_CLAUDE_TRANSCRIPT_PATH": "claude.jsonl",
            },
            "both",
        ),
        ({"AI_ROOM_CLAUDE_SESSION_ID": "session-123"}, "incomplete"),
    ],
)
def test_detect_current_session_fails_actionably(
    environ: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(SessionDetectionError, match=message):
        detect_current_session(environ)


def test_adapter_for_rejects_mismatched_join_identity() -> None:
    with pytest.raises(SessionDetectionError, match="requested claude.*detected codex"):
        adapter_for(AgentName.CLAUDE, {"CODEX_THREAD_ID": "thread-123"})


def test_adapter_for_keeps_two_sessions_in_same_room_distinct(
    fixture_dir: Path,
) -> None:
    first = adapter_for(
        AgentName.CLAUDE,
        {
            "AI_ROOM_CLAUDE_SESSION_ID": "session-123",
            "AI_ROOM_CLAUDE_TRANSCRIPT_PATH": str(
                fixture_dir / "claude_session.jsonl"
            ),
        },
    )
    second = adapter_for(
        AgentName.CLAUDE,
        {
            "AI_ROOM_CLAUDE_SESSION_ID": "session-456",
            "AI_ROOM_CLAUDE_TRANSCRIPT_PATH": str(
                fixture_dir / "claude_session_changed.jsonl"
            ),
        },
    )

    assert isinstance(first, ClaudeContextAdapter)
    assert isinstance(second, ClaudeContextAdapter)
    assert first.session_id == "session-123"
    assert second.session_id == "session-456"


def test_codex_adapter_discovers_transcript_by_contents(
    tmp_path: Path,
    fixture_dir: Path,
) -> None:
    sessions = tmp_path / "sessions" / "2026" / "07"
    sessions.mkdir(parents=True)
    (sessions / "wrong-name.jsonl").write_text(
        (fixture_dir / "codex_session.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    adapter = adapter_for(
        AgentName.CODEX,
        {
            "CODEX_THREAD_ID": "thread-123",
            "CODEX_HOME": str(tmp_path),
        },
    )

    assert isinstance(adapter, CodexContextAdapter)
    assert adapter.sample().input_tokens == 156_000


def test_codex_adapter_returns_unknown_without_thread_id(tmp_path: Path) -> None:
    adapter = CodexContextAdapter(environ={"CODEX_HOME": str(tmp_path)})

    sample = adapter.sample()

    assert sample.input_tokens is None
    assert "CODEX_THREAD_ID" in (sample.unknown_reason or "")


def test_codex_adapter_returns_unknown_for_ambiguous_content_matches(
    tmp_path: Path,
    fixture_dir: Path,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    contents = (fixture_dir / "codex_session.jsonl").read_text(encoding="utf-8")
    (sessions / "one.jsonl").write_text(contents, encoding="utf-8")
    (sessions / "two.jsonl").write_text(contents, encoding="utf-8")
    adapter = CodexContextAdapter(
        environ={
            "CODEX_THREAD_ID": "thread-123",
            "CODEX_HOME": str(tmp_path),
        }
    )

    sample = adapter.sample()

    assert sample.input_tokens is None
    assert "ambiguous" in (sample.unknown_reason or "").lower()


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_claude_session_start_exports_only_intended_values(
    tmp_path: Path,
    source: str,
) -> None:
    env_file = tmp_path / "claude env.sh"
    local_app_data = tmp_path / "local app data"
    transcript = tmp_path / "记录 文件夹" / "会话 '一'.jsonl"
    project = tmp_path / "项目 根"
    payload = {
        "session_id": "session-123",
        "transcript_path": str(transcript),
        "cwd": str(project),
        "source": source,
        "untrusted_extra": "must-not-be-exported",
    }

    result = claude_session_start.main(
        stdin=io.StringIO(json.dumps(payload, ensure_ascii=False)),
        environ={
            "CLAUDE_ENV_FILE": str(env_file),
            "LOCALAPPDATA": str(local_app_data),
        },
    )

    assert result == 0
    exports = env_file.read_text(encoding="utf-8").splitlines()
    assert exports == [
        "export AI_ROOM_CLAUDE_SESSION_ID='session-123'",
        (
            "export AI_ROOM_CLAUDE_TRANSCRIPT_PATH="
            f"'{str(transcript).replace(chr(39), chr(39) + chr(34) + chr(39) + chr(34) + chr(39))}'"
        ),
    ]
    registry = (
        local_app_data
        / "ai-room"
        / "claude-sessions"
        / "session-123.json"
    )
    record = json.loads(registry.read_text(encoding="utf-8"))
    assert record == {
        "cwd": str(project),
        "session_id": "session-123",
        "source": source,
        "transcript_path": str(transcript),
    }
    assert not project.exists()


@pytest.mark.parametrize("missing_key", ["session_id", "transcript_path", "cwd"])
def test_claude_session_start_rejects_missing_keys(
    tmp_path: Path,
    missing_key: str,
) -> None:
    payload = {
        "session_id": "session-123",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "cwd": str(tmp_path / "project"),
        "source": "startup",
    }
    del payload[missing_key]

    with pytest.raises(ValueError, match=missing_key):
        claude_session_start.main(
            stdin=io.StringIO(json.dumps(payload)),
            environ={"LOCALAPPDATA": str(tmp_path)},
        )


@pytest.mark.parametrize("unsafe_key", ["session_id", "transcript_path", "cwd"])
def test_claude_session_start_rejects_newlines(
    tmp_path: Path,
    unsafe_key: str,
) -> None:
    payload = {
        "session_id": "session-123",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "cwd": str(tmp_path / "project"),
        "source": "startup",
    }
    payload[unsafe_key] += "\nINJECTED=value"
    env_file = tmp_path / "env.sh"

    with pytest.raises(ValueError, match="newline"):
        claude_session_start.main(
            stdin=io.StringIO(json.dumps(payload)),
            environ={
                "CLAUDE_ENV_FILE": str(env_file),
                "LOCALAPPDATA": str(tmp_path),
            },
        )

    assert not env_file.exists()


def test_claude_session_start_atomically_replaces_registry_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_dir = tmp_path / "ai-room" / "claude-sessions"
    registry_dir.mkdir(parents=True)
    registry = registry_dir / "session-123.json"
    registry.write_text('{"old": true}', encoding="utf-8")
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def tracked_replace(source: str | Path, destination: str | Path) -> None:
        calls.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(claude_session_start.os, "replace", tracked_replace)
    payload = {
        "session_id": "session-123",
        "transcript_path": str(tmp_path / "new.jsonl"),
        "cwd": str(tmp_path / "project"),
        "source": "resume",
    }

    claude_session_start.main(
        stdin=io.StringIO(json.dumps(payload)),
        environ={"LOCALAPPDATA": str(tmp_path)},
    )

    assert len(calls) == 1
    temporary, destination = calls[0]
    assert temporary.parent == registry_dir
    assert destination == registry
    assert not temporary.exists()
    assert json.loads(registry.read_text(encoding="utf-8"))["source"] == "resume"
