"""Recognize the day header and the time stamps ``append_history`` writes.

Both history writers (V1 day files in ``memory.py``, V2 member rows in
``vector_memory_member.py``) open a day with ``# YYYY-MM-DD`` and put a
``#### HH:MM %Z`` stamp above each entry. Neither line names any work, so the
activity index lists the entry's own first line instead and the dashboard
memory graph draws no node for either. A titled heading such as
``#### 10:00 deploy chat`` is neither, and stays listed.
"""

from __future__ import annotations

import time

_DAY_HEADER_PREFIX = "# "
_STAMP_PREFIX = "#### "
_DATE_LENGTH = len("YYYY-MM-DD")
# The writers print a two-digit ``%H``; a one-digit hour is a stamp too, which
# the dashboard memory graph has always skipped.
_HOUR_LENGTHS = frozenset({1, 2})
_MINUTE_LENGTH = len("MM")
_OFFSET_SIGNS = frozenset("+-")
_OFFSET_DIGIT_COUNTS = frozenset({2, 4})
_UTC_PREFIX = "UTC"
_WINDOWS_NAME_LAST_WORD = "Time"
# What ``%Z`` prints as an abbreviation ("UTC", "ChST", "AEDT"): two to six
# ASCII letters, upper case at both ends.
_ABBREVIATION_LENGTHS = range(2, 7)


def _ascii_digits(text: str) -> bool:
    return text.isascii() and text.isdigit()


def is_history_day_header(line: str) -> bool:
    """Whether ``line`` is the ``# YYYY-MM-DD`` header a history day opens with."""
    if not line.startswith(_DAY_HEADER_PREFIX):
        return False
    rest = line[len(_DAY_HEADER_PREFIX) :]
    date, tail = rest[:_DATE_LENGTH], rest[_DATE_LENGTH:]
    return (
        len(date) == _DATE_LENGTH
        and date[4] == date[7] == "-"
        and _ascii_digits(date[:4] + date[5:7] + date[8:])
        and not tail.strip()
    )


def _stamp_zone(line: str) -> str | None:
    """The text after a ``#### HH:MM`` stamp's clock, or None when ``line`` is no stamp."""
    if not line.startswith(_STAMP_PREFIX):
        return None
    clock, _, zone = line[len(_STAMP_PREFIX) :].rstrip().partition(" ")
    hour, colon, minute = clock.partition(":")
    if not (
        colon
        and len(hour) in _HOUR_LENGTHS
        and len(minute) == _MINUTE_LENGTH
        and _ascii_digits(hour + minute)
    ):
        return None
    return zone


def _is_zone_abbreviation(zone: str) -> bool:
    return (
        len(zone) in _ABBREVIATION_LENGTHS
        and zone.isascii()
        and zone.isalpha()
        and zone[0].isupper()
        and zone[-1].isupper()
    )


def _is_zone_offset(zone: str) -> bool:
    """``+14``, ``-0330`` or ``+05:30``, bare or after ``UTC`` (``UTC-11``)."""
    body = zone.removeprefix(_UTC_PREFIX)
    if body[:1] not in _OFFSET_SIGNS:
        return False
    digits = body[1:]
    if len(digits) == 5 and digits[2] == ":":
        digits = digits[:2] + digits[3:]
    return len(digits) in _OFFSET_DIGIT_COUNTS and _ascii_digits(digits)


def _is_windows_zone_name(zone: str) -> bool:
    """An English Windows name: words joined by single spaces, the last one ``Time``."""
    words = zone.split(" ")
    return (
        len(words) >= 2
        and words[-1] == _WINDOWS_NAME_LAST_WORD
        and all(word.split() == [word] for word in words[:-1])
    )


def is_history_stamp(line: str) -> bool:
    """Whether ``line`` is the ``#### HH:MM %Z`` stamp ``append_history`` wrote.

    The zone is what ``%Z`` printed on the writing host: empty, an abbreviation,
    an offset, an English Windows name, or one of the reading host's own
    ``time.tzname`` names. That last one is the only bound on a localized Windows
    name, so a localized name written by a different host is not recognized and
    its stamp stays listed.
    """
    zone = _stamp_zone(line)
    if zone is None:
        return False
    return (
        not zone
        or zone in time.tzname
        or _is_zone_abbreviation(zone)
        or _is_zone_offset(zone)
        or _is_windows_zone_name(zone)
    )
