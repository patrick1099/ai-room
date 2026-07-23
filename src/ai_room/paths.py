"""Room identity and runtime path resolution."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from ai_room.domain import RoomRef

GitTopLevelResolver = Callable[[Path], Path | None]


def resolve_room(
    cwd: Path,
    explicit_name: str | None = None,
    *,
    git_toplevel: GitTopLevelResolver | None = None,
) -> RoomRef:
    """Build a deterministic room reference for one Git worktree or directory."""
    resolved_cwd = cwd.resolve()
    resolver = git_toplevel or _git_toplevel
    root = resolver(resolved_cwd) or resolved_cwd
    resolved_root = root.resolve()
    normalized_root = normalize_root(resolved_root)
    name = explicit_name or ""
    room_id = hashlib.sha256(f"{normalized_root}\0{name}".encode("utf-8")).hexdigest()[:24]

    return RoomRef(room_id=room_id, root=resolved_root, explicit_name=explicit_name)


def runtime_root(environ: Mapping[str, str]) -> Path:
    """Return the directory holding local ai-room runtime databases."""
    return Path(environ["LOCALAPPDATA"]) / "ai-room"


def _git_toplevel(cwd: Path) -> Path | None:
    command = ["git", "rev-parse", "--show-toplevel"]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    return Path(output) if output else None


def normalize_root(root: Path) -> str:
    """Return the one canonical worktree-root key used by room identities."""
    normalized = os.path.normpath(str(root)).replace("\\", "/")
    if os.name == "nt":
        return normalized.casefold()
    return normalized
