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

## Independent-review safety follow-up

- Review finding 1 was verified: an existing differing skill was treated as a
  planned write. RED was `1 failed` with `DID NOT RAISE
  InstallConflictError`. The installer now accepts an existing skill only when
  its bytes equal the repository source; divergent user content is refused
  during preparation. Focused GREEN was `1 passed in 0.09s`.
- Review finding 2 was verified across the supplied home and every `.codex`
  and `.claude` skill ancestor. RED was `7 failed`: raw
  `FileExistsError` escaped, and a blocked Claude ancestor could fail after a
  Codex write. The writer path state now distinguishes directories, and
  preparation validates every existing target ancestor, including the
  settings backup path when relevant, before returning any write operations.
  Focused GREEN was `7 passed in 0.14s`.
- A dedicated backup-path regression replaces the planned backup path with a
  child of a user-owned file and confirms preparation refuses it before either
  skill or settings is written.
- The spaces-only regression now uses both Unicode and spaces in the home and
  Python executable paths. It verifies the exact `list2cmdline` hook command,
  successful installation, both byte-identical skill files, and both source
  SHA-256 values.
- The atomic-failure regression now reaches the permitted existing-settings
  update path: exact installed skills remain unchanged, the settings backup
  succeeds, the settings replace fails, the original settings bytes remain,
  and the failed temporary sibling is removed. It no longer relies on
  overwriting a divergent skill, which is intentionally forbidden.
- Final focused installer suite: `25 passed in 0.46s`.
- Final bounded full suite:
  `python -m pytest -vv -o faulthandler_timeout=30` passed
  `211 passed in 46.96s`.
- `python -m compileall -q src\ai_room tests\test_install.py` and
  `git diff --check` exited 0.
- Eleven review-owned `%TEMP%\ai-room-task8-review-*` directories were removed
  after their resolved paths were checked to remain under `%TEMP%`; none
  remains.
- No real-home or isolated-home `--apply` was run. No Task 9 work or push was
  performed.

## Final-review Windows and null-configuration follow-up

- Present `null` review finding was verified for both `hooks` and
  `hooks.SessionStart`. RED was `2 failed`, each with `DID NOT RAISE
  InstallConflictError`. Settings merge now checks key membership separately
  from value type: missing keys are initialized, while present `null` and
  every other incompatible type are refused before writes. The two null and
  two missing-key cases passed together as `4 passed in 0.16s`.
- Windows junction/reparse review finding was verified with a simulated
  `FILE_ATTRIBUTE_REPARSE_POINT` value on an otherwise real `.claude`
  directory. RED was `1 failed` with `DID NOT RAISE InstallConflictError`.
  Path classification now reads optional `st_file_attributes` through a small
  helper, uses a guarded standard-library constant with the Windows `0x400`
  fallback, and rejects reparse outputs or ancestors through the existing
  preflight. This is compatible with Python 3.11 and non-Windows stat results.
  Reparse refusal plus ordinary-directory acceptance passed as
  `2 passed in 0.11s`.
- Final focused installer suite: `31 passed in 0.71s`.
- Final bounded full suite:
  `python -m pytest -vv -o faulthandler_timeout=30` passed
  `217 passed in 46.34s`.
- `python -m compileall -q src\ai_room tests\test_install.py` and
  `git diff --check` exited 0.
- Eight final-review `%TEMP%\ai-room-task8-finalreview-*` directories were
  removed after their resolved paths were checked to stay under `%TEMP%`;
  none remains.
- No real-home or isolated-home `--apply` was run. No Task 9 work or push was
  performed.
