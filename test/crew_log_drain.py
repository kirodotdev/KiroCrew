"""Drain barriers for tests that write through the crew-log emitter.

An emitter entry point called on an event loop returns once its work is HANDED to the
single ``mc-crewlog`` writer thread (off a loop it writes inline only when that session
owes nothing), so a test that reads what it just wrote needs a barrier first. The
obvious one, ``assert emit.flush(timeout=N)``, is a RATE assertion: every append behind
it is an ``fsync`` under a cross-process lock, 0.4 ms on a warm Linux host and over a
second on a loaded Windows runner (``test_flush_returns_only_when_buffer_empty``'s 52
appends took 58.9 s on Windows shard 7 of run 37016327909), so no constant N covers the
work on every host. :func:`assert_drained` gives up on a writer that STOPPED rather than
on one that is slow: it waits in windows and fails when a whole window lands nothing
new. Its other way out is a lost-run ceiling under the module's ``--timeout``, which
bounds the RUN rather than fitting the batch: a batch slower than that still fails
there, by name, instead of costing the worker.

Progress is read as the SIZE of the session-log segments under the current data home,
the only kind the queued writer appends to, never as the buffer count: the writer takes
a batch OUT of the buffer before it writes it, so an empty buffer is what a wedged
writer and a finished one both show.

:func:`settle` is the same barrier for the gap BETWEEN tests. A writer job resolves the
data home when it RUNS, not when it was queued, so a batch still in flight when a
test's ``KIROCREW_HOME`` pin lifts lands in whatever home the next test pins. That is
how one test's leftover ``tool/called`` entries became the next test's "thread 3:
expected 15, got 16" on that same shard, right after its teardown warned ``batch in
flight=True``. Called before the pin lifts, it fails the test that queued the work
instead of the test that would have inherited it.

:func:`unsync_appends` takes the fsync out of each append for a test whose subject is
the records rather than their durability, which is most of what makes a batch slow in
the first place.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.crew_log import KIND_SESSION, crew_log_root, emit, store

#: How long the writer may land nothing before the barrier reports it stuck. Not a
#: budget for the drain: a writer that keeps landing bytes keeps the barrier waiting,
#: however long the batch takes. The window has to outlast the slowest SINGLE append.
#: What was measured is a MEAN: 1.13 s per append across a 52-append batch on the shard
#: above, which this window exceeds about 13 times; one append on that shard can take
#: longer than the mean, so the true margin is smaller than that.
NO_PROGRESS_SECONDS = 15.0

#: Total ceiling for one barrier inside a test, half of the suite's ``--timeout=120``
#: in setup.cfg. A writer that trickles forever fails here as a readable assertion
#: instead of reaching that mark, where pytest-timeout kills the xdist worker and costs
#: the whole RUN rather than one test. The bound is per BARRIER: a test that runs several
#: long barriers plus :func:`settle` has to keep their sum under the 120 s itself.
DRAIN_CEILING_SECONDS = 60.0

#: Ceiling for :func:`settle`. pytest-timeout counts setup, call and teardown together
#: (``timeout_func_only`` is off), so a teardown after a body that already spent
#: :data:`DRAIN_CEILING_SECONDS` has only the other half of the budget, less that
#: body's own work.
SETTLE_CEILING_SECONDS = 30.0


def assert_drained(*, ceiling: float = DRAIN_CEILING_SECONDS) -> None:
    """Return once the writer is quiet, or raise naming the logs it was writing.

    Quiet is what ``emit.flush`` waits for: nothing queued and no batch in flight. The
    first window is measured from BEFORE the first wait, so a wedged writer is reported
    one window in, as promptly as a fixed ceiling of the same size would report it.

    Blocks the calling thread. An async test usually calls it through
    ``asyncio.to_thread`` so the event loop keeps running while the writer works; one
    whose code under test leaves tasks that would write into the record calls it
    directly, holding the loop as a plain ``emit.flush`` did.
    """
    started = time.monotonic()
    landed = _segment_sizes()
    while True:
        remaining = max(0.0, started + ceiling - time.monotonic())
        window = min(NO_PROGRESS_SECONDS, remaining)
        if emit.flush(timeout=window):
            return
        written = _segment_sizes()
        elapsed = time.monotonic() - started
        # Decided by the window, not by reading the clock again: a coarse clock (15.6 ms
        # on Windows through 3.12) can read a wait that ran to the ceiling as just short
        # of it, which would report a slow writer as one that landed nothing.
        if window >= remaining:
            raise AssertionError(_stuck(f"was still writing at {ceiling:.1f}s", elapsed, written))
        if not _grew(landed, written):
            raise AssertionError(_stuck(f"landed nothing for {window:.1f}s", elapsed, written))
        landed = written


def settle() -> None:
    """Wait out the writer under THIS test's data home, then forget its handles.

    Call it in teardown while the test's ``KIROCREW_HOME`` pin still holds. The reset
    runs even when the wait fails: it discards whatever is still queued, though not a
    batch the writer has already claimed, which is why the wait comes first.
    """
    try:
        assert_drained(ceiling=SETTLE_CEILING_SECONDS)
    finally:
        emit.reset_caches()


class _OsWithoutFsync:
    """The ``os`` the store module sees under :func:`unsync_appends`: the real one, no fsync.

    Installed onto ``store`` alone, so every other module keeps the real ``os``; the
    header's own atomic publish keeps its fsync too, because it goes through
    ``atomic_write``.
    """

    @staticmethod
    def fsync(fd: int) -> None:
        return None

    def __getattr__(self, attr: str) -> Any:
        return getattr(os, attr)


def unsync_appends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the fsync out of every session-log append for the rest of this test.

    For a test that pins WHAT the emitter records -- order, no loss, which model or
    turn an entry names -- rather than that it is durable. Each append is an fsync under
    a cross-process lock, which averaged 1.13 s on a loaded Windows shard (run
    37016327909) and turned a 160-append barrier into a 66 s test against a 60 s
    ceiling. Durability is the store's own property and ``test_crew_log_core`` pins it
    (an append inside a failing fsync, and the rollback after it), so here the fsync
    only prices the batch. Patches the store module's OWN ``os`` binding, never an
    attribute of the stdlib module.
    """
    monkeypatch.setattr(store, "os", _OsWithoutFsync())


def _segment_sizes() -> dict[str, int]:
    """Size of every session-log segment under the current data home, by path."""
    sizes: dict[str, int] = {}
    for segment in crew_log_root(KIND_SESSION).glob("*/log*.jsonl"):
        try:
            sizes[str(segment)] = segment.stat().st_size
        except OSError:  # removed between the listing and the stat
            continue
    return sizes


def _grew(before: dict[str, int], after: dict[str, int]) -> bool:
    """Whether any segment gained bytes or appeared. A removal alone is not progress."""
    return any(size > before.get(path, -1) for path, size in after.items())


def _stuck(what: str, elapsed: float, sizes: dict[str, int]) -> str:
    logs = ", ".join(f"{Path(path).parent.name}={size}B" for path, size in sorted(sizes.items()))
    return (
        f"the crew-log writer {what} ({elapsed:.1f}s into the barrier; "
        f"{emit.buffered_writes()} append(s) buffered, {emit.dropped_writes()} dropped; "
        f"session logs: {logs or 'none'})"
    )
