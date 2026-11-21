"""Per-phase timing of one script-hook fire, logged only when the fire is slow.

When one hook's fire, matcher to verdict, takes ``SLOW_FIRE_WARN_MS`` or more,
:meth:`HookFireTiming.warn_if_slow` logs one warning naming every phase. Each
thread hop reads ``wait`` (until a pool worker started it), ``run`` (in the worker)
and ``resume`` (until the event loop resumed the fire), so a saturated pool or a
stalled event loop shows apart from slow work. ``other`` is the fire's time outside
every phase; ``gc runs`` counts collections per generation (0/1/2) during the fire.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeVar

_T = TypeVar("_T")

#: A hook fire at or above this many milliseconds, matcher to verdict, logs one
#: warning with its phases.
SLOW_FIRE_WARN_MS = 1000.0


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

    def begin(self, name: str) -> FirePhase:
        phase = FirePhase(name)
        self._phases.append(phase)
        return phase

    @staticmethod
    def end(phase: FirePhase) -> None:
        phase.ended = time.perf_counter()

    async def hop(self, name: str, fn: Callable[..., _T], /, *args: Any) -> _T:
        """``asyncio.to_thread(fn, *args)``, timed as the phase *name*."""
        phase = self.begin(name)
        try:
            return await asyncio.to_thread(phase.worker(fn), *args)
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


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


#: Set by ``ScriptHookStore.fire`` around one hook's run, so ``run_script_hook`` adds
#: its phases to the fire's line; a direct call is not a fire and logs nothing.
CURRENT_FIRE_TIMING: ContextVar[HookFireTiming | None] = ContextVar(
    "kiro_crew_hook_fire_timing", default=None
)
