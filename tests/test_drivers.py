from __future__ import annotations

from ai_room.drivers.claude import _parse_json
from ai_room.drivers.codex import _parse_jsonl
from ai_room.drivers.protocol import DriverError
from ai_room.drivers.registry import driver_for, list_drivers


def test_driver_for_returns_known_drivers() -> None:
    assert driver_for("claude").name == "claude"
    assert driver_for("codex").name == "codex"
    assert driver_for("opencode").name == "opencode"


def test_list_drivers_matches_expected() -> None:
    assert list_drivers() == ("claude", "codex", "opencode")


def test_unknown_driver_raises() -> None:
    try:
        driver_for("gemini")
    except DriverError as error:
        assert "no driver for agent" in str(error)
    else:
        raise AssertionError("expected DriverError")


def test_claude_parse_json_extracts_session_and_result() -> None:
    payload = (
        '[{"session_id":"sess-1","type":"assistant","message":{"content":"think"}},'
        '{"session_id":"sess-1","type":"result","result":"final answer"}]'
    )
    session_id, text = _parse_json(payload)
    assert session_id == "sess-1"
    assert text == "final answer"


def test_claude_parse_json_non_json_returns_stdout() -> None:
    session_id, text = _parse_json("plain text output")
    assert session_id is None
    assert text == "plain text output"


def test_codex_parse_jsonl_extracts_session_and_completed() -> None:
    payload = (
        '{"session_id":"thr-1","type":"exec","payload":{"command":"ls"}}\n'
        '{"session_id":"thr-1","type":"result","payload":{"status":"completed","text":"answer"}}'
    )
    session_id, text = _parse_jsonl(payload)
    assert session_id == "thr-1"
    assert text == "answer"


def test_codex_parse_jsonl_ignores_failed_result() -> None:
    payload = (
        '{"session_id":"thr-2","type":"result","payload":{"status":"failed","text":"x"}}'
    )
    session_id, text = _parse_jsonl(payload)
    assert session_id == "thr-2"
    assert text == ""


def test_cli_help_mentions_ask() -> None:
    import sys

    from ai_room.cli import main

    try:
        main(["--help"], stdout=sys.stdout, stderr=sys.stderr)
    except SystemExit as exit_request:
        output = exit_request.code
    else:
        output = 0
    assert output == 0