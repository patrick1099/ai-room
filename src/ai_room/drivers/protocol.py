"""Headless sub-agent CLI drivers: run one vendor CLI as a one-shot sub-agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from abc import ABC, abstractmethod


@dataclass(frozen=True)
class DriverResult:
    """Outcome of one headless sub-agent invocation."""

    agent: str
    session_id: str | None
    text: str
    exit_code: int
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and bool(self.text.strip())


class DriverError(RuntimeError):
    """Raised when a driver cannot prepare or run its vendor CLI."""


class Driver(ABC):
    """Contract for running one vendor CLI headlessly and parsing its result."""

    name: str

    @abstractmethod
    def invoke(
        self,
        question: str,
        *,
        cwd: Path,
        model: str | None = None,
        timeout: float = 300.0,
        permission_mode: str | None = None,
        sandbox: str | None = None,
        allowed_write: tuple[str, ...] = (),
    ) -> DriverResult:
        """Run the headless CLI once and return its parsed result."""