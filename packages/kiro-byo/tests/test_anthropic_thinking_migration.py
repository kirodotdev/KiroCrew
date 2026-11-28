"""Regression: persisted Claude settings must follow the live wire model ID.

No live Anthropic credentials or endpoint are required for these wire-contract
checks. Other provider transports and unknown relay aliases remain untouched.
"""
from __future__ import annotations

from kiro_byo.config import ModelSpec, ProviderSpec
from kiro_byo.providers import anthropic_messages
from kiro_byo.providers.capabilities import anthropic_thinking_capabilities


def _provider(*, extra_body=None, temperature=None):
    return ProviderSpec(
        id="p1", name="Test", type="anthropic-messages", base_url="https://api.anthropic.com/v1",
        api_key="test", models=(), preset_id="anthropic", stream=True,
        extra_body=extra_body or {}, temperature=temperature,
    )


def _model(model_id: str, **kwargs):
    values = {
        "provider_id": "p1", "provider_name": "Test", "model_id": model_id,
        "name": model_id, "reasoning_supported": True, "reasoning_mode": "levels",
        "reasoning_can_disable": True, "reasoning_enable_value": "enabled",
        "reasoning_disable_value": "disabled", "effort_supported_levels": ("low", "medium", "high", "xhigh", "max"),
        "max_output_tokens": 16384,
    }
    values.update(kwargs)
    return ModelSpec(**values)


def _body(model_id: str, model: ModelSpec, *, provider=None, mode="on", effort="high"):
    return anthropic_messages._body(
        provider or _provider(), model_id, [{"role": "user", "content": "test"}],
        reasoning_effort=effort, thinking_mode=mode, model=model, tools=None,
    )


def test_claude_4_7_migrates_old_enabled_profile_and_extra_body():
    m = _model("claude-opus-4-7")
    p = _provider(extra_body={"thinking": {"type": "enabled", "budget_tokens": 7000},
                              "temperature": 0.5, "top_p": 0.7})
    body = _body(m.model_id, m, provider=p)
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert "temperature" not in body and "top_p" not in body
    headers = anthropic_messages._headers(p, m, "on", True, wire_thinking=body["thinking"])
    assert "anthropic-beta" not in headers


def test_claude_4_6_prefers_adaptive_even_when_cache_says_enabled():
    m = _model("claude-sonnet-4-6")
    assert _body(m.model_id, m)["thinking"] == {"type": "adaptive"}


def test_claude_4_5_remains_manual_even_with_wrong_adaptive_cache():
    m = _model("claude-opus-4-5", reasoning_mode="adaptive", reasoning_enable_value="adaptive")
    body = _body(m.model_id, m)
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    headers = anthropic_messages._headers(_provider(), m, "on", True, wire_thinking=body["thinking"])
    assert headers["anthropic-beta"] == "interleaved-thinking-2025-05-14"


def test_sonnet_5_5_turns_off_upfront_thinking_using_between_tools():
    m = _model("claude-sonnet-5-5")
    p = _provider(extra_body={"thinking": {"type": "disabled"}, "output_config": {"effort": "max"}})
    body = _body(m.model_id, m, provider=p, mode="off", effort="max")
    assert body["thinking"] == {"type": "between_tools"}
    assert "output_config" not in body


def test_opus_5_5_always_on_strips_stale_disabled_setting():
    m = _model("claude-opus-5-5")
    p = _provider(extra_body={"thinking": {"type": "disabled"}, "output_config": {"effort": "xhigh"}})
    body = _body(m.model_id, m, provider=p, mode="off")
    assert "thinking" not in body
    assert "output_config" not in body


def test_haiku_5_5_still_accepts_disabled_and_stale_effort_is_removed():
    m = _model("claude-haiku-5-5")
    body = _body(m.model_id, m, provider=_provider(extra_body={"output_config": {"effort": "xhigh"}}), mode="off")
    assert body["thinking"] == {"type": "disabled"}
    assert "output_config" not in body


def test_metadata_cannot_block_known_new_claude_thinking():
    m = _model("claude-sonnet-5", reasoning_supported=False, reasoning_enable_value="enabled")
    assert _body(m.model_id, m)["thinking"] == {"type": "adaptive"}


def test_unknown_relay_alias_keeps_declared_metadata_not_guessed():
    m = _model("custom-claude-sonnet-4-7")
    body = _body(m.model_id, m)
    assert body["thinking"]["type"] == "enabled"
    assert anthropic_thinking_capabilities(m.model_id) is None


def test_manual_thinking_with_too_small_output_budget_fails_locally():
    import pytest
    m = _model("claude-opus-4-5", max_output_tokens=1024)
    with pytest.raises(anthropic_messages.AnthropicMessagesError, match="max_tokens > 1024"):
        _body(m.model_id, m)


def test_new_claude_contract_does_not_modify_minimax_transport():
    from kiro_byo.providers import anthropic_messages as am
    m = _model("MiniMax-M3", reasoning_enable_value="adaptive")
    p = ProviderSpec(id="p1", name="MiniMax", type="minimax-messages", base_url="https://api.minimax.io/anthropic", api_key="test", models=(), preset_id="minimax", stream=True)
    body = _body(m.model_id, m, provider=p)
    assert body["thinking"] == {"type": "adaptive"}
    assert "cache_control" not in body


def test_claude_off_never_mutates_saved_nested_provider_extra_body():
    extra = {"output_config": {"effort": "max", "other": "keep"},
             "thinking": {"type": "enabled", "budget_tokens": 7000}}
    p = _provider(extra_body=extra)
    model = _model("claude-haiku-5-5")
    body = _body(model.model_id, model, provider=p, mode="off", effort="max")
    assert body["thinking"] == {"type": "disabled"}
    assert body["output_config"] == {"other": "keep"}
    assert p.extra_body == extra
    assert extra["output_config"]["effort"] == "max"
    next_body = _body(model.model_id, model, provider=p, mode="on", effort="high")
    assert next_body["output_config"] == {"effort": "high", "other": "keep"}
    assert p.extra_body == extra
