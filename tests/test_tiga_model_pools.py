"""Tiga routes model pools/roles, never one fixed model per Type."""
from __future__ import annotations

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec, _load_model
from kiro_byo.mode_router import (
    RoutingSignals,
    build_execution_plan,
    model_in_tier,
    model_tiers,
    route_collaboration_model,
    route_model,
)
from kiro_byo.provider_catalog import merge_official_model_profile, model_routing_from_row


def _model(provider: str, model_id: str, tiers: tuple[str, ...], *, quality: float, speed: float, economy: float) -> ModelSpec:
    return ModelSpec(
        provider, provider.upper(), model_id, model_id,
        routing_tiers=tiers,
        routing_quality=quality, routing_speed=speed, routing_economy=economy,
        reasoning_supported=True, reasoning_can_disable=True,
        effort_supported_levels=("low", "medium", "high", "xhigh", "max"),
        tool_use=True, input_modalities=("text", "image"), context_window=1_000_000,
    )


def _pool_config() -> KiroByoConfig:
    p1 = ProviderSpec("p1", "P1", "openai-compatible", "https://p1.invalid/v1", "", (
        _model("p1", "fast-a", ("fast", "balanced"), quality=.72, speed=.96, economy=.94),
        _model("p1", "strong-a", ("strong",), quality=.95, speed=.58, economy=.45),
    ))
    p2 = ProviderSpec("p2", "P2", "openai-compatible", "https://p2.invalid/v1", "", (
        _model("p2", "fast-b", ("fast", "balanced"), quality=.72, speed=.95, economy=.93),
        _model("p2", "strong-b", ("strong", "balanced"), quality=.95, speed=.57, economy=.46),
    ))
    return KiroByoConfig((p1, p2), mode_pack="tiga", mode_type="multi")


def _route(policy: str, prompt: str, *, effort: str = "", tools_required: bool = False):
    decision = route_model(
        _pool_config(), pack="tiga", policy=policy, prompt=prompt, history=[],
        has_image=False, tools_required=tools_required, tools_available=True,
        reasoning_effort=effort,
    )
    assert decision is not None
    return decision


def test_curated_routing_metadata_is_multi_role_and_not_name_only() -> None:
    astra = model_routing_from_row(merge_official_model_profile("openai", {"id": "gpt-6-astra"}))
    sol = model_routing_from_row(merge_official_model_profile("openai", {"id": "gpt-6.1-sol"}))
    luna = model_routing_from_row(merge_official_model_profile("openai", {"id": "gpt-6-luna"}))
    ultraspeed = model_routing_from_row(merge_official_model_profile("xiaomi", {"id": "mimo-v2.6-pro-ultraspeed"}))

    assert astra["tiers"] == ["strong"]
    assert sol["tiers"] == ["strong", "balanced"]
    assert luna["tiers"] == ["fast", "balanced"]
    assert ultraspeed["tiers"] == ["strong", "fast"]


def test_live_provider_routing_facts_override_curated_but_fill_missing_fields() -> None:
    row = merge_official_model_profile("openai", {
        "id": "gpt-6.1-sol",
        "routing": {"tiers": ["fast"], "speed": 0.99, "source": "provider-discovery"},
    })
    routing = model_routing_from_row(row)
    assert routing["tiers"] == ["fast"]
    assert routing["speed"] == 0.99
    # Omitted live facts still inherit curated values.
    assert routing["quality"] == 0.94
    assert routing["economy"] == 0.72
    assert routing["source"] == "mixed"
    assert routing["provenance"]["speed"] == {"source": "provider-discovery", "confidence": "high"}
    assert routing["provenance"]["tiers"] == {"source": "provider-discovery", "confidence": "high"}
    assert routing["provenance"]["quality"] == {"source": "official-profile", "confidence": "medium"}


def test_config_accepts_many_to_many_routing_tiers() -> None:
    model = _load_model("p", "P", {
        "id": "dual-role",
        "routing": {"tiers": ["strong", "fast"], "quality": .95, "speed": .96, "economy": .50},
    })
    assert model.routing_tiers == ("strong", "fast")
    assert model_tiers(model) == ("strong", "fast")
    assert model_in_tier(model, "strong")
    assert model_in_tier(model, "fast")




def test_discovered_routing_roles_survive_saved_catalogue_serialization() -> None:
    from kiro_byo.discovery import DiscoveredModel
    from kiro_byo.metadata_refresh import _model_dict

    raw = _model_dict(DiscoveredModel(
        "new-model", "New Model", routing_quality=.91, routing_speed=.88,
        routing_economy=.55, routing_tiers=("strong", "fast"),
    ))
    assert raw["routing"]["quality"] == .91
    assert raw["routing"]["speed"] == .88
    assert raw["routing"]["economy"] == .55
    assert raw["routing"]["tiers"] == ["strong", "fast"]
    assert "source" in raw["routing"]
    assert "confidence" in raw["routing"]
    assert "provenance" in raw["routing"]
    loaded = _load_model("p", "P", raw)
    assert loaded.routing_tiers == ("strong", "fast")
    assert model_in_tier(loaded, "strong") and model_in_tier(loaded, "fast")


