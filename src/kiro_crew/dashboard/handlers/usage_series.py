"""Stacked spend-over-time series for the Usage tab's area chart.

``GET /api/usage/series`` answers one question the per-day totals cannot:
*what is today's spend made of, and how did that composition drift?* The
reply is a dense day axis plus one zero-filled series per bucket of a chosen
dimension, shaped so the browser can stack the series directly (and, for the
cumulative view, run a prefix sum over each one). Rows come from the same
per-turn shards every other usage reader scans
(``<data home>/usage/tokens/YYYY-MM-DD.jsonl``), admitted by the same row
guard: a ``tokens`` row with a parseable local day.

Dimensions (``by``): ``channel`` (where the session came from: ``dashboard``,
``cron``, ``background``, ``telegram`` …), ``agent``, ``model``, and
``cohort`` — the ISO week in which the row's session was first seen inside the
window, keyed by that week's Monday. Cohort is the one dimension that is a
property of the session rather than of the row, so it is assigned in a second
pass once every row's session is known. A session that started before the
window counts from its first row inside it; the shards it started in have been
retired, so there is nothing to read a truer answer from, and the chart labels
its oldest cohort layer as sessions "started by" a date rather than a week of
starts.

The channel is derived from the row's session key with
:func:`~kiro_crew.messaging.link.telemetry_channel_of`, never read from the
row's own ``surface`` field — the same rule, for the same reason, as the Spend
panel's ``cost_breakdown``: historical rows can carry a wrong but non-empty
``surface`` (``dashboard`` for a Telegram turn), and the row schema has no
writer marker that would tell such a row from a trustworthy one. The key is
authoritative, and it also keeps the layer set closed (one label per channel,
not one per background job), so this chart and that table bucket spend by the
same vocabulary.

The value stacked is the row's ``credits``, the unit the kiro-cli backend
(and its KAS relay) bills in. A harness that bills in tokens or dollars writes
rows with no credits, so the Usage tab mounts the chart only when the
configured default harness (``agent.acp_backend``) reports credits
(``api/acpBackend.ts``); turns a crew member ran on a different harness
(``agent.member_acp_backend``) still land in the same store and count
whatever credits they recorded.

Bucketing is top-N by total over the window, with the remainder folded into
one ``other`` series so a dimension with hundreds of distinct values (agents,
session cohorts on a busy install) still renders as a legible stack. A row
whose dimension value is empty or absent — a row with no session key, a row
that predates ``agent``, a turn whose surface never set a model — lands in an
explicit ``unattributed`` series rather than being guessed at or dropped:
dropping it would make the stack's top edge disagree with the Daily History
credits for the same day. Series are returned in stack order, bottom first:
value dimensions largest-first, cohorts oldest-first, then ``other``, then
``unattributed``.

The parsed rows are cached on the shard fingerprint the other readers use
(``(path, mtime, size)`` per shard in the window, plus a TTL), so a dashboard
switching dimension re-aggregates a few thousand small tuples
instead of re-reading the shards. The cache is bounded on both axes the rule
``a-bound-bounds-every-field-it-retains`` names: :data:`MAX_ROWS` caps how
many rows the window may retain and :data:`MAX_FIELD_CHARS` caps every string
a row keeps. The count bound is applied as each shard is read -- a shard
holds at most the rows the window still has room for while it is being parsed,
never the whole file -- so when the cap trips the NEWEST rows are kept, the
oldest are counted rather than retained, and the payload says so
(``truncated``, ``dropped_rows``, and ``complete_from``: the first day every
row of which was kept) instead of presenting a shortened window as the whole
one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew import sel as _sel_mod
from kiro_crew.dashboard.handlers import usage as _usage
from kiro_crew.jsonl_util import bounded_records
from kiro_crew.messaging.link import telemetry_channel_of

logger = logging.getLogger(__name__)

#: Dimensions a caller may stack by. Everything but ``cohort`` is a row field.
SERIES_DIMENSIONS: tuple[str, ...] = ("channel", "agent", "model", "cohort")
DEFAULT_DIMENSION = "channel"
#: Buckets kept apart before the rest folds into ``other``. Matches the number
#: of distinguishable hues the dashboard's session palette generates, so the
#: chart never needs more colours than the theme can give it.
TOP_BUCKETS = 7
#: The series window is the shard retention window: a longer axis would only
#: add empty days.
WINDOW_DAYS = _usage._TOKEN_HISTORY_DAYS

#: Rows the window may retain. ``bounded_records`` bounds each record's size;
#: this bounds their count, so the parsed-row cache (and with it the handler's
#: memory) stays finite however busy a day's shard got. Sized at roughly two
#: turns a minute around the clock for the whole window — far above a single
#: operator's install, which the chart exists for. A store past it keeps its
#: newest rows and the payload counts the oldest ones left out.
MAX_ROWS = 100_000
#: Longest string a retained row keeps for any field (session key, agent,
#: model). No writer produces one this long; the bound exists so a hand-edited
#: or foreign shard cannot grow the cache through its fields.
MAX_FIELD_CHARS = 256

#: Reserved series keys. Chosen so no real channel, agent, model or week date
#: can collide with them (dimension values are free text but a user-facing
#: name wrapped in double underscores is not one anyone writes).
OTHER_KEY = "__other__"
UNATTRIBUTED_KEY = "__unattributed__"

_ROW_FIELDS = {"channel": "channel", "agent": "agent", "model": "model"}

# Parsed-row cache: keyed on the shard fingerprint plus a TTL safety net for
# clock skew and in-place edits, the same posture as usage._parse_token_history.
_SCAN_CACHE: _Scan | None = None
_SCAN_CACHE_KEY: tuple[tuple[str, float, int], ...] | None = None
_SCAN_CACHE_TS: float = 0.0
_SCAN_CACHE_TTL = 120.0


@dataclass(frozen=True, slots=True)
class _Row:
    """One admitted per-turn row, reduced to what the series needs."""

    day: str
    ts: float
    slot: str
    channel: str
    agent: str
    model: str
    credits: float


@dataclass(frozen=True, slots=True)
class _Scan:
    """The window's retained rows plus how the retention cap shortened it.

    ``dropped`` is how many admitted rows the cap refused. ``complete_from`` is
    the oldest day every row of which was kept — the first day the chart shows
    whole — or ``None`` when no day is (the cap tripped inside the newest shard)
    or nothing was refused.
    """

    rows: list[_Row]
    dropped: int
    complete_from: str | None = None


def _str_field(obj: dict[str, Any], key: str) -> str:
    """The row's string field, clipped to :data:`MAX_FIELD_CHARS` at retention."""
    value = obj.get(key)
    return value[:MAX_FIELD_CHARS] if isinstance(value, str) else ""


