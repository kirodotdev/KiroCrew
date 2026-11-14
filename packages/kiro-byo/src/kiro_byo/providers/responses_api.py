"""Native OpenAI/DeepSeek Responses API transport.

The Responses API preserves reasoning/function-call items as first-class objects
instead of flattening them into Chat Completions messages.  Keeping those items
in the Kiro session improves multi-step agent/tool fidelity and lets provider
prompt caches see a stable, authentic prefix.
"""
from __future__ import annotations

import copy
import json
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import make_chat_text, ChatTurn, ProviderError, ProviderInterrupted, ProviderTransportError, ToolCall, UsageInfo, matching_context as _matching_context
from .capabilities import ResponsesStatePolicy, responses_capabilities
from .http_sse import (
    InterruptibleResponse as _InterruptibleResponse,
    open_with_state_fallback as _open_with_state_fallback,
    iter_response_lines as _iter_response_lines,
    read_response_bytes as _read_response_bytes,
    safe_request_url as _safe_request_url,
    standard_json_headers as _standard_json_headers,
)


class ResponsesAPIError(ProviderError):
    pass


class ResponsesAPIInterrupted(ProviderInterrupted, ResponsesAPIError):
    pass



def _apply_responses_state_policy(
    body: dict[str, Any], policy: ResponsesStatePolicy, *, cache_key: str
) -> None:
    if policy.store is True:
        body["store"] = True
    elif policy.store is False:
        body.setdefault("store", False)
    else:
        body.pop("store", None)
    if policy.drop_cache_key:
        body.pop("prompt_cache_key", None)
    elif policy.send_cache_key and cache_key:
        body.setdefault("prompt_cache_key", cache_key)
    if policy.drop_previous_response_id:
        body.pop("previous_response_id", None)
    if policy.include_encrypted_reasoning:
        include = body.get("include") if isinstance(body.get("include"), list) else []
        if "reasoning.encrypted_content" not in include:
            include = [*include, "reasoning.encrypted_content"]
        body["include"] = include


def _endpoint(provider: ProviderSpec) -> str:
    return f"{provider.base_url.rstrip('/')}/responses"


def _headers(provider: ProviderSpec) -> dict[str, str]:
    headers = _standard_json_headers(provider)
    for name, value in responses_capabilities(provider.type).request_headers:
        headers.setdefault(name, value)
    return headers


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
            out.append(block["text"])
    return "".join(out)


def _content_parts(content: Any, *, assistant: bool = False) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "output_text" if assistant else "input_text", "text": content}] if content else []
    if not isinstance(content, list):
        text = str(content or "")
        return [{"type": "output_text" if assistant else "input_text", "text": text}] if text else []
    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str) and text:
                out.append({"type": "output_text" if assistant else "input_text", "text": text})
        elif kind in {"image_url", "input_image"} and not assistant:
            value = block.get("image_url")
            if isinstance(value, dict):
                value = value.get("url")
            if not isinstance(value, str):
                value = block.get("url") if isinstance(block.get("url"), str) else ""
            if value:
                part: dict[str, Any] = {"type": "input_image", "image_url": value}
                detail = block.get("detail")
                if isinstance(detail, str) and detail in {"low", "high", "original", "auto"}:
                    part["detail"] = detail
                out.append(part)
    return out




