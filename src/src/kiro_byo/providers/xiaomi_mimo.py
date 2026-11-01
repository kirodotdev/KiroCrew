"""Xiaomi MiMo request policy on the shared native Chat engine."""
from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted
from .chat_completions_common import (
    endpoint as _endpoint,
    headers as _headers,
    replay_native_messages as _messages,
    run_native_chat_completion,
)


class XiaomiMiMoError(ProviderError):
    pass


class XiaomiMiMoInterrupted(ProviderInterrupted, XiaomiMiMoError):
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
    del reasoning_effort  # MiMo currently exposes a binary thinking switch.
    body: dict[str, Any] = dict(provider.extra_body)
    body.update({"model": model_id, "messages": _messages(provider, messages), "stream": provider.stream})

    thinking_enabled: bool | None = None
    if model is not None and model.reasoning_supported is True:
        thinking_enabled = model.reasoning_default_enabled
        if thinking_mode == "off" and model.reasoning_can_disable is True and model.reasoning_disable_value:
            body["thinking"] = {"type": model.reasoning_disable_value}
            thinking_enabled = False
        elif thinking_mode in {"on", "auto"}:
            body["thinking"] = {"type": model.reasoning_enable_value or "enabled"}
            thinking_enabled = True

    if provider.temperature is not None and thinking_enabled is False:
        body["temperature"] = provider.temperature
    if tools:
        body["tools"] = tools
        body.setdefault("tool_choice", "auto")
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
    del cache_key  # MiMo Chat exposes no Kiro-managed cache key.
    body = _body(provider, model_id, messages, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, tools=tools)
    return run_native_chat_completion(
        provider, model_id, body, on_text,
        on_reasoning=on_reasoning,
        should_interrupt=should_interrupt,
        error_type=XiaomiMiMoError,
        interrupted_type=XiaomiMiMoInterrupted,
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
