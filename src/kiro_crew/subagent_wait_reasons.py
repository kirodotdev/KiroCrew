"""The kinds of WAIT the subagent admission gate labels a queued spawn with.

Deliberately a leaf module -- it imports nothing from ``kiro_crew`` -- so a
surface that only needs to ask "is this wait a deferral?" can import it without
acquiring an edge to :mod:`kiro_crew.subagent`. The channel command layer
(:mod:`kiro_crew.messaging.commands`) is the case in point: it keeps
``kiro_crew.subagent`` duck-typed on purpose, because that module reaches
``kiro_crew.slack`` transitively. :mod:`kiro_crew.subagent` re-exports these names
so its own callers keep reading them from there.

The kinds themselves are a report of a verdict the gate already made; no gate
reads them back. ``concurrency_limit`` is the ordinary wave shape -- a slot is
taken, or the stagger tick has not elapsed -- and clears on its own within
seconds. The others can wait for a long time, which is why the UI and every tool
answer must not describe them as a capacity queue: ``low_memory``,
``posture_critical`` and ``adaptive_cap_zero`` are DEFERRALS re-checked on the
pump's next eligible pass for as long as the host stays below the bar, and
``memory_pressure`` waits in the capacity window, within a bound of its own
(subagent.md, *macOS: the kernel memory-pressure hold*).
"""

from __future__ import annotations

QUEUED_REASON_CONCURRENCY_LIMIT = "concurrency_limit"
QUEUED_REASON_LOW_MEMORY = "low_memory"
QUEUED_REASON_POSTURE_CRITICAL = "posture_critical"
QUEUED_REASON_ADAPTIVE_CAP_ZERO = "adaptive_cap_zero"
#: The macOS kernel reports memory pressure (WARN or worse) while a dedicated
#: child of this gateway is running or warming. Carries no GB figures: the
#: reclaimable figure cleared the floor, so any pair of numbers would contradict
#: the verdict.
QUEUED_REASON_MEMORY_PRESSURE = "memory_pressure"

#: The words every surface opens a kernel memory-pressure report with: the
#: held start's detail, the ``[RESOURCES]`` line and the ``resource_status`` tool.
MEMORY_PRESSURE_PHRASE = "macOS reports memory pressure"
#: Seconds between re-checks of starts the pressure hold keeps, when no slot
#: release re-checks them sooner.
MEMORY_PRESSURE_RECHECK_SECS = 15
#: The held start's detail: the chip, ``POST /api/spawn`` and ``spawn_run`` relay
#: it as the reason the start has not begun.
MEMORY_PRESSURE_DETAIL = (
    f"{MEMORY_PRESSURE_PHRASE}; waiting until it eases or the running agents "
    f"finish (re-checked every {MEMORY_PRESSURE_RECHECK_SECS} s)"
)

#: The kinds a caller is told ``queued`` for (rather than ``spawned``): the row
#: is accepted but may not run for a long time.
DEFERRED_QUEUED_REASONS: frozenset[str] = frozenset(
    {
        QUEUED_REASON_LOW_MEMORY,
        QUEUED_REASON_POSTURE_CRITICAL,
        QUEUED_REASON_ADAPTIVE_CAP_ZERO,
        QUEUED_REASON_MEMORY_PRESSURE,
    }
)

#: The terminal error of a root start the pressure hold kept past its bound: it is
#: ended, never started under the pressure it waited on. It names both ways out,
#: the level easing and a running agent finishing, in the run card's own noun. The wording is the owner's;
#: a surface that groups terminal runs reads the outcome from the code-like
#: prefix instead of restating it (``NEVER_STARTED_PREFIX``, the run card).
MEMORY_PRESSURE_NEVER_STARTED = (
    "never started: waiting for memory (macOS memory pressure did not ease in time); "
    "retry once it eases or a running agent finishes"
)

__all__ = [
    "DEFERRED_QUEUED_REASONS",
    "MEMORY_PRESSURE_NEVER_STARTED",
    "MEMORY_PRESSURE_DETAIL",
    "MEMORY_PRESSURE_PHRASE",
    "MEMORY_PRESSURE_RECHECK_SECS",
    "QUEUED_REASON_ADAPTIVE_CAP_ZERO",
    "QUEUED_REASON_CONCURRENCY_LIMIT",
    "QUEUED_REASON_LOW_MEMORY",
    "QUEUED_REASON_MEMORY_PRESSURE",
    "QUEUED_REASON_POSTURE_CRITICAL",
]
