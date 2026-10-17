"""Which managed-venv trees a running process is using, for the update engine's prune.

The shadow-venv engine (:mod:`kiro_crew.platform.wheel_engine`) builds each
version as its own tree (``crew-venv-<version>``) and later prunes superseded
ones. A tree may be deleted only when no process runs from it, and that is
proved with a lock rather than guessed: every process started from an
engine-built tree holds a SHARED ``flock`` on the tree's :data:`LIVENESS_LOCK`
for its lifetime (:func:`hold_running_tree_lock`, called at CLI process entry
except for the gateway, which takes it off-loop after readiness), and the prune
deletes a tree only after taking that lock EXCLUSIVELY.

Standard library and ``platform_compat`` only: CLI entry and the gateway's
post-readiness hold must cost one ``stat`` when the process does not run
from an engine-built tree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from kiro_crew.platform_compat import IS_POSIX, try_acquire_lock

#: Written by the engine into every tree it builds, naming the layout (the
#: legacy venv path) that owns it. Only a tree carrying it, for this layout, is
#: ever swept or pruned, and only such a tree takes the liveness hold below.
TREE_MARKER = ".kirocrew-tree"

#: The file every process running from an engine-built tree holds a shared lock
#: on. Created by the engine at build time, so entry only ever opens it.
LIVENESS_LOCK = ".kirocrew-live.lock"

#: This process's hold; open for the process lifetime and released by the
#: kernel on exit or exec (the descriptor is close-on-exec).
_HELD_FD: int | None = None


def running_tree() -> Path | None:
    """The engine-built tree this interpreter runs from, or ``None``.

    Read from ``sys.prefix`` (the venv root), resolved, so a process started
    through the stable link still names the tree it actually loads from.
    """
    try:
        tree = Path(os.path.realpath(sys.prefix))
        return tree if (tree / TREE_MARKER).is_file() else None
    except OSError:
        return None


def hold_running_tree_lock() -> None:
    """Mark the tree this process runs from as in use, until the process ends.

    A no-op off an engine-built tree, on a second call, and off POSIX (the
    managed venv is a POSIX install). Best effort: a lock that cannot be taken
    leaves the tree unmarked, which is reported by nothing and protected by the
    prune's other rules (the stable target and the previous tree are kept).
    """
    global _HELD_FD
    if _HELD_FD is not None or not IS_POSIX:
        return
    tree = running_tree()
    if tree is None:
        return
    try:
        fd = os.open(str(tree / LIVENESS_LOCK), os.O_RDWR)
    except OSError:
        return
    if try_acquire_lock(fd, exclusive=False):
        _HELD_FD = fd
    else:
        os.close(fd)


def through_stable_link(path: str) -> str:
    """*path*, rewritten through ``crew-venv-current`` when it lies in an engine tree.

    For a launch path that is PERSISTED and executed later, outside this
    process's lifetime (a service ``ExecStart``, a launchd launcher, the
    ``~/.local/bin`` shim), which must follow every promotion rather than name a
    tree a later prune may delete. Never for a command this process hands its
    own children (an MCP server command, a jail re-exec): those must run this
    process's version, and its tree is kept while it holds the liveness lock. Costs a
    few ``stat`` calls for any other path; the engine is imported only for a
    path inside an engine-built tree (see
    :func:`kiro_crew.platform.wheel_engine.stable_launch_path`).
    """
    try:
        resolved = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return path
    for tree in resolved.parents:
        try:
            marked = (tree / TREE_MARKER).is_file()
        except OSError:
            return path
        if marked:
            from kiro_crew.platform.wheel_engine import stable_launch_path

            return stable_launch_path(path)
    return path


__all__ = [
    "LIVENESS_LOCK",
    "TREE_MARKER",
    "hold_running_tree_lock",
    "running_tree",
    "through_stable_link",
]
