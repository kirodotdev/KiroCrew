from __future__ import annotations

import json
import os
import tempfile
from io import StringIO
from pathlib import Path

from kiro_byo import crew_shared_patch
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec, _load_provider
from kiro_byo.protocol import EFFORT_CONFIG_ID, THINKING_CONFIG_ID, type_selection_id
from kiro_byo.provider_catalog import merge_official_model_profile, official_model_profile


def _frames(out: StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_deepseek_v41_flash_official_alias_has_effort_contract() -> None:
    for model_id in ("deepseek-flash", "deepseek-v4.1-flash", "DeepSeek-V4.1-Flash"):
        profile = official_model_profile("deepseek", model_id)
        assert profile["effort_supported_levels"] == ["low", "high", "max"]
        assert profile["effort_default_level"] == "high"
        assert profile["reasoning"]["mode"] == "levels"


def test_official_profile_can_fill_from_display_name_when_wire_id_is_stable_or_opaque() -> None:
    row = merge_official_model_profile(
        "deepseek",
        {
            "id": "provider-row-41-flash",
            "name": "DeepSeek-V4.1-Flash",
            "catalog_source": "provider-discovery",
        },
    )
    assert row["id"] == "provider-row-41-flash"
    assert row["effort_supported_levels"] == ["low", "high", "max"]
    assert row["reasoning"]["mode"] == "levels"


def test_saved_official_deepseek_config_is_enriched_for_effort_without_rediscovery() -> None:
    raw = {
        "id": "deepseek",
        "preset_id": "deepseek",
        "name": "DeepSeek",
        "type": "deepseek-responses",
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "models": [{"id": "deepseek-v4.1-flash", "name": "DeepSeek-V4.1-Flash"}],
        "default_model": "deepseek-v4.1-flash",
    }
    provider = _load_provider(raw, allow_legacy_plaintext=True, resolve_credentials=False)
    model = provider.models[0]
    assert model.selectable_effort_levels == ("low", "high", "max")
    assert model.effort_default_level == "high"


def test_type_to_concrete_switch_pushes_effort_option_to_crew_but_never_thinking_toggle() -> None:
    flash = ModelSpec(
        provider_id="deepseek",
        provider_name="DeepSeek",
        model_id="deepseek-flash",
        name="DeepSeek-V4.1-Flash",
        effort_supported_levels=("low", "high", "max"),
        effort_default_level="high",
        reasoning_mode="levels",
        reasoning_supported=True,
        reasoning_can_disable=True,
        reasoning_default_enabled=True,
        reasoning_disable_value="none",
    )
    provider = ProviderSpec(
        id="deepseek",
        name="DeepSeek",
        type="deepseek-responses",
        base_url="https://api.deepseek.com",
        api_key="",
        models=(flash,),
        default_model="deepseek-flash",
        preset_id="deepseek",
    )
    cfg = KiroByoConfig(providers=(provider,), default_model="DeepSeek-V4.1-Flash")

    tmp = tempfile.TemporaryDirectory()
    old_home = os.environ.get("KIRO_BYO_HOME")
    os.environ["KIRO_BYO_HOME"] = tmp.name
    old_config = AcpServer._config
    AcpServer._config = staticmethod(lambda: cfg)
    try:
        out = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=out)
        session = Session("effort-switch", tmp.name, type_selection_id("tiga", "multi"))
        server._put_session(session)
        server._handle_session_set_model_request(
            7,
            {"sessionId": session.session_id, "modelId": "DeepSeek-V4.1-Flash"},
        )
        updates = [
            frame["params"]["update"]
            for frame in _frames(out)
            if frame.get("method") == "session/update"
            and frame.get("params", {}).get("update", {}).get("sessionUpdate") == "config_option_update"
        ]
        assert updates
        by_id = {item["id"]: item for item in updates[-1]["configOptions"]}
        assert EFFORT_CONFIG_ID in by_id
        assert THINKING_CONFIG_ID not in by_id
        assert [item["value"] for item in by_id[EFFORT_CONFIG_ID]["options"]] == ["low", "high", "max"]
        assert by_id[EFFORT_CONFIG_ID]["currentValue"] == "high"
        server._executor.shutdown(wait=True)
    finally:
        AcpServer._config = old_config
        if old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = old_home
        tmp.cleanup()



def test_binary_deep_thinking_model_stays_byo_owned_and_has_no_crew_effort_selector() -> None:
    mimo = ModelSpec(
        provider_id="xiaomi",
        provider_name="Xiaomi MiMo",
        model_id="mimo-v2.5",
        name="MiMo-V2.5",
        reasoning_mode="toggle",
        reasoning_supported=True,
        reasoning_can_disable=True,
        reasoning_default_enabled=True,
        reasoning_enable_value="enabled",
        reasoning_disable_value="disabled",
    )
    provider = ProviderSpec(
        id="xiaomi",
        name="Xiaomi MiMo",
        type="xiaomi-mimo-chat",
        base_url="https://api.xiaomimimo.com/v1",
        api_key="",
        models=(mimo,),
        default_model="mimo-v2.5",
        preset_id="xiaomi",
    )
    cfg = KiroByoConfig(providers=(provider,), default_model="MiMo-V2.5")
    session = Session("binary-thinking", ".", "MiMo-V2.5")
    options = AcpServer._config_options(session, cfg)
    ids = {item["id"] for item in options}
    assert EFFORT_CONFIG_ID not in ids
    assert THINKING_CONFIG_ID not in ids
    assert AcpServer._thinking_config(mimo, "auto") is not None


