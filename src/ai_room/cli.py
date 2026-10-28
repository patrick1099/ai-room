"""结构: App CLI over the existing service, storage, context, and path ports.
用途: Expose the six stable ai-room commands with one-object JSON output.
用法: python -m ai_room join codex
原始需求: Six commands, exact document guards, UTF-8 JSON, stable exit codes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Event
from typing import TextIO

from .bindings import (
    BindingDatabaseBusyError,
    BindingDatabaseOpenError,
    BindingRegistry,
    BindingSchemaVersionError,
    RoomBindingConflictError,
    RoomBindingMissingError,
    RoomDatabaseMissingError,
    RoomJoinIncompleteError,
    binding_registry_path,
)
from .context import SessionDetectionError, adapter_for, detect_current_session
from .domain import (
    AgentName,
    ContextSample,
    Delivery,
    MemberStatus,
    TaskKind,
    TaskOutcome,
    TaskRequest,
    TaskView,
)
from .drivers import (
    PERMISSION_TIERS,
    DriverError,
    DriverRequest,
    DriverTimeout,
    driver_for,
    session_id_from,
)
from .inflight import clear_inflight, list_inflight, record_inflight
from .ledger import LedgerEntry, append_ledger, resume_hint
from .paths import normalize_root, resolve_room, runtime_root
from .receipt import changed_since, status_lines
from .service import AiRoomService
from .model_defaults import ModelDefaultsError, defaults_for
from .session_settings import SessionSettings, lookup_settings, record_settings
from .storage import (
    DatabaseBusyError,
    DatabaseOpenError,
    MalformedMessageError,
    PeerNotJoinedError,
    SQLiteStore,
    SchemaVersionError,
    StorageError,
    TaskConflictError,
    TaskNotDeliveredError,
)
from .workspace_guard import (
    WorkspaceCaptureError,
    capture_workspace,
    compare_workspace,
    normalize_exact_paths,
)


EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ARGUMENT = 2

_AGENT_LABELS = {
    AgentName.CODEX: "Codex",
    AgentName.CLAUDE: "Claude",
}
_KIND_ARGUMENTS = {
    "decision": TaskKind.DECISION,
    "requirements-review": TaskKind.REQUIREMENTS_REVIEW,
    "design-review": TaskKind.DESIGN_REVIEW,
    "plan-review": TaskKind.PLAN_REVIEW,
    "context-check": TaskKind.CONTEXT_CHECK,
}
_OUTCOME_ARGUMENTS = {
    "done": TaskOutcome.DONE,
    "blocked": TaskOutcome.BLOCKED,
    "compact-ready": TaskOutcome.COMPACT_READY,
    "checkpoint-needed": TaskOutcome.CHECKPOINT_NEEDED,
}
_DOCUMENT_REVIEW_KINDS = frozenset(
    (
        TaskKind.REQUIREMENTS_REVIEW,
        TaskKind.DESIGN_REVIEW,
        TaskKind.PLAN_REVIEW,
    )
)


class CliArgumentError(ValueError):
    """Raised for a public command-line contract violation."""


class CliOperationalError(RuntimeError):
    """Raised for an expected command state that cannot complete."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class JsonArgumentParser(argparse.ArgumentParser):
    """Route argparse failures through the CLI's stable JSON error boundary."""

    def error(self, message: str) -> None:
        raise CliArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    """Create the exact six-command parser."""
    parser = JsonArgumentParser(
        prog="ai-room",
        description=(
            "LLMs/agents: run 'ai-room --ai-help' for usage guidance."
        ),
    )
    _add_output_flags(parser)
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
    )

    join = commands.add_parser("join", help="join the current room")
    _add_output_flags(join)
    join.add_argument("agent", choices=tuple(agent.value for agent in AgentName))
    join.add_argument("--room", metavar="NAME")

    wait = commands.add_parser("wait", help="wait silently for one delivery")
    _add_output_flags(wait)
    wait.add_argument(
        "--checkpoint",
        action="append",
        default=[],
        metavar="EXACT_PATH",
    )
    wait.add_argument("--next-entry", metavar="TEXT")

    send = commands.add_parser("send", help="send one advisor task")
    _add_output_flags(send)
    send.add_argument(
        "--to",
        required=True,
        choices=tuple(agent.value for agent in AgentName),
    )
    send.add_argument(
        "--type",
        required=True,
        choices=tuple(_KIND_ARGUMENTS),
        dest="kind",
    )
    send.add_argument("--question", required=True)
    send.add_argument(
        "--related-doc",
        action="append",
        default=[],
        metavar="EXACT_PATH",
    )
    send.add_argument(
        "--writable-doc",
        action="append",
        default=[],
        metavar="EXACT_PATH",
    )
    send.add_argument(
        "--checkpoint-doc",
        action="append",
        default=[],
        metavar="EXACT_PATH",
    )
    send.add_argument("--next-entry", metavar="TEXT")
    send.add_argument("--idempotency-key", metavar="KEY")

    reply = commands.add_parser("reply", help="reply to one delivered task")
    _add_output_flags(reply)
    reply.add_argument("task_id")
    reply.add_argument(
        "--outcome",
        required=True,
        choices=tuple(_OUTCOME_ARGUMENTS),
    )
    reply.add_argument("--message", required=True)

    status = commands.add_parser("status", help="show room membership and active task")
    _add_output_flags(status)
    leave = commands.add_parser("leave", help="leave without deleting messages")
    _add_output_flags(leave)

    ask = commands.add_parser(
        "ask",
        help="dispatch one headless sub-agent task and record it in the ledger",
    )
    _add_output_flags(ask)
    ask.add_argument(
        "--to",
        required=True,
        choices=("claude", "codex", "opencode", "dsh"),
    )
    ask.add_argument("--question", required=True)
    _add_dispatch_arguments(ask)

    resume = commands.add_parser(
        "resume",
        help="continue a dispatch that was cut short, instead of re-running it",
    )
    _add_output_flags(resume)
    resume.add_argument(
        "--to",
        choices=("claude", "codex", "opencode", "dsh"),
        help="which vendor holds the session; required with --session",
    )
    resume.add_argument(
        "--session",
        metavar="SESSION_ID",
        help="handle to continue; omit to take the newest killed dispatch",
    )
    resume.add_argument(
        "--question",
        help="what to say on resuming; defaults to 'carry on where you stopped'",
    )
    _add_dispatch_arguments(resume)
    return parser


