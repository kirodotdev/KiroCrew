"""Context-window occupancy across sessions, and one session's context trace, for the
Telemetry panel.

An owner of :mod:`kiro_crew.dashboard.handlers.usage`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from kiro_crew.constants import crew_log_enabled, env_file_display
from kiro_crew.context_blocks import USER_LABEL
from kiro_crew.dashboard.handlers.usage_store import (
    _canonical_telemetry_surface,
    _coerce_int,
    _parse_row_ts,
    _shards_in_window,
    is_session_slot,
    session_category,
)
from kiro_crew.jsonl_util import bounded_records

# The facade's logger, so a log filter or capture keyed on the usage handler's name
# sees this module's records too.
logger = logging.getLogger("kiro_crew.dashboard.handlers.usage")


# A payload backstop, not a top-N: the panel lists sessions for the user to
# browse, sort and group, so cutting it to the "hottest" few hid most of them
# behind a number they could not reach. Measured over a 7d window this is 260
# sessions across all categories, which ships comfortably; the cap only exists
# so an account with thousands does not send all of them on every poll.
_CONTEXT_TOP_SESSIONS = 500


# Fingerprint + TTL cache, same contract as _TOKEN_CACHE: the Telemetry panel
# polls every 5s, and the shards are append-only, so (name, mtime, size) over
# the window invalidates exactly when a turn lands.
_CONTEXT_CACHE: dict[str, Any] | None = None


_CONTEXT_CACHE_KEY: tuple[Any, ...] | None = None


_CONTEXT_CACHE_TS: float = 0.0


_CONTEXT_CACHE_TTL = 30.0


def context_occupancy(days: float = 14, until: float | None = None) -> dict[str, Any]:
    """Aggregate per-turn context-window occupancy from the token row store.

    ``persist_token_record`` writes ``context_used`` / ``context_window`` on
    every turn across all nine dispatch surfaces; this is their read side,
    surfacing the session-death early warning those fields exist for.

    Occupancy is a per-turn ratio, so it is aggregated here rather than in the
    OTEL pipeline: the useful question is "which SESSION is close to its
    window", and slot keys are unbounded-cardinality labels that do not belong
    on a metric. Returns the turn-level percentile spread plus the hottest
    sessions by peak occupancy, newest first on ties.

    Rows predating the field, or any row whose window is missing/zero, are
    skipped — an unknown window cannot yield a ratio, and defaulting one would
    invent a number.

    ``until`` ends the window at that epoch instead of now (see
    :class:`TimeWindow`).
    """
    per_session: dict[str, dict[str, Any]] = {}
    pcts: list[float] = []
    cutoff = (time.time() if until is None else until) - (days * 86400)
    upper = math.inf if until is None else until

    shard_paths = _shards_in_window(days, until)
    try:
        cache_key: tuple[Any, ...] | None = (
            days,
            until,
            tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in shard_paths)),
        )
    except OSError:
        cache_key = None
    now = time.time()
    if (
        cache_key is not None
        and _CONTEXT_CACHE_KEY == cache_key
        and _CONTEXT_CACHE is not None
        and (now - _CONTEXT_CACHE_TS) < _CONTEXT_CACHE_TTL
    ):
        return _CONTEXT_CACHE

    for shard_path in shard_paths:
        try:
            with shard_path.open("rb") as fh:
                for line in bounded_records(fh, shard_path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    used = _coerce_int(obj.get("context_used"))
                    window = _coerce_int(obj.get("context_window"))
                    if used <= 0 or window <= 0:
                        continue
                    ts_raw = str(obj.get("ts") or "")
                    ts_epoch = _parse_row_ts(ts_raw)
                    if ts_epoch is None or ts_epoch < cutoff or ts_epoch >= upper:
                        continue
                    slot = str(obj.get("slot") or "unknown")
                    # Before the percentile sample, not after: the spread and the
                    # session list have to describe the same population, or the
                    # p90 on the card disagrees with every row beneath it.
                    if not is_session_slot(slot):
                        continue
                    p = (used / window) * 100.0
                    pcts.append(p)
                    cur = per_session.get(slot)
                    if cur is None:
                        cur = {
                            "slot": slot,
                            "category": session_category(slot),
                            "turns": 0,
                            "peak_pct": 0.0,
                            "used": 0,
                            "window": window,
                            "agent": "",
                            "model": "",
                            "surface": "",
                            "ts": "",
                            "_ts_epoch": 0.0,
                        }
                        per_session[slot] = cur
                    cur["turns"] = int(cur["turns"]) + 1
                    if p > float(cur["peak_pct"]):
                        cur["peak_pct"] = p
                    # Identity + absolute numbers describe the LATEST turn, so a
                    # session that switched model or agent mid-life is reported
                    # as what it is now, not what it once was.
                    if ts_epoch >= float(cur["_ts_epoch"]):
                        cur.update(
                            {
                                "_ts_epoch": ts_epoch,
                                "ts": str(ts_raw),
                                "used": used,
                                "window": window,
                                "agent": str(obj.get("agent") or ""),
                                "model": str(obj.get("model") or ""),
                                "surface": _canonical_telemetry_surface(
                                    str(obj.get("surface") or "")
                                ),
                            }
                        )
        except (OSError, UnicodeDecodeError):
            continue

    def _store(result: dict[str, Any]) -> dict[str, Any]:
        global _CONTEXT_CACHE, _CONTEXT_CACHE_KEY, _CONTEXT_CACHE_TS
        if cache_key is not None:
            _CONTEXT_CACHE, _CONTEXT_CACHE_KEY, _CONTEXT_CACHE_TS = result, cache_key, now
        return result

    if not pcts:
        return _store({"turns": 0, "sessions": [], "window_days": days})

    pcts.sort()

    def _q(q: float) -> float:
        # Nearest-rank on the sorted samples: these are exact per-turn values,
        # not histogram buckets, so no interpolation is warranted.
        idx = min(len(pcts) - 1, max(0, math.ceil(q * len(pcts)) - 1))
        return round(pcts[idx], 1)

    sessions = sorted(
        per_session.values(),
        key=lambda s: (float(s["peak_pct"]), float(s["_ts_epoch"])),
        reverse=True,
    )[:_CONTEXT_TOP_SESSIONS]
    for s in sessions:
        s.pop("_ts_epoch", None)
        s["peak_pct"] = round(float(s["peak_pct"]), 1)

    return _store(
        {
            "turns": len(pcts),
            "p50_pct": _q(0.50),
            "p90_pct": _q(0.90),
            "max_pct": round(pcts[-1], 1),
            "sessions": sessions,
            "window_days": days,
        }
    )


#: A label per Crew category, so a reader that groups blocks by label (the Session
#: Breakdown tree) still colours each category the way it always has.
_CATEGORY_LABEL = {
    "message": USER_LABEL,
    "memory": "memory",
    "rules": "lessons",
    "skills": "loaded_skill",
    "other": "unclassified",
}


def _as_blocks(ours: Any) -> dict[str, int]:
    if not isinstance(ours, dict):
        return {}
    return {
        _CATEGORY_LABEL[cat]: _coerce_int(v)
        for cat, v in ours.items()
        if cat in _CATEGORY_LABEL and _coerce_int(v) > 0
    }


def _usage_read(slot: str) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    """The slot's rendered ``usage`` context, its in-memory context state, and the
    revision both came from: one warm read, so the two always agree."""
    from kiro_crew.crew_log.projection import projection_of, read_slot_state

    try:
        checkpoint, revision = read_slot_state(slot, "usage")
        folded = projection_of(checkpoint, revision=revision).value
        state = checkpoint.state
        ctx = state.get("ctx") if isinstance(state, dict) else None
    except Exception:
        logger.warning("usage fold unreadable for slot %s", slot, exc_info=True)
        return {}, None, 0
    context = folded.get("context") if isinstance(folded, dict) else None
    return (
        context if isinstance(context, dict) else {},
        ctx if isinstance(ctx, dict) else None,
        int(revision),
    )


def context_turn_detail(slot: str, ordinal: int) -> dict[str, Any] | None:
    """One kept turn of *slot* as its whole panel, from the in-memory fold state.

    The pushed value carries only the chart's numbers per turn; the panel asks for a
    past turn's own blocks and engine parts here when that turn is picked. None when
    no kept row has *ordinal* (it left the ring, or never was).
    """
    from kiro_crew.crew_log.projection import context_turn

    _context, ctx, revision = _usage_read(slot)
    detail = context_turn(ctx, ordinal) if ctx is not None else None
    if detail is None:
        return None
    return {"slot": slot, "turn": detail, "revision": revision}


def context_trace(slot: str, days: int = 14) -> dict[str, Any]:
    """The slot's context, from its ``usage`` projection: the newest turn and a trend.

    ``latest`` is the newest turn's one breakdown: Kiro Crew's five running sums since
    the agent loop's context last restarted, and the agent loop's part -- the engine's
    own categories when it measured the turn, otherwise one part estimated from the
    newest occupancy reading at ``chars_per_token`` and marked so. ``trend`` is the
    newest 30 turns, each with its own turn ordinal, Crew's five sums and the agent
    loop's part; ``earlier`` counts the turns before them and ``start`` is the newest
    session-start turn, listed apart with the size its own panel heads with. A past
    turn's whole panel is read on demand (:func:`context_turn_detail`). ``revision``
    is the fold revision the page was read at, the panel's cache key for those reads.

    ``turns``, ``totals`` and ``injected_chars`` are built from the kept turns for the
    Session Breakdown tree, which reads them per sub-agent, each turn with what IT
    sent. ``peak_context_used`` and ``context_window`` are ONE pair from the fullest
    turn closed in the last *days* (:func:`context_peak`), else 0 and the configured
    window; *days* selects nothing else and is echoed as ``window_days``.
    """
    from kiro_crew.crew_log.projection import context_peak, context_turn_blocks

    context, ctx, revision = _usage_read(slot)
    latest = context.get("latest") if isinstance(context.get("latest"), dict) else None
    trend = [row for row in (context.get("trend") or []) if isinstance(row, dict)]
    start = context.get("start") if isinstance(context.get("start"), dict) else None

    turns: list[dict[str, Any]] = []
    for ordinal, phase, blocks in context_turn_blocks(ctx) if ctx is not None else []:
        # What THIS turn sent, by label: the Session Breakdown tree draws one bar per
        # turn under a per-turn caption, and the five sums are running totals since
        # the context restarted (the panel's resident view), which would grow each bar.
        if not blocks:
            continue
        turns.append(
            {
                "phase": phase,
                "blocks": blocks,
                "total_chars": sum(blocks.values()),
                "ordinal": ordinal,
                "first_start": phase == "session_start",
            }
        )
    totals = _as_blocks(latest.get("ours")) if latest else {}
    # The fullest turn INSIDE the requested days, as one reading and its own window:
    # the Session Breakdown tree divides one by the other, and a model switch moves
    # the window, so the pair must come from the same turn.
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    used, window = context_peak(ctx, since_ms) if ctx is not None else (0, 0)

    recording = crew_log_enabled()
    payload: dict[str, Any] = {
        "slot": slot,
        "revision": revision,
        "latest": latest,
        "trend": trend,
        "start": start,
        "earlier": _coerce_int(context.get("earlier")),
        "turn_count": _coerce_int(context.get("turns")),
        "chars_per_token": context.get("chars_per_token"),
        "turns": turns,
        "totals": totals,
        "injected_chars": sum(totals.values()),
        "user_chars": totals.get(USER_LABEL, 0),
        "peak_context_used": used,
        "context_window": window,
        "window_days": days,
        "recording": recording,
    }
    if not recording:
        payload["env_file"] = env_file_display()
    return payload


def _row_iso(raw: Any) -> tuple[float, str] | None:
    """A fold row's ``ts`` as ``(epoch seconds, ISO-8601 UTC)``, or ``None``."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    seconds = raw / 1000.0
    try:
        return seconds, datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
