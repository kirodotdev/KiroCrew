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


@dataclass(frozen=True)
class _OpenAICompatibleAdapter:
    transport: str = "openai-compatible"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return openai_compatible.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return openai_compatible.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)


@dataclass(frozen=True)
class _ResponsesAdapter:
    transport: str

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return responses_api.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return responses_api.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)


@dataclass(frozen=True)
class _AnthropicAdapter:
    transport: str = "anthropic-messages"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return anthropic_messages.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return anthropic_messages.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)


@dataclass(frozen=True)
class _GeminiAdapter:
    transport: str = "gemini-interactions"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return gemini_interactions.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return gemini_interactions.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)



@dataclass(frozen=True)
class _MistralAdapter:
    transport: str = "mistral-chat-v1"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return mistral_chat.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return mistral_chat.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)


@dataclass(frozen=True)
class _ZhipuAdapter:
    transport: str = "zhipu-chat-v4"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return zhipu_chat.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return zhipu_chat.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)


@dataclass(frozen=True)
class _QwenChatAdapter:
    transport: str = "qwen-chat-v1"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return qwen_chat.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return qwen_chat.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)


@dataclass(frozen=True)
class _MoonshotAdapter:
    transport: str = "moonshot-chat-v1"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return moonshot_chat.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return moonshot_chat.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)


@dataclass(frozen=True)
class _XiaomiMiMoAdapter:
    transport: str = "xiaomi-mimo-chat"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return xiaomi_mimo.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return xiaomi_mimo.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)


@dataclass(frozen=True)
class _CohereAdapter:
    transport: str = "cohere-v2"

    def completion_turn(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
        return cohere_v2.chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)

    def completion(self, provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
        return cohere_v2.chat_completion(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key)

_OPENAI = _OpenAICompatibleAdapter()
_ADAPTERS: dict[str, ProviderAdapter] = {
    "openai-compatible": _OPENAI,
    "openai-chat-completions": _OPENAI,
    "openai-responses": _ResponsesAdapter("openai-responses"),
    "deepseek-responses": _ResponsesAdapter("deepseek-responses"),
    "ark-responses": _ResponsesAdapter("ark-responses"),
    "xai-responses": _ResponsesAdapter("xai-responses"),
    "groq-responses": _ResponsesAdapter("groq-responses"),
    "anthropic-messages": _AnthropicAdapter("anthropic-messages"),
    "minimax-messages": _AnthropicAdapter("minimax-messages"),
    "gemini-interactions": _GeminiAdapter(),
    "cohere-v2": _CohereAdapter(),
    "qwen-chat-v1": _QwenChatAdapter(),
    "qwen-responses": _ResponsesAdapter("qwen-responses"),
    "hunyuan-responses": _ResponsesAdapter("hunyuan-responses"),
    "qianfan-responses": _ResponsesAdapter("qianfan-responses"),
    "zhipu-chat-v4": _ZhipuAdapter(),
    "mistral-chat-v1": _MistralAdapter(),
    "moonshot-chat-v1": _MoonshotAdapter(),
    "xiaomi-mimo-chat": _XiaomiMiMoAdapter(),
}


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
    return adapter_for(effective).completion_turn(effective, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model_spec, cache_key=cache_key, tools=tools, should_interrupt=should_interrupt)


def chat_completion(provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model_spec: ModelSpec | None = None, cache_key: str = "") -> str:
    effective = _effective_provider(provider, model_spec)
    return adapter_for(effective).completion(effective, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model_spec, cache_key=cache_key)
