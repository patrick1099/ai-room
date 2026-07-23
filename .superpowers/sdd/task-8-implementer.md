# Task 8 Implementer Report

## Status

Implemented the shared Codex/Claude agent guidance and the idempotent user
installer on base `2cc6d9d`.

## TDD evidence

- RED: `tests/test_install.py` failed during collection with
  `ModuleNotFoundError: No module named 'ai_room.install'`, proving the Task 8
  installer interface did not exist.
- First implementation run reached `15 passed, 1 failed`; the only failure
  identified a line-wrapped advisor prohibition in the shared skill contract.
- Focused GREEN after the minimum text correction:
  `16 passed in 0.28s`.
- Fresh post-refactor pre-commit focused verification:
  `16 passed in 0.32s`.

## Implementation

- `integrations/ai-room/SKILL.md`
  - tells each visible AI to join with its own identity and wait while idle;
  - includes all Task 7 command forms, concrete-question and exact-path rules;
  - keeps the advisor read-only for source and write-limited to exact
    `writable_docs`, with no tests, builds, deployments, or real operations;
  - requires one `DONE`, `BLOCKED`, `CHECKPOINT_NEEDED`, or `COMPACT_READY`
    reply before returning to wait;
  - documents Esc/Ctrl+C interruption, manual-only `/compact`, and the
    150k-200k and over-200k safe-checkpoint behavior.
- `src/ai_room/install.py`
  - exposes `build_install_plan` and `execute_install`;
  - uses one preflighted ordered operation path with recording and atomic
    filesystem writer adapters;
  - installs byte-identical skill files for Codex and Claude;
  - builds the exact SessionStart hook command with
    `subprocess.list2cmdline`;
  - preserves unrelated Claude settings and hooks, and does not duplicate the
    exact ai-room hook;
  - refuses invalid JSON, non-file destinations, duplicate/conflicting
    ai-room hooks, and existing backup destinations before writing;
  - writes a timestamped sibling backup before changing existing settings and
    replaces every changed file through a sibling temporary file plus
    `os.replace`;
  - keeps installation separate from the public six-command runtime CLI.
- `tests/test_install.py`
  - covers the required shared-skill contract, no-write check, apply, repeated
    apply, settings/hook preservation, invalid JSON, non-object JSON, atomic
    failure cleanup, backup ordering/content, spaces in paths, source hashes,
    preview/apply operation equality, non-file destinations, hook conflict,
    and isolated CLI check.

## Verification

- Fresh bounded full suite:
  `python -m pytest -vv -o faulthandler_timeout=30` passed
  `202 passed in 51.28s`.
- `python -m compileall -q src\ai_room tests\test_install.py` exited 0.
- `git diff --check` exited 0.
- `python -m ai_room.install --check` ran with both `HOME` and `USERPROFILE`
  set to a new `%TEMP%` directory, exited 0, reported exactly three planned
  writes, and left that isolated home with `0` items.
- No `--apply` was run against the real or isolated command-smoke home.

## Scope, cleanup, and identity

- No real user settings or home-directory skill files were changed.
- No Task 9 work, runtime CLI command, advisor source permission, or automatic
  compaction behavior was added.
- Eight task-owned `%TEMP%\ai-room-task8-*` verification directories were
  removed after their resolved paths were checked to remain under `%TEMP%`;
  no matching task directory remains.
- Commit identity is
  `patrick1099 <245735497+patrick1099@users.noreply.github.com>`.
- No blocking concerns remain.
