"""Reusable bounded worker-pool engine for long-lived agent sessions.

A provider-agnostic pool of warm workers that any KiroCrew app can reuse to run
agent work as **direct, long-lived sessions** instead of per-task ``/api/spawn``
sub-agents (which produce an agent card, a ``:lock:`` approval prompt, a Slack
relay, and a reaper slot). The engine owns only the *pooling* concerns; the
worker implementation (which agent, which backend, sweep protection, working
directory, effort, reset semantics) is supplied by the caller via a factory.

First consumer: Code Review Sage's ``ReviewPool`` (``apps/builtins/
code-review-sage/lib/review_pool.py``). Knowledge Library's ``LLMPool`` and
others can adopt it later without changing the engine — see the design note in
``docs/code-review-sage-design.md`` §9.

Engine guarantees (the semantics any consumer relies on):

  * **Lazy** — workers are created on demand, never eagerly. An unused pool
    spawns nothing.
  * **Bounded concurrency** — at most ``max_workers`` tasks run at once
    (== max live workers); further callers block on a semaphore.
  * **Throttled startup** — at most ``max_starting`` workers cold-start
    simultaneously (process launch + handshake is the expensive part).
  * **Optional clean-slate reset** — before a *reused* worker runs the next
    task its context is reset via ``worker.reset()``. Skipped for a freshly
    created worker (already clean) and when the caller passes ``reset=False``.
  * **Self-healing** — a worker found dead on acquire (or that raised during a
    task) is shut down and replaced; it never re-enters the idle set.

The engine is async; a synchronous caller bridges via
``asyncio.run_coroutine_threadsafe`` on the owning event loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# Sweep-protection shield. Pool workers are direct, long-lived agent sessions
# that the gateway's periodic orphan sweep cannot see via ``_collect_active_pids``
# (they are not SessionMap sessions nor warm-pool providers). Registering their
# PID here keeps the sweep from classifying a *busy* worker as a leaked orphan and
# SIGKILLing it mid-task (surfacing to the caller as "ACP process exited (code=1)").
# The engine owns this lifecycle so every pool built on it is protected by
# construction — no per-worker wiring, and it can't be forgotten by a new consumer.
# Imported defensively so the engine stays usable standalone / in hermetic tests.
try:
    from kiro_crew.session_pid import register_protected_pid, unregister_protected_pid
except Exception:  # pragma: no cover - standalone / test fallback
    def register_protected_pid(pid: int) -> None:  # type: ignore[misc]
        return None

    def unregister_protected_pid(pid: int) -> None:  # type: ignore[misc]
        return None

# Engine defaults. Consumers typically pass their own tuned values.
DEFAULT_MAX_WORKERS = 5
DEFAULT_MAX_STARTING = 2
DEFAULT_TASK_TIMEOUT = 1800.0


class PoolWorker(Protocol):
    """A long-lived pool worker. Implemented by each app's worker class (e.g.
    Code Review Sage's ``AcpReviewWorker``) and by fakes in tests."""

    async def start(self) -> None:
        """Spawn/initialize the underlying process or session."""
        ...

    async def send_message(self, prompt: str, timeout: float = ...) -> str:
        """Run one task on this worker and return its text result."""
        ...

    async def reset(self) -> None:
        """Wipe per-task context so the next task starts from a clean slate."""
        ...

    async def shutdown(self) -> None:
        """Gracefully destroy this worker."""
        ...

    def is_alive(self) -> bool:
        """True if this worker can still process tasks."""
        ...

    # OPTIONAL — ``def pid(self) -> int | None``: the underlying agent-process
    # PID. When present, the pool auto-shields it from the gateway's orphan
    # sweep (register on start, re-sync after every reset, unregister on
    # retire). Workers that omit it (e.g. test fakes) are simply not shielded —
    # harmless, since only live kiro-cli/claude PIDs are ever swept. Read via
    # ``_worker_pid`` (getattr), so it is NOT required by the Protocol.


# Factory returns an *unstarted* worker; the pool calls ``start()``.
WorkerFactory = Callable[[], PoolWorker]


def _worker_pid(worker: "PoolWorker") -> Optional[int]:
    """Best-effort read of a worker's underlying agent-process PID (optional
    ``pid()`` method). Returns a positive int or None (never raises)."""
    fn = getattr(worker, "pid", None)
    if not callable(fn):
        return None
    try:
        p = fn()
    except Exception:
        return None
    return p if isinstance(p, int) and p > 0 else None


async def _safe_shutdown(worker: PoolWorker) -> None:
    try:
        await worker.shutdown()
    except Exception:
        logger.debug("worker shutdown error", exc_info=True)


