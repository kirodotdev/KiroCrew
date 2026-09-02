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
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from aiohttp import web

from kiro_crew import __version__, beacon
from kiro_crew import sel as _sel_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.handlers.usage import (
    SPEND_WINDOW_DAYS,
    context_occupancy,
    context_trace,
    cost_breakdown,
    slot_turn_usage,
)
from kiro_crew.dashboard.state import NEW_SESSION_TITLE
from kiro_crew.hooks import validate_file_path
from kiro_crew.jsonl_util import bounded_records
from kiro_crew.metrics.provider import TELEMETRY_ENV_VAR, env_pin, otlp_egress_active
from kiro_crew.metrics.schema import RESOURCE_ATTR_PROCESS_START_TIME
from kiro_crew.metrics.turns import TURN_COST_METRIC, TURN_CREDITS_METRIC, TURN_METRIC
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_STARTUP_METRIC = "kirocrew.session.startup.duration"
# Read from the emitter's own constant rather than re-spelled: a reader and an
# emitter naming the instrument differently is a silently empty panel.
_TURN_METRIC = TURN_METRIC
# The turn's two billing histograms. Claimed BY NAME below, ahead of the generic
# histogram branch, because that branch reports every statistic under `*_ms`
# keys: a credit or a dollar amount arriving there would be rendered as a
# millisecond duration on the Telemetry page. They are reported inside the turn
# block under unit-neutral keys instead, each carrying its own `unit`.
_TURN_CREDITS_METRIC = TURN_CREDITS_METRIC
_TURN_COST_METRIC = TURN_COST_METRIC
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

# Gauge names whose reading is a monotonic PROCESS-LIFETIME total, not the
# current state of anything: the process CPU/GC instruments plus the inventory
# probe-failure count. Each instrument module declares its own
# ``LIFETIME_TOTAL_METRICS`` -- the module that registers an instrument is the one
# that knows what its reading means -- and this is where the two are merged, so
# neither module has to know about the other and no name is re-listed here.
#
# These are reduced through the CUMULATIVE reducer instead of the gauge one.
# Two reasons, and both matter:
#
#   * The gauge reducer reports the newest sample, which for a lifetime total is
#     "CPU seconds since this process started" — a number that only grows and
#     answers nothing about the window on screen. The cumulative reducer already
#     computes exactly the right thing (newest minus first-in-window, per
#     process-identity stream), so this is a routing decision, not a new reducer.
#   * They WERE observable counters, so shards written before that changed carry
#     them as CUMULATIVE sums. Routing both shapes into the same per-stream
#     structure is what keeps one continuous series across the switch instead of
#     splitting the window into a counter row and a gauge row.
#
# Resolved on FIRST USE, not at import. This module is imported while the gateway
# is still assembling its routes, and importing the two instrument modules there
# costs a measured 22ms and 14 extra modules (including sqlite3, via
# platform_compat's dependency chain) for a telemetry subsystem that is off by
# default — work on the boot path before the socket is even bound. The names are
# needed only inside :func:`_aggregate`, which runs per request, so the import
# happens there and the merged set is memoized for the life of the process.
_lifetime_total_gauges: "frozenset[str] | None" = None


def _lifetime_total_gauge_names() -> "frozenset[str]":
    """The merged lifetime-total roster, imported on first use and cached."""
    global _lifetime_total_gauges
    if _lifetime_total_gauges is None:
        from kiro_crew.metrics.inventory_gauges import LIFETIME_TOTAL_METRICS as _inventory
        from kiro_crew.metrics.process_gauges import LIFETIME_TOTAL_METRICS as _process

        _lifetime_total_gauges = frozenset(_process + _inventory)
    return _lifetime_total_gauges


# Only terminal-fault outcomes count toward fault_rate. The two watchdog
# recovery outcomes ("tool_stall" and "stale_recover") are NOT faults: a
# recovered stall is re-driven in place and tracked separately under
# kirocrew.watchdog.recovery.outcome. Counting them as faults would inflate the
# fault rate and hide the true error population. Use an explicit allowlist so
# future outcome labels added to _turn_outcome() must actively opt in — a
# cross-module test (test_telemetry_handler) fails on any label that is
# neither here nor explicitly excluded, so drift can't silently deflate
# fault_rate.
# "unknown" is included: it covers metric shards written before the explicit
# outcome labels were introduced. Excluding it would silently move pre-change
# fault counts into the denominator without increasing the numerator, biasing
# fault_rate downward on the 14-day lookback window.
# "stall_exhausted" is included: a stall turn arriving with its recovery
# budget already spent dies with "start a new chat" — the emit site labels
# it distinctly so the recovered-stall exclusion cannot hide dead sessions,
# and fault_rate stays a single-series computation.
# Every entry here must have a producer: either a turn_outcome return label
# or "unknown" (minted by this aggregator for attribute-less points) — the
# cross-module test enforces that, so a dead entry cannot linger and mislead
# readers about what fault_rate counts.
# "cancelled" is deliberately ABSENT, and its absence is a FIX rather than an
# omission: a user cancel used to fold into "error", so every press of Stop
# landed in this numerator and the one outcome the operator caused on purpose
# was reported as the system failing. It now has its own label and stays out of
# the numerator, while remaining in the DENOMINATOR alongside "ok" and the
# recovered stalls — a cancelled turn did run, so removing it would shrink the
# population fault_rate is a share of.
# "unclassified" is deliberately ABSENT: it marks a turn whose surface had no
# stop reason to give (a bare TurnUsage at a helper call site), so counting it
# would invent a fault for every clean background turn the moment this metric
# started sampling them. It is not folded into "ok" either — it stays its own
# slice in the outcome breakdown so the blind spot is visible rather than
# resolved by a guess in either direction.
_TERMINAL_FAULT_OUTCOMES = frozenset({"error", "timeout", "unknown", "stall_exhausted"})

# (shard-fingerprint, TTL) cache — shards are append-only, so a change to any
# shard's (mtime, size) invalidates the cache exactly when needed (same pattern
# as usage._parse_token_history).
_CACHE: dict[str, Any] | None = None
_CACHE_KEY: tuple[tuple[str, float, int], ...] | None = None
_CACHE_TS: float = 0.0
_CACHE_TTL = 30.0


