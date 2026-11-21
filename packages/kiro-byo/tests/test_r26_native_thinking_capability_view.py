"""R26: official native Claude capability migration reaches every UI/runtime reader.

Saved JSON is intentionally not mutated by read paths. A relay with a Claude-
shaped model ID must keep its own declared contract and thinking controls.
"""
from __future__ import annotations

import json

import pytest

from kiro_byo.acp_server import AcpServer
from kiro_byo.config import _load_provider, load_config
from kiro_byo.credential_store import MemoryCredentialStore
from kiro_byo.discovery import DiscoveredModel, profile_official_discovered_models
from kiro_byo.paths import use_app_home
from kiro_byo.provider_admin import list_providers, set_model_thinking_preference
from kiro_byo.provider_catalog import model_capability_from_row


def stale_model(model_id: str = "claude-opus-5-5") -> dict:
    return {
        "id": model_id, "name": model_id,
        "reasoning": {
            "mode": "levels", "supported": False, "can_disable": True,
            "default_enabled": False, "disable_value": "disabled",
            "enable_value": "enabled", "auto_value": "",
        },
        "effort_supported_levels": ["low", "medium", "high", "xhigh", "max"],
        "effort_default_level": "medium",
    }


def provider(model_id: str = "claude-opus-5-5", *, native: bool = True) -> dict:
    return {
        "id": "anth", "name": "Anthropic", "preset_id": "anthropic" if native else "custom",
        "base_url": "https://api.anthropic.com/v1" if native else "https://relay.example/v1",
        "type": "openai-compatible",
        "models": [stale_model(model_id)], "default_model": model_id,
    }


def test_native_reader_restores_fixed_on_from_stale_profile() -> None:
    native = _load_provider(provider())
    model = native.models[0]
    assert native.type == "anthropic-messages"
    assert model.reasoning_supported is True
    assert model.reasoning_can_disable is False
    assert model.reasoning_enable_value == "adaptive"
    assert model.reasoning_disable_value == ""
    assert AcpServer._thinking_choices(model) == ()
    assert model.selectable_effort_levels == ("low", "medium", "high", "xhigh", "max")


def test_sonnet_5_5_off_wire_uses_between_tools_and_ui_can_offer_off() -> None:
    model = _load_provider(provider("claude-sonnet-5-5")).models[0]
    assert model.reasoning_supported is True
    assert model.reasoning_can_disable is True
    assert model.reasoning_disable_value == "between_tools"
    assert model.reasoning_enable_value == "adaptive"
    assert "off" in AcpServer._thinking_choices(model)


def test_legacy_4_5_model_restores_manual_mode() -> None:
    model = _load_provider(provider("claude-opus-4-5")).models[0]
    assert model.reasoning_enable_value == "enabled"
    assert model.reasoning_auto_value == ""
    assert model.reasoning_can_disable is True


def test_official_capability_migration_uses_invocation_target() -> None:
    row = stale_model("catalog-alias")
    row["invocation_target"] = "claude-opus-5-5"
    fixed = model_capability_from_row(row, native_anthropic=True)["reasoning"]["thinking"]
    assert fixed["supported"] is True and fixed["can_disable"] is False
    # A custom relay may deliberately attach different semantics to that same ID.
    custom = model_capability_from_row(row)["reasoning"]["thinking"]
    assert custom["supported"] is False and custom["enable_value"] == "enabled"


def test_model_specific_chat_transport_override_does_not_get_native_fix() -> None:
    item = provider()
    item["models"][0]["transport"] = "openai-chat-completions"
    model = _load_provider(item).models[0]
    assert model.reasoning_supported is False
    assert model.reasoning_enable_value == "enabled"


def test_custom_endpoint_is_not_assumed_native_anthropic() -> None:
    model = _load_provider(provider(native=False)).models[0]
    assert model.reasoning_supported is False
    assert model.reasoning_supported is False
    assert model.reasoning_enable_value == "enabled"


def test_discovery_uses_same_native_migration_for_advertised_catalogue() -> None:
    old = DiscoveredModel(
        "claude-opus-5-5", "Claude Opus 5.5",
        reasoning_supported=False, reasoning_can_disable=True,
        reasoning_enable_value="enabled", reasoning_disable_value="disabled",
    )
    updated = profile_official_discovered_models("anthropic", [old])[0]
    assert updated.reasoning_supported is True
    assert updated.reasoning_can_disable is False
    assert updated.reasoning_enable_value == "adaptive"
    # Other providers' Claude-shaped aliases are not reinterpreted.
    other = profile_official_discovered_models("minimax", [old])[0]
    assert other.reasoning_supported is False
    assert other.reasoning_enable_value == "enabled"


def test_admin_and_runtime_agree_but_only_model_preference_is_persisted(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "providers.json"
    path.write_text(json.dumps({"providers": [provider()]}), encoding="utf-8")
    store = MemoryCredentialStore()
    with use_app_home(tmp_path):
        row = list_providers(store=store)[0]["models"][0]
        thinking = row["capabilities"]["reasoning"]["thinking"]
        assert thinking["can_disable"] is False
        assert thinking["enable_value"] == "adaptive"
        assert load_config(credential_store=store).providers[0].models[0].reasoning_can_disable is False
        with pytest.raises(Exception, match="does not support"):
            set_model_thinking_preference("anth", "claude-opus-5-5", "off", store=store)
        assert json.loads(path.read_text())["providers"][0]["models"][0] == stale_model()


def test_admin_chooses_native_preset_even_if_saved_transport_is_legacy(tmp_path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "providers.json").write_text(
        json.dumps({"providers": [provider("claude-sonnet-5-5")]}), encoding="utf-8"
    )
    with use_app_home(tmp_path):
        store = MemoryCredentialStore()
        row = list_providers(store=store)[0]["models"][0]
        assert row["capabilities"]["reasoning"]["thinking"]["disable_value"] == "between_tools"
        result = set_model_thinking_preference("anth", "claude-sonnet-5-5", "off", store=store)
        assert result["models"][0]["thinking_preference"] == "off"
        assert load_config(credential_store=store).providers[0].models[0].thinking_preference == "off"


def test_anthropic_legacy_interleaved_beta_uses_wire_id_not_catalog_alias() -> None:
    from kiro_byo.providers import anthropic_messages
    from kiro_byo.config import ModelSpec

    model = ModelSpec("anth", "Anthropic", "user-alias", "Claude",
                      invocation_target="claude-opus-4-5",
                      reasoning_supported=True, reasoning_enable_value="enabled")
    spec = _load_provider(provider("claude-opus-4-5"))
    headers = anthropic_messages._headers(
        spec, model, "on", True,
        wire_thinking={"type": "enabled", "budget_tokens": 4096},
        wire_model_id="claude-opus-4-5",
    )
    assert headers["anthropic-beta"] == "interleaved-thinking-2025-05-14"
