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
    native_chat_body,
    make_native_chat_turn,
    make_native_chat_text,
)


class XiaomiMiMoError(ProviderError):
    pass


class XiaomiMiMoInterrupted(ProviderInterrupted, XiaomiMiMoError):
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
    del reasoning_effort  # MiMo currently exposes a binary thinking switch.
    body = native_chat_body(provider, model_id, messages, tools=tools, allow_temperature=False, thinking_mode=thinking_mode)

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
    return body


chat_completion_turn = make_native_chat_turn(
    build_body=_body, error_type=XiaomiMiMoError,
    interrupted_type=XiaomiMiMoInterrupted, opener=lambda: urllib.request.urlopen,
)


chat_completion = make_native_chat_text(lambda: chat_completion_turn)
