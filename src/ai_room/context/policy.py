"""Pure policy for manual safe-boundary context compaction reminders."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_room.domain import ContextSample
from ai_room.workspace_guard import normalize_exact_paths


class CompactionAction(StrEnum):
    NONE = "none"
    REQUEST_CHECK = "request_check"
    URGENT_CHECK = "urgent_check"


@dataclass(frozen=True)
class PreviousContextCheck:
    input_tokens: int
    checkpoint_fingerprint: str
    awaiting_reset: bool = False


def evaluate_compaction(
    sample: ContextSample,
    previous: PreviousContextCheck | None,
    checkpoint_fingerprint: str,
) -> CompactionAction:
    tokens = sample.input_tokens
    if tokens is None or tokens < 150_000:
        return CompactionAction.NONE

    if (
        previous is not None
        and previous.awaiting_reset
        and tokens >= previous.input_tokens
    ):
        return CompactionAction.NONE

    if (
        previous is not None
        and tokens >= previous.input_tokens
        and tokens - previous.input_tokens < 10_000
        and checkpoint_fingerprint == previous.checkpoint_fingerprint
    ):
        return CompactionAction.NONE

    if tokens > 200_000:
        return CompactionAction.URGENT_CHECK
    return CompactionAction.REQUEST_CHECK


def checkpoint_fingerprint(root: Path, exact_paths: tuple[Path, ...]) -> str:
    normalized = normalize_exact_paths(root, exact_paths)
    digest = hashlib.sha256()
    resolved_root = Path(root).resolve()
    for relative in sorted(normalized, key=lambda path: (path.casefold(), path)):
        candidate = resolved_root.joinpath(*relative.split("/"))
        content_hash = "MISSING"
        try:
            if candidate.is_file():
                content_hash = _hash_file(candidate)
        except FileNotFoundError:
            pass
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
