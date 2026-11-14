"""R24: distinguish same-name relay models and unrecognized upstream aliases."""
from __future__ import annotations

import time
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import route_diagnostics, route_model
from kiro_byo.routing_dashboard import build_routing_dashboard
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore


def _config() -> KiroByoConfig:
    providers = tuple(
        ProviderSpec(provider_id, provider_id.upper(), "openai-compatible", "https://example.invalid/v1", "", (
            ModelSpec(provider_id, provider_id.upper(), "shared-model", "Shared", routing_tiers=("balanced",)),
        ))
        for provider_id in ("official", "relay")
    )
    return KiroByoConfig(providers, mode_pack="tiga", mode_type="multi")


def _record(provider_id: str) -> TurnTelemetry:
    return TurnTelemetry(
        timestamp=time.time(), session_id=f"{provider_id}-s", outcome="success",
        requested_model="kiro-byo-type:tiga:multi", provider_id=provider_id,
        served_model="shared-model", effort="", total_ms=400,
        first_visible_ms=100, upstream_ms=400, tool_ms=0, mcp_wait_ms=0,
        tool_calls=0, native_calls=0, tool_search_calls=0,
        active_tools=0, tool_schema_bytes=0, stream_chunks=1, stream_chars=5,
        mode_pack="tiga", mode_type="multi", task_profile="casual",
    )


def test_same_model_id_across_providers_is_not_merged(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    store = TurnTelemetryStore()
    store.record(_record("official"))
    store.record(_record("relay"))
    payload = build_routing_dashboard(_config(), telemetry=store)
    multi = next(p for p in payload["policies"] if p["id"] == "multi")
    assert len(multi["top_models"]) == 2
    assert { (r["provider_id"], r["model"], r["turns"]) for r in multi["top_models"] } == {
        ("official", "shared-model", 1), ("relay", "shared-model", 1)
    }
    assert len(multi["recent_usage"]["models"]) == 2


def test_upstream_model_alias_is_marked_unknown_without_rewriting_truth(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    decision = route_model(
        _config(), pack="tiga", policy="multi", prompt="hello", history=[],
        has_image=False, tools_available=False, tools_required=False,
    )
    assert decision is not None
    normal = route_diagnostics(decision)
    assert normal["served_in_candidates"] is True
    changed = route_diagnostics(decision, served_provider=decision.provider.id, served_model="internal-upstream-alias")
    assert changed["selected_model"]["model"] == "shared-model"
    assert changed["served_model"]["model"] == "internal-upstream-alias"
    assert changed["served_in_candidates"] is False
    assert any("outside original candidate pool" in r for r in changed["reasons"])
    assert not any(row["served"] for row in changed["candidates"])
