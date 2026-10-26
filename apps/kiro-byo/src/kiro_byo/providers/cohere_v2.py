"""Native Cohere Chat V2 transport.

Cohere's native API exposes reasoning blocks, multimodal content and tool-plan /
tool-call state that its OpenAI compatibility layer flattens.  Kiro BYO keeps the
assistant message exactly as returned so multi-step tool loops retain that state.
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
from .http_sse import InterruptibleResponse as _InterruptibleResponse, iter_response_lines as _iter_response_lines, read_response_bytes as _read_response_bytes, post_json as _post_json


class CohereV2Error(ProviderError):
    pass


class CohereV2Interrupted(ProviderInterrupted, CohereV2Error):
    def __init__(self, partial_text: str = "") -> None:
        ProviderInterrupted.__init__(self, partial_text)


def _endpoint(provider: ProviderSpec) -> str:
    base = provider.base_url.rstrip("/")
    if base.endswith("/v2"):
        return base + "/chat"
    return base + "/v2/chat"


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
    result: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role == "assistant":
            ctx = _matching_context(msg, provider)
            raw_message = ctx.get("message") if ctx else None
            if isinstance(raw_message, dict):
                # Round-trip Cohere's tool_plan, thinking blocks and tool_calls.
                result.append(json.loads(json.dumps(raw_message)))
                continue
            out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            if isinstance(msg.get("tool_calls"), list):
                out["tool_calls"] = msg["tool_calls"]
            result.append(out)
            continue
        if role == "tool":
            value = msg.get("content")
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False) if value is not None else ""
            result.append({
                "role": "tool",
                "tool_call_id": str(msg.get("tool_call_id") or ""),
                "content": [{"type": "document", "document": {"data": value}}],
            })
            continue
        if role in {"system", "user"}:
            # Cohere V2 accepts the same text/image_url content blocks used by
            # Crew. Preserve HTTP/data URLs exactly rather than re-encoding them.
            result.append({"role": role, "content": msg.get("content") or ""})
            continue
        if role == "developer":
            result.append({"role": "system", "content": msg.get("content") or ""})
    return result


def _tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            continue
        fn = tool["function"]
        if not isinstance(fn.get("name"), str):
            continue
        row = {
            "type": "function",
            "function": {
                "name": fn["name"],
                "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type":"object","properties":{}},
            },
        }
        if isinstance(fn.get("description"), str):
            row["function"]["description"] = fn["description"]
        out.append(row)
    return out


def _thinking(model: ModelSpec | None, thinking_mode: str) -> dict[str, Any] | None:
    if model is None or model.reasoning_supported is not True:
        return None
    if thinking_mode == "off":
        if model.reasoning_can_disable is True:
            return {"type": model.reasoning_disable_value or "disabled"}
        return None
    if thinking_mode in {"on", "auto", ""}:
        # Native Cohere currently exposes enable/disable plus a token budget,
        # not a universal named effort scale. Prefer the concrete model's
        # advertised wire literal so a future model can change vocabulary
        # without another provider-brand special case.
        return {"type": model.reasoning_enable_value or model.reasoning_auto_value or "enabled"}
    return None


def _body(provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], *, thinking_mode: str, model: ModelSpec | None, tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    body = dict(provider.extra_body)
    body.update({"model": model_id, "messages": _messages(provider, messages), "stream": provider.stream})
    if provider.temperature is not None:
        body["temperature"] = provider.temperature
    tool_rows = _tools(tools)
    if tool_rows:
        body["tools"] = tool_rows
    thinking = _thinking(model, thinking_mode)
    if thinking is not None:
        body["thinking"] = thinking
    if model is not None and model.max_output_tokens:
        body.setdefault("max_tokens", model.max_output_tokens)
    return body


def _text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            out.append(block["text"])
    return "".join(out)


def _tool_calls(message: Any) -> tuple[ToolCall, ...]:
    if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
        return ()
    out: list[ToolCall] = []
    for idx, row in enumerate(message["tool_calls"]):
        if not isinstance(row, dict) or not isinstance(row.get("function"), dict):
            continue
        fn = row["function"]
        if not isinstance(fn.get("name"), str):
            continue
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False, separators=(",", ":"))
        out.append(ToolCall(str(row.get("id") or f"call_{idx}"), fn["name"], args))
    return tuple(out)


def _usage(raw: Any) -> UsageInfo:
    if not isinstance(raw, dict):
        return UsageInfo()
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    def iv(v: Any) -> int:
        return max(0, int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf")) else 0
    return UsageInfo(input_tokens=iv(tokens.get("input_tokens")), output_tokens=iv(tokens.get("output_tokens")))



def _thinking_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
            out.append(block["thinking"])
    return "".join(out)

def _turn(provider: ProviderSpec, model_id: str, message: dict[str, Any], usage: Any, on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, already_emitted: bool) -> ChatTurn:
    text = _text(message)
    reasoning = _thinking_text(message)
    if reasoning and on_reasoning is not None and not already_emitted:
        on_reasoning(reasoning)
    if text and not already_emitted:
        on_text(text)
    return ChatTurn(
        text=text,
        tool_calls=_tool_calls(message),
        usage=_usage(usage),
        served_model=model_id,
        provider_context={"provider_id": provider.id, "transport": provider.type, "data": {"message": message}},
    )


def chat_completion_turn(provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "", tools: list[dict[str, Any]] | None = None, should_interrupt: Callable[[], bool] | None = None) -> ChatTurn:
    del reasoning_effort, cache_key  # no verified native named-effort/cache-key mapping
    if should_interrupt is not None and should_interrupt():
        raise CohereV2Interrupted("")
    body = _body(provider, model_id, messages, thinking_mode=thinking_mode, model=model, tools=tools)
    response = _post_json(
        url=_endpoint(provider), headers=_headers(provider), body=body,
        timeout_seconds=provider.timeout_seconds, error_type=CohereV2Error,
        opener=urllib.request.urlopen,
    )

    with _InterruptibleResponse(response, should_interrupt) as response:
        ctype = response.headers.get("Content-Type", "").lower()
        if not provider.stream or "text/event-stream" not in ctype:
            try:
                payload = json.loads(_read_response_bytes(response, should_interrupt, interrupted_type=CohereV2Interrupted).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CohereV2Error("response was not valid JSON") from exc
            if not isinstance(payload, dict):
                raise CohereV2Error("response JSON root was not an object")
            message = payload.get("message")
            if not isinstance(message, dict):
                raise CohereV2Error("response did not contain message")
            if should_interrupt is not None and should_interrupt():
                raise CohereV2Interrupted(_text(message))
            return _turn(provider, model_id, message, payload.get("usage"), on_text, on_reasoning=on_reasoning, already_emitted=False)

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_plan_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: Any = None
        response_id = ""
        for raw_line in _iter_response_lines(response, should_interrupt):
            if should_interrupt is not None and should_interrupt():
                raise CohereV2Interrupted("".join(text_parts))
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
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
            et = str(event.get("type") or "")
            if et == "message-start":
                response_id = str(event.get("id") or "")
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            dm = delta.get("message") if isinstance(delta.get("message"), dict) else {}
            if et == "content-delta":
                content = dm.get("content") if isinstance(dm.get("content"), dict) else {}
                chunk = content.get("text") if isinstance(content.get("text"), str) else ""
                if chunk:
                    text_parts.append(chunk); on_text(chunk)
                thought = content.get("thinking") if isinstance(content.get("thinking"), str) else ""
                if thought:
                    thinking_parts.append(thought)
                    if on_reasoning is not None:
                        on_reasoning(thought)
            elif et == "tool-plan-delta":
                plan = dm.get("tool_plan") if isinstance(dm.get("tool_plan"), str) else ""
                if plan:
                    tool_plan_parts.append(plan)
            elif et == "tool-call-start":
                raw_idx = event.get("index")
                idx = raw_idx if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) else 0
                row = dm.get("tool_calls") if isinstance(dm.get("tool_calls"), dict) else {}
                fn = row.get("function") if isinstance(row.get("function"), dict) else {}
                calls[idx] = {"id": str(row.get("id") or f"call_{idx}"), "name": str(fn.get("name") or ""), "arguments": str(fn.get("arguments") or "")}
            elif et == "tool-call-delta":
                raw_idx = event.get("index")
                idx = raw_idx if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) else 0
                row = dm.get("tool_calls") if isinstance(dm.get("tool_calls"), dict) else {}
                fn = row.get("function") if isinstance(row.get("function"), dict) else {}
                calls.setdefault(idx, {"id": f"call_{idx}", "name": "", "arguments": ""})["arguments"] += str(fn.get("arguments") or "")
            elif et == "message-end":
                usage = delta.get("usage")

        content: list[dict[str, Any]] = []
        if thinking_parts:
            content.append({"type": "thinking", "thinking": "".join(thinking_parts)})
        if text_parts:
            content.append({"type": "text", "text": "".join(text_parts)})
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_plan_parts:
            message["tool_plan"] = "".join(tool_plan_parts)
        if calls:
            message["tool_calls"] = [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for _, c in sorted(calls.items()) if c["name"]
            ]
        if response_id:
            message["id"] = response_id
        return _turn(provider, model_id, message, usage, on_text, on_reasoning=on_reasoning, already_emitted=True)


def chat_completion(provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, reasoning_effort: str | None = None, thinking_mode: str = "", model: ModelSpec | None = None, cache_key: str = "") -> str:
    return chat_completion_turn(provider, model_id, messages, on_text, on_reasoning=on_reasoning, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key).text
