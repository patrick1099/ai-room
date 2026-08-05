"""What the sub-agent actually changed, reported back to the caller.

This is deliberately *not* the advisor's workspace guard.  The guard exists to
enforce a boundary: the advisor was told exactly which documents it may edit,
so anything else it touches is a violation and fails the task.  A dispatched
sub-agent has no such list -- when you hand out a job you do not know in
advance which files it will need -- so there is nothing to enforce and nothing
to fail.

What the caller does need is a receipt: a plain statement of which files moved,
so it can review the work and decide what to do next.  It never affects the
exit code.

Two honest limits, because a receipt that quietly under-reports is worse than
no receipt:

- It only sees what git sees.  A write into a gitignored path does not appear.
- **It cannot catch work done in the wrong project.**  If the sub-agent ran
  somewhere else entirely -- which ``opencode run`` does whenever ``--dir`` is
  missing -- this reports a clean tree for a directory nothing happened in.
  The working directory has to be pinned with the vendor's own flag; a receipt
  cannot be a substitute for that.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_GIT_TIMEOUT = 15.0


def status_lines(cwd: Path) -> frozenset[str] | None:
    """Return ``git status --porcelain`` lines for ``cwd``.

    Returns ``None`` when the directory is not a git work tree or git could not
    be run.  ``None`` means "unknown", never "nothing changed" -- the caller
    must report the difference rather than imply a clean tree.
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return frozenset(
        line.rstrip() for line in (completed.stdout or "").splitlines() if line.strip()
    )


def changed_since(
    before: frozenset[str] | None,
    after: frozenset[str] | None,
) -> tuple[str, ...]:
    """Return the porcelain lines that appeared or changed between two snapshots.

    Comparing whole lines rather than paths means a file whose status changed
    (staged, then modified again) is reported too, not just newly seen paths.
    """
    if before is None or after is None:
        return ()
    return tuple(sorted(after - before))
