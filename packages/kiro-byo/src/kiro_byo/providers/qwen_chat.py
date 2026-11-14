"""Alibaba Qwen request policy on the shared native Chat engine."""
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


class QwenChatError(ProviderError):
    pass


class QwenChatInterrupted(ProviderInterrupted, QwenChatError):
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
    body = native_chat_body(provider, model_id, messages, tools=tools, include_usage=True, thinking_mode=thinking_mode)

    if model is not None:
        if thinking_mode == "off" and model.reasoning_can_disable is True:
            body["enable_thinking"] = False
        elif thinking_mode == "on" and model.reasoning_supported is True and model.reasoning_can_disable is True:
            body["enable_thinking"] = True
        if thinking_mode != "off" and reasoning_effort and model.effort_supported_levels and reasoning_effort in model.effort_supported_levels:
            body["reasoning_effort"] = reasoning_effort

    return body


chat_completion_turn = make_native_chat_turn(
    build_body=_body, error_type=QwenChatError,
    interrupted_type=QwenChatInterrupted, opener=lambda: urllib.request.urlopen,
)


chat_completion = make_native_chat_text(lambda: chat_completion_turn)
