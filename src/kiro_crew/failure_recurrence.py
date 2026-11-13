"""Find agent tool failures that keep coming back, from the security event log.

Report-only: this module reads ``tool_invocation`` rows and returns grouped
findings. It writes nothing — no lesson, no file, no event. A caller that wants
to turn a finding into a lesson must put it in front of the person first.

A finding is keyed by a *signature* built from fields that never carry a shell
command: the tool kind, the MCP tool name (only for MCP calls), a rule id when
the row names one, and the error text with paths, ids and numbers collapsed. The
``operation`` field of a non-MCP row is never read, because for a shell call it
is the command line and can hold a secret.
"""

from __future__ import annotations

import string
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from typing import Literal, Protocol

from kiro_crew.sel_reader import _parse_timestamp

Trend = Literal["new", "growing", "steady"]

#: Outcomes on a ``tool_invocation`` row that mean the call did not do its work.
_FAILED_OUTCOMES = frozenset(
    {"denied", "rejected", "blocked", "failed", "failure", "error", "timeout", "expired"}
)
_FAILED_PREFIXES = ("denied_", "rejected_")

#: Error texts that are startup gating, not a failure worth a lesson.
_IGNORED_ERRORS = frozenset({"feature disabled"})

#: Errors that name a failure class on their own; the tool that happened to be
#: running when it fired would split one class into many.
_CLASS_ERRORS = frozenset({"turn_limit", "child_escalation_limit"})

_MAX_ERROR_CHARS = 120
#: Punctuation that may wrap a path, id or number inside an error message.
_WRAP_CHARS = "'\"`,;:()"
_HEX_CHARS = frozenset(string.hexdigits)
_MIN_ID_CHARS = 8

#: Default read bound for :func:`scan`.
DEFAULT_READ_LIMIT = 50_000


class _EventLog(Protocol):
    def recent(
        self, limit: int = ..., *, since: datetime | None = ..., until: datetime | None = ...
    ) -> list[dict]: ...


@dataclass(frozen=True)
class Recurrence:
    """One failure signature that recurred inside the current window."""

    signature: str
    tool_kind: str
    tool: str  # the MCP tool name, or the class error; "" when neither applies
    rule_id: str
    error: str  # normalized error text
    hits: int
    sessions: int
    days: int
    previous_hits: int
    trend: Trend


def _collapse_token(token: str) -> str:
    core = token.strip(_WRAP_CHARS)
    if not core:
        return token
    start = token.index(core)
    head, tail = token[:start], token[start + len(core) :]
    if "/" in core or "\\" in core:
        core = "<path>"
    elif len(core) >= _MIN_ID_CHARS and set(core) <= _HEX_CHARS:
        core = "<id>"
    else:
        core = "".join(
            "<n>" if is_digit else "".join(run)
            for is_digit, run in groupby(core, lambda c: c in string.digits)
        )
    return head + core + tail


def normalize_error(text: str) -> str:
    """Collapse the parts of an error that differ between two runs of one failure."""
    return " ".join(_collapse_token(token) for token in text.split())[:_MAX_ERROR_CHARS]


def _is_failure(event: dict) -> bool:
    if event.get("event_type") != "tool_invocation":
        return False
    outcome = str(event.get("outcome") or "")
    if outcome not in _FAILED_OUTCOMES and not outcome.startswith(_FAILED_PREFIXES):
        return False
    return str(event.get("error") or "").strip().lower() not in _IGNORED_ERRORS


def _parts(event: dict) -> tuple[str, str, str, str]:
    """``(tool_kind, tool, rule_id, error)`` for a failure row, never the command text."""
    error = str(event.get("error") or "").strip()
    if error in _CLASS_ERRORS:
        return "", error, "", error
    tool_kind = str(event.get("tool_kind") or "").strip()[:_MAX_ERROR_CHARS] or "unknown"
    metadata = event.get("metadata")
    rule = metadata.get("rule_id") if isinstance(metadata, dict) else None
    rule_id = rule.strip()[:_MAX_ERROR_CHARS] if isinstance(rule, str) else ""
    # MCP dispatch refusals carry the server as `tool_kind` and `source="mcp"`.
    is_mcp = (
        tool_kind == "mcp" or event.get("source") == "mcp" or bool(event.get("downstream_service"))
    )
    tool = str(event.get("operation") or "").strip()[:_MAX_ERROR_CHARS] if is_mcp else ""
    return tool_kind, tool, rule_id, normalize_error(error)


def signature(event: dict) -> str | None:
    """The grouping key for a failed ``tool_invocation`` row, or ``None`` for any other row."""
    if not _is_failure(event):
        return None
    return "|".join(_parts(event))


def find_recurring(current: Iterable[dict], previous: Iterable[dict] = ()) -> list[Recurrence]:
    """Group *current* failures and keep the ones that recur.

    A signature recurs when it was seen in two or more sessions, or on two or
    more UTC days: one burst inside one session on one day is not recurrence.
    Rows with no session (``caller_identity`` empty) still count by day.

    *previous* is the window before, used only for the trend: ``new`` when the
    signature did not fail there at all, ``growing`` when it failed more often
    now, otherwise ``steady``.
    """
    groups: dict[str, tuple[tuple[str, str, str, str], list[dict]]] = {}
    for event in current:
        sig = signature(event)
        if sig is not None:
            groups.setdefault(sig, (_parts(event), []))[1].append(event)
    before: dict[str, int] = {}
    for event in previous:
        sig = signature(event)
        if sig is not None:
            before[sig] = before.get(sig, 0) + 1

    found: list[Recurrence] = []
    for sig, (parts, rows) in groups.items():
        sessions = {str(r.get("caller_identity") or "") for r in rows} - {""}
        days = {ts.date() for r in rows if (ts := _parse_timestamp(r.get("timestamp"))) is not None}
        if len(sessions) < 2 and len(days) < 2:
            continue
        prior = before.get(sig, 0)
        trend: Trend = "new" if prior == 0 else "growing" if len(rows) > prior else "steady"
        tool_kind, tool, rule_id, error = parts
        found.append(
            Recurrence(
                signature=sig,
                tool_kind=tool_kind,
                tool=tool,
                rule_id=rule_id,
                error=error,
                hits=len(rows),
                sessions=len(sessions),
                days=len(days),
                previous_hits=prior,
                trend=trend,
            )
        )
    found.sort(key=lambda r: (-r.hits, r.signature))
    return found


def scan(
    log: _EventLog,
    *,
    now: datetime,
    window: timedelta = timedelta(days=7),
    limit: int = DEFAULT_READ_LIMIT,
) -> list[Recurrence]:
    """Recurring failures in ``[now - window, now)``, trended against the window before.

    shortcut: one bounded ``recent()`` read of *limit* rows of every event type
    across both windows, newest first; on a log busier than that the previous
    window is cut short (more signatures read as ``new``), and past *limit* rows
    in the current window alone it is cut too. Page the read if a real install
    hits the bound.
    """
    start = now - window
    rows = log.recent(limit, since=start - window, until=now)
    current: list[dict] = []
    previous: list[dict] = []
    for row in rows:
        ts = _parse_timestamp(row.get("timestamp"))
        if ts is None:
            continue
        (current if ts >= start else previous).append(row)
    return find_recurring(current, previous)
