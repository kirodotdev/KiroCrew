from __future__ import annotations

import pytest
import time

from kiro_byo.adaptive_routing import blend_speed, snapshot_from_rows
from kiro_byo.mode_router import task_profile_for_signals
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import build_signals, route_model
from kiro_byo.policy_packs import AdaptiveRoutingProfile
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore


def _turn(provider: str, model: str, *, total: float, ttft: float, outcome: str = "success", task: str = "casual") -> TurnTelemetry:
    return TurnTelemetry(
        timestamp=time.time(), session_id=f"{provider}-{model}-{time.time_ns()}", outcome=outcome,
        requested_model="kiro-byo-type:tiga:sky", provider_id=provider, served_model=model,
        effort="", total_ms=total, first_visible_ms=ttft, upstream_ms=total,
        tool_ms=0, mcp_wait_ms=0, tool_calls=0, native_calls=0, tool_search_calls=0,
        active_tools=0, tool_schema_bytes=0, stream_chunks=1, stream_chars=10,
        mode_pack="tiga", mode_type="sky", task_profile=task,
    )


def _cfg() -> KiroByoConfig:
    a = ModelSpec(
        "p", "P", "a", "Alpha", routing_quality=.70, routing_speed=.82, routing_economy=.80,
        routing_tiers=("fast", "strong"), tool_use=True,
    )
    b = ModelSpec(
        "p", "P", "b", "Beta", routing_quality=.70, routing_speed=.78, routing_economy=.80,
        routing_tiers=("fast", "strong"), tool_use=True,
    )
    return KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "x", "", (a, b)),))


def test_task_profile_is_deterministic_without_router_model() -> None:
    code = build_signals("请分析并修复这段 Python code", [], has_image=False, tools_available=False, tools_required=False)
    vision = build_signals("看看这张图", [], has_image=True, tools_available=False, tools_required=False)
    tool = build_signals("打开这个文件", [], has_image=False, tools_available=True, tools_required=True)
    assert task_profile_for_signals(code) == "code"
    assert task_profile_for_signals(vision) == "vision"
    assert task_profile_for_signals(tool) == "tool"


def test_snapshot_uses_recent_latency_and_reliability() -> None:
    rows = []
    for _ in range(20):
        rows.append(_turn("p", "a", total=9000, ttft=1800).__dict__)
        rows.append(_turn("p", "b", total=2500, ttft=450).__dict__)
    snap = snapshot_from_rows(rows, task_profile="casual")
    a = snap.for_model("p", "a")
    b = snap.for_model("p", "b")
    assert b.runtime_speed is not None and a.runtime_speed is not None
    assert b.runtime_speed > a.runtime_speed
    assert b.confidence == "medium"
    assert b.reliability > .90


def test_runtime_evidence_shape_is_policy_data_not_hidden_router_constants() -> None:
    rows = []
    for _ in range(12):
        rows.append(_turn("p", "a", total=9000, ttft=1800).__dict__)
        rows.append(_turn("p", "b", total=2500, ttft=450).__dict__)
    conservative = AdaptiveRoutingProfile(
        confidence_medium=20, confidence_high=40,
        runtime_speed_floor=.45, runtime_speed_span=.20,
        sample_full=100, speed_blend_ceiling=.30,
    )
    snap = snapshot_from_rows(rows, task_profile="casual", profile=conservative)
    a = snap.for_model("p", "a")
    b = snap.for_model("p", "b")
    assert a.confidence == b.confidence == "low"
    assert a.runtime_speed == pytest.approx(.45)
    assert b.runtime_speed == pytest.approx(.65)
    # With a 100-sample saturation target and a 0.30 ceiling, twelve samples
    # only move the static prior a little. The behaviour comes from policy data.
    effective = blend_speed(.80, b, max_alpha=1.0, profile=conservative)
    assert .80 > effective > .79


def test_sky_runtime_latency_can_override_small_static_speed_gap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    store = TurnTelemetryStore()
    for _ in range(28):
        store.record(_turn("p", "a", total=9500, ttft=2100))
        store.record(_turn("p", "b", total=2200, ttft=350))
    decision = route_model(_cfg(), policy="sky", prompt="hello", history=[], has_image=False, tools_required=False, tools_available=False)
    assert decision is not None
    assert decision.model.model_id == "b"
    row = next(row for row in decision.candidates if row.model.model_id == "b")
    assert row.runtime_samples >= 20
    assert row.speed > row.static_speed


def test_power_reliability_is_dynamic_but_does_not_rewrite_quality_prior(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    store = TurnTelemetryStore()
    for i in range(30):
        store.record(_turn("p", "a", total=3000, ttft=500, outcome="error" if i < 14 else "success", task="research"))
        store.record(_turn("p", "b", total=3200, ttft=550, outcome="success", task="research"))
    decision = route_model(
        _cfg(), policy="power", prompt="Analyze this architecture carefully", history=[],
        has_image=False, tools_required=False, tools_available=False,
    )
    assert decision is not None
    assert decision.model.model_id == "b"
    a = next(row for row in decision.candidates if row.model.model_id == "a")
    b = next(row for row in decision.candidates if row.model.model_id == "b")
    # Static quality remains the catalog fact; telemetry contributes reliability
    # as a separate bounded signal instead of pretending to measure intelligence.
    assert a.quality == b.quality == .70
    assert b.reliability > a.reliability


def test_recoverable_fallback_failure_is_reliability_evidence(tmp_path, monkeypatch) -> None:
    from kiro_byo.adaptive_routing import current_snapshot
    from kiro_byo.runtime_trace import RuntimeTraceStore

    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    store = TurnTelemetryStore()
    for _ in range(10):
        store.record(_turn("p", "a", total=2500, ttft=400))
        store.record(_turn("p", "b", total=2500, ttft=400))
    trace = RuntimeTraceStore()
    for _ in range(6):
        trace.record("model.route.fallback", data={
            "from_provider": "p", "from_model": "a",
            "to_provider": "p", "to_model": "b", "error": "ProviderError",
        })
    snap = current_snapshot(task_profile="casual")
    assert snap.for_model("p", "a").reliability < snap.for_model("p", "b").reliability
