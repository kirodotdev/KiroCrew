"""``EnqueueOnlyThreadPoolExecutor``: a pool whose ``submit`` only enqueues.

The loop's default executor and the off-loop spawn pool use it, so the loop's
calls into them never wait while some thread starts a pool worker. These tests
hold the pool's own worker start, or the test's own work, on an event the test
releases, so no sleep stands in for a slow start; a hold still in place at the
bound fails by name.
"""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import threading
from typing import Any

import pytest

from kiro_crew import enqueue_only_pool
from kiro_crew import platform_compat_offloop_spawn as offloop
from kiro_crew.enqueue_only_pool import EnqueueOnlyThreadPoolExecutor
from kiro_crew.testing.wait import async_wait_until, default_timeout, finished, wait_until

#: A lost-run bound only: the tests release what they hold themselves, and a hold
#: still in place at this bound means the code under test waited for it.
_HOLD_BOUND_SECS = 10.0


def test_submit_returns_while_the_pool_is_still_starting_its_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starting = threading.Event()
    release = threading.Event()
    real_start = threading.Thread.start

    def held_start(thread: threading.Thread) -> None:
        if thread.name.startswith("t-enqueue_") and thread.name != "t-enqueue_grow":
            starting.set()
            assert release.wait(_HOLD_BOUND_SECS), "submit waited for the worker start"
        real_start(thread)

    monkeypatch.setattr(enqueue_only_pool.threading.Thread, "start", held_start)
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue")
    try:
        future = pool.submit(lambda: "ran")
        assert starting.wait(default_timeout())
        # submit returned while the grower is still inside the worker's start.
        assert not future.done()
        release.set()
        assert future.result(default_timeout()) == "ran"
    finally:
        release.set()
        pool.shutdown(wait=True)


@pytest.mark.parametrize("error", [RuntimeError, MemoryError])
def test_a_worker_that_cannot_start_fails_the_queued_call_and_the_next_submit_retries(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    real_start = threading.Thread.start
    refused: list[str] = []

    def refusing_start(thread: threading.Thread) -> None:
        if thread.name == "t-enqueue_0" and not refused:
            refused.append(thread.name)
            raise error("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(enqueue_only_pool.threading.Thread, "start", refusing_start)
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue")
    try:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            pool.submit(int, "1").result(default_timeout())
        assert pool.submit(int, "2").result(default_timeout()) == 2
    finally:
        pool.shutdown(wait=True)


# Each run starts the gateway loop's spawn path; the spawn tests keep them serial.
@pytest.mark.xdist_group(name="spawn_off_loop")
def test_a_spawn_cancelled_while_its_pool_cannot_start_a_worker_raises_the_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    real_start = threading.Thread.start

    def failing_start(thread: threading.Thread) -> None:
        if thread.name == "t-enqueue-spawn_0":
            entered.set()
            assert release.wait(_HOLD_BOUND_SECS), "the test never released the start"
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(enqueue_only_pool.threading.Thread, "start", failing_start)
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue-spawn")
    monkeypatch.setattr(offloop, "spawn_executor", lambda: pool)

    async def body() -> BaseException:
        spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(sys.executable, "-c", "pass"))
        await async_wait_until(entered.is_set, describe=lambda: "no worker start was tried")
        spawn.cancel()
        for _ in range(5):  # the spawn absorbs the cancel and keeps waiting for its build
            await asyncio.sleep(0)
        assert not spawn.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await finished(spawn, "the cancelled spawn")
        return raised.value

    try:
        with asyncio.Runner(loop_factory=offloop.gateway_loop_factory()) as runner:
            cancel = runner.run(body())
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert isinstance(cancel.__cause__, RuntimeError)


def test_the_pool_runs_at_most_its_cap_and_delivers_every_outcome() -> None:
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=2, thread_name_prefix="t-enqueue")
    gate = threading.Event()
    running = threading.Semaphore(0)

    def blocked(value: int) -> int:
        running.release()
        assert gate.wait(_HOLD_BOUND_SECS), "the test never opened the gate"
        return value

    try:
        futures = [pool.submit(blocked, n) for n in range(3)]
        assert running.acquire(timeout=default_timeout())
        assert running.acquire(timeout=default_timeout())
        assert sum(f.running() for f in futures) == 2
        gate.set()
        assert [f.result(default_timeout()) for f in futures] == [0, 1, 2]
        with pytest.raises(ValueError):
            pool.submit(int, "not a number").result(default_timeout())
    finally:
        gate.set()
        pool.shutdown(wait=True)
    # The waited shutdown joined the grower, so every worker it started is recorded.
    assert sorted(t.name for t in pool._threads) == ["t-enqueue_0", "t-enqueue_1"]