def _add_dispatch_arguments(command: argparse.ArgumentParser) -> None:
    """Add the options shared by ``ask`` and ``resume``.

    Both drive the same dispatch, so an option that exists on one and not the
    other would mean a resumed run silently ran under different rules than the
    run it continues.
    """
    command.add_argument(
        "--related-doc",
        action="append",
        default=[],
        metavar="EXACT_PATH",
    )
    command.add_argument(
        "--permission",
        choices=PERMISSION_TIERS,
        default="read-only",
        help=(
            "how much the sub-agent may do: read-only (default), "
            "workspace-write (edit files and run commands in the working "
            "directory), or full-access (no sandbox)"
        ),
    )
    command.add_argument(
        "--model",
        metavar="MODEL",
        help=(
            "model for the sub-agent; defaults to the strongest tier for "
            "claude/codex, and on resume to what the session last ran with"
        ),
    )
    command.add_argument(
        "--effort",
        metavar="LEVEL",
        help=(
            "reasoning effort: low|medium|high|xhigh|max (codex also ultra; "
            "opencode takes its provider's --variant names). Defaults to max "
            "for claude/codex, and on resume to what the session last ran with"
        ),
    )
    command.add_argument("--cwd", metavar="DIR", help="which project to dispatch against; the sub-agent runs in the room root selected by this path")
    command.add_argument(
        "--timeout",
        type=float,
        default=_env_seconds("AI_ROOM_TIMEOUT", 300.0),
        metavar="SECONDS",
        help=(
            "how long the sub-agent may stay SILENT (default 300, or "
            "$AI_ROOM_TIMEOUT); any output resets it, so a sub-agent that is "
            "still working is never killed"
        ),
    )
    command.add_argument(
        "--max-runtime",
        dest="max_runtime",
        type=float,
        default=_env_seconds("AI_ROOM_MAX_RUNTIME", 3600.0),
        metavar="SECONDS",
        help=(
            "hard cap on total run time regardless of output "
            "(default 3600, or $AI_ROOM_MAX_RUNTIME)"
        ),
    )
    command.add_argument("--permission-mode", dest="permission_mode", metavar="MODE")
    command.add_argument("--sandbox", metavar="MODE")
    command.add_argument("--no-ledger", action="store_true")


def _add_output_flags(parser: argparse.ArgumentParser) -> None:
    """Add the machine_only output flags accepted by every command.

    ai-room is a machine-only CLI: it always prints one JSON envelope, so
    ``--json`` and ``--format json`` are accepted for contract compatibility
    and change nothing.  The flags are accepted at the top level and on every
    subcommand, because an AI calling ``ai-room status --json`` must not be
    rejected for a flag the spec says every command supports.
    """
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine_only: output is always JSON (accepted for CLI-AI spec compatibility)",
    )
    parser.add_argument(
        "--format",
        choices=("json",),
        default="json",
        help="output format (only json is supported; equivalent to --json)",
    )


