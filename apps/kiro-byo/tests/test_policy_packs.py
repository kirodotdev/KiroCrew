from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_byo.mode_router import build_execution_plan, route_model
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import PolicyPackError, get_policy_pack, load_policy_packs, parse_policy_pack, public_policy_packs


def _custom_pack(*, description: str = "Custom hot-plug pack") -> dict:
    return {
        "schema_version": 1,
        "id": "aurora",
        "display_name": "Aurora Policy Pack",
        "description": description,
        "default_policy": "miracle",
        "policies": [
            {
                "id": "miracle",
                "display_name": "Miracle Type",
                "description": "A test policy loaded entirely from data.",
                "reasoning": {"thinking_auto": "adaptive_per_call", "effort_auto": "adaptive"},
                "routing": {"quality": 0.05, "speed": 0.85, "economy": 0.10},
                "secondary": {
                    "role": "verify",
                    "objective": "economy",
                    "trigger": {"always": True},
                    "reason": "custom independent verifier",
                    "self_reason": "custom self verifier",
                    "rationale": "custom pack requested a verifier",
                    "single_mode": "aurora_single",
                    "multi_mode": "aurora_verified",
                    "reasoning_intent": "economy",
                },
            }
        ],
    }


def _config(pack: str = "aurora", policy: str = "miracle") -> KiroByoConfig:
    fast = ModelSpec(
        provider_id="demo", provider_name="Demo", model_id="fast", name="Demo Flash",
        reasoning_supported=True, effort_supported_levels=("low", "high"), tool_use=True,
        routing_quality=0.4, routing_speed=0.99, routing_economy=0.9,
    )
    strong = ModelSpec(
        provider_id="demo", provider_name="Demo", model_id="strong", name="Demo Pro",
        reasoning_supported=True, effort_supported_levels=("low", "high", "max"), tool_use=True,
        routing_quality=0.98, routing_speed=0.3, routing_economy=0.2,
    )
    provider = ProviderSpec("demo", "Demo", "openai-compatible", "x", "", (fast, strong))
    return KiroByoConfig((provider,), mode_pack=pack, mode_type=policy)


def test_builtin_tiga_is_a_policy_pack_not_router_code() -> None:
    tiga = get_policy_pack("tiga")
    assert tiga is not None
    assert tiga.display_name == "Tiga Policy Pack"
    assert tiga.default_policy == "multi"
    assert [p.id for p in tiga.policies] == ["sky", "multi", "power", "glitter"]
    assert tiga.policy("glitter").display_name == "Glitter Type"


def test_custom_pack_hot_reloads_without_process_restart(tmp_path: Path) -> None:
    with use_app_home(tmp_path):
        directory = tmp_path / "policy_packs"
        directory.mkdir(parents=True)
        path = directory / "aurora.json"
        path.write_text(json.dumps(_custom_pack(description="first")), encoding="utf-8")
        assert get_policy_pack("aurora").description == "first"
        path.write_text(json.dumps(_custom_pack(description="second")), encoding="utf-8")
        assert get_policy_pack("aurora").description == "second"
        public = public_policy_packs()
        assert public["hot_reload"] is True
        assert public["directory"] == str(directory)
        aurora = next(row for row in public["packs"] if row["id"] == "aurora")
        assert aurora["policies"][0]["selection_id"] == "kiro-byo-type:aurora:miracle"


def test_execution_planner_consumes_custom_schema_without_knowing_policy_names(tmp_path: Path) -> None:
    with use_app_home(tmp_path):
        directory = tmp_path / "policy_packs"
        directory.mkdir(parents=True)
        (directory / "aurora.json").write_text(json.dumps(_custom_pack()), encoding="utf-8")
        decision = route_model(
            _config(), prompt="hello", history=[], has_image=False, tools_expected=False,
        )
        assert decision is not None
        assert decision.pack == "aurora"
        assert decision.pack_label == "Aurora Policy Pack"
        assert decision.policy == "miracle"
        assert decision.policy_label == "Miracle Type"
        assert decision.model.model_id == "fast"
        assert decision.policy_definition.auto_reasoning.thinking == "adaptive_per_call"
        assert decision.policy_definition.auto_reasoning.effort == "adaptive"
        plan = build_execution_plan(decision)
        assert plan.mode == "aurora_verified"
        assert plan.post_stage is not None
        assert plan.post_stage.reasoning_intent == "economy"
        assert plan.post_stage.pack == "aurora"
        assert plan.post_stage.policy == "miracle"


