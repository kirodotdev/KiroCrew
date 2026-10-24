from __future__ import annotations

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import RoutingSignals, route_collaboration_model, route_model


def _signals(complexity: float, *, code: bool = False, tools: bool = True) -> RoutingSignals:
    return RoutingSignals(
        prompt_chars=120,
        history_chars=0,
        history_messages=0,
        has_image=False,
        tools_available=tools,
        tools_required=tools,
        reasoning_effort="",
        code_task=code,
        complexity=complexity,
        estimated_input_tokens=2000,
    )


def _config() -> tuple[KiroByoConfig, ProviderSpec, ModelSpec]:
    lead = ModelSpec(
        "p1", "P1", "lead-pro", "Lead Pro", tier="strong", tool_use=True,
        reasoning_supported=True, reasoning_can_disable=True, reasoning_default_enabled=True,
        thinking_preference="auto", routing_quality=0.95, routing_speed=0.35, routing_economy=0.25,
    )
    same_fast = ModelSpec(
        "p1", "P1", "same-fast", "Same Fast", tier="fast", tool_use=True,
        reasoning_supported=True, reasoning_can_disable=True, reasoning_default_enabled=False,
        thinking_preference="off", routing_quality=0.35, routing_speed=0.95, routing_economy=0.95,
    )
    p1 = ProviderSpec("p1", "P1", "openai-compatible", "https://one.invalid/v1", "", (lead, same_fast))
    foreign_fast = ModelSpec(
        "p2", "P2", "foreign-fast", "Foreign Fast", tier="fast", tool_use=True,
        reasoning_supported=True, reasoning_can_disable=True, reasoning_default_enabled=True,
        thinking_preference="on", routing_quality=0.85, routing_speed=0.75, routing_economy=0.65,
    )
    p2 = ProviderSpec("p2", "P2", "openai-compatible", "https://two.invalid/v1", "", (foreign_fast,))
    return KiroByoConfig((p1, p2), mode_type="glitter"), p1, lead


def test_glitter_worker_is_rescored_from_each_subtask_not_leader_order() -> None:
    cfg, leader_provider, leader_model = _config()
    decision = route_model(
        cfg, pack="tiga", policy="glitter", prompt="long hard project", history=[],
        has_image=False, tools_required=True, tools_available=True,
    )
    assert decision is not None
    policy = decision.policy_definition

    easy = route_collaboration_model(
        cfg, policy, _signals(0.10), leader_provider=leader_provider, leader_model=leader_model,
    )
    hard = route_collaboration_model(
        cfg, policy, _signals(0.95, code=True), leader_provider=leader_provider, leader_model=leader_model,
    )
    assert easy is not None and hard is not None
    # Same-provider continuity wins for the cheap mechanical lookup.
    assert (easy.provider.id, easy.model.model_id) == ("p1", "same-fast")
    # A difficult subtask is allowed to cross providers when the other fast
    # worker is materially better suited. Provider affinity is not a lock.
    assert (hard.provider.id, hard.model.model_id) == ("p2", "foreign-fast")


def test_glitter_worker_requires_tool_capability_for_gathering_subtasks() -> None:
    cfg, leader_provider, leader_model = _config()
    no_tool = ModelSpec(
        "p3", "P3", "cheap-fast", "Cheap Fast", tier="fast", tool_use=False,
        routing_quality=0.4, routing_speed=1.0, routing_economy=1.0,
    )
    cfg = KiroByoConfig((*cfg.providers, ProviderSpec(
        "p3", "P3", "openai-compatible", "https://three.invalid/v1", "", (no_tool,)
    )), mode_type="glitter")
    decision = route_model(
        cfg, pack="tiga", policy="glitter", prompt="investigate", history=[],
        has_image=False, tools_required=True, tools_available=True,
    )
    assert decision is not None
    worker = route_collaboration_model(
        cfg, decision.policy_definition, _signals(0.05),
        leader_provider=leader_provider, leader_model=leader_model,
    )
    assert worker is not None
    assert worker.model.model_id != "cheap-fast"
    assert worker.model.tool_use is True
