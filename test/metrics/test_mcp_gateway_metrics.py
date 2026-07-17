"""Tests for MCP gateway OTEL metric emit call-sites.

Every test drives the REAL production emit path with a patched recorder and
asserts the captured metric name + attributes:

* ``kirocrew.mcp.warm_pool.acquire`` — via ``HotKeyStore.record_outcome``
* ``kirocrew.mcp.backend.acquire.duration`` — via ``gatewayd._emit_backend_acquire_metric``
* ``kirocrew.mcp.lazy_load.{count,duration}`` — via ``gatewayd._emit_lazy_load_metrics``

The metric names/attrs live in production (prewarm.py / gatewayd.py), so a
rename, attribute change, or removed emit fails these tests rather than passing
green against logic the test wrote itself.
"""

from unittest.mock import patch


class _CapturingRecorder:
    """Stand-in recorder that captures every counter() and histogram() call."""

    def __init__(self) -> None:
        self.counters: list[tuple[str, dict]] = []
        self.histograms: list[tuple[str, float, str, dict]] = []

    def counter(self, name: str, *, attrs: dict | None = None, **kwargs) -> None:
        self.counters.append((name, dict(attrs or {})))

    def histogram(
        self, name: str, value: float, *, unit: str = "ms", attrs: dict | None = None, **kwargs
    ) -> None:
        self.histograms.append((name, value, unit, dict(attrs or {})))


# ---------------------------------------------------------------------------
# kirocrew.mcp.warm_pool.acquire — prewarm.HotKeyStore.record_outcome
# ---------------------------------------------------------------------------


class TestWarmPoolAcquireCounter:
    """record_outcome emits a hit/miss counter (drives production)."""

    def _make_store(self, tmp_path):
        from kiro_crew.mcp_gateway.prewarm import HotKeyStore

        return HotKeyStore(tmp_path / "hot-keys.json")

    def test_hit_emits_counter(self, tmp_path):
        store = self._make_store(tmp_path)
        rec = _CapturingRecorder()
        # prewarm imports get_recorder at module top-level, so patch the
        # consumer binding, not the definition module.
        with patch("kiro_crew.mcp_gateway.prewarm.get_recorder", return_value=rec):
            store.record_outcome(hit=True)
        assert rec.counters == [("kirocrew.mcp.warm_pool.acquire", {"result": "hit"})]

    def test_miss_emits_counter(self, tmp_path):
        store = self._make_store(tmp_path)
        rec = _CapturingRecorder()
        with patch("kiro_crew.mcp_gateway.prewarm.get_recorder", return_value=rec):
            store.record_outcome(hit=False)
        assert rec.counters == [("kirocrew.mcp.warm_pool.acquire", {"result": "miss"})]

    def test_multiple_calls_accumulate(self, tmp_path):
        store = self._make_store(tmp_path)
        rec = _CapturingRecorder()
        with patch("kiro_crew.mcp_gateway.prewarm.get_recorder", return_value=rec):
            store.record_outcome(hit=True)
            store.record_outcome(hit=False)
            store.record_outcome(hit=True)
        assert [attrs["result"] for _, attrs in rec.counters] == ["hit", "miss", "hit"]


# ---------------------------------------------------------------------------
# kirocrew.mcp.backend.acquire.duration — gatewayd._emit_backend_acquire_metric
# ---------------------------------------------------------------------------


class TestBackendAcquireMetric:
    """The ensure_backend path emits acquire duration via this helper."""

    def test_warm_hit(self):
        from kiro_crew.mcp_gateway.gatewayd import _emit_backend_acquire_metric

        rec = _CapturingRecorder()
        with patch("kiro_crew.mcp_gateway.gatewayd.get_recorder", return_value=rec):
            _emit_backend_acquire_metric(12.5, warm=True)
        assert rec.histograms == [
            ("kirocrew.mcp.backend.acquire.duration", 12.5, "ms", {"warm": True})
        ]

    def test_cold_spawn(self):
        from kiro_crew.mcp_gateway.gatewayd import _emit_backend_acquire_metric

        rec = _CapturingRecorder()
        with patch("kiro_crew.mcp_gateway.gatewayd.get_recorder", return_value=rec):
            _emit_backend_acquire_metric(340.0, warm=False)
        assert rec.histograms == [
            ("kirocrew.mcp.backend.acquire.duration", 340.0, "ms", {"warm": False})
        ]


# ---------------------------------------------------------------------------
# kirocrew.mcp.lazy_load.{count,duration} — gatewayd._emit_lazy_load_metrics
# ---------------------------------------------------------------------------


class TestLazyLoadMetrics:
    """The lazy-spawn path emits count + duration + acquire via this helper."""

    def test_emits_count_duration_and_acquire(self):
        from kiro_crew.mcp_gateway.gatewayd import _emit_lazy_load_metrics

        rec = _CapturingRecorder()
        with patch("kiro_crew.mcp_gateway.gatewayd.get_recorder", return_value=rec):
            _emit_lazy_load_metrics(250.0, warm=False)

        # Counter: lazy_load.count
        assert rec.counters == [("kirocrew.mcp.lazy_load.count", {"transport": "stdio"})]

        # Histograms: lazy_load.duration + backend.acquire.duration
        by_name = {h[0]: h for h in rec.histograms}
        assert set(by_name) == {
            "kirocrew.mcp.lazy_load.duration",
            "kirocrew.mcp.backend.acquire.duration",
        }
        assert by_name["kirocrew.mcp.lazy_load.duration"] == (
            "kirocrew.mcp.lazy_load.duration", 250.0, "ms", {"transport": "stdio"},
        )
        assert by_name["kirocrew.mcp.backend.acquire.duration"] == (
            "kirocrew.mcp.backend.acquire.duration", 250.0, "ms", {"warm": False},
        )
