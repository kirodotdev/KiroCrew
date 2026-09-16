"""One shared answer to "did a ``--worktree`` config probe fail on the empty scope?".

``extensions.worktreeConfig=true`` makes git load ``$GIT_DIR/config.worktree``
in addition to ``.git/config``. Git creates that file lazily, so
extension-on-file-absent is a normal healthy state git itself treats as an
EMPTY worktree scope — yet probing it exits 128 ("unable to read config
file"), which a fail-closed filter-driver guard would misread as "cannot be
proven filter-free" and refuse a filter-free repo.

The order of operations here is the contract, not an implementation detail.
The guards PROBE FIRST and classify the failure AFTERWARDS — the same
use-then-verify discipline as :mod:`kiro_crew.hooks` and
:mod:`kiro_crew.pinned_fs`, where a name-based pre-check is never allowed to
stand in for the artifact actually used. A pre-check that dropped the scope
from the probe set would decide from a stale fact: a ``config.worktree``
written after the check would be read by git while the guard never looked at
it. Probe-first means every scope git can read is listed for drivers, and this
classifier is consulted only for a scope git itself just refused to read.

Four guards share this decision (``dashboard/handlers/worktree.py``,
``dashboard/handlers/files.py``, ``platform/update_governance.py``,
``apps/builtins/md_notebook/git_ops.py``), each through its own git runner and
environment. The classification lives here exactly once; each caller runs
``git rev-parse --absolute-git-dir`` on its failure path and feeds the result
in.
"""

from __future__ import annotations

import os

from kiro_crew import platform_compat

__all__ = ["worktree_probe_failure_is_empty_scope"]


def worktree_probe_failure_is_empty_scope(gitdir: str, base: str) -> bool:
    """True when a failed ``--worktree`` config probe hit the healthy empty scope.

    Called ONLY after ``git config --worktree ...`` exited non-zero. It must
    never gate whether the probe runs — see the module docstring for why the
    probe-first order is load-bearing.

    ``gitdir`` is the RAW stdout of ``git rev-parse --absolute-git-dir`` on
    success, or ``""`` when that probe failed. ``base`` anchors a relative
    ``gitdir``. Only git's own terminating newline is removed here: a path
    that genuinely begins or ends with whitespace must survive intact, or the
    ``lstat`` below inspects a DIFFERENT path than git reads and a wrong
    ``FileNotFoundError`` clears a scope that has a config file. Callers must
    pass stdout unstripped for the same reason.

    Only one state reads as the empty scope: ``config.worktree`` is genuinely
    ABSENT (``lstat`` says no entry) — the state git creates the file lazily
    for, where the probe's failure carries no information about repo content.
    Every other state fails closed (``False``), keeping the caller's refusal:
    a present entry of any kind — a regular file the probe found garbled, a
    symlink, a FIFO, a socket — and an ``lstat`` that errors for any other
    reason (permissions, IO). ``os.path.isfile`` would instead report a
    non-regular entry as absent and clear a scope git still reads.
    ``--absolute-git-dir`` (never ``--git-common-dir``): ``$GIT_DIR`` is per
    worktree, so a linked worktree's own ``config.worktree`` lives under
    ``$GIT_COMMON_DIR/worktrees/<id>``. An empty ``gitdir`` fails closed for
    the same reason — absence cannot be confirmed.

    The ``os.lstat`` here is a blocking stat: an async caller must run this
    function off the event loop (``asyncio.to_thread``).
    """
    path = gitdir
    # Exactly one line terminator, never path whitespace: .strip() would
    # rewrite a whitespace-bearing git dir into a different path and lstat
    # the wrong location. The CR is removed only on Windows, where a
    # text-mode pipe delivers git's terminator as CRLF; on POSIX git ends
    # the line with a bare LF, so a preceding CR is part of the real path
    # and removing it would misdirect the lstat the same way .strip() did.
    if path.endswith("\n"):
        path = path[:-1]
    if not platform_compat.IS_POSIX and path.endswith("\r"):
        path = path[:-1]
    if not path:
        return False
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    target = os.path.join(path, "config.worktree")
    try:
        os.lstat(target)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False
