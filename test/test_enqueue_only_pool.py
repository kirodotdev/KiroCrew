"""``EnqueueOnlyThreadPoolExecutor``: a pool whose ``submit`` only enqueues.

The loop's default executor, the off-loop spawn pool and every pool
``kiro_crew.executors`` builds use it, so the loop's calls into them never wait while
some thread starts a pool worker. These tests hold a worker start, or the test's own
work, on an event the test releases, so no sleep stands in for a slow start; a hold
still in place at the bound fails by name.
"""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import threading

import pytest

from kiro_crew import enqueue_only_pool
from kiro_crew import platform_compat_offloop_spawn as offloop
from kiro_crew.enqueue_only_pool import EnqueueOnlyThreadPoolExecutor
from kiro_crew.testing.wait import default_timeout, finished, wait_until

#: A lost-run bound only: the tests release what they hold themselves, and a hold
#: still in place at this bound means the code under test waited for it.
_HOLD_BOUND_SECS = 10.0


def _idle_worker_pool(max_workers: int) -> EnqueueOnlyThreadPoolExecutor:
    """A pool whose first worker, started where the pool is built, waits for work."""
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="t-enqueue")
    wait_until(lambda: pool._idle_semaphore._value == 1, describe=lambda: "no idle worker")
    return pool


def test_submit_returns_while_the_pool_is_still_starting_its_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starting = threading.Event()
    release = threading.Event()
    gate = threading.Event()
    real_start = threading.Thread.start

    def held_start(thread: threading.Thread) -> None:
        if thread.name == "t-enqueue_1":
            starting.set()
            assert release.wait(_HOLD_BOUND_SECS), "submit waited for the worker start"
        real_start(thread)

    monkeypatch.setattr(enqueue_only_pool.threading.Thread, "start", held_start)
    pool = _idle_worker_pool(max_workers=2)
    try:
        busy = pool.submit(gate.wait, _HOLD_BOUND_SECS)  # takes the one idle worker
        future = pool.submit(lambda: "ran")
        assert starting.wait(default_timeout())
        # submit returned while the grower is still inside the second worker's start.
        assert not future.done()
        release.set()
        assert future.result(default_timeout()) == "ran"
        gate.set()
        assert busy.result(default_timeout())
    finally:
        release.set()
        gate.set()
        pool.shutdown(wait=True)


@pytest.mark.parametrize("error", [RuntimeError, MemoryError])
def test_a_pool_whose_first_worker_cannot_start_raises_where_it_is_built(
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
    with pytest.raises(error, match="can't start new thread"):
        EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue")
    # Nothing was left running: the grower starts only after the first worker.
    assert not [t for t in threading.enumerate() if t.name in ("t-enqueue_0", "t-enqueue_grow")]
    pool = EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue")
    try:
        assert pool.submit(int, "2").result(default_timeout()) == 2
    finally:
        pool.shutdown(wait=True)


def test_a_worker_start_refused_while_growing_leaves_the_call_to_the_running_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = threading.Event()
    gate = threading.Event()
    real_start = threading.Thread.start

    def refusing_start(thread: threading.Thread) -> None:
        if thread.name == "t-enqueue_1":
            refused.set()
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(enqueue_only_pool.threading.Thread, "start", refusing_start)
    pool = _idle_worker_pool(max_workers=2)
    try:
        busy = pool.submit(gate.wait, _HOLD_BOUND_SECS)
        queued = pool.submit(int, "3")
        assert refused.wait(default_timeout())
        assert not queued.done()
        gate.set()
        assert busy.result(default_timeout())
        assert queued.result(default_timeout()) == 3
    finally:
        gate.set()
        pool.shutdown(wait=True)
    assert [t.name for t in pool._threads] == ["t-enqueue_0"]


# Each run starts the gateway loop's spawn path; the spawn tests keep them serial.
@pytest.mark.xdist_group(name="spawn_off_loop")
def test_a_spawn_whose_pool_cannot_be_built_raises_the_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = threading.Thread.start

    def refusing_start(thread: threading.Thread) -> None:
        if thread.name == "t-enqueue-spawn_0":
            raise RuntimeError("can't start new thread")
        real_start(thread)

    built: list[EnqueueOnlyThreadPoolExecutor] = []

    def spawn_executor() -> EnqueueOnlyThreadPoolExecutor:
        # Held here, so a pool that did build is not dropped before its grower runs.
        built.append(
            EnqueueOnlyThreadPoolExecutor(max_workers=1, thread_name_prefix="t-enqueue-spawn")
        )
        return built[-1]

    monkeypatch.setattr(enqueue_only_pool.threading.Thread, "start", refusing_start)
    monkeypatch.setattr(offloop, "spawn_executor", spawn_executor)

    async def body() -> None:
        await finished(asyncio.create_subprocess_exec(sys.executable, "-c", "pass"), "the spawn")

    try:
        with asyncio.Runner(loop_factory=offloop.gateway_loop_factory()) as runner:
            # The start error itself, raised where the pool is built.
            with pytest.raises(RuntimeError, match="^can't start new thread$"):
                runner.run(body())
    finally:
        for pool in built:
            pool.shutdown(wait=True)
    assert built == []


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


#: A child that leaves two pools with work at a normal exit: a running call and a call
#: queued behind it, and a running call on a pool already shut down without ``wait``
#: and dropped. Each call waits until the exit hook shuts the pools down, so a pool
#: whose work is not joined at exit prints nothing, whatever the timing. A non-daemon
#: thread then builds a new pool while the hook runs: its submit is refused, as the
#: stdlib refuses one after interpreter shutdown, rather than left to a worker no one joins.
_EXIT_CHILD = f"""
import threading
from kiro_crew.enqueue_only_pool import EnqueueOnlyThreadPoolExecutor as Pool

BOUND = {_HOLD_BOUND_SECS!r}
exiting = threading.Event()
body_done = False
real_shutdown = Pool.shutdown


def shutdown(self, wait=True, *, cancel_futures=False):
    if body_done:
        exiting.set()
    real_shutdown(self, wait, cancel_futures=cancel_futures)


Pool.shutdown = shutdown


def run_at_exit(entered, line):
    entered.set()
    if exiting.wait(BOUND):
        print(line, flush=True)


running = Pool(max_workers=1, thread_name_prefix="t-running")
entered = threading.Event()
running.submit(run_at_exit, entered, "running")
assert entered.wait(BOUND)
running.submit(print, "queued", flush=True)

dropped = Pool(max_workers=1, thread_name_prefix="t-dropped")
entered = threading.Event()
dropped.submit(run_at_exit, entered, "dropped")
assert entered.wait(BOUND)
dropped.shutdown(wait=False)
del dropped


def late_submit():
    if exiting.wait(BOUND):
        try:
            Pool(max_workers=1, thread_name_prefix="t-late").submit(print, "late", flush=True)
        except RuntimeError as exc:
            print("refused" if "cannot schedule new futures" in str(exc) else exc, flush=True)


threading.Thread(target=late_submit).start()  # joined after the exit hooks
body_done = True
"""


def test_a_normal_exit_finishes_running_and_queued_work_and_refuses_new_work() -> None:
    done = subprocess.run(
        [sys.executable, "-c", _EXIT_CHILD],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    assert done.returncode == 0, done.stderr
    assert sorted(done.stdout.split()) == ["dropped", "queued", "refused", "running"], (
        done.stdout,
        done.stderr,
    )
