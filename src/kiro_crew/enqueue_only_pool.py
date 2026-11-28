"""A thread pool whose ``submit`` never waits on another thread's ``Thread.start()``.

CPython's ``ThreadPoolExecutor.submit`` holds ``concurrent.futures.thread.
_global_shutdown_lock`` -- one lock for every pool in the process -- while it starts
a worker, and ``Thread.start()`` waits for the new thread to run. On a Windows host
near its commit limit that start takes seconds, so every other thread's ``submit``
waits on the lock, the event loop's included, long enough for the loop-stall
watchdog to exit the gateway. An idle worker does not help, because the lock is
taken before the stdlib looks for one.

:class:`EnqueueOnlyThreadPoolExecutor` takes only its own lock in ``submit`` and
starts no thread there. Building a pool starts its first worker and then its grower
thread, both on the building thread, so a pool always has a worker, and a start the
host refuses raises to the builder as the stdlib's first ``submit`` raised it to its
caller. The grower starts the rest, so a slow start delays only work still waiting
for a new worker, while the running workers keep taking work. It backs the loop's
default executor, the off-loop spawn pool and every thread pool
``kiro_crew.executors`` builds.

It subclasses ``ThreadPoolExecutor`` because ``loop.set_default_executor`` accepts
nothing else, and keeps the parent's ``_work_queue`` so readers of its depth
(``turn_timing``, ``cron_queue_depth``) still see the backlog. It runs none of the
parent's thread code. Its threads are daemons, and a pool dropped without a shutdown
stops them the way the stdlib's do, through a weak reference. A call queued before
``shutdown()`` still runs, and ``shutdown(wait=True)`` returns once it has. At
interpreter exit the pools' work is joined as the stdlib joins its own: one hook,
registered where the stdlib registers its own, makes every pool refuse new work from
then on, as the stdlib's do, shuts each pool registered by then down behind its queued
work and waits for every worker recorded by then, including those of a pool already
shut down without ``wait`` and dropped.
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

# For the exit hook: every live pool, and every worker a pool started with its queue,
# kept apart from the pool as the stdlib's ``_threads_queues`` keeps its own.
_registry_lock = threading.Lock()
_pools: weakref.WeakSet[EnqueueOnlyThreadPoolExecutor] = weakref.WeakSet()
_workers: weakref.WeakKeyDictionary[threading.Thread, queue.SimpleQueue[Any]] = (
    weakref.WeakKeyDictionary()
)
# Set when the exit hook starts: from then on every pool refuses new work, as the
# stdlib's pools refuse it after interpreter shutdown.
_exiting = False


class EnqueueOnlyThreadPoolExecutor(ThreadPoolExecutor):
    """A ``ThreadPoolExecutor`` whose ``submit`` only enqueues the call."""

    # The parent's attributes, holding this pool's own items: ``(future, fn, args,
    # kwargs)`` calls and the ``None`` stop, and the pool's workers.
    _work_queue: queue.SimpleQueue[Any]
    _threads: set[threading.Thread]

    def __init__(self, max_workers: int | None = None, thread_name_prefix: str = "") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        # One release per worker the grower should try to start.
        self._wanted = threading.Semaphore(0)
        _start_worker(self)  # a refused start raises here, before anything else runs
        stop = functools.partial(_stop_threads, self._work_queue, self._wanted)
        try:
            self._grower = threading.Thread(
                target=_grow,
                args=(weakref.ref(self, stop), self._wanted),
                name=f"{self._thread_name_prefix}_grow",
                daemon=True,
            )
            self._grower.start()
        except BaseException:
            self._work_queue.put(None)  # stop the worker started above
            raise
        with _registry_lock:
            _pools.add(self)

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        future: Future[_T] = Future()
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if _exiting:
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")
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
            # Only the grower adds workers after the first, and it stops once it has
            # seen the shutdown, so after it the set holds every worker that will run.
            self._grower.join()
            for worker in list(self._threads):
                worker.join()


def _join_at_exit() -> None:
    """Finish every pool's queued and running work before the interpreter exits."""
    global _exiting
    with _registry_lock:
        _exiting = True
        pools = list(_pools)
    for pool in pools:
        pool.shutdown(wait=False)
    # A grower can be inside a worker start; once the growers stop, the registry
    # holds every worker that will run.
    for pool in pools:
        pool._grower.join()
    with _registry_lock:
        workers = list(_workers.items())
    # A stop behind each queue's work, as the stdlib's hook puts one: it also stops the
    # idle workers of a pool built between the two snapshots, which takes no work.
    for _worker, queued in workers:
        queued.put(None)
    for worker, _queued in workers:
        worker.join()


# The stdlib's own pools join their workers through this same private hook, run
# before the non-daemon threads are joined and before ``atexit``.
threading._register_atexit(_join_at_exit)  # type: ignore[attr-defined]


def _stop_threads(work: queue.SimpleQueue[Any], wanted: threading.Semaphore, _ref: object) -> None:
    """Stop a dropped pool's threads: one ``None`` stops every worker, as at shutdown."""
    work.put(None)
    wanted.release()


def _start_worker(pool: EnqueueOnlyThreadPoolExecutor) -> None:
    worker = threading.Thread(
        target=_work,
        args=(pool._work_queue, pool._idle_semaphore),
        name=f"{pool._thread_name_prefix}_{len(pool._threads)}",
        daemon=True,
    )
    worker.start()
    pool._threads.add(worker)
    with _registry_lock:
        _workers[worker] = pool._work_queue


def _grow(
    pool_ref: weakref.ref[EnqueueOnlyThreadPoolExecutor], wanted: threading.Semaphore
) -> None:
    """Start the pool's workers past its first, up to its cap, off every caller's thread."""
    while True:
        wanted.acquire()
        pool = pool_ref()
        if pool is None or pool._shutdown:
            return
        if len(pool._threads) < pool._max_workers:
            try:
                # The slow call: it delays only work waiting for this worker.
                _start_worker(pool)
            except Exception as exc:  # noqa: BLE001 - RuntimeError or MemoryError
                # No thread can start now (thread or commit exhaustion): the running
                # workers keep taking the queue, and the next submit asks again.
                logger.warning("%s could not start a worker: %s", pool._thread_name_prefix, exc)
        del pool


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
