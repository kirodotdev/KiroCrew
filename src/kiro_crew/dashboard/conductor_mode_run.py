"""Whether a conductor mode run has settled: done, stopped, or still going.

The server half of the Board's run line and of the Sessions row's "Done": the two
facts only the gateway holds, the ledger's item states and whether the conductor's
patrol loop is still on. A conductor ends a run by sending its final report and then
stopping its patrol (``_LEDGER_CONDUCTOR_RULES["patrol_final_report"]``), so a board
with items and no active loop on its slot is settled: ``done`` when every item is
closed, ``stopped`` when some are still open. Nothing is stored, so a new goal, a
re-armed patrol or a reopened item leaves it on the next read. The live half (a turn
running, an approval, Pause all) is the dashboard's, which composes it first.

A stop the conductor made and one a person made both record ``USER_STOP`` on the
monitor, so the verdict never says who stopped it. Board-generic: a Bot's board gets
the same answer.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from kiro_crew.work_vocab import WORK_ITEM_STATES

#: The closed states that are not ``accepted``. From the shared vocabulary, so this
#: module stays off the store's import surface.
_CLOSED_NOT_ACCEPTED: Final[frozenset[str]] = frozenset(WORK_ITEM_STATES) - {"open", "accepted"}

#: An active loop (any watch) is on the conductor's slot.
PATROL_ON: Final[str] = "on"
#: No active loop: none was armed, it was stopped, or it ended another way.
PATROL_OFF: Final[str] = "off"
#: The loop ended at a time or budget bound.
PATROL_LIMIT: Final[str] = "limit"

#: A legacy loop's stop reasons that are a time or budget bound.
_LIMIT_REASONS: Final[frozenset[str]] = frozenset({"cycle_cap", "runtime_budget"})


def item_counts(states: Iterable[object], forgotten: Any = None) -> tuple[int, int, int]:
    """``(open, accepted, not_done)`` over item *states*, plus a fold's *forgotten* closes."""
    open_items = accepted = not_done = 0
    for state in states:
        if state == "accepted":
            accepted += 1
        elif state in _CLOSED_NOT_ACCEPTED:
            not_done += 1
        else:
            open_items += 1
    if isinstance(forgotten, dict):
        accepted += _count(forgotten.get("accepted"))
        not_done += _count(forgotten.get("rejected")) + _count(forgotten.get("abandoned"))
    return open_items, accepted, not_done


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def patrol_of(svc: Any, keys: Iterable[str]) -> str:
    """The patrol on the first of *keys* that holds a loop. CALL ON THE EVENT LOOP.

    ``get_by_slot`` walks the service's live loop table, which its own coroutines
    mutate. No service, no loop, or an unreadable one is ``off``.
    """
    getter = getattr(svc, "get_by_slot", None)
    if not callable(getter):
        return PATROL_OFF
    for key in dict.fromkeys(k for k in keys if k):
        try:
            loop = getter(key)
        except Exception:  # noqa: BLE001 - a loop-table read must not fail the board
            continue
        if loop is None:
            continue
        if getattr(loop, "active", False):
            return PATROL_ON
        from kiro_crew.monitoring.models import MonitorOutcome

        monitor = getattr(loop, "monitor", None)
        if monitor is not None and getattr(monitor, "outcome", None) is not None:
            return PATROL_LIMIT if monitor.outcome is MonitorOutcome.BUDGET else PATROL_OFF
        return PATROL_LIMIT if getattr(loop, "stopped_reason", "") in _LIMIT_REASONS else PATROL_OFF
    return PATROL_OFF


def run_facts(
    *, open_items: int, accepted: int, not_done: int, patrol: str, goal_items: int | None = None
) -> dict[str, Any]:
    """The ``run`` field: ``settled`` is ``""`` (still going, or no item yet),
    ``"done"`` or ``"stopped"``; ``at_limit`` marks a stop at a time or budget bound.

    *goal_items* is the header's ``goal_items_used`` when the caller holds it: a new
    goal that has created no item yet is not settled, whatever the earlier goal's
    items say. ``None`` (the row, whose fold carries no goal start) skips the check.
    """
    # shortcut: without goal_items the counts span the whole board, so the Sessions
    # row of a new goal with no item yet reads the previous goal's verdict until its
    # first item; scope it too once the work fold records the goal's start.
    if open_items + accepted + not_done == 0 or patrol == PATROL_ON or goal_items == 0:
        settled = ""
    else:
        settled = "done" if open_items == 0 else "stopped"
    return {
        "settled": settled,
        "open": open_items,
        "accepted": accepted,
        "not_done": not_done,
        "at_limit": settled == "stopped" and patrol == PATROL_LIMIT,
    }
