"""Native Google Gemini Interactions API adapter.

Interactions is Gemini's current first-class agent API. It represents thoughts,
function calls/results and multimodal content as typed steps and can optionally
chain server-side interaction state for better context-cache efficiency.
"""
from __future__ import annotations

from .. import __version__

import json
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ModelSpec, ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted, ProviderTransportError, ToolCall, UsageInfo
from .http_sse import InterruptibleResponse as _InterruptibleResponse, http_error_message as _http_error_message, iter_response_lines as _iter_response_lines, read_response_bytes as _read_response_bytes


class GeminiInteractionsError(ProviderError):
    pass


class GeminiInteractionsInterrupted(ProviderInterrupted, GeminiInteractionsError):
    def __init__(self, partial_text: str = "") -> None:
        ProviderInterrupted.__init__(self, partial_text)


def _endpoint(provider: ProviderSpec) -> str:
    base = provider.base_url.rstrip("/")
    path = "/interactions?alt=sse" if provider.stream else "/interactions"
    return f"{base}{path}"


def _headers(provider: ProviderSpec) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if provider.stream else "application/json",
        "User-Agent": f"Kiro-BYO/{__version__}",
        # Current Interactions wire semantics after the May 2026 breaking changes.
        "Api-Revision": "2026-05-20",
    }
    if provider.api_key:
        headers["x-goog-api-key"] = provider.api_key
    headers.update(provider.headers)
    return headers


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    return "".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") in {"text", "input_text", "output_text"})


def _content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        value = str(content or "")
        return [{"type": "text", "text": value}] if value else []
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
            if not value:
                continue
            match = re.match(r"^data:([^;,]+);base64,(.*)$", value, flags=re.DOTALL)
            if match:
                out.append({"type": "image", "mime_type": match.group(1), "data": match.group(2)})
            elif value.startswith("http://") or value.startswith("https://"):
                out.append({"type": "image", "uri": value})
    return out


def _context(message: dict[str, Any], provider: ProviderSpec) -> dict[str, Any] | None:
    raw = message.get("_kiro_provider_context")
    if not isinstance(raw, dict) or raw.get("provider_id") != provider.id or raw.get("transport") != provider.type:
        return None
    data = raw.get("data")
    return data if isinstance(data, dict) else None


def _function_steps(calls: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(calls, list):
        return out
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
        out.append({"type": "function_call", "id": str(call.get("id") or f"call_{idx}"), "name": fn["name"], "arguments": args})
    return out


def _full_history(provider: ProviderSpec, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    systems: list[str] = []
    steps: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            text = _text(message.get("content"))
            if text:
                systems.append(text)
            continue
        if role == "user":
            content = _content(message.get("content"))
            if content:
                steps.append({"type": "user_input", "content": content})
            continue
        if role == "assistant":
            ctx = _context(message, provider)
            raw_steps = ctx.get("steps") if ctx else None
            if isinstance(raw_steps, list) and raw_steps:
                steps.extend(json.loads(json.dumps(raw_steps)))
                continue
            text = _text(message.get("content"))
            if text:
                steps.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
            steps.extend(_function_steps(message.get("tool_calls")))
            continue
        if role == "tool":
            value = message.get("content")
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False) if value is not None else ""
            # The native result schema expects a content list and the matching
            # function name. Recover it from the preceding function call when possible.
            call_id = str(message.get("tool_call_id") or "")
            name = "tool"
            for previous in reversed(steps):
                if isinstance(previous, dict) and previous.get("type") == "function_call" and str(previous.get("id") or "") == call_id:
                    name = str(previous.get("name") or name)
                    break
            steps.append({"type": "function_result", "name": name, "call_id": call_id, "result": [{"type": "text", "text": value}]})
    return "\n\n".join(systems), steps


def _stateful_delta(provider: ProviderSpec, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], str]:
    """Return system, only-new steps, previous_interaction_id when safe.

    The latest provider-authentic assistant response anchors Gemini's server-side
    state. This avoids resending the complete history and lets Gemini maintain
    thought signatures/caches natively. If no valid anchor exists we return the
    full stateless history.
    """
    anchor_index = -1
    previous_id = ""
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if not isinstance(message, dict) or str(message.get("role") or "") != "assistant":
            continue
        ctx = _context(message, provider)
        interaction_id = str(ctx.get("interaction_id") or "") if ctx else ""
        if interaction_id:
            anchor_index = idx
            previous_id = interaction_id
            break
    system, full = _full_history(provider, messages)
    if anchor_index < 0:
        return system, full, ""
    _, tail = _full_history(provider, messages[anchor_index + 1 :])
    return system, tail, previous_id


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
        out.append(row)
    return out


