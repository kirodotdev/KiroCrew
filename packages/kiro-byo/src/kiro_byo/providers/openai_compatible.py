"""OpenAI-compatible Chat Completions adapter.

The current transport keeps cooperative interruption for cancel/steer while preserving:
multimodal user content and native function/tool calling.  The adapter still
uses only the Python standard library so it can live inside Kiro Crew's bundled
interpreter without installing wheels.
"""

from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import ProviderSpec
from .base import make_chat_text, ChatTurn, ProviderError, ProviderInterrupted, ProviderTransportError, ToolCall, UsageInfo
from .chat_completions_common import (
    ChatResponseProfile, endpoint as _endpoint, headers as _headers,
    discard_stale_chat_reasoning, run_native_chat_completion, usage_info as _usage_info,
)
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
    body: dict[str, Any] = copy.deepcopy(dict(provider.extra_body))
    discard_stale_chat_reasoning(body, thinking_mode)
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
        # A declared level-style profile alone does not prove that a gateway
        # accepts arbitrary binary literals as reasoning_effort.
        if disable.lower() == "none":
            body["reasoning_effort"] = disable
        else:
            raise OpenAICompatibleError(
                f"{model_id} declares switchable reasoning but the generic OpenAI-compatible "
                "adapter has no verified wire mapping for thinking=off; use a native provider "
                "transport or verified reasoning_effort=none semantics",
                failover_allowed=False,
            )
    elif reasoning_effort:
        # This parameter is an *effort level*, never the model's ON literal.
        # The engine has already resolved the Type policy and model override.
        if model is not None and model.effort_supported_levels and reasoning_effort not in model.effort_supported_levels:
            raise OpenAICompatibleError(
                f"{model_id} does not advertise reasoning effort {reasoning_effort!r}",
                failover_allowed=False,
            )
        body["reasoning_effort"] = reasoning_effort
    elif thinking_mode == "on" and model is not None and model.reasoning_can_disable is True:
        # A generic gateway cannot serialize a vendor's binary {thinking:type}
        # or enable_thinking switch. A verified default-on model needs no wire
        # override, but an OFF-by-default model must fail instead of silently
        # violating the persistent user lock.
        if model.reasoning_default_enabled is not True:
            raise OpenAICompatibleError(
                f"{model_id} cannot guarantee thinking=on through generic OpenAI-compatible transport; "
                "select a verified native adapter or configure a supported effort vocabulary",
                failover_allowed=False,
            )
        if body.get("reasoning_effort") == model.reasoning_disable_value:
            body.pop("reasoning_effort", None)
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


_GENERIC_RESPONSE_PROFILE = ChatResponseProfile(
    retain_context=False,
    reasoning_fallback_key="reasoning",
    nonstream_content_text=_content_text,
    parse_calls=_parse_tool_calls,
    reasoning_before_text=True,
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

    # Host cancellation takes priority over request-policy validation and I/O.
    if should_interrupt is not None and should_interrupt():
        raise OpenAICompatibleInterrupted("")
    return run_native_chat_completion(
        provider, model_id,
        _body(
            provider, model_id, messages,
            reasoning_effort=reasoning_effort, thinking_mode=thinking_mode,
            model=model, cache_key=cache_key, tools=tools,
        ),
        on_text, on_reasoning=on_reasoning, should_interrupt=should_interrupt,
        error_type=OpenAICompatibleError,
        interrupted_type=OpenAICompatibleInterrupted,
        response_profile=_GENERIC_RESPONSE_PROFILE,
        opener=urllib.request.urlopen,
    )


# Legacy text/compaction API delegates to the current replaceable Turn entry.
chat_completion = make_chat_text(lambda: chat_completion_turn)
