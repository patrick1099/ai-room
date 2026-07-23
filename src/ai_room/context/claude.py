"""Streaming reader for current-format Claude assistant usage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_room.domain import ContextSample, ContextSource

from .base import ContextAdapter, unknown_sample


class ClaudeContextAdapter(ContextAdapter):
    """Sample the transcript registered by the Claude SessionStart hook."""

    def __init__(self, *, transcript_path: Path, session_id: str) -> None:
        self._transcript_path = transcript_path
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def sample(self) -> ContextSample:
        return parse_claude_transcript(
            self._transcript_path,
            self._session_id,
        )


def parse_claude_transcript(
    transcript_path: Path,
    session_id: str,
) -> ContextSample:
    """Read only the latest matching assistant event's current usage fields."""
    latest: int | None = None
    saw_assistant = False
    saw_mismatched_identity = False
    invalid_lines = 0

    try:
        with transcript_path.open("r", encoding="utf-8") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    invalid_lines += 1
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "assistant":
                    continue

                identity = _claude_record_identity(record)
                if identity is not None and identity != session_id:
                    saw_mismatched_identity = True
                    continue

                saw_assistant = True
                latest = _claude_usage(record)
    except (OSError, UnicodeError) as exc:
        return unknown_sample(
            session_id,
            f"failed to read Claude transcript: {exc}",
        )

    if latest is None:
        details: list[str] = []
        if saw_assistant:
            details.append(
                "latest assistant event has an unsupported usage format"
            )
        elif saw_mismatched_identity:
            details.append(
                "assistant event identity does not match the requested session"
            )
        else:
            details.append("no assistant event was found")
        if invalid_lines:
            details.append(f"{invalid_lines} invalid JSON line(s) were skipped")
        return unknown_sample(
            session_id,
            "Claude format drift: " + "; ".join(details),
        )

    return ContextSample(
        input_tokens=latest,
        context_window=None,
        source=ContextSource.CLAUDE_USAGE,
        session_id=session_id,
    )


def _claude_record_identity(record: dict[str, Any]) -> str | None:
    for key in ("sessionId", "session_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _claude_usage(record: dict[str, Any]) -> int | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    components = (
        usage.get("input_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("cache_creation_input_tokens"),
    )
    if not all(_is_token_count(value) for value in components):
        return None
    return sum(components)


def _is_token_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
