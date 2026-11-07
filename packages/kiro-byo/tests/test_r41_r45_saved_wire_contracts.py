"""Regression: saved provider overrides cannot resurrect a disabled thinking mode.

These tests only validate deterministic request construction. They are not
claims of live upstream API or KiroCrew host acceptance.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from kiro_byo.config import ModelSpec, ProviderSpec
from kiro_byo.crew_contract_checks import rc6_contract_issues
from kiro_byo.providers import cohere_v2, gemini_interactions, mistral_chat, responses_api


def provider(transport: str, extra: dict | None = None) -> ProviderSpec:
    return ProviderSpec(
        id="p1", name="Primary", type=transport, base_url="https://api.example.test/v1",
        api_key="fake", models=(), stream=False, extra_body=extra or {},
    )


def model(*, off: str = "none", levels: tuple[str, ...] = ("low", "high")) -> ModelSpec:
    return ModelSpec(
        provider_id="p1", provider_name="Primary", model_id="example", name="Example",
        reasoning_supported=True, reasoning_mode="levels", reasoning_can_disable=True,
        reasoning_enable_value="enabled", reasoning_disable_value=off,
        effort_supported_levels=levels,
    )


MESSAGES = [{"role": "user", "content": "hello"}]


def test_rc6_gate_rejects_reverse_backend_operator(tmp_path: Path):
    host = tmp_path
    sources = {
        'mate_worker.py': '''
def resolve_worker_spec(spec, key, *, refuse_spec_policy=False):
    if refuse_spec_policy:
        raise RuntimeError("unsupported policy")
    return spec

def worker_session_spec(spec, key, *, refuse_spec_policy=False):
    return resolve_worker_spec(spec, key, refuse_spec_policy=refuse_spec_policy)
''',
        'mate_spec_policy.py': '''
def applies_spec_policy(backend):
    return backend != ACP_BACKEND_KAS

def layer_policy(target, policy):
    return None

def policy_of(spec):
    return spec
''',
        'mcp_gateway/backend_handshake.py': '''
_INITIALIZE_TIMEOUT_COLD_START_FLOOR_SECS = 30
def _effective_initialize_timeout_secs(configured):
    return max(configured, _INITIALIZE_TIMEOUT_COLD_START_FLOOR_SECS)
''',
        'config/validation.py': '''
_FAIL_CLOSED_PATHS = frozenset({"agent"})
def _apply_field_default(data, dot_path):
    return None
''',
    }
    for name, code in sources.items():
        path = host / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code)
    assert any("KAS-only" in x for x in rc6_contract_issues(host))


def test_mistral_off_overrides_saved_effort_and_preserves_other_settings():
    extra = {"reasoning_effort": "xhigh", "custom": {"mode": ["safe"]}}
    p = provider("mistral-chat-v1", extra)
    before = deepcopy(extra)
    off = mistral_chat._body(p, "mistral-large-4-0", MESSAGES,
        reasoning_effort="high", thinking_mode="off", model=model(), cache_key="run42", tools=None)
    assert off["reasoning_effort"] == "none"
    assert off["prompt_cache_key"] == "run42"
    assert off["custom"] == {"mode": ["safe"]}
    on = mistral_chat._body(p, "mistral-large-4-0", MESSAGES,
        reasoning_effort="high", thinking_mode="on", model=model(), cache_key="run42", tools=None)
    assert on["reasoning_effort"] == "high"
    assert extra == before
    assert off["custom"] is not extra["custom"]


def test_mistral_off_does_not_forward_stale_effort_without_capability_profile():
    p = provider("mistral-chat-v1", {"reasoning_effort": "xhigh"})
    off = mistral_chat._body(p, "unprofiled", MESSAGES,
        reasoning_effort=None, thinking_mode="off", model=None, cache_key="", tools=None)
    assert "reasoning_effort" not in off


def test_cohere_off_replaces_cached_thinking_without_changing_on():
    extra = {"thinking": {"type": "enabled", "budget_tokens": 2048}, "keep": 7}
    p = provider("cohere-v2", extra)
    m = model(off="disabled")
    off = cohere_v2._body(p, "cohere", MESSAGES, thinking_mode="off", model=m, tools=None)
    assert off["thinking"] == {"type": "disabled"}
    assert off["keep"] == 7
    on = cohere_v2._body(p, "cohere", MESSAGES, thinking_mode="on", model=m, tools=None)
    assert on["thinking"] == {"type": "enabled"}
    assert extra["thinking"] == {"type": "enabled", "budget_tokens": 2048}


def test_cohere_off_without_profile_removes_stale_thinking():
    p = provider("cohere-v2", {"thinking": {"type": "enabled"}})
    off = cohere_v2._body(p, "unprofiled", MESSAGES, thinking_mode="off", model=None, tools=None)
    assert "thinking" not in off


def test_gemini_off_cannot_inherit_old_thinking_level_or_mutate_saved_config():
    extra = {"generation_config": {"thinking_level": "high", "candidate_count": 1}}
    p = provider("gemini-interactions", extra)
    m = model(off="minimal")
    off = gemini_interactions._body(p, "gemini-future", MESSAGES,
        reasoning_effort="high", thinking_mode="off", model=m, tools=None)
    assert off["generation_config"] == {"thinking_level": "minimal", "candidate_count": 1}
    on = gemini_interactions._body(p, "gemini-future", MESSAGES,
        reasoning_effort="high", thinking_mode="on", model=m, tools=None)
    assert on["generation_config"]["thinking_level"] == "high"
    assert extra["generation_config"]["thinking_level"] == "high"


def test_gemini_off_missing_declared_wire_value_rejected():
    p = provider("gemini-interactions", {"generation_config": {"thinking_level": "high"}})
    with pytest.raises(gemini_interactions.GeminiInteractionsError, match="no verified"):
        gemini_interactions._body(p, "broken", MESSAGES,
            reasoning_effort=None, thinking_mode="off", model=model(off=""), tools=None)


@pytest.mark.parametrize("transport, off_literal", [
    ("openai-responses", "none"),
    ("qianfan-responses", "shallow"),
])
def test_responses_off_removes_mismatched_cached_wires(transport, off_literal):
    extra = {"thinking": {"type": "enabled"}, "reasoning": {"effort": "xhigh"}, "keep": {"v": [1]}}
    p = provider(transport, extra)
    before = deepcopy(extra)
    m = model(off=off_literal)
    off = responses_api._body(p, "future", MESSAGES, reasoning_effort=None,
        thinking_mode="off", model=m, cache_key="", tools=None)
    if transport == "qianfan-responses":
        assert off["thinking"] == {"type": "shallow"}
        assert "reasoning" not in off
    else:
        assert off["reasoning"] == {"effort": "none"}
        assert "thinking" not in off
    assert off["keep"] == extra["keep"]
    assert off["keep"] is not extra["keep"]
    assert extra == before
