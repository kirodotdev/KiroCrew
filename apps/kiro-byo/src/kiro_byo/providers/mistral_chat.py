"""Native Mistral Chat Completions transport.

Mistral's reasoning models expose provider-authentic ``ThinkChunk`` objects in
``message.content``. Flattening them to visible text makes the next turn lose the
reasoning prefix that Mistral explicitly asks clients to replay, which hurts
coherence and prompt-cache reuse. This adapter therefore keeps the native
assistant message as opaque provider context while exposing only final text to
Kiro Crew.
"""
from __future__ import annotations

from .. import __version__

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted, ToolCall, UsageInfo
from .openai_compatible import (
    _InterruptibleResponse,
    _http_error_message,
    _iter_response_lines,
    _read_response_bytes,
)


class MistralChatError(ProviderError):
    pass


class MistralChatInterrupted(ProviderInterrupted, MistralChatError):
    def __init__(self, partial_text: str = "") -> None:
        ProviderInterrupted.__init__(self, partial_text)


def _endpoint(provider: ProviderSpec) -> str:
    path = provider.chat_completions_path or "/chat/completions"
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{provider.base_url.rstrip('/')}/{path.lstrip('/')}"


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


def _matching_context(message: dict[str, Any], provider: ProviderSpec) -> dict[str, Any] | None:
    raw = message.get("_kiro_provider_context")
    if not isinstance(raw, dict):
        return None
    if str(raw.get("provider_id") or "") != provider.id:
        return None
    if str(raw.get("transport") or "") != provider.type:
        return None
    data = raw.get("data")
    return data if isinstance(data, dict) else None


def _messages(provider: ProviderSpec, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "assistant":
            context = _matching_context(message, provider)
            raw_message = context.get("message") if context else None
            if isinstance(raw_message, dict) and raw_message.get("role") == "assistant":
                # Keep ThinkChunk/TextChunk/tool_calls exactly as returned. This
                # is both a fidelity requirement and a prompt-cache optimization.
                out.append(json.loads(json.dumps(raw_message)))
                continue
        clean = {k: v for k, v in message.items() if not str(k).startswith("_kiro_")}
        out.append(clean)
    return out


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

    # Mistral uses one wire parameter for both "none" and enabled effort. Kiro's
    # session keeps the concepts separate so `none` never appears as an effort
    # intensity button.
    wire = ""
    if thinking_mode == "off":
        if model is not None and model.reasoning_can_disable is True and model.reasoning_disable_value:
            wire = model.reasoning_disable_value
    elif reasoning_effort and (model is None or not model.effort_supported_levels or reasoning_effort in model.effort_supported_levels):
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
    out: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            out.append(block["text"])
    return "".join(out)



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
            for item in thinking:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    out.append(item["text"])
    return "".join(out)

def _parse_tool_calls(raw_calls: Any) -> tuple[ToolCall, ...]:
    if not isinstance(raw_calls, list):
        return ()
    out: list[ToolCall] = []
    for idx, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function")
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            continue
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False, separators=(",", ":"))
        out.append(ToolCall(str(raw.get("id") or f"call_{idx}"), fn["name"], args))
    return tuple(out)