class _TelemetryState(NamedTuple):
    """Effective telemetry posture for the panel."""

    enabled: bool
    directory: Path
    env_pinned: bool
    env_var: str
    otlp_configured: bool


def _telemetry_cfg() -> _TelemetryState:
    """Resolve the telemetry posture the same way the exporter does.

    ``enabled`` is the EFFECTIVE state, not the stored flag: ``KIROCREW_TELEMETRY``
    overrides ``telemetry.enabled`` in the collector, so reporting the config value
    alone would tell a pinned host "off" while metrics were being written (or "on"
    while nothing was). ``env_pinned`` says the choice is not the config file's to
    make, which is what lets the panel's switch disable itself instead of offering
    a write that cannot take effect.

    The pin comes from ``metrics.provider`` rather than a second read of the env
    var here: two resolutions are two things to keep in sync, and a control that
    disagrees with the collector about what "on" means is worse than no control.
    """
    enabled = False
    env_pinned = False
    otlp_configured = False
    directory = config_dir() / "metrics"
    try:
        cfg = KiroCrewConfig.load().telemetry
        enabled = bool(cfg.enabled)
        if getattr(cfg, "local_dir", None):
            directory = Path(cfg.local_dir).expanduser()
        # Presence only, and resolved the same way _build_recorder resolves it:
        # from the active telemetry provider's destination set, NOT from the
        # endpoint string. An edition that supplies its own collector must not be
        # able to leave this panel reporting "nothing is exported" while metrics
        # leave the machine. The endpoint value never leaves that resolution; the
        # panel only needs to know whether egress would happen.
        try:
            otlp_configured = otlp_egress_active(cfg)
        except Exception:
            # Posture unresolvable (a provider that raised, an uncomposable
            # context). Report egress rather than promising local-only: this
            # answer is a DISCLOSURE, so its closed direction is "assume it
            # exports". The panel then disables the enable direction instead of
            # offering a write the config route refuses with 409 anyway.
            logger.debug("OTLP egress posture unresolvable; reporting egress", exc_info=True)
            otlp_configured = True
    except Exception:
        logger.debug("telemetry config load failed; assuming disabled", exc_info=True)
    env_var = TELEMETRY_ENV_VAR
    try:
        pin = env_pin()
    except Exception:
        logger.debug("telemetry env pin resolution failed", exc_info=True)
        pin = None
    if pin is not None:
        enabled = pin
        env_pinned = True
    return _TelemetryState(enabled, directory, env_pinned, env_var, otlp_configured)


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


def _pct_from_buckets(bucket_counts: list[int], bounds: list[float], q: float) -> float:
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
        # INVARIANT: the WHOLE data point is validated before the FIRST group
        # mutation, so a rejected point never half-lands and nothing invalid
        # can enter durable group state. Shards are external input and
        # Python's json accepts Infinity/NaN literals plus arbitrary-precision
        # integers, so every read routes through the _finite/_finite_int
        # chokepoints with the scalar branch's contract: value-poisoned
        # fields (bounds, count, sum, bucket counts) skip the whole point;
        # optional stats (min/max) degrade per-stat; a bucket list whose
        # length disagrees with its bounds degrades to no-buckets (the point
        # still counts); a garbage timestamp sorts oldest. Validation covers three
        # classes: VALUE (finite, exact -- ints never roundtrip through
        # float), STRUCTURE (containers are lists; a bucket list only merges
        # when it has exactly len(bounds)+1 entries, so group buckets always
        # match their bounds signature), and ACCUMULATION (the prospective sum must
        # stay finite -- two individually finite 1e308 sums must not emit an
        # Infinity literal downstream).
        bounds_raw = dp.get("explicit_bounds")
        bc_raw = dp.get("bucket_counts")
        if bounds_raw is None:
            bounds_raw = []
        if bc_raw is None:
            bc_raw = []
        if not isinstance(bounds_raw, (list, tuple)) or not isinstance(bc_raw, (list, tuple)):
            # Any non-list container is garbage and skips the point: a truthy
            # one (e.g. "explicit_bounds": 5) would raise TypeError at the
            # for-loop, and a falsy one (false, 0, "") must not silently read
            # as "absent" and corrupt the group's shape. Only a genuinely
            # missing/null key defaults to empty.
            return
        bounds_f: list[float] = []
        for b in bounds_raw:
            fb = _finite(b)
            if fb is None:
                return
            if bounds_f and not math.isfinite(fb - bounds_f[-1]):
                # Derived values must stay finite too: two individually
                # finite bounds like -1e308 and 1e308 subtract to inf inside
                # _pct_from_buckets' interpolation (hi - lo) and the API
                # would emit an Infinity literal. Same class as the
                # accumulated-sum guard below.
                return
            bounds_f.append(fb)
        key = tuple(bounds_f)
        n_raw = dp.get("count", 0)
        n = _finite_int(0 if n_raw is None else n_raw)
        if n is None:
            return
        # Uniform defaulting rule for every field in this method: ONLY a
        # missing or null key takes the default; any other value must survive
        # validation on its own ("" or false substituting 0 would let a
        # malformed point silently skew the mean).
        sum_raw = dp.get("sum", 0.0)
        fsum = _finite(0.0 if sum_raw is None else sum_raw)
        if fsum is None:
            return
        bc_f: list[int] = []
        for v in bc_raw:
            fv = _finite_int(v)
            if fv is None:
                return
            bc_f.append(fv)
        # A histogram point's bucket_counts has one more entry than its
        # bounds (the trailing +Inf bucket). A mismatched length cannot merge
        # into this bounds generation's shape (it would poison the group's
        # buckets and crash the percentile interpolation with IndexError),
        # but it is a merge-compatibility problem, not value poisoning: the
        # point's independently-validated scalars are still truthful, so the
        # disagreeing bucket list is DROPPED and the point still counts --
        # the same degrade path as a count-only point (no bucket_counts at
        # all), which stays legal. Pinned upstream by
        # test_telemetry_handlers_cov80.py (count keeps accumulating).
        if bc_f and len(bc_f) != len(bounds_f) + 1:
            bc_f = []
        g = self._groups.get(key)
        # Prospective-accumulation check BEFORE mutation: adding a finite sum
        # to a finite accumulator can still overflow to inf.
        acc_sum = (float(g["sum"]) if g is not None else 0.0) + fsum
        if not math.isfinite(acc_sum):
            return
        if g is None:
            g = {
                "count": 0,
                "sum": 0.0,
                "min": None,
                "max": None,
                "buckets": [0] * len(bc_f) if bc_f else [],
                "bounds": list(key),
                "outcomes": {},
                "newest_ns": 0,
            }
            self._groups[key] = g
        ts_raw = dp.get("time_unix_nano")
        ns = _finite_int(0 if ts_raw is None else ts_raw)
        if ns is None:
            # Ordering-only field: garbage degrades to oldest, never skips.
            ns = 0
        if ns > int(g["newest_ns"]):
            g["newest_ns"] = ns
        g["count"] += n
        g["sum"] = acc_sum
        if outcome:
            # Outcome tallies MUST be grouped too. Scoping only the buckets and
            # count would leave the outcome breakdown summing across generations
            # while count reported one — the dashboard would show N turns beside
            # an outcome bar totalling more than N, and a fault rate computed
            # over a different population than the latency next to it.
            g["outcomes"][outcome] = g["outcomes"].get(outcome, 0) + n
        mn, mx = _finite(dp.get("min")), _finite(dp.get("max"))
        if mn is not None:
            g["min"] = mn if g["min"] is None else min(g["min"], mn)
        if mx is not None:
            g["max"] = mx if g["max"] is None else max(g["max"], mx)
        if bc_f:
            if not g["buckets"]:
                g["buckets"] = [0] * len(bc_f)
            # Same bounds signature implies same bucket length (enforced per
            # point above), so this always holds; kept as cheap defense in
            # depth against a group built by older state.
            if len(bc_f) == len(g["buckets"]):
                for j, v in enumerate(bc_f):
                    g["buckets"][j] += v

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
                "count": 0,
                "mean_ms": 0.0,
                "p50_ms": 0.0,
                "p90_ms": 0.0,
                "min_ms": 0.0,
                "max_ms": 0.0,
                "other_generations": 0,
                "total_count": 0,
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


