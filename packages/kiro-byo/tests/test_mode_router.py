from __future__ import annotations

from kiro_byo.mode_router import build_execution_plan, build_signals, glitter_review_target, route_model
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec


def _config(policy: str = "multi") -> KiroByoConfig:
    # Tiers are curated, not inferred from the "flash"/"pro" in the model id.
    # Name-based tier inference is retired.
    flash = ModelSpec(
        provider_id="demo", provider_name="Demo", model_id="v4-flash", name="Demo V4 Flash",
        context_window=1_000_000, effort_supported_levels=("low", "high"),
        reasoning_supported=True, tool_use=True, input_modalities=("text", "image"),
        tier="fast",
    )
    pro = ModelSpec(
        provider_id="demo", provider_name="Demo", model_id="v4-pro", name="Demo V4 Pro",
        context_window=1_000_000, effort_supported_levels=("low", "high", "max"),
        reasoning_supported=True, tool_use=True, input_modalities=("text",),
        tier="strong",
    )
    provider = ProviderSpec("demo", "Demo", "openai-compatible", "https://example.invalid/v1", "", (flash, pro))
    return KiroByoConfig((provider,), mode_type=policy)


def test_sky_prefers_fast_tier_for_simple_turn() -> None:
    decision = route_model(_config(), policy="sky", prompt="hello", history=[], has_image=False, tools_expected=True)
    assert decision is not None
    assert decision.model.model_id == "v4-flash"


def test_power_and_glitter_prefer_quality_tier() -> None:
    for policy in ("power", "glitter"):
        decision = route_model(_config(), policy=policy, prompt="hello", history=[], has_image=False, tools_expected=True)
        assert decision is not None
        assert decision.model.model_id == "v4-pro"


def test_multi_escalates_complex_max_reasoning_turn() -> None:
    decision = route_model(
        _config(), policy="multi",
        prompt="Analyze and refactor this architecture with deep reasoning and tests",
        history=[], has_image=False, tools_expected=True, reasoning_effort="max",
    )
    assert decision is not None
    assert decision.model.model_id == "v4-pro"
    assert decision.signals.complexity > 0.5


def test_glitter_does_not_downgrade_its_strong_leader_for_image_capability() -> None:
    decision = route_model(_config(), policy="glitter", prompt="what is in this image", history=[], has_image=True, tools_expected=True)
    assert decision is None


def test_manual_routing_hints_override_generic_name_prior() -> None:
    cheap_pro = ModelSpec(
        "p", "P", "pro", "Something Pro", tool_use=True,
        routing_quality=0.20, routing_speed=0.99, routing_economy=0.99,
    )
    plain = ModelSpec(
        "p", "P", "plain", "Something", tool_use=True,
        routing_quality=0.95, routing_speed=0.40, routing_economy=0.30,
    )
    cfg = KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "x", "", (cheap_pro, plain)),))
    sky = route_model(cfg, policy="sky", prompt="hello", history=[], has_image=False, tools_expected=True)
    glitter = route_model(cfg, policy="glitter", prompt="hello", history=[], has_image=False, tools_expected=True)
    assert sky and sky.model.model_id == "pro"
    assert glitter and glitter.model.model_id == "plain"


def test_signal_estimate_accounts_for_history_and_task_type() -> None:
    sig = build_signals(
        "Please debug this code ```python\nraise ValueError\n```",
        [{"role": "user", "content": "x" * 20000}],
        has_image=False, tools_expected=True, reasoning_effort="high",
    )
    assert sig.code_task is True
    assert sig.estimated_input_tokens > 5000
    assert sig.complexity > 0.5


def test_glitter_never_lets_a_weaker_model_review_the_primary() -> None:
    decision = route_model(
        _config(), policy="glitter", prompt="Analyze this architecture carefully",
        history=[], has_image=False, tools_expected=True, reasoning_effort="high",
    )
    assert decision is not None
    review = glitter_review_target(decision)
    assert review is not None
    # Only a weaker Flash peer exists, so the strong primary reviews itself.
    assert decision.model.model_id == "v4-pro"
    assert review.model.model_id == decision.model.model_id
    assert "self-review" in review.reason


def test_glitter_uses_an_independent_reviewer_when_an_equal_peer_exists() -> None:
    peer = ModelSpec(
        provider_id="other", provider_name="Other", model_id="other-pro", name="Other Pro",
        context_window=1_000_000, effort_supported_levels=("low", "high", "max"),
        reasoning_supported=True, tool_use=True, input_modalities=("text",),
        tier="strong",
    )
    base = _config()
    other = ProviderSpec("other", "Other", "openai-compatible", "https://example.invalid/v1", "", (peer,))
    config = KiroByoConfig((*base.providers, other), mode_type="glitter")
    decision = route_model(
        config, policy="glitter", prompt="Analyze this architecture carefully",
        history=[], has_image=False, tools_expected=True, reasoning_effort="high",
    )
    assert decision is not None
    review = glitter_review_target(decision)
    assert review is not None
    assert review.model.model_id != decision.model.model_id
    assert review.model.model_id in {"v4-pro", "other-pro"}
    assert "self-review" not in review.reason


