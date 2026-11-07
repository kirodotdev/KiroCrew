"""Retention-coupled route evidence and honest measured performance summaries."""
from __future__ import annotations

import time

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.routing_dashboard import build_routing_dashboard
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore
from kiro_byo.policy_packs import get_policy_pack


def _record(timestamp, **changes):
    values = dict(
        timestamp=timestamp, session_id="s", outcome="success", requested_model="kiro-byo-type:tiga:sky",
        provider_id="p", served_model="flash", effort="", total_ms=1200, first_visible_ms=200,
        upstream_ms=700, tool_ms=400, mcp_wait_ms=0, tool_calls=1, native_calls=0,
        tool_search_calls=0, active_tools=2, tool_schema_bytes=0, stream_chunks=1, stream_chars=10,
        mode_pack="tiga", mode_type="sky", task_profile="casual",
        route_signals={"complexity": 0.21, "risk": 0.08, "latency_sensitivity": 0.91},
    )
    values.update(changes)
    return TurnTelemetry(**values)


def _cfg():
    model = ModelSpec("p", "P", "flash", "Flash", routing_tiers=("fast",))
    return KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "x", "", (model,)),), mode_type="sky")


def test_task_features_follow_requests_not_orphan_trace(tmp_path):
    telemetry = TurnTelemetryStore(tmp_path / "turns.jsonl")
    trace = RuntimeTraceStore(tmp_path / "events.jsonl")
    trace.record("model.route.selected", data={"pack": "tiga", "policy": "sky", "signals": {"complexity": 0.99}})
    assert not build_routing_dashboard(_cfg(), telemetry=telemetry, trace=trace)["recent_routes"]
    telemetry.record(_record(time.time()))
    result = build_routing_dashboard(_cfg(), telemetry=telemetry, trace=trace)
    assert result["policies"][0]["last_route"]["signals"]["complexity"] == 0.21
    # Trace may disappear independently without erasing retained completed-turn features.
    trace.clear()
    assert build_routing_dashboard(_cfg(), telemetry=telemetry, trace=trace)["recent_routes"]
    telemetry.clear()
    assert not build_routing_dashboard(_cfg(), telemetry=telemetry, trace=trace)["recent_routes"]


def test_feature_vector_ages_out_with_bounded_telemetry(tmp_path):
    telemetry = TurnTelemetryStore(tmp_path / "turns.jsonl", max_records=20)
    for i in range(21):
        telemetry.record(_record(time.time() + i, session_id=f"s-{i}", route_signals={"complexity": i / 21}))
    assert len(telemetry.recent(500)) == 20
    assert telemetry.recent(500)[0]["session_id"] == "s-1"
    telemetry.clear()
    assert telemetry.summary()["last24"]["requests"] == 0
    assert all(b["requests"] == 0 for b in telemetry.summary()["hourly"])


def test_sky_policy_prioritizes_speed_without_disabling_safety():
    pack = get_policy_pack("tiga")
    sky = pack.policy("sky").execution
    multi = pack.policy("multi").execution
    assert sky.routing.speed > multi.routing.speed
    assert sky.routing.speed > sky.routing.economy > sky.routing.quality
    assert sky.routing.tier_pool == "fast"
    assert sky.secondary is not None  # high risk must still permit verification