def test_effort_option_uses_standard_acp_thought_level_category() -> None:
    flash = ModelSpec(
        provider_id="deepseek",
        provider_name="DeepSeek",
        model_id="deepseek-v4.1-flash",
        name="DeepSeek-V4.1-Flash",
        effort_supported_levels=("low", "high", "max"),
        effort_default_level="high",
        reasoning_mode="levels",
        reasoning_supported=True,
        reasoning_can_disable=True,
        reasoning_default_enabled=True,
        reasoning_disable_value="none",
    )
    option = AcpServer._effort_config(flash, "high")
    assert option is not None
    assert option["id"] == EFFORT_CONFIG_ID
    assert option["category"] == "thought_level"
    assert option["currentValue"] == "high"
    assert [item["value"] for item in option["options"]] == ["low", "high", "max"]

def test_crew_patch_marks_byo_effort_as_advertised_session_option() -> None:
    import runpy

    helper_ns = runpy.run_path(str(Path(__file__).with_name("test_crew_shared_transform.py")))
    source = helper_ns["_minimal_backends_source"](dynamic_registry=True, zero_tool_capability=True)
    patched = crew_shared_patch.patch_text(
        "src/kiro_crew/agent_sdk/backends.py",
        source,
    )
    effort_members = patched.split("ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION =", 1)[1].split("\n", 1)[0]
    effort_ids = patched.split("EFFORT_CONFIG_OPTION_IDS =", 1)[1].split("}", 1)[0]
    assert "ACP_BACKEND_KIRO_BYO" in effort_members
    assert 'ACP_BACKEND_KIRO_BYO: "effortLevel"' in effort_ids


def test_concrete_switch_clears_incompatible_user_effort_and_full_update_omits_selector() -> None:
    flash = ModelSpec(
        provider_id="deepseek", provider_name="DeepSeek", model_id="deepseek-v4.1-flash", name="DeepSeek-V4.1-Flash",
        effort_supported_levels=("low", "high", "max"), effort_default_level="high",
        reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=True,
        reasoning_default_enabled=True, reasoning_disable_value="none",
    )
    mimo = ModelSpec(
        provider_id="xiaomi", provider_name="Xiaomi", model_id="mimo-v2.5", name="MiMo-V2.5",
        reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
        reasoning_default_enabled=True, reasoning_enable_value="enabled", reasoning_disable_value="disabled",
    )
    cfg = KiroByoConfig(
        providers=(
            ProviderSpec("deepseek", "DeepSeek", "deepseek-responses", "https://api.deepseek.com", "", (flash,), default_model=flash.model_id),
            ProviderSpec("xiaomi", "Xiaomi", "xiaomi-mimo-chat", "https://api.xiaomimimo.com/v1", "", (mimo,), default_model=mimo.model_id),
        ),
        default_model=flash.name,
    )
    out = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=out)
    try:
        session = Session("switch-capability", ".", flash.name, effort="high", effort_user_set=True)
        assert server._set_model(session, cfg, mimo.name)
        assert session.model == mimo.name
        assert session.effort == ""
        assert session.effort_user_set is False

        server._notify_config_update(session, cfg)
        updates = [
            frame["params"]["update"]
            for frame in _frames(out)
            if frame.get("method") == "session/update"
            and frame.get("params", {}).get("update", {}).get("sessionUpdate") == "config_option_update"
        ]
        assert updates
        ids = {item["id"] for item in updates[-1]["configOptions"]}
        assert EFFORT_CONFIG_ID not in ids
        assert THINKING_CONFIG_ID not in ids
    finally:
        server._executor.shutdown(wait=True)


