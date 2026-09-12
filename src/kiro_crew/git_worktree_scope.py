"""One shared answer to "is the ``--worktree`` git-config scope live here?".

``extensions.worktreeConfig=true`` makes git load ``$GIT_DIR/config.worktree``
in addition to ``.git/config``. Git creates that file lazily, so
extension-on-file-absent is a normal healthy state git itself treats as an
EMPTY worktree scope — probing it anyway exits 128 ("unable to read config
file"), which a fail-closed filter-driver guard would misread as "cannot be
proven filter-free" and refuse a filter-free repo.

Four guards make this decision (``dashboard/handlers/worktree.py``,
``dashboard/handlers/files.py``, ``platform/update_governance.py``,
``apps/builtins/md_notebook/git_ops.py``), each through its own git runner and
environment. The decision logic lives here exactly once; each caller gates on
``extensions.worktreeConfig`` first, then runs
``git rev-parse --absolute-git-dir`` and feeds the result in.
"""

from __future__ import annotations

import os

__all__ = ["worktree_scope_active"]


def worktree_scope_active(gitdir: str, base: str) -> bool:
    """True when the ``--worktree`` config scope must be probed.

    Callers check ``extensions.worktreeConfig`` themselves before calling —
    without the extension git ignores the file and the scope is never live.
    ``gitdir`` is the stdout of ``git rev-parse --absolute-git-dir`` on
    success, or ``""`` when that probe failed. ``base`` anchors a relative
    ``gitdir``.

    Only one state reads as inactive: ``config.worktree`` is genuinely
    ABSENT (``lstat`` says no entry), the empty scope git creates the file
    lazily for. Every other state keeps the scope in, so the caller's probe
    runs and fails closed: a present entry of any kind — a regular file, a
    symlink, a FIFO, a socket — and an ``lstat`` that errors for any other
    reason (permissions, IO). ``os.path.isfile`` would instead report a
    non-regular entry as inactive and silently drop the scope from the
    filter-driver probe while git still reads it. ``--absolute-git-dir``
    (never ``--git-common-dir``): ``$GIT_DIR`` is per worktree, so a linked
    worktree's own ``config.worktree`` lives under
    ``$GIT_COMMON_DIR/worktrees/<id>``. An empty ``gitdir`` keeps the scope
    in for the same fail-closed reason.

    The ``os.lstat`` here is a blocking stat: an async caller must run this
    function off the event loop (``asyncio.to_thread``).
    """
    path = gitdir.strip()
    if not path:
        return True
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    target = os.path.join(path, "config.worktree")
    try:
        os.lstat(target)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True
