"""The Telemetry panel's spend breakdown: a window's credits by session, category and
model, against the preceding window.

An owner of :mod:`kiro_crew.dashboard.handlers.usage`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

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


# Below this, a per-turn growth slope is noise rather than a trend.
_COST_MIN_GROWTH_TURNS = 6


# Occupancy at which the runtime compacts, so the projection has a real target.
_COMPACTION_PCT = 90.0


# A turn-to-turn occupancy fall this large is a compaction, not measurement
# jitter, and so ends the segment a growth slope may be fitted on. Occupancy
# does drift down by a fraction of a point between turns (what counts toward
# `context_used` varies), which is why the threshold is not simply "any fall".
_COMPACTION_DROP_PCT = 1.0


def cost_breakdown(days: float = SPEND_WINDOW_DAYS, until: float | None = None) -> dict[str, Any]:
    """Aggregate per-turn spend from the token row store into a cost view.

    Answers "what did the last *days* cost, compared with the *days* before it,
    and what drove the difference". ``credits`` is the only billing dimension the
    acp provider populates -- input/output/cache/cost come back hard zero -- so
    it is the unit here, with ``context_used`` as the second real signal.

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
    priciest: dict[str, Any] = {"credits": 0.0, "slot": "", "ts": ""}

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
                    ts_raw = str(obj.get("ts") or "")
                    ts_epoch = _parse_row_ts(ts_raw)
                    if ts_epoch is None or ts_epoch < prior_cutoff or ts_epoch >= upper:
                        continue

                    credits = float(obj.get("credits") or 0.0)
                    # Shards written before the persist-side guard can already
                    # hold `NaN` / `Infinity` — `json.loads` accepts those bare
                    # tokens even though they are not valid JSON. Letting one
                    # through would poison every total it touches and make
                    # `web.json_response` emit a body no browser can parse, so
                    # the row is dropped rather than counted as free.
                    if not math.isfinite(credits):
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
                            },
                        )
                        c["credits"] = float(c["credits"]) + credits
                        c["turns"] = int(c["turns"]) + 1
                        c["first_ts"] = min(float(c["first_ts"]), ts_epoch)
                        c["last_ts"] = max(float(c["last_ts"]), ts_epoch)
                        window = _coerce_int(obj.get("context_window"))
                        if used > 0 and window > 0:
                            pct = used / window * 100.0
                            c["peak_pct"] = max(float(c["peak_pct"]), pct)
                            c["_occ"].append((ts_epoch, pct))
        except (OSError, UnicodeDecodeError):
            continue

    def _store(result: dict[str, Any]) -> dict[str, Any]:
        global _COST_CACHE, _COST_CACHE_KEY, _COST_CACHE_TS
        if cache_key is not None:
            _COST_CACHE, _COST_CACHE_KEY, _COST_CACHE_TS = result, cache_key, now
        return result

    def _per_turn(e: dict[str, Any]) -> float:
        t = int(e["turns"])
        return round(float(e["credits"]) / t, 1) if t else 0.0

    total = float(cur_tot["credits"])

    def _rank(
        bucket: dict[str, dict[str, Any]], deltas: dict[str, float] | None
    ) -> list[dict[str, Any]]:
        out = []
        for e in sorted(bucket.values(), key=lambda x: -float(x["credits"])):
            row = {
                "name": e["name"],
                "credits": round(float(e["credits"]), 1),
                "turns": int(e["turns"]),
                "per_turn": _per_turn(e),
                "share_pct": round(float(e["credits"]) / total * 100, 1) if total else 0.0,
            }
            if deltas is not None:
                was = deltas.get(str(e["name"]), 0.0)
                # A model with no prior spend has no percentage to report; the
                # frontend renders that as "new" rather than a fake infinity.
                row["delta_pct"] = (
                    round((float(e["credits"]) - was) / was * 100, 0) if was > 0 else None
                )
            out.append(row)
        return out

    band_rows = []
    for idx in sorted(bands):
        vals = bands[idx]
        lo = idx * 200_000
        band_rows.append(
            {
                "label": f"{lo // 1000}k–{(lo + 200_000) // 1000}k" if idx < 5 else "1M+",
                "turns": len(vals),
                "mean_credits": round(sum(vals) / len(vals), 1),
            }
        )

    convo_rows = []
    for c in sorted(convos.values(), key=lambda x: -float(x["credits"]))[:_COST_TOP_CONVOS]:
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
                "credits": round(float(c["credits"]), 1),
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
            "credits": round(total, 1),
            "turns": int(cur_tot["turns"]),
            "per_turn": _per_turn(cur_tot),
            "prior_credits": round(prev_credits, 1),
            "prior_turns": int(prev_tot["turns"]),
            "prior_per_turn": _per_turn(prev_tot),
            "delta_pct": (
                round((total - prev_credits) / prev_credits * 100, 0) if prev_credits > 0 else None
            ),
            "priciest": {
                "credits": round(float(priciest["credits"]), 1),
                "slot": priciest["slot"],
                "ts": priciest["ts"],
            },
            "by_model": _rank(by_model, prev_model),
            "by_channel": _rank(by_channel, prev_channel),
            "by_category": _rank(by_category, prev_category),
            "context_bands": band_rows,
            "conversations": convo_rows,
            "conversation_count": len(convos),
            # The one category with a route. Sent rather than duplicated in
            # the frontend so a change here cannot leave a dead link there.
            "navigable_category": NAVIGABLE_CATEGORY,
        }
    )
