"""A crewmate Heartbeat's active hours, as its goal record keeps them.

``active_from`` (a whole hour, 0-23) up to, not including, ``active_to``
(1-24), in the zone the Heartbeat's schedule runs in: the record's timezone
when it names one, else the gateway's default zone (the host's unless one is
configured). Outside them no wake is scheduled. 0 to 24, the default, is the
whole day, and a window never crosses midnight.
:mod:`kiro_crew.member_goals` stores and validates them, and
:mod:`kiro_crew.dashboard.goal_reminder` narrows the Heartbeat's cron hour field
to them.
"""

from __future__ import annotations

from typing import Any

#: The whole day: the active hours of a Heartbeat that has none.
ACTIVE_HOURS_ALL_DAY = (0, 24)


def _is_hour(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def window(hours: Any) -> tuple[int, int]:
    """*hours* as ``(start, end)`` when the pair is a window of active hours, else the whole day."""
    if isinstance(hours, (list, tuple)) and len(hours) == 2:
        start, end = hours
        if _is_hour(start, 0, 23) and _is_hour(end, 1, 24) and start < end:
            return start, end
    return ACTIVE_HOURS_ALL_DAY


def active_hours(schedules: dict[str, Any]) -> tuple[int, int]:
    """The active hours a record's *schedules* hold; the whole day when they hold none."""
    return window((schedules.get("active_from"), schedules.get("active_to")))


def hours_error(changes: dict[str, Any], current: dict[str, Any] | None = None) -> str | None:
    """Why *changes* to a record's schedules give no active hours, or ``None`` when they do.

    Each end the changes supply must be a whole hour in its range. With
    *current*, the stored schedules read under the write lock, the window the
    changes leave must also start before it ends.
    """
    if "active_from" in changes and not _is_hour(changes["active_from"], 0, 23):
        return "active_from must be a whole hour, 0 to 23"
    if "active_to" in changes and not _is_hour(changes["active_to"], 1, 24):
        return "active_to must be a whole hour, 1 to 24"
    if current is not None:
        merged = {**current, **changes}
        if merged["active_from"] >= merged["active_to"]:
            return "active hours must start before they end"
    return None