def _usage(raw: Any) -> UsageInfo:
    if not isinstance(raw, dict):
        return UsageInfo()

    def iv(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
        return 0

    prompt_details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
    completion_details = raw.get("completion_tokens_details") if isinstance(raw.get("completion_tokens_details"), dict) else {}
    return UsageInfo(
        input_tokens=iv(raw.get("prompt_tokens", raw.get("input_tokens"))),
        output_tokens=iv(raw.get("completion_tokens", raw.get("output_tokens"))),
        cached_tokens=iv(prompt_details.get("cached_tokens")),
        reasoning_tokens=iv(completion_details.get("reasoning_tokens")),
        cached_tokens_reported="cached_tokens" in prompt_details,
        reasoning_tokens_reported="reasoning_tokens" in completion_details,
    )


def _provider_context(provider: ProviderSpec, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_id": provider.id,
        "transport": provider.type,
        "data": {"message": json.loads(json.dumps(message))},
    }


def _merge_content(acc: list[dict[str, Any]], incoming: Any) -> None:
    """Merge streamed Mistral chunks while retaining their native structure."""
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
            # Streaming can split the inner TextChunk; concatenate adjacent
            # thinking blocks to reconstruct a replayable assistant message.
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


def _append_tool_delta(parts: dict[int, dict[str, str]], raw_calls: Any) -> None:
    if not isinstance(raw_calls, list):
        return
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        idx = raw.get("index")
        if not isinstance(idx, int):
            idx = len(parts)
        acc = parts.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if isinstance(raw.get("id"), str):
            # Most APIs repeat the id rather than fragmenting it. Avoid doubling.
            if not acc["id"]:
                acc["id"] = raw["id"]
        fn = raw.get("function")
        if isinstance(fn, dict):
            if isinstance(fn.get("name"), str):
                if not acc["name"]:
                    acc["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                acc["arguments"] += fn["arguments"]


def _raw_tool_calls(parts: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, acc in sorted(parts.items()):
        if not acc["name"]:
            continue
        rows.append({
            "id": acc["id"] or f"call_{idx}",
            "type": "function",
            "function": {"name": acc["name"], "arguments": acc["arguments"]},
        })
    return rows


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
    if should_interrupt is not None and should_interrupt():
        raise MistralChatInterrupted("")

    body = _body(provider, model_id, messages, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools)
    request = urllib.request.Request(
        _endpoint(provider),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=_headers(provider),
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=provider.timeout_seconds)
    except urllib.error.HTTPError as exc:
        raise MistralChatError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise MistralChatError(f"connection failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise MistralChatError(f"request timed out after {provider.timeout_seconds:g}s") from exc

    with _InterruptibleResponse(response, should_interrupt) as response:
        content_type = response.headers.get("Content-Type", "").lower()
        if (not provider.stream) or ("text/event-stream" not in content_type):
            try:
                payload = json.loads(_read_response_bytes(response, should_interrupt).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MistralChatError("response was not valid JSON/SSE") from exc
            if not isinstance(payload, dict):
                raise MistralChatError("response JSON root was not an object")
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                raise MistralChatError(error["message"])
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise MistralChatError("response did not contain choices[0]")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise MistralChatError("response did not contain choices[0].message")
            text = _visible_text(message.get("content"))
            reasoning = _thinking_text(message.get("content"))
            if reasoning and on_reasoning is not None:
                on_reasoning(reasoning)
            if text:
                on_text(text)
            if should_interrupt is not None and should_interrupt():
                raise MistralChatInterrupted(text)
            native = {k: json.loads(json.dumps(v)) for k, v in message.items() if k in {"role", "content", "tool_calls"}}
            native.setdefault("role", "assistant")
            return ChatTurn(
                text=text,
                tool_calls=_parse_tool_calls(message.get("tool_calls")),
                usage=_usage(payload.get("usage")),
                served_model=str(payload.get("model") or model_id),
                provider_context=_provider_context(provider, native),
            )

        text_parts: list[str] = []
        native_content: list[dict[str, Any]] = []
        call_parts: dict[int, dict[str, str]] = {}
        usage = UsageInfo()
        served_model = model_id
        for raw_line in _iter_response_lines(response, should_interrupt):
            if should_interrupt is not None and should_interrupt():
                raise MistralChatInterrupted("".join(text_parts))
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
                raise MistralChatError(error["message"])
            if isinstance(event.get("model"), str) and event["model"]:
                served_model = event["model"]
            if isinstance(event.get("usage"), dict):
                usage = _usage(event["usage"])
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            before = _visible_text(native_content)
            before_reasoning = _thinking_text(native_content)
            _merge_content(native_content, content)
            after = _visible_text(native_content)
            after_reasoning = _thinking_text(native_content)
            reasoning_delta = after_reasoning[len(before_reasoning):] if after_reasoning.startswith(before_reasoning) else ""
            if reasoning_delta and on_reasoning is not None:
                on_reasoning(reasoning_delta)
            visible_delta = after[len(before):] if after.startswith(before) else ""
            if visible_delta:
                text_parts.append(visible_delta)
                on_text(visible_delta)
            _append_tool_delta(call_parts, delta.get("tool_calls"))

        if should_interrupt is not None and should_interrupt():
            raise MistralChatInterrupted("".join(text_parts))
        raw_calls = _raw_tool_calls(call_parts)
        native: dict[str, Any] = {"role": "assistant", "content": native_content if native_content else "".join(text_parts)}
        if raw_calls:
            native["tool_calls"] = raw_calls
        return ChatTurn(
            text="".join(text_parts),
            tool_calls=_parse_tool_calls(raw_calls),
            usage=usage,
            served_model=served_model,
            provider_context=_provider_context(provider, native),
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
