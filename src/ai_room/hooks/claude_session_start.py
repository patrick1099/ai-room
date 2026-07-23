"""Minimal Claude SessionStart hook for environment and registry propagation."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO


def main(
    *,
    stdin: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Register one Claude session without modifying project or user settings."""
    input_stream = stdin or sys.stdin
    environment = os.environ if environ is None else environ
    payload = json.load(input_stream)
    if not isinstance(payload, dict):
        raise ValueError("SessionStart input must be one JSON object")

    session_id = _required_value(payload, "session_id")
    transcript_path = _required_value(payload, "transcript_path")
    cwd = _required_value(payload, "cwd")
    source = payload.get("source")
    if source is not None:
        if not isinstance(source, str):
            raise ValueError("source must be a string when present")
        _reject_newline(source, "source")

    if session_id in {".", ".."} or "/" in session_id or "\\" in session_id:
        raise ValueError("session_id must be safe for use as a registry filename")

    local_app_data = environment.get("LOCALAPPDATA", "")
    if not local_app_data:
        raise ValueError("LOCALAPPDATA is required to register the Claude session")
    _reject_newline(local_app_data, "LOCALAPPDATA")

    record = {
        "cwd": cwd,
        "session_id": session_id,
        "source": source,
        "transcript_path": transcript_path,
    }
    _write_registry_record(
        Path(local_app_data),
        session_id,
        record,
    )

    env_file = environment.get("CLAUDE_ENV_FILE")
    if env_file:
        _reject_newline(env_file, "CLAUDE_ENV_FILE")
        _append_exports(
            Path(env_file),
            {
                "AI_ROOM_CLAUDE_SESSION_ID": session_id,
                "AI_ROOM_CLAUDE_TRANSCRIPT_PATH": transcript_path,
            },
        )
    return 0


def _required_value(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    _reject_newline(value, key)
    return value


def _reject_newline(value: str, name: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} must not contain a newline")


def _append_exports(env_file: Path, values: Mapping[str, str]) -> None:
    with env_file.open("a", encoding="utf-8", newline="\n") as output:
        for name, value in values.items():
            output.write(f"export {name}={_shell_quote(value)}\n")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _write_registry_record(
    local_app_data: Path,
    session_id: str,
    record: Mapping[str, object],
) -> None:
    registry_dir = local_app_data / "ai-room" / "claude-sessions"
    registry_dir.mkdir(parents=True, exist_ok=True)
    destination = registry_dir / f"{session_id}.json"
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=registry_dir,
        prefix=f".{session_id}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as output:
            json.dump(
                record,
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