def _stateful_tail(provider: ProviderSpec, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Return the newest provider response id and messages after its anchor.

    Qwen Responses and current Hunyuan/TokenHub native Responses support
    ``previous_response_id``.  Reusing the provider's server-side continuation
    avoids retransmitting the whole Kiro history on every agent/tool step while
    keeping a stateless full-history fallback when the remote id expires.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or str(message.get("role") or "") != "assistant":
            continue
        context = _matching_context(message, provider)
        response_id = str(context.get("response_id") or "") if context else ""
        if response_id:
            return response_id, messages[index + 1:]
    return "", messages


def _input_items(provider: ProviderSpec, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            text = _text_from_content(message.get("content"))
            if text:
                instructions.append(text)
            continue
        if role == "assistant":
            context = _matching_context(message, provider)
            raw_output = context.get("output") if context else None
            if isinstance(raw_output, list) and raw_output:
                # Responses items (reasoning/function/message) are valid input
                # items on the next stateless turn and must remain unmodified.
                items.extend(json.loads(json.dumps(raw_output)))
                continue
            parts = _content_parts(message.get("content"), assistant=True)
            if parts:
                items.append({"role": "assistant", "content": parts})
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, list):
                for idx, call in enumerate(raw_calls):
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function")
                    if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
                        continue
                    args = fn.get("arguments")
                    if not isinstance(args, str):
                        args = json.dumps(args or {}, ensure_ascii=False, separators=(",", ":"))
                    items.append({
                        "type": "function_call",
                        "call_id": str(call.get("id") or f"call_{idx}"),
                        "name": fn["name"],
                        "arguments": args,
                    })
            continue
        if role == "tool":
            output = message.get("content")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False) if output is not None else ""
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": output,
            })
            continue
        if role in {"user", "developer"}:
            parts = _content_parts(message.get("content"), assistant=False)
            if parts:
                items.append({"role": role, "content": parts})
            continue
        text = _text_from_content(message.get("content"))
        if text:
            items.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
    return "\n\n".join(instructions), items


def _tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            continue
        row: dict[str, Any] = {
            "type": "function",
            "name": fn["name"],
            "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}},
        }
        if isinstance(fn.get("description"), str) and fn.get("description"):
            row["description"] = fn["description"]
        # Strict is useful when a schema explicitly asks for it, but should not
        # be fabricated for third-party schemas that were not authored for it.
        if isinstance(fn.get("strict"), bool):
            row["strict"] = fn["strict"]
        out.append(row)
    return out


def _usage(raw: Any) -> UsageInfo:
    if not isinstance(raw, dict):
        return UsageInfo()
    def iv(v: Any) -> int:
        return max(0, int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf")) else 0
    inp = raw.get("input_tokens_details") if isinstance(raw.get("input_tokens_details"), dict) else {}
    out = raw.get("output_tokens_details") if isinstance(raw.get("output_tokens_details"), dict) else {}
    return UsageInfo(
        input_tokens=iv(raw.get("input_tokens")),
        output_tokens=iv(raw.get("output_tokens")),
        cached_tokens=iv(inp.get("cached_tokens")),
        reasoning_tokens=iv(out.get("reasoning_tokens")),
        cached_tokens_reported="cached_tokens" in inp,
        reasoning_tokens_reported="reasoning_tokens" in out,
    )


def _output_content_text(
    output: Any, *, item_type: str, content_key: str, part_types: frozenset[str]
) -> str:
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != item_type:
            continue
        content = item.get(content_key)
        if isinstance(content, list):
            parts.extend(
                part["text"] for part in content
                if isinstance(part, dict) and part.get("type") in part_types
                and isinstance(part.get("text"), str)
            )
    return "".join(parts)


def _output_text(output: Any) -> str:
    return _output_content_text(output, item_type="message", content_key="content", part_types=frozenset({"output_text", "text"}))


def _reasoning_summary(output: Any) -> str:
    return _output_content_text(output, item_type="reasoning", content_key="summary", part_types=frozenset({"summary_text", "text"}))


def _tool_calls(output: Any) -> tuple[ToolCall, ...]:
    if not isinstance(output, list):
        return ()
    result: list[ToolCall] = []
    for idx, item in enumerate(output):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = item.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False, separators=(",", ":"))
        result.append(ToolCall(str(item.get("call_id") or item.get("id") or f"call_{idx}"), name, args))
    return tuple(result)