def _usage(raw: Any) -> UsageInfo:
    if not isinstance(raw, dict):
        return UsageInfo()
    def iv(v: Any) -> int:
        return max(0, int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf")) else 0
    cached = iv(raw.get("total_cached_tokens"))
    return UsageInfo(
        input_tokens=iv(raw.get("total_input_tokens", raw.get("prompt_tokens"))),
        output_tokens=iv(raw.get("total_output_tokens", raw.get("completion_tokens"))),
        cached_tokens=cached,
        reasoning_tokens=iv(raw.get("total_thought_tokens")),
        cached_tokens_reported="total_cached_tokens" in raw,
        reasoning_tokens_reported="total_thought_tokens" in raw,
    )


def _text_calls(steps: Any) -> tuple[str, tuple[ToolCall, ...]]:
    if not isinstance(steps, list):
        return "", ()
    text: list[str] = []
    calls: list[ToolCall] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if step.get("type") == "model_output":
            content = step.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                        text.append(item["text"])
        elif step.get("type") == "function_call" and isinstance(step.get("name"), str):
            args = step.get("arguments")
            calls.append(ToolCall(
                str(step.get("id") or f"call_{idx}"), str(step["name"]),
                json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False, separators=(",", ":")),
            ))
    return "".join(text), tuple(calls)



def _summary_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        content = value.get("content")
        return _summary_text(content) if content is not None else ""
    if isinstance(value, list):
        return "".join(_summary_text(item) for item in value)
    return ""


def _thought_summary_text(steps: Any) -> str:
    if not isinstance(steps, list):
        return ""
    out: list[str] = []
    for step in steps:
        if isinstance(step, dict) and step.get("type") == "thought":
            out.append(_summary_text(step.get("summary")))
    return "".join(out)

def _body(provider: ProviderSpec, model_id: str, messages: list[dict[str, Any]], *, reasoning_effort: str | None, thinking_mode: str, model: ModelSpec | None, tools: list[dict[str, Any]] | None, force_stateless: bool = False) -> dict[str, Any]:
    use_state = bool(provider.extra_body.get("store", True)) and not force_stateless
    if use_state:
        system, inputs, previous = _stateful_delta(provider, messages)
    else:
        system, inputs = _full_history(provider, messages)
        previous = ""
    body: dict[str, Any] = {k: v for k, v in provider.extra_body.items() if k not in {"store", "generation_config"}}
    body.update({"model": model_id, "input": inputs, "stream": provider.stream, "store": use_state})
    if system:
        body["system_instruction"] = system
    if previous:
        body["previous_interaction_id"] = previous
    tool_rows = _tools(tools)
    if tool_rows:
        body["tools"] = tool_rows
    generation = dict(provider.extra_body.get("generation_config") or {}) if isinstance(provider.extra_body.get("generation_config"), dict) else {}
    if provider.temperature is not None:
        generation["temperature"] = provider.temperature
    if reasoning_effort and model is not None and reasoning_effort in model.effort_supported_levels:
        generation["thinking_level"] = reasoning_effort
    if model is not None and model.max_output_tokens:
        generation.setdefault("max_output_tokens", model.max_output_tokens)
    if generation:
        body["generation_config"] = generation
    # Gemini thinking_level is intensity, not a universal on/off switch. Current
    # official profiles only advertise a real switch when the API has one.
    if thinking_mode == "off" and model is not None and model.reasoning_can_disable is True and model.reasoning_disable_value:
        body.setdefault("generation_config", {})["thinking_level"] = model.reasoning_disable_value
    return body


