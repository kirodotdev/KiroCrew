"""A thread pool whose ``submit`` never waits on another thread's ``Thread.start()``.

CPython's ``ThreadPoolExecutor.submit`` holds ``concurrent.futures.thread.
_global_shutdown_lock`` -- one lock for every pool in the process -- while it starts
a worker, and ``Thread.start()`` waits for the new thread to run. On a Windows host
near its commit limit that start takes seconds, so every other thread's ``submit``
waits on the lock, the event loop's included, long enough for the loop-stall
watchdog to exit the gateway. An idle worker does not help, because the lock is
taken before the stdlib looks for one.

:class:`EnqueueOnlyThreadPoolExecutor` takes only its own lock in ``submit`` and
starts no thread there: one grower thread per pool starts the workers, so a slow
start delays only work still waiting for a new worker, while idle workers keep
taking work. It backs the loop's default executor and the off-loop spawn pool.
The other pools keep the stdlib executor, so a loop-thread ``run_in_executor`` on
one of them still takes the process-wide lock.

It subclasses ``ThreadPoolExecutor`` because ``loop.set_default_executor`` accepts
nothing else, and keeps the parent's ``_work_queue`` so readers of its depth
(``turn_timing``) still see the backlog. It runs none of the parent's thread code.
Its threads are daemons: a pool shut down without ``wait`` (``loop.close()``,
``atexit``) never holds interpreter exit, and a pool dropped without a shutdown
stops its threads the way the stdlib's do, through a weak reference. A call queued
before ``shutdown()`` still runs (unless the pool is dropped before its grower wakes),
and ``shutdown(wait=True)`` returns once it has.
"""

from __future__ import annotations

import functools
import logging
import queue
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, ParamSpec, TypeVar

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")


class EnqueueOnlyThreadPoolExecutor(ThreadPoolExecutor):
    """A ``ThreadPoolExecutor`` whose ``submit`` only enqueues the call."""

    # The parent's attributes, holding this pool's own items: ``(future, fn, args,
    # kwargs)`` calls and the ``None`` stop, and the workers the grower started.
    _work_queue: queue.SimpleQueue[Any]
    _threads: set[threading.Thread]

    def __init__(self, max_workers: int | None = None, thread_name_prefix: str = "") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        # One release per worker the grower should try to start.
        self._wanted = threading.Semaphore(0)
        stop = functools.partial(_stop_threads, self._work_queue, self._wanted)
        self._grower = threading.Thread(
            target=_grow,
            args=(weakref.ref(self, stop), self._wanted),
            name=f"{self._thread_name_prefix}_grow",
            daemon=True,
        )
        self._grower.start()

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        future: Future[_T] = Future()
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._work_queue.put((future, fn, args, kwargs))
        if not self._idle_semaphore.acquire(timeout=0):
            self._wanted.release()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._shutdown_lock:
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        item = self._work_queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
            self._work_queue.put(None)
        self._wanted.release()
        if wait:
            # Only the grower adds workers, and it stops once it has seen the
            # shutdown, so after it the set holds every worker that will run.
            self._grower.join()
            for worker in list(self._threads):
                worker.join()


def _stop_threads(work: queue.SimpleQueue[Any], wanted: threading.Semaphore, _ref: object) -> None:
    """Stop a dropped pool's threads: one ``None`` stops every worker, as at shutdown."""
    work.put(None)
    wanted.release()


def _grow(
    pool_ref: weakref.ref[EnqueueOnlyThreadPoolExecutor], wanted: threading.Semaphore
) -> None:
    """Start the pool's workers, up to its cap, off every caller's thread."""
    while True:
        wanted.acquire()
        pool = pool_ref()
        if pool is None:
            return
        stopping = pool._shutdown
        # A shut-down pool with no worker still starts one, which runs the calls
        # queued before the shutdown and then stops.
        if len(pool._threads) < (1 if stopping else pool._max_workers):
            try:
                worker = threading.Thread(
                    target=_work,
                    args=(pool._work_queue, pool._idle_semaphore),
                    name=f"{pool._thread_name_prefix}_{len(pool._threads)}",
                    daemon=True,
                )
                # The slow call: it delays only work waiting for this worker.
                worker.start()
                pool._threads.add(worker)
            except Exception as exc:  # noqa: BLE001 - RuntimeError or MemoryError
                # No thread can start now (thread or commit exhaustion): the next
                # submit asks again. A pool with no worker at all fails what is
                # queued, as the stdlib's submit raised this to its caller.
                logger.warning("%s could not start a worker: %s", pool._thread_name_prefix, exc)
                if not pool._threads:
                    _fail_queued(pool, exc)
        if stopping:
            return
        del pool


def _fail_queued(pool: EnqueueOnlyThreadPoolExecutor, exc: Exception) -> None:
    stopped = False
    with pool._shutdown_lock:
        while True:
            try:
                item = pool._work_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                stopped = True
            elif item[0].set_running_or_notify_cancel():
                error = RuntimeError(f"cannot start a pool worker: {exc}")
                error.__cause__ = exc
                item[0].set_exception(error)
        if stopped:
            pool._work_queue.put(None)


def _work(work: queue.SimpleQueue[Any], idle: threading.Semaphore) -> None:
    while True:
        try:
            item = work.get_nowait()
        except queue.Empty:
            # Idle only while nothing is queued, as the stdlib's worker counts it.
            idle.release()
            item = work.get()
        if item is None:
            work.put(None)  # pass the stop on to the next worker
            return
        future, fn, args, kwargs = item
        del item
        if future.set_running_or_notify_cancel():
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: B036 - delivered to the caller
                future.set_exception(exc)
            else:
                future.set_result(result)
                del result
        # Drop the call before waiting for the next one, so nothing it returned or
        # raised stays reachable from an idle worker.
        del future, fn, args, kwargs
