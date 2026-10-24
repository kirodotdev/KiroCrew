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


def context_trace(slot: str, days: int = 14) -> dict[str, Any]:
    """Per-turn injection breakdown for one slot, oldest turn first.

    Served from the slot's ``usage`` PROJECTION -- one memoised fold read -- and no
    longer by scanning the token shards. The scan it replaces opened every shard in
    the window, JSON-parsed every row in each, and threw away all but this slot's:
    work proportional to the whole installation's turn volume to answer a question
    about one session. The crew log already records the same composition
    (``context/composed``, which the ``usage`` fold folds into ``context.turns``),
    so the projection is where the answer lives.

    Billing stays out of the payload: :func:`slot_turn_usage` is the per-turn reader
    for ``credits`` / ``duration_ms``, and this trace answers only "what was
    injected".

    Kept out of the OTEL pipeline for the same reason as :func:`context_occupancy`:
    this is per-session, per-turn detail, and slot keys are unbounded-cardinality
    labels that must not become metric labels. The bounded half (block label ->
    size, aggregated) is what belongs on a metric; this is the drill-down.

    ONE BOUND on the payload: *days*. Turns older than the window are excluded, so
    ``window_days`` keeps meaning what it meant and a caller asking for 14 days is
    still answered about 14 days. The fold's own truncation needs no second field,
    because every row carries its own ``ordinal`` -- a reader shown a truncated list
    reads the first row's true position and knows exactly how much precedes it, which
    a whole-session drop count applied to an array index could never tell it.

    ``peak_context_used`` and ``context_window`` are the PROVIDER's own occupancy
    reading and the window it was taken against, and they are ONE pair from ONE turn
    -- the fullest turn INSIDE the day window. Both halves matter. The pair, because
    the Session Breakdown tree divides one by the other, and a reading over a window
    from somewhere else describes no turn that ran (a model switch moves the window).
    Inside the window, because the caller asked about a span of days: on a long
    session the fullest turn is frequently older than every row in that span. Neither
    is derived from the turn's token counts, which are billing summed over every model
    call and on a tool-using turn exceed the window they would be divided by.

    HISTORY BEFORE THIS RELEASE. The crew log is the only source now, so a session
    whose ``context/composed`` entries predate this fold reads with an unstated
    ``phase`` (its chart does not split the session-start turn out) and no occupancy
    until its first turn closes under this build; a session recorded with the crew log
    switched off reads empty. No read-through to the token shards is offered, and that
    is deliberate rather than deferred: this release stops writing ``ctx_blocks`` to
    the row, and the scan it would perform was bounded to ``days``, so such a fallback
    could serve data for at most that many days after release and would read nothing
    for the rest of its life. A read path with a provable expiry date is worse than
    none. Both cases self-heal as new turns are recorded.

    Block sizes are in characters and occupancy is in tokens.
    """
    # Imported here rather than at module scope, the same way ``work_ledger`` reaches
    # this function: the projection module pulls in the ledger and work stores, and
    # this handler module is imported from the route table they in turn reach.
    from kiro_crew.crew_log.projection import read_slot_projection

    try:
        folded = read_slot_projection(slot, "usage").value
    except Exception:
        # A damaged or unreadable log reads as "nothing folded" rather than as a 500,
        # the same contract the panel and work readers keep. WARNING because a trace
        # that silently stopped updating has no other trace of its own, and it
        # reproduces on every read until the log is repaired.
        logger.warning("usage fold unreadable for slot %s", slot, exc_info=True)
        folded = {}
    context = folded.get("context") if isinstance(folded, dict) else None
    if not isinstance(context, dict):
        context = {}
    rows = context.get("turns")
    if not isinstance(rows, list):
        rows = []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    turns: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    peak_used = 0
    peak_window = 0
    # Whether any row in the window carried a reading. It separates two causes of a
    # zero window that must not share an answer: no reading in this span at all (the
    # configured size is then the best a reader can be given), or a reading whose
    # window the provider never stated (there IS no size it was measured against).
    peak_seen = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = _row_iso(row.get("ts"))
        if stamp is None:
            # A row this reader cannot DATE cannot be placed inside or outside the
            # window, and the window is the one thing the caller asked about.
            continue
        when, iso = stamp
        if when < cutoff:
            continue
        sources = row.get("sources")
        if not isinstance(sources, dict):
            continue
        blocks = {str(k): _coerce_int(v) for k, v in sources.items() if _coerce_int(v) > 0}
        if not blocks:
            continue
        for label, size in blocks.items():
            totals[label] = totals.get(label, 0) + size
        # PRESENCE of the key, not its value, is what says this row's turn reported
        # occupancy: the fold stamps both fields only when a closer carried them, so a
        # row with no reading is absent rather than zero. Testing the value instead
        # would make an unreported turn indistinguishable from a turn that genuinely
        # measured zero, and every row would then claim to be a reading of 0.
        has_reading = "used" in row
        used = _coerce_int(row.get("used"))
        # The window this row's occupancy is a fraction of. For a MEASURED turn it is
        # the provider's own ``used_window`` -- the size the reading was actually taken
        # against, which a model switch moves -- so ``context_used / context_window``
        # is a coherent ratio from one measurement. The configured ``window`` (stamped
        # at composition from the newest request/configured) is only the fallback for
        # an UNMEASURED turn, which has no reading and so no window of its own; pairing
        # a measured ``used`` with the configured size instead would divide the
        # provider's reading by a size it was never measured against. This mirrors the
        # peak below, which already takes its window from ``used_window``.
        row_window = (
            _coerce_int(row.get("used_window")) if has_reading else _coerce_int(row.get("window"))
        )
        turns.append(
            {
                "ts": iso,
                "phase": str(row.get("phase") or ""),
                "blocks": blocks,
                # The fold records the composition's own total, which is what the
                # writer measured; summing the blocks would silently drop whatever
                # the row reports as omitted detail.
                "total_chars": _coerce_int(row.get("chars")) or sum(blocks.values()),
                # The provider's occupancy reading for the turn this composition
                # belongs to, 0 when that turn reported none.
                "context_used": used,
                "context_window": row_window,
                "model": str(row.get("model") or ""),
                # The row's EXACT position in the whole session history, assigned by the
                # fold before any truncation. A reader shows this as the turn's number
                # directly, so it stays true no matter how many older rows the fold
                # dropped or this day view excluded -- applying one whole-session omitted
                # count to an array index would corrupt it, because the index counts only
                # the rows still present AND inside the window.
                "ordinal": _coerce_int(row.get("ordinal")),
                # The fold's mark for the session's real start, so the panel labels
                # only that row "session start" and any later one a rebuild.
                "first_start": row.get("first_start") is True,
                # What the agent engine measured of the same turn, and its time to
                # first output; ``None`` for a turn it did not measure.
                **_engine_view(row.get("measured")),
            }
        )
        # The peak INSIDE the requested window, with the window that same reading was
        # measured against. Taken here rather than from a session-wide figure because
        # the caller asked about a span of days: on a long session the fullest turn is
        # frequently older than every row in that span, and reporting it would answer a
        # question nobody asked.
        #
        # Fullest means the highest ``used / used_window`` RATIO, not the highest
        # absolute ``used``: a model switch moves the window, so a turn with more used
        # tokens against a larger window can be LESS full than a turn with fewer used
        # tokens against a smaller one -- comparing ``used`` alone would crown the wrong
        # turn and then divide it by a size it was never measured against. Only a
        # "valid pair" (a reading whose window is positive) has a defined ratio, so a
        # reading with no stated window is not a peak candidate: it has no size to be a
        # fraction of, and letting it win would report a peak over a zero window. The
        # ratio is compared by cross-multiplication (``used * peak_window`` vs
        # ``peak_used * window``) to stay integer-exact and never divide by zero; both
        # windows are positive here. ``>=`` keeps the LATEST among equally-full turns,
        # the reading describing the model currently running.
        row_used_window = _coerce_int(row.get("used_window"))
        if has_reading and row_used_window > 0:
            fuller = not peak_seen or used * peak_window >= peak_used * row_used_window
            if fuller:
                peak_used = used
                peak_window = row_used_window
                peak_seen = True

    recording = crew_log_enabled()
    payload: dict[str, Any] = {
        "slot": slot,
        "turns": turns,
        "totals": totals,
        "injected_chars": sum(totals.values()),
        "user_chars": totals.get(USER_LABEL, 0),
        "peak_context_used": peak_used,
        # The peak's own window once a reading exists in this span; otherwise the size
        # the session is configured with, which is what a reader with no reading can
        # still be told.
        "context_window": peak_window if peak_seen else _coerce_int(context.get("window")),
        "window_days": days,
        # Engine categories this build does not know, by name; see ``_engine_view``.
        "engine_unknown": (
            context.get("measured_unknown")
            if isinstance(context.get("measured_unknown"), dict)
            else {}
        ),
        # Whether the gateway is recording at all. An empty list cannot say, and the
        # two cases want different words: a session whose next turn will fill the
        # panel, and a gateway whose ``KIROCREW_CREW_LOG`` switched recording off, for
        # which nothing will ever arrive. Same field, same reading, as the crew-log
        # fold read's own ``recording``.
        "recording": recording,
    }
    if not recording:
        # The ``.env`` the gateway reads, so the empty state names the file to edit,
        # as the Crew Log panel's off state does.
        payload["env_file"] = env_file_display()
    return payload


