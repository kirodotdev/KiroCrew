"""Zhipu/Z.AI GLM request policy on the shared native Chat engine."""
from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted
from .chat_completions_common import (
    replay_native_messages as _messages,
    run_native_chat_completion,
)


class ZhipuChatError(ProviderError):
    pass


class ZhipuChatInterrupted(ProviderInterrupted, ZhipuChatError):
    def __init__(self, partial_text: str = "") -> None:
        ProviderInterrupted.__init__(self, partial_text)


def _body(
    provider: ProviderSpec,
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    reasoning_effort: str | None,
    thinking_mode: str,
    model: ModelSpec | None,
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = dict(provider.extra_body)
    body.update({"model": model_id, "messages": _messages(provider, messages), "stream": provider.stream})
    if provider.temperature is not None:
        body["temperature"] = provider.temperature

    if model is not None and model.reasoning_supported is True:
        if thinking_mode == "off" and model.reasoning_can_disable is True and model.reasoning_disable_value:
            body["thinking"] = {"type": model.reasoning_disable_value}
        elif thinking_mode in {"on", "auto"} or model.reasoning_can_disable is False:
            enable = str(model.reasoning_enable_value or "enabled").strip()
            if enable:
                body["thinking"] = {"type": enable, "clear_thinking": False}
        if thinking_mode != "off" and reasoning_effort and reasoning_effort in model.effort_supported_levels:
            body["reasoning_effort"] = reasoning_effort

    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
        mid = model_id.lower()
        if provider.stream and (mid.startswith("glm-5") or mid.startswith("glm-4.7") or mid.startswith("glm-4.6")):
            body["tool_stream"] = True
    return body


def chat_completion_turn(
    provider: ProviderSpec,
    model_id: str,
    messages: list[dict[str, Any]],
    on_text: Callable[[str], None],
    *,
    on_reasoning: Callable[[str], None] | None = None,
    reasoning_effort: str | None = None,
    thinking_mode: str = "",
    model: ModelSpec | None = None,
    cache_key: str = "",
    tools: list[dict[str, Any]] | None = None,
    should_interrupt: Callable[[], bool] | None = None,
) -> ChatTurn:
    del cache_key  # GLM uses provider-managed implicit caching on this wire.
    body = _body(provider, model_id, messages, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, tools=tools)
    return run_native_chat_completion(
        provider, model_id, body, on_text,
        on_reasoning=on_reasoning,
        should_interrupt=should_interrupt,
        error_type=ZhipuChatError,
        interrupted_type=ZhipuChatInterrupted,
        opener=urllib.request.urlopen,
    )


def chat_completion(
    provider: ProviderSpec,
    model_id: str,
    messages: list[dict[str, Any]],
    on_text: Callable[[str], None],
    *,
    on_reasoning: Callable[[str], None] | None = None,
    reasoning_effort: str | None = None,
    thinking_mode: str = "",
    model: ModelSpec | None = None,
    cache_key: str = "",
) -> str:
    return chat_completion_turn(
        provider, model_id, messages, on_text,
        on_reasoning=on_reasoning, reasoning_effort=reasoning_effort,
        thinking_mode=thinking_mode, model=model, cache_key=cache_key,
    ).text
