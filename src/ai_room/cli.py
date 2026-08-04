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
from .drivers import DriverError, DriverRequest, DriverTimeout, driver_for
from .ledger import LedgerEntry, append_ledger
from .paths import normalize_root, resolve_room, runtime_root
from .service import AiRoomService
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
EXIT_ARGUMENT = 2
EXIT_OPERATIONAL = 3
EXIT_CANCEL = 130

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
        description="Coordinate one Codex and one Claude session.",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
    )

    join = commands.add_parser("join", help="join the current room")
    join.add_argument("agent", choices=tuple(agent.value for agent in AgentName))
    join.add_argument("--room", metavar="NAME")

    wait = commands.add_parser("wait", help="wait silently for one delivery")
    wait.add_argument(
        "--checkpoint",
        action="append",
        default=[],
        metavar="EXACT_PATH",
    )
    wait.add_argument("--next-entry", metavar="TEXT")

    send = commands.add_parser("send", help="send one advisor task")
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
    reply.add_argument("task_id")
    reply.add_argument(
        "--outcome",
        required=True,
        choices=tuple(_OUTCOME_ARGUMENTS),
    )
    reply.add_argument("--message", required=True)

    commands.add_parser("status", help="show room membership and active task")
    commands.add_parser("leave", help="leave without deleting messages")

    ask = commands.add_parser(
        "ask",
        help="dispatch one headless sub-agent task and record it in the ledger",
    )
    ask.add_argument(
        "--to",
        required=True,
        choices=("claude", "codex", "opencode"),
    )
    ask.add_argument("--question", required=True)
    ask.add_argument(
        "--related-doc",
        action="append",
        default=[],
        metavar="EXACT_PATH",
    )
    ask.add_argument(
        "--writable-doc",
        action="append",
        default=[],
        metavar="EXACT_PATH",
    )
    ask.add_argument("--model", metavar="MODEL")
    ask.add_argument("--cwd", metavar="DIR", help="which project to dispatch against; the sub-agent runs in the room root selected by this path")
    ask.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS")
    ask.add_argument("--permission-mode", dest="permission_mode", metavar="MODE")
    ask.add_argument("--sandbox", metavar="MODE")
    ask.add_argument("--no-ledger", action="store_true")
    return parser


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
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
        _validate_arguments(arguments)
    except CliArgumentError as error:
        _write_error(errors, "argument_error", str(error))
        return EXIT_ARGUMENT
    except argparse.ArgumentError as error:
        _write_error(errors, "argument_error", str(error))
        return EXIT_ARGUMENT
    except SystemExit as exit_request:
        return int(exit_request.code)

    active_environ = os.environ if environ is None else environ
    active_cwd = Path.cwd() if cwd is None else Path(cwd)
    store: SQLiteStore | None = None
    registry: BindingRegistry | None = None
    service: AiRoomService | None = None
    try:
        if arguments.command == "ask":
            ask_cwd = Path(arguments.cwd) if arguments.cwd else active_cwd
            room = resolve_room(ask_cwd)
            try:
                sender = detect_current_session(active_environ).agent
            except SessionDetectionError:
                sender = None
            result = _command_ask(arguments, room.root, sender)
            ok = bool(result.get("ok"))
            _write_json(
                output,
                {
                    "ok": ok,
                    "command": "ask",
                    "room": room.room_id,
                    "result": result,
                },
            )
            return EXIT_SUCCESS if ok else EXIT_OPERATIONAL
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
        _write_json(
            output,
            {
                "ok": True,
                "command": arguments.command,
                "room": room.room_id,
                "result": result,
            },
        )
        if service is not None:
            service.acknowledge_delivered_reply()
        return EXIT_SUCCESS
    except _WaitInterrupted:
        return EXIT_CANCEL
    except KeyboardInterrupt:
        raise
    except RoomBindingMissingError:
        _write_error(
            errors,
            "room_binding_missing",
            "No room binding exists for this session; run ai-room join first.",
        )
        return EXIT_OPERATIONAL
    except RoomJoinIncompleteError:
        _write_error(
            errors,
            "room_join_incomplete",
            "Room join is incomplete; repeat the same ai-room join command.",
        )
        return EXIT_OPERATIONAL
    except RoomBindingConflictError:
        _write_error(
            errors,
            "room_binding_conflict",
            "This session is bound to another room; leave it before joining.",
        )
        return EXIT_OPERATIONAL
    except RoomDatabaseMissingError:
        _write_error(
            errors,
            "room_database_missing",
            "The bound room database is missing; leave and join again.",
        )
        return EXIT_OPERATIONAL
    except BindingSchemaVersionError as error:
        _write_error(
            errors,
            "binding_schema_version_unsupported",
            str(error),
        )
        return EXIT_OPERATIONAL
    except BindingDatabaseOpenError as error:
        _write_error(errors, "binding_database_open_failed", str(error))
        return EXIT_OPERATIONAL
    except BindingDatabaseBusyError as error:
        _write_error(errors, "binding_database_busy", str(error))
        return EXIT_OPERATIONAL
    except CliArgumentError as error:
        _write_error(errors, "argument_error", str(error))
        return EXIT_ARGUMENT
    except PeerNotJoinedError:
        if arguments.command == "send":
            peer = AgentName(arguments.to)
            message = f"{_AGENT_LABELS[peer]} has not joined this room."
            code = "peer_not_joined"
        else:
            message = (
                f"{_AGENT_LABELS[identity.agent]} current session is not "
                "joined to this room."
            )
            code = "session_not_joined"
        _write_error(errors, code, message)
        return EXIT_OPERATIONAL
    except SessionDetectionError as error:
        _write_error(errors, "session_detection_failed", str(error))
        return EXIT_OPERATIONAL
    except ValueError as error:
        _write_error(errors, "argument_error", str(error))
        return EXIT_ARGUMENT
    except SchemaVersionError as error:
        _write_error(errors, "schema_version_unsupported", str(error))
        return EXIT_OPERATIONAL
    except DatabaseOpenError as error:
        _write_error(errors, "database_open_failed", str(error))
        return EXIT_OPERATIONAL
    except DatabaseBusyError as error:
        _write_error(errors, "database_busy", str(error))
        return EXIT_OPERATIONAL
    except MalformedMessageError as error:
        _write_error(errors, "malformed_message", str(error))
        return EXIT_OPERATIONAL
    except TaskNotDeliveredError as error:
        _write_error(errors, "task_not_delivered", str(error))
        return EXIT_OPERATIONAL
    except TaskConflictError as error:
        _write_error(errors, "task_conflict", str(error))
        return EXIT_OPERATIONAL
    except WorkspaceCaptureError as error:
        _write_error(
            errors,
            "workspace_capture_failed",
            f"Workspace capture failed safely: {error.reason}.",
        )
        return EXIT_OPERATIONAL
    except DriverError as error:
        _write_error(errors, "subagent_driver_error", str(error))
        return EXIT_OPERATIONAL
    except CliOperationalError as error:
        _write_error(errors, error.code, str(error))
        return EXIT_OPERATIONAL
    except StorageError as error:
        _write_error(errors, "storage_error", str(error))
        return EXIT_OPERATIONAL
    except (KeyError, OSError) as error:
        message = (
            "LOCALAPPDATA is required for ai-room runtime data."
            if isinstance(error, KeyError)
            else f"Runtime operation failed: {type(error).__name__}."
        )
        _write_error(errors, "runtime_configuration", message)
        return EXIT_OPERATIONAL
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
        stderr.write(
            "ai-room wait interrupted; room membership and messages were "
            "preserved.\n"
        )
        stderr.flush()
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
    target = arguments.to
    related_docs = normalize_exact_paths(
        root,
        (Path(value) for value in arguments.related_doc),
    )
    writable_docs = normalize_exact_paths(
        root,
        (Path(value) for value in arguments.writable_doc),
    )
    request = DriverRequest(
        question=arguments.question,
        cwd=root,
        model=arguments.model,
        timeout=arguments.timeout,
        permission_mode=arguments.permission_mode,
        sandbox=arguments.sandbox,
        related_docs=related_docs,
        writable_docs=writable_docs,
    )
    driver = driver_for(target)
    before = capture_workspace(root)
    try:
        result = driver.invoke(request)
    except DriverTimeout:
        _record_ledger(
            arguments,
            root,
            target,
            related_docs,
            request,
            exit_code=-1,
            status="timeout",
            session_id=None,
            violations=(),
            sender=sender,
        )
        raise
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
            violations=(),
            sender=sender,
        )
        raise

    # The sub-agent already ran and its session id is in hand; only the
    # after-capture can raise WorkspaceCaptureError here, so the ledger must
    # keep the real session id instead of None.
    try:
        after = capture_workspace(root)
        guard = compare_workspace(before, after, writable_docs)
    except WorkspaceCaptureError:
        _record_ledger(
            arguments,
            root,
            target,
            related_docs,
            request,
            exit_code=result.exit_code,
            status="capture-error",
            session_id=result.session_id,
            violations=(),
            sender=sender,
        )
        raise

    status = "ok" if result.ok else "error"
    if guard.violations:
        status = "guard-blocked"
    ledger_path = _record_ledger(
        arguments,
        root,
        target,
        related_docs,
        request,
        exit_code=result.exit_code,
        status=status,
        session_id=result.session_id,
        violations=guard.violations,
        sender=sender,
    )
    ok = result.ok and not guard.violations
    return {
        "ok": ok,
        "agent": target,
        "sender": None if sender is None else sender.value,
        "session_id": result.session_id,
        "exit_code": result.exit_code,
        "text": result.text,
        "stderr": result.stderr,
        "guard_violations": list(guard.violations),
        "ledger": None if ledger_path is None else str(ledger_path),
    }


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
    violations: tuple[str, ...],
    sender: str | None = None,
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
            exit_code=exit_code,
            status=status,
            violations=violations,
            sender=sender,
        ),
    )


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
    **details: object,
) -> None:
    error: dict[str, object] = {"code": code, "message": message}
    error.update(details)
    _write_json(stream, {"ok": False, "error": error})


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