def _finite(raw: Any) -> float | None:
    """Coerce a shard scalar to a finite float, or None.

    THE single entry point for untrusted shard reads — the scalar branch in
    ``_aggregate`` and every field ``_Hist.add`` consumes. Shards are
    external input and Python's ``json`` accepts ``Infinity``/``NaN``
    literals, so a bare ``float(...)`` admits values that poison sums and an
    ``int(float(...))`` timestamp conversion raises ``OverflowError`` — four
    review rounds landed in this branch before this invariant: every scalar
    passes through here, and anything non-numeric or non-finite becomes None.
    """
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json accepts arbitrary-precision integers, and
        # float(10**400) overflows rather than returning inf.
        return None
    if not math.isfinite(v):
        return None
    return v


# OTel histogram count fields are uint64 on the wire; anything beyond this
# scale is garbage, and the bound keeps accumulated counts far below float
# range so downstream stats (float division in ``stats()``) cannot overflow.
_INT_BOUND = 2**63


def _finite_int(raw: Any) -> int | None:
    """Coerce a shard integer field (count, bucket count, ns) EXACTLY, or None.

    Integer inputs never roundtrip through float -- ``int(float(2**53 + 1))``
    silently rounds to 2**53 and the API would emit corrupted counts.
    Booleans (JSON ``true``/``false``) are garbage in an integer field, a
    negative value is invalid for uint64-wire counts, and a fractional value
    would silently truncate -- all three reject rather than coerce. Values
    beyond the uint64-scale bound are rejected either way.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        i = raw
    elif isinstance(raw, str):
        # protobuf JSON encodes uint64/int64 as STRINGS; parse them exactly
        # ("9007199254740993" through float would round to ...992). A
        # non-integer string falls through to the float path (e.g. "3.0").
        try:
            i = int(raw)
        except ValueError:
            f = _finite(raw)
            if f is None or not f.is_integer():
                return None
            i = int(f)
    else:
        f = _finite(raw)
        if f is None or not f.is_integer():
            return None
        i = int(f)
    return i if 0 <= i <= _INT_BOUND else None


def _day_of(dp: dict[str, Any], fallback: str) -> str:
    """The local calendar day a data point was recorded on, else *fallback*.

    ``OSError`` belongs in the except tuple below: ``astimezone()`` raises it on a
    broken tz database, and this is the only OS-touching call in the aggregation
    loop — which runs outside the shard reader's own ``OSError`` handler, so an
    escape here would 500 the whole panel over one malformed ``time_unix_nano``.
    """
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


def _iter_export_cycles(
    shard_paths: list[Path],
) -> Iterator[tuple[dict[str, Any], str, str]]:
    """Yield ``(export cycle, shard day, shard pid)`` per parseable shard line.

    One JSONL line is one ``MetricsData.to_json()`` export cycle. Corruption is
    skipped at the narrowest scope that can still be salvaged: one unparseable
    line, or one shard that is unreadable / not valid UTF-8. Cycles already
    yielded from a shard that then fails mid-read are kept — a torn tail must not
    discard the cycles ahead of it.

    Canonical shard names are ``metrics-YYYY-MM-DD-<pid>[-rotated…].jsonl``; the
    PID scopes scalar samples to their owning process. A stem that doesn't carry
    one aggregates under ``""`` rather than being dropped.
    """
    for p in shard_paths:
        stem_parts = p.stem.split("-")
        shard_day = "-".join(stem_parts[1:4])
        shard_pid = stem_parts[4] if len(stem_parts) > 4 and stem_parts[4].isdigit() else ""
        try:
            with p.open("rb") as fh:
                for line in bounded_records(fh, p, label="telemetry"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    yield obj, shard_day, shard_pid
        except (OSError, UnicodeDecodeError):
            continue


def _iter_metric_points(
    shard_paths: list[Path],
) -> Iterator[tuple[str, dict[str, Any], str, str, str, dict[str, Any]]]:
    """Yield ``(name, data point, shard day, shard pid, identity, data block)``.

    Every ``kirocrew.*`` data point is yielded; the name filter is load-bearing
    rather than defensive. One meter carries all three namespaces the recorder
    accepts — ``kirocrew.*``, ``gen_ai.*`` and ``app.<app_id>.*``
    (``metrics/schema.py``) — so points from every one of them reach this shard.
    Dropping the other two here is what keeps them out of :func:`_other_series`,
    whose rows the startup panel renders as core metrics.

    ``identity`` is the writing process's resource-level start-time token
    (``RESOURCE_ATTR_PROCESS_START_TIME``, stamped by the local exporter), or
    ``""`` for legacy shards written before the field existed — the scope level
    is still pure OTLP grouping and stays unread. Tolerate-garbage applies
    twice: a resource whose shape is not the exporter's dict form, AND a token
    that is not a string (the exporter only ever writes strings), both read as
    identity-less rather than raising or minting a spurious identity — a
    stringified garbage value would silently disable the legacy reset
    heuristic for that stream.

    The metric-level ``data`` block rides along because a Sum's block carries
    ``aggregation_temporality``/``is_monotonic`` while a Gauge's carries neither
    — the scalar branch of :func:`_aggregate` classifies on it.
    """
    for obj, shard_day, shard_pid in _iter_export_cycles(shard_paths):
        for rm in obj.get("resource_metrics", []) or []:
            resource = rm.get("resource")
            res_attrs = resource.get("attributes") if isinstance(resource, dict) else None
            identity = ""
            if isinstance(res_attrs, dict):
                raw = res_attrs.get(RESOURCE_ATTR_PROCESS_START_TIME)
                if isinstance(raw, str):
                    identity = raw
            for sm in rm.get("scope_metrics", []) or []:
                for metric in sm.get("metrics", []) or []:
                    name = metric.get("name") or ""
                    if not name.startswith("kirocrew."):
                        continue
                    data = metric.get("data") or {}
                    for dp in data.get("data_points", []) or []:
                        yield name, dp, shard_day, shard_pid, identity, data


def _daily_series(daily: dict[str, dict[str, _Hist]]) -> list[dict[str, Any]]:
    """Per-day startup percentiles (cold p50/p90, warm p50), oldest day first."""
    out: list[dict[str, Any]] = []
    for day in sorted(daily):
        c, w = daily[day]["cold"], daily[day]["warm"]
        out.append(
            {
                "date": day,
                "count": c.count + w.count,
                "cold_p50_ms": round(_pct_from_buckets(c.buckets, c.bounds, 0.50), 1),
                "cold_p90_ms": round(_pct_from_buckets(c.buckets, c.bounds, 0.90), 1),
                "warm_p50_ms": round(_pct_from_buckets(w.buckets, w.bounds, 0.50), 1),
            }
        )
    return out


def _model_key(attrs: dict[str, Any]) -> str:
    """The ``model`` attribute as a split key, or ``"unknown"``.

    The emitter omits the attribute entirely when the model is empty rather than
    sending ``model=""`` (``metrics/turns._model_attrs``), and shards written
    before the attribute existed carry it nowhere. Both fold to one named bucket
    so the amount is still counted: dropping the sample would make the split's
    totals disagree with the pooled ``total`` reported beside them.
    """
    raw = attrs.get("model")
    text = str(raw).strip() if raw is not None else ""
    return text or "unknown"


def _amount_by_model(by_model: dict[str, "_Hist"]) -> list[dict[str, Any]]:
    """Per-model spend attribution: count, total and mean, biggest spender first.

    Count/total/mean rather than the percentiles ``_amount_stats`` reports for the
    pooled figure. The question a spend split answers is "which model spent the
    money", which totals answer exactly; per-model p50/p90 would multiply the
    payload by a bucket array per model to answer a distribution question nobody
    asked of a single model.

    EVERY model, never truncated -- the same rule the sibling ``cost_breakdown``
    block on this page states for its own ``by_model``, and for the same reason: a
    top-N cut hides exactly the cheap-model-creep a spend split exists to show.
    Safe because the key is bounded by domain rather than by hope -- ``model`` is
    drawn from the host's CONFIGURED model set (see ``metrics/turns._model_attrs``,
    which is also what lets it be an attribute under the cardinality contract at
    all), so this is a handful of entries, not one per request.

    Sorted by total DESCENDING, then by name, so the row a reader wants is first
    and the order is stable across requests for equal totals.
    """
    rows: list[dict[str, Any]] = []
    for model, hist in by_model.items():
        g = hist._dominant()
        if g is None:
            continue
        cnt = int(g["count"])
        if cnt <= 0:
            continue
        total = float(g["sum"])
        rows.append(
            {
                "model": model,
                "count": cnt,
                "total": round(total, 6),
                "mean": round(total / cnt, 6),
            }
        )
    rows.sort(key=lambda r: (-float(r["total"]), str(r["model"])))
    return rows


def _amount_stats(
    hist: "_Hist", unit: str, by_model: dict[str, "_Hist"] | None = None
) -> dict[str, Any]:
    """Percentiles for a NON-duration histogram, under unit-neutral keys.

    ``_Hist.stats()`` names every field ``*_ms`` and rounds to one decimal, both
    of which are correct for the duration family and wrong for an amount: a
    dollar figure reported as ``p50_ms`` is a unit lie the frontend then formats
    with a millisecond suffix, and one-decimal rounding turns a sub-cent turn
    into ``0.0``. So the keys drop the suffix, the ``unit`` travels WITH the
    numbers instead of being implied by them, and rounding keeps six decimals —
    enough for a fraction of a cent to survive.

    Deliberately NOT a second signature on ``stats()``: the duration surfaces
    read ``p50_ms`` from a dozen places, and making that key conditional would
    make every one of them depend on a unit argument they have no reason to know
    about.

    ``by_model`` adds the attribution split. The pooled figure alone cannot
    answer "which model spent this", which is the question the ``model``
    attribute was added to the emitters to make answerable — without the split
    here, the attribute is recorded and then discarded at the one place that
    reports the amount. Absent (rather than empty) when no split was collected,
    so a caller that does not track one publishes no empty key.
    """
    g = hist._dominant()
    if g is None:
        return {"count": 0, "unit": unit}
    cnt = int(g["count"])
    out = {
        "count": cnt,
        "unit": unit,
        "total": round(float(g["sum"]), 6),
        "mean": round(float(g["sum"]) / cnt, 6) if cnt else 0.0,
        "p50": round(_pct_from_buckets(g["buckets"], g["bounds"], 0.50), 6),
        "p90": round(_pct_from_buckets(g["buckets"], g["bounds"], 0.90), 6),
        "min": round(g["min"], 6) if g["min"] is not None else 0.0,
        "max": round(g["max"], 6) if g["max"] is not None else 0.0,
        # Same disclosure the duration surfaces make: >0 means the window
        # straddles a bucket-boundary change and only the dominant generation is
        # reported here, with total_count the full population.
        "other_generations": hist.other_generations,
        "total_count": hist.total_count,
    }
    if by_model:
        out["by_model"] = _amount_by_model(by_model)
    return out


def _other_series(
    other_hist: dict[str, _Hist],
    other_split: dict[str, dict[str, _Hist]],
    other_ctr: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """The generic surface: every point that no dedicated block claimed.

    Selection is by data-point SHAPE, not by metric name, so a startup or turn
    point that is not a histogram lands here under its own name instead of in the
    block named after it.

    Histograms first, then counters, each group name-sorted, so a panel reading
    this list renders the same order on every request.
    """
    out: list[dict[str, Any]] = []
    for name in sorted(other_hist):
        s = other_hist[name].stats()
        s.update({"name": name, "kind": "histogram"})
        splits = other_split.get(name)
        if splits:
            s["splits"] = {sig: splits[sig].stats() for sig in sorted(splits)}
        out.append(s)
    for name in sorted(other_ctr):
        rec = other_ctr[name]
        out.append(
            {
                "name": name,
                "kind": "counter",
                "total": round(rec["total"], 3),
                "by_attr": {k: round(v, 3) for k, v in rec["by_attr"].items()},
            }
        )
    return out


def _cumulative_series(
    other_cum: dict[str, dict[tuple[str, str, str], list[tuple[int, float]]]],
) -> list[dict[str, Any]]:
    """Window-relative totals for lifetime-total streams, name-sorted.

    Two record shapes land here and they describe the same thing: a CUMULATIVE
    sum (what the process CPU/GC instruments wrote while they were observable
    counters) and a lifetime-total GAUGE (what they write now). Either way the
    stream re-emits a process-lifetime snapshot every export cycle. Two
    ordering/window traps shape this reducer:

    * Samples were buffered during the scan and are sorted by timestamp here,
      because shard iteration order is not chronological — a per-PID stream
      spans one shard per day plus rotations, and running reset detection in
      file order would misread an older sample seen after a newer one as a
      process restart, banking the newer segment and inflating the total.
    * Each stream's total is **window-relative**: its first in-window sample is
      the baseline, so a process older than the shard window reports only the
      activity that happened inside the window, never its lifetime total. A
      process that started in-window loses at most the activity before its
      first export cycle — under-reporting, never over-reporting.

    Streams are keyed by (shard PID, process identity, attrs). The identity is
    the resource-level start-time token the exporter stamps
    (``RESOURCE_ATTR_PROCESS_START_TIME``), so a PID reused by a new process
    lands in a NEW stream deterministically — each process contributes its own
    window-relative delta even when the reuser's first snapshot already exceeds
    the predecessor's maximum, the one shape the value heuristic below cannot
    see. An unchanged identity across provider rebuilds (telemetry off/on)
    stitches the rebuild segments into one stream. Within an identity-keyed
    stream a value below the running maximum is shard garbage, never a reset:
    one identity is one OS process, whose lifetime totals are monotonic,
    and banking a garbage drop would double-count the recovery.

    The value-below-segment-max RESET heuristic applies ONLY to identity-less
    streams (legacy shards written before the field existed, or platforms
    whose start-time read is unavailable): a drop marks a process boundary,
    banking the finished segment; re-emitted snapshots >= the max are no-ops,
    so provider rebuilds stay idempotent. Either way, stream total = banked
    segments + live segment - baseline (never negative: the baseline is a
    member of the first segment, so that segment's max bounds it);
    cross-process total = sum over streams.
    """
    out: list[dict[str, Any]] = []
    for name in sorted(other_cum):
        cum_attrs: dict[str, float] = {}
        cum_total = 0.0
        for (_, identity, csig), samples in other_cum[name].items():
            ordered = sorted(samples, key=lambda t: t[0])
            baseline = ordered[0][1]
            if identity:
                cval = max(val for _, val in ordered) - baseline
            else:
                banked = 0.0
                seg: float | None = None
                for _, val in ordered:
                    if seg is not None and val < seg:
                        banked += seg
                        seg = val
                    else:
                        seg = val if seg is None else max(seg, val)
                cval = banked + (seg or 0.0) - baseline
            if csig:
                cum_attrs[csig] = cum_attrs.get(csig, 0.0) + cval
            else:
                cum_total += cval
        if cum_total == 0.0 and cum_attrs:
            cum_total = sum(cum_attrs.values())
        out.append(
            {
                "name": name,
                "kind": "counter",
                "total": round(cum_total, 3),
                "by_attr": {a: round(x, 3) for a, x in cum_attrs.items()},
            }
        )
    return out


def _gauge_series(
    other_gauge: dict[str, dict[tuple[str, str], tuple[int, float]]],
) -> list[dict[str, Any]]:
    """Newest-sample rows for point-in-time gauges, name-sorted.

    Shards are per-PID and several kirocrew processes (gateway, MCP daemons)
    can export the same gauge names concurrently — collapsing them on timestamp
    alone would show whichever process exported last as "the" process state.
    """
    out: list[dict[str, Any]] = []
    for name in sorted(other_gauge):
        samples = other_gauge[name]
        pids = {pid for pid, _ in samples}
        if len(pids) <= 1:
            # One process in the window (the common case): same shape as before.
            latest = next((v for (_, k), v in samples.items() if k == ""), None)
            by_attr = {k: round(v[1], 3) for (_, k), v in samples.items() if k}
            headline = (
                latest[1] if latest is not None else max(samples.values(), key=lambda t: t[0])[1]
            )
        else:
            # Concurrent processes exported this gauge. The headline is the
            # newest process's reading (after a restart that is the live one),
            # and by_attr carries every process's own newest sample under a
            # pid= key so no process masquerades as another.
            newest_pid = max(
                pids,
                key=lambda pid: max(v[0] for (p, _), v in samples.items() if p == pid),
            )
            latest = next(
                (v for (p, k), v in samples.items() if p == newest_pid and k == ""),
                None,
            )
            headline = (
                latest[1]
                if latest is not None
                else max(
                    (v for (p, _), v in samples.items() if p == newest_pid),
                    key=lambda t: t[0],
                )[1]
            )
            by_attr = {}
            for (pid, k), v in sorted(samples.items()):
                sig = f"pid={pid or 'unknown'}" + (f",{k}" if k else "")
                by_attr[sig] = round(v[1], 3)
        out.append(
            {
                "name": name,
                "kind": "gauge",
                "latest": round(headline, 3),
                "by_attr": by_attr,
            }
        )
    return out


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
    # name -> (pid, attr-signature) -> (time_unix_nano, value): newest sample
    # wins WITHIN one process. Shards are per-PID and several kirocrew
    # processes (gateway, MCP daemons) can export the same gauge names
    # concurrently — collapsing them on timestamp alone would show whichever
    # process exported last as "the" process state.
    other_gauge: dict[str, dict[tuple[str, str], tuple[int, float]]] = {}
    # Lifetime-total streams — CUMULATIVE sums, plus the lifetime-total gauges
    # named by _lifetime_total_gauge_names(): per (pid, process-identity,
    # attrs) stream, buffer (time_unix_nano, value) samples during the scan.
    # The identity is the resource-level start-time token, so a reused PID
    # starts a NEW stream deterministically ("" for legacy shards, which
    # reduce under the value heuristic). The reduction — timestamp ordering,
    # counter-RESET detection, window-relative baseline — happens in
    # _cumulative_series once the scan is done, because shard iteration order
    # is not chronological and reset detection is only sound on a time-ordered
    # stream.
    other_cum: dict[str, dict[tuple[str, str, str], list[tuple[int, float]]]] = {}
    turn = _Hist()
    # The turn's billed amount, kept OUT of other_hist so it is never reported
    # under `*_ms` keys. Exactly one of the two is populated on a given host —
    # the acp backend bills credits, claude_code bills dollars — so the other
    # reports an empty stat block, which reads as "this host does not bill here"
    # rather than as a measured zero.
    turn_credits = _Hist()
    turn_cost = _Hist()
    # Spend per model, for the attribution split reported inside the turn block.
    # Keyed by the emitted ``model`` attribute; a turn served by a fallback model
    # blanks it, which _model_key folds to "unknown" rather than dropping the
    # sample -- spend that happened must appear in the split's total even when the
    # model that spent it cannot be named.
    turn_credits_by_model: dict[str, _Hist] = {}
    turn_cost_by_model: dict[str, _Hist] = {}
    # Resolved once per call rather than per datapoint: the first call imports the
    # instrument modules (deferred off the gateway boot path) and every later one
    # is a cached lookup.
    lifetime_totals = _lifetime_total_gauge_names()

    for name, dp, shard_day, shard_pid, identity, data in _iter_metric_points(shard_paths):
        attrs = dp.get("attributes") or {}
        is_hist = "bucket_counts" in dp
        if name == _STARTUP_METRIC and is_hist:
            # One startup emits an end-to-end point (phase absent, or
            # phase=total from the kiro path) PLUS one point per internal phase.
            # Only the end-to-end point is a startup: counting the phase points
            # too would multiply the startup count by ~4 and sum four unrelated
            # latency distributions into one set of buckets, a bimodal
            # "distribution" that is really set_model + session_new +
            # spawn_init + total stacked together.
            phase = str(attrs.get("phase", _PHASE_TOTAL))
            if phase != _PHASE_TOTAL:
                phases.setdefault(phase, _Hist()).add(dp)
                continue
            spawned = bool(attrs.get("spawned"))
            oc = str(attrs.get("outcome", "unknown"))
            (cold if spawned else warm).add(dp)
            # Which conversation source paid this startup. Older shards predate
            # the attribute, so they aggregate under "unknown" rather than being
            # dropped.
            by_channel.setdefault(str(attrs.get("channel", "unknown")), _Hist()).add(dp)
            # Outcomes go through _Hist so they are scoped to the same bounds
            # generation as the count and percentiles reported.
            overall.add(dp, outcome=oc)
            day = _day_of(dp, shard_day)
            db = daily.setdefault(day, {"cold": _Hist(), "warm": _Hist()})
            db["cold" if spawned else "warm"].add(dp)
        elif name == _TURN_METRIC and is_hist:
            turn.add(dp, outcome=str(attrs.get("outcome", "unknown")))
        elif name == _TURN_CREDITS_METRIC and is_hist:
            turn_credits.add(dp)
            turn_credits_by_model.setdefault(_model_key(attrs), _Hist()).add(dp)
        elif name == _TURN_COST_METRIC and is_hist:
            turn_cost.add(dp)
            turn_cost_by_model.setdefault(_model_key(attrs), _Hist()).add(dp)
        elif is_hist:
            other_hist.setdefault(name, _Hist()).add(dp)
            for ak in _OTHER_SPLIT_ATTRS:
                if ak not in attrs:
                    continue
                sig = f"{ak}={str(attrs[ak]).lower()}"
                other_split.setdefault(name, {}).setdefault(sig, _Hist()).add(dp)
        elif "value" in dp:
            # Shards are external input and this parser's contract is
            # tolerate-garbage (guarded json.loads upstream, _Hist.add's
            # TypeError/ValueError guards for histograms). The scalar branch
            # follows the same invariant: every field read coerces defensively
            # — a garbage value skips the point, a garbage timestamp sorts
            # oldest (gauges) or skips the point (cumulative sums, which
            # cannot order an untimed sample) — so one bad record can never
            # 500 the endpoint.
            fval = _finite(dp.get("value"))
            if fval is None:
                continue
            val = fval
            fts = _finite(dp.get("time_unix_nano") or 0)
            ts = int(fts) if fts is not None else 0
            # A Sum's data block carries aggregation_temporality/is_monotonic;
            # a Gauge's carries neither. DELTA sums (regular counters)
            # accumulate across cycles. CUMULATIVE sums re-emit a
            # process-lifetime snapshot every cycle, so summing them would
            # multiply by cycle count — they are buffered per (PID, identity,
            # attrs) stream and reduced window-relative after the scan
            # (_cumulative_series). Gauges keep the newest sample per attribute
            # set, EXCEPT the lifetime-total gauges (process CPU/GC), whose
            # newest sample is a running total rather than a state: those take
            # the same window-relative path, which is also what stitches them to
            # the cumulative-sum records the same instruments wrote before they
            # became gauges. See _lifetime_total_gauge_names().
            is_sum = "aggregation_temporality" in data or "is_monotonic" in data
            try:
                # OTel JSON: DELTA=1, CUMULATIVE=2. Same chokepoint contract
                # as _finite: json accepts Infinity literals, and int(inf)
                # raises OverflowError, not ValueError.
                cumulative = int(data.get("aggregation_temporality") or 0) == 2
            except (TypeError, ValueError, OverflowError):
                cumulative = False
            key = ",".join(f"{k}={attrs[k]}" for k in sorted(attrs)) if attrs else ""
            if (is_sum and cumulative) or (not is_sum and name in lifetime_totals):
                if ts <= 0:
                    # A lifetime total that cannot be ordered cannot join the
                    # delta math — and letting it sort oldest would make it the
                    # stream baseline, resurrecting the
                    # lifetime-as-window-total bug on one corrupt record.
                    continue
                other_cum.setdefault(name, {}).setdefault((shard_pid, identity, key), []).append(
                    (ts, val)
                )
            elif is_sum:
                rec = other_ctr.setdefault(name, {"total": 0.0, "by_attr": {}})
                rec["total"] += val
                if attrs:
                    rec["by_attr"][key] = rec["by_attr"].get(key, 0.0) + val
            else:
                g = other_gauge.setdefault(name, {})
                gkey = (shard_pid, key)
                prev = g.get(gkey)
                if prev is None or ts >= prev[0]:
                    g[gkey] = (ts, val)

    daily_out = _daily_series(daily)
    other = _other_series(other_hist, other_split, other_ctr)
    other.extend(_cumulative_series(other_cum))
    other.extend(_gauge_series(other_gauge))

    turn_outcome = turn.outcomes
    turn_total = sum(turn_outcome.values())
    turn_faults = sum(v for k, v in turn_outcome.items() if k in _TERMINAL_FAULT_OUTCOMES)
    # fault_rate is computed over the turns whose outcome is KNOWN, not over every
    # turn. ``unclassified`` marks a turn whose surface had no stop reason to give
    # (a helper call site passing a bare TurnUsage), and it cannot go in either
    # position honestly: in the numerator it invents a fault for every clean
    # background turn, and in the denominator alone it silently dilutes the rate
    # towards zero as background traffic grows — an optimistic dashboard, which is
    # the failure mode this metric's widening was supposed to end. Excluded from
    # both. The count needs no field of its own: it already ships in this same
    # response as ``outcome["unclassified"]``, so a reader can see exactly how
    # much of the window fault_rate does not cover.
    turn_classified = turn_total - turn_outcome.get("unclassified", 0)
    turn_block = {
        # ``other_generations`` arrives via stats(): >0 means the window
        # straddles a bucket-boundary change and only the dominant generation
        # is reported (see _Hist).
        **turn.stats(),
        "outcome": turn_outcome,
        "fault_rate": round(turn_faults / turn_classified, 4) if turn_classified else 0.0,
        # What the turn COST, beside how long it took, so spend per turn is read
        # against latency over the same population rather than joined by hand.
        "credits": _amount_stats(turn_credits, "credit", turn_credits_by_model),
        "cost_usd": _amount_stats(turn_cost, "usd", turn_cost_by_model),
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
            "phases": [{"name": n, **phases[n].stats()} for n in sorted(phases)],
            # Startup cost grouped by conversation source, so a slow surface can
            # be identified directly instead of being inferred by correlating
            # export windows against the gateway log.
            "by_channel": [{"name": n, **by_channel[n].stats()} for n in sorted(by_channel)],
        },
        "turn": turn_block,
        "other": other,
    }


def _parse_startup_metrics() -> dict[str, Any]:
    """Windowed + fingerprint-cached aggregation over the metric shards."""
    global _CACHE, _CACHE_KEY, _CACHE_TS
    directory = _telemetry_cfg().directory
    shards = _shards_in_window(directory, _WINDOW_DAYS)
    if not shards:
        _CACHE, _CACHE_KEY = None, None
        return {"startup": None, "turn": None, "other": [], "shard_count": 0}

    try:
        key = tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in shards))
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
    state = _telemetry_cfg()
    data = await asyncio.to_thread(_parse_startup_metrics)
    context = await asyncio.to_thread(_context_block)
    cost = await asyncio.to_thread(_cost_block)
    if cost:
        cost = await _with_conversation_titles(request, cost)
    return web.json_response(
        {
            "enabled": state.enabled,
            "env_pinned": state.env_pinned,
            "env_var": state.env_var,
            "window_days": _WINDOW_DAYS,
            "metrics_dir": str(state.directory),
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

    Dashboard-only. Unlike ``/api/usage/turns`` this reader has no row-ownership
    model, and its rows carry the turn's billing — so an app caller is refused
    outright (deny-by-default, App Kit §5.2) rather than handed an arbitrary
    slot's data. The 404 is indistinguishable from an unknown route on purpose,
    and the refusal is SEL-audited like every app-caller decision.
    """
    request_app = str(request.get("app", "") or "")
    slot = (request.query.get("slot") or "").strip()
    if request_app:

        def _audit_denied() -> None:
            _sel_mod.sel().log_api_access(
                caller=request_app,
                operation="context_trace",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot or '(missing)'}",
                error="dashboard-only endpoint",
            )

        await asyncio.to_thread(_audit_denied)
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    if not slot:
        return web.json_response({"error": "slot is required", "code": "slot_required"}, status=400)
    trace = await asyncio.to_thread(context_trace, slot, _WINDOW_DAYS)
    return web.json_response(trace)


