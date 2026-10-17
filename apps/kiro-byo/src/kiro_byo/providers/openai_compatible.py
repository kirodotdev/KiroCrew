"""OpenAI-compatible Chat Completions adapter.

0.1.0 retains the transport with cooperative interruption for cancel/steer while preserving:
multimodal user content and native function/tool calling.  The adapter still
uses only the Python standard library so it can live inside Kiro Crew's bundled
interpreter without installing wheels.
"""

from __future__ import annotations

from .. import __version__

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ProviderSpec
from .base import ChatTurn, ProviderError, ProviderInterrupted, ToolCall, UsageInfo


class OpenAICompatibleError(ProviderError):
    pass


class OpenAICompatibleInterrupted(ProviderInterrupted, OpenAICompatibleError):
    """OpenAI-compatible transport yielded to cancel/steer."""

    def __init__(self, partial_text: str = "") -> None:
        ProviderInterrupted.__init__(self, partial_text)


class _InterruptibleResponse:
    """Close a live urllib response as soon as the ACP control plane interrupts.

    Cooperative checks alone can sit blocked in ``response.read()`` / SSE iteration
    until another network packet arrives.  The watcher turns cancel/steer into an
    actual transport abort, then the normal adapter code converts that close into
    ``OpenAICompatibleInterrupted``.
    """

    def __init__(self, response: Any, should_interrupt: Callable[[], bool] | None) -> None:
        self.response = response
        self.should_interrupt = should_interrupt
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> Any:
        if self.should_interrupt is not None:
            def _watch() -> None:
                while not self.stop.wait(0.05):
                    try:
                        interrupted = bool(self.should_interrupt and self.should_interrupt())
                    except Exception:
                        interrupted = False
                    if not interrupted:
                        continue
                    try:
                        self.response.close()
                    except Exception:
                        pass
                    return

            self.thread = threading.Thread(
                target=_watch,
                name="kiro-byo-http-interrupt",
                daemon=True,
            )
            self.thread.start()
        return self.response

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop.set()
        try:
            self.response.close()
        finally:
            if self.thread is not None and self.thread is not threading.current_thread():
                self.thread.join(timeout=0.2)


def _read_response_bytes(response: Any, should_interrupt: Callable[[], bool] | None) -> bytes:
    try:
        return response.read()
    except (OSError, ValueError, http.client.IncompleteRead) as exc:
        if should_interrupt is not None and should_interrupt():
            raise OpenAICompatibleInterrupted("") from exc
        raise


def _iter_response_lines(response: Any, should_interrupt: Callable[[], bool] | None):
    try:
        yield from response
    except (OSError, ValueError, http.client.IncompleteRead) as exc:
        if should_interrupt is not None and should_interrupt():
            return
        raise


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
    if reasoning_effort:
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




def _usage_info(raw: Any) -> UsageInfo:
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


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        if raw:
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict) and isinstance(error.get("message"), str):
                        detail = error["message"]
                    elif isinstance(payload.get("message"), str):
                        detail = payload["message"]
                if not detail:
                    detail = raw[:600]
            except json.JSONDecodeError:
                detail = raw[:600]
    except Exception:
        detail = ""
    base = f"HTTP {exc.code} {exc.reason}"
    return f"{base}: {detail}" if detail else base


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
        cache_key=cache_key,
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
        raise OpenAICompatibleError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise OpenAICompatibleError(f"connection failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise OpenAICompatibleError(f"request timed out after {provider.timeout_seconds:g}s") from exc

    with _InterruptibleResponse(response, should_interrupt) as response:
        content_type = response.headers.get("Content-Type", "").lower()
        if (not provider.stream) or ("text/event-stream" not in content_type):
            try:
                payload = json.loads(_read_response_bytes(response, should_interrupt).decode("utf-8"))
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
