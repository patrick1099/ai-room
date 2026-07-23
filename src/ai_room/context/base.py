"""Shared contracts and environment-backed session detection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ai_room.domain import AgentName, ContextSample, ContextSource


class SessionDetectionError(ValueError):
    """The caller environment does not identify exactly one supported session."""


@dataclass(frozen=True)
class SessionIdentity:
    """Identity and transcript location for one interactive AI session."""

    agent: AgentName
    session_id: str
    transcript_path: Path | None


class ContextAdapter(ABC):
    """Read one current-session context sample."""

    @abstractmethod
    def sample(self) -> ContextSample:
        """Return a real context sample or an explicit unknown sample."""


def unknown_sample(session_id: str | None, reason: str) -> ContextSample:
    """Build the fail-closed result shared by transcript adapters."""
    return ContextSample(
        input_tokens=None,
        context_window=None,
        source=ContextSource.UNKNOWN,
        session_id=session_id,
        unknown_reason=reason,
    )


def detect_current_session(environ: Mapping[str, str]) -> SessionIdentity:
    """Detect exactly one Codex or Claude session from the caller environment."""
    codex_session_id = environ.get("CODEX_THREAD_ID", "").strip()
    claude_session_id = environ.get("AI_ROOM_CLAUDE_SESSION_ID", "").strip()
    claude_transcript = environ.get(
        "AI_ROOM_CLAUDE_TRANSCRIPT_PATH",
        "",
    ).strip()

    if bool(claude_session_id) != bool(claude_transcript):
        raise SessionDetectionError(
            "incomplete Claude session environment: both "
            "AI_ROOM_CLAUDE_SESSION_ID and AI_ROOM_CLAUDE_TRANSCRIPT_PATH "
            "are required"
        )

    has_codex = bool(codex_session_id)
    has_claude = bool(claude_session_id)
    if has_codex and has_claude:
        raise SessionDetectionError(
            "both Codex and Claude sessions were detected; run ai-room from "
            "a terminal belonging to only one interactive session"
        )
    if not has_codex and not has_claude:
        raise SessionDetectionError(
            "no current AI session detected; expected CODEX_THREAD_ID or "
            "the Claude SessionStart environment values"
        )
    if has_codex:
        return SessionIdentity(
            agent=AgentName.CODEX,
            session_id=codex_session_id,
            transcript_path=None,
        )
    return SessionIdentity(
        agent=AgentName.CLAUDE,
        session_id=claude_session_id,
        transcript_path=Path(claude_transcript),
    )


def adapter_for(
    agent: AgentName,
    environ: Mapping[str, str],
) -> ContextAdapter:
    """Build the adapter for the detected caller and verify its requested role."""
    identity = detect_current_session(environ)
    if identity.agent is not agent:
        raise SessionDetectionError(
            f"requested {agent.value} but detected {identity.agent.value} "
            f"session {identity.session_id}"
        )

    if agent is AgentName.CODEX:
        from .codex import CodexContextAdapter

        return CodexContextAdapter(environ=environ)

    from .claude import ClaudeContextAdapter

    if identity.transcript_path is None:
        raise SessionDetectionError("detected Claude session has no transcript path")
    return ClaudeContextAdapter(
        transcript_path=identity.transcript_path,
        session_id=identity.session_id,
    )
