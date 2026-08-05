"""Driver registry: resolve a headless driver by agent name."""

from __future__ import annotations

import json

from .claude import ClaudeDriver
from .codex import CodexDriver
from .opencode import OpenCodeDriver
from .protocol import Driver, DriverError

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


#: Every vendor announces its handle in the first event it emits, just under a
#: different key: claude ``session_id``, codex ``thread_id``, opencode
#: ``sessionID``.
_SESSION_KEYS = ("session_id", "thread_id", "sessionID")


def session_id_from(stdout: str) -> str | None:
    """Recover a session id from partial, possibly truncated vendor output.

    Used on the paths where the run did not finish cleanly.  The turn was still
    billed, so losing the handle means losing the ability to resume it -- and a
    killed sub-agent is exactly the case where resuming matters most.  Scans all
    three vendors' key names rather than dispatching on the agent, because a
    stream cut mid-line may be all we have.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        for key in _SESSION_KEYS:
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    return None
