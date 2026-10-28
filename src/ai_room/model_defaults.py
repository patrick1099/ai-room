"""Default model and effort per agent, read from ``resources/models.toml``.

Model names are data, not logic: they go stale with every vendor release, so
they live in a file that can be edited without touching a driver or a test.
The drivers keep only what the vendor CLI itself can do (which effort values
its flag accepts).
"""

from __future__ import annotations

import tomllib
from importlib.resources import files
from pathlib import Path

from .session_settings import SessionSettings


class ModelDefaultsError(ValueError):
    """The defaults file is missing or malformed."""


def defaults_file() -> Path:
    return Path(str(files("ai_room").joinpath("resources", "models.toml")))


def defaults_for(agent: str, path: Path | None = None) -> SessionSettings:
    """What ``agent`` runs with when the caller names nothing.

    An agent without a table means "the vendor's own default" on purpose.  A
    file that cannot be read or parsed is an error, not a fallback: quietly
    running a consultation on the vendor default is exactly the downgrade this
    file exists to prevent.
    """
    source = path or defaults_file()
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
    except OSError as error:
        raise ModelDefaultsError(f"cannot read {source}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ModelDefaultsError(f"{source} is not valid TOML: {error}") from error
    table = document.get(agent) or {}
    if not isinstance(table, dict):
        raise ModelDefaultsError(f"{source}: [{agent}] must be a table")
    values = {}
    for key in ("model", "effort"):
        value = table.get(key)
        if value is not None and not isinstance(value, str):
            raise ModelDefaultsError(f"{source}: {agent}.{key} must be a string")
        values[key] = value or None
    return SessionSettings(**values)
