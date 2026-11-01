"""OpenAI-compatible Chat Completions adapter.

The current transport keeps cooperative interruption for cancel/steer while preserving:
multimodal user content and native function/tool calling.  The adapter still
uses only the Python standard library so it can live inside Kiro Crew's bundled
interpreter without installing wheels.
"""

from __future__ import annotations

from .. import __version__

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted, ToolCall, UsageInfo
from .chat_completions_common import usage_info as _usage_info
from .http_sse import (
    InterruptibleResponse as _InterruptibleResponse,
    http_error_message as _http_error_message,
    iter_response_lines as _iter_response_lines,
    read_response_bytes as _shared_read_response_bytes,
    post_json as _post_json,
)


class OpenAICompatibleError(ProviderError):
    pass


class OpenAICompatibleInterrupted(ProviderInterrupted, OpenAICompatibleError):
    """OpenAI-compatible transport yielded to cancel/steer."""

    def __init__(self, partial_text: str = "") -> None:
        ProviderInterrupted.__init__(self, partial_text)


def _read_response_bytes(
    response: Any,
    should_interrupt: Callable[[], bool] | None,
    *,
    interrupted_type: type[ProviderInterrupted] = OpenAICompatibleInterrupted,
) -> bytes:
    """Compatibility shim around the provider-neutral HTTP helper."""

    return _shared_read_response_bytes(
        response, should_interrupt, interrupted_type=interrupted_type
    )


def _endpoint(provider: ProviderSpec) -> str:
    path = provider.chat_completions_path
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{provider.base_url}/{path.lstrip('/')}"


def _headers(provider: ProviderSpec) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if provider.stream else "application/json",
        "User-Agent": f"Kiro-BYO/{__version__}",
    }
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    headers.update(provider.headers)
    return headers


