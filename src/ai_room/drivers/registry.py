"""Driver registry: resolve a headless driver by agent name."""

from __future__ import annotations

from .claude import ClaudeDriver
from .codex import CodexDriver
from .opencode import OpenCodeDriver
from .protocol import Driver, DriverError, session_id_in_line

_DRIVERS: dict[str, type[Driver]] = {
    "claude": ClaudeDriver,
    "codex": CodexDriver,
    "opencode": OpenCodeDriver,
}


def driver_for(name: str) -> Driver:
    """Return a driver instance for ``name`` or raise DriverError."""
    cls = _DRIVERS.get(name)
    if cls is None:
        raise DriverError(f"no driver for agent {name!r}")
    return cls()


def list_drivers() -> tuple[str, ...]:
    """Return the available headless driver names."""
    return tuple(_DRIVERS)


def session_id_from(stdout: str) -> str | None:
    """Recover a session id from partial, possibly truncated vendor output.

    Used on the paths where the run did not finish cleanly.  The turn was still
    billed, so losing the handle means losing the ability to resume it -- and a
    killed sub-agent is exactly the case where resuming matters most.
    """
    for line in stdout.splitlines():
        session_id = session_id_in_line(line)
        if session_id:
            return session_id
    return None
