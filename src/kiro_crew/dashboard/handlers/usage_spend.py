"""A session's spend: the key a slot's spend is filed under, the per-slot spend the
Sessions table and Spend tab read, the per-day credits, and one slot's per-turn usage.

An owner of :mod:`kiro_crew.dashboard.handlers.usage`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from kiro_crew.dashboard.handlers.usage_store import (
    _SESSIONS_HISTORY_DAYS,
    SPEND_WINDOW_DAYS,
    _parse_row_day,
    _parse_row_ts,
    _shards_in_window,
    _usage_number,
    is_session_slot,
)
from kiro_crew.jsonl_util import bounded_records

# Bare dashboard slot key: chat-<seq>-<epoch>. The shard's ``slot`` field uses
# this shape (without a ``dashboard:`` prefix), while the session manager keys
# are ``dashboard:chat-<seq>-<epoch>``. Normalization aligns them.
_BARE_CHAT_SLOT_RE = re.compile(r"^chat-\d+-\d+$")


# ── per-slot spend (shared by the Sessions table and Spend tab) ────────────


def spend_key_for_slot(slot: str) -> str:
    """The key a slot's spend is filed under in :func:`slot_spend`'s result.

    One owner for this rule. Shards record a bare dashboard slot key
    (``chat-69-1785905004``) while sessions are addressed as
    ``dashboard:chat-69-…``; a caller that re-derived the prefix would be a second
    owner, and a second owner of an identity rule is exactly how the spend join
    and the session list drifted apart before.
    """
    return f"dashboard:{slot}" if _BARE_CHAT_SLOT_RE.match(slot) else slot


_SLOT_SPEND_CACHE: dict[str, dict[str, float]] = {}


_SLOT_SPEND_CACHE_SIG: tuple[object, ...] = ()


_SLOT_SPEND_CACHE_AT: float = 0.0


# The shard signature alone cannot key this cache. The result also depends on
# ``cutoff = now - days*86400``, which moves continuously, so on an idle machine
# (no shard write) a row that ages PAST the cutoff would keep being counted
# until some shard changed -- up to a day, since ``_shards_in_window`` only
# re-picks the shard set when the date rolls. A short TTL bounds that drift to
# seconds against a 7-day window while still sparing the 5s poll a re-read.
_SLOT_SPEND_TTL_S = 60.0


def slot_spend(days: int = SPEND_WINDOW_DAYS) -> dict[str, dict[str, float]]:
    """Per-session spend over the last *days*: ``{session_key: {"credits", "turns"}}``.

    This is the ONE aggregation path that both the Sessions table and the Spend
    tab's per-conversation view must go through. It uses the same per-row
    ``ts_epoch`` cutoff as :func:`cost_breakdown`, so a row inside the boundary
    shard but older than the cutoff is NOT counted; and it normalizes bare
    dashboard slot keys (``chat-69-1785905004``) to the full session key form
    (``dashboard:chat-69-1785905004``) so a direct lookup by session key works.

    Cached against shard size + mtime AND a short TTL, so polling every few
    seconds does not re-read the window while the moving cutoff still takes
    effect. Safe to call from a thread (the offloaded sampling thread in
    session_memory).
    """
    global _SLOT_SPEND_CACHE, _SLOT_SPEND_CACHE_SIG, _SLOT_SPEND_CACHE_AT

    now = time.time()
    paths = _shards_in_window(days)
    sig: tuple[object, ...] = tuple(
        (str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths if p.exists()
    )
    if (
        sig == _SLOT_SPEND_CACHE_SIG
        and _SLOT_SPEND_CACHE_SIG
        and now - _SLOT_SPEND_CACHE_AT < _SLOT_SPEND_TTL_S
    ):
        return _SLOT_SPEND_CACHE

    cutoff = now - (days * 86400)
    out: dict[str, dict[str, float]] = {}

    for path in paths:
        try:
            with path.open("rb") as fh:
                for line in bounded_records(fh, path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    # Per-row timestamp cutoff: the shard file date is a coarse
                    # filter (a shard can span midnight), so rows older than the
                    # cutoff must still be excluded individually.
                    ts_epoch = _parse_row_ts(str(obj.get("ts") or ""))
                    if ts_epoch is None or ts_epoch < cutoff:
                        continue
                    slot = str(obj.get("slot") or "")
                    if not slot or not is_session_slot(slot):
                        continue
                    credits = obj.get("credits")
                    if not isinstance(credits, (int, float)) or not math.isfinite(credits):
                        continue
                    # Normalize bare dashboard keys to the full session-key form.
                    key = spend_key_for_slot(slot)
                    cur = out.setdefault(key, {"credits": 0.0, "turns": 0.0})
                    cur["credits"] += float(credits)
                    cur["turns"] += 1
        except (OSError, UnicodeDecodeError):
            continue

    _SLOT_SPEND_CACHE, _SLOT_SPEND_CACHE_SIG = out, sig
    _SLOT_SPEND_CACHE_AT = now
    return out


def daily_credits(days: int = _SESSIONS_HISTORY_DAYS) -> dict[str, float]:
    """Credits spent per LOCAL calendar day over the last *days*.

    ``{"YYYY-MM-DD": credits}`` for every day that has at least one counted
    row. Rows are admitted by the same three tests :func:`slot_spend` applies
    (a ``tokens`` row, inside the per-row epoch cutoff, with a finite numeric
    ``credits``) and keyed by :func:`_parse_row_day`, the local day the shard
    partition itself uses. There is deliberately NO slot filter: every turn the
    backend billed counts, background slots included, so a day's figure is the
    day's whole spend rather than only its conversations.

    Numeric hygiene: ``credits`` is coerced through ``float`` before the finite
    test, because ``math.isfinite`` on an int wider than a double raises rather
    than answers, and a row whose addition would push a day's total past the
    finite range is dropped so the payload can never carry ``Infinity``.
    """
    cutoff = time.time() - (days * 86400)
    out: dict[str, float] = {}
    for path in _shards_in_window(days):
        try:
            with path.open("rb") as fh:
                for line in bounded_records(fh, path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    ts_raw = obj.get("ts")
                    ts_epoch = _parse_row_ts(str(ts_raw or ""))
                    if ts_epoch is None or ts_epoch < cutoff:
                        continue
                    credits = obj.get("credits")
                    if isinstance(credits, bool) or not isinstance(credits, (int, float)):
                        continue
                    try:
                        value = float(credits)
                    except OverflowError:
                        continue
                    if not math.isfinite(value):
                        continue
                    day = _parse_row_day(ts_raw)
                    if day is None:
                        continue
                    total = out.get(day, 0.0) + value
                    if not math.isfinite(total):
                        continue
                    out[day] = total
        except (OSError, UnicodeDecodeError):
            # Same policy as every other shard reader here: a corrupt or
            # unreadable shard costs its own rows, not the whole window.
            continue
    return out


#: Numeric per-turn fields copied from a shard row into a usage-turns row, in
#: the order the API returns them. One owner: the reader below and its tests
#: both consume this tuple, so a field added to the recorder shows up in the
#: API by being added HERE, not by editing two lists that can drift.
TURN_USAGE_FIELDS: tuple[str, ...] = (
    "input",
    "output",
    "cache_create",
    "cache_read",
    "credits",
    "cost",
    "duration_ms",
    "context_used",
    "context_window",
)


def slot_turn_usage(
    slot: str,
    days: float = SPEND_WINDOW_DAYS,
    *,
    app: str | None = None,
    until: float | None = None,
) -> list[dict[str, Any]]:
    """Per-turn usage rows for ONE session, oldest first.

    The per-turn drill-down under :func:`slot_spend`'s aggregate: each row is
    one turn's shard record — model, token counts, credits, duration, and the
    context meter — for the slot named. Built for the app-facing usage API: an
    app that runs agent sessions (via app-owned slots) can account for what its
    own turns cost without reaching into the shard files, whose location and
    row shape are this module's private contract.

    Same walk and the same reasons as :func:`context_trace`: per-session
    per-turn detail stays OUT of the OTEL pipeline because slot keys are
    unbounded-cardinality labels. Malformed lines and foreign slots are
    skipped, not errors — a shard is an append-only log written by a live
    gateway, and a torn tail line is expected during a write.

    ``app`` is the ownership filter for app callers: only rows STAMPED with
    that app at write time are returned. Ownership recorded on the row is what
    survives the slot — a live-slot check both leaks on slot-name reuse (a
    recreated slot vouches for the previous owner's rows) and denies an app
    its own completed sessions. Rows written before the stamp existed carry no
    ``app`` and are invisible to app callers: deny-by-default for ownership
    that was never recorded. ``None`` (the dashboard) reads everything.

    The window is enforced per ROW, not only per shard file: the oldest shard
    in the window covers a whole day, so without a row-level cutoff a request
    for N days returns rows up to a day older than asked — wrong in the one
    place this API exists for, accounting. ``until`` ends the window at that
    epoch instead of now (see :class:`TimeWindow`).
    """
    turns: list[dict[str, Any]] = []
    cutoff = (time.time() if until is None else until) - days * 86400
    upper = math.inf if until is None else until
    for shard_path in _shards_in_window(days, until):
        try:
            with shard_path.open("rb") as fh:
                for line in bounded_records(fh, shard_path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    if str(obj.get("slot") or "") != slot:
                        continue
                    if app is not None and str(obj.get("app") or "") != app:
                        continue
                    ts = _parse_row_ts(str(obj.get("ts") or ""))
                    if ts is None or ts < cutoff or ts >= upper:
                        # An unparseable timestamp cannot prove it is inside the
                        # window; accounting excludes what it cannot date.
                        continue
                    row: dict[str, Any] = {
                        "ts": str(obj.get("ts") or ""),
                        "model": str(obj.get("model") or ""),
                    }
                    for field in TURN_USAGE_FIELDS:
                        value = _usage_number(obj.get(field))
                        if value is not None:
                            row[field] = value
                    turns.append(row)
        except (OSError, UnicodeDecodeError):
            continue
    return turns
