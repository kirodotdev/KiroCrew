"""Native Anthropic Messages API adapter.

Unlike the OpenAI compatibility layer this preserves Claude thinking signatures,
native tool_use/tool_result blocks, image content, adaptive thinking and prompt
caching.  Those details are required for high-fidelity multi-step agent turns.
"""
from __future__ import annotations

from .. import __version__

import base64
import json
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted, ToolCall, UsageInfo
from .http_sse import (
    InterruptibleResponse as _InterruptibleResponse,
    http_error_message as _http_error_message,
    iter_response_lines as _iter_response_lines,
    read_response_bytes as _read_response_bytes,
    post_json as _post_json,
)


class AnthropicMessagesError(ProviderError):
    pass


class AnthropicMessagesInterrupted(ProviderInterrupted, AnthropicMessagesError):
    def __init__(self, partial_text: str = "") -> None:
        ProviderInterrupted.__init__(self, partial_text)


def _endpoint(provider: ProviderSpec) -> str:
    base = provider.base_url.rstrip("/")
    return base if base.endswith("/messages") else f"{base}/messages"


def _headers(provider: ProviderSpec, model: ModelSpec | None, thinking_mode: str, tools: bool) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if provider.stream else "application/json",
        "User-Agent": f"Kiro-BYO/{__version__}",
    }
    if provider.type == "minimax-messages":
        # MiniMax's recommended Anthropic-compatible inference path authenticates
        # with Bearer. Its model-list endpoint separately uses X-Api-Key.
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        headers.update(provider.headers)
        return headers

    headers["anthropic-version"] = "2023-06-01"
    if provider.api_key:
        headers["x-api-key"] = provider.api_key
    # Manual extended-thinking Claude 4.x models need the beta to reason between
    # tool results. Modern adaptive models interleave natively and do not need it.
    mid = (model.model_id if model is not None else "").lower()
    enable = (model.reasoning_enable_value if model is not None else "").lower()
    if tools and thinking_mode != "off" and enable == "enabled" and re.search(r"claude-.*-4(?:-|$)", mid):
        headers.setdefault("anthropic-beta", "interleaved-thinking-2025-05-14")
    headers.update(provider.headers)
    return headers


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
            chunks.append(block["text"])
    return "".join(chunks)


def _image_block(url: str) -> dict[str, Any] | None:
    if not url:
        return None
    if url.startswith("data:"):
        match = re.match(r"^data:([^;,]+);base64,(.*)$", url, flags=re.DOTALL)
        if not match:
            return None
        mime, data = match.group(1), match.group(2)
        # Validate base64 cheaply so malformed ACP image blocks fail locally.
        try:
            base64.b64decode(data, validate=True)
        except Exception:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        text = str(content or "")
        return [{"type": "text", "text": text}] if text else []
    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str) and block.get("text"):
            out.append({"type": "text", "text": block["text"]})
        elif kind in {"image_url", "input_image"}:
            value = block.get("image_url")
            if isinstance(value, dict):
                value = value.get("url")
            if not isinstance(value, str):
                value = block.get("url") if isinstance(block.get("url"), str) else ""
            image = _image_block(value)
            if image is not None:
                out.append(image)
    return out


def _context(message: dict[str, Any], provider: ProviderSpec) -> dict[str, Any] | None:
    raw = message.get("_kiro_provider_context")
    if not isinstance(raw, dict) or raw.get("provider_id") != provider.id or raw.get("transport") != provider.type:
        return None
    data = raw.get("data")
    return data if isinstance(data, dict) else None