def test_a_call_queued_before_any_worker_started_runs_before_a_waited_shutdown_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    go = threading.Event()
    real_grow = enqueue_only_pool._grow

    def late_grow(*args: Any) -> None:
        assert go.wait(_HOLD_BOUND_SECS), "the test never let the grower run"
        real_grow(*args)

    monkeypatch.setattr(enqueue_only_pool, "_grow", late_grow)
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=2, thread_name_prefix="t-enqueue")
    future = pool.submit(int, "3")
    pool.shutdown(wait=False)  # before the grower has started a worker for the call
    go.set()
    pool.shutdown(wait=True)
    assert future.done(), "the waited shutdown returned before the queued call ran"
    assert future.result() == 3


def test_a_worker_counts_as_idle_once_however_many_queued_calls_it_ran() -> None:
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue")
    gate = threading.Event()
    try:
        first = pool.submit(gate.wait, _HOLD_BOUND_SECS)
        queued = [pool.submit(int, str(n)) for n in range(2)]
        gate.set()
        assert first.result(default_timeout())
        assert [f.result(default_timeout()) for f in queued] == [0, 1]
        # As the stdlib's worker does, it marks itself idle only on finding the queue
        # empty, so readers can count busy workers as threads minus idle tokens.
        wait_until(lambda: pool._idle_semaphore._value > 0)
        assert pool._idle_semaphore._value == 1
    finally:
        gate.set()
        pool.shutdown(wait=True)


def test_shutdown_cancels_queued_work_and_refuses_new_work() -> None:
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue")
    entered = threading.Event()
    gate = threading.Event()

    def blocked() -> None:
        entered.set()
        assert gate.wait(_HOLD_BOUND_SECS), "the test never opened the gate"

    running = pool.submit(blocked)
    try:
        assert entered.wait(default_timeout())
        cancelled = pool.submit(int)
        queued = pool.submit(int)
        assert cancelled.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        assert queued.cancelled()
        with pytest.raises(RuntimeError, match="after shutdown"):
            pool.submit(int)
    finally:
        gate.set()
    pool.shutdown(wait=True)
    assert running.done() and running.exception() is None
    assert not any(t.is_alive() for t in pool._threads)


def test_a_pool_dropped_without_shutdown_stops_its_threads() -> None:
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue-dropped")
    assert pool.submit(int, "7").result(default_timeout()) == 7
    threads = [t for t in threading.enumerate() if t.name.startswith("t-enqueue-dropped")]
    assert len(threads) == 2  # the grower and one worker
    del pool
    gc.collect()
    wait_until(
        lambda: not any(t.is_alive() for t in threads),
        describe=lambda: [t.name for t in threads if t.is_alive()],
    )


def test_an_interpreter_with_a_live_pool_exits() -> None:
    script = (
        "from kiro_crew.enqueue_only_pool import EnqueueOnlyThreadPoolExecutor\n"
        "pool = EnqueueOnlyThreadPoolExecutor(max_workers=2)\n"
        "assert pool.submit(int, '5').result(10) == 5\n"
        "pool.submit(__import__('threading').Event().wait)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    assert done.returncode == 0, done.stderr
