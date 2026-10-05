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
answer must not describe them as a capacity queue: ``low_memory`` is re-checked
after the admit wait for as long as the host stays below the bar (a store
DEFERRAL for a durable row, a stamped window wait for one with no row), bounded
by ``agent.subagent_queue_max_wait_secs`` and ended with
:data:`QUEUED_WAIT_EXPIRED_TEXT`; and ``memory_pressure`` waits in the capacity
window, bounded by the same key (subagent.md, *macOS: the kernel memory-pressure
hold*). There is no pause kind: the adaptive controller never takes the
execution cap to 0, because it does not read free memory or loop lag (the floor
owns memory).

The memory posture tier (``resource_critical_gb``) is not one of them: spawns
are not gated on it, only on the floor (``low_memory``). Cron still defers its
firings on posture, through its own gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

QUEUED_REASON_CONCURRENCY_LIMIT = "concurrency_limit"
QUEUED_REASON_LOW_MEMORY = "low_memory"
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

#: The terminal a deferred spawn ends with once it has waited for memory longer
#: than ``agent.subagent_queue_max_wait_secs``: its ``error``, delivered to the
#: parent like any other result. Owner wording; one spelling for the report, the
#: store row and every test that reads either.
QUEUED_WAIT_EXPIRED_TEXT = "never started: waiting for memory"

__all__ = [
    "DEFERRED_QUEUED_REASONS",
    "MEMORY_PRESSURE_NEVER_STARTED",
    "MEMORY_PRESSURE_DETAIL",
    "MEMORY_PRESSURE_PHRASE",
    "MEMORY_PRESSURE_RECHECK_SECS",
    "QUEUED_REASON_CONCURRENCY_LIMIT",
    "QUEUED_REASON_LOW_MEMORY",
    "QUEUED_REASON_MEMORY_PRESSURE",
    "QUEUED_WAIT_EXPIRED_TEXT",
]


#: What a queued record's wait kind means, for one the gateway sent without the
#: gate's own sentence: a capacity wait, or a deferral already past its admit
#: wait and not yet re-checked.
QUEUED_KIND_TEXT: dict[str, str] = {
    QUEUED_REASON_LOW_MEMORY: "not enough free memory to start it",
    QUEUED_REASON_CONCURRENCY_LIMIT: "waiting for a free slot behind the concurrency limit",
    # A held row has no ``deferred`` event; the hold is read live, so its kind
    # carries the gate's own sentence.
    QUEUED_REASON_MEMORY_PRESSURE: MEMORY_PRESSURE_DETAIL,
}

#: ``resuming_reason`` of a queued record that already ran: a ``recovering``
#: row after a gateway restart, or a ``retry_wait`` row that ran before.
RESUMING_AFTER_RESTART = "gateway_restart"
RESUMING_RETRY = "retry"

#: A queued record that already ran (``resuming``), by ``resuming_reason``.
RESUMING_TEXT: dict[str, str] = {
    RESUMING_AFTER_RESTART: "waiting to resume after a gateway restart",
    RESUMING_RETRY: "waiting to retry",
}


def queued_wait_text(record: Mapping[str, Any]) -> str:
    """Why an accepted spawn with no run waits, in words (NOT redacted).

    One wording for every reader of a queued record (``spawn_status``,
    ``spawn_list``, ``spawn_sub_agents``, ``kirocrew spawn list``): a run that
    already started says what it waits to resume; otherwise the gate's own
    sentence, else the wait kind.
    """
    if record.get("resuming") is True:
        return RESUMING_TEXT.get(str(record.get("resuming_reason") or ""), "waiting to resume")
    detail = str(record.get("reason_detail") or "").strip()
    if detail:
        return detail
    return QUEUED_KIND_TEXT.get(str(record.get("reason") or ""), "waiting to start")
