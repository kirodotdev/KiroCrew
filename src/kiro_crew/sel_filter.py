"""Pick security events by field and time for ``kirocrew security events``.

A row is kept when each field the caller named equals the row's own value (plain string
equality, no pattern). The time window is the one ``SecurityEventLog.recent`` already
applies, so the log keeps a single copy of that rule.

``recent`` applies its row limit BEFORE any field test, so a field filter is run over a
bounded scan of the window (``scan_limit`` rows, newest first) and the limit is cut
afterwards. A scan that used its whole budget says so (``Selection.scan_capped``): older
rows in the window were not looked at, and a narrower ``since``/``until`` reaches them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import NamedTuple, Protocol

#: Rows one CLI run may look at when a field filter is given.
CLI_SCAN_LIMIT = 50_000

#: Filter name (flag) -> the key a log row carries it under.
FIELD_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "operation": "operation",
        "outcome": "outcome",
        "source": "source",
        "caller": "caller_identity",
    }
)


class _Reader(Protocol):
    def recent(
        self, limit: int = ..., *, since: datetime | None = ..., until: datetime | None = ...
    ) -> list[dict]: ...


class Selection(NamedTuple):
    """Rows newest first, and whether the scan stopped at its budget."""

    rows: list[dict]
    scan_capped: bool


def select_events(
    log: _Reader,
    *,
    limit: int,
    filters: dict[str, str],
    since: datetime | None,
    until: datetime | None,
    scan_limit: int,
) -> Selection:
    """Up to *limit* rows of *log* matching every non-empty filter, newest first.

    With no field filter the log's own ``recent`` answers and nothing is scanned past
    *limit*.
    """
    wanted = [(FIELD_KEYS[name], value) for name, value in filters.items() if value]
    if not wanted:
        return Selection(log.recent(limit=limit, since=since, until=until), False)
    scanned = log.recent(limit=scan_limit, since=since, until=until)
    rows = [row for row in scanned if all(row.get(key) == value for key, value in wanted)]
    return Selection(rows[:limit], len(scanned) >= scan_limit and len(rows) < limit)
