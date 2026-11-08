"""The two goal facts a Crewmates roster row carries, and the refresh that keeps them current.

``GET /api/members`` (:mod:`kiro_crew.dashboard.handlers.members_roster`) ships
``has_goal`` and ``heartbeat_on`` on every row so the roster and the crewmate
switcher can say who has a goal and who is on watch without opening each mate's
Goals and Schedules tabs. Both are read off the member's own goal record
(:mod:`kiro_crew.member_goals`), one file per member, in one thread hop for the
whole roster (:func:`goal_facts_for_rows`).

A goal record changes rarely -- the owner's Goals or Schedules tab, a card the
mate's turn applied, a check-in turning itself off when the last goal goes -- and
nothing else announces those writes to the dashboard. :func:`start_roster_refresh`
registers one :func:`kiro_crew.member_goals.add_change_listener` that pushes an
``agents`` refresh frame on the serving loop, which is the frame the roster query
already re-reads on (its key sits under ``kirocrew-agents``). The listener runs on
the writing thread, so the push is handed to the loop, as the crewmate feed does.
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import Any

from kiro_crew import member_goals

logger = logging.getLogger("kiro_crew.dashboard.members_roster_goals")

#: Row key for "the member has at least one goal".
FIELD_HAS_GOAL = "has_goal"
#: Row key for "the member's heartbeat check-in is on" (it acts on its own).
FIELD_HEARTBEAT_ON = "heartbeat_on"

#: The refresh kind the roster query re-reads on.
_REFRESH_KIND = "agents"


def goal_facts(slug: str, member: str) -> dict[str, bool]:
    """``{has_goal, heartbeat_on}`` for *member*'s record; both false when it has none.

    An unreadable record (:class:`~kiro_crew.member_goals.GoalsUnreadable`) reads as
    no facts rather than failing the roster: the row still renders, and the Goals
    tab is where that record's fault is reported.
    """
    try:
        rec = member_goals.read_goals(slug, member)
    except member_goals.GoalsUnreadable:
        logger.debug("goal record for %r unreadable; roster row carries no goal facts", slug)
        return {FIELD_HAS_GOAL: False, FIELD_HEARTBEAT_ON: False}
    schedules = rec.get("schedules") or {}
    return {
        FIELD_HAS_GOAL: member_goals.has_goal(rec),
        FIELD_HEARTBEAT_ON: bool(schedules.get(member_goals.CADENCE_HEARTBEAT) is True),
    }


def goal_facts_for_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    """The goal facts of every row, keyed by member NAME. File IO: run it off the loop.

    Keyed by name rather than slug because a slug is a lossy fold: two names can
    share one, and ``read_goals`` already answers the empty record for a name that
    does not own the record stored under it.
    """
    return {row["name"]: goal_facts(row["slug"], row["name"]) for row in rows}


_lock = threading.Lock()
_registered = False
_state_ref: Any = None


def _on_goal_written(slug: str, member: str, rec: dict[str, Any]) -> None:
    state = _state_ref() if _state_ref is not None else None
    if state is None:
        return
    loop = getattr(state, "serving_loop", None)
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(state.push_refresh, _REFRESH_KIND)
    except RuntimeError:
        logger.debug("roster refresh for %r's goal write after the loop closed", slug)


def start_roster_refresh(state: Any) -> None:
    """Push an ``agents`` refresh after every goal write from now on. Once per process."""
    global _state_ref, _registered
    with _lock:
        _state_ref = weakref.ref(state)
        if not _registered:
            member_goals.add_change_listener(_on_goal_written)
            _registered = True