def _credits_of(obj: dict[str, Any]) -> float:
    """The row's credits as a finite float; anything else counts 0.

    ``float`` of an int wider than a double raises rather than answers, and a
    non-finite value would reach the client as ``Infinity``, which is not JSON.
    """
    number = _usage._usage_number(obj.get("credits"))
    if number is None:
        return 0.0
    try:
        value = float(number)
    except OverflowError:
        return 0.0
    return value if math.isfinite(value) else 0.0


def _row_from(obj: dict[str, Any]) -> _Row | None:
    if obj.get("_type") != "tokens":
        return None
    ts_raw = obj.get("ts")
    ts_epoch = _usage._parse_row_ts(str(ts_raw or ""))
    day = _usage._parse_row_day(ts_raw)
    if ts_epoch is None or day is None:
        return None
    slot = _str_field(obj, "slot")
    return _Row(
        day=day,
        ts=ts_epoch,
        slot=slot,
        # From the session key, not the row's ``surface`` field — see the
        # module doc. A row with no key has no channel to derive and stays
        # unattributed rather than taking the classifier's ``unknown``.
        channel=telemetry_channel_of(slot) if slot else "",
        agent=_str_field(obj, "agent"),
        # Same canonicalisation the daily chart applies, so a model that
        # crossed a provider id migration never splits into two buckets.
        model=model_registry.canonicalize_for_provider(
            _str_field(obj, "model"), _str_field(obj, "provider")
        ),
        credits=_credits_of(obj),
    )


