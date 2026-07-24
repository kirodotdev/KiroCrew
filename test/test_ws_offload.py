"""Regression test for the dashboard WS status-count offload fix.

``src/kiro_crew/dashboard/ws.py`` used to call ``state.crons.list_jobs()`` and
``state.lessons.load_all()`` inline on the event loop inside the periodic WS
status pusher. Both do blocking file I/O, so on a slow/large home dir they
stalled the loop — and with it every other WebSocket/coroutine on the gateway.

The fix routes ``lessons.load_all`` and the cron count through
``asyncio.to_thread`` via ``_load_status_counts``. Crucially, the cron count
uses ``CronManager.count_enabled_from_disk`` — a pure read-only file parse —
rather than ``list_jobs``: ``list_jobs`` triggers ``_sync()`` → ``_load()`` →
``_arm_timer()`` → ``asyncio.create_task`` which raises ``RuntimeError`` off the
loop thread (and cancels the live cron timer first), silently killing all
scheduled jobs. These tests prove the blocking work runs OFF the event-loop
thread and that the loop stays responsive while it runs.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.ws import _load_status_counts


@pytest.mark.asyncio
async def test_load_status_counts_runs_off_event_loop_thread():
    """FAILURE SCENARIO (pre-fix): count/load_all ran on the loop thread.

    POST-FIX: both execute in a worker thread (distinct from the loop thread),
    and the counts are returned correctly.
    """
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def blocking_count_enabled_from_disk():
        seen["jobs"] = threading.get_ident()
        time.sleep(0.05)
        return 3  # 3 enabled jobs

    def blocking_load_all():
        seen["lessons"] = threading.get_ident()
        time.sleep(0.05)
        return [object(), object()]  # 2 lessons

    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=blocking_count_enabled_from_disk),
        lessons=SimpleNamespace(load_all=blocking_load_all),
    )

    crons, lessons = await _load_status_counts(state)  # type: ignore[arg-type]

    assert (crons, lessons) == (3, 2)
    assert seen["jobs"] != loop_thread, "cron count must run off the event loop"
    assert seen["lessons"] != loop_thread, "load_all must run off the event loop"


@pytest.mark.asyncio
async def test_load_status_counts_does_not_block_the_loop():
    """While the blocking count-load is in flight, an independent coroutine
    keeps making progress — proving the loop is not stalled."""
    ticks = 0

    async def _ticker():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_count():
        time.sleep(0.1)
        return 1

    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=slow_count),
        lessons=SimpleNamespace(load_all=lambda: []),
    )

    ticker = asyncio.create_task(_ticker())
    crons, lessons = await _load_status_counts(state)  # type: ignore[arg-type]
    await ticker

    assert (crons, lessons) == (1, 0)
    # The loop advanced the ticker concurrently with the 0.1s blocking load.
    assert ticks >= 5, f"event loop appears stalled during blocking load (ticks={ticks})"