class WorkerPool:
    """Lazy, bounded, self-healing pool of reusable workers (see module docstring).

    ``worker_factory`` is REQUIRED and returns an unstarted worker implementing
    :class:`PoolWorker`. The pool is provider-agnostic: it never imports a
    backend client itself.
    """

    def __init__(
        self,
        worker_factory: WorkerFactory,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_starting: int = DEFAULT_MAX_STARTING,
        default_timeout: float = DEFAULT_TASK_TIMEOUT,
        name: str = "worker-pool",
    ) -> None:
        if worker_factory is None:  # type: ignore[truthy-function]
            raise ValueError("worker_factory is required")
        self._factory: WorkerFactory = worker_factory
        self._max_workers = max(1, max_workers)
        self._default_timeout = default_timeout
        self._max_starting = max(1, max_starting)
        self._task_sema = asyncio.Semaphore(self._max_workers)
        self._start_sema = asyncio.Semaphore(self._max_starting)
        self._lock = asyncio.Lock()
        self._idle: list[PoolWorker] = []
        self._created = 0           # number of LIVE workers (idle + in-flight)
        self._closing = False
        self._name = name
        # id(worker) -> the PID currently shielded from the orphan sweep for that
        # worker. Kept in sync across start/reset/retire so a worker's live process
        # is always protected and a retired one is always released.
        self._protected: dict[int, int] = {}

    # ── Introspection (status / tests) ──
    @property
    def idle_count(self) -> int:
        return len(self._idle)

    @property
    def created_count(self) -> int:
        return self._created

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def max_starting(self) -> int:
        return self._max_starting

    def stats(self) -> dict:
        """Live occupancy snapshot for status surfaces."""
        busy = max(0, self._created - len(self._idle))
        return {
            "workers": self._created,
            "idle": len(self._idle),
            "busy": busy,
            "max": self._max_workers,
            "starting_max": self._max_starting,
        }

    async def _new_worker(self) -> PoolWorker:
        w = self._factory()
        await w.start()
        return w

    def _sync_protection(self, worker: PoolWorker) -> None:
        """Align the sweep-protection shield with a worker's *current* PID.

        Called after ``start()`` and after every ``reset()`` (which respawns the
        process under a new PID). Unregisters the previously shielded PID and
        registers the new one. Idempotent and best-effort — a worker without a
        ``pid()`` is left unshielded (no-op)."""
        key = id(worker)
        new = _worker_pid(worker)
        old = self._protected.get(key)
        if old == new:
            return
        if old is not None:
            unregister_protected_pid(old)
            self._protected.pop(key, None)
        if new is not None:
            register_protected_pid(new)
            self._protected[key] = new

    def _release_protection(self, worker: PoolWorker) -> None:
        """Drop a worker's sweep-protection shield (worker is being retired)."""
        old = self._protected.pop(id(worker), None)
        if old is not None:
            unregister_protected_pid(old)

    async def _retire(self, worker: PoolWorker) -> None:
        """Single retirement path: release the shield, then destroy the worker.

        Ordering matters — we unregister BEFORE shutdown so that even if
        ``shutdown()`` raises, the (soon-dead) PID isn't left shielded and thus
        able to protect an unrelated process that later recycles the PID."""
        self._release_protection(worker)
        await _safe_shutdown(worker)

    async def _get_or_create(self) -> tuple[PoolWorker, bool]:
        """Return (worker, reused). Caller already holds a task-sema permit."""
        reap: list[PoolWorker] = []
        need_new = False
        worker: Optional[PoolWorker] = None
        async with self._lock:
            while self._idle:
                cand = self._idle.pop()
                if cand.is_alive():
                    worker = cand
                    break
                # Dead idle worker: drop it and free its slot so we can replace it.
                self._created -= 1
                reap.append(cand)
            if worker is None:
                self._created += 1
                need_new = True
        for dead in reap:
            await self._retire(dead)
        if not need_new:
            assert worker is not None
            return worker, True
        # Cold-start a new worker, throttled to max_starting simultaneous launches.
        try:
            async with self._start_sema:
                worker = await self._new_worker()
                # Shield the freshly-spawned process from the orphan sweep the
                # moment it exists (before it can outlive a sweep cycle).
                self._sync_protection(worker)
        except BaseException:
            async with self._lock:
                self._created -= 1
            raise
        return worker, False

    async def acquire(self) -> tuple[PoolWorker, bool]:
        """Acquire a worker (blocks while all ``max_workers`` are busy).

        Returns (worker, reused). The caller MUST ``release`` it afterwards."""
        if self._closing:
            raise RuntimeError(f"{self._name} is shutting down")
        await self._task_sema.acquire()
        ok = False
        try:
            worker, reused = await self._get_or_create()
            ok = True
            return worker, reused
        finally:
            if not ok:
                # creation failed — give the concurrency permit back.
                self._task_sema.release()

    async def release(self, worker: PoolWorker, healthy: bool = True) -> None:
        """Return a worker to the idle set, or retire it if unhealthy/closing."""
        try:
            if healthy and not self._closing and worker.is_alive():
                async with self._lock:
                    self._idle.append(worker)
            else:
                async with self._lock:
                    self._created -= 1
                await self._retire(worker)
        finally:
            self._task_sema.release()

    async def send(
        self,
        prompt: str,
        timeout: Optional[float] = None,
        reset: bool = True,
    ) -> str:
        """Acquire a worker, (reset it if reused), run the task, release it.

        ``reset`` gives the next task a clean slate; it is skipped for a
        freshly-created worker (already clean)."""
        if timeout is None:
            timeout = self._default_timeout
        worker, reused = await self.acquire()
        healthy = True
        try:
            if reset and reused:
                await worker.reset()
                # reset() respawns the process under a new PID — re-shield it.
                self._sync_protection(worker)
            return await worker.send_message(prompt, timeout=timeout)
        except BaseException:
            healthy = False
            raise
        finally:
            await self.release(worker, healthy)

    async def shutdown(self) -> None:
        """Retire all idle workers. In-flight workers retire on their release."""
        self._closing = True
        async with self._lock:
            idle = self._idle[:]
            self._idle.clear()
            self._created -= len(idle)
        for w in idle:
            await self._retire(w)
        logger.info("%s shut down (%d idle workers retired)", self._name, len(idle))

    async def __aenter__(self) -> "WorkerPool":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.shutdown()
