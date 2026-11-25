"""Span shards the writer makes must be shards the trace reader can read."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kiro_crew.tracing import render
from kiro_crew.tracing.exporter import JsonlSpanExporter, _SpanShards

KEY = "shard-cap-turn"


@pytest.fixture(autouse=True)
def _traces_dir(tmp_path: Path, _floor_monkeypatch):
    """Point both the sink and the CLI reader at a throwaway traces folder."""
    _floor_monkeypatch.setattr("kiro_crew.tracing.provider.traces_dir", lambda: tmp_path)
    return tmp_path


def test_the_writer_and_reader_declare_one_shared_cap():
    assert getattr(_SpanShards, "MAX_SHARD_BYTES", None) == render.MAX_SHARD_BYTES


def test_the_writer_rotates_before_the_reader_cap_and_keeps_the_turn_findable(
    _traces_dir, _floor_monkeypatch
):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    cap = 4096
    _floor_monkeypatch.setattr(render, "MAX_SHARD_BYTES", cap)
    exporter = JsonlSpanExporter(_traces_dir, retention_days=0, max_total_mb=0)
    # The writer's cap is a class attribute on the fixed tree; on the base tree
    # setting it here is harmless, so the test fails on an assertion, not an error.
    setattr(exporter._shards, "MAX_SHARD_BYTES", cap)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    for index in range(40):
        tracer.start_span(f"step-{index}", attributes={"kirocrew.turn_id": KEY}).end()
    provider.shutdown()

    shards = sorted(_traces_dir.glob("spans-*.jsonl"))
    assert len(shards) > 1
    assert all(path.stat().st_size <= cap for path in shards)
    spans = render.read_spans(_traces_dir)
    assert len(spans) == 40
    assert all(KEY in span.keys for span in spans)
    assert render.pick_trace(spans, KEY)[1] == 39


def test_an_oversized_shard_is_an_error_not_a_no_match(_traces_dir, _floor_monkeypatch, capsys):
    _floor_monkeypatch.setattr(render, "MAX_SHARD_BYTES", 10)
    (_traces_dir / "spans-2026-11-14-100.jsonl").write_text("x" * 11, encoding="utf-8")

    with pytest.raises(SystemExit) as exited:
        render.trace_show(argparse.Namespace(key=KEY))

    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert captured.out == ""
    assert "Error: cannot read spans-2026-11-14-100.jsonl" in captured.err
