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
from collections.abc import Iterator
from pathlib import Path
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
# How many sessions ``slot_spend`` keeps for the folder roll-up, one key set
# shared by both periods. It rides the memoised payload, so it needs a count
# bound like every retained list. :class:`_SlotSpend` chooses the kept keys while
# the rows are read (never more than twice this many entries), and
# :class:`_ExactSlotSpend` then sums exact spend for those keys only. Sessions
# past the cap are summed into ``overflow``, so every credit is still counted,
# just not attributed to a folder.
_COST_SLOT_SPEND_CAP = 5000
#: Longest session key ``slot_spend`` retains. The count cap above bounds memory
#: only when each retained field is bounded too, and the key is a field: a slot
#: name reaches the token rows from the API, so 5000 entries of an unbounded
#: string is unbounded. A longer key is never admitted; its spend goes to
#: ``over_long``, apart from the count cap's ``overflow``, so it is counted (in
#: Unfiled) but not attributed, and not described as past the cap. Generous by
#: design and the same bound as
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


def _per_turn(e: dict[str, Any]) -> float:
    t = int(e["turns"])
    return round(float(e["credits"]) / t, 1) if t else 0.0


def rank_spend(
    bucket: dict[str, dict[str, Any]], deltas: dict[str, float] | None, total: float
) -> list[dict[str, Any]]:
    """Rank one spend grouping, largest first, with share and period delta.

    *bucket* maps a grouping key to ``{"name", "credits", "turns"}`` (plus any
    extra identity fields, which are copied through); *deltas* maps the same key
    to the preceding period's credits. The key and the displayed name differ only
    for folders, whose names are not unique, so the delta is looked up by key.
    """
    out = []
    for key, e in sorted(bucket.items(), key=lambda kv: -float(kv[1]["credits"])):
        row = {
            **{k: v for k, v in e.items() if k not in ("credits", "turns")},
            "name": e["name"],
            "credits": round(float(e["credits"]), 1),
            "turns": int(e["turns"]),
            "per_turn": _per_turn(e),
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


class _SlotEntry:
    """One session's spend: both periods, one key."""

    __slots__ = ("credits", "turns", "prior", "has_current", "has_prior")

    def __init__(self) -> None:
        self.credits = 0.0
        self.turns = 0
        self.prior = 0.0
        self.has_current = False
        self.has_prior = False

    def count(self, credits: float, prior: bool) -> None:
        if prior:
            self.prior += credits
            self.has_prior = True
        else:
            self.credits += credits
            self.turns += 1
            self.has_current = True


class _SlotSpend:
    """Pass 1 of the per-session spend: chooses which session keys to keep.

    Fed one usage row at a time by :func:`cost_breakdown`. It only CHOOSES the
    kept set; :class:`_ExactSlotSpend` then re-reads the same rows and sums the
    kept sessions' spend exactly. The bound applies at admission, not
    afterwards: a cap applied to a finished map still lets the map grow to one
    entry per distinct key in the window first, and a slot name reaches the
    token rows from the API.

    * A key longer than ``_COST_SLOT_KEY_MAX_CHARS`` is never admitted.
    * When admitting a new key would take the map past ``2 x
      _COST_SLOT_SPEND_CAP`` entries, the lowest-ranked half is dropped first,
      so the map never holds more than twice the cap. :meth:`kept` folds down
      to the cap itself.
    * ONE key set serves both periods, ranked by current credits, then prior
      credits (both descending), then the key, so which sessions are kept is
      deterministic.

    The figures held here are partial whenever a key was dropped and seen
    again (admitted afresh, without its earlier rows), so they only rank and
    are never reported. That also means the kept set can differ from the
    exact top ``_COST_SLOT_SPEND_CAP`` when an evicted key returns later in
    the stream; the sessions it does keep are attributed exactly in both
    periods by the second pass.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[str, _SlotEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, key: str, credits: float, *, prior: bool = False) -> None:
        """Count one usage row toward *key*'s rank: current period, or the prior one."""
        if len(key) > _COST_SLOT_KEY_MAX_CHARS:
            return
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= 2 * _COST_SLOT_SPEND_CAP:
                self._fold(_COST_SLOT_SPEND_CAP)
            entry = self._entries[key] = _SlotEntry()
        entry.count(credits, prior)

    def _fold(self, keep: int) -> None:
        """Keep the top *keep* entries and drop the rest."""
        if len(self._entries) <= keep:
            return
        ranked = sorted(self._entries.items(), key=lambda kv: (-kv[1].credits, -kv[1].prior, kv[0]))
        for key, _entry in ranked[keep:]:
            del self._entries[key]

    def kept(self) -> frozenset[str]:
        """The chosen session keys, at most ``_COST_SLOT_SPEND_CAP`` of them."""
        self._fold(_COST_SLOT_SPEND_CAP)
        return frozenset(self._entries)


class _ExactSlotSpend:
    """Pass 2 of the per-session spend: exact sums for the kept keys only.

    Fed the same rows :class:`_SlotSpend` saw, after it has chosen *kept*. A row
    whose key is kept is summed into that key's entry; every other row is
    summed by WHY it was not kept, because the two causes read differently to a
    user:

    * ``overflow``: a session past ``_COST_SLOT_SPEND_CAP``, the count cap;
    * ``over_long``: a key longer than ``_COST_SLOT_KEY_MAX_CHARS``, which is
      never retained however few sessions the window holds.

    So the map holds at most ``len(kept)`` entries, every kept session's current
    credits, turns and prior credits are exact, and kept plus both remainders is
    every credit and turn in both periods. Neither remainder carries a session
    count: each is summed per row, not per session.
    """

    __slots__ = ("_kept", "_entries", "_capped", "_over_long")

    def __init__(self, kept: frozenset[str]) -> None:
        self._kept = kept
        self._entries: dict[str, _SlotEntry] = {}
        self._capped = _SlotEntry()
        self._over_long = _SlotEntry()

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, key: str, credits: float, *, prior: bool = False) -> None:
        """Count one usage row: *credits* for the current period, or the prior one."""
        if len(key) > _COST_SLOT_KEY_MAX_CHARS:
            self._over_long.count(credits, prior)
        elif key in self._kept:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = _SlotEntry()
            entry.count(credits, prior)
        else:
            self._capped.count(credits, prior)

    @staticmethod
    def _remainder(e: _SlotEntry) -> dict[str, Any]:
        return {"credits": e.credits, "turns": e.turns, "prior_credits": e.prior}

    def result(self) -> dict[str, Any]:
        """The kept sessions per period and the two summed remainders."""
        return {
            "current": {
                k: {"credits": e.credits, "turns": e.turns}
                for k, e in self._entries.items()
                if e.has_current
            },
            "prior": {k: e.prior for k, e in self._entries.items() if e.has_prior},
            "overflow": self._remainder(self._capped),
            "over_long": self._remainder(self._over_long),
        }


