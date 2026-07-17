"""Drive the REAL telemetry aggregation over synthetic OTEL metric shards.

These exercise production code paths in dashboard/handlers/telemetry.py
(``_pct_from_buckets``, ``_Hist``, ``_aggregate``) rather than replicating the
logic, so a regression in the shard parser or percentile math fails the test.
"""
import json
from pathlib import Path

from kiro_crew.dashboard.handlers.telemetry import _aggregate, _Hist, _pct_from_buckets

_BOUNDS = [10, 20, 30, 40, 50]


def test_pct_from_buckets_interpolates_within_bucket():
    # bucket_counts has len(bounds)+1 entries; all 4 obs fall in the 20-30 bucket.
    counts = [0, 0, 4, 0, 0, 0]
    p50 = _pct_from_buckets(counts, _BOUNDS, 0.50)
    assert 20.0 <= p50 <= 30.0


def test_pct_from_buckets_empty_is_zero():
    assert _pct_from_buckets([0, 0], [10], 0.5) == 0.0


def test_pct_from_buckets_overflow_bucket_returns_lower_bound():
    # All obs in the +Inf overflow bucket (index == len(bounds)).
    assert _pct_from_buckets([0, 0, 0, 0, 0, 3], _BOUNDS, 0.90) == float(_BOUNDS[-1])


def test_hist_merges_data_points():
    h = _Hist()
    dp = {
        "count": 2, "sum": 30.0, "min": 10.0, "max": 20.0,
        "bucket_counts": [0, 1, 1, 0, 0, 0], "explicit_bounds": _BOUNDS,
    }
    h.add(dp)
    h.add(dp)
    s = h.stats()
    assert s["count"] == 4
    assert s["min_ms"] == 10.0
    assert s["max_ms"] == 20.0
    assert s["mean_ms"] == 15.0  # 60.0 / 4


def _write_shard(tmp_path: Path, metrics: list) -> Path:
    line = {"resource_metrics": [{"scope_metrics": [{"metrics": metrics}]}]}
    p = tmp_path / "metrics-2026-07-11-1234.jsonl"
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return p


def test_aggregate_startup_turn_and_other(tmp_path: Path):
    startup = {"name": "kirocrew.session.startup.duration", "data": {"data_points": [
        {"attributes": {"outcome": "ready", "spawned": True}, "count": 3, "sum": 45.0,
         "min": 10.0, "max": 25.0, "bucket_counts": [0, 1, 1, 1, 0, 0], "explicit_bounds": _BOUNDS},
    ]}}
    turn = {"name": "kirocrew.turn.duration", "data": {"data_points": [
        {"attributes": {"outcome": "ok"}, "count": 3, "sum": 30.0, "min": 5.0, "max": 15.0,
         "bucket_counts": [1, 1, 1, 0, 0, 0], "explicit_bounds": _BOUNDS},
        {"attributes": {"outcome": "error"}, "count": 1, "sum": 45.0, "min": 45.0, "max": 45.0,
         "bucket_counts": [0, 0, 0, 0, 1, 0], "explicit_bounds": _BOUNDS},
    ]}}
    warm = {"name": "kirocrew.mcp.warm_pool.acquire", "data": {"data_points": [
        {"attributes": {"result": "hit"}, "value": 3},
        {"attributes": {"result": "miss"}, "value": 1},
    ]}}

    result = _aggregate([_write_shard(tmp_path, [startup, turn, warm])])

    # Startup: split by spawned, distribution buckets surfaced.
    assert result["startup"]["overall"]["count"] == 3
    assert result["startup"]["cold"]["count"] == 3  # spawned=True
    assert result["startup"]["warm"]["count"] == 0
    assert result["startup"]["distribution"]["buckets"]

    # Turn: outcome split + fault rate = non-ok / total.
    assert result["turn"]["outcome"] == {"ok": 3, "error": 1}
    assert result["turn"]["fault_rate"] == 0.25  # 1 error / 4

    # Other: warm-pool counter with per-attr breakdown.
    warm_rows = [o for o in result["other"] if o["name"] == "kirocrew.mcp.warm_pool.acquire"]
    assert warm_rows and warm_rows[0]["kind"] == "counter"
    assert warm_rows[0]["total"] == 4.0
    assert warm_rows[0]["by_attr"]["result=hit"] == 3.0
    assert warm_rows[0]["by_attr"]["result=miss"] == 1.0