def _env_seconds(name: str, fallback: float) -> float:
    """Read a seconds budget from the environment, ignoring nonsense.

    These two budgets have to be settable per machine, because the useful
    value depends on the caller's own shell timeout -- which is a property of
    which CLI is driving ai-room, not of ai-room.  A malformed or non-positive
    value falls back rather than raising: a typo in a machine-wide environment
    variable must not make every dispatch fail.
    """
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one command and return its stable process exit code."""
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    _configure_utf8(output)
    _configure_utf8(errors)
    if _handle_ai_help(argv, output):
        return EXIT_SUCCESS
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
        _validate_arguments(arguments)
    except CliArgumentError as error:
        _write_error(errors, "E_VALIDATION", str(error))
        return EXIT_ARGUMENT
    except argparse.ArgumentError as error:
        _write_error(errors, "E_VALIDATION", str(error))
        return EXIT_ARGUMENT
    except SystemExit as exit_request:
        return int(exit_request.code)

    active_environ = os.environ if environ is None else environ
    active_cwd = Path.cwd() if cwd is None else Path(cwd)
    store: SQLiteStore | None = None
    registry: BindingRegistry | None = None
    service: AiRoomService | None = None
    try:
        if arguments.command in _DISPATCH_COMMANDS:
            ask_cwd = Path(arguments.cwd) if arguments.cwd else active_cwd
            room = resolve_room(ask_cwd)
            try:
                sender = detect_current_session(active_environ).agent
            except SessionDetectionError:
                sender = None
            result = _DISPATCH_COMMANDS[arguments.command](
                arguments, room.root, sender
            )
            if result.get("ok"):
                _write_success(
                    output,
                    {
                        "command": arguments.command,
                        "room": room.room_id,
                        "result": result,
                    },
                )
                return EXIT_SUCCESS
            _write_dispatch_error(
                errors,
                arguments.command,
                room.room_id,
                result,
            )
            return EXIT_FAILURE
        identity = detect_current_session(active_environ)
        requested_agent = (
            AgentName(arguments.agent)
            if arguments.command == "join"
            else identity.agent
        )
        adapter = adapter_for(requested_agent, active_environ)
        runtime_directory = runtime_root(active_environ)
        registry = BindingRegistry.open(
            binding_registry_path(runtime_directory),
            time.time,
        )
        root = resolve_room(active_cwd).root
        root_key = normalize_root(root)

        if arguments.command == "join":
            explicit_name = arguments.room
            registry.reserve_binding(
                root_key,
                requested_agent,
                identity.session_id,
                explicit_name,
            )
            room = resolve_room(active_cwd, explicit_name)
            database = runtime_directory / f"{room.room_id}.sqlite3"
            store = SQLiteStore.open(database, time.time)
            service = _build_service(
                store,
                room,
                requested_agent,
                identity.session_id,
                adapter,
            )
            result = _invoke_command(
                arguments,
                service,
                room.root,
                adapter.sample,
                errors,
                identity.agent,
            )
            registry.activate_binding(
                root_key,
                requested_agent,
                identity.session_id,
                explicit_name,
            )
        elif arguments.command == "leave":
            binding = registry.resolve_binding_for_leave(
                root_key,
                identity.agent,
                identity.session_id,
            )
            room = resolve_room(active_cwd, binding.explicit_name)
            database = runtime_directory / f"{room.room_id}.sqlite3"
            if database.exists():
                store = SQLiteStore.open(database, time.time)
                service = _build_service(
                    store,
                    room,
                    identity.agent,
                    identity.session_id,
                    adapter,
                )
                result = _invoke_command(
                    arguments,
                    service,
                    room.root,
                    adapter.sample,
                    errors,
                    identity.agent,
                )
            else:
                result = _left_result(identity.agent)
            registry.delete_binding(
                root_key,
                identity.agent,
                identity.session_id,
            )
        else:
            binding = registry.resolve_active_binding(
                root_key,
                identity.agent,
                identity.session_id,
            )
            room = resolve_room(active_cwd, binding.explicit_name)
            database = runtime_directory / f"{room.room_id}.sqlite3"
            if not database.exists():
                raise RoomDatabaseMissingError(
                    "bound room database is missing"
                )
            store = SQLiteStore.open(database, time.time)
            service = _build_service(
                store,
                room,
                identity.agent,
                identity.session_id,
                adapter,
            )
            result = _invoke_command(
                arguments,
                service,
                room.root,
                adapter.sample,
                errors,
                identity.agent,
            )

        if result is None:
            raise CliOperationalError(
                "waiter_replaced",
                "This wait was replaced by another waiter for the same session.",
            )
        _write_success(
            output,
            {
                "command": arguments.command,
                "room": room.room_id,
                "result": result,
            },
        )
        if service is not None:
            service.acknowledge_delivered_reply()
        return EXIT_SUCCESS
    except _WaitInterrupted:
        _write_error(
            errors,
            "E_INTERRUPTED",
            "Waiting was interrupted; room membership and messages were "
            "preserved.",
            details={"state_preserved": True},
            suggestion="Run ai-room wait --json when ready to continue waiting.",
        )
        return EXIT_FAILURE
    except KeyboardInterrupt:
        raise
    except RoomBindingMissingError:
        _write_error(
            errors,
            "E_NOT_FOUND",
            "No room binding exists for this session; run ai-room join first.",
            details={"reason": "room_binding_missing"},
            suggestion="ai-room join <agent>",
        )
        return EXIT_FAILURE
    except RoomJoinIncompleteError:
        _write_error(
            errors,
            "E_VALIDATION",
            "Room join is incomplete; repeat the same ai-room join command.",
            details={"reason": "room_join_incomplete"},
            suggestion="ai-room join <agent>",
        )
        return EXIT_FAILURE
    except RoomBindingConflictError:
        _write_error(
            errors,
            "E_VALIDATION",
            "This session is bound to another room; leave it before joining.",
            details={"reason": "room_binding_conflict"},
            suggestion="ai-room leave",
        )
        return EXIT_FAILURE
    except RoomDatabaseMissingError:
        _write_error(
            errors,
            "E_NOT_FOUND",
            "The bound room database is missing; leave and join again.",
            details={"reason": "room_database_missing"},
            suggestion="ai-room leave && ai-room join <agent>",
        )
        return EXIT_FAILURE
    except BindingSchemaVersionError as error:
        _write_error(
            errors,
            "E_IO",
            str(error),
            details={"reason": "binding_schema_version_unsupported"},
        )
        return EXIT_FAILURE
    except BindingDatabaseOpenError as error:
        _write_error(
            errors,
            "E_IO",
            str(error),
            details={"reason": "binding_database_open_failed"},
        )
        return EXIT_FAILURE
    except BindingDatabaseBusyError as error:
        _write_error(
            errors,
            "E_IO",
            str(error),
            details={"reason": "binding_database_busy"},
            retryable=True,
        )
        return EXIT_FAILURE
    except CliArgumentError as error:
        _write_error(errors, "E_VALIDATION", str(error))
        return EXIT_ARGUMENT
    except PeerNotJoinedError:
        if arguments.command == "send":
            peer = AgentName(arguments.to)
            message = f"{_AGENT_LABELS[peer]} has not joined this room."
            reason = "peer_not_joined"
        else:
            message = (
                f"{_AGENT_LABELS[identity.agent]} current session is not "
                "joined to this room."
            )
            reason = "session_not_joined"
        _write_error(errors, "E_NOT_FOUND", message, details={"reason": reason})
        return EXIT_FAILURE
    except SessionDetectionError as error:
        _write_error(
            errors,
            "E_PLATFORM",
            str(error),
            details={"reason": "session_detection_failed"},
        )
        return EXIT_FAILURE
    except ValueError as error:
        _write_error(errors, "E_VALIDATION", str(error))
        return EXIT_ARGUMENT
    except SchemaVersionError as error:
        _write_error(
            errors,
            "E_IO",
            str(error),
            details={"reason": "schema_version_unsupported"},
        )
        return EXIT_FAILURE
    except DatabaseOpenError as error:
        _write_error(
            errors,
            "E_IO",
            str(error),
            details={"reason": "database_open_failed"},
        )
        return EXIT_FAILURE
    except DatabaseBusyError as error:
        _write_error(
            errors,
            "E_IO",
            str(error),
            details={"reason": "database_busy"},
            retryable=True,
        )
        return EXIT_FAILURE
    except MalformedMessageError as error:
        _write_error(
            errors,
            "E_IO",
            str(error),
            details={"reason": "malformed_message"},
        )
        return EXIT_FAILURE
    except TaskNotDeliveredError as error:
        _write_error(
            errors,
            "E_VALIDATION",
            str(error),
            details={"reason": "task_not_delivered"},
            suggestion="ai-room wait --json",
        )
        return EXIT_FAILURE
    except TaskConflictError as error:
        _write_error(
            errors,
            "E_VALIDATION",
            str(error),
            details={"reason": "task_conflict"},
        )
        return EXIT_FAILURE
    except WorkspaceCaptureError as error:
        _write_error(
            errors,
            "E_IO",
            f"Workspace capture failed safely: {error.reason}.",
            details={"reason": "workspace_capture_failed"},
        )
        return EXIT_FAILURE
    except DriverError as error:
        _write_error(
            errors,
            "E_EXTERNAL_TOOL",
            str(error),
            details={"reason": "subagent_driver_error"},
        )
        return EXIT_FAILURE
    except CliOperationalError as error:
        if error.code == "no_inflight_run":
            code, rc = "E_NOT_FOUND", EXIT_FAILURE
        elif error.code == "waiter_replaced":
            code, rc = "E_VALIDATION", EXIT_FAILURE
        elif error.code == "resume_agent_required":
            code, rc = "E_VALIDATION", EXIT_ARGUMENT
        elif error.code == "model_defaults_invalid":
            code, rc = "E_VALIDATION", EXIT_FAILURE
        else:
            code, rc = "E_INTERNAL", EXIT_FAILURE
        _write_error(errors, code, str(error), details={"reason": error.code})
        return rc
    except StorageError as error:
        _write_error(
            errors,
            "E_IO",
            str(error),
            details={"reason": "storage_error"},
        )
        return EXIT_FAILURE
    except (KeyError, OSError) as error:
        message = (
            "LOCALAPPDATA is required for ai-room runtime data."
            if isinstance(error, KeyError)
            else f"Runtime operation failed: {type(error).__name__}."
        )
        _write_error(
            errors,
            "E_PLATFORM",
            message,
            details={"reason": "runtime_configuration"},
        )
        return EXIT_FAILURE
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            if registry is not None:
                registry.close()


def _build_service(
    store: SQLiteStore,
    room,
    agent: AgentName,
    session_id: str,
    adapter,
) -> AiRoomService:
    return AiRoomService(
        store,
        room,
        agent,
        session_id=session_id,
        context_adapter=adapter,
    )


def _invoke_command(
    arguments: argparse.Namespace,
    service: AiRoomService,
    root: Path,
    sample_context,
    errors: TextIO,
    agent: AgentName,
) -> dict[str, object] | None:
    return _COMMANDS[arguments.command](
        arguments,
        service,
        root,
        sample_context,
        errors,
        agent,
    )


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if arguments.command != "send":
        return
    kind = _KIND_ARGUMENTS[arguments.kind]
    if kind in _DOCUMENT_REVIEW_KINDS and not arguments.related_doc:
        raise CliArgumentError(
            f"{arguments.kind} requires at least one --related-doc."
        )
    if kind in (TaskKind.DECISION, TaskKind.CONTEXT_CHECK):
        if any(value for value in arguments.writable_doc):
            raise CliArgumentError(
                f"{arguments.kind} does not allow --writable-doc."
            )


def _command_join(
    arguments: argparse.Namespace,
    service: AiRoomService,
    root: Path,
    sample_context,
    stderr: TextIO,
    agent: AgentName,
) -> dict[str, object]:
    member = service.join()
    label = _AGENT_LABELS[member.agent]
    return {
        "agent": member.agent.value,
        "status": member.status.value,
        "message": f"{label} joined this room.",
    }


def _command_wait(
    arguments: argparse.Namespace,
    service: AiRoomService,
    root: Path,
    sample_context,
    stderr: TextIO,
    agent: AgentName,
) -> dict[str, object] | None:
    checkpoints = tuple(
        Path(value)
        for value in _normalize_docs(root, arguments.checkpoint)
    )
    try:
        delivery = service.wait(
            Event(),
            checkpoints,
            next_entry=arguments.next_entry,
        )
    except KeyboardInterrupt:
        raise _WaitInterrupted from None
    if delivery is None:
        return None
    return _delivery_result(delivery, service.get_task(delivery.task_id))


def _command_send(
    arguments: argparse.Namespace,
    service: AiRoomService,
    root: Path,
    sample_context,
    stderr: TextIO,
    agent: AgentName,
) -> dict[str, object]:
    related_docs = _normalize_docs(root, arguments.related_doc)
    writable_docs = _normalize_docs(root, arguments.writable_doc)
    checkpoint_docs = _normalize_docs(root, arguments.checkpoint_doc)
    request = TaskRequest(
        room_id=service.status().room_id,
        sender=agent,
        recipient=AgentName(arguments.to),
        kind=_KIND_ARGUMENTS[arguments.kind],
        question=arguments.question,
        related_docs=related_docs,
        writable_docs=writable_docs,
        context=sample_context(),
        checkpoint_docs=checkpoint_docs,
        next_entry=arguments.next_entry,
        idempotency_key=arguments.idempotency_key or str(uuid.uuid4()),
    )
    task = service.send(request)
    return _task_result(task)


def _command_reply(
    arguments: argparse.Namespace,
    service: AiRoomService,
    root: Path,
    sample_context,
    stderr: TextIO,
    agent: AgentName,
) -> dict[str, object]:
    reply = service.reply(
        arguments.task_id,
        _OUTCOME_ARGUMENTS[arguments.outcome],
        arguments.message,
    )
    return {
        "task_id": reply.task_id,
        "reply_message_id": reply.reply_message_id,
        "state": reply.state.value,
        "guard_violations": list(reply.guard_violations),
    }


def _command_status(
    arguments: argparse.Namespace,
    service: AiRoomService,
    root: Path,
    sample_context,
    stderr: TextIO,
    agent: AgentName,
) -> dict[str, object]:
    status = service.status()
    active_task = (
        None if status.active_task is None else _task_result(status.active_task)
    )
    return {
        "members": {
            agent.value: {
                "status": member.status.value,
                "message": _member_status_message(agent, member.status),
            }
            for agent, member in status.members.items()
        },
        "active_task": active_task,
    }


def _command_leave(
    arguments: argparse.Namespace,
    service: AiRoomService,
    root: Path,
    sample_context,
    stderr: TextIO,
    agent: AgentName,
) -> dict[str, object]:
    service.leave()
    return _left_result(agent)


def _left_result(agent: AgentName) -> dict[str, object]:
    label = _AGENT_LABELS[agent]
    return {
        "agent": agent.value,
        "status": "left",
        "message": f"{label} left this room; messages were preserved.",
    }


def _command_ask(
    arguments: argparse.Namespace,
    root: Path,
    sender: AgentName | None,
) -> dict[str, object]:
    """Run one headless sub-agent dispatch and record it in the ledger.

    ``sender`` is None when the caller could not be identified: ask is a
    mailbox-less command and must not be blocked by session detection.
    """
    # claude is the only vendor that lets the handle be chosen up front. Doing
    # so means a run killed before it emits anything is still resumable, which
    # codex and opencode cannot offer -- their ids only exist once they speak.
    preassigned = str(uuid.uuid4()) if arguments.to == "claude" else None
    return _dispatch(
        arguments,
        root,
        sender,
        target=arguments.to,
        question=arguments.question,
        preassigned=preassigned,
        resume_session=None,
    )


#: What a resumed sub-agent is told when the caller has nothing to add.  It has
#: to say "carry on", not restate the task: the sub-agent already holds the
#: original question, and repeating it invites a restart from scratch, which is
#: the exact waste resuming exists to avoid.
_RESUME_PROMPT = (
    "Continue the task from this conversation where you stopped. "
    "Do not start over and do not repeat work already done; "
    "pick up at the next unfinished step and report the result."
)


def _command_resume(
    arguments: argparse.Namespace,
    root: Path,
    sender: AgentName | None,
) -> dict[str, object]:
    """Continue a dispatch that was cut short rather than paying for it twice.

    A killed run is the most expensive failure there is: the turn was billed in
    full and produced nothing the caller can use.  Re-dispatching the same task
    bills it a second time, so continuing the existing session is the default
    move and this command exists to make it a single call.
    """
    target, session_id, question = _resume_target(arguments, root)
    return _dispatch(
        arguments,
        root,
        sender,
        target=target,
        question=question,
        preassigned=None,
        resume_session=session_id,
    )


def _resume_target(
    arguments: argparse.Namespace,
    root: Path,
) -> tuple[str, str, str]:
    """Decide which session to continue and what to say to it."""
    question = arguments.question or _RESUME_PROMPT
    if arguments.session:
        if not arguments.to:
            raise CliOperationalError(
                "resume_agent_required",
                "--session needs --to claude|codex|opencode|dsh: a handle does not "
                "say which vendor issued it.",
            )
        return arguments.to, arguments.session, question
    candidates = list_inflight(root)
    if arguments.to:
        candidates = [run for run in candidates if run.agent == arguments.to]
    if not candidates:
        raise CliOperationalError(
            "no_inflight_run",
            "No dispatch is recorded as cut short in this room. Pass --to and "
            "--session with a handle from .ai-room/ledger.md to resume an "
            "older one.",
        )
    run = candidates[0]
    return run.agent, run.session_id, question


def _resolve_settings(
    arguments: argparse.Namespace,
    root: Path,
    driver,
    target: str,
    resume_session: str | None,
) -> SessionSettings:
    """Pick the model and effort: explicit flag, then the resumed session's own,
    then the per-agent default in ``resources/models.toml``.

    A resume inherits as a pair and inherits exactly, including "vendor
    default": continuing a conversation on a different model or at a different
    effort than it was started with is a silent downgrade (or upgrade) the
    caller never asked for.  Only a session with no record at all -- started
    before settings were kept, or under --no-ledger -- falls back to defaults.
    """
    recorded = (
        lookup_settings(root, agent=target, session_id=resume_session)
        if resume_session
        else None
    )
    if recorded is not None:
        fallback = recorded
    else:
        try:
            fallback = defaults_for(target)
        except ModelDefaultsError as error:
            raise CliOperationalError("model_defaults_invalid", str(error)) from error
    model = arguments.model or fallback.model
    effort = arguments.effort or fallback.effort
    if effort is not None and driver.efforts is not None:
        if not driver.efforts:
            raise CliArgumentError(f"{target} has no reasoning-effort setting.")
        if effort not in driver.efforts:
            raise CliArgumentError(
                f"{target} does not accept --effort {effort!r}; "
                f"expected one of {', '.join(driver.efforts)}."
            )
    return SessionSettings(model=model, effort=effort)


def _dispatch(
    arguments: argparse.Namespace,
    root: Path,
    sender: AgentName | None,
    *,
    target: str,
    question: str,
    preassigned: str | None,
    resume_session: str | None,
) -> dict[str, object]:
    """Run one sub-agent, whether it is a new task or a continued one."""
    related_docs = normalize_exact_paths(
        root,
        (Path(value) for value in arguments.related_doc),
    )
    driver = driver_for(target)
    settings = _resolve_settings(arguments, root, driver, target, resume_session)
    inflight_path: list[Path] = []

    def keep_settings(session_id: str | None) -> None:
        if session_id and not arguments.no_ledger:
            record_settings(
                root, agent=target, session_id=session_id, settings=settings
            )

    def remember(session_id: str) -> None:
        # Written while the sub-agent is still talking, because the process may
        # be killed from outside and then nothing later in this function runs.
        if arguments.no_ledger:
            return
        keep_settings(session_id)
        try:
            inflight_path.append(
                record_inflight(
                    root,
                    agent=target,
                    session_id=session_id,
                    question=question,
                    cwd=root,
                )
            )
        except OSError:
            # Bookkeeping must never take down a dispatch that is working.
            pass

    request = DriverRequest(
        question=question,
        cwd=root,
        permission=arguments.permission,
        model=settings.model,
        effort=settings.effort,
        timeout=arguments.timeout,
        max_runtime=arguments.max_runtime,
        permission_mode=arguments.permission_mode,
        sandbox=arguments.sandbox,
        related_docs=related_docs,
        session_id=preassigned,
        resume_session=resume_session,
        on_session_id=remember,
    )
    # A preassigned handle is resumable before the sub-agent says a word, so
    # its settings must be on disk before it runs too.
    keep_settings(preassigned or resume_session)
    # The receipt is taken in the sub-agent's own working directory, not the
    # room root: reporting on a directory the work never happened in is worse
    # than reporting nothing.
    workdir = request.cwd
    before = status_lines(workdir)
    try:
        result = driver.invoke(request)
    except DriverTimeout as error:
        return _timed_out(
            arguments,
            root,
            sender,
            target=target,
            request=request,
            related_docs=related_docs,
            error=error,
            driver=driver,
            preassigned=preassigned,
            resume_session=resume_session,
            changed=changed_since(before, status_lines(workdir)),
            inflight=inflight_path,
            keep_settings=keep_settings,
        )
    except DriverError:
        _record_ledger(
            arguments,
            root,
            target,
            related_docs,
            request,
            exit_code=-1,
            status="error",
            session_id=None,
            changed_files=changed_since(before, status_lines(workdir)),
            sender=sender,
        )
        clear_inflight(inflight_path[0] if inflight_path else None)
        raise

    changed = changed_since(before, status_lines(workdir))
    status = "ok" if result.ok else "error"
    keep_settings(result.session_id)
    ledger_path = _record_ledger(
        arguments,
        root,
        target,
        related_docs,
        request,
        exit_code=result.exit_code,
        status=status,
        session_id=result.session_id,
        changed_files=changed,
        sender=sender,
        result=result,
    )
    # The run reported back, so it is not an orphan any more.
    clear_inflight(inflight_path[0] if inflight_path else None)
    return {
        "ok": result.ok,
        "agent": target,
        "status": status,
        "sender": None if sender is None else sender.value,
        "session_id": result.session_id,
        "resumed_from": resume_session,
        "model": settings.model,
        "effort": settings.effort,
        "exit_code": result.exit_code,
        "text": result.text,
        "stderr": result.stderr,
        "changed_files": list(changed),
        "resume_command": _resume_invocation(target, result.session_id, root),
        "ledger": None if ledger_path is None else str(ledger_path),
    }


def _timed_out(
    arguments: argparse.Namespace,
    root: Path,
    sender: AgentName | None,
    *,
    target: str,
    request: DriverRequest,
    related_docs: tuple[str, ...],
    error: DriverTimeout,
    driver,
    preassigned: str | None,
    resume_session: str | None,
    changed: tuple[str, ...],
    inflight: list[Path],
    keep_settings,
) -> dict[str, object]:
    """Report a cut-short dispatch as a result, not as a bare error.

    The turn was paid for in full, so everything it produced is reported: the
    partial answer, and above all the handle needed to continue it.  Raising
    here instead -- which is what this used to do -- left the caller holding a
    one-line "timed out" with no way to resume, and re-dispatching the same
    task from scratch was then the only move available to it.
    """
    salvaged = driver.parse_partial(error.stdout)
    # What the sub-agent actually said wins over what we planned to call it: a
    # vendor that ignored the preassigned handle would otherwise be resumed
    # under an id that names nothing.
    session_id = (
        salvaged.session_id
        or session_id_from(error.stdout)
        or resume_session
        or preassigned
    )
    keep_settings(session_id)
    ledger_path = _record_ledger(
        arguments,
        root,
        target,
        related_docs,
        request,
        exit_code=-1,
        status="timeout",
        session_id=session_id,
        changed_files=changed,
        sender=sender,
        result=salvaged,
    )
    clear_inflight(inflight[0] if inflight else None)
    resume_command = _resume_invocation(target, session_id, root)
    return {
        "ok": False,
        "agent": target,
        "status": "timeout",
        "timeout_reason": error.reason,
        "message": str(error),
        "sender": None if sender is None else sender.value,
        "session_id": session_id,
        "resumed_from": resume_session,
        "model": request.model,
        "effort": request.effort,
        "exit_code": -1,
        "text": salvaged.text,
        "stderr": error.stderr.strip(),
        "changed_files": list(changed),
        "resume_command": resume_command,
        "vendor_resume_command": resume_hint(target, session_id),
        "hint": (
            "This turn was already billed. Continue it with resume_command; "
            "do not re-send the same ask, which starts over and pays again."
            if resume_command
            else "The sub-agent was killed before it announced a handle, so "
            "this turn cannot be continued; re-dispatching is the only option."
        ),
        "ledger": None if ledger_path is None else str(ledger_path),
    }


def _resume_invocation(target: str, session_id: str | None, root: Path) -> str | None:
    """The exact ``ai-room resume`` call that continues this dispatch."""
    if not session_id:
        return None
    return (
        f"ai-room resume --to {target} --session {session_id} --cwd {root}"
    )


def _record_ledger(
    arguments: argparse.Namespace,
    root: Path,
    target: str,
    related_docs: tuple[str, ...],
    request: DriverRequest,
    *,
    exit_code: int,
    status: str,
    session_id: str | None,
    changed_files: tuple[str, ...],
    sender: str | None = None,
    result: DriverResult | None = None,
) -> Path | None:
    """Append one ledger entry unless --no-ledger was given."""
    if arguments.no_ledger:
        return None
    return append_ledger(
        root,
        LedgerEntry(
            agent=target,
            question=request.question,
            session_id=session_id,
            related_docs=related_docs,
            model=request.model,
            effort=request.effort,
            exit_code=exit_code,
            status=status,
            changed_files=changed_files,
            sender=sender,
            is_error=None if result is None else result.is_error,
            subtype=None if result is None else result.subtype,
            permission_denials=()
            if result is None
            else result.permission_denials,
            total_cost_usd=None if result is None else result.total_cost_usd,
            num_turns=None if result is None else result.num_turns,
            usage=None if result is None else result.usage,
        ),
    )


#: The two commands that run a sub-agent instead of touching the mailbox, so
#: they take the same early exit in main() and never require an identity.
_DISPATCH_COMMANDS = {
    "ask": _command_ask,
    "resume": _command_resume,
}

_COMMANDS = {
    "join": _command_join,
    "wait": _command_wait,
    "send": _command_send,
    "reply": _command_reply,
    "status": _command_status,
    "leave": _command_leave,
}


class _WaitInterrupted(BaseException):
    """Internal non-error control flow for an interrupted wait."""


def _normalize_docs(root: Path, values: Sequence[str]) -> tuple[str, ...]:
    return normalize_exact_paths(root, (Path(value) for value in values))


def _task_result(task: TaskView) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "state": task.state.value,
        "kind": task.request.kind.value,
        "sender": task.request.sender.value,
        "recipient": task.request.recipient.value,
    }


def _delivery_result(
    delivery: Delivery,
    task: TaskView,
) -> dict[str, object]:
    context = task.request.context
    reply_command = (
        f"ai-room reply {task.task_id} --outcome "
        "done|blocked|compact-ready|checkpoint-needed --message TEXT"
    )
    return {
        "message_id": delivery.message_id,
        "task_id": task.task_id,
        "kind": task.request.kind.value,
        "sender": delivery.sender.value,
        "recipient": delivery.recipient.value,
        "question": task.request.question,
        "message": delivery.body,
        "outcome": None if delivery.outcome is None else delivery.outcome.value,
        "related_docs": list(task.request.related_docs),
        "writable_docs": list(task.request.writable_docs),
        "checkpoint_docs": list(task.request.checkpoint_docs),
        "context": {
            "input_tokens": _known_or_unknown(context.input_tokens),
            "context_window": _known_or_unknown(context.context_window),
            "source": context.source.value,
        },
        "next_entry": task.request.next_entry,
        "reply": {
            "task_id": task.task_id,
            "command": reply_command,
        },
    }


def _known_or_unknown(value: int | None) -> int | str:
    return "unknown" if value is None else value


def _member_status_message(
    agent: AgentName,
    status: MemberStatus,
) -> str:
    label = _AGENT_LABELS[agent]
    if status is MemberStatus.NEVER_JOINED:
        return f"{label} has not joined this room."
    if status is MemberStatus.WAITING:
        return f"{label} is waiting for messages."
    return f"{label} has joined but is not waiting."


def _write_error(
    stream: TextIO,
    code: str,
    message: str,
    *,
    details: object | None = None,
    retryable: bool = False,
    suggestion: str | None = None,
) -> None:
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if details is not None:
        error["details"] = details
    if suggestion is not None:
        error["suggestion"] = suggestion
    _write_json(stream, {"ok": False, "data": None, "error": error, "meta": {}})


def _write_success(
    stream: TextIO,
    data: object,
) -> None:
    _write_json(stream, {"ok": True, "data": data, "error": None, "meta": {}})


def _write_dispatch_error(
    stream: TextIO,
    command: str,
    room: str,
    result: dict[str, object],
) -> None:
    """Report a failed or cut-short dispatch as an E_EXTERNAL_TOOL envelope.

    The sub-agent (claude/codex/opencode) is an external tool: when it reports
    failure or is killed, the exit code and every field of its receipt belong
    in ``error.details``, and the resume command becomes the ``suggestion`` so
    the caller can continue the billed turn instead of paying for it twice.
    """
    details: dict[str, object] = {"command": command, "room": room}
    for key, value in result.items():
        if key == "ok":
            continue
        details[key] = value
    suggestion = result.get("resume_command")
    message = result.get("message") or result.get("hint") or (
        "sub-agent reported failure"
    )
    _write_error(
        stream,
        "E_EXTERNAL_TOOL",
        message,
        details=details,
        suggestion=suggestion,
    )


def _write_json(stream: TextIO, value: object) -> None:
    stream.write(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    stream.flush()


def _configure_utf8(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


AI_HELP = """---
name: ai-room
description: >
  Coordinate one Codex, one Claude, opencode, and dsh session headlessly.
  Dispatch a sub-agent task (ask), resume a cut-short dispatch (resume), or
  run the mailbox protocol (join/wait/send/reply/status/leave). machine_only:
  every command always prints one JSON envelope to stdout; failures go to
  stderr as an envelope with error.code. Use when the user wants to consult
  another AI, dispatch a task to opencode (dsh only when the user explicitly allows), or continue a killed dispatch.
