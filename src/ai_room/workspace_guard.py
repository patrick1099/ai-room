"""Content-hash guard for exact writable paths within one room root."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .domain import GuardResult


_GLOB_CHARACTERS = frozenset("*?[]")


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Stable relative paths and SHA-256 hashes captured for one task round."""

    files: tuple[tuple[str, str], ...]


class WorkspaceGuardError(RuntimeError):
    """Raised when a reply would cross the task's exact write boundary."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(sorted(violations, key=_path_key))
        changed = ", ".join(self.violations)
        super().__init__(f"workspace changes outside writable_docs: {changed}")


def normalize_exact_paths(
    root: Path,
    paths: Iterable[Path],
) -> tuple[str, ...]:
    """Resolve exact file paths beneath root and reject broad or unsafe input."""
    resolved_root = Path(root).resolve()
    normalized: list[str] = []
    seen: set[str] = set()

    for path in paths:
        candidate_path = Path(path)
        raw = str(candidate_path)
        if not raw or any(character in raw for character in _GLOB_CHARACTERS):
            raise ValueError(f"writable path must be exact: {raw!r}")

        candidate = (
            candidate_path
            if candidate_path.is_absolute()
            else resolved_root / candidate_path
        ).resolve()
        relative = _relative_to_root(resolved_root, candidate)
        if relative is None or relative == ".":
            raise ValueError(f"writable path escapes room root: {raw!r}")
        if candidate.is_dir():
            raise ValueError(f"writable path must be a file path: {raw!r}")

        portable = relative.replace("\\", "/")
        key = _path_key(portable)
        if key in seen:
            raise ValueError(f"duplicate writable path: {portable}")
        seen.add(key)
        normalized.append(portable)

    return tuple(normalized)


def capture_workspace(root: Path) -> WorkspaceSnapshot:
    """Hash tracked and visible untracked files, or all non-Git regular files."""
    resolved_root = Path(root).resolve()
    if not resolved_root.exists():
        return WorkspaceSnapshot(())
    if not resolved_root.is_dir():
        raise ValueError("room root must be a directory")

    git_paths = _git_workspace_paths(resolved_root)
    relative_paths = (
        git_paths if git_paths is not None else _non_git_paths(resolved_root)
    )
    files: list[tuple[str, str]] = []
    for relative in sorted(set(relative_paths), key=_path_key):
        candidate = resolved_root.joinpath(*relative.split("/"))
        try:
            if not candidate.is_file():
                continue
            digest = _hash_file(candidate)
        except FileNotFoundError:
            continue
        files.append((relative, digest))
    return WorkspaceSnapshot(tuple(files))


def compare_workspace(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
    writable_docs: Iterable[str],
) -> GuardResult:
    """Classify content changes using exact, case-aware platform path keys."""
    before_by_key = {_path_key(path): (path, digest) for path, digest in before.files}
    after_by_key = {_path_key(path): (path, digest) for path, digest in after.files}
    writable_keys = {_path_key(path.replace("\\", "/")) for path in writable_docs}
    allowed: list[str] = []
    violations: list[str] = []

    for key in set(before_by_key) | set(after_by_key):
        before_entry = before_by_key.get(key)
        after_entry = after_by_key.get(key)
        before_hash = None if before_entry is None else before_entry[1]
        after_hash = None if after_entry is None else after_entry[1]
        if before_hash == after_hash:
            continue

        display_path = (
            after_entry[0] if after_entry is not None else before_entry[0]
        )
        if key in writable_keys:
            allowed.append(display_path)
        else:
            violations.append(display_path)

    return GuardResult(
        allowed_changes=tuple(sorted(allowed, key=_path_key)),
        violations=tuple(sorted(violations, key=_path_key)),
    )


def _git_workspace_paths(root: Path) -> tuple[str, ...] | None:
    try:
        top_level = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            shell=False,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if top_level.returncode != 0:
        return None

    output = os.fsdecode(top_level.stdout).strip()
    if not output or _path_key(str(Path(output).resolve())) != _path_key(str(root)):
        return None

    tracked = _run_git_file_list(root, ["ls-files", "-z"])
    untracked = _run_git_file_list(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    return tracked + untracked


def _run_git_file_list(root: Path, arguments: list[str]) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        shell=False,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        reason = os.fsdecode(result.stderr).strip() or "unknown Git error"
        raise RuntimeError(f"cannot capture Git workspace: {reason}")
    return tuple(
        os.fsdecode(item).replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    )


def _non_git_paths(root: Path) -> tuple[str, ...]:
    runtime = _runtime_below(root)
    paths: list[str] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            directory
            for directory in directories
            if runtime is None
            or not _is_same_or_below(
                (current_path / directory).resolve(),
                runtime,
            )
        ]
        for filename in filenames:
            candidate = current_path / filename
            if candidate.is_file():
                relative = candidate.relative_to(root).as_posix()
                paths.append(relative)
    return tuple(paths)


def _runtime_below(root: Path) -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    runtime = (Path(local_app_data) / "ai-room").resolve()
    return runtime if _relative_to_root(root, runtime) is not None else None


def _relative_to_root(root: Path, candidate: Path) -> str | None:
    try:
        common = os.path.commonpath(
            (os.path.normcase(str(root)), os.path.normcase(str(candidate)))
        )
    except ValueError:
        return None
    if common != os.path.normcase(str(root)):
        return None
    return os.path.relpath(candidate, root)


def _is_same_or_below(candidate: Path, parent: Path) -> bool:
    return _relative_to_root(parent, candidate) is not None


def _path_key(path: str) -> str:
    normalized = os.path.normpath(path).replace("\\", "/")
    return normalized.casefold() if os.name == "nt" else normalized


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
