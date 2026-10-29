"""``kirocrew.crew_mode.*`` -- how Crew Mode is used, in six counters.

Answers four questions and nothing else: how often Crew Mode is turned on and what
share of new chats that is, how many dispatched runs finish, how many steers a run
takes, and how often a crew member is stuck or needs the person. A **run** is one
crew member item a Crew Mode chat dispatched: it starts at the item's create and
finishes at its close. A share is two counters, not a gauge: ``turned_on /
new_chats`` over any window a backend picks.

No attribute carries a session key, an item id, a title, a message, a summary or an
agent name: every value is a member of a closed tuple declared here or a bool, and an
unrecognised one maps to :data:`OTHER`. Telemetry consent gates every emit, because
``emit_counter`` resolves a no-op recorder when telemetry is off, and an emit never
raises into the instrumented path.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.metrics.events import emit_counter

#: One per chat that turns Crew Mode on (a new chat born on a conductor). ``via``
#: says how; ``repeat`` is true when a Crew Mode chat already existed, which is the
#: repeat-use signal.
CREW_MODE_TURNED_ON = "kirocrew.crew_mode.turned_on"

#: One per new chat, with ``crew_mode`` saying whether it was born in Crew Mode.
#: The denominator of the share: ``sum(turned_on) / sum(new_chats)``.
CREW_MODE_NEW_CHATS = "kirocrew.crew_mode.new_chats"

#: One per run (a Crew Mode chat's crew member item) that finished, by how it ended.
CREW_MODE_RUNS_FINISHED = "kirocrew.crew_mode.runs_finished"

#: One per finished run, with the number of person steers it took, bucketed.
CREW_MODE_STEERS_PER_RUN = "kirocrew.crew_mode.steers_per_run"

#: One per person steer into a crew member, by whether it landed on a ledger item.
CREW_MODE_STEERS = "kirocrew.crew_mode.steers"

#: One per crew member report on a Crew Mode chat that says it is stuck or needs the
#: person (``blocked`` or ``question``), by status and reason.
CREW_MODE_WORKER_WAITS = "kirocrew.crew_mode.worker_waits"

OTHER = "other"

#: How Crew Mode was turned on: a new chat born on a conductor, or an existing
#: chat switched onto one.
TURNED_ON_VIA: tuple[str, ...] = ("create", "switch")

#: How a run ended: the ledger's terminal states.
RUN_STATES: tuple[str, ...] = ("accepted", "rejected", "abandoned")

#: Steers one run took.
STEER_BUCKETS: tuple[str, ...] = ("0", "1", "2", "3+")

#: The two report statuses that mean the crew member waits, and why.
WAIT_STATUSES: tuple[str, ...] = ("blocked", "question")
NO_REASON = "none"
WAIT_REASONS: tuple[str, ...] = ("approval", "needs_human", NO_REASON)


def _member(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else OTHER


def steer_bucket(count: Any) -> str:
    """*count* as a member of :data:`STEER_BUCKETS`, or :data:`OTHER`."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return OTHER
    return "3+" if count >= 3 else str(count)


def record_turned_on(via: str, *, repeat: bool) -> None:
    emit_counter(CREW_MODE_TURNED_ON, {"via": _member(via, TURNED_ON_VIA), "repeat": bool(repeat)})


def record_new_chat(*, crew_mode: bool) -> None:
    emit_counter(CREW_MODE_NEW_CHATS, {"crew_mode": bool(crew_mode)})


def record_run_finished(state: Any, steers: Any) -> None:
    emit_counter(CREW_MODE_RUNS_FINISHED, {"state": _member(state, RUN_STATES)})
    emit_counter(CREW_MODE_STEERS_PER_RUN, {"steers": steer_bucket(steers)})


def record_steer(*, on_item: bool) -> None:
    emit_counter(CREW_MODE_STEERS, {"on_item": bool(on_item)})


def record_worker_report(status: Any, reason: Any) -> None:
    """Count a report only when it says the crew member waits; other statuses count nothing."""
    if status not in WAIT_STATUSES:
        return
    emit_counter(
        CREW_MODE_WORKER_WAITS,
        {
            "status": _member(status, WAIT_STATUSES),
            "reason": _member(reason if reason else NO_REASON, WAIT_REASONS),
        },
    )


def board_is_crew_mode(state: Any, board_key: str) -> bool:
    """Whether *board_key* (a ledger key) belongs to a Crew Mode root. No I/O; never raises."""
    try:
        from kiro_crew import crew_mode

        if board_key in crew_mode.known_roots():
            return True
        slot = getattr(state, "_slots", {}).get(board_key)
        return slot is not None and crew_mode.slot_is_crew_mode_root(slot)
    except Exception:
        return False
