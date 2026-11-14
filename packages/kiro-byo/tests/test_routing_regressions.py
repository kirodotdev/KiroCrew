from __future__ import annotations

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import build_signals, route_model


def _config() -> KiroByoConfig:
    fast_a = ModelSpec("p", "P", "alpha-flash", "Alpha Flash", tier="fast", tool_use=True, input_modalities=("text",))
    fast_b = ModelSpec("p", "P", "beta-mini", "Beta Mini", tier="fast", tool_use=True, input_modalities=("text",))
    strong = ModelSpec("p", "P", "gamma-pro", "Gamma Pro", tier="strong", tool_use=True, input_modalities=("text",))
    return KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", (fast_a, fast_b, strong)),), mode_type="multi")


def test_crew_wrapper_does_not_pollute_task_difficulty_but_still_counts_for_context() -> None:
    envelope = (
        "SYSTEM MEMORY: code files pytest architecture refactor " * 500
        + "\n[CURRENT USER REQUEST -- respond to this]\n"
        + "没事，随便聊聊"
    )
    old_user_envelope = (
        "SYSTEM RULES: code test files architecture " * 500
        + "\n--- USER MESSAGE BEGIN ---\n你好\n--- USER MESSAGE END ---"
    )
    signals = build_signals(
        envelope,
        [{"role": "user", "content": old_user_envelope}],
        has_image=False,
        tools_expected=True,
        reasoning_effort="",
    )
    assert signals.prompt_chars == len("没事，随便聊聊")
    assert signals.history_chars == len("你好")
    assert signals.code_task is False
    assert signals.complexity < 0.3
    # Context gating still sees the real wrapped bytes sent upstream.
    assert signals.estimated_input_tokens > 5000


def test_sky_provider_error_fallbacks_never_escape_fast_pool_when_fast_is_capable() -> None:
    decision = route_model(
        _config(), pack="tiga", policy="sky", prompt="hello", history=[],
        has_image=False, tools_expected=True,
    )
    assert decision is not None
    assert decision.model.tier == "fast"
    assert decision.execution_fallbacks
    assert {row.model.tier for row in decision.execution_fallbacks} == {"fast"}
    assert any(row.model.tier == "strong" for row in decision.candidates)


def test_power_provider_error_fallbacks_never_escape_strong_pool_when_strong_is_capable() -> None:
    decision = route_model(
        _config(), pack="tiga", policy="power", prompt="hello", history=[],
        has_image=False, tools_expected=True,
    )
    assert decision is not None
    assert decision.model.tier == "strong"
    assert {row.model.tier for row in decision.execution_fallbacks} == {"strong"}


def test_multilingual_substantive_requests_do_not_depend_on_task_keyword_hits() -> None:
    # These intentionally avoid the legacy _COMPLEX vocabulary (analyze,
    # architecture, 分析, etc.). Difficulty should come mostly from request
    # shape and clause structure, so equivalent requests stay in one band.
    prompts = [
        "Review a large project and fix complex problems.",
        "请审查一个大型项目并修复复杂问题。",
        "大規模なプロジェクトをレビューして複雑な問題を修正してください。",
        "Revisa un proyecto grande y corrige problemas complejos.",
    ]
    scores = [
        build_signals(text, [], has_image=False, tools_expected=False).complexity
        for text in prompts
    ]
    assert min(scores) >= 0.48
    assert max(scores) - min(scores) <= 0.18


def test_short_greetings_remain_easy_across_writing_systems() -> None:
    prompts = ["hello", "你好", "こんにちは", "hola"]
    scores = [
        build_signals(text, [], has_image=False, tools_expected=False).complexity
        for text in prompts
    ]
    assert max(scores) < 0.30
