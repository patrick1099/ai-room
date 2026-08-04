"""Driver registry: resolve a headless driver by agent name."""

from __future__ import annotations

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