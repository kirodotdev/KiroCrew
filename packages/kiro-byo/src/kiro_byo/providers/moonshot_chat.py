"""Moonshot/Kimi request policy on the shared native Chat engine."""
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


class MoonshotChatError(ProviderError):
    pass


class MoonshotChatInterrupted(ProviderInterrupted, MoonshotChatError):
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
    mid = model_id.lower()
    current_kimi = mid == "kimi-k3" or mid.startswith("kimi-k2.7-code") or mid == "kimi-k2.6"
    body = native_chat_body(provider, model_id, messages, tools=tools, allow_temperature=not current_kimi, include_usage=True, thinking_mode=thinking_mode)

    if model is not None:
        if thinking_mode == "off" and model.reasoning_can_disable is True and model.reasoning_disable_value:
            body["thinking"] = {"type": model.reasoning_disable_value}
        elif thinking_mode in {"on", "auto"} and model.reasoning_supported is True:
            enable = model.reasoning_enable_value or ""
            if enable:
                thinking: dict[str, Any] = {"type": enable}
                if mid.startswith("kimi-k2.6") and enable == "enabled":
                    thinking["keep"] = "all"
                body["thinking"] = thinking
        if reasoning_effort and model.effort_supported_levels and reasoning_effort in model.effort_supported_levels and thinking_mode != "off":
            body["reasoning_effort"] = reasoning_effort

    return body


chat_completion_turn = make_native_chat_turn(
    build_body=_body, error_type=MoonshotChatError,
    interrupted_type=MoonshotChatInterrupted, opener=lambda: urllib.request.urlopen,
)


chat_completion = make_native_chat_text(lambda: chat_completion_turn)
