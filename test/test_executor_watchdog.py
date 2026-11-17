"""Tests for the default executor's liveness probe.

The probe is driven with an injected clock, injected futures, injected worker
samples and recording report/exit hooks, so no test waits on a real interval or
ends the process. One test blocks a real one-worker pool behind an event to show
a queued no-op, and a worker parked on one item, are what the probe reads as a
stall with no progress.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import executor_watchdog
from kiro_crew.dashboard.executor_watchdog import (
    EXECUTOR_STALL_EXIT_CODE,
    ExecutorStallProbe,
    default_worker_items,
    format_thread_stacks,
)
from kiro_crew.executors import DEFAULT_EXECUTOR_THREAD_PREFIX
from kiro_crew.testing.wait import async_wait_until, await_until

#: The interval the event-loop heartbeat sleeps between beats.
_HEARTBEAT_INTERVAL_SECS = 5.0


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Harness:
    """A probe whose futures, worker samples, reports and exits are recorded."""

    def __init__(self, *, armed: bool = True, submit: Any = None, **kwargs: Any) -> None:
        self.clock = _Clock()
        self.futures: list[asyncio.Future[None]] = []
        self.items: dict[int, Any] = {1: object()}
        self.renders = 0
        self.writes = 0
        self.exits: list[int] = []
        self.fallbacks: list[tuple[float, Callable[[], None]]] = []
        self.armed = armed
        self.probe = ExecutorStallProbe(
            armed=lambda: self.armed,
            report_after=60.0,
            exit_after=300.0,
            no_progress_after=240.0,
            now=self.clock,
            submit=submit or self._submit,
            worker_items=lambda: dict(self.items),
            render_stacks=self._render,
            write_report=self._write,
            run_off_loop=lambda work: work(),
            call_later=lambda delay, cb: self.fallbacks.append((delay, cb)),
            exit_process=self.exits.append,
            **kwargs,
        )

    def _submit(self) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.futures.append(future)
        return future

    def _render(self) -> str:
        self.renders += 1
        return "<stacks>"

    def _write(self) -> None:
        self.writes += 1

    def advance(self, secs: float) -> None:
        self.clock.t += secs
        self.probe.tick()


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


@pytest.mark.asyncio
async def test_a_probe_that_completes_is_replaced_and_reports_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = _Harness()
    with caplog.at_level(logging.DEBUG, logger=executor_watchdog.logger.name):
        h.probe.tick()
        h.futures[0].set_result(None)
        await h.futures[0]
        h.advance(400.0)
    assert len(h.futures) == 2
    assert h.renders == 0 and h.exits == []
    assert not _messages(caplog, logging.ERROR) and not _messages(caplog, logging.WARNING)


@pytest.mark.asyncio
async def test_a_stalled_pool_with_no_progress_is_reported_once_then_exits_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = _Harness()
    with caplog.at_level(logging.DEBUG, logger=executor_watchdog.logger.name):
        h.probe.tick()
        h.advance(59.0)
        assert h.renders == 0
        h.advance(1.0)
        h.advance(100.0)
        assert h.renders == 1 and h.exits == []
        h.advance(140.0)
        h.advance(5.0)
    # One probe is in flight the whole time: a stalled pool collects no backlog.
    assert len(h.futures) == 1
    assert h.exits == [EXECUTOR_STALL_EXIT_CODE]
    assert h.renders == 2 and h.writes == 2
    # The loop schedules the exit too, so a report that never finishes cannot hold
    # it; whichever runs second does nothing.
    assert [delay for delay, _ in h.fallbacks] == [5.0]
    h.fallbacks[0][1]()
    assert h.exits == [EXECUTOR_STALL_EXIT_CODE]
    errors = _messages(caplog, logging.ERROR)
    assert len(errors) == 2
    assert errors[0].startswith("default executor stalled: a no-op has waited 60s")
    assert "restarts if it is still stuck at 300s with no worker progress for 240s" in errors[0]
    assert errors[1].startswith("default executor stalled: a no-op has waited 300s")
    assert "none of them has started new work for 240s" in errors[1]
    assert "Exiting with status 1 to be restarted" in errors[1]
    assert all(e.endswith("Thread stacks:\n<stacks>") for e in errors)


@pytest.mark.asyncio
async def test_a_saturated_pool_that_keeps_starting_work_is_never_restarted() -> None:
    h = _Harness()
    h.probe.tick()
    for _ in range(40):
        h.items = {1: object()}
        h.advance(30.0)
    assert h.exits == []
    assert h.renders == 1


@pytest.mark.asyncio
async def test_the_no_progress_clock_starts_at_the_last_change() -> None:
    h = _Harness()
    h.probe.tick()
    h.advance(60.0)
    h.items = {1: object()}
    h.advance(340.0)  # waited 400s; the change at this sample restarts the clock
    h.advance(235.0)
    assert h.exits == []
    h.advance(5.0)
    assert h.exits == [EXECUTOR_STALL_EXIT_CODE]


@pytest.mark.asyncio
async def test_a_worker_that_finishes_or_appears_counts_as_progress() -> None:
    h = _Harness()
    h.probe.tick()
    h.advance(60.0)
    h.items = {1: h.items[1], 2: None}
    h.advance(240.0)
    assert h.exits == []
    h.advance(240.0)
    assert h.exits == [EXECUTOR_STALL_EXIT_CODE]


@pytest.mark.asyncio
async def test_a_stall_never_exits_while_the_loop_watchdog_is_not_running() -> None:
    h = _Harness(armed=False)
    h.probe.tick()
    for _ in range(4):
        h.advance(300.0)
    assert h.exits == [] and h.fallbacks == []
    assert h.renders == 1


@pytest.mark.asyncio
async def test_a_recovered_stall_says_so_and_can_be_reported_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = _Harness()
    with caplog.at_level(logging.DEBUG, logger=executor_watchdog.logger.name):
        h.probe.tick()
        h.advance(90.0)
        h.clock.t += 30.0
        h.futures[0].set_result(None)
        await h.futures[0]
        h.probe.tick()
        h.advance(60.0)
    assert _messages(caplog, logging.WARNING) == [
        "default executor recovered: its liveness probe waited 120s for a worker"
    ]
    assert len(h.futures) == 2
    assert h.renders == 2
    assert h.exits == []


@pytest.mark.asyncio
async def test_a_probe_that_cannot_be_submitted_does_not_raise() -> None:
    calls: list[int] = []

    def _closed() -> asyncio.Future[None]:
        calls.append(1)
        raise RuntimeError("cannot schedule new futures after shutdown")

    h = _Harness(submit=_closed)
    h.probe.tick()
    h.probe.tick()
    assert calls == [1, 1]
    assert h.exits == []


@pytest.mark.asyncio
async def test_a_no_op_queued_behind_a_parked_worker_reads_as_a_stall_without_progress() -> None:
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=DEFAULT_EXECUTOR_THREAD_PREFIX)
    gate = threading.Event()
    started = threading.Event()
    submitted: list[asyncio.Future[None]] = []

    def _park() -> None:
        started.set()
        gate.wait(30.0)

    def _submit() -> asyncio.Future[None]:
        future = loop.run_in_executor(pool, executor_watchdog._noop)
        submitted.append(future)
        return future

    h = _Harness(submit=_submit)
    blocker = loop.run_in_executor(pool, _park)
    try:
        assert started.wait(10.0)
        first = default_worker_items()
        second = default_worker_items()
        parked = [ident for ident, item in first.items() if item is not None]
        assert len(parked) == 1
        assert second[parked[0]] is first[parked[0]]
        h.probe.tick()
        h.advance(60.0)
        assert h.renders == 1
        assert not submitted[0].done()
    finally:
        gate.set()
        await await_until(blocker, what="the parked worker")
        await await_until(submitted[0], what="the first probe")
        h.probe.tick()
        if len(submitted) == 2:
            await await_until(submitted[1], what="the second probe")
        pool.shutdown(wait=True)
    assert len(submitted) == 2


@pytest.mark.asyncio
async def test_the_report_is_written_off_the_loop_thread() -> None:
    written = threading.Event()
    writers: list[str] = []

    def _write() -> None:
        writers.append(threading.current_thread().name)
        written.set()

    clock = _Clock()
    probe = ExecutorStallProbe(
        armed=lambda: False,
        now=clock,
        submit=lambda: asyncio.get_running_loop().create_future(),
        worker_items=dict,
        render_stacks=lambda: "<stacks>",
        write_report=_write,
        exit_process=lambda code: None,
    )
    probe.tick()
    clock.t += 60.0
    probe.tick()
    assert written.wait(10.0)
    assert writers == ["mc-executor-stall-report"]


def test_the_rendered_stacks_name_each_thread_and_its_frames() -> None:
    def _marker_frame() -> str:
        return format_thread_stacks()

    rendered = _marker_frame()
    assert "Thread MainThread (0x" in rendered
    assert "in _marker_frame" in rendered
    assert "in test_the_rendered_stacks_name_each_thread_and_its_frames" in rendered
    # Frames only: no source line is read, because a stalled disk is the likely cause.
    assert "return format_thread_stacks()" not in rendered


def test_the_rendered_stacks_are_capped() -> None:
    rendered = format_thread_stacks(max_threads=0)
    assert rendered.startswith("... ") and rendered.endswith("more thread(s) not shown")


@pytest.mark.asyncio
async def test_every_heartbeat_ticks_the_probe_armed_by_the_loop_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat ticks the probe once per beat, after the beat, and the probe
    may exit only while the loop-stall watchdog says it is running."""
    import kiro_crew.dashboard.server as server

    calls: list[str] = []
    armed: list[Any] = []

    class _Probe:
        def __init__(self, **kwargs: Any) -> None:
            armed.append(kwargs["armed"])

        def tick(self) -> None:
            calls.append("tick")

    monkeypatch.setattr(executor_watchdog, "ExecutorStallProbe", _Probe)
    # Virtual intervals, injected into this heartbeat alone: each takes one of two
    # permits, so the heartbeat beats twice and parks in its third sleep until it
    # is cancelled.
    intervals = asyncio.Semaphore(2)
    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        delays.append(delay)
        await intervals.acquire()

    # The sealed-file scan reads the real config home; this test is about the probe.
    from kiro_crew.dashboard.handlers import config_pending

    monkeypatch.setattr(config_pending, "sync_pending_notes", AsyncMock())
    # Only the attributes the heartbeat reads, so a later read of anything else
    # fails here instead of starting a subsystem through a truthy mock.
    state = types.SimpleNamespace(
        resource_pressure_notifier=types.SimpleNamespace(maybe_sample=AsyncMock()),
        repeated_failures_notifier=types.SimpleNamespace(maybe_check=MagicMock()),
    )
    trimmer = MagicMock()
    trimmer.maybe_trim = AsyncMock(return_value=0)
    watchdog = MagicMock()
    watchdog.claim_lag_enrichment.return_value = False
    watchdog.beat.side_effect = lambda: calls.append("beat")
    watchdog.is_running.return_value = False
    task = server._start_loop_heartbeat(state, watchdog, trimmer, sleep=_sleep)
    try:
        await async_wait_until(
            lambda: calls.count("tick") >= 2,
            describe=lambda: f"the heartbeat made these calls: {calls!r}",
        )
    finally:
        task.cancel()
        await await_until(asyncio.gather(task, return_exceptions=True), what="the heartbeat")
    assert calls[:4] == ["beat", "tick", "beat", "tick"]
    assert set(delays) == {_HEARTBEAT_INTERVAL_SECS}
    assert len(armed) == 1 and armed[0]() is False
    watchdog.is_running.return_value = True
    assert armed[0]() is True