def _messages(provider: ProviderSpec, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    systems: list[str] = []
    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            text = _text(message.get("content"))
            if text:
                systems.append(text)
            continue
        if role == "assistant":
            ctx = _context(message, provider)
            raw_blocks = ctx.get("content") if ctx else None
            if isinstance(raw_blocks, list) and raw_blocks:
                out.append({"role": "assistant", "content": json.loads(json.dumps(raw_blocks))})
                continue
            blocks: list[dict[str, Any]] = []
            text = _text(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for idx, call in enumerate(calls):
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function")
                    if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
                        continue
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    if not isinstance(args, dict):
                        args = {"value": args}
                    blocks.append({"type": "tool_use", "id": str(call.get("id") or f"toolu_{idx}"), "name": fn["name"], "input": args})
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            content = message.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False) if content is not None else ""
            block = {"type": "tool_result", "tool_use_id": str(message.get("tool_call_id") or ""), "content": content}
            # Anthropic requires tool results in a user message. Merge adjacent
            # result messages so parallel tool calls remain one user turn.
            if out and out[-1].get("role") == "user" and isinstance(out[-1].get("content"), list) and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        blocks = _user_blocks(message.get("content"))
        if blocks:
            out.append({"role": "user", "content": blocks})
    return "\n\n".join(systems), out


def _tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            continue
        row: dict[str, Any] = {
            "name": fn["name"],
            "input_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}},
        }
        if isinstance(fn.get("description"), str) and fn.get("description"):
            row["description"] = fn["description"]
        out.append(row)
    return out


def _usage(raw: Any) -> UsageInfo:
    if not isinstance(raw, dict):
        return UsageInfo()
    def iv(v: Any) -> int:
        return max(0, int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf")) else 0
    cache_read = iv(raw.get("cache_read_input_tokens"))
    cache_create = iv(raw.get("cache_creation_input_tokens"))
    uncached = iv(raw.get("input_tokens"))
    # Anthropic reports cache read/write tokens separately from input_tokens.
    total_input = uncached + cache_read + cache_create
    cache_reported = "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw
    return UsageInfo(
        input_tokens=total_input,
        output_tokens=iv(raw.get("output_tokens")),
        cached_tokens=cache_read,
        cache_write_tokens=cache_create,
        reasoning_tokens=0,
        cached_tokens_reported="cache_read_input_tokens" in raw,
        cache_write_tokens_reported="cache_creation_input_tokens" in raw,
        reasoning_tokens_reported=False,
    )


def _max_tokens(model: ModelSpec | None, provider: ProviderSpec) -> int:
    explicit = provider.extra_body.get("max_tokens")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    if model is not None and model.max_output_tokens:
        return model.max_output_tokens
    return 16384


def _thinking_config(model: ModelSpec | None, thinking_mode: str, effort: str | None, max_tokens: int) -> dict[str, Any] | None:
    if model is None or model.reasoning_supported is not True:
        return None
    if thinking_mode == "off":
        if model.reasoning_can_disable is True and model.reasoning_disable_value:
            return {"type": model.reasoning_disable_value}
        return None
    # Always-on models often work best when the field is omitted; adaptive-only
    # profiles explicitly advertise adaptive so Kiro can request the native mode.
    enable = (model.reasoning_enable_value or model.reasoning_auto_value).strip()
    if not enable and model.reasoning_mode == "adaptive":
        enable = "adaptive"
    if enable == "enabled":
        # Legacy extended thinking uses a token budget, distinct from effort.
        budgets = {"low": 2048, "medium": 4096, "high": 8192, "xhigh": 16384, "max": 32768}
        requested = effort if effort in budgets else model.effort_default_level
        budget = budgets.get(requested, 8192)
        # Anthropic requires max_tokens > budget_tokens outside interleaved cases.
        budget = max(1024, min(budget, max(1024, max_tokens - 1)))
        return {"type": "enabled", "budget_tokens": budget}
    if enable in {"adaptive", "between_tools"}:
        return {"type": enable}
    # Default-enabled/always-on models can safely use provider defaults.
    return None


def _minimax_thinking_config(model: ModelSpec | None, thinking_mode: str) -> dict[str, Any] | None:
    if model is None or model.reasoning_supported is not True:
        return None
    if thinking_mode == "off":
        # MiniMax-M3 turns thinking off by omitting the field; sending
        # {type:"disabled"} is not equivalent for all MiniMax generations.
        if model.reasoning_can_disable is True and model.reasoning_disable_value == "__omit__":
            return None
        if model.reasoning_can_disable is True and model.reasoning_disable_value:
            return {"type": model.reasoning_disable_value}
        return None
    enable = str(model.reasoning_enable_value or "").strip()
    if enable in {"adaptive", "enabled"}:
        return {"type": enable}
    # M2.x is always-thinking and is most compatible when the thinking field is
    # omitted. M3.1 has an explicit adaptive profile above, so it is unaffected.
    return None


def _add_minimax_prompt_cache(body: dict[str, Any]) -> None:
    """Add provider-native explicit cache breakpoints without mutating history.

    MiniMax follows Anthropic's block cache semantics. Cache the stable
    tools/system prefix first, then (when possible) the latest completed history
    text block before the current user turn. This keeps the agent/tool schema and
    long conversation prefix cheap without caching transient request-only data.
    """
    system = body.get("system")
    if isinstance(system, str) and system:
        body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(system, list) and system:
        for block in reversed(system):
            if isinstance(block, dict):
                block.setdefault("cache_control", {"type": "ephemeral"})
                break
    elif isinstance(body.get("tools"), list) and body["tools"]:
        last_tool = body["tools"][-1]
        if isinstance(last_tool, dict):
            last_tool.setdefault("cache_control", {"type": "ephemeral"})

    rows = body.get("messages")
    if not isinstance(rows, list) or len(rows) < 2:
        return
    # Do not place the conversation breakpoint on the final user request. Mark a
    # completed prior block so the same prefix can be reused by the next turn.
    for message in reversed(rows[:-1]):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                block.setdefault("cache_control", {"type": "ephemeral"})
                return


def _body(provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], *, reasoning_effort: str | None, thinking_mode: str, model: ModelSpec | None, tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    system, rows = _messages(provider, messages)
    body: dict[str, Any] = {k: v for k, v in provider.extra_body.items() if k != "max_tokens"}
    maximum = _max_tokens(model, provider)
    body.update({"model": model_id, "messages": rows, "max_tokens": maximum, "stream": provider.stream})
    if system:
        body["system"] = system
    tool_rows = _tools(tools)
    if tool_rows:
        body["tools"] = tool_rows
        body.setdefault("tool_choice", {"type": "auto"})
    if provider.type == "minimax-messages":
        # MiniMax exposes explicit Anthropic-style block caching. It is cheaper
        # and more predictable than inventing a compatibility-layer cache key.
        _add_minimax_prompt_cache(body)
        thinking = _minimax_thinking_config(model, thinking_mode)
        if thinking is not None:
            body["thinking"] = thinking
        # M3.1 exposes five native thinking-depth levels through effort; M3 has
        # only the thinking switch and M2.x is fixed, so this appears only when
        # the model profile actually declares selectable levels.
        if thinking_mode != "off" and reasoning_effort and model is not None and reasoning_effort in model.effort_supported_levels:
            body.setdefault("output_config", {})
            if isinstance(body["output_config"], dict):
                body["output_config"]["effort"] = reasoning_effort
        # MiniMax docs define model-side sampling for current reasoning families;
        # do not inject a legacy temperature unless the user explicitly put one
        # in extra_body for an endpoint that accepts it.
    else:
        # Anthropic's automatic top-level cache control keeps the stable prefix
        # cached while preserving native thinking/tool blocks.
        body.setdefault("cache_control", {"type": "ephemeral"})
        thinking = _thinking_config(model, thinking_mode, reasoning_effort, maximum)
        if thinking is not None:
            body["thinking"] = thinking
        # Claude requires temperature=1/unset while thinking and current 4.7+/5.x
        # generations deprecate sampling parameters entirely. Prefer omission over a
        # provider-side 400 when an old provider config still contains temperature.
        model_key = str(model_id or "").lower()
        modern_no_sampling = any(token in model_key for token in (
            "-5", "-4-7", "-4-8", "fable", "mythos",
        ))
        if provider.temperature is not None and not modern_no_sampling and thinking is None:
            body["temperature"] = provider.temperature
        # Effort only has meaning while deep thinking is enabled. Some current
        # Claude models accept `disabled` only when no high/xhigh/max effort is sent,
        # so an Off session must suppress output_config.effort entirely.
        if thinking_mode != "off" and reasoning_effort and model is not None and reasoning_effort in model.effort_supported_levels:
            body.setdefault("output_config", {})
            if isinstance(body["output_config"], dict):
                body["output_config"]["effort"] = reasoning_effort
    return body


def _error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return ""


def _text_and_calls(content: Any) -> tuple[str, tuple[ToolCall, ...]]:
    if not isinstance(content, list):
        return "", ()
    text: list[str] = []
    calls: list[ToolCall] = []
    for idx, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
            calls.append(ToolCall(
                str(block.get("id") or f"toolu_{idx}"),
                block["name"],
                json.dumps(block.get("input") if isinstance(block.get("input"), dict) else {}, ensure_ascii=False, separators=(",", ":")),
            ))
    return "".join(text), tuple(calls)



def _thinking_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
            out.append(block["thinking"])
    return "".join(out)

def _turn(provider: ProviderSpec, model_id: str, payload: dict[str, Any], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, already_streamed: bool) -> ChatTurn:
    err = _error(payload)
    if err:
        raise AnthropicMessagesError(err)
    content = payload.get("content") if isinstance(payload.get("content"), list) else []
    text, calls = _text_and_calls(content)
    reasoning = _thinking_text(content)
    if reasoning and on_reasoning is not None and not already_streamed:
        on_reasoning(reasoning)
    if text and not already_streamed:
        on_text(text)
    return ChatTurn(
        text=text,
        tool_calls=calls,
        usage=_usage(payload.get("usage")),
        served_model=str(payload.get("model") or model_id),
        provider_context={"provider_id": provider.id, "transport": provider.type, "data": {"content": content, "message_id": str(payload.get("id") or "")}},
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
    del cache_key
    if should_interrupt is not None and should_interrupt():
        raise AnthropicMessagesInterrupted("")
    body = _body(provider, model_id, messages, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, tools=tools)
    response = _post_json(
        url=_endpoint(provider), headers=_headers(provider, model, thinking_mode, bool(tools)), body=body,
        timeout_seconds=provider.timeout_seconds, error_type=AnthropicMessagesError,
        opener=urllib.request.urlopen,
    )

    with _InterruptibleResponse(response, should_interrupt) as response:
        ctype = response.headers.get("Content-Type", "").lower()
        if not provider.stream or "text/event-stream" not in ctype:
            try:
                payload = json.loads(_read_response_bytes(response, should_interrupt, interrupted_type=AnthropicMessagesInterrupted).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AnthropicMessagesError("response was not valid JSON") from exc
            if not isinstance(payload, dict):
                raise AnthropicMessagesError("response JSON root was not an object")
            turn = _turn(provider, model_id, payload, on_text, on_reasoning=on_reasoning, already_streamed=False)
            if should_interrupt is not None and should_interrupt():
                raise AnthropicMessagesInterrupted(turn.text)
            return turn

        blocks: dict[int, dict[str, Any]] = {}
        text_parts: list[str] = []
        usage = UsageInfo()
        message_meta: dict[str, Any] = {"model": model_id, "id": ""}
        event_name = ""
        for raw in _iter_response_lines(response, should_interrupt):
            if should_interrupt is not None and should_interrupt():
                raise AnthropicMessagesInterrupted("".join(text_parts))
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type") or event_name)
            if etype == "error":
                raise AnthropicMessagesError(_error(event) or "Anthropic stream error")
            if etype == "message_start" and isinstance(event.get("message"), dict):
                msg = event["message"]
                message_meta["id"] = str(msg.get("id") or "")
                message_meta["model"] = str(msg.get("model") or model_id)
                usage = _usage(msg.get("usage"))
            elif etype == "content_block_start":
                idx = event.get("index")
                block = event.get("content_block")
                if isinstance(idx, int) and isinstance(block, dict):
                    blocks[idx] = json.loads(json.dumps(block))
                    if block.get("type") == "tool_use" and not isinstance(block.get("input"), dict):
                        blocks[idx]["input"] = {}
                        blocks[idx]["_input_json"] = ""
            elif etype == "content_block_delta":
                idx = event.get("index")
                delta = event.get("delta")
                if not isinstance(idx, int) or not isinstance(delta, dict):
                    continue
                block = blocks.setdefault(idx, {})
                dtype = str(delta.get("type") or "")
                if dtype == "text_delta" and isinstance(delta.get("text"), str):
                    text = delta["text"]
                    block["type"] = block.get("type") or "text"
                    block["text"] = str(block.get("text") or "") + text
                    text_parts.append(text)
                    on_text(text)
                elif dtype == "thinking_delta" and isinstance(delta.get("thinking"), str):
                    block["type"] = block.get("type") or "thinking"
                    block["thinking"] = str(block.get("thinking") or "") + delta["thinking"]
                    if on_reasoning is not None:
                        on_reasoning(delta["thinking"])
                elif dtype == "signature_delta" and isinstance(delta.get("signature"), str):
                    block["signature"] = str(block.get("signature") or "") + delta["signature"]
                elif dtype == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                    block["_input_json"] = str(block.get("_input_json") or "") + delta["partial_json"]
            elif etype == "content_block_stop":
                idx = event.get("index")
                if isinstance(idx, int) and idx in blocks:
                    block = blocks[idx]
                    raw_json = block.pop("_input_json", "")
                    if block.get("type") == "tool_use" and raw_json:
                        try:
                            parsed = json.loads(raw_json)
                        except json.JSONDecodeError:
                            parsed = {"_raw": raw_json}
                        block["input"] = parsed if isinstance(parsed, dict) else {"value": parsed}
            elif etype == "message_delta":
                if isinstance(event.get("usage"), dict):
                    delta_usage = _usage(event["usage"])
                    # message_delta normally reports output_tokens only; preserve
                    # message_start input/cache counters when absent.
                    usage = UsageInfo(
                        input_tokens=usage.input_tokens or delta_usage.input_tokens,
                        output_tokens=delta_usage.output_tokens or usage.output_tokens,
                        cached_tokens=usage.cached_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                        reasoning_tokens=0,
                        cached_tokens_reported=usage.cached_tokens_reported,
                        cache_write_tokens_reported=usage.cache_write_tokens_reported,
                        reasoning_tokens_reported=False,
                    )

        if should_interrupt is not None and should_interrupt():
            raise AnthropicMessagesInterrupted("".join(text_parts))
        content = [blocks[i] for i in sorted(blocks) if isinstance(blocks[i], dict) and blocks[i].get("type")]
        text, calls = _text_and_calls(content)
        return ChatTurn(
            text=text or "".join(text_parts),
            tool_calls=calls,
            usage=usage,
            served_model=str(message_meta.get("model") or model_id),
            provider_context={"provider_id": provider.id, "transport": provider.type, "data": {"content": content, "message_id": str(message_meta.get("id") or "")}},
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
        reasoning_effort=reasoning_effort, thinking_mode=thinking_mode,
        model=model, cache_key=cache_key, tools=None,
    ).text
