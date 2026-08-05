---
name: ai-room
description: Coordinate one visible Codex session and one visible Claude Code session as primary and document-only advisor through the local ai-room mailbox.
---

# ai-room shared agent contract

Use this skill when Codex and Claude Code are both open in visible interactive
sessions for the same worktree. Either tool may be the primary or the advisor.

## Join and idle behavior

Join with your own identity, never the peer's identity:

```text
ai-room join codex [--room NAME]
ai-room join claude [--room NAME]
```

After joining, and whenever you have no active work, enter:

```text
ai-room wait
```

`wait` is deliberately blocking and silent. The user can press Esc or Ctrl+C
to interrupt it without leaving the room or losing queued messages. After a
formal reply, return to `ai-room wait`.

## When to consult the peer

Use the peer for a material technical decision, a requirements/design/plan
review, or a requested context check. Send one concrete question and every
exact path needed to answer it. Never ask the peer to "review everything".

The Task 7 command forms are:

```text
ai-room wait [--checkpoint EXACT_PATH] [--next-entry TEXT]
ai-room send --to codex|claude --type decision --question TEXT [--related-doc EXACT_PATH] [--checkpoint-doc EXACT_PATH] [--next-entry TEXT] [--idempotency-key KEY]
ai-room send --to codex|claude --type requirements-review|design-review|plan-review --question TEXT --related-doc EXACT_PATH [--related-doc EXACT_PATH] [--writable-doc EXACT_PATH] [--checkpoint-doc EXACT_PATH] [--next-entry TEXT] [--idempotency-key KEY]
ai-room send --to codex|claude --type context-check --question TEXT [--related-doc EXACT_PATH] [--checkpoint-doc EXACT_PATH] [--next-entry TEXT] [--idempotency-key KEY]
ai-room reply TASK_ID --outcome done|blocked|compact-ready|checkpoint-needed --message TEXT
ai-room status
ai-room leave
```

Only document reviews may carry `--writable-doc`. Use repository-relative,
exact file paths for `related_docs`, `writable_docs`, and checkpoint documents;
never send a directory, glob, or vague path.

## Headless sub-agent dispatch (`ask`)

`ask` runs one vendor CLI as a one-shot sub-agent without a visible session or
mailbox. It records the dispatch and the sub-agent session id in
`<root>/.ai-room/ledger.md` so the turn can be resumed later.

```text
ai-room ask --to claude|codex|opencode --question TEXT [--permission read-only|workspace-write|full-access] [--related-doc EXACT_PATH] [--model MODEL] [--cwd DIR] [--timeout SECONDS] [--permission-mode MODE] [--sandbox MODE] [--no-ledger]
```

- `--permission` is the tier, and it defaults to `read-only`. `workspace-write`
  lets the sub-agent edit files and run commands in the working directory;
  `full-access` lifts the sandbox. A dispatched sub-agent is a worker, not an
  advisor: at `workspace-write` it may run tests and builds.
- `--cwd` selects which project to dispatch against, and it is pinned with each
  vendor's own flag. Do not rely on the shell's current directory.
- Tiers are set by `ask` itself and never inherited from the machine's vendor
  config, because the same flags mean different things under different configs.
  One caveat worth knowing: on codex, `workspace-write` permits a sandbox
  refusal to be escalated into a run outside the sandbox. Without that a write
  can land as a file the caller cannot read. So `workspace-write` on codex is
  slightly wider than "confined to the working directory".
- Exit code 0 is the vendor's verdict: the sub-agent succeeded. Failures and
  timeouts exit 3. Files the sub-agent changed are reported back in
  `changed_files` and in the ledger as a receipt for review -- they never
  change the exit code, because a dispatched job has no allow-list to violate.
- The sub-agent session id is written to the ledger for resuming
  (`claude -r ID`, `codex exec resume ID`, `opencode run --session ID`).

### `ask` blocks. Give it enough time.

`ask` is synchronous and returns only when the sub-agent is done. A question
takes tens of seconds; a real task takes minutes. **Your own shell tool's
timeout is what kills it**, and the three of you have wildly different defaults:

| you are | default shell timeout | what you must do |
|---|---|---|
| claude | ~600s | pass a larger `timeout` on the Bash call for long dispatches |
| opencode | 120s | raise the global default with `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` |
| codex | **10s** | you must pass `timeout_ms` explicitly on every call, or even a plain question is killed |

If you need to keep working while it runs, do not expect `ask` to detach --
it will not. Start your own subagent and have it make the blocking call.
Delegation is not free: a trivial task costs two to three minutes of overhead,
so dispatch a consultation synchronously and only delegate real work.

The change receipt is not a sandbox. It reports what git sees in the
sub-agent's working directory, so it misses gitignored paths, and it cannot
tell you the job ran in the wrong project -- if the sub-agent worked somewhere
else, the receipt is simply clean. Only the pinned working directory prevents
that.

## Advisor boundary

As advisor, read source when it is needed to make the requested decision or
review. You may write only the exact files listed in `writable_docs`. You must
never edit source code. You must never run tests, builds, deployments, or real operations
for the primary. A read-only review may have no writable document.

Answer the concrete question once with exactly one outcome:

- `DONE`: the decision or permitted document work is complete.
- `BLOCKED`: a user choice, requirement resolution, or unavailable fact is
  required.
- `CHECKPOINT_NEEDED`: the exact checkpoint record is incomplete.
- `COMPACT_READY`: the checkpoint is complete and manual compaction is safe.

After replying, return to `ai-room wait`. Do not request another full review
after `DONE` unless a new concrete issue exists.

## Safe compaction

Compaction is always manual. Never execute `/compact`; only tell the user to
run it after the peer returns `COMPACT_READY`.

- Below 150k input tokens, continue normally.
- From 150k through 200k, ask the peer to judge the next safe node. Include
  exact checkpoint documents and the next recovery entry.
- Above 200k, finish or explicitly pause only the smallest current work unit.
  Record decisions, changes, verification, open items, and the next entry in
  the exact checkpoint documents, then prioritize the peer context check.
- Never interrupt an in-flight write, build, test, or diagnosis merely to
  compact.

If the peer returns `CHECKPOINT_NEEDED`, update only the stated checkpoint
documents and resume the same check with:

```text
ai-room wait --checkpoint EXACT_PATH [--checkpoint EXACT_PATH] --next-entry TEXT
```

When `COMPACT_READY` arrives, tell the user which tool/session is ready and ask
the user to run `/compact` manually. After compaction, resume from the recorded
next entry and return to `ai-room wait` when idle.
