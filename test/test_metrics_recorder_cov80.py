"""The MetricsRecorder paths ``test/metrics/test_recorder.py`` leaves untouched.

Three of them, all guardrails rather than happy paths:

* the up/down counter — the only instrument kind with no coverage at all, and
  the one whose whole point is that a NEGATIVE delta is legal;
* the per-name instrument cache — a second emit on the same name must reuse the
  instrument, because the SDK warns on duplicate creation and the caches are
  what make that warning impossible;
* the swallow-everything contract — a telemetry failure (an invalid name, an
  instrument whose ``add``/``record`` raises) must be logged and dropped, never
  raised into the call site.
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kiro_crew.metrics.recorder import MetricsRecorder


def _recorder_with_reader() -> tuple[MetricsRecorder, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return MetricsRecorder(provider.get_meter("test")), reader


def _points(reader: InMemoryMetricReader) -> list[tuple[str, Any]]:
    data = reader.get_metrics_data()
    out: list[tuple[str, Any]] = []
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for dp in metric.data.data_points:
                    out.append((metric.name, dp))
    return out


class _BoomMeter:
    """A meter whose instruments raise on use — the "telemetry broke" case."""

    class _Boom:
        def add(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("instrument exploded")

        def record(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("instrument exploded")

    def create_counter(self, *_a: Any, **_kw: Any) -> "_BoomMeter._Boom":
        return self._Boom()

    def create_up_down_counter(self, *_a: Any, **_kw: Any) -> "_BoomMeter._Boom":
        return self._Boom()

    def create_histogram(self, *_a: Any, **_kw: Any) -> "_BoomMeter._Boom":
        return self._Boom()


class TestUpDownCounter:
    def test_up_down_counter_records_a_negative_delta(self) -> None:
        rec, reader = _recorder_with_reader()
        rec.up_down_counter("kirocrew.pool.live", 3, attrs={"kind": "backend"})
        rec.up_down_counter("kirocrew.pool.live", -2)

        matching = [dp for name, dp in _points(reader) if name == "kirocrew.pool.live"]
        assert matching, "the up/down counter must reach the reader"
        assert sum(dp.value for dp in matching) == 1

    def test_second_emit_reuses_the_cached_instrument(self) -> None:
        """The SDK warns on duplicate instrument creation, so the name cache must
        be consulted before ``create_up_down_counter`` is called again."""
        created: list[str] = []
        added: list[float] = []

        class _Inst:
            def add(self, value: float, attributes: Any = None) -> None:
                added.append(value)

        class CountingMeter:
            def create_up_down_counter(self, name: str, **_kw: Any) -> Any:
                created.append(name)
                return _Inst()

        rec = MetricsRecorder(CountingMeter())  # type: ignore[arg-type]
        rec.up_down_counter("kirocrew.pool.live", 1)
        rec.up_down_counter("kirocrew.pool.live", -1)
        assert created == ["kirocrew.pool.live"]
        assert added == [1, -1]

    def test_disabled_recorder_skips_the_up_down_path_entirely(self) -> None:
        rec = MetricsRecorder(None)
        rec.up_down_counter("kirocrew.pool.live", -1)
        assert rec.enabled is False


class TestFailuresAreSwallowed:
    def test_invalid_name_is_logged_not_raised(self, caplog: Any) -> None:
        rec, _reader = _recorder_with_reader()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.metrics.recorder"):
            rec.up_down_counter("Not A Metric Name", 1)
        assert any("up_down_counter" in r.message for r in caplog.records)

    def test_instrument_add_failure_is_logged_not_raised(self, caplog: Any) -> None:
        rec = MetricsRecorder(_BoomMeter())  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="kiro_crew.metrics.recorder"):
            rec.up_down_counter("kirocrew.pool.live", 1)
            rec.counter("kirocrew.pool.spawns")
            rec.histogram("kirocrew.pool.latency", 5.0)
        messages = [r.message for r in caplog.records]
        assert any("up_down_counter" in m for m in messages)
        assert any("counter" in m for m in messages)
        assert any("histogram" in m for m in messages)

    def test_app_metric_cannot_spoof_the_core_namespace(self, caplog: Any) -> None:
        """``app_id`` set + a ``kirocrew.`` name is a namespace violation, and the
        facade must drop it rather than emit it."""
        rec, reader = _recorder_with_reader()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.metrics.recorder"):
            rec.up_down_counter("kirocrew.core.thing", 1, app_id="some_app")
        assert not [n for n, _ in _points(reader) if n == "kirocrew.core.thing"]
