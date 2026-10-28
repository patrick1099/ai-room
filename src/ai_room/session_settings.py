"""The model and effort each sub-agent session ran with, kept for resuming.

A resumed turn is the same conversation, so it has to run on the same model at
the same effort as the turn it continues.  The vendors do not reliably carry
either across a resume, and the caller of ``ai-room resume`` usually has only a
handle, so the settings are recorded here under that handle when the session
first announces it, and read back when it is resumed.

One file per session rather than one shared index: two shells dispatching at
once must not be able to overwrite each other's record.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .ledger import LEDGER_DIRECTORY, write_self_gitignore

SESSIONS_DIRECTORY = "sessions"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class SessionSettings:
    model: str | None
    effort: str | None


def _settings_path(root: Path, agent: str, session_id: str) -> Path:
    name = _UNSAFE.sub("_", f"{agent}-{session_id}")
    return root / LEDGER_DIRECTORY / SESSIONS_DIRECTORY / f"{name}.json"


def record_settings(
    root: Path,
    *,
    agent: str,
    session_id: str,
    settings: SessionSettings,
) -> None:
    """Remember what ``session_id`` ran with.  Never raises."""
    path = _settings_path(root, agent, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_self_gitignore(root / LEDGER_DIRECTORY)
        path.write_text(
            json.dumps(
                {"model": settings.model, "effort": settings.effort},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def lookup_settings(
    root: Path, *, agent: str, session_id: str
) -> SessionSettings | None:
    """What ``session_id`` last ran with, or None when it was never recorded."""
    try:
        payload = json.loads(
            _settings_path(root, agent, session_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    effort = payload.get("effort")
    return SessionSettings(
        model=model if isinstance(model, str) else None,
        effort=effort if isinstance(effort, str) else None,
    )
