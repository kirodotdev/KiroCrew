"""S4-01: mode registry compatibility, fail-closed activation and hot reload."""
from __future__ import annotations

import json

import pytest

from kiro_byo.mode_registry import MODE_PLUGIN_API_VERSION, load_mode_registry
from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import (
    PolicyPackError, default_pack, get_policy_pack, load_policy_packs,
    public_policy_packs, resolve_policy,
)
from kiro_byo.mode_router import route_model
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec


def test_mode_registry_keeps_stable_builtin_identifiers():
    registry = load_mode_registry()
    assert MODE_PLUGIN_API_VERSION == 1
    tiga = registry.get("TIGA")
    assert tiga is not None and tiga.enabled
    assert tiga.type_ids == ("sky", "multi", "power", "glitter")
    assert registry.default().id == default_pack().id == "tiga"
    assert tiga.pack == get_policy_pack("tiga")
    assert registry.packs == load_policy_packs()
    assert tiga.resolve_type("multi") == resolve_policy("tiga", "multi")[1]


def test_dyna_registered_executable_in_product_order_with_flash_as_default():
    registry = load_mode_registry()
    dyna = registry.get("dyna")
    assert dyna is not None and dyna.enabled
    assert dyna.type_ids == ("miracle", "flash", "strong")
    assert dyna.pack.default_policy == "flash"
    assert registry.default().id == "tiga"
    for candidate in dyna.type_ids:
        assert dyna.resolve_type(candidate).id == candidate
        assert registry.resolve("dyna", candidate)[1].id == candidate
    public = public_policy_packs()
    assert public["mode_registry_api_version"] == 1
    assert public["mode_plugin_kind"] == "data-only-policy-pack"
    row = next(item for item in public["packs"] if item["id"] == "dyna")
    assert row["enabled"] is True and row["status"] == "available"


def test_unknown_mode_type_and_uninstalled_mode_fail_closed():
    registry = load_mode_registry()
    assert registry.get("unknown") is None
    with pytest.raises(PolicyPackError, match="not installed"):
        registry.resolve("unknown", "multi")
    with pytest.raises(PolicyPackError, match="not present"):
        registry.resolve("tiga", "unavailable")
    with pytest.raises(PolicyPackError, match="not present"):
        resolve_policy("tiga", "unavailable")


def test_registry_reflects_data_only_plugin_hot_reload(tmp_path):
    raw = {
        "schema_version": 4, "id": "aurora", "display_name": "Aurora",
        "default_policy": "flash", "policies": [
            {"id": "flash", "execution": {"routing": {
                "quality": 0.2, "speed": 0.7, "economy": 0.1,
            }}, "semantics": {"display_name": "Flash"}},
        ],
    }
    with use_app_home(tmp_path):
        directory = tmp_path / "policy_packs"
        directory.mkdir()
        custom_path = directory / "aurora.json"
        custom_path.write_text(json.dumps(raw), encoding="utf-8")
        first = load_mode_registry()
        assert first.get("aurora").type_ids == ("flash",)
        assert first.resolve("aurora", "flash")[1].routing.speed == pytest.approx(0.7)
        raw["policies"][0]["execution"]["routing"]["speed"] = 0.8
        custom_path.write_text(json.dumps(raw), encoding="utf-8")
        assert load_mode_registry().resolve("aurora", "flash")[1].routing.speed == pytest.approx(0.8)
        assert first.resolve("aurora", "flash")[1].routing.speed == pytest.approx(0.7)
        custom_path.unlink()
        assert load_mode_registry().get("aurora") is None


def test_dyna_id_remains_reserved_against_user_override(tmp_path):
    with use_app_home(tmp_path):
        directory = tmp_path / "policy_packs"
        directory.mkdir()
        (directory / "dyna.json").write_text(json.dumps({
            "schema_version": 1, "id": "dyna", "enabled": True,
            "display_name": "Override", "default_policy": "flash",
            "policies": [{"id": "flash", "routing": {
                "quality": 1, "speed": 0, "economy": 0,
            }}],
        }), encoding="utf-8")
        registry = load_mode_registry()
        assert registry.get("dyna").enabled is True
        assert any("reserved" in e["error"] for e in registry.errors)


def test_existing_type_routing_stays_configurable_without_extra_decision_model():
    models = (
        ModelSpec(provider_id="demo", provider_name="Demo", model_id="fast", name="Fast",
                  routing_quality=0.5, routing_speed=0.99, routing_economy=0.95, tool_use=True),
        ModelSpec(provider_id="demo", provider_name="Demo", model_id="strong", name="Strong",
                  routing_quality=0.97, routing_speed=0.35, routing_economy=0.25, tool_use=True),
    )
    provider = ProviderSpec("demo", "Demo", "openai-compatible", "x", "", models)
    for type_id in ("sky", "multi", "power", "glitter"):
        config = KiroByoConfig((provider,), mode_pack="tiga", mode_type=type_id)
        decision = route_model(config, prompt="Summarize the notes", history=[],
                               has_image=False, tools_expected=False)
        assert decision is not None
        assert decision.pack == "tiga"
        assert decision.policy == type_id
        assert decision.model.model_id in {"fast", "strong"}
