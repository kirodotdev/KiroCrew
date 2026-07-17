"""Telemetry handlers — read the local OTEL metric shards for the dashboard.

Stage-1 wired an OpenTelemetry recorder whose default sink is per-process JSONL
under ``~/.kirocrew/metrics/metrics-YYYY-MM-DD-<pid>.jsonl`` (see
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

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.hooks import validate_file_path

logger = logging.getLogger(__name__)

_STARTUP_METRIC = "kirocrew.session.startup.duration"
_TURN_METRIC = "kirocrew.turn.duration"
_WINDOW_DAYS = 14

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
    """Accumulator merging histogram data points that share a dimension key."""

    __slots__ = ("count", "sum", "min", "max", "buckets", "bounds")

    def __init__(self) -> None:
        self.count = 0
        self.sum = 0.0
        self.min: float | None = None
        self.max: float | None = None
        self.buckets: list[int] = []
        self.bounds: list[float] = []

    def add(self, dp: dict[str, Any]) -> None:
        bc = dp.get("bucket_counts") or []
        eb = dp.get("explicit_bounds") or []
        self.count += int(dp.get("count", 0) or 0)
        self.sum += float(dp.get("sum", 0.0) or 0.0)
        mn, mx = dp.get("min"), dp.get("max")
        if mn is not None:
            self.min = mn if self.min is None else min(self.min, mn)
        if mx is not None:
            self.max = mx if self.max is None else max(self.max, mx)
        if bc:
            if not self.buckets:
                self.buckets = [0] * len(bc)
                self.bounds = list(eb)
            if len(bc) == len(self.buckets):
                for j, v in enumerate(bc):
                    self.buckets[j] += int(v or 0)

    def stats(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean_ms": round(self.sum / self.count, 1) if self.count else 0.0,
            "p50_ms": round(_pct_from_buckets(self.buckets, self.bounds, 0.50), 1),
            "p90_ms": round(_pct_from_buckets(self.buckets, self.bounds, 0.90), 1),
            "min_ms": round(self.min, 1) if self.min is not None else 0.0,
            "max_ms": round(self.max, 1) if self.max is not None else 0.0,
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
    outcome: dict[str, int] = {}
    daily: dict[str, dict[str, _Hist]] = {}  # day -> {"cold"|"warm": _Hist}
    # generic surface for every other kirocrew.* metric
    other_hist: dict[str, _Hist] = {}
    other_ctr: dict[str, dict[str, Any]] = {}  # name -> {total, by_attr}
    turn = _Hist()
    turn_outcome: dict[str, int] = {}  # kirocrew.turn.duration count by outcome

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
                                        overall.add(dp)
                                        spawned = bool(attrs.get("spawned"))
                                        (cold if spawned else warm).add(dp)
                                        oc = str(attrs.get("outcome", "unknown"))
                                        outcome[oc] = outcome.get(oc, 0) + int(
                                            dp.get("count", 0) or 0
                                        )
                                        day = _day_of(dp, shard_day)
                                        db = daily.setdefault(
                                            day, {"cold": _Hist(), "warm": _Hist()}
                                        )
                                        db["cold" if spawned else "warm"].add(dp)
                                    elif name == _TURN_METRIC and is_hist:
                                        turn.add(dp)
                                        oc = str(attrs.get("outcome", "unknown"))
                                        turn_outcome[oc] = turn_outcome.get(oc, 0) + int(
                                            dp.get("count", 0) or 0
                                        )
                                    elif is_hist:
                                        other_hist.setdefault(name, _Hist()).add(dp)
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

    turn_total = sum(turn_outcome.values())
    turn_faults = sum(v for k, v in turn_outcome.items() if k != "ok")
    turn_block = {
        **turn.stats(),
        "outcome": turn_outcome,
        "fault_rate": round(turn_faults / turn_total, 4) if turn_total else 0.0,
    }

    return {
        "startup": {
            "overall": overall.stats(),
            "cold": cold.stats(),
            "warm": warm.stats(),
            "outcome": outcome,
            "daily": daily_out,
            "distribution": {"buckets": overall.buckets, "bounds": overall.bounds},
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


async def api_telemetry_startup(request: web.Request) -> web.Response:
    """GET /api/telemetry/startup — session-startup latency + all kirocrew.* metrics.

    Returns ``enabled`` (telemetry main switch), ``window_days``, ``shard_count``,
    a detailed ``startup`` block (overall/cold/warm p50/p90 + outcome + daily), and
    a generic ``other`` list surfacing every other emitted kirocrew.* metric.
    """
    enabled, directory = _telemetry_cfg()
    data = await asyncio.to_thread(_parse_startup_metrics)
    return web.json_response(
        {
            "enabled": enabled,
            "window_days": _WINDOW_DAYS,
            "metrics_dir": str(directory),
            "shard_count": data.get("shard_count", 0),
            "startup": data.get("startup"),
            "turn": data.get("turn"),
            "other": data.get("other", []),
        }
    )