def test_sky_can_use_two_distinct_fast_models_without_switching_type() -> None:
    decision = _route("sky", "high risk check", effort="xhigh")
    plan = build_execution_plan(decision)
    assert decision.policy == "sky"
    assert model_in_tier(decision.model, "fast")
    assert plan.post_stage is not None
    assert model_in_tier(plan.post_stage.model, "fast")
    assert (plan.post_stage.provider.id, plan.post_stage.model.model_id) != (decision.provider.id, decision.model.model_id)


def test_multi_can_use_two_distinct_models_without_switching_type() -> None:
    decision = _route("multi", "deep architecture review", effort="xhigh")
    plan = build_execution_plan(decision)
    assert decision.policy == "multi"
    assert plan.post_stage is not None
    assert (plan.post_stage.provider.id, plan.post_stage.model.model_id) != (decision.provider.id, decision.model.model_id)


def test_power_can_use_two_distinct_strong_models_without_switching_type() -> None:
    decision = _route("power", "请审查并修复这段 Python 代码中的并发 bug")
    plan = build_execution_plan(decision)
    assert decision.policy == "power"
    assert model_in_tier(decision.model, "strong")
    assert plan.post_stage is not None
    assert model_in_tier(plan.post_stage.model, "strong")
    assert (plan.post_stage.provider.id, plan.post_stage.model.model_id) != (decision.provider.id, decision.model.model_id)


def test_glitter_uses_distinct_leader_worker_and_reviewer_pools() -> None:
    cfg = _pool_config()
    decision = route_model(
        cfg, pack="tiga", policy="glitter", prompt="审计一个大型代码项目并找出问题",
        history=[], has_image=False, tools_required=True, tools_available=True,
    )
    assert decision is not None
    assert decision.policy == "glitter"
    assert model_in_tier(decision.model, "strong")

    signals = RoutingSignals(
        prompt_chars=80, history_chars=0, history_messages=0, has_image=False,
        tools_available=True, tools_required=True, reasoning_effort="", code_task=False,
        complexity=.20, estimated_input_tokens=2000,
    )
    worker = route_collaboration_model(
        cfg, decision.policy_definition, signals,
        leader_provider=decision.provider, leader_model=decision.model,
    )
    assert worker is not None
    assert model_in_tier(worker.model, "fast")

    plan = build_execution_plan(decision)
    assert plan.post_stage is not None
    assert model_in_tier(plan.post_stage.model, "strong")
    assert (plan.post_stage.provider.id, plan.post_stage.model.model_id) != (decision.provider.id, decision.model.model_id)
    assert (worker.provider.id, worker.model.model_id) != (decision.provider.id, decision.model.model_id)

def test_curated_routing_outweighs_historical_cache() -> None:
    row = merge_official_model_profile("openai", {
        "id": "gpt-6.1-sol",
        "catalog_source": "cache",
        "routing": {
            "tiers": ["fast"],
            "quality": 0.50,
            "speed": 0.99,
            "economy": 0.99,
            "source": "provider-discovery",
        },
    })
    routing = model_routing_from_row(row)
    assert routing["tiers"] == ["strong", "balanced"]
    assert routing["quality"] == 0.94
    assert routing["speed"] == 0.70
    assert routing["economy"] == 0.72
    assert routing["source"] == "official-profile"
    assert routing["confidence"] == "medium"


def test_local_model_override_is_final_routing_authority(tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "model_catalog.override.json").write_text(json.dumps({
        "schema_version": 1,
        "providers": {
            "openai": {
                "gpt-6.1-sol": {
                    "routing": {"tiers": ["fast"], "quality": 0.51}
                }
            }
        },
    }), encoding="utf-8")

    row = merge_official_model_profile("openai", {
        "id": "gpt-6.1-sol",
        "catalog_source": "provider-discovery",
        "routing": {"speed": 0.99, "source": "provider-discovery"},
    })
    routing = model_routing_from_row(row)
    assert routing["tiers"] == ["fast"]
    assert routing["quality"] == 0.51
    assert routing["speed"] == 0.99
    assert routing["provenance"]["tiers"] == {"source": "local-override", "confidence": "high"}
    assert routing["provenance"]["quality"] == {"source": "local-override", "confidence": "high"}
    assert routing["provenance"]["speed"] == {"source": "provider-discovery", "confidence": "high"}


