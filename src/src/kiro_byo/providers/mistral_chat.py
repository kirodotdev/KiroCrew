"""Native Mistral Chat Completions request policy.

Mistral's request body differs from generic OpenAI-shaped Chat Completions, and
its assistant ``content`` can contain provider-authentic ThinkChunk objects.
The HTTP/SSE/tool/usage engine is nevertheless identical, so this module only
owns request policy plus a structured-content codec.
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import ProviderError, ProviderInterrupted
from .chat_completions_common import (
    ChatContentCodec,
    replay_native_messages,
    run_native_chat_completion,
)


class MistralChatError(ProviderError):
    pass


class MistralChatInterrupted(ProviderInterrupted, MistralChatError):
    def __init__(self, partial_text: str = "") -> None:
        ProviderInterrupted.__init__(self, partial_text)


def _messages(provider: ProviderSpec, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return replay_native_messages(provider, messages)


def _body(
    provider: ProviderSpec,
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    reasoning_effort: str | None,
    thinking_mode: str,
    model: ModelSpec | None,
    cache_key: str,
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = dict(provider.extra_body)
    body.update({"model": model_id, "messages": _messages(provider, messages), "stream": provider.stream})
    if provider.temperature is not None:
        body["temperature"] = provider.temperature

    # Mistral uses one wire parameter for both disabled and enabled effort. Kiro
    # keeps the UI concepts separate so the disable sentinel is never offered as
    # an effort intensity.
    wire = ""
    if thinking_mode == "off":
        if model is not None and model.reasoning_can_disable is True and model.reasoning_disable_value:
            wire = model.reasoning_disable_value
    elif reasoning_effort and (
        model is None or not model.effort_supported_levels or reasoning_effort in model.effort_supported_levels
    ):
        wire = reasoning_effort
    if wire:
        body["reasoning_effort"] = wire

    if cache_key:
        body.setdefault("prompt_cache_key", cache_key)
    if tools:
        body["tools"] = tools
        body.setdefault("tool_choice", "auto")
    return body


def _visible_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _thinking_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "thinking":
            continue
        thinking = block.get("thinking")
        if isinstance(thinking, str):
            out.append(thinking)
        elif isinstance(thinking, list):
            out.extend(
                item["text"]
                for item in thinking
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            )
    return "".join(out)


def _merge_content(acc: list[dict[str, Any]], incoming: Any) -> None:
    """Merge streamed Mistral chunks while retaining replayable native shape."""

    if isinstance(incoming, str):
        if not incoming:
            return
        if acc and acc[-1].get("type") == "text":
            acc[-1]["text"] = str(acc[-1].get("text") or "") + incoming
        else:
            acc.append({"type": "text", "text": incoming})
        return
    if not isinstance(incoming, list):
        return
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        block = json.loads(json.dumps(raw))
        kind = str(block.get("type") or "")
        if kind == "text" and isinstance(block.get("text"), str):
            text = block["text"]
            if acc and acc[-1].get("type") == "text":
                acc[-1]["text"] = str(acc[-1].get("text") or "") + text
            else:
                acc.append(block)
            continue
        if kind == "thinking" and isinstance(block.get("thinking"), list):
            text = "".join(
                str(item.get("text") or "")
                for item in block["thinking"]
                if isinstance(item, dict) and item.get("type") == "text"
            )
            if text and acc and acc[-1].get("type") == "thinking" and isinstance(acc[-1].get("thinking"), list):
                inner = acc[-1]["thinking"]
                if inner and isinstance(inner[-1], dict) and inner[-1].get("type") == "text":
                    inner[-1]["text"] = str(inner[-1].get("text") or "") + text
                else:
                    inner.append({"type": "text", "text": text})
            else:
                acc.append(block)
            continue
        acc.append(block)


def _finalize_content(state: list[dict[str, Any]], fallback_text: str) -> Any:
    return state if state else fallback_text


_MISTRAL_CONTENT_CODEC = ChatContentCodec(
    new_stream_state=list,
    merge_stream_content=_merge_content,
    visible_text=_visible_text,
    reasoning_text=_thinking_text,
    finalize_stream_content=_finalize_content,
)


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
):
    body = _body(
        provider,
        model_id,
        messages,
        reasoning_effort=reasoning_effort,
        thinking_mode=thinking_mode,
        model=model,
        cache_key=cache_key,
        tools=tools,
    )
    return run_native_chat_completion(
        provider,
        model_id,
        body,
        on_text,
        on_reasoning=on_reasoning,
        should_interrupt=should_interrupt,
        error_type=MistralChatError,
        interrupted_type=MistralChatInterrupted,
        content_codec=_MISTRAL_CONTENT_CODEC,
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
        provider,
        model_id,
        messages,
        on_text,
        on_reasoning=on_reasoning,
        reasoning_effort=reasoning_effort,
        thinking_mode=thinking_mode,
        model=model,
        cache_key=cache_key,
        tools=None,
    ).text
