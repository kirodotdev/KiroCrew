"""Reopening the task store's file as the next incarnation.

Only public functions are called: ``TaskStore`` and its methods. The clock is
injected everywhere, in this process and in the child, so no result depends on
wall time.
"""

from __future__ import annotations

from pathlib import Path

from overload_fakes import Clock
from overload_fakes import task_record as record

from kiro_crew.taskq.store import TaskStore

#: The virtual time a child writes with. The parent reopens at a later instant
#: so every lease and backoff the dead child left has expired.
BASE_CLOCK = 5000.0


def reopen_store(directory: Path, now: float, *, clock: Clock | None = None) -> TaskStore:
    """A new incarnation over the same file, as a restarted gateway sees it.

    A test that needs to move time afterwards passes its own ``clock``; the
    store reads it, so advancing it is the only way time passes.
    """
    return TaskStore(
        directory / "tasks.db", clock=clock if clock is not None else Clock(now), network_fs=False
    ).open()


__all__ = ["BASE_CLOCK", "record", "reopen_store"]