async def api_usage_turns(request: web.Request) -> web.Response:
    """GET /api/usage/turns?slot=<session key>[&days=N] — per-turn usage rows.

    The per-turn drill-down under the Spend tab's aggregate, and the surface an
    APP is granted (via its manifest's ``permissions.api``) to account for what
    its own agent slots cost — tokens, credits, duration and the context meter,
    one row per turn. Same independence as the context trace: usage rows are
    always written, so this works with OTEL collection off.

    App isolation is ROW-level (App Kit §5.2, deny-by-default): an app caller
    receives only rows stamped with its own app at write time, however the slot
    is named and whether or not it is still live. A foreign slot key therefore
    answers 200 with no rows — indistinguishable from a slot that never ran —
    and rows that predate the stamp are invisible to app callers. A live-slot
    ownership check was deliberately rejected: it leaks on slot-name reuse and
    denies an app its own completed sessions, which are exactly what an audit
    reads. A DISABLED app is refused outright (``is_app_enabled``,
    deny-by-default, same gate the opt-in builtin routes wrap every handler
    in): disable must revoke read access, not only future writes.

    Every app-caller decision is SEL-logged — including a malformed request's
    refusal, so a probing app leaves a trail — and all SEL calls plus the
    enablement check run off-loop (first use initialises SEL's key material on
    disk). ``days`` clamps to the spend window's ceiling rather than refusing:
    shards beyond it have been retired anyway.
    """
    request_app = str(request.get("app", "") or "")
    slot = (request.query.get("slot") or "").strip()

    def _audit(outcome: str, error: str = "", resources: str = "") -> None:
        _sel_mod.sel().log_api_access(
            caller=request_app,
            operation="usage_turns",
            outcome=outcome,
            source="app_isolation",
            resources=resources or f"slot={slot or '(missing)'}",
            error=error,
        )

    if request_app and not await asyncio.to_thread(_app_is_enabled, request_app):
        await asyncio.to_thread(_audit, "denied", "app is disabled")
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    if not slot:
        if request_app:
            await asyncio.to_thread(_audit, "denied", "slot missing")
        return web.json_response({"error": "slot is required", "code": "slot_required"}, status=400)
    try:
        days = int(request.query.get("days") or SPEND_WINDOW_DAYS)
    except ValueError:
        days = SPEND_WINDOW_DAYS
    days = max(1, min(days, SPEND_WINDOW_DAYS))
    turns = await asyncio.to_thread(
        slot_turn_usage, slot, days, app=request_app if request_app else None
    )
    if request_app:
        await asyncio.to_thread(_audit, "allowed", "", f"slot={slot} rows={len(turns)}")
    return web.json_response({"slot": slot, "days": days, "turns": turns})