def test_type_switch_clears_manual_effort_and_hides_type_effort_selector() -> None:
    low_high = ModelSpec(
        provider_id="p", provider_name="P", model_id="low-high", name="LowHigh",
        effort_supported_levels=("low", "high"), effort_default_level="low",
        reasoning_mode="levels", reasoning_supported=True,
    )
    max_model = ModelSpec(
        provider_id="p", provider_name="P", model_id="max", name="MaxModel",
        effort_supported_levels=("high", "max"), effort_default_level="high",
        reasoning_mode="levels", reasoning_supported=True,
    )
    cfg = KiroByoConfig(
        providers=(ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", (low_high, max_model)),),
        default_model=low_high.name,
        mode_pack="tiga",
    )
    server = AcpServer(stdin=StringIO(), stdout=StringIO())
    try:
        session = Session("type-effort", ".", low_high.name, effort="max", effort_user_set=True)
        assert server._set_model(session, cfg, type_selection_id("tiga", "power"))
        assert session.effort == ""
        assert session.effort_user_set is False
        ids = {item["id"] for item in server._config_options(session, cfg)}
        assert EFFORT_CONFIG_ID not in ids
    finally:
        server._executor.shutdown(wait=True)



def test_live_type_config_never_exports_representative_model_effort_levels() -> None:
    flash = ModelSpec(
        provider_id="p", provider_name="P", model_id="flash", name="Flash",
        effort_supported_levels=("low", "high"), effort_default_level="low",
        reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=True,
        reasoning_enable_value="enabled", reasoning_disable_value="disabled", tier="fast",
    )
    plain = ModelSpec(
        provider_id="p", provider_name="P", model_id="plain", name="Plain",
        reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
        reasoning_enable_value="enabled", reasoning_disable_value="disabled", tier="fast",
    )
    cfg = KiroByoConfig(
        providers=(ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", (flash, plain)),),
        default_model=flash.name, mode_pack="tiga", mode_type="sky",
    )
    session = Session("type-live", ".", type_selection_id("tiga", "sky"))
    ids = {item["id"] for item in AcpServer._config_options(session, cfg)}
    assert EFFORT_CONFIG_ID not in ids
    assert THINKING_CONFIG_ID not in ids


def test_openai_official_profile_exposes_all_declared_effort_levels_to_crew() -> None:
    raw = {
        "id": "openai",
        "preset_id": "openai",
        "name": "OpenAI",
        "type": "openai-responses",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "models": [{"id": "gpt-6-astra", "name": "GPT-6 Astra"}],
        "default_model": "gpt-6-astra",
    }
    provider = _load_provider(raw, allow_legacy_plaintext=True, resolve_credentials=False)
    model = provider.models[0]
    assert model.reasoning_mode == "levels"
    assert model.selectable_effort_levels == ("low", "medium", "high", "xhigh", "max")
    option = AcpServer._effort_config(model, "xhigh")
    assert option is not None
    assert [item["value"] for item in option["options"]] == ["low", "medium", "high", "xhigh", "max"]
    assert option["currentValue"] == "xhigh"


def test_anthropic_adaptive_thinking_still_exposes_declared_effort_levels_to_crew() -> None:
    raw = {
        "id": "anthropic",
        "preset_id": "anthropic",
        "name": "Anthropic",
        "type": "anthropic-messages",
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "",
        "models": [{"id": "claude-opus-5-5", "name": "Claude Opus 5.5"}],
        "default_model": "claude-opus-5-5",
    }
    provider = _load_provider(raw, allow_legacy_plaintext=True, resolve_credentials=False)
    model = provider.models[0]
    # This is the regression that a reasoning-mode whitelist used to break:
    # adaptive thinking and selectable output effort are independent controls.
    assert model.reasoning_mode == "adaptive"
    assert model.selectable_effort_levels == ("low", "medium", "high", "xhigh", "max")
    option = AcpServer._effort_config(model, "medium")
    assert option is not None
    assert [item["value"] for item in option["options"]] == ["low", "medium", "high", "xhigh", "max"]
    assert option["currentValue"] == "medium"


def test_effort_selector_is_not_hardcoded_to_provider_mode_count_or_vocabulary() -> None:
    cases = (
        ModelSpec(
            provider_id="future-a", provider_name="Future A", model_id="two", name="Two",
            effort_supported_levels=("eco", "deep"), effort_default_level="eco",
            reasoning_mode="provider-next", reasoning_supported=True,
        ),
        ModelSpec(
            provider_id="future-b", provider_name="Future B", model_id="four", name="Four",
            effort_supported_levels=("minimal", "standard", "deep", "extreme"),
            effort_default_level="standard", reasoning_mode="adaptive", reasoning_supported=True,
        ),
        ModelSpec(
            provider_id="future-c", provider_name="Future C", model_id="five", name="Five",
            effort_supported_levels=("eco", "low", "normal", "high", "ultra"),
            effort_default_level="normal", reasoning_mode="toggle", reasoning_supported=True,
        ),
    )
    for model in cases:
        option = AcpServer._effort_config(model, model.effort_default_level)
        assert option is not None
        assert tuple(item["value"] for item in option["options"]) == model.effort_supported_levels
        assert option["currentValue"] == model.effort_default_level


def test_multiple_effort_levels_are_hidden_only_when_reasoning_is_explicitly_unsupported() -> None:
    contradictory = ModelSpec(
        provider_id="future", provider_name="Future", model_id="disabled", name="Disabled",
        effort_supported_levels=("low", "high"), effort_default_level="high",
        reasoning_mode="provider-next", reasoning_supported=False,
    )
    assert contradictory.selectable_effort_levels == ()
    assert AcpServer._effort_config(contradictory, "high") is None
