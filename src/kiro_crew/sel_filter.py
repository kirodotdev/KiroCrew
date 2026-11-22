"""Pick security events by field and time, and render them as an export file.

A row is kept when each field the caller named equals the row's own value (plain string
equality, no pattern). The time window is the one ``SecurityEventLog.recent`` already
applies, so the log keeps a single copy of that rule.

``recent`` applies its row limit BEFORE any field test, so a field filter is run over a
bounded scan of the window (``scan_limit`` rows, newest first) and the limit is cut
afterwards. A scan that used its whole budget says so (``Selection.scan_capped``): older
rows in the window were not looked at, and an earlier ``until`` reaches them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, NamedTuple, Protocol

from kiro_crew.terminal_safe import strip_control_characters

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


#: Export columns, fixed so a script sees the same shape every time.
CSV_COLUMNS = tuple(
    "timestamp event_id event_type caller_identity agent source operation tool_kind outcome "
    "resources downstream_service request_id error metadata prev_hash entry_hash".split()
)

#: A cell starting with one of these is read by a spreadsheet as a formula.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


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
    limit: int | None,
    filters: dict[str, str],
    since: datetime | None,
    until: datetime | None,
    scan_limit: int,
) -> Selection:
    """Up to *limit* rows of *log* matching every non-empty filter, newest first.

    With no field filter the log's own ``recent`` answers and nothing is scanned past
    *limit*; ``None`` keeps every match the scan reaches.
    """
    wanted = [(FIELD_KEYS[name], value) for name, value in filters.items() if value]
    if not wanted:
        rows = log.recent(limit=scan_limit if limit is None else limit, since=since, until=until)
        return Selection(rows, limit is None and len(rows) >= scan_limit)
    scanned = log.recent(limit=scan_limit, since=since, until=until)
    rows = [row for row in scanned if all(row.get(key) == value for key, value in wanted)]
    rows = rows if limit is None else rows[:limit]
    return Selection(rows, len(scanned) >= scan_limit and (limit is None or len(rows) < limit))


def render_jsonl(rows: list[dict]) -> str:
    """One JSON object per line, each row exactly as the log holds it."""
    return "".join(json.dumps(row) + "\n" for row in rows)


def _cell(value: Any) -> str:
    """One CSV cell: no control characters, a lone surrogate escaped, inert in a spreadsheet."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True)
    elif isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    else:
        text = str(value)
    text = strip_control_characters(text).encode("utf-8", "backslashreplace").decode("utf-8")
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


def render_csv(rows: list[dict]) -> str:
    """A header row, then one row per event in ``CSV_COLUMNS`` order."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in CSV_COLUMNS])
    return buffer.getvalue()


def build_manifest(
    rows: list[dict],
    *,
    fmt: str,
    payload: str,
    filters: dict[str, str],
    since: datetime | None,
    until: datetime | None,
    scan_capped: bool,
    now: datetime,
) -> dict[str, Any]:
    """Filters, row count, time span and the end chain hashes of an export (*rows* oldest first)."""
    first = rows[0] if rows else {}
    last = rows[-1] if rows else {}
    return {
        "format": fmt,
        "exported_at": now.isoformat(),
        "filters": {name: value for name, value in filters.items() if value},
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "rows": len(rows),
        "first_timestamp": first.get("timestamp"),
        "last_timestamp": last.get("timestamp"),
        "chain_hash_first": first.get("entry_hash"),
        "chain_hash_last": last.get("entry_hash"),
        "scan_capped": scan_capped,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
