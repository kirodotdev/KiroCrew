"""Tests for kiro_crew.metrics.recorder — the MetricsRecorder facade."""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kiro_crew.metrics.recorder import MetricsRecorder


def _recorder_with_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return MetricsRecorder(provider.get_meter("test")), reader


def _data_points(reader):
    data = reader.get_metrics_data()
    points = []
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for dp in metric.data.data_points:
                    points.append((metric.name, dp))
    return points


class TestNoOpRecorder:
    def test_disabled_recorder_is_noop(self):
        rec = MetricsRecorder(None)
        assert rec.enabled is False
        # Must not raise even though there is no underlying meter.
        rec.counter("kirocrew.x")
        rec.histogram("kirocrew.y", 1.0)
        rec.up_down_counter("kirocrew.z", 1)


class TestRecord:
    def test_counter_recorded(self):
        rec, reader = _recorder_with_reader()
        rec.counter("kirocrew.test.count", 3, attrs={"ok": True})
        names = [n for n, _ in _data_points(reader)]
        assert "kirocrew.test.count" in names

    def test_histogram_recorded(self):
        rec, reader = _recorder_with_reader()
        rec.histogram("kirocrew.test.dur", 12.5, unit="ms")
        names = [n for n, _ in _data_points(reader)]
        assert "kirocrew.test.dur" in names

    def test_session_startup_histogram_contract(self):
        """The exact call ensure_ready() makes must record cleanly."""
        rec, reader = _recorder_with_reader()
        rec.histogram(
            "kirocrew.session.startup.duration",
            1234.0,
            unit="ms",
            attrs={"outcome": "ready", "spawned": True},
        )
        names = [n for n, _ in _data_points(reader)]
        assert "kirocrew.session.startup.duration" in names


class TestGuardrails:
    def test_app_cannot_spoof_core_namespace(self):
        """validate_name raises on spoof; recorder swallows -> nothing recorded."""
        rec, reader = _recorder_with_reader()
        rec.counter("kirocrew.evil", app_id="my_app")  # invalid namespace
        assert _data_points(reader) == []

    def test_app_valid_namespace_recorded(self):
        rec, reader = _recorder_with_reader()
        rec.counter("app.my_app.requests", 1, app_id="my_app")
        names = [n for n, _ in _data_points(reader)]
        assert "app.my_app.requests" in names

    def test_credential_attr_redacted(self):
        rec, reader = _recorder_with_reader()
        rec.counter(
            "kirocrew.test.count",
            1,
            attrs={"api_key": "AKIAIOSFODNN7EXAMPLE1", "safe": "ok"},
        )
        points = _data_points(reader)
        assert points, "metric should have recorded"
        _, dp = points[0]
        assert dp.attributes.get("api_key") == "[REDACTED]"
        assert dp.attributes.get("safe") == "ok"
