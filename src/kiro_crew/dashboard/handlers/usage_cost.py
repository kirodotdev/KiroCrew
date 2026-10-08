"""The Telemetry panel's spend breakdown: a window's credits by session, category and
model, against the preceding window.

An owner of :mod:`kiro_crew.dashboard.handlers.usage`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import heapq
import json
import math
import time
from typing import Any, Iterator

from kiro_crew.dashboard.handlers.usage_spend import _finite_number
from kiro_crew.dashboard.handlers.usage_store import (
    NAVIGABLE_CATEGORY,
    SPEND_WINDOW_DAYS,
    _coerce_int,
    _parse_row_ts,
    _shards_in_window,
    is_session_slot,
    session_category,
)
from kiro_crew.jsonl_util import bounded_records
from kiro_crew.messaging.link import telemetry_channel_of

_COST_CACHE: dict[str, Any] | None = None


_COST_CACHE_KEY: tuple[Any, ...] | None = None


_COST_CACHE_TS: float = 0.0


_COST_CACHE_TTL = 30.0


# A payload backstop, not a display choice: the panel polls every few seconds,
# so an account with thousands of sessions should not ship all of them on every
# refetch. The shown/total pair travels with the list either way, so truncation
# is stated rather than implied.
_COST_TOP_CONVOS = 500
# How many sessions ``slot_spend`` keeps for the folder roll-up, one key set
# shared by both periods. It rides the memoised payload, so it needs a count
# bound like every retained list. :func:`_slot_spend` applies it once the rows
# are read. Sessions past the cap are summed into ``unsorted``, so every credit
# is still counted, just not attributed to a folder.
_COST_SLOT_SPEND_CAP = 5000
#: Longest session key ``slot_spend`` retains. The count cap above bounds what
#: the cache holds only when each retained field is bounded too, and the key is
#: a field: a slot name reaches the token rows from the API. A longer key is
#: never kept; its spend goes to ``unsorted`` (counted in Unfiled, not
#: attributed). Generous by design and the same bound as
#: ``messaging/turn_ceiling.py``'s ``MAX_RETAINED_KEY_CHARS``: real keys are
#: ``chat-<n>-<ts>``, ``cron:<job>:<run>``, ``task-review-<token>`` or a surface
#: name plus provider ids, well inside it.
_COST_SLOT_KEY_MAX_CHARS = 256


# Below this, a per-turn growth slope is noise rather than a trend.
_COST_MIN_GROWTH_TURNS = 6


# Occupancy at which the runtime compacts, so the projection has a real target.
_COMPACTION_PCT = 90.0


# A turn-to-turn occupancy fall this large is a compaction, not measurement
# jitter, and so ends the segment a growth slope may be fitted on. Occupancy
# does drift down by a fraction of a point between turns (what counts toward
# `context_used` varies), which is why the threshold is not simply "any fall".
_COMPACTION_DROP_PCT = 1.0


def _per_turn(e: dict[str, Any], places: int = 1) -> float:
    t = int(e["turns"])
    return round(float(e["credits"]) / t, places) if t else 0.0


def rank_spend(
    bucket: dict[str, dict[str, Any]],
    deltas: dict[str, float] | None,
    total: float,
    places: int = 1,
) -> list[dict[str, Any]]:
    """Rank one spend grouping, largest first, with share and period delta.

    *bucket* maps a grouping key to ``{"name", "credits", "turns"}`` (plus any
    extra identity fields, which are copied through); *deltas* maps the same key
    to the preceding period's credits. The key and the displayed name differ only
    for folders, whose names are not unique, so the delta is looked up by key.
    *places* is the rounding of the amounts: one decimal for credits, more for
    dollars (see ``cost_breakdown``'s ``unit``).
    """
    out = []
    for key, e in sorted(bucket.items(), key=lambda kv: -float(kv[1]["credits"])):
        row = {
            **{k: v for k, v in e.items() if k not in ("credits", "turns")},
            "name": e["name"],
            "credits": round(float(e["credits"]), places),
            "turns": int(e["turns"]),
            "per_turn": _per_turn(e, places),
            "share_pct": round(float(e["credits"]) / total * 100, 1) if total else 0.0,
        }
        if deltas is not None:
            was = deltas.get(key, 0.0)
            # A name with no prior spend has no percentage to report; the
            # frontend renders that as "new" rather than a fake infinity.
            row["delta_pct"] = (
                round((float(e["credits"]) - was) / was * 100, 0) if was > 0 else None
            )
        out.append(row)
    return out


def _slot_spend(
    current: dict[str, dict[str, Any]],
    prior: dict[str, float],
    prior_transcripts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Cut the per-session spend of one pass down to what the cache may hold.

    *current* maps a session key to its exact current-period ``credits`` and
    ``turns`` (``cost_breakdown``'s ``convos``, plus ``""`` for rows with no
    slot); *prior* maps a key to its exact prior-period credits. ONE key set
    serves both periods, so a kept session keeps its prior spend and its
    folder's delta compares like with like. The empty key is always kept: it is
    Unfiled whatever happens. Of the rest, a key longer than
    ``_COST_SLOT_KEY_MAX_CHARS`` is never kept, and the top
    ``_COST_SLOT_SPEND_CAP`` by current credits, then prior credits, then key,
    are. Every other session's credits, turns and prior credits are summed into
    ``unsorted``, so kept plus ``unsorted`` is every credit and turn.

    ``transcripts`` maps a kept key to the transcript its rows recorded, the
    current period's over *prior_transcripts*. One longer than
    ``_COST_SLOT_KEY_MAX_CHARS`` is not kept, and its key falls back to the
    key-shape rules.
    """
    ranked = heapq.nsmallest(
        _COST_SLOT_SPEND_CAP,
        (k for k in {*current, *prior} if k and len(k) <= _COST_SLOT_KEY_MAX_CHARS),
        key=lambda k: (
            -float(current[k]["credits"]) if k in current else 0.0,
            -prior.get(k, 0.0),
            k,
        ),
    )
    kept = {"", *ranked}
    unsorted: dict[str, Any] = {"credits": 0.0, "turns": 0, "prior_credits": 0.0}
    kept_current: dict[str, dict[str, Any]] = {}
    for k, e in current.items():
        if k in kept:
            kept_current[k] = {"credits": float(e["credits"]), "turns": int(e["turns"])}
        else:
            unsorted["credits"] += float(e["credits"])
            unsorted["turns"] += int(e["turns"])
    kept_prior: dict[str, float] = {}
    for k, credits in prior.items():
        if k in kept:
            kept_prior[k] = credits
        else:
            unsorted["prior_credits"] += credits
    recorded = dict(prior_transcripts or {})
    recorded.update(
        (k, str(e.get("transcript") or "")) for k, e in current.items() if e.get("transcript")
    )
    kept_transcripts = {
        k: t for k, t in recorded.items() if k and k in kept and len(t) <= _COST_SLOT_KEY_MAX_CHARS
    }
    return {
        "current": kept_current,
        "prior": kept_prior,
        "unsorted": unsorted,
        "transcripts": kept_transcripts,
    }


def cost_breakdown(days: float = SPEND_WINDOW_DAYS, until: float | None = None) -> dict[str, Any]:
    """Aggregate per-turn spend from the token row store into a cost view.

    Answers "what did the last *days* cost, compared with the *days* before it,
    and what drove the difference". ``credits`` is the billing dimension the
    Kiro harnesses populate, with ``context_used`` as the second real signal; a
    window whose turns carry no credits but a dollar ``cost`` (a dollar-billed
    harness) is aggregated from ``cost`` instead, and ``unit`` names which.

    Three deliberate shapes:

    * **Every** model is reported, never a top-N slice. A truncated list hides a
      model switch, which on real data moved the bill far more than usage volume
      did, and a single-user store is small enough to render complete.
    * ``channel`` is derived from the session key, NOT read from the row's
      ``surface`` field. Historical rows can carry a wrong but non-empty value
      such as ``dashboard`` for a Telegram turn, and the token-row schema has no
      version or writer marker that distinguishes them from trustworthy rows.
      The key therefore remains authoritative until the store gains such a
      boundary; a surface-first fallback would silently misattribute history.
    * Context bands are absolute token counts rather than occupancy ratios:
      spend tracks how many tokens get re-sent, and window sizes differ per
      model, so a ratio would average incomparable populations.

    ``until`` ends the window at that epoch instead of now (see
    :class:`TimeWindow`). The comparison period is always the equal-length
    stretch immediately before the window, whichever way it is anchored.
    """
    now = time.time()
    anchor = now if until is None else until
    upper = math.inf if until is None else until
    cutoff = anchor - (days * 86400)
    prior_cutoff = anchor - (2 * days * 86400)

    shard_paths = _shards_in_window(2 * days, until)
    try:
        cache_key: tuple[Any, ...] | None = (
            days,
            until,
            tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in shard_paths)),
        )
    except OSError:
        cache_key = None
    if (
        cache_key is not None
        and _COST_CACHE_KEY == cache_key
        and _COST_CACHE is not None
        and (now - _COST_CACHE_TS) < _COST_CACHE_TTL
    ):
        return _COST_CACHE

    def _blank() -> dict[str, Any]:
        return {"credits": 0.0, "turns": 0}

    cur_tot, prev_tot = _blank(), _blank()
    by_model: dict[str, dict[str, Any]] = {}
    by_channel: dict[str, dict[str, Any]] = {}
    prev_model: dict[str, float] = {}
    prev_channel: dict[str, float] = {}
    bands: dict[int, list[float]] = {}
    convos: dict[str, dict[str, Any]] = {}
    by_category: dict[str, dict[str, Any]] = {}
    prev_category: dict[str, float] = {}
    # Per-session spend for the folder roll-up. The current period reads
    # ``convos``; these hold what it does not: prior-period credits per session,
    # and current spend on rows with no slot.
    prev_slot: dict[str, float] = {}
    prev_transcript: dict[str, str] = {}
    no_slot = _blank()
    priciest: dict[str, Any] = {"credits": 0.0, "slot": "", "ts": ""}

    def _window_rows() -> Iterator[tuple[dict[str, Any], str, float]]:
        """Each token row stamped inside the window or the one before it."""
        for shard_path in shard_paths:
            try:
                with shard_path.open("rb") as fh:
                    for line in bounded_records(fh, shard_path, label="usage"):
                        try:
                            obj = json.loads(line)
                        except (ValueError, RecursionError):  # past the nesting limit
                            continue
                        if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                            continue
                        ts_raw = str(obj.get("ts") or "")
                        ts_epoch = _parse_row_ts(ts_raw)
                        if ts_epoch is None or ts_epoch < prior_cutoff or ts_epoch >= upper:
                            continue
                        yield obj, ts_raw, ts_epoch
            except (OSError, UnicodeDecodeError):
                continue

    def _billed(obj: dict[str, Any], key: str) -> bool:
        value = _finite_number(obj.get(key))
        return value is not None and value > 0

    # The unit is read off the rows rather than the configured harness: a
    # dollar-billed harness records `cost` and leaves `credits` empty, and the
    # store may hold turns from a harness other than the current default. Any
    # credit-bearing row keeps the view in credits, so a Kiro install stops at
    # its first billed row; only a window with no credits at all and some
    # dollar cost is shown in dollars.
    # shortcut: a store mixing credit and dollar rows shows credits only; split
    # the view per unit when a crew runs both harnesses side by side.
    # Decided on the rows the headline sums: this window's session turns. The
    # window before it may predate a harness switch, so a credit row there must
    # not hold a window of dollar turns at zero; its amounts are then read as
    # dollars too, and a prior window that billed none reads as no prior spend.
    field, unit = "credits", "credits"
    saw_cost = False
    for obj, _ts_raw, ts_epoch in _window_rows():
        row_slot = str(obj.get("slot") or "")
        if ts_epoch < cutoff or (row_slot and not is_session_slot(row_slot)):
            continue
        if _billed(obj, "credits"):
            break
        saw_cost = saw_cost or _billed(obj, "cost")
    else:
        if saw_cost:
            field, unit = "cost", "usd"
    # Dollars keep their cents; credits read to one decimal as before.
    places = 4 if unit == "usd" else 1

    for obj, ts_raw, ts_epoch in _window_rows():
        raw_credits = obj.get(field)
        # Shards written before the persist-side guard can already
        # hold `NaN` / `Infinity` — `json.loads` accepts those bare
        # tokens even though they are not valid JSON. Letting one
        # through would poison every total it touches and make
        # `web.json_response` emit a body no browser can parse, so
        # the row is dropped rather than counted as free. A string
        # or an int wider than a double is dropped the same way; a
        # row with no credits is a turn that billed none.
        credits = 0.0 if not raw_credits else _finite_number(raw_credits)
        if credits is None:
            continue
        model = str(obj.get("model") or "unknown")
        slot = str(obj.get("slot") or "")

        # Dropped here, before ANY accumulator sees it, so every
        # figure on the page is computed over one population. A
        # subagent is a fragment of another session's turn rather
        # than a session, and it carries no field pointing back at
        # the session that spawned it, so it can be neither listed
        # nor attributed. Filtering at the grouping step instead
        # would leave it in the totals and force a reconciling
        # footnote for money with no row.
        if slot and not is_session_slot(slot):
            continue

        if ts_epoch < cutoff:
            prev_tot["credits"] += credits
            prev_tot["turns"] = int(prev_tot["turns"]) + 1
            prev_model[model] = prev_model.get(model, 0.0) + credits
            ch = telemetry_channel_of(slot or None)
            prev_channel[ch] = prev_channel.get(ch, 0.0) + credits
            cat = session_category(slot)
            prev_category[cat] = prev_category.get(cat, 0.0) + credits
            prev_slot[slot] = prev_slot.get(slot, 0.0) + credits
            if slot and obj.get("transcript"):
                prev_transcript[slot] = str(obj["transcript"])
            continue

        cur_tot["credits"] += credits
        cur_tot["turns"] = int(cur_tot["turns"]) + 1
        if credits > float(priciest["credits"]):
            priciest = {"credits": credits, "slot": slot, "ts": ts_raw}

        for bucket, name in (
            (by_model, model),
            (by_channel, telemetry_channel_of(slot or None)),
            (by_category, session_category(slot)),
        ):
            e = bucket.setdefault(name, {"name": name, "credits": 0.0, "turns": 0})
            e["credits"] = float(e["credits"]) + credits
            e["turns"] = int(e["turns"]) + 1

        used = _coerce_int(obj.get("context_used"))
        if used > 0 and credits > 0:
            bands.setdefault(min(used // 200_000, 5), []).append(credits)

        if slot:
            c = convos.setdefault(
                slot,
                {
                    "slot": slot,
                    "credits": 0.0,
                    "turns": 0,
                    "peak_pct": 0.0,
                    "first_ts": ts_epoch,
                    "last_ts": ts_epoch,
                    "_occ": [],
                    "transcript": "",
                },
            )
            if obj.get("transcript"):
                c["transcript"] = str(obj["transcript"])
            c["credits"] = float(c["credits"]) + credits
            c["turns"] = int(c["turns"]) + 1
            c["first_ts"] = min(float(c["first_ts"]), ts_epoch)
            c["last_ts"] = max(float(c["last_ts"]), ts_epoch)
            window = _coerce_int(obj.get("context_window"))
            if used > 0 and window > 0:
                pct = used / window * 100.0
                c["peak_pct"] = max(float(c["peak_pct"]), pct)
                c["_occ"].append((ts_epoch, pct))
        else:
            no_slot["credits"] += credits
            no_slot["turns"] = int(no_slot["turns"]) + 1

    def _store(result: dict[str, Any]) -> dict[str, Any]:
        global _COST_CACHE, _COST_CACHE_KEY, _COST_CACHE_TS
        if cache_key is not None:
            _COST_CACHE, _COST_CACHE_KEY, _COST_CACHE_TS = result, cache_key, now
        return result

    total = float(cur_tot["credits"])

    def _rank(
        bucket: dict[str, dict[str, Any]], deltas: dict[str, float] | None
    ) -> list[dict[str, Any]]:
        return rank_spend(bucket, deltas, total, places)

    slot_current: dict[str, dict[str, Any]] = dict(convos)
    if int(no_slot["turns"]):
        slot_current[""] = no_slot

    band_rows = []
    for idx in sorted(bands):
        vals = bands[idx]
        lo = idx * 200_000
        band_rows.append(
            {
                "label": f"{lo // 1000}k–{(lo + 200_000) // 1000}k" if idx < 5 else "1M+",
                "turns": len(vals),
                "mean_credits": round(sum(vals) / len(vals), places),
            }
        )

    convo_rows = []
    transcripts: dict[str, str] = {}
    for c in sorted(convos.values(), key=lambda x: -float(x["credits"]))[:_COST_TOP_CONVOS]:
        if c["transcript"]:
            transcripts[c["slot"]] = c["transcript"]
        occ = sorted(c.pop("_occ"))
        growth: float | None = None
        to_90: int | None = None
        # Fit the slope only on the stretch since the last compaction, never
        # across the whole window. Occupancy is sawtooth, not monotonic: a
        # compaction drops it back to near-empty, and 7 of the 8 top-spending
        # conversations measured carry between one and four such drops. A secant
        # from the first turn to the last therefore spans discontinuities and is
        # dragged down by every reset, which understates the live growth rate by
        # ~47x on real data (one 104-turn conversation projected 795 turns of
        # headroom while its current segment was climbing at 4%/turn, i.e. ~17
        # turns from compaction). Erring toward "plenty of room left" is the
        # damaging direction for a figure whose whole purpose is a warning.
        seg = occ
        for i in range(1, len(occ)):
            if occ[i][1] < occ[i - 1][1] - _COMPACTION_DROP_PCT:
                seg = occ[i:]
        # A slope needs enough points in the CURRENT segment, not merely enough
        # turns in the window: a long conversation freshly past a compaction
        # knows nothing about its new trajectory yet, and withholding is the
        # honest answer rather than projecting from two or three points.
        if len(seg) >= _COST_MIN_GROWTH_TURNS:
            rate = (seg[-1][1] - seg[0][1]) / (len(seg) - 1)
            if rate > 0:
                growth = round(rate, 2)
                remaining = _COMPACTION_PCT - seg[-1][1]
                to_90 = math.ceil(remaining / rate) if remaining > 0 else 0
        convo_rows.append(
            {
                "slot": c["slot"],
                # The session taxonomy the panel groups by: `bg` for machine
                # work, otherwise the transport it came from. The frontend needs
                # it for two things — the category chip, and linkability, since
                # only a dashboard session has a route to open. Deriving either
                # from the key shape in the frontend would put a second,
                # drifting copy of session-key knowledge there.
                "category": session_category(c["slot"]),
                # The unollapsed label underneath the category, so a `bg` row
                # still says whether it was a subagent, a cron or the heartbeat
                # instead of becoming an anonymous lump.
                "channel": telemetry_channel_of(c["slot"]),
                "credits": round(float(c["credits"]), places),
                "turns": int(c["turns"]),
                "peak_pct": round(float(c["peak_pct"]), 1),
                "span_days": round((float(c["last_ts"]) - float(c["first_ts"])) / 86400, 1),
                # A closed conversation has no stored title, so the frontend
                # labels it by start date -- eight rows all reading "untitled"
                # cannot be told apart, which defeats the ranking.
                "first_ts": round(float(c["first_ts"]), 3),
                "growth_pct_per_turn": growth,
                "turns_to_compaction": to_90,
            }
        )

    prev_credits = float(prev_tot["credits"])
    return _store(
        {
            "window_days": days,
            # What every amount below is counted in: "credits", or "usd" when
            # the window's turns were billed in dollars. The keys keep their
            # `credits` names either way.
            "unit": unit,
            "credits": round(total, places),
            "turns": int(cur_tot["turns"]),
            "per_turn": _per_turn(cur_tot, places),
            "prior_credits": round(prev_credits, places),
            "prior_turns": int(prev_tot["turns"]),
            "prior_per_turn": _per_turn(prev_tot, places),
            "delta_pct": (
                round((total - prev_credits) / prev_credits * 100, 0) if prev_credits > 0 else None
            ),
            "priciest": {
                "credits": round(float(priciest["credits"]), places),
                "slot": priciest["slot"],
                "ts": priciest["ts"],
            },
            "by_model": _rank(by_model, prev_model),
            "by_channel": _rank(by_channel, prev_channel),
            "by_category": _rank(by_category, prev_category),
            # Every session's own spend, current and preceding period, so a
            # caller holding the dashboard state can group by something this
            # module cannot see (the sidebar folder a session is filed in).
            # Internal: the handler consumes and drops it before the payload
            # leaves, since it is one entry per session in the window.
            "slot_spend": _slot_spend(slot_current, prev_slot, prev_transcript),
            "context_bands": band_rows,
            "conversations": convo_rows,
            # Slot -> the transcript its rows recorded, for the endpoint to title
            # a closed linked tab by; the endpoint drops it from the response.
            "transcripts": transcripts,
            "conversation_count": len(convos),
            # The one category with a route. Sent rather than duplicated in
            # the frontend so a change here cannot leave a dead link there.
            "navigable_category": NAVIGABLE_CATEGORY,
        }
    )
