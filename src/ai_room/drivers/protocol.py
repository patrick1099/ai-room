"""Headless sub-agent CLI drivers: run one vendor CLI as a one-shot sub-agent."""

from __future__ import annotations

import json
from collections.abc import Callable
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
    # Vendor-authoritative outcome fields (when the CLI exposes them).
    is_error: bool | None = None
    subtype: str | None = None
    permission_denials: tuple[str, ...] = ()
    total_cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict | None = None

    @property
    def ok(self) -> bool:
        # The vendor's own is_error is authoritative, but it never excuses a
        # nonzero exit: a run that crashed after reporting is_error=false did
        # not succeed.  Both must hold.
        if self.is_error is not None and self.is_error:
            return False
        return self.exit_code == 0 and bool(self.text.strip())


#: The three permission tiers, ordered from least to most privileged.  Every
#: driver maps these onto its vendor's native flags; the names are ai-room's,
#: the semantics must match across vendors.
PERMISSION_TIERS = ("read-only", "workspace-write", "full-access")


@dataclass(frozen=True)
class DriverRequest:
    """Everything a driver needs to launch one headless sub-agent.

    ``cwd`` is the sub-agent's working directory and every driver must pin it
    with its vendor's native flag, never with the process cwd alone: ``opencode
    run`` ignores the process cwd entirely and will work in whatever project it
    last remembered.

    ``permission`` is the tier, not a vendor spelling.  ``permission_mode`` and
    ``sandbox`` stay as raw per-vendor escape hatches and win over the tier when
    given.  ``related_docs`` is read-only context handed to the sub-agent.

    ``timeout`` is a silence budget and ``max_runtime`` a hard cap; see
    :func:`ai_room.drivers.process.run_cli`.

    ``session_id`` is a handle chosen *before* the run (claude only).
    ``resume_session`` is a handle from a run that already happened: setting it
    continues that conversation instead of starting a new one, which is the
    only way a killed dispatch does not have to be paid for twice.
    """

    question: str
    cwd: Path
    permission: str = "read-only"
    model: str | None = None
    timeout: float = 300.0
    permission_mode: str | None = None
    sandbox: str | None = None
    related_docs: tuple[str, ...] = ()
    extra_dirs: tuple[str, ...] = ()
    session_id: str | None = None
    agent_name: str | None = None
    max_runtime: float | None = None
    resume_session: str | None = None
    #: Called with the sub-agent's session id the moment it announces one,
    #: while the run is still going.  The handle must reach durable storage
    #: before the run ends, because the run may not end on our terms.
    on_session_id: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        if self.permission not in PERMISSION_TIERS:
            tiers = ", ".join(PERMISSION_TIERS)
            raise ValueError(
                f"unknown permission tier {self.permission!r} (expected one of {tiers})"
            )

    @property
    def read_only(self) -> bool:
        """True when the sub-agent may not write anything."""
        return self.permission == "read-only"

    @property
    def full_access(self) -> bool:
        """True when every sandbox restriction is lifted."""
        return self.permission == "full-access"


def compose_prompt(request: DriverRequest) -> str:
    """Build the prompt actually sent to the sub-agent.

    The prompt carries context, not policy.  Permission is enforced by the
    vendor's own flags, so the only thing worth saying is that a read-only run
    cannot write -- and that is stated as a fact so the sub-agent reports back
    instead of failing silently, not as a rule it is asked to obey.  A writable
    run is told nothing extra: it is a worker with hands, and the old advisor
    wording ("do not run tests or builds") does not apply to it.
    """
    question = request.question
    if request.related_docs:
        lines = "\n".join(f"- {doc}" for doc in request.related_docs)
        question += f"\n\nRelevant documents:\n{lines}"
    if request.read_only:
        question += (
            "\n\nThis run is read-only: file writes are blocked. "
            "If the task needs one, say so in your answer instead of attempting it."
        )
    return question


#: Every vendor announces its handle in the first event it emits, just under a
#: different key: claude ``session_id``, codex ``thread_id``, opencode
#: ``sessionID``.  All three are scanned regardless of which driver is running,
#: because a stream cut mid-line may be all there is to go on.
_SESSION_KEYS = ("session_id", "thread_id", "sessionID")


def session_id_in_line(line: str) -> str | None:
    """Return the session handle announced by one JSON event line, if any."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    for key in _SESSION_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def session_id_watcher(request: DriverRequest) -> Callable[[str], None] | None:
    """Build the ``on_line`` callback that reports the handle as it appears.

    Reporting it once is enough and reporting it twice is noise, so the watcher
    latches.  Returns None when the caller did not ask to be told, so the
    driver passes no callback at all rather than an empty one.
    """
    notify = request.on_session_id
    if notify is None:
        return None
    seen: list[str] = []

    def watch(line: str) -> None:
        if seen:
            return
        session_id = session_id_in_line(line)
        if session_id:
            seen.append(session_id)
            notify(session_id)

    return watch


class DriverError(RuntimeError):
    """Raised when a driver cannot prepare or run its vendor CLI."""


class Driver(ABC):
    """Contract for running one vendor CLI headlessly and parsing its result."""

    name: str

    @abstractmethod
    def invoke(self, request: DriverRequest) -> DriverResult:
        """Run the headless CLI once and return its parsed result."""

    def parse_partial(self, stdout: str) -> DriverResult:
        """Best-effort result from the output of a run that was cut short.

        A killed run was still billed, so whatever the sub-agent had already
        said belongs to the caller: it is often a usable answer, and it always
        carries the handle needed to resume.  Overrides reuse the driver's own
        parser, which must therefore tolerate a stream that ends mid-line.
        """
        return DriverResult(
            agent=self.name, session_id=None, text="", exit_code=-1, stderr=""
        )