def _error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return ""


def _interaction(payload: dict[str, Any]) -> dict[str, Any]:
    wrapped = payload.get("interaction")
    return wrapped if isinstance(wrapped, dict) else payload


def _turn(provider: ProviderSpec, model_id: str, interaction: dict[str, Any], on_text: Callable[[str], None], *, on_reasoning: Callable[[str], None] | None = None, already_streamed: bool) -> ChatTurn:
    err = _error(interaction)
    if err:
        raise GeminiInteractionsError(err)
    steps = interaction.get("steps") if isinstance(interaction.get("steps"), list) else []
    text, calls = _text_calls(steps)
    reasoning = _thought_summary_text(steps)
    if reasoning and on_reasoning is not None and not already_streamed:
        on_reasoning(reasoning)
    if text and not already_streamed:
        on_text(text)
    return ChatTurn(
        text=text,
        tool_calls=calls,
        usage=_usage(interaction.get("usage")),
        served_model=str(interaction.get("model") or model_id).removeprefix("models/"),
        provider_context={"provider_id": provider.id, "transport": provider.type, "data": {"steps": steps, "interaction_id": str(interaction.get("id") or "")}},
    )


def _request(provider: ProviderSpec, body: dict[str, Any]) -> Any:
    req = urllib.request.Request(_endpoint(provider), data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=_headers(provider), method="POST")
    return urllib.request.urlopen(req, timeout=provider.timeout_seconds)


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
        raise GeminiInteractionsInterrupted("")
    body = _body(provider, model_id, messages, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, tools=tools)
    try:
        response = _request(provider, body)
    except urllib.error.HTTPError as exc:
        # Stored interactions expire. If a durable Kiro session outlives Gemini's
        # retention window, transparently fall back to stateless full-history
        # replay rather than breaking session/load.
        if body.get("previous_interaction_id") and exc.code in {400, 404, 410}:
            try:
                body = _body(provider, model_id, messages, reasoning_effort=reasoning_effort, thinking_mode=thinking_mode, model=model, tools=tools, force_stateless=True)
                response = _request(provider, body)
            except urllib.error.HTTPError as retry:
                raise GeminiInteractionsError(_http_error_message(retry)) from retry
            except urllib.error.URLError as retry:
                raise ProviderTransportError(f"connection failed: {retry.reason}") from retry
            except (TimeoutError, socket.timeout) as retry:
                raise ProviderTransportError(f"request timed out after {provider.timeout_seconds:g}s") from retry
            except OSError as retry:
                raise ProviderTransportError(f"connection failed while opening request: {type(retry).__name__}: {retry}") from retry
        else:
            raise GeminiInteractionsError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise ProviderTransportError(f"connection failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderTransportError(f"request timed out after {provider.timeout_seconds:g}s") from exc
    except OSError as exc:
        raise ProviderTransportError(f"connection failed while opening request: {type(exc).__name__}: {exc}") from exc

    with _InterruptibleResponse(response, should_interrupt) as response:
        ctype = response.headers.get("Content-Type", "").lower()
        if not provider.stream or "text/event-stream" not in ctype:
            try:
                payload = json.loads(_read_response_bytes(response, should_interrupt, interrupted_type=GeminiInteractionsInterrupted).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GeminiInteractionsError("response was not valid JSON") from exc
            if not isinstance(payload, dict):
                raise GeminiInteractionsError("response JSON root was not an object")
            err = _error(payload)
            if err:
                raise GeminiInteractionsError(err)
            turn = _turn(provider, model_id, _interaction(payload), on_text, on_reasoning=on_reasoning, already_streamed=False)
            if should_interrupt is not None and should_interrupt():
                raise GeminiInteractionsInterrupted(turn.text)
            return turn

        steps: dict[int, dict[str, Any]] = {}
        text_parts: list[str] = []
        interaction_meta: dict[str, Any] = {"id": "", "model": model_id, "usage": {}}
        event_name = ""
        for raw in _iter_response_lines(response, should_interrupt):
            if should_interrupt is not None and should_interrupt():
                raise GeminiInteractionsInterrupted("".join(text_parts))
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
            etype = str(event.get("event_type") or event.get("type") or event_name)
            err = _error(event)
            if err:
                raise GeminiInteractionsError(err)
            if etype == "interaction.created":
                inter = event.get("interaction")
                if isinstance(inter, dict):
                    interaction_meta["id"] = str(inter.get("id") or "")
                    interaction_meta["model"] = str(inter.get("model") or model_id)
            elif etype == "step.start":
                idx = event.get("index")
                step = event.get("step")
                if isinstance(idx, int) and isinstance(step, dict):
                    steps[idx] = json.loads(json.dumps(step))
                    if step.get("type") == "function_call":
                        steps[idx]["_args"] = ""
            elif etype == "step.delta":
                idx = event.get("index")
                delta = event.get("delta")
                if not isinstance(idx, int) or not isinstance(delta, dict):
                    continue
                step = steps.setdefault(idx, {})
                dtype = str(delta.get("type") or "")
                if dtype == "text" and isinstance(delta.get("text"), str):
                    text = delta["text"]
                    step.setdefault("type", "model_output")
                    content = step.setdefault("content", [])
                    if not isinstance(content, list):
                        content = []
                        step["content"] = content
                    if content and isinstance(content[-1], dict) and content[-1].get("type") == "text":
                        content[-1]["text"] = str(content[-1].get("text") or "") + text
                    else:
                        content.append({"type": "text", "text": text})
                    if step.get("type") == "model_output":
                        text_parts.append(text)
                        on_text(text)
                elif dtype == "arguments_delta" and isinstance(delta.get("arguments"), str):
                    step["_args"] = str(step.get("_args") or "") + delta["arguments"]
                elif dtype == "thought_summary":
                    step.setdefault("type", "thought")
                    content = delta.get("content")
                    if isinstance(content, dict):
                        summary = step.setdefault("summary", [])
                        if isinstance(summary, list):
                            summary.append(json.loads(json.dumps(content)))
                        reasoning_delta = _summary_text(content)
                        if reasoning_delta and on_reasoning is not None:
                            on_reasoning(reasoning_delta)
                elif dtype == "thought_signature" and isinstance(delta.get("signature"), str):
                    step.setdefault("type", "thought")
                    step["signature"] = delta["signature"]
            elif etype == "step.stop":
                idx = event.get("index")
                if isinstance(idx, int) and idx in steps:
                    step = steps[idx]
                    raw_args = step.pop("_args", "")
                    if step.get("type") == "function_call" and raw_args:
                        try:
                            parsed = json.loads(raw_args)
                        except json.JSONDecodeError:
                            parsed = {"_raw": raw_args}
                        step["arguments"] = parsed if isinstance(parsed, dict) else {"value": parsed}
            elif etype == "interaction.completed":
                inter = event.get("interaction")
                if isinstance(inter, dict):
                    # Current server includes final usage and may include fully
                    # assembled steps. Prefer those when present.
                    interaction_meta.update(inter)
                    if isinstance(inter.get("steps"), list):
                        steps = {i: json.loads(json.dumps(s)) for i, s in enumerate(inter["steps"]) if isinstance(s, dict)}
                metadata = event.get("metadata")
                if isinstance(metadata, dict) and isinstance(metadata.get("total_usage"), dict):
                    interaction_meta["usage"] = metadata["total_usage"]

        if should_interrupt is not None and should_interrupt():
            raise GeminiInteractionsInterrupted("".join(text_parts))
        assembled = [steps[i] for i in sorted(steps) if isinstance(steps[i], dict) and steps[i].get("type")]
        interaction_meta["steps"] = assembled
        text, calls = _text_calls(assembled)
        return ChatTurn(
            text=text or "".join(text_parts),
            tool_calls=calls,
            usage=_usage(interaction_meta.get("usage")),
            served_model=str(interaction_meta.get("model") or model_id).removeprefix("models/"),
            provider_context={"provider_id": provider.id, "transport": provider.type, "data": {"steps": assembled, "interaction_id": str(interaction_meta.get("id") or "")}},
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
