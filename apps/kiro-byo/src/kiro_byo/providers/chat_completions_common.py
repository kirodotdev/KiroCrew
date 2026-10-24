"""Shared engine for native OpenAI-shaped Chat Completions transports.

Zhipu, Moonshot, Qwen and Xiaomi each have real provider-specific reasoning
controls, but their HTTP response envelope, SSE deltas, function calls, usage
shape and native assistant replay are the same. This module owns that common
wire engine; the vendor modules only build their request body and error types.
"""
from __future__ import annotations

from .. import __version__

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted, ToolCall, UsageInfo
from .http_sse import InterruptibleResponse, iter_response_lines, post_json, read_response_bytes


def endpoint(provider: ProviderSpec) -> str:
    path = provider.chat_completions_path or "/chat/completions"
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{provider.base_url.rstrip('/')}/{path.lstrip('/')}"


def headers(provider: ProviderSpec) -> dict[str, str]:
    out = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if provider.stream else "application/json",
        "User-Agent": f"Kiro-BYO/{__version__}",
    }
    if provider.api_key:
        out["Authorization"] = f"Bearer {provider.api_key}"
    out.update(provider.headers)
    return out


def matching_context(message: dict[str, Any], provider: ProviderSpec) -> dict[str, Any] | None:
    raw = message.get("_kiro_provider_context")
    if not isinstance(raw, dict):
        return None
    if str(raw.get("provider_id") or "") != provider.id:
        return None
    if str(raw.get("transport") or "") != provider.type:
        return None
    data = raw.get("data")
    return data if isinstance(data, dict) else None


