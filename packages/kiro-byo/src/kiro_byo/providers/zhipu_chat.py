"""Zhipu/Z.AI GLM request policy on the shared native Chat engine."""
from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted
from .chat_completions_common import (
    replay_native_messages as _messages,
    native_chat_body,
    make_native_chat_turn,
    make_native_chat_text,
)


class ZhipuChatError(ProviderError):
    pass


class ZhipuChatInterrupted(ProviderInterrupted, ZhipuChatError):
    pass


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
    body = native_chat_body(provider, model_id, messages, tools=tools, thinking_mode=thinking_mode)

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
        body["tool_choice"] = "auto"
        mid = model_id.lower()
        if provider.stream and (mid.startswith("glm-5") or mid.startswith("glm-4.7") or mid.startswith("glm-4.6")):
            body["tool_stream"] = True
    return body


chat_completion_turn = make_native_chat_turn(
    build_body=_body, error_type=ZhipuChatError,
    interrupted_type=ZhipuChatInterrupted, opener=lambda: urllib.request.urlopen,
)


chat_completion = make_native_chat_text(lambda: chat_completion_turn)