def _shard_rows(path: Path, budget: int) -> tuple[list[_Row], int]:
    """The newest ``budget`` admitted rows of one shard, plus how many it refused.

    Within a day the file is chronological, so a deque bounded by the budget
    holds the tail as the file streams past: an admitted row beyond the bound
    is counted and let go at the point of retention, and the shard never sits
    in memory whole however large it grew.
    """
    kept: deque[_Row] = deque(maxlen=budget)
    refused = 0
    with path.open("rb") as fh:
        for line in bounded_records(fh, path, label="usage"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            row = _row_from(obj)
            if row is None:
                continue
            if len(kept) == budget:
                refused += 1
            kept.append(row)
    return list(kept), refused


def _scan_rows(days: int) -> _Scan:
    rows: list[_Row] = []
    dropped = 0
    complete_from: str | None = None
    unbroken = True
    # The shard window's cutoff is inclusive, so it also yields the shard for the
    # day before the axis opens; its rows never reach the chart, so they must not
    # spend the row budget or be counted as refused.
    first_day = _day_axis(days, datetime.now().astimezone().date())[0]
    shards = [p for p in _usage._shards_in_window(days) if p.stem >= first_day]
    # Newest shard first, so the cap — when it trips — shortens the window from
    # its OLD end and the recent days a spend chart is read for stay whole.
    for path in sorted(shards, key=lambda p: p.name, reverse=True):
        try:
            shard, refused = _shard_rows(path, MAX_ROWS - len(rows))
        except (OSError, UnicodeDecodeError):
            # A corrupt or unreadable shard costs its own rows, not the window;
            # but a day whose rows were lost is not complete, so "complete from"
            # cannot reach past it either.
            unbroken = False
            continue
        rows.extend(shard)
        # Shards are read newest first and the cap only ever refuses from the
        # old end, so every shard retained whole before the first refusal is a
        # day the chart shows complete; the oldest of them is where "complete"
        # starts. A day with no shard at all counts as complete too.
        if unbroken and refused == 0 and not dropped:
            complete_from = path.stem
        dropped += refused
    if dropped:
        logger.warning(
            "usage series: the window held %d more rows than the %d retained; "
            "the oldest were left out",
            dropped,
            MAX_ROWS,
        )
    return _Scan(rows, dropped, complete_from if dropped else None)


def load_rows() -> _Scan:
    """Every retained row in the window, cached on the shard fingerprint.

    Returns the rows together with the count the cap refused, so the caller can
    disclose a shortened window instead of serving it as the whole one.
    """
    global _SCAN_CACHE, _SCAN_CACHE_KEY, _SCAN_CACHE_TS

    shard_paths = _usage._shards_in_window(WINDOW_DAYS)
    cache_key: tuple[tuple[str, float, int], ...] | None
    try:
        cache_key = tuple(
            sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in shard_paths)
        )
    except OSError:
        cache_key = None
    now = time.time()
    if (
        _SCAN_CACHE is not None
        and cache_key is not None
        and _SCAN_CACHE_KEY == cache_key
        and (now - _SCAN_CACHE_TS) < _SCAN_CACHE_TTL
    ):
        return _SCAN_CACHE
    scan = _scan_rows(WINDOW_DAYS)
    _SCAN_CACHE = scan
    _SCAN_CACHE_KEY = cache_key
    _SCAN_CACHE_TS = now
    return scan


def _week_monday(ts: float) -> str:
    """The Monday of the ISO week containing the LOCAL day of ``ts``."""
    local = datetime.fromtimestamp(ts).astimezone()
    monday = local.date() - timedelta(days=local.isoweekday() - 1)
    return monday.isoformat()


def _cohort_of_slot(rows: list[_Row]) -> dict[str, str]:
    """Each session's cohort: the ISO week of its first row inside the window."""
    first_seen: dict[str, float] = {}
    for row in rows:
        if not row.slot:
            continue
        seen = first_seen.get(row.slot)
        if seen is None or row.ts < seen:
            first_seen[row.slot] = row.ts
    return {slot: _week_monday(ts) for slot, ts in first_seen.items()}


def _bucket_key(row: _Row, by: str, cohorts: dict[str, str]) -> str:
    if by == "cohort":
        return cohorts.get(row.slot, "") if row.slot else ""
    return getattr(row, _ROW_FIELDS[by])


def _day_axis(days: int, today: date) -> list[str]:
    start = today - timedelta(days=days - 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(days)]


def _round(value: float) -> float:
    return round(value, 6)


def _add(total: float, value: float) -> float:
    """``total + value``, or ``total`` unchanged when the sum leaves the finite range.

    Every operand is finite, but a sum of finite floats can still overflow to
    ``Infinity``, which is not JSON. Same rule as the Daily History credits: the
    contribution that would push a figure past the finite range is dropped.
    """
    candidate = total + value
    return candidate if math.isfinite(candidate) else total


