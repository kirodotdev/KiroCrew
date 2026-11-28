"""Per-phase timing of one script-hook fire, logged only when the fire is slow.

When one hook's fire, matcher to verdict, takes ``SLOW_FIRE_WARN_MS`` or more,
:meth:`HookFireTiming.warn_if_slow` logs one warning naming every phase. Each
thread hop reads ``wait`` (until a pool worker started it), ``run`` (in the worker)
and ``resume`` (until the event loop resumed the fire), so a saturated pool or a
stalled event loop shows apart from slow work. ``other`` is the fire's time outside
every phase; ``gc runs`` counts collections per generation (0/1/2) during the fire.

:class:`EventFireTiming` times one whole ``ScriptHookStore.fire`` call, so an event
whose hooks are each under the threshold still logs when together they are not.

A hop runs on :func:`hook_hop_executor`, not on the loop's default executor. A
fire makes several hops per hook, one hook after another, before a tool call is
approved, so on the shared default pool every burst of unrelated work (dashboard
reads, tree walks) was paid once per hop per hook. On its own pool a hop waits
only behind other hook hops, each a few milliseconds of work.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import gc
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from typing import Any, TypeVar

from kiro_crew.enqueue_only_pool import EnqueueOnlyThreadPoolExecutor

_T = TypeVar("_T")

#: A hook fire at or above this many milliseconds, matcher to verdict, logs one
#: warning with its phases; an event's whole fire at or above it logs one more.
SLOW_FIRE_WARN_MS = 1000.0

#: A healthy hop (a governance check, the deny-list vet, a matcher, the status
#: write) takes milliseconds, so this bounds how many can be wedged at once, not
#: throughput: a started call cannot be cancelled and keeps its worker.
_MAX_HOOK_HOP_WORKERS = 8

_pool: ThreadPoolExecutor | None = None  # process-cache-ok: worker threads, no per-test data
_pool_lock = threading.Lock()


def hook_hop_executor() -> ThreadPoolExecutor:
    """Return the process-wide hook hop pool, creating it on first use.

    Threads are named ``mc-hookhop``. Kept off the loop's default executor so a
    busy default pool cannot delay each step of every hook before approval.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = EnqueueOnlyThreadPoolExecutor(
                    max_workers=_MAX_HOOK_HOP_WORKERS, thread_name_prefix="mc-hookhop"
                )
                atexit.register(_pool.shutdown, wait=False, cancel_futures=True)
    return _pool


def _gc_collections() -> list[int]:
    return [int(gen.get("collections", 0)) for gen in gc.get_stats()]


class FirePhase:
    """One named step of a hook fire: its wall time and, for a hop, its worker span."""

    __slots__ = ("name", "started", "ended", "worker_started", "worker_ended")

    def __init__(self, name: str) -> None:
        self.name = name
        self.started = time.perf_counter()
        self.ended: float | None = None
        self.worker_started: float | None = None
        self.worker_ended: float | None = None

    def worker(self, fn: Callable[..., _T]) -> Callable[..., _T]:
        """Wrap *fn* so the thread that runs it records when it started and returned."""

        def run(*args: Any, **kwargs: Any) -> _T:
            self.worker_started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.worker_ended = time.perf_counter()

        return run

    def describe(self, now: float) -> str:
        end = self.ended if self.ended is not None else now
        text = f"{self.name}={_ms(end - self.started)}ms"
        if self.worker_started is None:
            return text
        worker_end = self.worker_ended if self.worker_ended is not None else now
        wait = _ms(self.worker_started - self.started)
        run = _ms(worker_end - self.worker_started)
        resume = _ms(end - worker_end)
        return f"{text} (wait {wait}, run {run}, resume {resume})"


class HookFireTiming:
    def __init__(self, subject: str, event: str) -> None:
        self.subject = subject
        self.event = event
        self._started = time.perf_counter()
        self._gc_at_start = _gc_collections()
        self._phases: list[FirePhase] = []

    def elapsed(self) -> float:
        """Seconds since this hook's fire started."""
        return time.perf_counter() - self._started

    def begin(self, name: str) -> FirePhase:
        phase = FirePhase(name)
        self._phases.append(phase)
        return phase

    @staticmethod
    def end(phase: FirePhase) -> None:
        phase.ended = time.perf_counter()

    async def hop(self, name: str, fn: Callable[..., _T], /, *args: Any) -> _T:
        """Run ``fn(*args)`` on :func:`hook_hop_executor`, timed as the phase *name*.

        Like ``asyncio.to_thread``, the call runs in a copy of the caller's context.
        """
        phase = self.begin(name)
        try:
            bound = functools.partial(copy_context().run, phase.worker(fn), *args)
            return await asyncio.get_running_loop().run_in_executor(hook_hop_executor(), bound)
        finally:
            self.end(phase)

    def warn_if_slow(self, log: logging.Logger) -> None:
        """Log one warning naming every phase when the fire took the threshold or longer.

        A phase still open (a step that raised or timed out) counts until now.
        """
        now = time.perf_counter()
        total = now - self._started
        if total * 1000 < SLOW_FIRE_WARN_MS:
            return
        in_phases = sum((p.ended if p.ended is not None else now) - p.started for p in self._phases)
        parts = ", ".join(p.describe(now) for p in self._phases) or "no phases"
        gc_runs = "/".join(
            str(after - before) for before, after in zip(self._gc_at_start, _gc_collections())
        )
        log.warning(
            "Slow hook fire: %s (%s) took %dms: %s; other=%dms; gc runs=%s",
            self.subject,
            self.event,
            _ms(total),
            parts,
            _ms(max(total - in_phases, 0.0)),
            gc_runs,
        )


class EventFireTiming:
    """One event's whole fire: every hook that ran, then the ``hooks.json`` status write."""

    def __init__(self, event: str) -> None:
        self.event = event
        self._started = time.perf_counter()
        self._hooks: list[tuple[str, float]] = []

    def add(self, hook: HookFireTiming) -> None:
        """Record *hook* as one that ran, with its total up to now."""
        self._hooks.append((hook.subject, hook.elapsed()))

    def warn_if_slow(self, log: logging.Logger) -> None:
        """Log one warning when hooks ran and the whole fire took the threshold or longer.

        ``other`` is the fire's time in no hook that ran: matchers that did not
        match, the bookkeeping between hooks, and the status write.
        """
        if not self._hooks:
            return
        total = time.perf_counter() - self._started
        if total * 1000 < SLOW_FIRE_WARN_MS:
            return
        in_hooks = sum(secs for _, secs in self._hooks)
        log.warning(
            "Slow hook event: %s took %dms; hooks=%d: %s; other=%dms",
            self.event,
            _ms(total),
            len(self._hooks),
            ", ".join(f"{name}={_ms(secs)}ms" for name, secs in self._hooks),
            _ms(max(total - in_hooks, 0.0)),
        )


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


#: Set by ``ScriptHookStore.fire`` around one hook's run, so ``run_script_hook`` adds
#: its phases to the fire's line; a direct call is not a fire and logs nothing.
CURRENT_FIRE_TIMING: ContextVar[HookFireTiming | None] = ContextVar(
    "kiro_crew_hook_fire_timing", default=None
)
