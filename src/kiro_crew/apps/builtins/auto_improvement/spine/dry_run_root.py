"""Remove the throwaway run root a spine ``--dry-run`` builds (``driver._run_dry``).

The run root holds the stub git clone, its worktrees and, without ``--data-dir``, the
run's archive, ledger and PR queue. It goes when the run ends, on success and on error,
except for a clone the run retired as unsafe, which stays where its log line points.
"""

from __future__ import annotations

import os
from pathlib import Path


def remove_dry_root(tmp: Path, clone: Path) -> None:
    """Remove the run root *tmp*, keeping only the container *clone* was retired into.

    ``rmtree_force`` because the root holds a git clone: on Windows its read-only loose
    objects refuse to unlink and ``shutil.rmtree(..., ignore_errors=True)`` reports
    success over a tree that is still there. ``_retire_unsafe_clone`` moves *clone* into
    ``<parent>/.<clone name>.unsafe-<hex>/`` and the run logs that path as "clone retained
    at <path>" for inspection, so that container stays and is named here. A partial
    removal prints the path left behind.
    """
    from kiro_crew.platform_compat import rmtree_force

    retired_prefix = f".{clone.name}.unsafe-"
    try:
        entries = list(tmp.iterdir())
    except OSError:
        entries = []
    kept = [
        e
        for e in entries
        if e.name.startswith(retired_prefix) and e.is_dir() and not e.is_symlink()
    ]
    data = tmp / "data"
    had_data = data.is_dir()
    if not kept:
        if not rmtree_force(tmp):
            print(f"  run root not fully removed: {tmp}")
    else:
        for entry in entries:
            if entry in kept:
                continue
            if entry.is_dir() and not entry.is_symlink():
                gone = rmtree_force(entry)
            else:
                try:
                    entry.unlink()
                except OSError:
                    pass
                gone = not os.path.lexists(entry)
            if not gone:
                print(f"  run root not fully removed: {entry}")
        for entry in kept:
            print(f"  kept the clone retired as unsafe: {entry}")
    if had_data and not os.path.lexists(data):
        print(f"  removed the throwaway data under {data} (pass --data-dir to keep it)")