def _usage_rows(
    shard_paths: list[Path],
    prior_cutoff: float,
    upper: float = math.inf,
    *,
    seen: dict[Path, int] | None = None,
    upto: dict[Path, int] | None = None,
) -> Iterator[tuple[dict[str, Any], str, float, float, str]]:
    """The token rows :func:`cost_breakdown` counts, as ``(row, ts, epoch, credits, slot)``.

    One filter for every pass over the shards, so the passes agree on the
    population: a ``tokens`` row with a parseable timestamp inside the two-period
    window (before *upper*, the window's end), finite credits, and a slot that is empty or a session (see the
    comment at the session test). An unreadable shard is skipped.

    *seen*, when given, is filled with how many rows each shard yielded. *upto*
    replays a previous pass: each shard yields at most that many rows, and a
    shard missing from it yields none. The shards are append-only, so the first
    N rows are the same rows, and a turn persisted between two passes cannot
    reach the second one without having reached the first.
    """
    for shard_path in shard_paths:
        limit = None if upto is None else upto.get(shard_path, 0)
        if limit == 0:
            continue
        n = 0
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

                    n += 1
                    if seen is not None:
                        seen[shard_path] = n
                    yield obj, ts_raw, ts_epoch, credits, slot
                    if limit is not None and n >= limit:
                        break
        except (OSError, UnicodeDecodeError):
            continue


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
    slot_keys = _SlotSpend()
    priciest: dict[str, Any] = {"credits": 0.0, "slot": "", "ts": ""}
    rows_read: dict[Path, int] = {}

    for obj, ts_raw, ts_epoch, credits, slot in _usage_rows(
        shard_paths, prior_cutoff, upper, seen=rows_read
    ):
        model = str(obj.get("model") or "unknown")

        if ts_epoch < cutoff:
            prev_tot["credits"] += credits
            prev_tot["turns"] = int(prev_tot["turns"]) + 1
            prev_model[model] = prev_model.get(model, 0.0) + credits
            ch = telemetry_channel_of(slot or None)
            prev_channel[ch] = prev_channel.get(ch, 0.0) + credits
            cat = session_category(slot)
            prev_category[cat] = prev_category.get(cat, 0.0) + credits
            slot_keys.add(slot, credits, prior=True)
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
        slot_keys.add(slot, credits)

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

    # Second pass, over exactly the rows the first one read: exact spend for the
    # keys the first pass chose. Choosing first is what lets a kept session
    # keep its prior-period spend even when the first pass evicted it before its
    # current rows arrived; bounded by the kept set, like the first pass.
    slot_spend = _ExactSlotSpend(slot_keys.kept())
    for _obj, _ts_raw, ts_epoch, credits, slot in _usage_rows(
        shard_paths, prior_cutoff, upper, upto=rows_read
    ):
        slot_spend.add(slot, credits, prior=ts_epoch < cutoff)

    def _store(result: dict[str, Any]) -> dict[str, Any]:
        global _COST_CACHE, _COST_CACHE_KEY, _COST_CACHE_TS
        if cache_key is not None:
            _COST_CACHE, _COST_CACHE_KEY, _COST_CACHE_TS = result, cache_key, now
        return result

    total = float(cur_tot["credits"])

    def _rank(
        bucket: dict[str, dict[str, Any]], deltas: dict[str, float] | None
    ) -> list[dict[str, Any]]:
        return rank_spend(bucket, deltas, total)

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
            # Every session's own spend, current and preceding period, so a
            # caller holding the dashboard state can group by something this
            # module cannot see (the sidebar folder a session is filed in).
            # Internal: the handler consumes and drops it before the payload
            # leaves, since it is one entry per session in the window.
            "slot_spend": slot_spend.result(),
            "context_bands": band_rows,
            "conversations": convo_rows,
            "conversation_count": len(convos),
            # The one category with a route. Sent rather than duplicated in
            # the frontend so a change here cannot leave a dead link there.
            "navigable_category": NAVIGABLE_CATEGORY,
        }
    )
