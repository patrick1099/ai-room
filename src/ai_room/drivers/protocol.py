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


@dataclass(frozen=True)
class DriverRequest:
    """Everything a driver needs to launch one headless sub-agent.

    ``related_docs`` is read-only context handed to the sub-agent.  ``writable_docs``
    is the exact-path allow-list; when it is empty the sub-agent is read-only and
    must not touch any file or run tests/builds.
    """

    question: str
    cwd: Path
    model: str | None = None
    timeout: float = 300.0
    permission_mode: str | None = None
    sandbox: str | None = None
    related_docs: tuple[str, ...] = ()
    writable_docs: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        """True when no writable paths were granted."""
        return not self.writable_docs


def compose_prompt(request: DriverRequest) -> str:
    """Build the prompt actually sent to the sub-agent.

    The related docs and the write boundary are part of the prompt, not just the
    ledger, otherwise the sub-agent has no idea they exist.
    """
    question = request.question
    if request.related_docs:
        lines = "\n".join(f"- {doc}" for doc in request.related_docs)
        question += f"\n\nRelevant documents (read-only):\n{lines}"
    if request.read_only:
        question += (
            "\n\nYou are read-only this run: do not modify any file, "
            "and do not run tests or builds."
        )
    else:
        writable = ", ".join(request.writable_docs)
        question += (
            f"\n\nYou may write only these files: {writable}. "
            "Do not modify anything else."
        )
    return question


class DriverError(RuntimeError):
    """Raised when a driver cannot prepare or run its vendor CLI."""


class Driver(ABC):
    """Contract for running one vendor CLI headlessly and parsing its result."""

    name: str

    @abstractmethod
    def invoke(self, request: DriverRequest) -> DriverResult:
        """Run the headless CLI once and return its parsed result."""
