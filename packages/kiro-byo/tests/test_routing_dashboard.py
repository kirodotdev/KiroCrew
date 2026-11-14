from __future__ import annotations

import time

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.routing_dashboard import build_routing_dashboard
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore


def _cfg() -> KiroByoConfig:
    model = ModelSpec(
        "p", "P", "flash", "Flash", routing_quality=.72, routing_speed=.88,
        routing_economy=.86, routing_tiers=("fast", "balanced"), tool_use=True,
    )
    return KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "x", "", (model,)),), mode_pack="tiga", mode_type="multi")


def test_routing_dashboard_is_read_only_observability(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    telemetry = TurnTelemetryStore()
    now = time.time()
    telemetry.record(TurnTelemetry(
        timestamp=now, session_id="s", outcome="success", requested_model="kiro-byo-type:tiga:multi",
        provider_id="p", served_model="flash", effort="low", total_ms=2100, first_visible_ms=350,
        upstream_ms=2100, tool_ms=0, mcp_wait_ms=0, tool_calls=0, native_calls=0,
        tool_search_calls=0, active_tools=0, tool_schema_bytes=0, stream_chunks=2, stream_chars=20,
        mode_pack="tiga", mode_type="multi", task_profile="casual", routing_score=.82, routing_confidence="low",
        route_signals={
            "complexity": .2, "breadth": .7, "parallelizability": .6,
            "risk": .3, "tool_intensity": .4, "write_intensity": .1,
            "context_demand": .5, "verification_value": .8,
            "latency_sensitivity": .9,
        },
    ))
    telemetry.record(TurnTelemetry(
        timestamp=now - 61 * 3600, session_id="old", outcome="error", requested_model="kiro-byo-type:tiga:multi",
        provider_id="p", served_model="flash", effort="low", total_ms=5000, first_visible_ms=900,
        upstream_ms=5000, tool_ms=0, mcp_wait_ms=0, tool_calls=0, native_calls=0,
        tool_search_calls=0, active_tools=0, tool_schema_bytes=0, stream_chunks=1, stream_chars=0,
        mode_pack="tiga", mode_type="multi", task_profile="casual", routing_score=.5, routing_confidence="low",
    ))
    trace = RuntimeTraceStore()
    trace.record("model.route.selected", session_id="s", data={
        "pack": "tiga", "policy": "multi", "policy_label": "Multi Type",
        "provider_id": "p", "resolved_model": "flash", "resolved_model_name": "Flash",
        "signals": {
            "task_profile": "casual", "complexity": .2, "breadth": .7,
            "parallelizability": .6, "risk": .3, "tool_intensity": .4,
            "write_intensity": .1, "context_demand": .5,
            "verification_value": .8, "latency_sensitivity": .9,
        },
        "reasons": ["Tiga / Multi Type policy"],
        "candidates": [{"provider_id": "p", "model": "flash", "score": .82, "runtime_confidence": "low"}],
    })
    payload = build_routing_dashboard(_cfg(), telemetry=telemetry, trace=trace)
    assert payload["adaptive"]["router_model"] is False
    assert payload["adaptive"]["learned_turns"] == 2
    assert payload["policies"]
    multi = next(row for row in payload["policies"] if row["id"] == "multi")
    assert multi["turns"] == 2
    assert multi["weights"] == {"quality": .5, "speed": .25, "economy": .25}
    assert multi["max_parallel"] == 2
    assert multi["worker_mode"] == "adaptive"
    assert multi["single_writer"] is True
    assert multi["secondary_enabled"] is True
    assert multi["recent_usage"]["window_hours"] == 60
    assert multi["recent_usage"]["turns"] == 1
    assert multi["recent_usage"]["success"] == 1
    assert multi["recent_usage"]["failed"] == 0
    assert multi["recent_usage"]["models"][0]["name"] == "Flash"
    profile = payload["models"][0]
    assert profile["static"]["quality"] == .72
    assert profile["samples"] == 2
    route = payload["recent_routes"][0]
    assert route["model"] == "flash"
    assert route["complexity"] == .2
    assert route["signals"] == {
        "complexity": .2,
        "breadth": .7,
        "parallelizability": .6,
        "risk": .3,
        "tool_intensity": .4,
        "write_intensity": .1,
        "context_demand": .5,
        "verification_value": .8,
        "latency_sensitivity": .9,
    }