ai_help_version: 0.1.0
tool_version: 0.1.0
---

## When to Use

Use this tool when the user asks to:
- 把方案交给对面那个 peer 审（你是 claude 就问 codex，是 codex 就问 claude）
- 派一个边界清楚的任务给 opencode 执行（dsh 仅在用户明确允许时）
- 续接一个被掐断的派发（超时的正确动作是 resume，不是重发同一条 ask）
- 开两个窗口互当顾问（信箱模式 join/wait/send/reply）

Do NOT use for:
- 需要两侧同时在线才能进行的实时对话（那是信箱模式，ask 是无头的）
- 让决策者（peer）动手改代码——决策者只出判断，保持 read-only

## Quick Reference

- **咨询决策者（只读）:** `ai-room ask --to codex --question "..." --cwd <project>`
- **派 opencode 执行（默认）:** `ai-room ask --to opencode --permission workspace-write --question "..." --cwd <project>`
- **派 dsh 执行（需用户明确允许）:** `ai-room ask --to dsh --permission workspace-write --question "..." --cwd <project>`
- **续接被掐断的派发:** `ai-room resume --to codex --session <id> --cwd <project>`

## Command Reference

- `ask --to claude|codex|opencode|dsh --question TEXT [--related-doc PATH]... [--permission read-only|workspace-write|full-access] [--model MODEL] [--effort LEVEL] [--cwd DIR] [--timeout SEC] [--max-runtime SEC] [--no-ledger]`
  - 必填：`--to`、`--question`。默认 `read-only`。
  - 不给 `--model`/`--effort` 时用包内 `resources/models.toml` 的默认（决策者是最强档 + max；opencode 用它自己的默认）。
    `--effort` 取值 low|medium|high|xhigh|max（codex 另有 ultra；opencode 传给 `--variant`）。
  - 成功：rc 0，stdout 信封 `data.result` 含 agent/status/model/effort/text/changed_files/session_id/resume_command/ledger。
  - 失败或超时：rc 1，stderr 信封 `error.code=E_EXTERNAL_TOOL`，`error.details` 含完整结果，
    `error.suggestion` 是可续接的 `ai-room resume ...` 命令。