def test_pack_schema_rejects_invalid_weights() -> None:
    raw = _custom_pack()
    raw["policies"][0]["routing"]["quality"] = 2
    with pytest.raises(PolicyPackError):
        parse_policy_pack(raw)

from kiro_byo.config import load_catalog_config
from kiro_byo.provider_admin import mode_settings, set_mode_selection


def test_config_and_admin_select_custom_pack_without_restart(tmp_path: Path) -> None:
    with use_app_home(tmp_path):
        directory = tmp_path / "policy_packs"
        directory.mkdir(parents=True)
        (directory / "aurora.json").write_text(json.dumps(_custom_pack()), encoding="utf-8")
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir(parents=True)
        providers_path = cfg_dir / "providers.json"
        providers_path.write_text(json.dumps({"mode": {"id": "aurora", "type": "miracle"}, "providers": []}), encoding="utf-8")
        cfg = load_catalog_config(providers_path)
        assert cfg.mode_pack == "aurora"
        assert cfg.mode_type == "miracle"
        settings = mode_settings()
        assert settings["pack"] == "aurora"
        assert settings["policy"] == "miracle"
        assert settings["pack_label"] == "Aurora Policy Pack"
        # Mode changes must not mutate the independent concrete-model axis.
        stored_before = json.loads(providers_path.read_text(encoding="utf-8"))
        stored_before["default_model"] = "demo/m"
        stored_before["providers"] = [{
            "id": "demo", "name": "Demo", "type": "openai-compatible",
            "base_url": "https://example.invalid/v1",
            "models": [{"id": "m", "name": "M"}], "default_model": "m",
        }]
        providers_path.write_text(json.dumps(stored_before), encoding="utf-8")

        updated = set_mode_selection("multi", "tiga")
        assert updated["pack"] == "tiga"
        assert updated["policy"] == "multi"
        stored = json.loads(providers_path.read_text(encoding="utf-8"))
        assert stored["mode"] == {"id": "tiga", "type": "multi"}
        assert stored["default_model"] == "demo/m"
        assert stored["providers"][0]["default_model"] == "m"


def test_builtin_v4_separates_user_semantics_and_keeps_router_tuning_in_data() -> None:
    tiga = get_policy_pack("tiga")
    assert tiga is not None
    sky = tiga.policy("sky")
    glitter = tiga.policy("glitter")
    assert sky is not None
    assert glitter is not None
    assert sky.semantics.display_name == "Sky Type"
    assert sky.semantics.localized_name == "空中型"
    assert sky.execution.routing.tier_pool == "fast"
    assert sky.execution.routing.adaptive.half_life_days == pytest.approx(3.0)
    assert sky.execution.routing.fallback_scoring is not None
    assert sky.execution.secondary is not None and sky.execution.secondary.scoring is not None
    assert sky.execution.orchestration.objective == "latency"
    assert sky.execution.orchestration.single_writer is True
    assert glitter.execution.orchestration.worker_mode == "always"
    assert glitter.execution.orchestration.worker_tier_pool == "fast"
    assert glitter.execution.collaboration_routing is not None
    assert glitter.execution.collaboration_routing.complexity_shift.center == pytest.approx(0.0)
    assert glitter.execution.collaboration_provider_affinity == pytest.approx(0.025)
    # Product names live outside the machine policy.
    assert not hasattr(sky.execution, "display_name")
    assert not hasattr(sky.execution, "description")


def test_schema_v1_custom_pack_is_migrated_into_split_internal_model() -> None:
    pack = parse_policy_pack(_custom_pack())
    miracle = pack.policy("miracle")
    assert miracle is not None
    assert miracle.semantics.display_name == "Miracle Type"
    assert miracle.execution.routing.speed == pytest.approx(0.85)
    assert miracle.execution.auto_reasoning.thinking == "adaptive_per_call"


def test_route_decision_compiles_name_free_execution_intent() -> None:
    decision = route_model(
        _config("tiga", "multi"), pack="tiga", policy="multi",
        prompt="hello", history=[], has_image=False, tools_expected=False,
    )
    assert decision is not None
    intent = decision.execution_intent
    assert intent.routing.tier_pool == "any"
    assert intent.collaboration == "none"
    assert intent.reasoning.thinking == "adaptive"
    assert not hasattr(intent, "display_name")
    assert not hasattr(intent, "localized_name")
    assert not hasattr(intent, "description")