def _app_is_enabled(app_name: str) -> bool:
    """Deny-by-default enablement probe, import deferred to the worker thread.

    Late import for the same reason the builtin routes defer theirs: the apps
    manager pulls in the registry, and a module-scope import here would create
    a handlers→apps import edge the dashboard package deliberately avoids.
    """
    try:
        from kiro_crew.apps.manager import is_app_enabled

        return bool(is_app_enabled(app_name))
    except Exception:  # noqa: BLE001 — an unanswerable check is a denial
        return False


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
        titles.update(await asyncio.to_thread(_persisted_titles, conversation_log, unresolved))

    rows = []
    for row in conversations:
        title = titles.get(str(row.get("slot") or ""))
        if title:
            safe, _ = redact_exfiltration_urls(title)
            safe, _ = redact_credentials(safe)
            row = {**row, "title": safe}
        rows.append(row)
    return {**cost, "conversations": rows}


def _telemetry_overlay_pins(leaf: str) -> bool:
    """Return whether ``config.local.json`` sets ``telemetry.<leaf>``.

    That overlay deep-merges OVER ``config.json`` at load, and the Settings
    toggles write the BASE file — so an entry here makes a switch snap back to
    the overlay's value after a successful write. Reporting it lets the panel say
    why instead of looking broken. Best-effort: an unreadable or malformed
    overlay is reported as "not pinned" rather than raising, since this is a
    diagnostic (the effective value the handler reports is still authoritative).

    Shared by both telemetry switches: the shadowing mechanism is the overlay,
    not the key, so a second copy per key would be two things to keep in sync.
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
    return isinstance(section, dict) and leaf in section


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
        overlay_override = await asyncio.to_thread(_telemetry_overlay_pins, "beacon_enabled")
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


async def api_collection_status(request: web.Request) -> web.Response:
    """GET /api/telemetry/collection — local metric collection state for Settings → Privacy.

    Powers the recording switch, and is deliberately separate from
    ``/api/telemetry/startup``: that route parses every metric shard in the window
    to aggregate percentiles, which is far too much work for a panel that only
    needs to know whether a switch is on.

    ``enabled`` is the EFFECTIVE state rather than the stored flag, because
    ``KIROCREW_TELEMETRY`` overrides ``telemetry.enabled`` inside the collector: a
    switch that read back the config value alone would sit on "off" while metrics
    were being written. ``env_pinned`` says the env var is what decides, so the
    panel can disable the control instead of offering a write the collector
    ignores, and ``overlay_override`` does the same for a ``config.local.json``
    entry that would make the switch snap back after a successful save.

    ``otlp_configured`` reports that enabling collection would send metrics off
    this machine — resolved from the active telemetry provider's destination set,
    the same one ``_build_recorder`` attaches readers for, not from the
    ``telemetry.otlp_endpoint`` string (which is only how the DEFAULT provider
    names a destination; an edition may supply its own collector). That makes
    collection not-local — ``_build_recorder`` attaches an OTLP reader — so the
    config route refuses to ENABLE it from here and the panel disables that
    direction rather than offering a write that comes back 409. Disabling stays
    available on such a host, which is where an opt-out matters most. The endpoint
    itself is never returned: it can carry credentials, and the panel only needs to
    know that one exists.

    Nothing here is an egress control — the switch cannot start egress, by the gate
    above — so unlike the beacon there is no governance ceiling to report.
    """
    state = await asyncio.to_thread(_telemetry_cfg)
    overlay_override = await asyncio.to_thread(_telemetry_overlay_pins, "enabled")
    return web.json_response(
        {
            "enabled": state.enabled,
            "env_pinned": state.env_pinned,
            "env_var": state.env_var,
            "overlay_override": overlay_override,
            "otlp_configured": state.otlp_configured,
            "metrics_dir": str(state.directory),
        }
    )
