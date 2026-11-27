"""One refresh pass's answers from git about a checkout, each asked once.

A pass asks the same read-only questions in several steps: the PR lookup, both
context bars, the Code tab and the worker rows each want the checkout's branch,
its ``origin`` URL or whether the branch is pushed. Inside :func:`reading`
those steps ask through :func:`current_branch`, :func:`branch_in_thread`,
:func:`remote_url` and :func:`has_remote_branch`, which ask git the first time
and answer the rest from the pass's :class:`CheckoutReads`; outside it they ask
git every time. A refresh pass pushes nothing and creates no PR, so nothing in
it moves the remote ref the pushed answer comes from.

The branch is reused only while the checkout's ``HEAD`` file is the one it was
read from (:func:`_head_stamp`: a switch, a rename or a commit on a detached
HEAD rewrites that file), so every step shows the branch as it is when the step
runs, as a read of its own would.

Nothing outlives the pass: the reads close when :func:`reading` exits, so the
next operation asks git again. Only the task that opened the pass answers from
them, and a worker thread only when that task hands them over (:func:`lend`),
so a task the pass starts (which copies the context) never does. The reads
ride a context variable, not an argument, because the steps are the service's
overridable methods (``_refresh_pr``, ``_bar_state``, ``_fill_worker_prs``,
``_build_diff``), whose signatures stay as they are.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypeVar

from kiro_crew.slack.code_channels.workspace import git_ops

#: What a branch switch changes about the ``HEAD`` file: identity, size, times.
_Stamp = tuple[int, int, int, int]
_T = TypeVar("_T")


@dataclass
class CheckoutReads:
    """What git answered in this pass, per checkout folder."""

    task: asyncio.Task[Any] | None
    closed: bool = False
    branch: dict[str, tuple[_Stamp, str]] = field(default_factory=dict)
    remote: dict[str, str] = field(default_factory=dict)
    pushed: dict[tuple[str, str], bool] = field(default_factory=dict)


_READS: ContextVar[CheckoutReads | None] = ContextVar("code_channel_checkout_reads", default=None)
#: The reads a pass lent to the worker thread running one of its steps.
_LENT: ContextVar[CheckoutReads | None] = ContextVar("code_channel_lent_reads", default=None)


@contextlib.contextmanager
def reading() -> Iterator[CheckoutReads]:
    """Share git's answers among the steps of the pass run inside it."""
    reads = CheckoutReads(asyncio.current_task())
    token = _READS.set(reads)
    try:
        yield reads
    finally:
        reads.closed = True
        _READS.reset(token)


def _ours() -> CheckoutReads | None:
    reads = _READS.get()
    if reads is None or reads.closed or reads.task is not asyncio.current_task():
        return None
    return reads


def lend(fn: Callable[..., _T]) -> Callable[..., _T]:
    """*fn* for ``asyncio.to_thread``, answering from this task's pass in that thread."""
    reads = _ours()

    def run(*args: Any, **kwargs: Any) -> _T:
        _LENT.set(reads)  # the thread's own copy of the context
        return fn(*args, **kwargs)

    return run


def _head_stamp(cwd: str) -> _Stamp | None:
    """The checkout's ``HEAD`` file as stat sees it, ``None`` when it cannot be found.

    A linked worktree's ``.git`` is a file naming its git dir, which the agent
    can rewrite: it is read through :func:`safe_read_file` (sensitive paths
    refused through any link, a regular file only, opened non-blocking).
    """
    from kiro_crew.hooks import safe_read_file

    dot_git = os.path.join(cwd, ".git")
    try:
        if os.path.isdir(dot_git):
            gitdir = dot_git
        else:
            line = safe_read_file(dot_git).strip()
            if not line.startswith("gitdir:"):
                return None
            gitdir = os.path.join(cwd, line[len("gitdir:") :].strip())
        found = os.stat(os.path.join(gitdir, "HEAD"))
    except (OSError, ValueError):
        return None
    return found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns


def _branch(cwd: str, reads: CheckoutReads | None) -> str:
    """Blocking: the branch, from *reads* while ``HEAD`` is unchanged, else from git."""
    if reads is None or reads.closed:
        return git_ops.current_branch(cwd)
    # Stamped BEFORE git is asked: a switch between the two shows as a change.
    now = _head_stamp(cwd)
    cached = reads.branch.get(cwd)
    if now is not None and cached is not None and cached[0] == now:
        return cached[1]
    branch = git_ops.current_branch(cwd)
    if now is not None:
        reads.branch[cwd] = (now, branch)
    return branch


async def current_branch(cwd: str) -> str:
    return await asyncio.to_thread(_branch, cwd, _ours())


def branch_in_thread(cwd: str) -> str:
    """Blocking :func:`current_branch`, for a step a pass lent its reads to."""
    return _branch(cwd, _LENT.get())


async def remote_url(cwd: str) -> str:
    reads = _ours()
    if reads is not None and cwd in reads.remote:
        return reads.remote[cwd]
    url = await asyncio.to_thread(git_ops.remote_url, cwd)
    if reads is not None:
        reads.remote[cwd] = url
    return url


async def has_remote_branch(cwd: str, branch: str) -> bool:
    reads = _ours()
    if reads is not None and (cwd, branch) in reads.pushed:
        return reads.pushed[(cwd, branch)]
    pushed = await asyncio.to_thread(git_ops.has_remote_branch, cwd, branch)
    if reads is not None:
        reads.pushed[(cwd, branch)] = pushed
    return pushed