def _sum(values: Iterable[float]) -> float:
    total = 0.0
    for value in values:
        total = _add(total, value)
    return total


def build_series(
    rows: list[_Row],
    *,
    by: str = DEFAULT_DIMENSION,
    days: int = WINDOW_DAYS,
    top: int = TOP_BUCKETS,
    dropped: int = 0,
    complete_from: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Aggregate ``rows`` into the stacked-series payload. Pure; see the module doc.

    ``dropped`` is how many admitted rows the retention cap refused and
    ``complete_from`` the first day it left whole; both pass through as the
    payload's truncation disclosure.
    """
    today = today or datetime.now().astimezone().date()
    dates = _day_axis(days, today)
    index = {d: i for i, d in enumerate(dates)}
    window_rows = [r for r in rows if r.day in index]
    cohorts = _cohort_of_slot(window_rows) if by == "cohort" else {}

    per_bucket: dict[str, list[float]] = {}
    unattributed = [0.0] * days
    for row in window_rows:
        value = row.credits
        if value == 0.0:
            continue
        key = _bucket_key(row, by, cohorts)
        target = unattributed if not key else per_bucket.setdefault(key, [0.0] * days)
        target[index[row.day]] = _add(target[index[row.day]], value)

    ranked = sorted(per_bucket.items(), key=lambda kv: (-_sum(kv[1]), kv[0]))
    kept, folded = ranked[:top], ranked[top:]
    if by == "cohort":
        kept.sort(key=lambda kv: kv[0])

    series: list[dict[str, Any]] = [
        {
            "key": key,
            "kind": "bucket",
            "values": [_round(v) for v in values],
            "total": _round(_sum(values)),
        }
        for key, values in kept
    ]
    if folded:
        other = [0.0] * days
        for _key, values in folded:
            for i, v in enumerate(values):
                other[i] = _add(other[i], v)
        series.append(
            {
                "key": OTHER_KEY,
                "kind": "other",
                "values": [_round(v) for v in other],
                "total": _round(_sum(other)),
                "members": len(folded),
            }
        )
    if any(unattributed):
        series.append(
            {
                "key": UNATTRIBUTED_KEY,
                "kind": "unattributed",
                "values": [_round(v) for v in unattributed],
                "total": _round(_sum(unattributed)),
            }
        )
    return {
        "dates": dates,
        "series": series,
        "total": _round(_sum(s["total"] for s in series)),
        "truncated": dropped > 0,
        "dropped_rows": dropped,
        "complete_from": complete_from if dropped else None,
    }


def _query_choice(
    request: web.Request, name: str, allowed: tuple[str, ...], default: str
) -> str | None:
    """The query value when it is one of ``allowed``; the default when absent; ``None`` otherwise."""
    raw = (request.query.get(name) or "").strip()
    if not raw:
        return default
    return raw if raw in allowed else None


async def api_usage_series(request: web.Request) -> web.Response:
    """GET /api/usage/series?by=channel|agent|model|cohort.

    Dashboard-only: the payload is the whole install's spend with no slot
    filter, so an app token — scoped to its own rows on ``/api/usage/turns`` —
    is answered 404 here, indistinguishable from a route it was never granted,
    and the refusal is SEL-audited like every app-caller decision. An unknown
    ``by`` is a 400, because silently substituting the default would render a
    chart of something other than what was asked for.
    """
    request_app = str(request.get("app", "") or "")
    if request_app:

        def _audit_denied() -> None:
            _sel_mod.sel().log_api_access(
                caller=request_app,
                operation="usage_series",
                outcome="denied",
                source="app_isolation",
                error="dashboard-only endpoint",
            )

        await asyncio.to_thread(_audit_denied)
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    by = _query_choice(request, "by", SERIES_DIMENSIONS, DEFAULT_DIMENSION)
    if by is None:
        return web.json_response(
            {
                "error": f"by must be one of {', '.join(SERIES_DIMENSIONS)}",
                "code": "invalid_dimension",
            },
            status=400,
        )
    scan = await asyncio.to_thread(load_rows)
    return web.json_response(
        build_series(scan.rows, by=by, dropped=scan.dropped, complete_from=scan.complete_from)
    )