- `resume [--to ...] [--session ID] [--question TEXT] ...`
  - 不给 --session 续接本房间最近一个被掐断的派发；给 --session 必须同时给 --to。
  - 模型和思考档位沿用该会话上一轮的（显式给 `--model`/`--effort` 才覆盖）。
- `join AGENT [--room NAME]` / `wait` / `send` / `reply` / `status` / `leave`（信箱协议，见 SKILL.md）

## Input / Output

- 时间戳一律 ISO 8601；字段 snake_case；缺失用 null。
- `--json` 与 `--format json` 等价接受；本工具 machine_only，默认恒为 JSON，两者都是 no-op。
- stdout 只有一行 JSON 信封；日志/进度/错误信封一律 stderr。

## Side Effects & Safety

- `ask` / `resume`：启动一个真实的子 agent（claude/codex/opencode/dsh），**按整轮计费**。
  超时那一轮已计费——续接（resume），绝不重发。写台账 `.ai-room/ledger.md`（`--no-ledger` 关闭）。
- `join/wait/send/reply/status/leave`：只读写 `%LOCALAPPDATA%/ai-room` 的房间库，不碰项目文件。
- `wait` 是故意阻塞且静默的；中断只结束本次等待，不丢任务、不离开房间（E_INTERRUPTED，可安全重跑）。
- 默认只读档：`--permission read-only` 时子 agent 不能写任何文件。