def test_power_has_no_glitter_review_stage() -> None:
    decision = route_model(
        _config(), policy="power", prompt="Analyze this architecture carefully",
        history=[], has_image=False, tools_expected=True, reasoning_effort="high",
    )
    assert decision is not None
    assert glitter_review_target(decision) is None


def test_glitter_self_review_fallback_when_only_one_model_is_eligible() -> None:
    only = ModelSpec(
        provider_id="solo", provider_name="Solo", model_id="solo-pro", name="Solo Pro",
        reasoning_supported=True, tool_use=True, input_modalities=("text", "image"),
        routing_quality=0.98, routing_speed=0.4, routing_economy=0.3,
    )
    cfg = KiroByoConfig((ProviderSpec("solo", "Solo", "openai-compatible", "x", "", (only,)),))
    decision = route_model(cfg, policy="glitter", prompt="hard task", history=[], has_image=False, tools_expected=True)
    assert decision is not None
    review = glitter_review_target(decision)
    assert review is not None
    assert review.model.model_id == decision.model.model_id
    assert "self-review" in review.reason


def test_execution_planner_sky_stays_single_pass_for_simple_turn() -> None:
    decision = route_model(_config(), policy="sky", prompt="hello", history=[], has_image=False, tools_expected=False)
    assert decision is not None
    plan = build_execution_plan(decision)
    assert plan.mode == "fast_single_pass"
    assert plan.post_stage is None


def test_execution_planner_sky_can_verify_exceptionally_hard_turn() -> None:
    decision = route_model(
        _config(), policy="sky",
        prompt="Analyze this architecture and prove the design is correct " + ("x" * 6000),
        history=[{"role": "user", "content": "y" * 70000}],
        has_image=False, tools_expected=True, reasoning_effort="max",
    )
    assert decision is not None
    plan = build_execution_plan(decision)
    assert plan.mode == "fast_verified"
    assert plan.post_stage is not None
    assert plan.post_stage.role == "verify"


def test_execution_planner_multi_verifies_only_when_value_justifies_it() -> None:
    simple = route_model(_config(), policy="multi", prompt="hello", history=[], has_image=False, tools_expected=False)
    assert simple is not None
    assert build_execution_plan(simple).post_stage is None
    hard = route_model(
        _config(), policy="multi", prompt="Debug and refactor this code architecture with tests",
        history=[], has_image=False, tools_expected=True, reasoning_effort="max",
    )
    assert hard is not None
    plan = build_execution_plan(hard)
    assert plan.mode == "balanced_verified"
    assert plan.post_stage is not None and plan.post_stage.role == "verify"




def test_execution_planner_multi_keeps_normal_complex_work_single_pass() -> None:
    decision = route_model(
        _config(), policy="multi",
        prompt=(
            "Inspect this project architecture, compare the main components, identify likely issues, "
            "and propose a careful implementation plan with tradeoffs."
        ),
        history=[], has_image=False, tools_expected=True, reasoning_effort="",
    )
    assert decision is not None
    # Multi is the transparent default: ordinary substantial work still uses the
    # normal one-model agent loop. Secondary verification is reserved for truly
    # exceptional risk or an explicit max/xhigh user request.
    plan = build_execution_plan(decision)
    assert plan.mode == "balanced_single_pass"
    assert plan.post_stage is None


def test_execution_planner_power_is_reliability_first_not_always_multi_pass() -> None:
    simple = route_model(_config(), policy="power", prompt="hello", history=[], has_image=False, tools_expected=False)
    assert simple is not None
    assert build_execution_plan(simple).mode == "strong_single_pass"
    substantive = route_model(
        _config(), policy="power", prompt="Analyze this architecture carefully",
        history=[], has_image=False, tools_expected=True, reasoning_effort="high",
    )
    assert substantive is not None
    plan = build_execution_plan(substantive)
    assert plan.mode == "strong_verified"
    assert plan.post_stage is not None and plan.post_stage.role == "verify"


def test_execution_planner_glitter_always_reviews_when_candidate_exists() -> None:
    decision = route_model(_config(), policy="glitter", prompt="hello", history=[], has_image=False, tools_expected=False)
    assert decision is not None
    plan = build_execution_plan(decision)
    assert plan.mode == "maximum_quality_reviewed"
    assert plan.post_stage is not None and plan.post_stage.role == "review"

# Cold-start sampling is constrained to policy-near-ties, not random model
# switching. These tests use only in-memory catalogue facts (no API calls).
def _cold_start_config(*, reverse: bool = False) -> KiroByoConfig:
    models = tuple(ModelSpec(
        "relay", "Relay", ident, ident, routing_tiers=(tier,), tool_use=True,
        input_modalities=("text", "image"),
    ) for ident, tier in (("alpha", "fast"), ("beta", "fast"), ("gamma", "strong")))
    if reverse:
        models = tuple(reversed(models))
    return KiroByoConfig((ProviderSpec(
        "relay", "Relay", "openai-compatible", "https://example.invalid/v1", "", models,
    ),))