def replay_native_messages(provider: ProviderSpec, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replay provider-authentic assistant state only to the transport that made it."""

    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") == "assistant":
            context = matching_context(message, provider)
            native = context.get("message") if context else None
            if isinstance(native, dict) and native.get("role") == "assistant":
                out.append(json.loads(json.dumps(native)))
                continue
        out.append({k: v for k, v in message.items() if not str(k).startswith("_kiro_")})
    return out


def visible_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _no_reasoning(_content: Any) -> str:
    return ""


def _new_text_stream() -> list[str]:
    return []


def _merge_text_stream(state: list[str], incoming: Any) -> None:
    if isinstance(incoming, str) and incoming:
        state.append(incoming)


def _finalize_text_stream(state: list[str], fallback_text: str) -> Any:
    return "".join(state) if state else (fallback_text or None)


@dataclass(frozen=True)
class ChatContentCodec:
    """Inject only the content-shape differences into the shared Chat engine.

    HTTP, SSE, usage and tool-call semantics remain common. A provider whose
    assistant content is structured (Mistral ThinkChunk is the motivating case)
    supplies five pure operations rather than cloning the entire transport.
    """

    new_stream_state: Callable[[], Any]
    merge_stream_content: Callable[[Any, Any], None]
    visible_text: Callable[[Any], str]
    reasoning_text: Callable[[Any], str]
    finalize_stream_content: Callable[[Any, str], Any]


DEFAULT_CHAT_CONTENT_CODEC = ChatContentCodec(
    new_stream_state=_new_text_stream,
    merge_stream_content=_merge_text_stream,
    visible_text=visible_text,
    reasoning_text=_no_reasoning,
    finalize_stream_content=_finalize_text_stream,
)

def usage_info(raw: Any) -> UsageInfo:
    if not isinstance(raw, dict):
        return UsageInfo()

    def _int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, float) and value >= 0:
            return int(value)
        return 0

    prompt_details = raw.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = raw.get("input_tokens_details")
    completion_details = raw.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = raw.get("output_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    if not isinstance(completion_details, dict):
        completion_details = {}
    return UsageInfo(
        input_tokens=_int(raw.get("prompt_tokens", raw.get("input_tokens"))),
        output_tokens=_int(raw.get("completion_tokens", raw.get("output_tokens"))),
        cached_tokens=_int(prompt_details.get("cached_tokens")),
        reasoning_tokens=_int(completion_details.get("reasoning_tokens")),
        cached_tokens_reported="cached_tokens" in prompt_details,
        reasoning_tokens_reported="reasoning_tokens" in completion_details,
    )


def parse_tool_calls(raw_calls: Any) -> tuple[ToolCall, ...]:
    if not isinstance(raw_calls, list):
        return ()
    out: list[ToolCall] = []
    for idx, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function")
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
            continue
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False, separators=(",", ":"))
        out.append(ToolCall(str(raw.get("id") or f"call_{idx}"), fn["name"], args))
    return tuple(out)


def provider_context(provider: ProviderSpec, message: dict[str, Any]) -> dict[str, Any]:
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
        if not acc["name"]:
            continue
        rows.append({
            "id": acc["id"] or f"call_{idx}",
            "type": "function",
            "function": {"name": acc["name"], "arguments": acc["arguments"]},
        })
    return rows


def run_native_chat_completion(
    provider: ProviderSpec,
    model_id: str,
    body: dict[str, Any],
    on_text: Callable[[str], None],
    *,
    on_reasoning: Callable[[str], None] | None,
    should_interrupt: Callable[[], bool] | None,
    error_type: type[ProviderError],
    interrupted_type: type[ProviderInterrupted],
    content_codec: ChatContentCodec = DEFAULT_CHAT_CONTENT_CODEC,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> ChatTurn:
    """Execute the shared native Chat Completions response protocol."""

    if should_interrupt is not None and should_interrupt():
        raise interrupted_type("")

    response = post_json(
        url=endpoint(provider),
        headers=headers(provider),
        body=body,
        timeout_seconds=provider.timeout_seconds,
        error_type=error_type,
        opener=opener,
    )

    with InterruptibleResponse(response, should_interrupt) as response:
        content_type = response.headers.get("Content-Type", "").lower()
        if (not provider.stream) or ("text/event-stream" not in content_type):
            try:
                payload = json.loads(
                    read_response_bytes(
                        response,
                        should_interrupt,
                        interrupted_type=interrupted_type,
                    ).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise error_type("response was not valid JSON/SSE") from exc
            if not isinstance(payload, dict):
                raise error_type("response JSON root was not an object")
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                raise error_type(error["message"])
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise error_type("response did not contain choices[0]")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise error_type("response did not contain choices[0].message")
            content = message.get("content")
            text = content_codec.visible_text(content)
            content_reasoning = content_codec.reasoning_text(content)
            field_reasoning = message.get("reasoning_content")
            if content_reasoning and on_reasoning is not None:
                on_reasoning(content_reasoning)
            if isinstance(field_reasoning, str) and field_reasoning and on_reasoning is not None:
                on_reasoning(field_reasoning)
            if text:
                on_text(text)
            if should_interrupt is not None and should_interrupt():
                raise interrupted_type(text)
            native = {
                k: json.loads(json.dumps(v))
                for k, v in message.items()
                if k in {"role", "content", "reasoning_content", "tool_calls", "name"}
            }
            native.setdefault("role", "assistant")
            return ChatTurn(
                text=text,
                tool_calls=parse_tool_calls(message.get("tool_calls")),
                usage=usage_info(payload.get("usage")),
                served_model=str(payload.get("model") or model_id),
                provider_context=provider_context(provider, native),
            )

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        content_state = content_codec.new_stream_state()
        call_parts: dict[int, dict[str, str]] = {}
        usage = UsageInfo()
        served_model = model_id
        for raw_line in iter_response_lines(response, should_interrupt):
            if should_interrupt is not None and should_interrupt():
                raise interrupted_type("".join(text_parts))
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
                raise error_type(error["message"])
            if isinstance(event.get("model"), str) and event["model"]:
                served_model = event["model"]
            if isinstance(event.get("usage"), dict):
                usage = usage_info(event["usage"])
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            before_text = content_codec.visible_text(content_state)
            before_content_reasoning = content_codec.reasoning_text(content_state)
            content_codec.merge_stream_content(content_state, content)
            after_text = content_codec.visible_text(content_state)
            after_content_reasoning = content_codec.reasoning_text(content_state)
            visible_delta = after_text[len(before_text):] if after_text.startswith(before_text) else ""
            content_reasoning_delta = (
                after_content_reasoning[len(before_content_reasoning):]
                if after_content_reasoning.startswith(before_content_reasoning)
                else ""
            )
            if visible_delta:
                text_parts.append(visible_delta)
                on_text(visible_delta)
            if content_reasoning_delta and on_reasoning is not None:
                on_reasoning(content_reasoning_delta)
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                reasoning_parts.append(reasoning)
                if on_reasoning is not None:
                    on_reasoning(reasoning)
            _append_tool_delta(call_parts, delta.get("tool_calls"))

        if should_interrupt is not None and should_interrupt():
            raise interrupted_type("".join(text_parts))
        raw_calls = _raw_tool_calls(call_parts)
        native: dict[str, Any] = {
            "role": "assistant",
            "content": content_codec.finalize_stream_content(content_state, "".join(text_parts)),
        }
        if reasoning_parts:
            native["reasoning_content"] = "".join(reasoning_parts)
        if raw_calls:
            native["tool_calls"] = raw_calls
        return ChatTurn(
            text="".join(text_parts),
            tool_calls=parse_tool_calls(raw_calls),
            usage=usage,
            served_model=served_model,
            provider_context=provider_context(provider, native),
        )
