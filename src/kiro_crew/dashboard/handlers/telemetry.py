"""Telemetry handlers — read the local OTEL metric shards for the dashboard.

An OpenTelemetry recorder's default sink is per-process JSONL
under ``~/.kiro/crew/metrics/metrics-YYYY-MM-DD-<pid>.jsonl`` (see
``kiro_crew.metrics.local_exporter``). Each line is one export cycle serialized
via ``MetricsData.to_json()`` — resource_metrics -> scope_metrics -> metrics ->
data.data_points, where a histogram data point carries ``bucket_counts`` +
``explicit_bounds`` + ``count``/``sum``/``min``/``max`` and a sum/counter data
point carries ``value``.

This module scans those shards (windowed + cached, mirroring the token-usage
handler in ``usage.py``), aggregates the session-startup histogram into
p50/p90 split by cold/warm (the ``spawned`` attribute) + an outcome breakdown,
and generically surfaces every other ``kirocrew.*`` metric so newly-added emit
call-sites (warm-pool acquire, MCP/skill lazy-load) show up without a code
change here.

Cross-process note: the startup metric is emitted by the ACP/gateway processes,
NOT the dashboard process, so an in-memory reservoir in this process could never
observe it — reading the durable shards is the only correct cross-process path.

Percentiles are interpolated from the histogram buckets (the DELTA-temporality
exporter + the explicit-bucket View in ``provider.py`` make this meaningful and
day-additive). mean/min/max are exact from the data point.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import __version__, beacon
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.handlers.usage import context_occupancy, context_trace, cost_breakdown
from kiro_crew.dashboard.state import NEW_SESSION_TITLE
from kiro_crew.hooks import validate_file_path
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_STARTUP_METRIC = "kirocrew.session.startup.duration"
_TURN_METRIC = "kirocrew.turn.duration"
# The end-to-end startup point. The claude path emits no ``phase`` attribute at
# all, so an absent phase is treated as the total (see _aggregate).
_PHASE_TOTAL = "total"
_WINDOW_DAYS = 14

# Spend is compared against the preceding period of the same length, so the
# window is a week: "more or less than last week" is the question, and a
# 14-day window would have no equal-length predecessor inside the retention.
_COST_WINDOW_DAYS = 7

# Attribute keys the generic ``other`` histograms are additionally split on, so
# one side of a split can be reported on its own.
#
# Restricted to a NAMED set of low-cardinality flags rather than splitting on
# every attribute present: ``kirocrew.gateway.request.duration`` carries
# method+route, which would grow one sub-histogram per endpoint and force an
# arbitrary truncation cap on the payload. ``warm`` is boolean, so the split is
# two entries wide and needs no cap.
_OTHER_SPLIT_ATTRS = frozenset({"warm"})

# (shard-fingerprint, TTL) cache — shards are append-only, so a change to any
# shard's (mtime, size) invalidates the cache exactly when needed (same pattern
# as usage._parse_token_history).
_CACHE: dict[str, Any] | None = None
_CACHE_KEY: tuple[tuple[str, float, int], ...] | None = None
_CACHE_TS: float = 0.0
_CACHE_TTL = 30.0


def _telemetry_cfg() -> tuple[bool, Path]:
    """Return (enabled, metrics_dir), resolved the same way the exporter is."""
    enabled = False
    directory = config_dir() / "metrics"
    try:
        cfg = KiroCrewConfig.load().telemetry
        enabled = bool(cfg.enabled)
        if getattr(cfg, "local_dir", None):
            directory = Path(cfg.local_dir).expanduser()
    except Exception:
        logger.debug("telemetry config load failed; assuming disabled", exc_info=True)
    return enabled, directory


def _shards_in_window(directory: Path, days: int) -> list[Path]:
    """Shards whose filename date falls inside the last ``days`` days."""
    if not directory.exists():
        return []
    # Security: telemetry.local_dir is user-configurable (and
    # expanduser'd), so refuse to read a metrics dir that resolves to a
    # sensitive path (~/.aws, ~/.ssh, ...). Mirrors skills.py's use of
    # validate_file_path (resolves symlinks + is_sensitive_path check).
    if validate_file_path(str(directory)) is None:
        logger.warning("telemetry metrics dir failed sensitive-path check; skipping read")
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    out: list[Path] = []
    for p in directory.glob("metrics-*.jsonl"):
        # Defensive: skip any shard that resolves to a sensitive path (symlink).
        if validate_file_path(str(p)) is None:
            continue
        # filename: metrics-YYYY-MM-DD-<pid>.jsonl
        stem = p.stem  # metrics-YYYY-MM-DD-<pid>
        parts = stem.split("-")
        if len(parts) < 4:
            continue
        try:
            d = datetime.strptime("-".join(parts[1:4]), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= cutoff:
            out.append(p)
    return out


def _pct_from_buckets(
    bucket_counts: list[int], bounds: list[float], q: float
) -> float:
    """Interpolate the q-quantile (0..1) from explicit histogram buckets.

    ``bucket_counts`` has one more element than ``bounds`` (the trailing +Inf
    overflow bucket). Linear-interpolates within the bucket that crosses the
    target rank; the overflow bucket can only report its lower bound.
    """
    total = sum(bucket_counts)
    if total <= 0:
        return 0.0
    target = q * total
    cum = 0.0
    for i, c in enumerate(bucket_counts):
        if c <= 0:
            continue
        prev = cum
        cum += c
        if cum >= target:
            lo = bounds[i - 1] if i > 0 else 0.0
            if i >= len(bounds):  # +Inf overflow bucket — no upper bound
                return float(lo)
            hi = bounds[i]
            frac = (target - prev) / c if c > 0 else 0.0
            return float(lo + (hi - lo) * frac)
    return float(bounds[-1]) if bounds else 0.0


class _Hist:
    """Accumulator merging histogram data points that share a dimension key.

    Data points are grouped by their EXACT ``explicit_bounds``, and every
    reported statistic comes from a single group. This matters whenever bucket
    boundaries change: a data point's bounds are baked in at record time, so a
    14-day scan window straddling a boundary change holds two incompatible
    generations of the same metric.

    Merging them positionally fabricates values. Two generations with the same
    bucket-count length would pass a naive length check while meaning entirely
    different things — a pre-change sample sitting in the old ``+Inf`` bucket
    would be added to the new ``+Inf`` bucket, and a 5s sample could be counted
    into a 5-minute bucket, letting ``_pct_from_buckets`` report a p90 that no
    turn ever took. Grouping also keeps ``count``/``sum``/``min``/``max``
    consistent with the percentiles: accumulating those across generations while
    only one generation's buckets survive would describe a mean over one
    population and percentiles over another.

    The reported group is the one holding the **newest** data point, not the
    largest. Majority selection would let a stale generation keep winning for as
    long as it out-counted the new one: right after a boundary change the window
    still holds up to ``_WINDOW_DAYS`` of old samples against a handful of new
    ones, so the OLD bounds would be reported — for the turn metric that means
    continuing to serve the very ceiling-pinned percentiles this grouping exists
    to eliminate, while omitting the new samples entirely. Recency makes the
    change take effect on the first post-change sample. The reported population
    is then small but truthful, and ``count`` says so; fuller-but-wrong is the
    failure mode being fixed.

    ``other_generations`` exposes how many groups were seen beyond the reported
    one so a caller can surface a mixed window rather than silently trusting a
    subset.
    """

    __slots__ = ("_groups",)

    def __init__(self) -> None:
        # bounds signature -> accumulated stats for that boundary generation
        self._groups: dict[tuple[float, ...], dict[str, Any]] = {}

    def add(self, dp: dict[str, Any], outcome: str = "") -> None:
        bc = dp.get("bucket_counts") or []
        try:
            key = tuple(float(b) for b in (dp.get("explicit_bounds") or []))
        except (TypeError, ValueError):
            return
        g = self._groups.get(key)
        if g is None:
            g = {
                "count": 0,
                "sum": 0.0,
                "min": None,
                "max": None,
                "buckets": [0] * len(bc) if bc else [],
                "bounds": list(key),
                "outcomes": {},
                "newest_ns": 0,
            }
            self._groups[key] = g
        try:
            ns = int(dp.get("time_unix_nano") or 0)
        except (TypeError, ValueError):
            ns = 0
        if ns > int(g["newest_ns"]):
            g["newest_ns"] = ns
        n = int(dp.get("count", 0) or 0)
        g["count"] += n
        g["sum"] += float(dp.get("sum", 0.0) or 0.0)
        if outcome:
            # Outcome tallies MUST be grouped too. Scoping only the buckets and
            # count would leave the outcome breakdown summing across generations
            # while count reported one — the dashboard would show N turns beside
            # an outcome bar totalling more than N, and a fault rate computed
            # over a different population than the latency next to it.
            g["outcomes"][outcome] = g["outcomes"].get(outcome, 0) + n
        mn, mx = dp.get("min"), dp.get("max")
        if mn is not None:
            g["min"] = mn if g["min"] is None else min(g["min"], mn)
        if mx is not None:
            g["max"] = mx if g["max"] is None else max(g["max"], mx)
        if bc:
            if not g["buckets"]:
                g["buckets"] = [0] * len(bc)
            # Same bounds signature implies same bucket length; the guard only
            # defends against a malformed shard mixing lengths under one bounds
            # list, which would otherwise raise IndexError.
            if len(bc) == len(g["buckets"]):
                for j, v in enumerate(bc):
                    g["buckets"][j] += int(v or 0)

    def _dominant(self) -> dict[str, Any] | None:
        """The generation holding the newest sample.

        ``count`` is only a tie-break, reached when data points carry no
        ``time_unix_nano`` (synthetic or older shards) so every group ties at 0.
        """
        if not self._groups:
            return None
        return max(
            self._groups.values(),
            key=lambda g: (int(g["newest_ns"]), int(g["count"])),
        )

    @property
    def count(self) -> int:
        g = self._dominant()
        return int(g["count"]) if g else 0

    @property
    def buckets(self) -> list[int]:
        g = self._dominant()
        return list(g["buckets"]) if g else []

    @property
    def bounds(self) -> list[float]:
        g = self._dominant()
        return list(g["bounds"]) if g else []

    @property
    def other_generations(self) -> int:
        """Boundary generations present beyond the reported one (0 = clean)."""
        return max(0, len(self._groups) - 1)

    @property
    def total_count(self) -> int:
        """Samples across EVERY generation, not just the reported one.

        ``count`` is deliberately scoped to one boundary generation, so on a
        mixed window it under-reports. Pairing the two lets a caller say
        "showing 141 of 1970" instead of publishing 141 as if it were the whole
        population — which is what made a histogram card contradict a counter
        for the same event with nothing explaining the gap.
        """
        return sum(int(g["count"]) for g in self._groups.values())

    @property
    def outcomes(self) -> dict[str, int]:
        """Outcome tallies for the reported generation only.

        Consistent by construction with ``count`` and the percentiles, so a
        fault rate derived from this describes the same population as the
        latency shown beside it.
        """
        g = self._dominant()
        return dict(g["outcomes"]) if g else {}

    def stats(self) -> dict[str, Any]:
        """Reported-generation stats, WITH the mixed-window disclosure.

        ``other_generations`` / ``total_count`` are part of this payload on
        purpose rather than something each caller adds by hand: emitting them
        here guarantees every histogram surface discloses a mixed window instead
        of publishing a one-generation subset as if it were the whole
        population (a subset can drop the large majority of samples, and makes a
        histogram card contradict the counter for the same event).
        """
        g = self._dominant()
        if g is None:
            return {
                "count": 0, "mean_ms": 0.0, "p50_ms": 0.0,
                "p90_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0,
                "other_generations": 0, "total_count": 0,
            }
        cnt = int(g["count"])
        return {
            "count": cnt,
            "mean_ms": round(float(g["sum"]) / cnt, 1) if cnt else 0.0,
            "p50_ms": round(_pct_from_buckets(g["buckets"], g["bounds"], 0.50), 1),
            "p90_ms": round(_pct_from_buckets(g["buckets"], g["bounds"], 0.90), 1),
            "min_ms": round(g["min"], 1) if g["min"] is not None else 0.0,
            "max_ms": round(g["max"], 1) if g["max"] is not None else 0.0,
            # >0 means the window straddles a bucket-boundary change and only the
            # dominant generation is reported; total_count is the full population.
            "other_generations": self.other_generations,
            "total_count": self.total_count,
        }


def _day_of(dp: dict[str, Any], fallback: str) -> str:
    ns = dp.get("time_unix_nano")
    if ns:
        try:
            return (
                datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d")
            )
        except (ValueError, OverflowError, OSError):
            pass
    return fallback


def _aggregate(shard_paths: list[Path]) -> dict[str, Any]:
    overall = _Hist()
    cold = _Hist()  # spawned == True
    warm = _Hist()  # spawned == False
    daily: dict[str, dict[str, _Hist]] = {}  # day -> {"cold"|"warm": _Hist}
    phases: dict[str, _Hist] = {}  # startup internal phase -> _Hist
    by_channel: dict[str, _Hist] = {}  # conversation source -> _Hist
    # generic surface for every other kirocrew.* metric
    other_hist: dict[str, _Hist] = {}
    # name -> "attr=value" -> _Hist, for _OTHER_SPLIT_ATTRS only
    other_split: dict[str, dict[str, _Hist]] = {}
    other_ctr: dict[str, dict[str, Any]] = {}  # name -> {total, by_attr}
    turn = _Hist()

    for p in shard_paths:
        shard_day = "-".join(p.stem.split("-")[1:4])
        try:
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    for rm in obj.get("resource_metrics", []) or []:
                        for sm in rm.get("scope_metrics", []) or []:
                            for m in sm.get("metrics", []) or []:
                                name = m.get("name") or ""
                                if not name.startswith("kirocrew."):
                                    continue
                                data = m.get("data") or {}
                                for dp in data.get("data_points", []) or []:
                                    attrs = dp.get("attributes") or {}
                                    is_hist = "bucket_counts" in dp
                                    if name == _STARTUP_METRIC and is_hist:
                                        # One startup emits an end-to-end point
                                        # (phase absent, or phase=total from the
                                        # kiro path) PLUS one point per internal
                                        # phase. Only the end-to-end point is a
                                        # startup: counting the phase points too
                                        # would multiply the startup count by ~4
                                        # and sum four unrelated latency
                                        # distributions into one set of buckets,
                                        # a bimodal "distribution" that is really
                                        # set_model + session_new + spawn_init +
                                        # total stacked together.
                                        phase = str(attrs.get("phase", _PHASE_TOTAL))
                                        if phase != _PHASE_TOTAL:
                                            phases.setdefault(phase, _Hist()).add(dp)
                                            continue
                                        spawned = bool(attrs.get("spawned"))
                                        oc = str(attrs.get("outcome", "unknown"))
                                        (cold if spawned else warm).add(dp)
                                        # Which conversation source paid this
                                        # startup. Older shards predate the
                                        # attribute, so they aggregate under
                                        # "unknown" rather than being dropped.
                                        by_channel.setdefault(
                                            str(attrs.get("channel", "unknown")), _Hist()
                                        ).add(dp)
                                        # Outcomes go through _Hist so they are
                                        # scoped to the same bounds generation as
                                        # the count and percentiles reported.
                                        overall.add(dp, outcome=oc)
                                        day = _day_of(dp, shard_day)
                                        db = daily.setdefault(
                                            day, {"cold": _Hist(), "warm": _Hist()}
                                        )
                                        db["cold" if spawned else "warm"].add(dp)
                                    elif name == _TURN_METRIC and is_hist:
                                        turn.add(
                                            dp,
                                            outcome=str(
                                                attrs.get("outcome", "unknown")
                                            ),
                                        )
                                    elif is_hist:
                                        other_hist.setdefault(name, _Hist()).add(dp)
                                        for ak in _OTHER_SPLIT_ATTRS:
                                            if ak not in attrs:
                                                continue
                                            sig = f"{ak}={str(attrs[ak]).lower()}"
                                            other_split.setdefault(
                                                name, {}
                                            ).setdefault(sig, _Hist()).add(dp)
                                    elif "value" in dp:
                                        rec = other_ctr.setdefault(
                                            name, {"total": 0.0, "by_attr": {}}
                                        )
                                        val = float(dp.get("value", 0.0) or 0.0)
                                        rec["total"] += val
                                        if attrs:
                                            key = ",".join(
                                                f"{k}={attrs[k]}"
                                                for k in sorted(attrs)
                                            )
                                            rec["by_attr"][key] = (
                                                rec["by_attr"].get(key, 0.0) + val
                                            )
        except (OSError, UnicodeDecodeError):
            continue

    daily_out = []
    for day in sorted(daily):
        c, w = daily[day]["cold"], daily[day]["warm"]
        daily_out.append(
            {
                "date": day,
                "count": c.count + w.count,
                "cold_p50_ms": round(_pct_from_buckets(c.buckets, c.bounds, 0.50), 1),
                "cold_p90_ms": round(_pct_from_buckets(c.buckets, c.bounds, 0.90), 1),
                "warm_p50_ms": round(_pct_from_buckets(w.buckets, w.bounds, 0.50), 1),
            }
        )

    other = []
    for name in sorted(other_hist):
        s = other_hist[name].stats()
        s.update({"name": name, "kind": "histogram"})
        splits = other_split.get(name)
        if splits:
            s["splits"] = {sig: splits[sig].stats() for sig in sorted(splits)}
        other.append(s)
    for name in sorted(other_ctr):
        rec = other_ctr[name]
        other.append(
            {
                "name": name,
                "kind": "counter",
                "total": round(rec["total"], 3),
                "by_attr": {k: round(v, 3) for k, v in rec["by_attr"].items()},
            }
        )

    turn_outcome = turn.outcomes
    turn_total = sum(turn_outcome.values())
    turn_faults = sum(v for k, v in turn_outcome.items() if k != "ok")
    turn_block = {
        # ``other_generations`` arrives via stats(): >0 means the window
        # straddles a bucket-boundary change and only the dominant generation
        # is reported (see _Hist).
        **turn.stats(),
        "outcome": turn_outcome,
        "fault_rate": round(turn_faults / turn_total, 4) if turn_total else 0.0,
    }

    return {
        "startup": {
            "overall": overall.stats(),
            "cold": cold.stats(),
            "warm": warm.stats(),
            "outcome": overall.outcomes,
            "daily": daily_out,
            "distribution": {"buckets": overall.buckets, "bounds": overall.bounds},
            # Internal phase split (kiro backend): spawn_init, session_new,
            # set_model. Deliberately outside the startup totals above — these
            # are components of one startup, not startups.
            "phases": [
                {"name": n, **phases[n].stats()} for n in sorted(phases)
            ],
            # Startup cost grouped by conversation source, so a slow surface can
            # be identified directly instead of being inferred by correlating
            # export windows against the gateway log.
            "by_channel": [
                {"name": n, **by_channel[n].stats()} for n in sorted(by_channel)
            ],
        },
        "turn": turn_block,
        "other": other,
    }


def _parse_startup_metrics() -> dict[str, Any]:
    """Windowed + fingerprint-cached aggregation over the metric shards."""
    global _CACHE, _CACHE_KEY, _CACHE_TS
    _enabled, directory = _telemetry_cfg()
    shards = _shards_in_window(directory, _WINDOW_DAYS)
    if not shards:
        _CACHE, _CACHE_KEY = None, None
        return {"startup": None, "turn": None, "other": [], "shard_count": 0}

    try:
        key = tuple(
            sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in shards)
        )
    except OSError:
        key = None
    now = time.time()
    if (
        key is not None
        and _CACHE_KEY == key
        and _CACHE is not None
        and (now - _CACHE_TS) < _CACHE_TTL
    ):
        return _CACHE

    result = _aggregate(shards)
    result["shard_count"] = len(shards)
    if key is not None:
        _CACHE, _CACHE_KEY, _CACHE_TS = result, key, now
    return result


def _context_block() -> dict[str, Any] | None:
    """Per-turn context-window occupancy, or None when nothing is recorded.

    Best-effort: this panel must still render its OTEL sections if the token row
    store is unreadable.
    """
    try:
        block = context_occupancy(_WINDOW_DAYS)
    except Exception:
        logger.debug("context occupancy aggregation failed", exc_info=True)
        return None
    return block if block.get("turns") else None


def _cost_block() -> dict[str, Any] | None:
    """Per-turn spend attribution, or None when nothing is recorded.

    Best-effort for the same reason as :func:`_context_block`: an unreadable row
    store must not take the OTEL sections down with it.
    """
    try:
        block = cost_breakdown(_COST_WINDOW_DAYS)
    except Exception:
        logger.debug("cost breakdown aggregation failed", exc_info=True)
        return None
    return block if block.get("turns") else None


async def api_telemetry_startup(request: web.Request) -> web.Response:
    """GET /api/telemetry/startup — session-startup latency + all kirocrew.* metrics.

    Returns ``enabled`` (telemetry main switch), ``window_days``, ``shard_count``,
    a detailed ``startup`` block (overall/cold/warm p50/p90 + outcome + daily +
    internal phase split), a ``context`` block (per-turn context-window
    occupancy), a ``cost`` block (spend attribution), and a generic ``other``
    list surfacing every other emitted kirocrew.* metric.

    ``context`` and ``cost`` are sourced from the per-turn token row store, NOT
    from the OTEL shards: occupancy is a per-session ratio and slot keys are
    unbounded-cardinality, which is exactly what must not become a metric label.
    They are reported here anyway because "how full is the window" and "what did
    it cost" belong next to the other per-turn health signals rather than on a
    separate page. Both are independent of the telemetry main switch — those rows
    are always written — so they are fetched even when OTEL export is off.
    """
    enabled, directory = _telemetry_cfg()
    data = await asyncio.to_thread(_parse_startup_metrics)
    context = await asyncio.to_thread(_context_block)
    cost = await asyncio.to_thread(_cost_block)
    if cost:
        cost = await _with_conversation_titles(request, cost)
    return web.json_response(
        {
            "enabled": enabled,
            "window_days": _WINDOW_DAYS,
            "metrics_dir": str(directory),
            "shard_count": data.get("shard_count", 0),
            "startup": data.get("startup"),
            "turn": data.get("turn"),
            "context": context,
            "cost": cost,
            "other": data.get("other", []),
        }
    )


async def api_context_trace(request: web.Request) -> web.Response:
    """GET /api/telemetry/context-trace?slot=<session key> — per-turn injection.

    Returns what KiroCrew added to each turn of one session, block by block, so
    the user can audit their own context rather than reverse-engineering it. The
    aggregate (bounded, block-keyed) half of the same data belongs on a metric;
    this per-session, per-turn half deliberately does not — see
    :func:`kiro_crew.dashboard.handlers.usage.context_trace`.

    Independent of the telemetry main switch: the usage rows this reads are
    always written, so the trace works with OTEL collection off.
    """
    slot = (request.query.get("slot") or "").strip()
    if not slot:
        return web.json_response(
            {"error": "slot is required", "code": "slot_required"}, status=400
        )
    trace = await asyncio.to_thread(context_trace, slot, _WINDOW_DAYS)
    return web.json_response(trace)


def _persisted_titles(conversation_log: Any, slot_keys: list[str]) -> dict[str, str]:
    """Read the persisted title for each of *slot_keys*. Blocking; call off-loop.

    ``get_metadata`` rather than ``list_sessions``: the latter falls back to the
    first user message and then to the session key when the metadata line names
    no title, which would turn a ranking label into prompt text and leave no way
    to tell a named conversation from an unnamed one. Only an explicit
    ``metadata["title"]`` counts here, which is the same thing the live slot
    carries.

    Keyed by SLOT key on the way out, so the caller never has to know how a slot
    maps onto a transcript. Distinct slots can share one transcript (a
    channel-born slot's conversation IS the channel's), so the read is
    deduplicated by transcript key rather than by slot.
    """
    by_transcript: dict[str, str] = {}
    out: dict[str, str] = {}
    for slot_key in slot_keys:
        try:
            transcript_key = slot_transcript_key(slot_key)
        except Exception:  # pragma: no cover — a key shape no rule recognises
            continue
        if transcript_key not in by_transcript:
            try:
                meta = conversation_log.get_metadata(transcript_key) or {}
            except Exception:
                logger.debug("no persisted title for %s", transcript_key, exc_info=True)
                meta = {}
            by_transcript[transcript_key] = str(meta.get("title") or "")
        title = by_transcript[transcript_key]
        if title and title != NEW_SESSION_TITLE:
            out[slot_key] = title
    return out


async def _with_conversation_titles(request: web.Request, cost: dict[str, Any]) -> dict[str, Any]:
    """Attach a redacted human title to each ranked conversation, where known.

    A title is resolved from the live slot first, so a rename is reflected before
    it has been persisted. A conversation the user has since closed has no slot,
    and its title is read back from the transcript's metadata line instead —
    without that fallback the longer the window, the more of the ranking renders
    unnamed, which is backwards for the question the window exists to answer.

    A row with neither still reports an absent title rather than the raw key,
    leaving the frontend to decide how to render an unnamed row.

    ``display_title`` is LLM-authored (``chat_title._generate_title_via_kiro``),
    so it carries the same two scanners the slot's own serialization applies at
    ``_ChatSlot.to_dict``. This endpoint is a SECOND serialization boundary for
    that field, and the scan is load-bearing rather than duplicated: a title set
    through ``api_chat_slot_resume`` is written to the slot unredacted, so
    nothing upstream of here has sanitised it. A persisted title takes the same
    path, so where it came from cannot change what leaves here.

    The metadata reads are the only blocking work, and they run in a thread: the
    surrounding handler already offloads its three other blocks, and this one is
    bounded by the ranked rows (``_COST_TOP_CONVOS``) rather than by the number
    of sessions on disk.

    Rows are copied before the title is attached. ``cost_breakdown`` hands back
    its memoised object by reference, so writing into the row would store the
    title in module-global cache and keep serving it for the rest of the TTL
    after the conversation was renamed or closed.
    """
    try:
        state = request.app["state"]
    except KeyError:
        return cost
    # ``get_slot`` is the only public way in: the slot map itself is private
    # (``DashboardState._slots``), so reaching for a ``slots`` attribute silently
    # resolves nothing and every row renders unnamed.
    get_slot = getattr(state, "get_slot", None)
    if not callable(get_slot):
        return cost

    conversations = cost.get("conversations") or []
    titles: dict[str, str] = {}
    unresolved: list[str] = []
    for row in conversations:
        slot_key = str(row.get("slot") or "")
        if not slot_key:
            continue
        slot = get_slot(slot_key)
        title = getattr(slot, "display_title", "") if slot is not None else ""
        if title and title != NEW_SESSION_TITLE:
            titles[slot_key] = str(title)
        elif slot_key not in titles:
            unresolved.append(slot_key)

    conversation_log = getattr(state, "conversation_log", None)
    if unresolved and conversation_log is not None:
        titles.update(
            await asyncio.to_thread(_persisted_titles, conversation_log, unresolved)
        )

    rows = []
    for row in conversations:
        title = titles.get(str(row.get("slot") or ""))
        if title:
            safe, _ = redact_exfiltration_urls(title)
            safe, _ = redact_credentials(safe)
            row = {**row, "title": safe}
        rows.append(row)
    return {**cost, "conversations": rows}


def _beacon_overlay_pins_value() -> bool:
    """Return whether ``config.local.json`` sets ``telemetry.beacon_enabled``.

    That overlay deep-merges OVER ``config.json`` at load, and the Settings
    toggle writes the BASE file — so an entry here makes the switch snap back to
    the overlay's value after a successful write. Reporting it lets the panel say
    why instead of looking broken. Best-effort: an unreadable or malformed
    overlay is reported as "not pinned" rather than raising, since this is a
    diagnostic (the effective value in ``enabled`` is still authoritative).
    """
    from kiro_crew.config.loader import config_local_path

    try:
        path = config_local_path()
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    section = data.get("telemetry") if isinstance(data, dict) else None
    return isinstance(section, dict) and "beacon_enabled" in section


async def api_beacon_status(request: web.Request) -> web.Response:
    """GET /api/telemetry/beacon — anonymous-heartbeat state for Settings → Privacy.

    Powers the in-product opt-out toggle. ``enabled`` is the stored
    ``telemetry.beacon_enabled`` (what the toggle writes); ``would_send`` /
    ``reason`` are the EFFECTIVE verdict, which can differ because
    ``KIROCREW_TELEMETRY_DISABLED``, a CI host, a non-default data home, or a
    ``config.local.json`` overlay all suppress sending regardless of this flag.
    Surfacing both is the point: a toggle that reads back "on" while an env var
    silences the beacon (or vice versa) would be a false promise on a privacy
    control, so the UI can say which one is actually in force.

    ``env_override`` reports specifically whether the env var is what pins the
    state, so the panel can disable the toggle instead of offering a write that
    cannot take effect. ``overlay_override`` does the same for a
    ``config.local.json`` entry, which deep-merges OVER ``config.json`` at load —
    the toggle writes the base file, so an overlay entry would otherwise let the
    switch snap back with no explanation (the CLI reports this same case; see
    ``cli_commands._telemetry``). ``governance_override`` reports the third and
    strongest case: an enterprise ceiling pinning ``capabilities.telemetry`` off,
    where the PATCH route itself returns 403 — so the panel must disable the
    control AND say who pinned it, since this is the one the user cannot lift.
    Read-only, and never materializes an install id (``beacon.status`` uses
    ``create=False``).
    """
    overlay_override = False
    try:
        # to_thread, not a bare load(): KiroCrewConfig.load() stats and reads
        # config.json (+ any config.local.json overlay), and this handler runs on
        # the aiohttp event loop — a synchronous read here stalls every other
        # request behind it. The rest of this module already routes its file work
        # through to_thread for the same reason.
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        enabled = cfg.telemetry.beacon_enabled
        endpoint = cfg.telemetry.beacon_endpoint
        acked = cfg.dashboard.privacy_acked
        overlay_override = await asyncio.to_thread(_beacon_overlay_pins_value)
    except Exception:
        # A diagnostic must never 500: an unreadable config is exactly when the
        # user wants to see this panel. Fail toward "off" so the UI never claims
        # telemetry is on when we cannot prove it.
        logger.debug("beacon config load failed; reporting disabled", exc_info=True)
        enabled, endpoint, acked = False, "", False

    info = await asyncio.to_thread(
        beacon.status,
        endpoint,
        enabled=enabled,
        app_version=__version__,
        acked=acked,
    )
    return web.json_response(
        {
            "enabled": bool(info.get("beacon_enabled", enabled)),
            "would_send": bool(info.get("would_send", False)),
            "reason": str(info.get("reason", "")),
            # The stable discriminant the panel translates. `reason` stays as
            # untranslated operator detail for logs and bug reports; the UI must
            # render this instead, never the prose.
            "reason_code": str(info.get("reason_code", "")),
            "endpoint_configured": bool(info.get("endpoint_configured", False)),
            "env_override": beacon.is_env_opted_out(),
            "env_var": beacon.DISABLE_ENV,
            "overlay_override": overlay_override,
            # Resolved inside beacon.status (already on a worker thread) rather
            # than re-evaluated here, so this reports the same verdict that
            # should_send and the PATCH gate act on.
            "governance_override": bool(info.get("governance_pinned_off", False)),
        }
    )