def test_loaded_model_preserves_mixed_routing_provenance() -> None:
    row = merge_official_model_profile("openai", {
        "id": "gpt-6.1-sol",
        "catalog_source": "provider-discovery",
        "routing": {"speed": 0.99, "source": "provider-discovery"},
    })
    model = _load_model("p", "P", row)
    facts = {field: (source, confidence) for field, source, confidence in model.routing_provenance}
    assert model.routing_source == "mixed"
    assert model.routing_confidence == "medium"
    assert facts["speed"] == ("provider-discovery", "high")
    assert facts["quality"] == ("official-profile", "medium")


def test_sky_unified_plan_stays_single_agent_for_ordinary_fast_work() -> None:
    decision = _route("sky", "快速告诉我这个函数是什么意思")
    plan = build_execution_plan(decision)
    assert plan.topology == "single"
    assert [agent.role for agent in plan.agents] == ["lead"]
    assert plan.primary.tier_pool == "fast"


def test_multi_broad_read_only_task_admits_fast_worker_but_write_task_does_not() -> None:
    cfg = _pool_config()
    read_decision = route_model(
        cfg, pack="tiga", policy="multi",
        prompt="读取并比较整个仓库里 auth、provider、session 三个目录的实现，找出所有问题",
        history=[], has_image=False, tools_required=True, tools_available=True,
    )
    assert read_decision is not None
    read_plan = build_execution_plan(read_decision)
    assert read_plan.worker is not None
    assert read_plan.worker.tool_policy == "read_only"
    assert read_plan.worker.tier_pool == "fast"
    worker = route_collaboration_model(
        cfg, read_decision.execution_intent, read_decision.signals,
        leader_provider=read_decision.provider, leader_model=read_decision.model,
    )
    assert worker is not None
    assert model_in_tier(worker.model, "fast")

    write_decision = route_model(
        cfg, pack="tiga", policy="multi",
        prompt="修改整个仓库里 auth、provider、session 三个目录并修复所有问题",
        history=[], has_image=False, tools_required=True, tools_available=True,
    )
    assert write_decision is not None
    write_plan = build_execution_plan(write_decision)
    assert write_plan.worker is None
    assert write_plan.task_profile.write_intensity > read_plan.task_profile.write_intensity


def test_power_broad_read_only_task_uses_strong_worker_and_strong_review() -> None:
    cfg = _pool_config()
    decision = route_model(
        cfg, pack="tiga", policy="power",
        prompt="全面审查这个仓库的多个模块，读取所有相关文件并找出架构问题",
        history=[], has_image=False, tools_required=True, tools_available=True,
    )
    assert decision is not None
    plan = build_execution_plan(decision)
    assert plan.worker is not None
    assert plan.worker.tier_pool == "strong"
    assert plan.post_stage is not None
    assert model_in_tier(plan.post_stage.model, "strong")
    worker = route_collaboration_model(
        cfg, decision.execution_intent, decision.signals,
        leader_provider=decision.provider, leader_model=decision.model,
    )
    assert worker is not None
    assert model_in_tier(worker.model, "strong")


def test_glitter_unified_plan_is_leader_fast_worker_strong_reviewer() -> None:
    decision = _route("glitter", "审计一个大型代码项目并找出问题", tools_required=True)
    plan = build_execution_plan(decision)
    assert plan.topology == "leader-worker-review"
    assert plan.primary.tier_pool == "strong"
    assert plan.worker is not None and plan.worker.tier_pool == "fast"
    assert plan.worker.tool_policy == "read_only"
    assert plan.reviewer is not None and plan.reviewer.tier_pool == "strong"


def test_broad_read_only_work_uses_type_specific_parallel_worker_budget() -> None:
    cfg = _pool_config()
    prompt = (
        "全面读取并比较整个仓库里 auth、provider、session、runtime、tools 多个目录的所有实现，"
        "同时检查每个模块的边界、异常处理、测试覆盖和相互依赖，找出全部问题"
    )
    expected = {"multi": 2, "power": 3, "glitter": 4}
    for policy, worker_count in expected.items():
        decision = route_model(
            cfg, pack="tiga", policy=policy, prompt=prompt, history=[],
            has_image=False, tools_required=True, tools_available=True,
        )
        assert decision is not None
        plan = build_execution_plan(decision)
        assert len(plan.workers) == worker_count
        assert plan.max_parallel == worker_count
        assert plan.topology in {"leader-parallel-workers", "leader-parallel-workers-review"}
        assert all(worker.context_policy == "isolated_snapshot" for worker in plan.workers)
        assert all(worker.tool_policy == "read_only" for worker in plan.workers)
        assert len({worker.agent_name for worker in plan.workers}) == worker_count
