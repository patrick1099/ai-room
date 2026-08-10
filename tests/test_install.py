from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import ai_room.install as install_module
from ai_room.install import (
    FilesystemWriter,
    InstallConflictError,
    RecordingWriter,
    build_install_plan,
    execute_install,
    main,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = REPOSITORY_ROOT / "integrations" / "ai-room" / "SKILL.md"


def _plan(home: Path, python_exe: Path | None = None):
    return build_install_plan(
        home,
        python_exe or Path(sys.executable),
        SOURCE_SKILL,
    )


def _settings_path(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _skill_paths(home: Path) -> tuple[Path, Path]:
    return (
        home / ".codex" / "skills" / "ai-room" / "SKILL.md",
        home / ".claude" / "skills" / "ai-room" / "SKILL.md",
    )


def _skill_file_names() -> tuple[str, ...]:
    """Every file the skill is made of: the router plus one manual per vendor.

    Read from the repository rather than hard-coded, so adding a manual does
    not silently stop being installed.
    """
    return tuple(
        sorted(path.name for path in SOURCE_SKILL.parent.iterdir() if path.suffix == ".md")
    )


def _expected_skill_files(home: Path) -> dict[Path, bytes]:
    """Each destination the installer must write, mapped to its source bytes."""
    return {
        home / vendor / "skills" / "ai-room" / name: (
            SOURCE_SKILL.parent / name
        ).read_bytes()
        for vendor in (".codex", ".claude")
        for name in _skill_file_names()
    }


def _skill_operation_count() -> int:
    return len(_skill_file_names()) * 2


def _write_settings(home: Path, data: object) -> Path:
    path = _settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _ai_room_groups(data: dict[str, object]) -> list[object]:
    groups = data["hooks"]["SessionStart"]  # type: ignore[index]
    return [
        group
        for group in groups  # type: ignore[union-attr]
        if any(
            "ai_room.hooks.claude_session_start" in hook.get("command", "")
            for hook in group.get("hooks", [])
            if isinstance(hook, dict)
        )
    ]


def test_shared_skill_contains_the_role_and_compaction_contract() -> None:
    text = SOURCE_SKILL.read_text(encoding="utf-8")

    required_phrases = (
        "ai-room join codex",
        "ai-room join claude",
        "ai-room wait",
        "--related-doc EXACT_PATH",
        "--writable-doc EXACT_PATH",
        "DONE",
        "BLOCKED",
        "CHECKPOINT_NEEDED",
        "COMPACT_READY",
        "Esc",
        "Ctrl+C",
        "150k",
        "200k",
        "/compact",
        "writable_docs",
    )
    missing = [phrase for phrase in required_phrases if phrase not in text]
    assert not missing, f"shared skill lost: {missing}"
    # The advisor boundary is the one thing a reader must not be able to miss.
    assert "绝不改源码" in text
    assert "绝不替主聊跑测试、构建、部署或任何真实操作" in text
    # `ask` is the default and the mailbox is opt-in.  Lose this and the model
    # goes back to asking the user to open a second window for a one-command job.
    assert "默认走 `ask`" in text
    # The role split is the whole point: claude/codex decide, opencode executes,
    # and no *plan* reaches opencode until a decision-maker has seen it.
    assert "只当执行者" in text
    assert "先咨询，后执行" in text
    # The gate is on consultation, not on dispatch.  Without this the model
    # reads the rule as per-task and either grinds through mechanical work
    # itself or burns a consultation round on a typo fix.
    assert "廉价劳力" in text
    assert "这件事有没有技术取舍" in text
    # The two timeout rules that cost real money when a reader gets them wrong:
    # re-sending buys an already-billed turn a second time, and shrinking the
    # silence budget to dodge an outer shell gate kills sub-agents mid-thought.
    assert "续接，绝不重发" in text
    assert "resume_command" in text
    assert "压小 `--timeout`" in text


def test_shared_skill_routes_to_one_manual_per_vendor() -> None:
    """SKILL.md is a router: every vendor manual it names must exist.

    Without this, dropping or renaming a manual leaves the router pointing at
    nothing, and the vendor that reads it silently loses its whole contract.
    """
    text = SOURCE_SKILL.read_text(encoding="utf-8")
    # Each manual must name that vendor's own role and peer -- getting the
    # peer wrong means consulting the executor or dispatching work to the
    # decision-maker -- plus how it is identified and the shell timeout that
    # silently kills a blocking `ask`.
    manuals = {
        "claude-code.md": (
            "先让 codex 过一遍方案",
            "廉价劳力",
            "AI_ROOM_CLAUDE_SESSION_ID",
            "ai-room wait",
            # The knob, not a number: the ceiling is configurable per machine,
            # so pinning a literal value here would only pin a stale claim.
            "BASH_MAX_TIMEOUT_MS",
            "ai-room resume",
            "压小 `--timeout`",
        ),
        "codex.md": (
            "先让 claude 过一遍方案",
            "廉价劳力",
            "CODEX_THREAD_ID",
            "ai-room wait",
            "timeout_ms",
            "ai-room resume",
            "压小 `--timeout`",
        ),
        # opencode is the executor and has no mailbox identity at all, so its
        # manual must say both rather than describe a role it cannot hold.
        "opencode.md": (
            "你是干活的那一个",
            "只能用 `ask`",
            "invalid choice: 'opencode'",
            "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
            "ai-room resume",
            "压小 `--timeout`",
        ),
    }
    for name, required in manuals.items():
        assert name in text, f"SKILL.md no longer routes to {name}"
        manual = SOURCE_SKILL.parent / name
        assert manual.is_file(), f"routed manual is missing: {manual}"
        body = manual.read_text(encoding="utf-8")
        for phrase in required:
            assert phrase in body, f"{name} lost: {phrase!r}"
        # Every manual must send the reader to the shared contract, not
        # re-state it and drift.
        assert "SKILL.md" in body, f"{name} does not point back at SKILL.md"


def test_check_records_operations_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "isolated home"
    plan = _plan(home)

    report = execute_install(plan, RecordingWriter())

    assert [operation.action for operation in report.operations] == ["write"] * (
        _skill_operation_count() + 1
    )
    assert not home.exists()


def test_apply_installs_skills_and_one_session_start_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home)

    report = execute_install(plan, FilesystemWriter())

    assert len(report.operations) == _skill_operation_count() + 1
    # The router is useless without the manuals it names, so both vendors must
    # receive the whole directory, not just SKILL.md.
    for path, expected in _expected_skill_files(home).items():
        assert path.read_bytes() == expected, path
    data = json.loads(_settings_path(home).read_text(encoding="utf-8"))
    assert _ai_room_groups(data) == [plan.session_start_group]


def test_repeat_apply_is_idempotent_and_does_not_duplicate_hook(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home)
    execute_install(plan, FilesystemWriter())

    second = execute_install(plan, FilesystemWriter())

    data = json.loads(_settings_path(home).read_text(encoding="utf-8"))
    assert _ai_room_groups(data) == [plan.session_start_group]
    assert [operation.action for operation in second.operations] == ["unchanged"] * (
        _skill_operation_count() + 1
    )
    assert not list(_settings_path(home).parent.glob("settings.json.*.bak"))


def test_existing_claude_settings_and_hooks_are_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    stop_group = {
        "matcher": "",
        "hooks": [{"type": "command", "command": "existing-stop"}],
    }
    session_group = {
        "matcher": "startup",
        "hooks": [{"type": "command", "command": "existing-start"}],
    }
    _write_settings(
        home,
        {
            "theme": "dark",
            "hooks": {
                "Stop": [stop_group],
                "SessionStart": [session_group],
            },
        },
    )

    execute_install(_plan(home), FilesystemWriter())

    data = json.loads(_settings_path(home).read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["hooks"]["Stop"] == [stop_group]
    assert data["hooks"]["SessionStart"] == [
        session_group,
        _plan(home).session_start_group,
    ]


def test_invalid_settings_json_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    settings = _settings_path(home)
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")

    with pytest.raises(InstallConflictError, match="valid JSON"):
        execute_install(_plan(home), FilesystemWriter())

    assert all(not path.exists() for path in _skill_paths(home))
    assert settings.read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("value", [[], "settings"])
def test_non_object_settings_json_is_refused(
    tmp_path: Path,
    value: object,
) -> None:
    home = tmp_path / "home"
    _write_settings(home, value)

    with pytest.raises(InstallConflictError, match="JSON object"):
        execute_install(_plan(home), FilesystemWriter())


@pytest.mark.parametrize(
    ("settings_data", "message"),
    [
        ({"hooks": None}, "hooks must be a JSON object"),
        (
            {"hooks": {"SessionStart": None}},
            "SessionStart hooks must be a JSON array",
        ),
    ],
)
def test_present_null_hook_configuration_is_refused_before_any_write(
    tmp_path: Path,
    settings_data: dict[str, object],
    message: str,
) -> None:
    home = tmp_path / "home"
    settings = _write_settings(home, settings_data)
    original = settings.read_bytes()

    with pytest.raises(InstallConflictError, match=message):
        execute_install(_plan(home), FilesystemWriter())

    assert settings.read_bytes() == original
    assert all(not path.exists() for path in _skill_paths(home))


@pytest.mark.parametrize("settings_data", [{}, {"hooks": {}}])
def test_missing_hook_keys_are_initialized(
    tmp_path: Path,
    settings_data: dict[str, object],
) -> None:
    home = tmp_path / "home"
    _write_settings(home, settings_data)
    plan = _plan(home)

    execute_install(plan, FilesystemWriter())

    installed = json.loads(_settings_path(home).read_text(encoding="utf-8"))
    assert _ai_room_groups(installed) == [plan.session_start_group]


def test_atomic_settings_failure_keeps_existing_file_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    for skill in _skill_paths(home):
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_bytes(SOURCE_SKILL.read_bytes())
    settings = _settings_path(home)
    original = b'{\n  "theme": "dark"\n}\n'
    settings.write_bytes(original)
    real_replace = install_module.os.replace

    def fail_settings_replace(source: Path, target: Path) -> None:
        if Path(target) == settings:
            raise OSError("replace denied")
        real_replace(source, target)

    monkeypatch.setattr(
        install_module.os,
        "replace",
        fail_settings_replace,
    )

    with pytest.raises(OSError, match="replace denied"):
        execute_install(_plan(home), FilesystemWriter())

    assert settings.read_bytes() == original
    assert list(settings.parent.glob(f".{settings.name}.*.tmp")) == []


def test_existing_settings_are_backed_up_before_the_changed_file(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    original = b'{\n  "theme": "dark"\n}\n'
    settings = _settings_path(home)
    settings.parent.mkdir(parents=True)
    settings.write_bytes(original)
    plan = _plan(home)

    report = execute_install(plan, FilesystemWriter())

    actions = [(operation.action, operation.path) for operation in report.operations]
    backup_index = next(
        index for index, (action, _) in enumerate(actions) if action == "backup"
    )
    settings_index = actions.index(("write", settings))
    assert backup_index < settings_index
    backup_path = actions[backup_index][1]
    assert backup_path.parent == settings.parent
    assert backup_path.name.startswith("settings.json.")
    assert backup_path.name.endswith(".bak")
    assert backup_path.read_bytes() == original


def test_hook_pins_bash_because_powershell_cannot_parse_the_command(
    tmp_path: Path,
) -> None:
    """A quoted path followed by -m is a parse error in PowerShell.

    The command is quoted with Windows rules so a Python under "Program Files"
    still works, but PowerShell reads a leading quoted path as a string
    expression and rejects the next token. Leaving the shell to Claude Code's
    default meant the hook silently died that way whenever Git Bash was not on
    the search path -- which is what happens when claude runs as an ai-room
    sub-agent.
    """
    plan = _plan(tmp_path / "home")
    hook = plan.session_start_group["hooks"][0]
    assert hook["shell"] == "bash"


def test_unicode_and_spaces_paths_install_exact_skills_and_hook(
    tmp_path: Path,
) -> None:
    home = tmp_path / "用户 home with spaces"
    python_exe = tmp_path / "Python 运行时 Runtime" / "python.exe"

    plan = _plan(home, python_exe)
    report = execute_install(plan, FilesystemWriter())

    expected_command = subprocess.list2cmdline(
        [str(python_exe), "-m", "ai_room.hooks.claude_session_start"]
    )
    assert plan.session_start_group == {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
            {"type": "command", "command": expected_command, "shell": "bash"}
        ],
    }
    data = json.loads(_settings_path(home).read_text(encoding="utf-8"))
    assert _ai_room_groups(data) == [plan.session_start_group]
    recorded = {operation.path: operation.sha256 for operation in report.operations}
    for path, expected in _expected_skill_files(home).items():
        assert path.read_bytes() == expected, path
        assert recorded[path] == hashlib.sha256(expected).hexdigest(), path


def test_installed_skill_hashes_match_repository_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    execute_install(_plan(home), FilesystemWriter())

    expected = hashlib.sha256(SOURCE_SKILL.read_bytes()).hexdigest()
    actual = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _skill_paths(home)
    ]
    assert actual == [expected, expected]


def test_differing_existing_skill_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    codex_skill, claude_skill = _skill_paths(home)
    claude_skill.parent.mkdir(parents=True)
    claude_skill.write_bytes(b"user-modified skill")

    with pytest.raises(InstallConflictError, match="differs from repository source"):
        execute_install(_plan(home), FilesystemWriter())

    assert not codex_skill.exists()
    assert claude_skill.read_bytes() == b"user-modified skill"
    assert not _settings_path(home).exists()


def test_preview_and_apply_share_the_same_operations(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(home, {"theme": "dark"})
    plan = _plan(home)

    preview = execute_install(plan, RecordingWriter())
    applied = execute_install(plan, FilesystemWriter())

    assert preview.operations == applied.operations


def test_existing_non_file_destination_is_refused_before_writes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    destination = _skill_paths(home)[1]
    destination.mkdir(parents=True)

    with pytest.raises(InstallConflictError, match="regular file"):
        execute_install(_plan(home), FilesystemWriter())

    assert not _skill_paths(home)[0].exists()


@pytest.mark.parametrize(
    "ancestor_parts",
    [
        (),
        (".codex",),
        (".codex", "skills"),
        (".codex", "skills", "ai-room"),
        (".claude",),
        (".claude", "skills"),
        (".claude", "skills", "ai-room"),
    ],
)
def test_non_directory_ancestor_is_refused_before_any_write(
    tmp_path: Path,
    ancestor_parts: tuple[str, ...],
) -> None:
    home = tmp_path / "home"
    ancestor = home.joinpath(*ancestor_parts)
    ancestor.parent.mkdir(parents=True, exist_ok=True)
    ancestor.write_bytes(b"user-owned ancestor")

    with pytest.raises(InstallConflictError, match="ancestor is not a directory"):
        execute_install(_plan(home), FilesystemWriter())

    assert ancestor.read_bytes() == b"user-owned ancestor"
    assert all(not path.exists() for path in _skill_paths(home))
    assert not _settings_path(home).exists()


def test_non_directory_backup_ancestor_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    settings = _write_settings(home, {"theme": "dark"})
    original_settings = settings.read_bytes()
    blocked_parent = home / "blocked backup parent"
    blocked_parent.write_bytes(b"user-owned backup ancestor")
    plan = replace(
        _plan(home),
        settings_backup=blocked_parent / "settings.json.timestamp.bak",
    )

    with pytest.raises(InstallConflictError, match="ancestor is not a directory"):
        execute_install(plan, FilesystemWriter())

    assert blocked_parent.read_bytes() == b"user-owned backup ancestor"
    assert settings.read_bytes() == original_settings
    assert all(not path.exists() for path in _skill_paths(home))


def test_windows_reparse_point_ancestor_is_refused_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    reparse_ancestor = home / ".claude"
    reparse_ancestor.mkdir(parents=True)
    actual_file_attributes = getattr(
        install_module,
        "_file_attributes",
        lambda path: 0,
    )

    def simulated_file_attributes(path: Path) -> int:
        if path == reparse_ancestor:
            return 0x400
        return actual_file_attributes(path)

    monkeypatch.setattr(
        install_module,
        "_file_attributes",
        simulated_file_attributes,
        raising=False,
    )

    with pytest.raises(InstallConflictError, match="ancestor is not a directory"):
        execute_install(_plan(home), FilesystemWriter())

    assert reparse_ancestor.is_dir()
    assert all(not path.exists() for path in _skill_paths(home))
    assert not _settings_path(home).exists()


def test_ordinary_real_directory_ancestors_remain_valid(tmp_path: Path) -> None:
    home = tmp_path / "ordinary home"
    (home / ".codex" / "skills").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)

    execute_install(_plan(home), FilesystemWriter())

    assert all(
        path.read_bytes() == SOURCE_SKILL.read_bytes()
        for path in _skill_paths(home)
    )


def test_conflicting_ai_room_hook_command_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(
        home,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "other-python -m "
                                    "ai_room.hooks.claude_session_start"
                                ),
                            }
                        ],
                    }
                ]
            }
        },
    )

    with pytest.raises(InstallConflictError, match="conflicting ai-room"):
        execute_install(_plan(home), FilesystemWriter())


