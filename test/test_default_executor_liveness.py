"""The loop stall watchdog also catches a wedged default executor.

Every ``asyncio.to_thread`` call waits in the loop's default executor, so a pool
whose workers are all blocked stops every turn while the loop itself stays live
and keeps beating. :meth:`LoopStallWatchdog.default_executor_live` probes that
pool from the heartbeat, which beats only while it answers ``True``.

The loop is a stand-in whose ``run_in_executor`` hands the probe to a real
one-worker pool, and the clock is injected, so no test waits out the budget.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_loop_watchdog import _PagingFixture

import kiro_crew.dashboard.server as server
from kiro_crew.dashboard import loop_watchdog
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog

_LOGGER = "test.default_executor_liveness"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Loop:
    """``run_in_executor(None, fn)`` on a real pool, keeping each probe it was handed."""

    def __init__(self, pool: ThreadPoolExecutor) -> None:
        self.pool = pool
        self.probes: list[Future[Any]] = []

    def run_in_executor(self, executor: None, fn: Callable[[], Any]) -> Future[Any]:
        assert executor is None, "the probe must go through the default executor"
        probe = self.pool.submit(fn)
        self.probes.append(probe)
        return probe


def _watchdog(clock: _Clock, dumps: list[float]) -> LoopStallWatchdog:
    return LoopStallWatchdog(
        stall_after=30.0,
        exit_after=None,
        now=clock,
        dump=lambda: dumps.append(clock.t),
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
        enrich=lambda _silence: ["enrichment"],
        log=logging.getLogger(_LOGGER),
    )


def _records(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == _LOGGER]


def test_a_wedged_default_executor_stops_the_beat_until_it_runs_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, dumps = _Clock(), []
    wd = _watchdog(clock, dumps)
    release = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        loop = _Loop(pool)
        try:
            wedged = pool.submit(release.wait, 30.0)  # the pool's only worker, blocked
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                assert wd.default_executor_live(loop) is True  # probe queued behind it
                wd.beat()
                clock.t += loop_watchdog.EXECUTOR_STALL_SECS - 1.0
                assert wd.default_executor_live(loop) is True  # inside the budget
                clock.t += 1.0
                assert wd.default_executor_live(loop) is False
                assert wd.default_executor_live(loop) is False
                assert len(loop.probes) == 1, "a queued probe is not stacked on"
                # The withheld beat is a stall to the same watchdog that dumps a wedged loop.
                assert wd.check() is True
                assert dumps == [clock.t]
                said = [m for _, m in _records(caplog) if m.startswith("default executor has not")]
                assert said == [
                    f"default executor has not run a no-op in "
                    f"{loop_watchdog.EXECUTOR_STALL_SECS:.0f}s while the event loop runs, so "
                    "every to_thread call is stuck; withholding the heartbeat so the stall "
                    "watchdog dumps every thread's stack (the 'event loop silent' lines that "
                    "follow are this executor, not the loop)"
                ], said
                release.set()
                wedged.result(timeout=30.0)
                loop.probes[0].result(timeout=30.0)
                assert wd.default_executor_live(loop) is True
                wd.beat()
                assert wd.check() is False
            assert len(loop.probes) == 2, "a fresh probe follows the one that ran"
            assert (
                logging.WARNING,
                "default executor ran its probe again; heartbeat resumed",
            ) in _records(caplog)
        finally:
            release.set()


def test_a_default_executor_that_answers_is_never_called_stalled() -> None:
    """The budget runs from each probe's own submit, not from the first one."""
    clock, dumps = _Clock(), []
    wd = _watchdog(clock, dumps)
    with ThreadPoolExecutor(max_workers=1) as pool:
        loop = _Loop(pool)
        for _ in range(3):
            assert wd.default_executor_live(loop) is True
            loop.probes[-1].result(timeout=30.0)
            clock.t += loop_watchdog.EXECUTOR_STALL_SECS * 2
        assert len(loop.probes) == 3


@pytest.mark.asyncio
async def test_the_heartbeat_beats_only_while_the_default_executor_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = asyncio.Semaphore(2)

    async def _sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        await ticks.acquire()  # virtual intervals: two ticks, no real wait

    # As the heartbeat tests in test_dashboard_server_composition_contract_owners.py do;
    # rebinding the facade's own ``asyncio`` would trip its composition census.
    monkeypatch.setattr(
        asyncio, "sleep", _sleep  # flake-ok: virtual heartbeat intervals, restored at teardown
    )
    state = MagicMock()
    # The second tick's sample ends the heartbeat, after its beat decision.
    state.resource_pressure_notifier.maybe_sample = AsyncMock(
        side_effect=[None, RuntimeError("end")]
    )
    trimmer = MagicMock()
    trimmer.maybe_trim = AsyncMock(return_value=0)
    watchdog = MagicMock()
    watchdog.claim_lag_enrichment.return_value = False
    watchdog.default_executor_live.side_effect = [False, True]
    task = server._start_loop_heartbeat(state, watchdog, trimmer)
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30.0)
    loop = asyncio.get_running_loop()
    assert [c.args for c in watchdog.default_executor_live.call_args_list] == [(loop,), (loop,)]
    assert watchdog.beat.call_count == 1, "the tick whose probe had not run must not beat"


def test_a_beat_withheld_for_the_executor_is_not_deferred_as_paging() -> None:
    """The loop thread runs while the executor is wedged, so its faults defer nothing."""
    fx = _PagingFixture()
    try:
        stuck = SimpleNamespace(run_in_executor=lambda _executor, _fn: Future())
        assert fx.wd.default_executor_live(stuck) is True
        fx.clock.t += loop_watchdog.EXECUTOR_STALL_SECS
        assert fx.wd.default_executor_live(stuck) is False
        fx.poll_at(10.0)
        fx.poll_at(15.0, add_faults=40)
        fx.poll_at(20.0, add_faults=12)
        assert fx.arms == [], "the managed exit budget must not be re-armed"
        assert fx.paging_lines() == []
    finally:
        fx.wd.stop()
