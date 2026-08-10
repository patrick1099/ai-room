"""Handles of dispatches that are still running, written the moment they exist.

The ledger records what a dispatch *did*, which means it is written when the
dispatch is over.  That is exactly the case this file does not cover: ``ask`` is
synchronous, so the caller's own shell timeout can kill the whole ``ai-room``
process mid-run, and then no ``except`` branch runs and nothing is recorded at
all.  The turn was still billed, and without its handle it cannot be resumed --
the caller's only remaining move is to dispatch the same task again and pay for
it twice.

So the session id is written here the instant the sub-agent announces it,
seconds into the run, and the file is removed when the dispatch reports back
normally.  Whatever is left behind is therefore exactly the set of runs that
were killed from outside, which is what ``ai-room resume`` picks up.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .ledger import LEDGER_DIRECTORY, write_self_gitignore

INFLIGHT_DIRECTORY = "inflight"


@dataclass(frozen=True)
class InflightRun:
    """One dispatch that announced a handle and never reported back."""

    agent: str
    session_id: str
    question: str
    cwd: str
    started_at: str
    path: Path


def inflight_directory(root: Path) -> Path:
    return root / LEDGER_DIRECTORY / INFLIGHT_DIRECTORY


def record_inflight(
    root: Path,
    *,
    agent: str,
    session_id: str,
    question: str,
    cwd: Path,
) -> Path:
    """Write the handle of a running dispatch and return the file path.

    Named after the process so two shells dispatching at once cannot overwrite
    each other's record.
    """
    directory = inflight_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    write_self_gitignore(root / LEDGER_DIRECTORY)
    path = directory / f"{agent}-{os.getpid()}.json"
    payload = {
        "agent": agent,
        "session_id": session_id,
        "question": question[:400],
        "cwd": str(cwd),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def clear_inflight(path: Path | None) -> None:
    """Drop the record of a dispatch that reported back.

    Never raises: a dispatch that finished must not be turned into a failure by
    a bookkeeping file that was already gone.
    """
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def list_inflight(root: Path) -> list[InflightRun]:
    """Return the killed-from-outside dispatches, newest first."""
    directory = inflight_directory(root)
    if not directory.is_dir():
        return []
    runs: list[InflightRun] = []
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or not payload.get("session_id"):
            continue
        runs.append(
            InflightRun(
                agent=str(payload.get("agent") or ""),
                session_id=str(payload["session_id"]),
                question=str(payload.get("question") or ""),
                cwd=str(payload.get("cwd") or ""),
                started_at=str(payload.get("started_at") or ""),
                path=path,
            )
        )
    runs.sort(key=lambda run: run.started_at, reverse=True)
    return runs