def _reasoning_effort(provider: ProviderSpec, model: ModelSpec | None, thinking_mode: str, effort: str | None) -> str:
    capabilities = responses_capabilities(provider.type)
    if thinking_mode == "off":
        if model is not None and model.reasoning_can_disable is True:
            declared = str(model.reasoning_disable_value or "").strip().lower()
            # Responses `reasoning.effort` is not the same as a native
            # `thinking.type` switch. Never send `disabled` as an effort.
            if declared == "none":
                return declared
        return capabilities.reasoning_off_value
    if effort and (model is None or not model.effort_supported_levels or effort in model.effort_supported_levels):
        return effort
    # `enabled`/`adaptive` are thinking switch literals, not reasoning
    # effort levels on an effort-based Responses wire.
    if thinking_mode == "on" and model is not None and model.reasoning_enable_value in model.effort_supported_levels:
        return model.reasoning_enable_value
    return ""


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
    allow_previous: bool = True,
) -> dict[str, Any]:
    capabilities = responses_capabilities(provider.type)
    previous_response_id = ""
    input_messages = messages
    if allow_previous and capabilities.supports_previous_response_id:
        previous_response_id, input_messages = _stateful_tail(provider, messages)
    instructions, items = _input_items(provider, input_messages)
    body: dict[str, Any] = copy.deepcopy(dict(provider.extra_body))
    # Distinct native Responses families use either reasoning.effort or a
    # thinking switch, never both. An explicit Off discards the stale opposite
    # wire from an older saved provider configuration before resolving policy.
    if thinking_mode == "off":
        body.pop("thinking", None)
        body.pop("reasoning", None)
    body.update({"model": model_id, "input": items, "stream": provider.stream})
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    if instructions:
        body["instructions"] = instructions
    if provider.temperature is not None:
        body["temperature"] = provider.temperature
    tool_rows = _tools(tools)
    if tool_rows:
        body["tools"] = tool_rows
        body.setdefault("tool_choice", "auto")
    if capabilities.reasoning_wire == "thinking-toggle":
        # Some Responses transports expose a deep-thinking toggle instead of an
        # effort vocabulary. Only emit it when the model profile proves support.
        if model is not None and model.reasoning_supported is True:
            if thinking_mode == "off" and model.reasoning_can_disable is True:
                body["thinking"] = {"type": model.reasoning_disable_value or "disabled"}
            elif thinking_mode in {"on", "auto"}:
                body["thinking"] = {"type": model.reasoning_enable_value or model.reasoning_auto_value or "enabled"}
    else:
        wire_effort = _reasoning_effort(provider, model, thinking_mode, reasoning_effort)
        if model is not None and model.reasoning_can_disable is True:
            if thinking_mode == "on" and not wire_effort and model.reasoning_default_enabled is not True:
                raise ResponsesAPIError(
                    f"{model_id}: no verified reasoning-effort wire value can enable thinking"
                )
            if thinking_mode == "off" and not wire_effort:
                raise ResponsesAPIError(
                    f"{model_id}: no verified reasoning-effort wire value can disable thinking"
                )
        if wire_effort:
            body["reasoning"] = {"effort": wire_effort}
            if wire_effort != "none":
                for key in capabilities.strip_when_reasoning:
                    body.pop(key, None)
    if model is not None and model.max_output_tokens:
        body.setdefault("max_output_tokens", model.max_output_tokens)
    if capabilities.state_policy is not None:
        _apply_responses_state_policy(body, capabilities.state_policy, cache_key=cache_key)
    return body


def _error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str):
            return msg
    if isinstance(error, str):
        return error
    return ""


