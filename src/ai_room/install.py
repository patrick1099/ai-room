"""结构: App installer over a small write Port with recording/filesystem Adapters.
用途: Install the whole ai-room skill directory and one idempotent Claude hook.
用法: python -m ai_room.install --check
原始需求: Preview and apply one safe ordered install plan without replacing user configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Protocol, TextIO


_HOOK_MODULE = "ai_room.hooks.claude_session_start"
_HOOK_MATCHER = "startup|resume|clear|compact"
_REPARSE_POINT_ATTRIBUTE = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x400,
)


class InstallError(RuntimeError):
    """Base class for expected installer failures."""


class InstallConflictError(InstallError):
    """Raised when installation would overwrite or discard user configuration."""


class PathState(StrEnum):
    MISSING = "missing"
    FILE = "file"
    DIRECTORY = "directory"
    OTHER = "other"


@dataclass(frozen=True)
class InstallPlan:
    #: (file name, contents) for every file of the skill, SKILL.md first.
    skill_files: tuple[tuple[str, bytes], ...]
    codex_skill_dir: Path
    claude_skill_dir: Path
    claude_settings: Path
    settings_backup: Path
    session_start_group: dict[str, object]

    def skill_writes(self) -> tuple[tuple[Path, bytes], ...]:
        """Every destination file and its contents, both vendors expanded."""
        return tuple(
            (directory / name, data)
            for directory in (self.codex_skill_dir, self.claude_skill_dir)
            for name, data in self.skill_files
        )


@dataclass(frozen=True)
class InstallOperation:
    action: str
    path: Path
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "path": str(self.path),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class InstallReport:
    operations: tuple[InstallOperation, ...]


class InstallWriter(Protocol):
    """Port for all filesystem observation and mutation used by installation."""

    def state(self, path: Path) -> PathState:
        ...

    def read_bytes(self, path: Path) -> bytes:
        ...

    def atomic_write(self, path: Path, data: bytes) -> None:
        ...


class RecordingWriter:
    """Read the current filesystem but record intended writes without changing it."""

    def state(self, path: Path) -> PathState:
        return _path_state(path)

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def atomic_write(self, path: Path, data: bytes) -> None:
        del path, data


class FilesystemWriter:
    """Apply writes with a temporary sibling and one atomic replace."""

    def state(self, path: Path) -> PathState:
        return _path_state(path)

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def atomic_write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class _PreparedWrite:
    operation: InstallOperation
    data: bytes | None


def build_install_plan(
    home: Path,
    python_exe: Path,
    source_skill: Path | None = None,
) -> InstallPlan:
    """Build immutable destinations, source bytes, hook, and backup identity."""
    normalized_home = Path(home).expanduser()
    normalized_python = Path(python_exe)
    skill_files = _skill_sources(source_skill)

    hook_command = subprocess.list2cmdline(
        [str(normalized_python), "-m", _HOOK_MODULE]
    )
    session_start_group: dict[str, object] = {
        "matcher": _HOOK_MATCHER,
        # The shell is pinned rather than left to Claude Code's default,
        # because the two shells do not agree on this command.  It is quoted
        # with Windows rules -- a Python installed under "Program Files" needs
        # that -- and a line starting with a quoted path is a *string
        # expression* to PowerShell, so it fails to parse before python is
        # ever launched: "Unexpected token '-m'".  Observed for real when
        # claude ran as an ai-room sub-agent and Git Bash was not on the
        # sub-process's search path, so Claude Code fell back to PowerShell.
        # Pinning bash also makes the missing-Git-Bash case report itself
        # instead of turning into that parse error.
        "hooks": [
            {"type": "command", "command": hook_command, "shell": "bash"}
        ],
    }
    settings = normalized_home / ".claude" / "settings.json"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return InstallPlan(
        skill_files=skill_files,
        codex_skill_dir=normalized_home / ".codex" / "skills" / "ai-room",
        claude_skill_dir=normalized_home / ".claude" / "skills" / "ai-room",
        claude_settings=settings,
        settings_backup=settings.with_name(f"{settings.name}.{timestamp}.bak"),
        session_start_group=session_start_group,
    )


def execute_install(
    plan: InstallPlan,
    writer: InstallWriter,
) -> InstallReport:
    """Preflight the full plan, then execute its one ordered operation path."""
    prepared = _prepare_install(plan, writer)
    for item in prepared:
        if item.data is not None:
            writer.atomic_write(item.operation.path, item.data)
    return InstallReport(tuple(item.operation for item in prepared))


def _prepare_install(
    plan: InstallPlan,
    writer: InstallWriter,
) -> tuple[_PreparedWrite, ...]:
    skill_writes = plan.skill_writes()
    destinations = tuple(path for path, _ in skill_writes) + (plan.claude_settings,)
    states = _preflight_destinations(destinations, writer)

    existing_settings: bytes | None = None
    if states[plan.claude_settings] is PathState.FILE:
        existing_settings = writer.read_bytes(plan.claude_settings)
        settings_data, settings_changed = _merge_settings(
            existing_settings, plan.session_start_group
        )
    else:
        settings_data, settings_changed = _merge_settings(
            None, plan.session_start_group
        )

    if existing_settings is not None and settings_changed:
        backup_state = _preflight_destinations(
            (plan.settings_backup,), writer
        )[plan.settings_backup]
        if backup_state is not PathState.MISSING:
            raise InstallConflictError(
                f"settings backup destination already exists: "
                f"{plan.settings_backup}"
            )

    prepared: list[_PreparedWrite] = []
    for path, data in skill_writes:
        prepared.append(_prepare_file(path, data, states[path], writer))

    if existing_settings is not None and settings_changed:
        prepared.append(_prepared("backup", plan.settings_backup, existing_settings))

    if settings_changed:
        prepared.append(_prepared("write", plan.claude_settings, settings_data))
    else:
        prepared.append(_prepared("unchanged", plan.claude_settings, None))
    return tuple(prepared)


def _preflight_destinations(
    destinations: tuple[Path, ...],
    writer: InstallWriter,
) -> dict[Path, PathState]:
    states: dict[Path, PathState] = {}
    checked_ancestors: set[Path] = set()
    for path in destinations:
        state = writer.state(path)
        states[path] = state
        if state not in (PathState.MISSING, PathState.FILE):
            raise InstallConflictError(
                f"installation destination is not a regular file: {path}"
            )
        for ancestor in path.parents:
            if ancestor in checked_ancestors:
                continue
            checked_ancestors.add(ancestor)
            ancestor_state = writer.state(ancestor)
            if ancestor_state in (PathState.MISSING, PathState.DIRECTORY):
                continue
            raise InstallConflictError(
                f"installation ancestor is not a directory: {ancestor}"
            )
    return states


def _prepare_file(
    path: Path,
    desired: bytes,
    state: PathState,
    writer: InstallWriter,
) -> _PreparedWrite:
    if state is PathState.FILE:
        if writer.read_bytes(path) == desired:
            return _prepared("unchanged", path, None, desired)
        raise InstallConflictError(
            f"existing skill differs from repository source: {path}"
        )
    return _prepared("write", path, desired)


def _prepared(
    action: str,
    path: Path,
    data: bytes | None,
    hash_data: bytes | None = None,
) -> _PreparedWrite:
    digest_source = hash_data if hash_data is not None else (data or b"")
    return _PreparedWrite(
        InstallOperation(
            action=action,
            path=path,
            sha256=hashlib.sha256(digest_source).hexdigest(),
        ),
        data,
    )


def _merge_settings(
    existing: bytes | None,
    desired_group: dict[str, object],
) -> tuple[bytes, bool]:
    if existing is None:
        settings: dict[str, object] = {}
    else:
        try:
            parsed = json.loads(existing.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InstallConflictError(
                "Claude settings must contain valid JSON"
            ) from error
        if not isinstance(parsed, dict):
            raise InstallConflictError(
                "Claude settings root must be a JSON object"
            )
        settings = parsed

    if "hooks" not in settings:
        hooks: dict[str, object] = {}
        settings["hooks"] = hooks
    else:
        hooks_value = settings["hooks"]
        if not isinstance(hooks_value, dict):
            raise InstallConflictError(
                "Claude settings hooks must be a JSON object"
            )
        hooks = hooks_value

    if "SessionStart" not in hooks:
        session_groups: list[object] = []
        hooks["SessionStart"] = session_groups
    else:
        session_value = hooks["SessionStart"]
        if not isinstance(session_value, list):
            raise InstallConflictError(
                "Claude SessionStart hooks must be a JSON array"
            )
        session_groups = session_value

    exact_groups = 0
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            commands = _hook_commands(group)
            if not any(_HOOK_MODULE in command for command in commands):
                continue
            if event == "SessionStart" and group == desired_group:
                exact_groups += 1
                continue
            raise InstallConflictError(
                "conflicting ai-room hook command already exists"
            )

    if exact_groups > 1:
        raise InstallConflictError("duplicate ai-room hook commands already exist")
    if exact_groups == 1:
        if existing is None:
            raise AssertionError("new settings cannot already contain a hook")
        return existing, False

    session_groups.append(desired_group)
    encoded = json.dumps(
        settings, ensure_ascii=False, indent=2
    ).encode("utf-8") + b"\n"
    return encoded, True


def _hook_commands(group: object) -> list[str]:
    if not isinstance(group, dict):
        return []
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return []
    commands: list[str] = []
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = hook.get("command")
        if isinstance(command, str):
            commands.append(command)
    return commands


def _path_state(path: Path) -> PathState:
    if _is_reparse_point(path) or path.is_symlink():
        return PathState.OTHER
    if path.is_file():
        return PathState.FILE
    if path.is_dir():
        return PathState.DIRECTORY
    if path.exists():
        return PathState.OTHER
    return PathState.MISSING


def _is_reparse_point(path: Path) -> bool:
    return bool(_file_attributes(path) & _REPARSE_POINT_ATTRIBUTE)


def _file_attributes(path: Path) -> int:
    try:
        return int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return 0


def _skill_sources(source_skill: Path | None) -> tuple[tuple[str, bytes], ...]:
    """Load SKILL.md together with every vendor manual sitting beside it.

    SKILL.md is a router: it names one manual per vendor and each vendor reads
    only its own.  Installing the router without its manuals would leave the
    reader pointed at files that do not exist, so the whole directory travels
    as one unit and the caller still names only SKILL.md.
    """
    if source_skill is None:
        directory = files("ai_room").joinpath("resources")
        names = sorted(
            entry.name for entry in directory.iterdir() if entry.name.endswith(".md")
        )
        loader = directory.joinpath
    else:
        normalized_source = Path(source_skill)
        if _path_state(normalized_source) is not PathState.FILE:
            raise InstallConflictError(
                f"source skill is not a regular file: {normalized_source}"
            )
        parent = normalized_source.parent
        names = sorted(
            entry.name
            for entry in parent.iterdir()
            if entry.suffix == ".md" and _path_state(entry) is PathState.FILE
        )
        loader = parent.joinpath

    if "SKILL.md" not in names:
        raise InstallConflictError("skill source has no SKILL.md")
    # SKILL.md first: the ordered plan should read as router-then-manuals.
    names = ["SKILL.md"] + [name for name in names if name != "SKILL.md"]
    return tuple((name, loader(name).read_bytes()) for name in names)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_room.install",
        description="Preview or apply the ai-room user integration.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="preview without writes")
    mode.add_argument("--apply", action="store_true", help="apply atomic writes")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    home: Path | None = None,
    python_exe: Path | None = None,
    source_skill: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run isolated preview or explicitly requested apply mode."""
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    arguments = _build_parser().parse_args(argv)
    mode = "check" if arguments.check else "apply"
    try:
        plan = build_install_plan(
            home or Path.home(),
            python_exe or Path(sys.executable),
            source_skill,
        )
        writer: InstallWriter
        writer = RecordingWriter() if arguments.check else FilesystemWriter()
        report = execute_install(plan, writer)
    except InstallError as error:
        print(f"ai-room install refused: {error}", file=error_output)
        return 1

    json.dump(
        {
            "mode": mode,
            "operations": [
                operation.as_dict() for operation in report.operations
            ],
        },
        output,
        ensure_ascii=False,
        indent=2,
    )
    output.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
