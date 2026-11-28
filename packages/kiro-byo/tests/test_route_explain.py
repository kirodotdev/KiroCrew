"""S2: Tiga stays task-/pool-driven; completed records explain the choice."""
from __future__ import annotations

import time
from dataclasses import replace

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import route_diagnostics, route_model
from kiro_byo.routing_dashboard import build_routing_dashboard
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore


def _config() -> KiroByoConfig:
    # Four configured models, two legitimately fast candidates. Sky should not
    # be rewritten into a hard-coded single-model switch.
    models = (
        ModelSpec("p", "P", "quick-a", "Quick A", routing_tiers=("fast",),
                  routing_quality=.72, routing_speed=.99, routing_economy=.90,
                  tool_use=False, input_modalities=("text",)),
        ModelSpec("p", "P", "quick-b", "Quick B", routing_tiers=("fast",),
                  routing_quality=.69, routing_speed=.94, routing_economy=.87,
                  tool_use=True, input_modalities=("text", "image")),
        ModelSpec("p", "P", "strong-a", "Strong A", routing_tiers=("strong",),
                  routing_quality=.97, routing_speed=.50, routing_economy=.35,
                  tool_use=True, input_modalities=("text",)),
        ModelSpec("p", "P", "strong-b", "Strong B", routing_tiers=("strong",),
                  routing_quality=.94, routing_speed=.53, routing_economy=.41,
                  tool_use=True, input_modalities=("text", "image")),
    )
    return KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", models),), mode_pack="tiga", mode_type="sky")


def _route(policy: str, *, image: bool = False, tools: bool = False):
    decision = route_model(_config(), pack="tiga", policy=policy,
        prompt="请解释这个函数", history=[], has_image=image,
        tools_required=tools, tools_available=True)
    assert decision is not None
    return decision


def test_sky_chooses_another_fast_model_when_task_requires_tools() -> None:
    ordinary = _route("sky")
    with_tools = _route("sky", tools=True)
    assert ordinary.model.model_id == "quick-a"
    assert with_tools.model.model_id == "quick-b"
    assert with_tools.policy == "sky"
    d = route_diagnostics(with_tools)
    assert d["eligible_models"] == 3  # quick-a lacks tools
    assert d["in_tier_models"] == 1
    assert any(x["model"] == "quick-a" and "tool" in x["reason"] for x in d["excluded_models"])


def test_power_keeps_strong_pool_but_can_change_by_task_capability() -> None:
    ordinary = _route("power")
    with_image = _route("power", image=True)
    assert ordinary.model.model_id == "strong-a"
    assert with_image.model.model_id == "strong-b"
    d = route_diagnostics(with_image)
    assert d["policy"] == "power"
    assert d["primary_tier"] == "strong"
    assert d["in_tier_models"] == 1


def test_route_diagnostics_distinguish_strategy_pin_and_actual_fallback() -> None:
    decision = _route("sky")
    pin = replace(decision, provider=decision.candidates[1].provider,
                  model=decision.candidates[1].model, selection_source="sticky_pin")
    assert route_diagnostics(pin)["selection_source"] == "sticky_pin"
    fallback = route_diagnostics(pin, served_provider="p", served_model="strong-b")
    assert fallback["selection_source"] == "provider_fallback"
    assert fallback["policy"] == "sky"  # type never changes just to describe a fallback


def test_completed_route_keeps_explanation_even_without_runtime_trace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    decision = _route("sky", tools=True)
    record = TurnTelemetry(
        timestamp=time.time(), session_id="s", outcome="success",
        requested_model="kiro-byo-type:tiga:sky", provider_id="p", served_model="quick-b",
        effort="", total_ms=200, first_visible_ms=100, upstream_ms=190,
        tool_ms=0, mcp_wait_ms=0, tool_calls=0, native_calls=0,
        tool_search_calls=0, active_tools=0, tool_schema_bytes=0,
        stream_chunks=2, stream_chars=30,
        mode_pack="tiga", mode_type="sky", task_profile="tool",
        route_signals={"complexity": .25}, route_diagnostics=route_diagnostics(decision),
    )
    store = TurnTelemetryStore()
    store.record(record)
    dashboard = build_routing_dashboard(_config(), telemetry=store)
    recent = dashboard["recent_routes"][0]
    assert recent["policy"] == "sky"
    assert recent["model"] == "quick-b"
    assert recent["diagnostics"]["in_tier_models"] == 1
    assert recent["diagnostics"]["excluded_models"]
    assert recent["reasons"]
