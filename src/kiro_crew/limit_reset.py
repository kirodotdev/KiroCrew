"""Read the reset time out of a usage-limit sentence.

A spent session, 5-hour or weekly allowance comes back from the backend as one
sentence that ends in when it opens again ("... resets 5:50pm", "... It resets
Monday at 9am", "... resets in 2 hours 10 minutes"). :func:`parse_limit_reset`
turns that clause into an epoch second, so the usage-limit card can show the
time without reading the sentence again.

The clock comes in as an argument; nothing is read from the host except the
local zone for a clock time that names none. A clause it cannot read (a zone
name that does not load included), one that is not ahead, or one beyond
:data:`MAX_RESET_AHEAD_SECS` yields ``None`` with a warning that names the reason
(never the sentence), and the caller keeps what it did without a time. The parser
never raises.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

#: The furthest reset the parser will name. The longest window a backend reports
#: is a week; a little over covers a weekly reset read just after it opened.
MAX_RESET_AHEAD_SECS = 8 * 24 * 3600

_RE_RESET_CLAUSE = re.compile(r"\bresets?\b(?P<tail>[^\n]*)", re.IGNORECASE)
_RE_RELATIVE = re.compile(
    r"\bin\s+(?P<body>(?:\d+\s*(?:d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?)\b\s*"
    r"(?:and\s+|,\s*)?)+)",
    re.IGNORECASE,
)
_RE_RELATIVE_PART = re.compile(r"(\d+)\s*([dhm])", re.IGNORECASE)
_RE_CLOCK = re.compile(
    r"(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s*)?(?:at\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap])?\.?(?(meridiem)m\b\.?)",
    re.IGNORECASE,
)
# A calendar date ("Nov 15", an ISO date, "11/15") is a form this parser does not read.
_RE_CALENDAR_DATE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d"
    r"|\d{4}-\d{2}-\d{2}|\b\d{1,2}/\d{1,2}\b",
    re.IGNORECASE,
)
_RE_ZONE = re.compile(r"\((?P<zone>[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*)\)")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RELATIVE_UNIT_SECS = {"d": 86400, "h": 3600, "m": 60}


def _local_now(tail: str, now: datetime) -> datetime | None:
    """*now* as a wall clock in the zone the clause's time is written in.

    The zone the clause names in parentheses, else *now*'s own when it is a named
    zone, else the host's. The host's is a naive wall clock: ``timestamp()`` on it
    applies the host's rules for that date, so a weekday reset across a DST change
    keeps its clock time, which a fixed UTC offset would not. ``None`` when the
    named zone does not load: a time read in the wrong zone is worse than none.
    """
    named = _RE_ZONE.search(tail)
    if named:
        try:
            return now.astimezone(ZoneInfo(named.group("zone")))
        except (KeyError, ValueError, OSError):
            return None
    if isinstance(now.tzinfo, ZoneInfo):
        return now
    return datetime.fromtimestamp(now.timestamp())


def _relative_secs(tail: str) -> float | None:
    found = _RE_RELATIVE.search(tail)
    if not found:
        return None
    parts = _RE_RELATIVE_PART.findall(found.group("body"))
    return float(sum(int(n) * _RELATIVE_UNIT_SECS[u.lower()] for n, u in parts)) or None


def _clock_at(tail: str, now: datetime) -> datetime | None:
    """The next moment the clause's clock time (and weekday, if any) occurs after *now*."""
    if _RE_CALENDAR_DATE.search(tail):
        return None
    # A bare number is not a time: it needs a meridiem or a minutes part.
    clock = next(
        (
            c
            for c in _RE_CLOCK.finditer(tail)
            if c.group("meridiem") or c.group("minute") is not None
        ),
        None,
    )
    if clock is None:
        return None
    hour = int(clock.group("hour"))
    minute = int(clock.group("minute") or 0)
    meridiem = (clock.group("meridiem") or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    local_now = _local_now(tail, now)
    if local_now is None:
        return None
    when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = (clock.group("day") or "").lower()
    if day:
        when += timedelta(days=(_WEEKDAYS.index(day) - local_now.weekday()) % 7)
    if when <= local_now:
        when += timedelta(days=7 if day else 1)
    return when


def _parse(text: str, moment: datetime) -> float | None:
    clause = _RE_RESET_CLAUSE.search(text)
    if not clause:
        return None
    tail = clause.group("tail")
    relative = _relative_secs(tail)
    if relative is not None:
        at = moment.timestamp() + relative
    else:
        when = _clock_at(tail, moment)
        if when is None:
            logger.warning("usage-limit reset clause is not a time this parser reads; dropped")
            return None
        at = when.timestamp()
    ahead = at - moment.timestamp()
    if ahead <= 0:
        logger.warning("usage-limit reset time is not ahead; dropped")
        return None
    if ahead > MAX_RESET_AHEAD_SECS:
        logger.warning("usage-limit reset time is more than eight days out; dropped")
        return None
    return at


def parse_limit_reset(text: str, now: datetime | None = None) -> float | None:
    """Epoch seconds at which the limit in *text* opens again, or ``None``.

    *now* must be timezone-aware; it defaults to the host clock. The result is
    the instant the clause names, always in the future and no more than
    :data:`MAX_RESET_AHEAD_SECS` away. A sentence with no "resets" clause
    is ``None`` without a warning; any failure while reading one is ``None``
    with a warning, never an exception.
    """
    try:
        moment = now if now is not None else datetime.now().astimezone()
        return _parse(str(text or ""), moment)
    except Exception as exc:  # a malformed clause must never break the error path
        logger.warning("usage-limit reset time could not be read (%s); dropped", type(exc).__name__)
        return None