def test_main_check_uses_isolated_home_and_reports_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "isolated home"

    result = main(
        ["--check"],
        home=home,
        python_exe=Path(sys.executable),
        source_skill=SOURCE_SKILL,
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "check"
    assert len(payload["operations"]) == _skill_operation_count() + 1
    assert not home.exists()


def test_non_editable_install_uses_skill_matching_repository_source(
    tmp_path: Path,
) -> None:
    installed_target = tmp_path / "installed target"
    isolated_home = tmp_path / "isolated home"
    install_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(installed_target),
            str(REPOSITORY_ROOT),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install_result.returncode == 0, (
        install_result.stdout + install_result.stderr
    )

    installed_environment = os.environ.copy()
    installed_environment["HOME"] = str(isolated_home)
    installed_environment["USERPROFILE"] = str(isolated_home)
    installed_environment["PYTHONPATH"] = str(installed_target)
    check_result = subprocess.run(
        [sys.executable, "-m", "ai_room.install", "--check"],
        cwd=tmp_path,
        env=installed_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check_result.returncode == 0, check_result.stderr

    packaged_skill = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from importlib.resources import files;"
                "import sys;"
                "sys.stdout.buffer.write("
                "files('ai_room').joinpath('resources', 'SKILL.md').read_bytes()"
                ")"
            ),
        ],
        cwd=tmp_path,
        env=installed_environment,
        capture_output=True,
        check=False,
    )
    assert packaged_skill.returncode == 0, packaged_skill.stderr.decode(
        errors="replace"
    )

    # A wheel that ships the router without its manuals installs a skill that
    # points at files that are not there, so the package data glob is part of
    # the contract.
    packaged_names = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from importlib.resources import files;"
                "print(' '.join(sorted("
                "entry.name for entry in files('ai_room').joinpath('resources').iterdir()"
                " if entry.name.endswith('.md'))))"
            ),
        ],
        cwd=tmp_path,
        env=installed_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert packaged_names.returncode == 0, packaged_names.stderr
    assert packaged_names.stdout.split() == list(_skill_file_names())

    source_bytes = SOURCE_SKILL.read_bytes()
    assert packaged_skill.stdout == source_bytes
    expected_hash = hashlib.sha256(source_bytes).hexdigest()
    payload = json.loads(check_result.stdout)
    installed_names: dict[str, set[str]] = {}
    for operation in payload["operations"]:
        operation_path = Path(operation["path"])
        if operation_path.suffix == ".md":
            installed_names.setdefault(operation_path.name, set()).add(
                operation["sha256"]
            )
    assert set(installed_names) == set(_skill_file_names())
    assert installed_names["SKILL.md"] == {expected_hash}
    assert not isolated_home.exists()
