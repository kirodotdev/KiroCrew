"""High-fidelity Zhipu/Z.AI GLM Chat Completions adapter.

The public GLM API is OpenAI-shaped, but current GLM models expose native
extensions that a generic compatibility adapter would drop: ``reasoning_content``,
``thinking``, ``reasoning_effort`` and streamed tool calls.  This adapter keeps
those fields intact between agent/tool turns so GLM can reuse its own reasoning
state and provider-side implicit context cache instead of reconstructing it.
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
    _usage_info,
)


class ZhipuChatError(ProviderError):
    pass


class ZhipuChatInterrupted(ProviderInterrupted, ZhipuChatError):
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
    if not isinstance(raw, dict) or raw.get("provider_id") != provider.id or raw.get("transport") != provider.type:
        return None
    data = raw.get("data")
    return data if isinstance(data, dict) else None


def _messages(provider: ProviderSpec, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") == "assistant":
            context = _matching_context(message, provider)
            native = context.get("message") if context else None
            if isinstance(native, dict) and native.get("role") == "assistant":
                # Preserve provider-native reasoning/tool state. GLM agent models
                # can continue from reasoning_content rather than rebuilding it.
                out.append(json.loads(json.dumps(native)))
                continue
        out.append({k: v for k, v in message.items() if not str(k).startswith("_kiro_")})
    return out


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

    # Keep user-configured sampling only when explicitly requested. GLM's modern
    # reasoning families are trained around temperature 1, so Kiro never invents
    # another value here.
    if provider.temperature is not None:
        body["temperature"] = provider.temperature

    if model is not None and model.reasoning_supported is True:
        if thinking_mode == "off" and model.reasoning_can_disable is True and model.reasoning_disable_value:
            body["thinking"] = {"type": model.reasoning_disable_value}
        elif thinking_mode in {"on", "auto"} or model.reasoning_can_disable is False:
            enable = str(model.reasoning_enable_value or "enabled").strip()
            if enable:
                # clear_thinking=false is the agent-oriented path: retain prior
                # reasoning_content over tool loops instead of discarding it.
                body["thinking"] = {"type": enable, "clear_thinking": False}
        if (
            thinking_mode != "off"
            and reasoning_effort
            and reasoning_effort in model.effort_supported_levels
        ):
            body["reasoning_effort"] = reasoning_effort

    if tools:
        body["tools"] = tools
        # Z.AI's first-party tool interface documents auto as the portable mode.
        body["tool_choice"] = "auto"
        mid = model_id.lower()
        # Stream function arguments on current agent-capable families. Unknown
        # catalogue entries stay conservative rather than receiving a guessed knob.
        if provider.stream and (
            mid.startswith("glm-5") or mid.startswith("glm-4.7") or mid.startswith("glm-4.6")
        ):
            body["tool_stream"] = True
    return body


def _visible_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text"))
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


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


def _provider_context(provider: ProviderSpec, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_id": provider.id,
        "transport": provider.type,
        "data": {"message": json.loads(json.dumps(message))},
    }


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
        if isinstance(raw.get("id"), str) and not acc["id"]:
            acc["id"] = raw["id"]
        fn = raw.get("function")
        if isinstance(fn, dict):
            if isinstance(fn.get("name"), str) and not acc["name"]:
                acc["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                acc["arguments"] += fn["arguments"]


def _raw_tool_calls(parts: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, acc in sorted(parts.items()):
        if acc["name"]:
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
    del cache_key  # GLM's first-party API uses automatic implicit context caching.
    if should_interrupt is not None and should_interrupt():
        raise ZhipuChatInterrupted("")

    body = _body(
        provider, model_id, messages,
        reasoning_effort=reasoning_effort,
        thinking_mode=thinking_mode,
        model=model,
        tools=tools,
    )
    request = urllib.request.Request(
        _endpoint(provider),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=_headers(provider),
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=provider.timeout_seconds)
    except urllib.error.HTTPError as exc:
        raise ZhipuChatError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise ZhipuChatError(f"connection failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ZhipuChatError(f"request timed out after {provider.timeout_seconds:g}s") from exc

    with _InterruptibleResponse(response, should_interrupt) as response:
        ctype = response.headers.get("Content-Type", "").lower()
        if (not provider.stream) or ("text/event-stream" not in ctype):
            try:
                payload = json.loads(_read_response_bytes(response, should_interrupt).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ZhipuChatError("response was not valid JSON/SSE") from exc
            if not isinstance(payload, dict):
                raise ZhipuChatError("response JSON root was not an object")
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                raise ZhipuChatError(error["message"])
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ZhipuChatError("response did not contain choices[0]")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise ZhipuChatError("response did not contain choices[0].message")
            text = _visible_text(message.get("content"))
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning and on_reasoning is not None:
                on_reasoning(reasoning)
            if text:
                on_text(text)
            if should_interrupt is not None and should_interrupt():
                raise ZhipuChatInterrupted(text)
            native = {
                k: json.loads(json.dumps(v))
                for k, v in message.items()
                if k in {"role", "content", "reasoning_content", "tool_calls", "name"}
            }
            native.setdefault("role", "assistant")
            return ChatTurn(
                text=text,
                tool_calls=_parse_tool_calls(message.get("tool_calls")),
                usage=_usage_info(payload.get("usage")),
                served_model=str(payload.get("model") or model_id),
                provider_context=_provider_context(provider, native),
            )

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        call_parts: dict[int, dict[str, str]] = {}
        usage = UsageInfo()
        served_model = model_id
        for raw_line in _iter_response_lines(response, should_interrupt):
            if should_interrupt is not None and should_interrupt():
                raise ZhipuChatInterrupted("".join(text_parts))
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
                raise ZhipuChatError(error["message"])
            if isinstance(event.get("model"), str) and event["model"]:
                served_model = event["model"]
            if isinstance(event.get("usage"), dict):
                usage = _usage_info(event["usage"])
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
                on_text(content)
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                reasoning_parts.append(reasoning)
                if on_reasoning is not None:
                    on_reasoning(reasoning)
            _append_tool_delta(call_parts, delta.get("tool_calls"))

        if should_interrupt is not None and should_interrupt():
            raise ZhipuChatInterrupted("".join(text_parts))
        raw_calls = _raw_tool_calls(call_parts)
        native: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
        if reasoning_parts:
            native["reasoning_content"] = "".join(reasoning_parts)
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
        provider, model_id, messages, on_text,
        on_reasoning=on_reasoning,
        reasoning_effort=reasoning_effort,
        thinking_mode=thinking_mode,
        model=model,
        cache_key=cache_key,
        tools=None,
    ).text