def _engine_view(measured: Any) -> dict[str, Any]:
    """A row's engine measurement as the panel reads it: ``engine`` and ``ttft_ms``.

    Every figure in ``engine`` is in CHARACTERS and describes the turn's LAST model
    request, as the engine measured it. Its ``history.user`` covers the user text of
    every turn still in the engine's context, and Kiro Crew composed most of it, so the
    fold's running sum of composed characters (``crew_chars``) is subtracted to leave
    ``user_other_chars``: user-role text Kiro Crew did not compose (steering, engine
    wake-ups). The subtraction is clamped at zero AND reported: ``mismatch_chars`` is
    how far Crew's own count exceeds the engine's, which happens when the engine
    dropped user text Crew still counts (a rewind, a summary that absorbed the turn).
    ``approximate`` says a turn was sent again since the sum restarted, which is
    when that can happen. The engine counts UTF-16 code units and Crew counts code
    points, so each character outside the Basic Multilingual Plane (most emoji) in
    a prompt shows as one character of ``user_other_chars``.

    ``engine`` is ``None`` when the turn carries no breakdown -- an older engine, a
    non-KAS backend, a turn that failed before its footer -- so the panel says "not
    measured" rather than drawing zeros. ``ttft_ms`` is ``None`` the same way.
    """
    if not isinstance(measured, dict):
        return {"engine": None, "ttft_ms": None}
    ttft = measured.get("ttft") if isinstance(measured.get("ttft"), dict) else None
    if not measured.get("breakdown"):
        return {"engine": None, "ttft_ms": ttft}
    raw_history = measured.get("history")
    history: dict[str, Any] = raw_history if isinstance(raw_history, dict) else {}
    crew = _coerce_int(measured.get("crew_chars"))
    user = history.get("user")
    engine: dict[str, Any] = {
        "total_chars": measured.get("total_chars"),
        "model_calls": measured.get("model_calls"),
        "categories": dict(measured.get("categories") or {}),
        "history": dict(history),
        "unknown_chars": _coerce_int(measured.get("unknown_chars")),
        "compacted": measured.get("compacted") is True,
        "schema_known": measured.get("schema_known") is not False,
        # A turn was composed again (retry, regenerate, rewind) since the sum last
        # restarted, so ``crew_chars`` may count an attempt the engine dropped.
        "approximate": measured.get("rerun") is True,
        "crew_chars": crew,
        "user_other_chars": max(0, user - crew) if isinstance(user, int) else None,
        "mismatch_chars": max(0, crew - user) if isinstance(user, int) else 0,
    }
    return {"engine": engine, "ttft_ms": ttft}


def _row_iso(raw: Any) -> tuple[float, str] | None:
    """A fold row's ``ts`` as ``(epoch seconds, ISO-8601 UTC)``, or ``None``.

    The crew log stamps an entry in epoch MILLISECONDS, and the payload's declared
    shape is a string, so both forms are produced here from one conversion rather
    than at the two places that need them -- a reader that dated a row one way and
    displayed it another could show a turn it had excluded.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    seconds = raw / 1000.0
    try:
        return seconds, datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        # A stamp outside the platform's representable range dates nothing.
        return None