def _turn_from_response(provider: ProviderSpec, model_id: str, response: dict[str, Any], *, emitted_text: bool, emitted_reasoning: bool, on_text: Callable[[str], None], on_reasoning: Callable[[str], None] | None = None) -> ChatTurn:
    error = _error(response)
    if error:
        raise ResponsesAPIError(error)
    output = response.get("output") if isinstance(response.get("output"), list) else []
    text = _output_text(output)
    reasoning = _reasoning_summary(output)
    if reasoning and on_reasoning is not None and not emitted_reasoning:
        on_reasoning(reasoning)
    if text and not emitted_text:
        on_text(text)
    return ChatTurn(
        text=text,
        tool_calls=_tool_calls(output),
        usage=_usage(response.get("usage")),
        served_model=str(response.get("model") or model_id),
        provider_context={
            "provider_id": provider.id,
            "transport": provider.type,
            "data": {"output": output, "response_id": str(response.get("id") or "")},
        },
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
) -> ChatTurn:
    if should_interrupt is not None and should_interrupt():
        raise ResponsesAPIInterrupted("")
    body = _body(provider, model_id, messages, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, cache_key=cache_key, tools=tools)

    def _open(payload: dict[str, Any]):
        req = urllib.request.Request(_safe_request_url(_endpoint(provider)), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=_headers(provider), method="POST")
        return urllib.request.urlopen(req, timeout=provider.timeout_seconds)

    response = _open_with_state_fallback(
        body, open_request=_open,
        state_field="previous_response_id",
        retry_statuses=responses_capabilities(provider.type).previous_response_retry_statuses,
        make_stateless=lambda: _body(
            provider, model_id, messages, reasoning_effort=reasoning_effort,
            thinking_mode=thinking_mode, model=model, cache_key=cache_key,
            tools=tools, allow_previous=False,
        ),
        should_interrupt=should_interrupt, interrupted_type=ResponsesAPIInterrupted,
        error_type=ResponsesAPIError, timeout_seconds=provider.timeout_seconds,
    )

    with _InterruptibleResponse(response, should_interrupt) as response:
        ctype = response.headers.get("Content-Type", "").lower()
        if not provider.stream or "text/event-stream" not in ctype:
            try:
                payload = json.loads(_read_response_bytes(response, should_interrupt, interrupted_type=ResponsesAPIInterrupted).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ResponsesAPIError("response was not valid JSON") from exc
            if not isinstance(payload, dict):
                raise ResponsesAPIError("response JSON root was not an object")
            turn = _turn_from_response(provider, model_id, payload, emitted_text=False, emitted_reasoning=False, on_text=on_text, on_reasoning=on_reasoning)
            if should_interrupt is not None and should_interrupt():
                raise ResponsesAPIInterrupted(turn.text)
            return turn

        text_parts: list[str] = []
        reasoning_streamed = False
        final_response: dict[str, Any] | None = None
        output_done: list[dict[str, Any]] = []
        served_model = model_id
        usage = UsageInfo()
        stream_finished = False
        for raw in _iter_response_lines(response, should_interrupt):
            if should_interrupt is not None and should_interrupt():
                raise ResponsesAPIInterrupted("".join(text_parts))
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or line.startswith("event:") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                stream_finished = True
                continue
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type") or "")
            err = _error(event)
            if err:
                raise ResponsesAPIError(err)
            if etype in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"} and isinstance(event.get("delta"), str):
                reasoning_streamed = True
                if on_reasoning is not None:
                    on_reasoning(event["delta"])
            elif etype == "response.output_text.delta" and isinstance(event.get("delta"), str):
                delta = event["delta"]
                text_parts.append(delta)
                on_text(delta)
            elif etype == "response.output_item.done" and isinstance(event.get("item"), dict):
                output_done.append(event["item"])
            if etype in {"response.completed", "response.incomplete", "response.failed"}:
                stream_finished = True
                candidate = event.get("response")
                if isinstance(candidate, dict):
                    final_response = candidate
                if etype == "response.failed":
                    raise ResponsesAPIError(_error(candidate) or "provider response failed")
            if isinstance(event.get("model"), str):
                served_model = event["model"]
            if isinstance(event.get("usage"), dict):
                usage = _usage(event["usage"])

        if should_interrupt is not None and should_interrupt():
            raise ResponsesAPIInterrupted("".join(text_parts))
        if not stream_finished:
            raise ProviderTransportError("upstream streaming response closed without a completion marker")
        if final_response is not None:
            return _turn_from_response(provider, model_id, final_response, emitted_text=bool(text_parts), emitted_reasoning=reasoning_streamed, on_text=on_text, on_reasoning=on_reasoning)
        # Defensive fallback for proxies that omit the terminal response object.
        text = "".join(text_parts) or _output_text(output_done)
        return ChatTurn(
            text=text,
            tool_calls=_tool_calls(output_done),
            usage=usage,
            served_model=served_model,
            provider_context={"provider_id": provider.id, "transport": provider.type, "data": {"output": output_done}},
        )


# Legacy text/compaction API delegates to the current replaceable Turn entry.
chat_completion = make_chat_text(lambda: chat_completion_turn)
