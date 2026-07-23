"""Pure safe-compaction policy and exact checkpoint fingerprint tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_room.context.policy import (
    CompactionAction,
    PreviousContextCheck,
    checkpoint_fingerprint,
    evaluate_compaction,
)
from ai_room.domain import ContextSample, ContextSource


def sample(tokens: int | None) -> ContextSample:
    return ContextSample(
        input_tokens=tokens,
        context_window=258_000 if tokens is not None else None,
        source=(
            ContextSource.CODEX_TOKEN_COUNT
            if tokens is not None
            else ContextSource.UNKNOWN
        ),
        session_id="session-a",
        unknown_reason=None if tokens is not None else "unavailable",
    )


def previous_check(tokens: int, fingerprint: str) -> PreviousContextCheck:
    return PreviousContextCheck(
        input_tokens=tokens,
        checkpoint_fingerprint=fingerprint,
    )


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (None, CompactionAction.NONE),
        (149_999, CompactionAction.NONE),
        (150_000, CompactionAction.REQUEST_CHECK),
        (200_000, CompactionAction.REQUEST_CHECK),
        (200_001, CompactionAction.URGENT_CHECK),
    ],
)
def test_thresholds(tokens: int | None, expected: CompactionAction) -> None:
    assert evaluate_compaction(sample(tokens), None, "checkpoint-a") is expected


def test_same_checkpoint_requires_ten_thousand_more_tokens() -> None:
    previous = previous_check(tokens=155_000, fingerprint="same")

    assert (
        evaluate_compaction(sample(164_999), previous, "same")
        is CompactionAction.NONE
    )
    assert (
        evaluate_compaction(sample(165_000), previous, "same")
        is CompactionAction.REQUEST_CHECK
    )


def test_changed_checkpoint_content_allows_a_repeat_before_ten_thousand() -> None:
    previous = previous_check(tokens=155_000, fingerprint="before")

    assert (
        evaluate_compaction(sample(155_001), previous, "after")
        is CompactionAction.REQUEST_CHECK
    )


def test_context_drop_is_a_reset_not_a_suppressed_repeat() -> None:
    previous = previous_check(tokens=205_000, fingerprint="same")

    assert (
        evaluate_compaction(sample(100_000), previous, "same")
        is CompactionAction.NONE
    )
    assert (
        evaluate_compaction(sample(150_000), previous, "same")
        is CompactionAction.REQUEST_CHECK
    )


def test_compact_ready_waits_for_a_lower_sample_before_new_checks() -> None:
    previous = PreviousContextCheck(
        input_tokens=205_000,
        checkpoint_fingerprint="before",
        awaiting_reset=True,
    )

    assert (
        evaluate_compaction(sample(215_000), previous, "changed")
        is CompactionAction.NONE
    )
    assert (
        evaluate_compaction(sample(150_000), previous, "changed")
        is CompactionAction.REQUEST_CHECK
    )


def test_checkpoint_fingerprint_uses_only_sorted_exact_paths_and_content(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    first = docs / "first.md"
    second = docs / "second.md"
    unrelated = docs / "unrelated.md"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    unrelated.write_text("unrelated", encoding="utf-8")

    before = checkpoint_fingerprint(
        tmp_path,
        (Path("docs/second.md"), Path("docs/first.md")),
    )
    unrelated.write_text("changed but out of scope", encoding="utf-8")
    reordered = checkpoint_fingerprint(
        tmp_path,
        (Path("docs/first.md"), Path("docs/second.md")),
    )
    second.write_text("changed checkpoint", encoding="utf-8")
    changed = checkpoint_fingerprint(
        tmp_path,
        (Path("docs/first.md"), Path("docs/second.md")),
    )

    assert reordered == before
    assert changed != before


def test_checkpoint_fingerprint_distinguishes_missing_from_empty(
    tmp_path: Path,
) -> None:
    path = tmp_path / "checkpoint.md"

    missing = checkpoint_fingerprint(tmp_path, (Path("checkpoint.md"),))
    path.write_bytes(b"")
    empty = checkpoint_fingerprint(tmp_path, (Path("checkpoint.md"),))

    assert missing != empty
