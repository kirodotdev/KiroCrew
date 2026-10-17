#!/usr/bin/env python3
"""Refuse to move a checkout that a running Kiro Crew gateway is using.

Usage: live_checkout_guard.py [PATH]   (PATH defaults to the current directory)

Run it BEFORE any command that moves HEAD or rewrites files in a clone:
rebase, checkout, switch, merge, pull, reset, restore, stash. ``git fetch``
only updates remote refs and is safe anywhere.

How it finds the gateway's checkout. For each ``gateway-<port>.pid`` under
``<KIROCREW_HOME or ~/.kiro/crew>/run`` whose process is alive, it takes:

* the launcher path in ``gateway-<port>.bin`` (``<checkout>/.venv/bin/kirocrew``
  for a source install), and
* the process's working directory (``/proc/<pid>/cwd``, Linux only),

and walks each up to the nearest ``.git``. If PATH is in the same work tree,
the move would swap code under the live gateway: its pooled MCP backends would
load a mix of old and new modules. A linked worktree has its own ``.git`` file,
so it never matches the clone it came from.

Exit codes: 0 safe; 30 refused (PATH is a live gateway's checkout);
2 could not read the run directory (treat as refused).

Stdlib only, and it never imports kiro_crew.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

EXIT_SAFE = 0
EXIT_REFUSED = 30
EXIT_ENV = 2


def _home() -> Path:
    override = os.environ.get("KIROCREW_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".kiro" / "crew"


def work_tree_root(path: Path) -> Path | None:
    """Nearest directory at or above ``path`` holding a ``.git`` entry."""
    try:
        path = path.resolve()
    except OSError:
        return None
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a running process, as far as this host can tell.

    Read from ``/proc/<pid>`` where it exists. Without ``/proc`` (macOS,
    Windows) a recorded pid counts as live, which errs toward refusing.
    """
    if pid <= 0:
        return False
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    return (proc / str(pid)).exists()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def gateway_sources(run_dir: Path) -> list[tuple[int, Path]]:
    """``(pid, path)`` pairs naming where each live gateway's code came from."""
    found: list[tuple[int, Path]] = []
    for pid_file in sorted(run_dir.glob("gateway-*.pid")):
        text = _read_text(pid_file)
        if not text.isdigit():
            continue
        pid = int(text)
        if not _pid_alive(pid):
            continue
        launcher = _read_text(pid_file.with_suffix(".bin"))
        if launcher:
            found.append((pid, Path(launcher)))
        cwd_link = Path(f"/proc/{pid}/cwd")
        try:
            found.append((pid, Path(os.readlink(cwd_link))))
        except OSError:
            pass
    return found


def check(target: Path, run_dir: Path) -> tuple[int, str]:
    """Return ``(exit_code, message)`` for moving ``target``."""
    if not run_dir.is_dir():
        return EXIT_SAFE, f"OK: no gateway run directory at {run_dir}"
    try:
        sources = gateway_sources(run_dir)
    except OSError as exc:
        return EXIT_ENV, f"ERROR: could not read {run_dir}: {exc.__class__.__name__}"
    root = work_tree_root(target)
    if root is None:
        return EXIT_SAFE, f"OK: {target} is not inside a git work tree"
    for pid, source in sources:
        if work_tree_root(source) == root:
            return EXIT_REFUSED, (
                f"REFUSED: {root} is the checkout the running gateway (pid {pid}) "
                f"runs from ({source}). Do not rebase, checkout, switch, merge, pull "
                f"or reset here. Make a worktree instead:\n"
                f"  git -C {root} fetch origin main\n"
                f"  git -C {root} worktree add ../<name> -b <branch> origin/main"
            )
    return EXIT_SAFE, f"OK: no running gateway runs from {root}"


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] in {"-h", "--help"}:
        print(__doc__)
        return EXIT_SAFE
    target = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    code, message = check(target, _home() / "run")
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