## Exit Codes

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行/业务失败（含 E_INTERNAL 与可捕获中断 E_INTERRUPTED） |
| 2 | 参数/用法错误（E_VALIDATION） |

## Errors & Recovery

| code | 含义 | 恢复 |
|---|---|---|
| `E_VALIDATION` | 参数/用法错误（rc2） | 修参数；resume --session 需同时给 --to |
| `E_NOT_FOUND` | 房间/绑定/成员/任务不存在 | 先 join 或先 send |
| `E_IO` | 数据库打开/读写/schema 失败 | leave 后重新 join |
| `E_PLATFORM` | 会话身份探测失败/LOCALAPPDATA 缺失 | 在对应 AI CLI 的会话环境内运行 |
| `E_EXTERNAL_TOOL` | 子 agent 驱动失败或派发失败/超时 | 用 error.details.resume_command 续接 |
| `E_INTERRUPTED` | wait 被中断 | 状态已保全，重跑 wait |
| `E_INTERNAL` | 未预期内部错误 | 报 bug |

❌ 错：`ai-room ask --to codex --session abc`（resume 的参数）
✅ 对：`ai-room resume --to codex --session abc --cwd <project>`
"""


def _handle_ai_help(argv: Sequence[str] | None, output: TextIO) -> bool:
    """Eagerly answer ``--ai-help`` before argument parsing.

    The scan runs on the raw argv so that a required subcommand cannot block
    the bare ``--ai-help`` call, and it respects ``--`` as the end of options:
    ``ai-room -- --ai-help`` must treat ``--ai-help`` as an operand.
    """
    if argv is None:
        argv = sys.argv[1:]
    stopped = False
    for token in argv:
        if stopped:
            break
        if token == "--":
            stopped = True
            continue
        if token == "--ai-help":
            output.write(AI_HELP)
            output.flush()
            return True
    return False
