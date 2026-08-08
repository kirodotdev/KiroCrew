"""``/api/system`` must bound its own cost.

Two independent leaks are pinned here:

1. The TTL is stamped only AFTER a collection returns, so while one is running
   every further poll saw a stale cache and started its own. The dashboard polls
   this endpoint at exactly the TTL, so on a host where a collection is slow
   (macOS spawns six subprocesses; Linux reads /proc and spawns none) the
   collections stacked up.
2. The whole-machine process scan was recomputed on the live graph's cadence,
   even though a process COUNT does not need 2s freshness and costs a full ``ps``
   walk off /proc — a walk the shared MCP gateway makes bigger.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from kiro_crew.dashboard import handlers_system as hs


@pytest.fixture(autouse=True)
def _clear_caches():
    hs._metrics_cache = {}
    hs._metrics_cache_ts = 0.0
    hs._metrics_lock = None
    hs._proc_scan_cache = {}
    hs._proc_scan_cache_ts = 0.0
    yield
    hs._metrics_cache = {}
    hs._metrics_cache_ts = 0.0
    hs._metrics_lock = None
    hs._proc_scan_cache = {}
    hs._proc_scan_cache_ts = 0.0


class _Req(dict):
    """Minimal stand-in for web.Request — api_system reads nothing off it."""


@pytest.mark.asyncio
async def test_concurrent_pollers_share_one_collection(monkeypatch):
    """Several tabs polling at once must cost ONE collection, not one each.

    The collection is made genuinely slow (a real blocking sleep on the real
    executor) because that is the condition under which the un-coalesced version
    launched a new one per poll: the TTL is stamped only after a collection
    returns, so every poll arriving DURING one saw a stale cache. The count is
    not scheduling-dependent — the lock either serializes these or it does not.
    """
    calls = {"n": 0}

    def slow_collect():
        calls["n"] += 1
        time.sleep(0.25)
        return {"cpu_pct": 1.0, "call": calls["n"]}

    monkeypatch.setattr(hs, "_collect_system_metrics", slow_collect)

    results = await asyncio.gather(*(hs.api_system(_Req()) for _ in range(5)))

    assert calls["n"] == 1, f"expected one collection for five concurrent polls, got {calls['n']}"
    assert all(r.status == 200 for r in results)


def test_process_scan_is_cached_past_the_metrics_ttl(monkeypatch):
    """The expensive scan must NOT refresh on the live graph's cadence."""
    calls = {"n": 0}
    monkeypatch.setattr(
        hs, "_scan_mcp_processes", lambda: (calls.__setitem__("n", calls["n"] + 1) or {"mcp_total": calls["n"]})
    )
    clock = {"t": 500.0}
    monkeypatch.setattr(hs.time, "monotonic", lambda: clock["t"])

    data: dict[str, object] = {}
    hs._apply_mcp_process_counts(data)
    assert calls["n"] == 1

    # Well past the metrics TTL, but inside the scan's own TTL.
    clock["t"] += hs._METRICS_CACHE_TTL * 3
    assert hs._METRICS_CACHE_TTL * 3 < hs._PROC_SCAN_CACHE_TTL
    hs._apply_mcp_process_counts(data)
    assert calls["n"] == 1, "process scan must not follow the metrics TTL"

    clock["t"] += hs._PROC_SCAN_CACHE_TTL
    hs._apply_mcp_process_counts(data)
    assert calls["n"] == 2, "past its own TTL the scan must refresh"


def test_process_scan_ttl_is_longer_than_the_metrics_ttl():
    """The whole point of splitting the caches — if these converge the split is
    decorative and the scan is back on the graph's cadence."""
    assert hs._PROC_SCAN_CACHE_TTL > hs._METRICS_CACHE_TTL


def test_scan_reports_both_fields_even_when_it_fails(monkeypatch):
    """A failed scan must still populate both keys, so the UI renders 0 rather
    than dropping the field and showing undefined."""
    monkeypatch.setattr(hs.os, "listdir", lambda p: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(hs.sys, "platform", "linux")

    out = hs._scan_mcp_processes()

    assert out["mcp_total"] == 0
    assert out["mcp_processes"] == {"sandbox": 0, "kiro_cli": 0}
