"""Exact-path workspace guard tests."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import ai_room.workspace_guard as workspace_guard
from ai_room.domain import (
    AgentName,
    ContextSample,
    ContextSource,
    RoomRef,
    TaskKind,
    TaskOutcome,
    TaskRequest,
    TaskState,
)
from ai_room.service import AiRoomService
from ai_room.storage import SQLiteStore
from ai_room.workspace_guard import (
    WorkspaceGuardError,
    capture_workspace,
    compare_workspace,
    normalize_exact_paths,
)


class FakeClock:
    def __init__(self, current: float = 1_000.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "中文仓库"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "已有修改.c").write_text("committed", encoding="utf-8")
    (root / "docs" / "plan.md").write_text("original", encoding="utf-8")
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    return root


def test_capture_hashes_tracked_and_visible_untracked_but_not_ignored(
    repo: Path,
) -> None:
    (repo / "可见.txt").write_text("visible", encoding="utf-8")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "secret.txt").write_text("ignored", encoding="utf-8")

    paths = {path for path, _ in capture_workspace(repo).files}

    assert "src/已有修改.c" in paths
    assert "可见.txt" in paths
    assert "ignored/secret.txt" not in paths


def test_preexisting_dirty_file_is_not_a_violation_until_changed(repo: Path) -> None:
    source = repo / "src" / "已有修改.c"
    source.write_text("before task", encoding="utf-8")
    baseline = capture_workspace(repo)

    assert compare_workspace(baseline, capture_workspace(repo), ()).violations == ()

    source.write_text("advisor changed it", encoding="utf-8")

    assert compare_workspace(
        baseline, capture_workspace(repo), ()
    ).violations == ("src/已有修改.c",)


def test_exact_allowlist_does_not_grant_sibling_files(repo: Path) -> None:
    baseline = capture_workspace(repo)
    (repo / "docs" / "plan.md").write_text("ok", encoding="utf-8")
    (repo / "docs" / "other.md").write_text("not ok", encoding="utf-8")

    result = compare_workspace(
        baseline,
        capture_workspace(repo),
        ("docs/plan.md",),
    )

    assert result.allowed_changes == ("docs/plan.md",)
    assert result.violations == ("docs/other.md",)


def test_allowlist_handles_document_edit_creation_and_deletion(repo: Path) -> None:
    baseline = capture_workspace(repo)
    (repo / "docs" / "plan.md").unlink()
    (repo / "docs" / "评审.md").write_text("new", encoding="utf-8")

    result = compare_workspace(
        baseline,
        capture_workspace(repo),
        ("docs/plan.md", "docs/评审.md"),
    )

    assert result.allowed_changes == ("docs/plan.md", "docs/评审.md")
    assert result.violations == ()


@pytest.mark.parametrize(
    "candidate",
    [
        Path("..") / "outside.md",
        Path("docs") / "*.md",
        Path("docs") / "[ab].md",
    ],
)
def test_normalize_rejects_traversal_and_globs(
    repo: Path,
    candidate: Path,
) -> None:
    with pytest.raises(ValueError):
        normalize_exact_paths(repo, (candidate,))


def test_normalize_rejects_directories_and_duplicate_keys(repo: Path) -> None:
    with pytest.raises(ValueError, match="file path"):
        normalize_exact_paths(repo, (Path("docs"),))
    with pytest.raises(ValueError, match="duplicate"):
        normalize_exact_paths(
            repo,
            (Path("docs/plan.md"), Path("docs") / "." / "plan.md"),
        )


def test_normalize_accepts_exact_future_chinese_file(repo: Path) -> None:
    assert normalize_exact_paths(
        repo,
        (repo / "docs" / "未来评审.md",),
    ) == ("docs/未来评审.md",)


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_windows_case_variants_match_the_same_exact_file(repo: Path) -> None:
    normalized = normalize_exact_paths(
        repo,
        (Path("DOCS") / "PLAN.MD",),
    )
    baseline = capture_workspace(repo)
    (repo / "docs" / "plan.md").write_text("changed", encoding="utf-8")

    result = compare_workspace(baseline, capture_workspace(repo), normalized)

    assert result.allowed_changes == ("docs/plan.md",)
    assert result.violations == ()


def test_non_git_directory_hashes_regular_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "普通目录"
    root.mkdir()
    (root / "文档.txt").write_text("before", encoding="utf-8")
    runtime = root / "runtime" / "ai-room"
    runtime.mkdir(parents=True)
    (runtime / "room.sqlite3").write_text("runtime", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(root / "runtime"))

    baseline = capture_workspace(root)
    (root / "文档.txt").write_text("after", encoding="utf-8")
    paths = {path for path, _ in baseline.files}
    result = compare_workspace(baseline, capture_workspace(root), ())

    assert "runtime/ai-room/room.sqlite3" not in paths
    assert result.violations == ("文档.txt",)


@pytest.mark.parametrize(
    ("marker_kind", "git_failure", "expected_reason"),
    [
        ("directory", "missing", "git_unavailable"),
        ("file", "failed", "git_inspection_failed"),
    ],
)
def test_git_marker_fails_closed_when_git_inspection_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_kind: str,
    git_failure: str,
    expected_reason: str,
) -> None:
    root = tmp_path / f"git-{marker_kind}-{git_failure}"
    root.mkdir()
    marker = root / ".git"
    if marker_kind == "directory":
        marker.mkdir()
        (marker / "config").write_text("internal", encoding="utf-8")
    else:
        marker.write_text("gitdir: ../worktrees/example\n", encoding="utf-8")
    (root / "ignored.log").write_text("must not be walked", encoding="utf-8")

    if git_failure == "missing":

        def fail_git(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(workspace_guard.subprocess, "run", fail_git)
    else:
        monkeypatch.setattr(
            workspace_guard.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0],
                128,
                stdout=b"",
                stderr=b"fatal: inspection failed",
            ),
        )

    with pytest.raises(RuntimeError) as caught:
        capture_workspace(root)

    assert type(caught.value).__name__ == "WorkspaceCaptureError"
    assert caught.value.reason == expected_reason


def test_true_non_git_directory_still_uses_recursive_capture_when_git_rejects_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "not-a-repository"
    root.mkdir()
    (root / "文档.txt").write_text("content", encoding="utf-8")
    monkeypatch.setattr(
        workspace_guard.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            128,
            stdout=b"",
            stderr=b"fatal: not a git repository",
        ),
    )

    snapshot = capture_workspace(root)

    assert {path for path, _ in snapshot.files} == {"文档.txt"}


@pytest.fixture
def guarded_services(tmp_path: Path):
    room_root = tmp_path / "guarded-room"
    (room_root / "src").mkdir(parents=True)
    (room_root / "docs").mkdir()
    (room_root / "src" / "app.py").write_text("original", encoding="utf-8")
    clock = FakeClock()
    database = tmp_path / "guard.sqlite3"
    primary_store = SQLiteStore.open(database, clock)
    advisor_store = SQLiteStore.open(database, clock)
    room = RoomRef("guard-room", room_root)
    primary = AiRoomService(
        primary_store,
        room,
        AgentName.CODEX,
        session_id="codex-session",
        clock=clock,
        poll_seconds=0.01,
        stale_seconds=15.0,
    )
    advisor = AiRoomService(
        advisor_store,
        room,
        AgentName.CLAUDE,
        session_id="claude-session",
        clock=clock,
        poll_seconds=0.01,
        stale_seconds=15.0,
    )
    primary.join()
    advisor.join()
    yield primary, advisor, room_root, clock
    primary_store.close()
    advisor_store.close()


def _request(
    *,
    kind: TaskKind = TaskKind.DESIGN_REVIEW,
    writable_docs: tuple[str, ...] = ("docs/review.md",),
) -> TaskRequest:
    return TaskRequest(
        room_id="guard-room",
        sender=AgentName.CODEX,
        recipient=AgentName.CLAUDE,
        kind=kind,
        question="请评审",
        related_docs=("docs/plan.md",),
        writable_docs=writable_docs,
        context=ContextSample(
            input_tokens=None,
            context_window=None,
            source=ContextSource.UNKNOWN,
            session_id=None,
            unknown_reason="fixture",
        ),
        checkpoint_docs=(),
        next_entry=None,
        idempotency_key=f"guard-{kind.value}",
    )


def test_service_blocks_reply_after_out_of_scope_change_and_preserves_file(
    guarded_services,
) -> None:
    primary, advisor, room_root, _ = guarded_services
    sent = primary.send(_request())
    assert advisor.wait(threading.Event()) is not None
    source = room_root / "src" / "app.py"
    source.write_text("advisor edit", encoding="utf-8")

    with pytest.raises(WorkspaceGuardError) as raised:
        advisor.reply(sent.task_id, TaskOutcome.DONE, "完成")

    assert raised.value.violations == ("src/app.py",)
    assert source.read_text(encoding="utf-8") == "advisor edit"
    assert advisor.status().active_task.state is TaskState.WORKING


def test_service_allows_only_the_exact_document_from_request(
    guarded_services,
) -> None:
    primary, advisor, room_root, _ = guarded_services
    sent = primary.send(_request())
    assert advisor.wait(threading.Event()) is not None
    (room_root / "docs" / "review.md").write_text("approved", encoding="utf-8")

    result = advisor.reply(sent.task_id, TaskOutcome.DONE, "完成")

    assert result.state is TaskState.DONE


def test_redelivery_reuses_original_round_baseline(guarded_services) -> None:
    primary, advisor, room_root, clock = guarded_services
    sent = primary.send(_request())
    first = advisor.wait(threading.Event())
    assert first is not None
    (room_root / "src" / "app.py").write_text("changed after claim", encoding="utf-8")
    clock.advance(16.0)

    redelivered = advisor.wait(threading.Event())

    assert redelivered is not None
    assert redelivered.message_id == first.message_id
    with pytest.raises(WorkspaceGuardError, match="src/app.py"):
        advisor.reply(sent.task_id, TaskOutcome.DONE, "完成")


@pytest.mark.parametrize("kind", [TaskKind.DECISION, TaskKind.CONTEXT_CHECK])
def test_decision_and_context_tasks_never_allow_workspace_changes(
    guarded_services,
    kind: TaskKind,
) -> None:
    primary, advisor, room_root, _ = guarded_services
    sent = primary.send(
        replace(
            _request(kind=kind, writable_docs=()),
            idempotency_key=f"no-writes-{kind.value}",
        )
    )
    assert advisor.wait(threading.Event()) is not None
    (room_root / "docs" / "decision.md").write_text("changed", encoding="utf-8")

    with pytest.raises(WorkspaceGuardError, match="docs/decision.md"):
        advisor.reply(sent.task_id, TaskOutcome.DONE, "完成")
