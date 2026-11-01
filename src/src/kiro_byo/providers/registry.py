"""Provider transport registry.

ACP/session/tool orchestration talks only to this module. Official providers can
therefore use their highest-fidelity native protocol without leaking wire-format
logic into Kiro Crew session handling.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import ChatTurn, ProviderAdapter, ProviderError
from . import anthropic_messages, cohere_v2, gemini_interactions, mistral_chat, moonshot_chat, openai_compatible, qwen_chat, responses_api, xiaomi_mimo, zhipu_chat
from .capabilities import responses_transport_ids


@dataclass(frozen=True)
class _ModuleAdapter:
    """Thin registry binding from a transport id to one protocol module.

    Transport adapters differ in wire semantics inside their modules, not in the
    two registry forwarding methods.  Keeping that forwarding generic prevents
    every new platform/product alias from growing another near-identical Python
    adapter class.
    """

    transport: str
    module: Any

    def completion_turn(
        self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]],
        on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None,
        reasoning_effort: str | None = None, thinking_mode: str = "",
        model: ModelSpec | None = None, cache_key: str = "",
        tools: list[dict[str, Any]] | None = None,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> ChatTurn:
        return self.module.chat_completion_turn(
            provider, model_id, messages, on_text, on_reasoning=on_reasoning,
            reasoning_effort=reasoning_effort, thinking_mode=thinking_mode,
            model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt,
        )

    def completion(
        self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]],
        on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None,
        reasoning_effort: str | None = None, thinking_mode: str = "",
        model: ModelSpec | None = None, cache_key: str = "",
    ) -> str:
        return self.module.chat_completion(
            provider, model_id, messages, on_text, on_reasoning=on_reasoning,
            reasoning_effort=reasoning_effort, thinking_mode=thinking_mode,
            model=model, cache_key=cache_key,
        )


def _module_adapter(transport: str, module: Any) -> ProviderAdapter:
    return _ModuleAdapter(transport, module)


_OPENAI = _module_adapter("openai-compatible", openai_compatible)
_ADAPTERS: dict[str, ProviderAdapter] = {
    "openai-compatible": _OPENAI,
    "openai-chat-completions": _OPENAI,
    "anthropic-messages": _module_adapter("anthropic-messages", anthropic_messages),
    "minimax-messages": _module_adapter("minimax-messages", anthropic_messages),
    "gemini-interactions": _module_adapter("gemini-interactions", gemini_interactions),
    "cohere-v2": _module_adapter("cohere-v2", cohere_v2),
    "qwen-chat-v1": _module_adapter("qwen-chat-v1", qwen_chat),
    "zhipu-chat-v4": _module_adapter("zhipu-chat-v4", zhipu_chat),
    "mistral-chat-v1": _module_adapter("mistral-chat-v1", mistral_chat),
    "moonshot-chat-v1": _module_adapter("moonshot-chat-v1", moonshot_chat),
    "xiaomi-mimo-chat": _module_adapter("xiaomi-mimo-chat", xiaomi_mimo),
}
for _transport in responses_transport_ids():
    _ADAPTERS[_transport] = _module_adapter(_transport, responses_api)


def adapter_for(provider: ProviderSpec) -> ProviderAdapter:
    adapter = _ADAPTERS.get(str(provider.type or "").strip().lower())
    if adapter is None:
        raise ProviderError(f"unsupported provider transport {provider.type!r}; configured providers must use an installed adapter")
    return adapter


def _effective_provider(provider: ProviderSpec, model_spec: ModelSpec | None) -> ProviderSpec:
    """Apply a verified per-model transport without changing provider identity.

    Some official catalogues expose multiple generations through one endpoint,
    but only a subset supports the richer Responses protocol.  A model-level
    override keeps those models full-fidelity while older models continue using
    the provider's safe default transport.
    """
    override = str(getattr(model_spec, "transport", "") or "").strip().lower()
    if not override or override == provider.type:
        return provider
    if override not in _ADAPTERS:
        raise ProviderError(f"unsupported model transport {override!r} for {model_spec.model_id!r}")
    return replace(provider, type=override)


def chat_completion_turn(provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model_spec: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
    effective = _effective_provider(provider, model_spec)
    wire_model_id = model_spec.wire_model_id if model_spec is not None else model_id
    return adapter_for(effective).completion_turn(effective, wire_model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model_spec, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)


def chat_completion(provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model_spec: ModelSpec | None = None, cache_key: str = "") -> str:
    effective = _effective_provider(provider, model_spec)
    wire_model_id = model_spec.wire_model_id if model_spec is not None else model_id
    return adapter_for(effective).completion(effective, wire_model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model_spec, cache_key=cache_key)
