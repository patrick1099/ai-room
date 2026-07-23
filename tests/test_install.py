from __future__ import annotations

import hashlib
import json
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
    assert all(phrase in text for phrase in required_phrases)
    assert "never edit source code" in text.lower()
    assert "never run tests, builds, deployments, or real operations" in text.lower()


def test_check_records_operations_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "isolated home"
    plan = _plan(home)

    report = execute_install(plan, RecordingWriter())

    assert [operation.action for operation in report.operations] == [
        "write",
        "write",
        "write",
    ]
    assert not home.exists()


def test_apply_installs_skills_and_one_session_start_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home)

    report = execute_install(plan, FilesystemWriter())

    assert len(report.operations) == 3
    source_bytes = SOURCE_SKILL.read_bytes()
    assert all(path.read_bytes() == source_bytes for path in _skill_paths(home))
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
    assert [operation.action for operation in second.operations] == [
        "unchanged",
        "unchanged",
        "unchanged",
    ]
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
        "hooks": [{"type": "command", "command": expected_command}],
    }
    data = json.loads(_settings_path(home).read_text(encoding="utf-8"))
    assert _ai_room_groups(data) == [plan.session_start_group]
    expected_hash = hashlib.sha256(SOURCE_SKILL.read_bytes()).hexdigest()
    assert [operation.sha256 for operation in report.operations[:2]] == [
        expected_hash,
        expected_hash,
    ]
    assert all(
        path.read_bytes() == SOURCE_SKILL.read_bytes()
        for path in _skill_paths(home)
    )


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
    assert len(payload["operations"]) == 3
    assert not home.exists()
