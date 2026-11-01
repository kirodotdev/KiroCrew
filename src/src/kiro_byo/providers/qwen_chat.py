"""Alibaba Qwen request policy on the shared native Chat engine."""
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


class QwenChatError(ProviderError):
    pass


class QwenChatInterrupted(ProviderInterrupted, QwenChatError):
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

    if model is not None:
        if thinking_mode == "off" and model.reasoning_can_disable is True:
            body["enable_thinking"] = False
        elif thinking_mode == "on" and model.reasoning_supported is True and model.reasoning_can_disable is True:
            body["enable_thinking"] = True
        if thinking_mode != "off" and reasoning_effort and model.effort_supported_levels and reasoning_effort in model.effort_supported_levels:
            body["reasoning_effort"] = reasoning_effort

    if tools:
        body["tools"] = tools
        body.setdefault("tool_choice", "auto")
    if provider.stream:
        stream_options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
        stream_options.setdefault("include_usage", True)
        body["stream_options"] = stream_options
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
    del cache_key  # Qwen Chat exposes no Kiro-managed cache key on this path.
    body = _body(provider, model_id, messages, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, tools=tools)
    return run_native_chat_completion(
        provider, model_id, body, on_text,
        on_reasoning=on_reasoning,
        should_interrupt=should_interrupt,
        error_type=QwenChatError,
        interrupted_type=QwenChatInterrupted,
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
