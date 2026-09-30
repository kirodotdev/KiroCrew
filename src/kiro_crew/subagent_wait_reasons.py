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
seconds. The other three are DEFERRALS: the row is re-checked on the pump's next
eligible pass and can wait for as long as the host stays below the bar, which is
why the UI and every tool answer must not describe them as a capacity queue.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

QUEUED_REASON_CONCURRENCY_LIMIT = "concurrency_limit"
QUEUED_REASON_LOW_MEMORY = "low_memory"
QUEUED_REASON_POSTURE_CRITICAL = "posture_critical"
QUEUED_REASON_ADAPTIVE_CAP_ZERO = "adaptive_cap_zero"

#: The kinds a caller is told ``queued`` for (rather than ``spawned``): the row
#: is accepted but may not run for a long time.
DEFERRED_QUEUED_REASONS: frozenset[str] = frozenset(
    {
        QUEUED_REASON_LOW_MEMORY,
        QUEUED_REASON_POSTURE_CRITICAL,
        QUEUED_REASON_ADAPTIVE_CAP_ZERO,
    }
)

__all__ = [
    "DEFERRED_QUEUED_REASONS",
    "QUEUED_REASON_ADAPTIVE_CAP_ZERO",
    "QUEUED_REASON_CONCURRENCY_LIMIT",
    "QUEUED_REASON_LOW_MEMORY",
    "QUEUED_REASON_POSTURE_CRITICAL",
    "adaptive_pause_text",
]


def adaptive_pause_text(configured_cap: int) -> str:
    """The gate's sentence for a spawn held while the effective cap is 0.

    One wording for the accept answer and for every later read of the row, which
    has no ``deferred`` event to carry it: the pause is the manager's state, not
    the row's.
    """
    return (
        "dispatch paused: the host is low on memory or overloaded, so no new "
        f"subagent starts until it recovers (configured cap {configured_cap}, "
        "effective cap 0)"
    )


#: What a queued record's wait kind means, for one the gateway sent without the
#: gate's own sentence: a capacity wait, or a deferral already past its admit
#: wait and not yet re-checked.
QUEUED_KIND_TEXT: dict[str, str] = {
    QUEUED_REASON_LOW_MEMORY: "not enough free memory to start it",
    QUEUED_REASON_POSTURE_CRITICAL: "host memory is critically low",
    QUEUED_REASON_ADAPTIVE_CAP_ZERO: (
        "starts are paused while the host is low on memory or overloaded"
    ),
    QUEUED_REASON_CONCURRENCY_LIMIT: "waiting for a free slot behind the concurrency limit",
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
