"""The Heartbeat's timing on a crewmate's goal record: its interval and its hours.

The Heartbeat wakes the crewmate every :data:`HEARTBEAT_INTERVALS` minutes
(RFC "Crewmate heartbeat"). Its hours are an optional window of whole hours,
``active_from`` up to ``active_until`` in the record's timezone: wakes start at
``active_from`` and none starts at or after ``active_until``. The whole day
(:data:`DAY_START` to :data:`DAY_END`) is the default and keeps the Heartbeat
running around the clock. A window never crosses midnight.

:mod:`kiro_crew.member_goals` re-exports every name here, and that is the path
callers import.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

#: The heartbeat's intervals, in minutes, and the one a record without one reads as.
#: Each wake is a full model turn over the crewmate's whole context, and a
#: provider's prompt cache lives minutes, not half-hours, so no interval here reuses
#: the last wake's cache: credits scale with the number of wakes alone. Hence a
#: 30-minute floor and an hourly default (RFC "Crewmate heartbeat", Cost and cache).
HEARTBEAT_INTERVALS = (30, 60, 120, 240)
DEFAULT_HEARTBEAT_INTERVAL = 60
#: The Heartbeat's hours, as whole hours of the day: ``active_from`` is 0-23 and
#: ``active_until`` 1-24, and the whole day is the default.
DAY_START = 0
DAY_END = 24


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_heartbeat_interval(value: Any) -> bool:
    """Whether *value* is one of :data:`HEARTBEAT_INTERVALS` (an int, never a bool)."""
    return _is_int(value) and value in HEARTBEAT_INTERVALS


def is_active_from(value: Any) -> bool:
    """Whether *value* is an hour the Heartbeat's window can start at (0-23)."""
    return _is_int(value) and DAY_START <= value < DAY_END


def is_active_until(value: Any) -> bool:
    """Whether *value* is an hour the Heartbeat's window can end at (1-24)."""
    return _is_int(value) and DAY_START < value <= DAY_END


def active_hours(schedules: dict[str, Any]) -> tuple[int, int]:
    """The Heartbeat's ``(from, until)`` hours in *schedules*; the whole day when unset or invalid."""
    start: Any = schedules.get("active_from")
    end: Any = schedules.get("active_until")
    if is_active_from(start) and is_active_until(end) and start < end:
        return int(start), int(end)
    return DAY_START, DAY_END


def in_active_hours(schedules: dict[str, Any], now: float, tz: tzinfo) -> bool:
    """Whether the epoch *now*, read in *tz*, falls inside the Heartbeat's hours.

    The cron hour field only says when a wake falls due. A run the timer starts
    late (a host that slept past its tick, a delayed timer) is checked against
    this at dispatch, so no wake starts outside the hours.
    """
    start, end = active_hours(schedules)
    return start <= datetime.fromtimestamp(now, tz).hour < end


def interval_label(minutes: Any) -> str:
    """The one way a heartbeat interval is written: ``"30 min"``, ``"1 hr"``, ``"2 hr"``.

    The card, the tool text and the wake prompt use it, and the Goals tab's
    ``fmtInterval`` gives the same English words (``Intl`` short units), so a
    user reads one wording everywhere. A value off the list reads as the default.
    """
    value = minutes if is_heartbeat_interval(minutes) else DEFAULT_HEARTBEAT_INTERVAL
    return f"{value} min" if value < 60 else f"{value // 60} hr"


def wakes_per_day(minutes: Any, active_from: int = DAY_START, active_until: int = DAY_END) -> int:
    """The most wakes a heartbeat at *minutes* makes in a day: 48, 24, 12 or 6 all day long.

    Inside a window of hours it counts the wakes that start in it: wakes run
    on the clock's marks from *active_from*, so a part interval counts as one.
    The bound the user consents to on the card, matching the Schedules tab's
    ``wakesPerDay``. A value off the list reads as the default.
    """
    value = minutes if is_heartbeat_interval(minutes) else DEFAULT_HEARTBEAT_INTERVAL
    start, end = active_hours({"active_from": active_from, "active_until": active_until})
    return -(-(end - start) * 60 // value)


def interval_choices() -> str:
    """Every interval in :func:`interval_label` words, for tool and card text.

    Built from :data:`HEARTBEAT_INTERVALS`, so the text a model reads can never
    offer an interval the validator refuses: ``"30 min, 1 hr (the default), 2 hr or 4 hr"``.
    """
    words = [
        interval_label(m) + (" (the default)" if m == DEFAULT_HEARTBEAT_INTERVAL else "")
        for m in HEARTBEAT_INTERVALS
    ]
    return ", ".join(words[:-1]) + " or " + words[-1]


def interval_minutes_text() -> str:
    """:data:`HEARTBEAT_INTERVALS` as the numbers a tool call sends: ``"30, 60, 120 or 240"``."""
    nums = [str(m) for m in HEARTBEAT_INTERVALS]
    return ", ".join(nums[:-1]) + " or " + nums[-1]