def test_low_risk_sky_samples_near_tied_in_tier_models_without_mixing_strong() -> None:
    from kiro_byo.mode_router import route_diagnostics

    config = _cold_start_config()
    selected = set()
    for n in range(32):
        message = f"hello {n}"
        decision = route_model(config, pack="tiga", policy="sky", prompt=message,
                               history=[], has_image=False)
        assert decision is not None
        assert decision.model.model_id in {"alpha", "beta"}
        assert decision.selection_source == "bounded_cold_start"
        assert decision.execution_fallbacks[0].model.model_id == decision.model.model_id
        details = route_diagnostics(decision)
        assert details["selection_source"] == "bounded_cold_start"
        selected.add(decision.model.model_id)
        repeated = route_model(config, pack="tiga", policy="sky", prompt=message,
                               history=[], has_image=False)
        assert repeated and repeated.model.model_id == decision.model.model_id
        reversed_order = route_model(_cold_start_config(reverse=True), pack="tiga",
                                     policy="sky", prompt=message, history=[], has_image=False)
        assert reversed_order and reversed_order.model.model_id == decision.model.model_id
    assert selected == {"alpha", "beta"}


def test_dyna_flash_can_sample_multiple_tiers_when_policy_pool_is_any() -> None:
    picks = {
        route_model(_cold_start_config(), pack="dyna", policy="flash",
                    prompt=f"hello {n}", history=[], has_image=False).model.model_id
        for n in range(40)
    }
    assert picks == {"alpha", "beta", "gamma"}


def test_strict_strong_type_keeps_top_rank_instead_of_exploring() -> None:
    config = _cold_start_config()
    for n in range(10):
        decision = route_model(config, pack="tiga", policy="power",
                               prompt=f"hello {n}", history=[], has_image=False)
        assert decision is not None
        assert decision.model.model_id == "gamma"
        assert decision.selection_source == "policy_ranked"


def test_cold_start_respects_risk_write_gates_and_runtime_evidence() -> None:
    from dataclasses import replace
    from kiro_byo.mode_router import _cold_start_choice, CandidateScore, RoutingSignals
    from kiro_byo.policy_packs import get_policy_pack

    policy = next(p.execution.routing for p in get_policy_pack("dyna").policies if p.id == "flash")
    models = _cold_start_config().providers[0].models
    provider = _cold_start_config().providers[0]
    leader = CandidateScore(provider, models[0], .8, .8, .8, .8, (), runtime_samples=7)
    unobserved = CandidateScore(provider, models[1], .79, .8, .8, .8, (), runtime_samples=0)
    signals = RoutingSignals(40, 0, 0, False, False, False, "", False, .1, 100)
    pick = _cold_start_choice([leader, unobserved], signals, policy_key="dyna:flash",
                              request="hello", routing=policy)
    assert pick is unobserved  # bounded learning before the runner has samples
    assert _cold_start_choice([leader, unobserved], replace(signals, risk=.7),
                              policy_key="dyna:flash", request="hello", routing=policy) is None
    assert _cold_start_choice([leader, unobserved], replace(signals, write_intensity=.7),
                              policy_key="dyna:flash", request="hello", routing=policy) is None
    assert _cold_start_choice([leader, replace(unobserved, score=.6)], signals,
                              policy_key="dyna:flash", request="hello", routing=policy) is None
    assert _cold_start_choice([leader, replace(unobserved, runtime_samples=8)], signals,
                              policy_key="dyna:flash", request="hello", routing=policy) is None
    assert _cold_start_choice([leader, replace(unobserved, model=replace(models[1], tool_use=None))],
                              replace(signals, tools_required=True, tools_available=True),
                              policy_key="dyna:flash", request="hello", routing=policy) is None


def test_sky_fallback_exploration_uses_fallback_scores_not_primary_scores() -> None:
    # These models are explicitly Balanced. Sky may sample its fallback pool,
    # but a fallback-score gap must beat the cold-start exploration margin.
    a = ModelSpec("relay", "Relay", "a", "A", tier="balanced",
                  routing_quality=.60, routing_speed=.80, routing_economy=.40)
    b = ModelSpec("relay", "Relay", "b", "B", tier="balanced",
                  routing_quality=.75, routing_speed=.65, routing_economy=.65)
    config = KiroByoConfig((ProviderSpec(
        "relay", "Relay", "openai-compatible", "https://example.invalid/v1", "", (a, b)
    ),))
    for n in range(25):
        decision = route_model(config, pack="tiga", policy="sky", prompt=f"hello {n}",
                               history=[], has_image=False)
        assert decision is not None
        assert decision.model.model_id == "b"  # 0.65 vs 0.62 fallback score
        assert decision.selection_source == "policy_ranked"
        assert "outside-tier fallback" in " ".join(decision.reason)
