"""Tests for kiro_crew.metrics.local_exporter — JsonlMetricExporter."""

import json

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from kiro_crew.metrics.local_exporter import JsonlMetricExporter


def _provider(tmp_path):
    # Very long interval so the daemon thread never fires mid-test; we flush
    # explicitly via provider.force_flush().
    reader = PeriodicExportingMetricReader(
        JsonlMetricExporter(tmp_path), export_interval_millis=3_600_000.0
    )
    return MeterProvider(metric_readers=[reader])


def test_export_writes_valid_jsonl(tmp_path):
    provider = _provider(tmp_path)
    try:
        provider.get_meter("test").create_counter("kirocrew.test.count").add(
            5, attributes={"ok": "yes"}
        )
        provider.force_flush()

        files = list(tmp_path.glob("metrics-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert lines, "expected at least one JSON line"
        parsed = json.loads(lines[0])  # each line is one valid JSON object
        assert "resource_metrics" in parsed
        assert "kirocrew.test.count" in lines[0]
    finally:
        provider.shutdown()


def test_export_appends_one_line_per_flush(tmp_path):
    provider = _provider(tmp_path)
    try:
        counter = provider.get_meter("test").create_counter("kirocrew.test.count")
        counter.add(1)
        provider.force_flush()
        counter.add(1)
        provider.force_flush()

        files = list(tmp_path.glob("metrics-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
    finally:
        provider.shutdown()