def _body(
    provider: ProviderSpec,
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    reasoning_effort: str | None = None,
    thinking_mode: str = "",
    model: Any = None,
    cache_key: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = dict(provider.extra_body)
    # Session records may carry private provider-authentic continuation metadata.
    # Compatibility endpoints must never see Kiro-internal keys.
    clean_messages = [
        {k: v for k, v in message.items() if not str(k).startswith("_kiro_")}
        for message in messages
    ]
    body.update({"model": model_id, "messages": clean_messages, "stream": provider.stream})
    if provider.temperature is not None:
        body["temperature"] = provider.temperature
    # A user-level thinking=off preference is a hard boundary.  For generic
    # OpenAI-compatible endpoints we only know one portable switch:
    # ``reasoning_effort=none`` (or an equivalent catalogue-declared disable
    # value on a levels-style reasoning API).  Never silently ignore an Off
    # request for a toggle whose wire shape is unknown; fail closed instead of
    # accidentally sending an enabled-thinking request.
    if thinking_mode == "off" and model is not None and getattr(model, "reasoning_can_disable", None) is True:
        disable = str(getattr(model, "reasoning_disable_value", "") or "").strip()
        mode = str(getattr(model, "reasoning_mode", "") or "").strip().lower()
        if disable and (disable.lower() == "none" or mode == "levels"):
            body["reasoning_effort"] = disable
        else:
            raise OpenAICompatibleError(
                f"{model_id} declares switchable reasoning but the generic OpenAI-compatible "
                "adapter has no verified wire mapping for thinking=off; use a native provider "
                "transport or verified reasoning_effort=none semantics"
            )
    elif reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if provider.preset_id == "mistral" and cache_key:
        # Mistral's official Chat API supports prompt_cache_key and bills cache
        # hits at the provider's reduced cached-input rate.
        body.setdefault("prompt_cache_key", cache_key)
    if tools:
        body["tools"] = tools
        body.setdefault("tool_choice", "auto")
    return body


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    out.append(text)
        return "".join(out)
    return str(content) if content is not None else ""




def _parse_tool_calls(raw_calls: Any) -> tuple[ToolCall, ...]:
    if not isinstance(raw_calls, list):
        return ()
    out: list[ToolCall] = []
    for idx, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = fn.get("arguments")
        out.append(
            ToolCall(
                call_id=str(raw.get("id") or f"call_{idx}"),
                name=name,
                arguments_text=arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
            )
        )
    return tuple(out)


def chat_completion_turn(
    provider: ProviderSpec,
    model_id: str,
    messages: list[dict[str, Any]],
    on_text: Callable[[str], None],
    *,
    on_reasoning: Callable[[str], None] | None = None,
    reasoning_effort: str | None = None,
    thinking_mode: str = "",
    model: Any = None,
    cache_key: str = "",
    tools: list[dict[str, Any]] | None = None,
    should_interrupt: Callable[[], bool] | None = None,
) -> ChatTurn:
    """Run one upstream turn, returning text and any native tool calls.

    ``should_interrupt`` is deliberately transport-generic.  Kiro BYO uses it
    for both hard cancel and mid-turn steer; provider adapters added later can
    expose the same yield point without importing ACP concepts.
    """

    if should_interrupt is not None and should_interrupt():
        raise OpenAICompatibleInterrupted("")

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
    response = _post_json(
        url=_endpoint(provider), headers=_headers(provider), body=body,
        timeout_seconds=provider.timeout_seconds, error_type=OpenAICompatibleError,
        opener=urllib.request.urlopen,
    )

    with _InterruptibleResponse(response, should_interrupt) as response:
        content_type = response.headers.get("Content-Type", "").lower()
        if (not provider.stream) or ("text/event-stream" not in content_type):
            try:
                payload = json.loads(_read_response_bytes(response, should_interrupt, interrupted_type=OpenAICompatibleInterrupted).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise OpenAICompatibleError("response was not valid JSON/SSE") from exc
            if not isinstance(payload, dict):
                raise OpenAICompatibleError("response JSON root was not an object")
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                raise OpenAICompatibleError(error["message"])
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise OpenAICompatibleError("response did not contain choices[0]")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise OpenAICompatibleError("response did not contain choices[0].message")
            text = _content_text(message.get("content"))
            reasoning = message.get("reasoning_content")
            if not isinstance(reasoning, str):
                reasoning = message.get("reasoning") if isinstance(message.get("reasoning"), str) else ""
            if reasoning and on_reasoning is not None:
                on_reasoning(reasoning)
            if text:
                on_text(text)
            if should_interrupt is not None and should_interrupt():
                # The non-streaming response itself is the next natural yield
                # boundary. Emit it, preserve it as partial context, then let the
                # ACP layer consume steer/cancel without ending the host turn.
                raise OpenAICompatibleInterrupted(text)
            return ChatTurn(
                text=text,
                tool_calls=_parse_tool_calls(message.get("tool_calls")),
                usage=_usage_info(payload.get("usage")),
                served_model=str(payload.get("model") or model_id),
            )

        text_parts: list[str] = []
        call_parts: dict[int, dict[str, str]] = {}
        usage = UsageInfo()
        served_model = model_id
        for raw_line in _iter_response_lines(response, should_interrupt):
            if should_interrupt is not None and should_interrupt():
                raise OpenAICompatibleInterrupted("".join(text_parts))
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                raise OpenAICompatibleError(error["message"])
            if isinstance(event.get("model"), str) and event.get("model"):
                served_model = event["model"]
            if isinstance(event.get("usage"), dict):
                usage = _usage_info(event.get("usage"))
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                continue
            reasoning = delta.get("reasoning_content")
            if not isinstance(reasoning, str):
                reasoning = delta.get("reasoning") if isinstance(delta.get("reasoning"), str) else ""
            if reasoning and on_reasoning is not None:
                on_reasoning(reasoning)
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
                on_text(content)
            raw_calls = delta.get("tool_calls")
            if not isinstance(raw_calls, list):
                continue
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    continue
                index = raw_call.get("index")
                if not isinstance(index, int):
                    index = len(call_parts)
                acc = call_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if isinstance(raw_call.get("id"), str):
                    acc["id"] += raw_call["id"]
                fn = raw_call.get("function")
                if isinstance(fn, dict):
                    if isinstance(fn.get("name"), str):
                        acc["name"] += fn["name"]
                    if isinstance(fn.get("arguments"), str):
                        acc["arguments"] += fn["arguments"]

        if should_interrupt is not None and should_interrupt():
            raise OpenAICompatibleInterrupted("".join(text_parts))

        calls = tuple(
            ToolCall(
                call_id=acc["id"] or f"call_{idx}",
                name=acc["name"],
                arguments_text=acc["arguments"],
            )
            for idx, acc in sorted(call_parts.items())
            if acc["name"]
        )
        return ChatTurn(
            text="".join(text_parts),
            tool_calls=calls,
            usage=usage,
            served_model=served_model,
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
    model: Any = None,
    cache_key: str = "",
) -> str:
    """Compatibility helper used by compaction, which deliberately has no tools."""
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
