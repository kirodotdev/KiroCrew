"""Every histogram the telemetry API publishes must disclose a mixed window.

``_Hist`` reports only the dominant bucket-boundary generation on purpose, so a
boundary change cannot sum two unrelated distributions. The cost is that on a
mixed window ``count`` is a subset. ``other_generations`` + ``total_count`` are
what let a card say "showing 141 of 1970" instead of publishing the subset as if
it were the whole population.

The bug these tests lock out: the disclosure was added by hand in the ``turn``
and ``startup`` blocks and forgotten in the generic ``other`` surface, so three
real metrics under-reported by 58-93% with nothing on the page saying so, and a
histogram card contradicted the counter for the same event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kiro_crew.dashboard.handlers.telemetry import _aggregate, _Hist

_BOUNDS_A = [10.0, 50.0, 100.0, 500.0, 1000.0]
_BOUNDS_B = [25.0, 250.0, 2500.0, 25000.0, 250000.0]   # a boundary change

# Anything kirocrew.* that is neither the startup metric nor turn.duration lands
# in the generic `other` surface -- the one that forgot the disclosure.
_OTHER_METRIC = "kirocrew.mcp.backend.acquire.duration"


def _dp(bounds: list[float], bucket: int, count: int, ns: int) -> dict[str, Any]:
    counts = [0] * (len(bounds) + 1)
    counts[bucket] = count
    return {
        "attributes": {},
        "count": count,
        "sum": float(count * 15),
        "min": 15.0,
        "max": 15.0,
        "bucket_counts": counts,
        "explicit_bounds": bounds,
        "time_unix_nano": ns,
    }


def _shard(tmp_path: Path, metrics: list) -> list[Path]:
    line = {"resource_metrics": [{"scope_metrics": [{"metrics": metrics}]}]}
    p = tmp_path / "metrics-2026-08-02-0001.jsonl"
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return [p]


# ───────────────────────── _Hist semantics ─────────────────────────

def test_clean_window_reports_total_equal_to_count():
    """Single generation: the subset IS the population, so the two agree."""
    h = _Hist()
    h.add(_dp(_BOUNDS_A, 1, count=7, ns=1))
    s = h.stats()
    assert s["other_generations"] == 0
    assert s["total_count"] == s["count"] == 7


def test_mixed_window_total_exceeds_the_reported_count():
    h = _Hist()
    h.add(_dp(_BOUNDS_A, 1, count=5, ns=1))     # older generation
    h.add(_dp(_BOUNDS_B, 2, count=2, ns=99))    # newer, wins on recency
    s = h.stats()
    assert s["other_generations"] == 1
    assert s["count"] == 2, "recency should pick the newer generation"
    assert s["total_count"] == 7, "total must span EVERY generation"


def test_total_count_is_not_just_the_dominant_count():
    """Negative control for the test above.

    If ``total_count`` were wired to the dominant group it would still equal
    ``count`` here and `test_mixed_window_...` would pass vacuously for the
    wrong reason.
    """
    h = _Hist()
    h.add(_dp(_BOUNDS_A, 1, count=5, ns=1))
    h.add(_dp(_BOUNDS_B, 2, count=2, ns=99))
    assert h.total_count != h.count


def test_empty_histogram_still_carries_the_keys():
    """Shape uniformity: a consumer spreading stats() always gets the fields."""
    s = _Hist().stats()
    assert s["other_generations"] == 0
    assert s["total_count"] == 0


# ─────────────────── the API surface that regressed ───────────────────

def test_other_histograms_disclose_a_mixed_window(tmp_path):
    """REGRESSION: this is what the `other` surface used to omit entirely.

    Fails on the pre-fix code, where the `other` list was built from stats()
    plus only name/kind.
    """
    shards = _shard(tmp_path, [{
        "name": _OTHER_METRIC,
        "data": {"data_points": [
            _dp(_BOUNDS_A, 1, count=9, ns=1),
            _dp(_BOUNDS_B, 2, count=3, ns=99),
        ]},
    }])
    res = _aggregate(shards)
    hists = [e for e in res["other"] if e.get("kind") == "histogram"]
    assert hists, "the metric did not reach the `other` surface"
    entry = next(e for e in hists if e["name"] == _OTHER_METRIC)
    assert entry["other_generations"] == 1
    assert entry["count"] == 3
    assert entry["total_count"] == 12


def _histogram_blocks(node: Any, path: str = "") -> list[tuple[str, dict]]:
    """Every dict in the response that is shaped like histogram stats."""
    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if "count" in node and "p50_ms" in node:
            found.append((path or "<root>", node))
        for k, v in node.items():
            found.extend(_histogram_blocks(v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(_histogram_blocks(v, f"{path}[{i}]"))
    return found


def test_every_published_histogram_carries_the_disclosure(tmp_path):
    """Structural invariant, so a FUTURE surface cannot omit it again.

    Deliberately a walk over the whole response rather than a per-block
    assertion: the failure mode being guarded is a new histogram surface added
    later that forgets the field, which named-block assertions would not catch.
    """
    shards = _shard(tmp_path, [
        {"name": _OTHER_METRIC,
         "data": {"data_points": [_dp(_BOUNDS_A, 1, count=4, ns=1)]}},
        {"name": "kirocrew.turn.duration",
         "data": {"data_points": [_dp(_BOUNDS_A, 2, count=2, ns=2)]}},
        {"name": "kirocrew.startup.duration",
         "data": {"data_points": [_dp(_BOUNDS_A, 1, count=3, ns=3)]}},
    ])
    res = _aggregate(shards)
    blocks = _histogram_blocks(res)
    assert len(blocks) >= 4, (
        f"walker found only {len(blocks)} histogram blocks -- too few for the "
        "assertion below to mean anything"
    )
    for path, blk in blocks:
        assert "other_generations" in blk, f"{path} omits other_generations"
        assert "total_count" in blk, f"{path} omits total_count"


def test_the_walker_can_actually_fail():
    """Negative control for the invariant test's own walker."""
    blocks = _histogram_blocks({"a": {"count": 1, "p50_ms": 2.0}})
    assert len(blocks) == 1
    assert "other_generations" not in blocks[0][1]
