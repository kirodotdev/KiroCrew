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
        "id": "dyna",
        "display_name": "Dyna Policy Pack",
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
                    "single_mode": "dyna_single",
                    "multi_mode": "dyna_verified",
                    "reasoning_intent": "economy",
                },
            }
        ],
    }


def _config(pack: str = "dyna", policy: str = "miracle") -> KiroByoConfig:
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
        path = directory / "dyna.json"
        path.write_text(json.dumps(_custom_pack(description="first")), encoding="utf-8")
        assert get_policy_pack("dyna").description == "first"
        path.write_text(json.dumps(_custom_pack(description="second")), encoding="utf-8")
        assert get_policy_pack("dyna").description == "second"
        public = public_policy_packs()
        assert public["hot_reload"] is True
        assert public["directory"] == str(directory)
        dyna = next(row for row in public["packs"] if row["id"] == "dyna")
        assert dyna["policies"][0]["selection_id"] == "kiro-byo-type:dyna:miracle"


def test_execution_planner_consumes_custom_schema_without_knowing_policy_names(tmp_path: Path) -> None:
    with use_app_home(tmp_path):
        directory = tmp_path / "policy_packs"
        directory.mkdir(parents=True)
        (directory / "dyna.json").write_text(json.dumps(_custom_pack()), encoding="utf-8")
        decision = route_model(
            _config(), prompt="hello", history=[], has_image=False, tools_expected=False,
        )
        assert decision is not None
        assert decision.pack == "dyna"
        assert decision.pack_label == "Dyna Policy Pack"
        assert decision.policy == "miracle"
        assert decision.policy_label == "Miracle Type"
        assert decision.model.model_id == "fast"
        assert decision.policy_definition.auto_reasoning.thinking == "adaptive_per_call"
        assert decision.policy_definition.auto_reasoning.effort == "adaptive"
        plan = build_execution_plan(decision)
        assert plan.mode == "dyna_verified"
        assert plan.post_stage is not None
        assert plan.post_stage.reasoning_intent == "economy"
        assert plan.post_stage.pack == "dyna"
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
        (directory / "dyna.json").write_text(json.dumps(_custom_pack()), encoding="utf-8")
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir(parents=True)
        providers_path = cfg_dir / "providers.json"
        providers_path.write_text(json.dumps({"mode": {"id": "dyna", "type": "miracle"}, "providers": []}), encoding="utf-8")
        cfg = load_catalog_config(providers_path)
        assert cfg.mode_pack == "dyna"
        assert cfg.mode_type == "miracle"
        settings = mode_settings()
        assert settings["pack"] == "dyna"
        assert settings["policy"] == "miracle"
        assert settings["pack_label"] == "Dyna Policy Pack"
        updated = set_mode_selection("multi", "tiga")
        assert updated["pack"] == "tiga"
        assert updated["policy"] == "multi"
        stored = json.loads(providers_path.read_text(encoding="utf-8"))
        assert stored["mode"] == {"id": "tiga", "type": "multi"}
