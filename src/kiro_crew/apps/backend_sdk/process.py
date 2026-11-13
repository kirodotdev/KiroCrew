"""Cross-platform process, link and lock helpers (``kiro_crew.platform_compat``).

Use these instead of the POSIX calls they replace: ``fcntl`` does not exist on
Windows, and there ``os.kill(pid, 0)`` terminates the target instead of probing it.
"""

from __future__ import annotations

import os
from asyncio.subprocess import Process
from contextlib import AbstractContextManager

from kiro_crew import platform_compat

IS_WINDOWS: bool = platform_compat.IS_WINDOWS
IS_MACOS: bool = platform_compat.IS_MACOS
IS_LINUX: bool = platform_compat.IS_LINUX
IS_POSIX: bool = platform_compat.IS_POSIX


async def kill_and_reap(proc: Process, *, timeout: float | None = None) -> None:
    """Kill *proc* and its descendants, then wait for it under a bound.

    The cleanup for a child whose ``communicate()`` was abandoned on a timeout or a
    cancellation. On POSIX the descendants are reached through the child's own
    process group, so spawn it with ``start_new_session=True``; once the child has
    exited and been reaped its group is not signalled (its pid may have been
    reused), so a descendant that outlived it keeps running. Windows ends the
    child's whole tree.
    """
    await platform_compat.kill_and_reap(proc, timeout=timeout)


def is_link_or_junction(path: str | os.PathLike[str]) -> bool:
    """Whether *path* itself is a symlink, or a junction on Windows."""
    return bool(platform_compat.is_link_or_junction(path))


def first_linked_ancestor(path: str | os.PathLike[str]) -> str | None:
    """The first ANCESTOR of *path*, tested from the root down, that is a link, or ``None``.

    The leaf itself is not tested: pair this with :func:`is_link_or_junction`.
    """
    return platform_compat.first_linked_ancestor(path)


def rmtree_force(path: str | os.PathLike[str]) -> bool:
    """Remove the tree at *path*, read-only entries included. Never raises.

    ``True`` when *path* is gone afterwards, judged from the filesystem.
    """
    return bool(platform_compat.rmtree_force(path))


def file_lock(
    fd: int,
    *,
    exclusive: bool = True,
    required: bool = False,
    wait: bool = True,
    timeout: float | None = None,
) -> AbstractContextManager[None]:
    """An advisory lock on the open descriptor *fd* for the duration of a ``with`` block.

    ``flock`` on POSIX and ``msvcrt.locking`` on Windows (a shared request takes
    an exclusive lock there). It fails closed: when the lock cannot be taken within
    *timeout* (the gateway's ceiling when ``None``), it raises instead of entering
    the block unlocked, and on the event-loop thread a contended lock is refused at
    once, so call it from a worker thread when it must wait.
    """
    lock: AbstractContextManager[None] = platform_compat.file_lock(
        fd, exclusive=exclusive, required=required, wait=wait, timeout=timeout
    )
    return lock


__all__ = [
    "IS_LINUX",
    "IS_MACOS",
    "IS_POSIX",
    "IS_WINDOWS",
    "file_lock",
    "first_linked_ancestor",
    "is_link_or_junction",
    "kill_and_reap",
    "rmtree_force",
]
