"""Streaming reader for current-format Codex session token events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ai_room.domain import ContextSample, ContextSource

from .base import ContextAdapter, unknown_sample


class _ContradictoryIdentityError(ValueError):
    """One Codex record carries incompatible recognized session identities."""


class CodexContextAdapter(ContextAdapter):
    """Discover and sample the transcript identified by ``CODEX_THREAD_ID``."""

    def __init__(self, *, environ: Mapping[str, str]) -> None:
        self._environ = dict(environ)

    @property
    def session_id(self) -> str | None:
        return self._environ.get("CODEX_THREAD_ID") or None

    def sample(self) -> ContextSample:
        session_id = self.session_id
        if session_id is None:
            return unknown_sample(
                None,
                "CODEX_THREAD_ID is missing; cannot identify the Codex session",
            )

        codex_home = self._environ.get("CODEX_HOME")
        home = Path(codex_home) if codex_home else Path.home() / ".codex"
        sessions_root = home / "sessions"
        if not sessions_root.is_dir():
            return unknown_sample(
                session_id,
                f"Codex sessions directory is not readable: {sessions_root}",
            )

        matches: list[Path] = []
        unreadable = 0
        try:
            transcripts = sessions_root.rglob("*.jsonl")
            for transcript in transcripts:
                try:
                    identified = _transcript_identifies_thread(
                        transcript,
                        session_id,
                    )
                except _ContradictoryIdentityError:
                    return unknown_sample(
                        session_id,
                        "Codex format drift: contradictory identities in "
                        f"transcript {transcript}",
                    )
                if identified is None:
                    unreadable += 1
                elif identified:
                    matches.append(transcript)
        except OSError as exc:
            return unknown_sample(
                session_id,
                f"failed to discover Codex transcripts: {exc}",
            )

        if not matches:
            detail = (
                f"; {unreadable} transcript(s) could not be read"
                if unreadable
                else ""
            )
            return unknown_sample(
                session_id,
                f"no Codex transcript identifies thread {session_id}{detail}",
            )
        if len(matches) > 1:
            return unknown_sample(
                session_id,
                f"ambiguous Codex transcript match for thread {session_id}: "
                f"{len(matches)} files",
            )
        return parse_codex_transcript(matches[0], session_id)


def parse_codex_transcript(
    transcript_path: Path,
    session_id: str,
) -> ContextSample:
    """Read only the latest matching current-format Codex token-count event."""
    latest: tuple[int, int | None] | None = None
    saw_token_count = False
    saw_identity = False
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

                identities = _codex_record_identities(record)
                if len(identities) > 1:
                    return unknown_sample(
                        session_id,
                        "Codex format drift: contradictory identities in "
                        "one transcript record",
                    )
                if identities:
                    saw_identity = True
                    if _is_session_meta(record) and session_id not in identities:
                        return unknown_sample(
                            session_id,
                            "Codex transcript identity does not match the "
                            f"requested thread {session_id}",
                        )

                payload = record.get("payload")
                if not (
                    record.get("type") == "event_msg"
                    and isinstance(payload, dict)
                    and payload.get("type") == "token_count"
                ):
                    continue
                if identities and session_id not in identities:
                    continue

                saw_token_count = True
                latest = _codex_usage(payload)
    except (OSError, UnicodeError) as exc:
        return unknown_sample(
            session_id,
            f"failed to read Codex transcript: {exc}",
        )

    if latest is None:
        details: list[str] = []
        if saw_token_count:
            details.append(
                "latest token_count record has an unsupported format"
            )
        else:
            details.append("no current-format token_count record was found")
        if invalid_lines:
            details.append(f"{invalid_lines} invalid JSON line(s) were skipped")
        if saw_identity:
            details.append("the transcript identity was validated")
        return unknown_sample(session_id, "Codex format drift: " + "; ".join(details))

    input_tokens, context_window = latest
    return ContextSample(
        input_tokens=input_tokens,
        context_window=context_window,
        source=ContextSource.CODEX_TOKEN_COUNT,
        session_id=session_id,
    )


def _transcript_identifies_thread(
    transcript_path: Path,
    session_id: str,
) -> bool | None:
    try:
        with transcript_path.open("r", encoding="utf-8") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                identities = _codex_record_identities(record)
                if identities:
                    if len(identities) > 1:
                        raise _ContradictoryIdentityError
                    return session_id in identities
    except (OSError, UnicodeError):
        return None
    return False


def _is_session_meta(record: Mapping[str, Any]) -> bool:
    return record.get("type") == "session_meta"


def _codex_record_identities(record: Mapping[str, Any]) -> set[str]:
    identities: set[str] = set()
    for key in ("session_id", "thread_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            identities.add(value)

    if _is_session_meta(record):
        payload = record.get("payload")
        if isinstance(payload, dict):
            for key in ("id", "session_id", "thread_id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    identities.add(value)
    return identities


def _codex_usage(payload: Mapping[str, Any]) -> tuple[int, int | None] | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    last_usage = info.get("last_token_usage")
    if not isinstance(last_usage, dict):
        return None
    input_tokens = last_usage.get("input_tokens")
    if not _is_token_count(input_tokens):
        return None

    context_window = info.get("model_context_window")
    if not _is_token_count(context_window):
        context_window = None
    return input_tokens, context_window


def _is_token_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
