"""Provider-transport capability descriptors.

Concrete provider identity belongs at the adapter boundary.  Shared wire engines
consume capability records instead of branching on provider brand/type names.
Keeping those differences here makes transport modules mechanical and keeps new
provider quirks from leaking back into shared request/stream code.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResponsesStatePolicy:
    """How a Responses transport treats state/cache body fields."""

    # True -> force True; False -> setdefault False; None -> remove.
    store: bool | None = None
    send_cache_key: bool = False
    drop_cache_key: bool = False
    drop_previous_response_id: bool = False
    include_encrypted_reasoning: bool = False


@dataclass(frozen=True)
class ResponsesCapabilities:
    """Wire capabilities consumed by the shared Responses implementation."""

    state_policy: ResponsesStatePolicy | None = None
    request_headers: tuple[tuple[str, str], ...] = ()
    supports_previous_response_id: bool = False
    previous_response_retry_statuses: frozenset[int] = frozenset()
    reasoning_wire: str = "effort"  # effort | thinking-toggle
    reasoning_off_value: str = ""
    strip_when_reasoning: tuple[str, ...] = ()


_DEFAULT_RESPONSES = ResponsesCapabilities()

_RESPONSES_CAPABILITIES: dict[str, ResponsesCapabilities] = {
    "openai-responses": ResponsesCapabilities(
        state_policy=ResponsesStatePolicy(store=False, send_cache_key=True),
        strip_when_reasoning=("temperature", "top_p", "top_logprobs", "logprobs"),
    ),
    "deepseek-responses": ResponsesCapabilities(reasoning_off_value="none"),
    "ark-responses": ResponsesCapabilities(
        state_policy=ResponsesStatePolicy(store=False, send_cache_key=True),
    ),
    "xai-responses": ResponsesCapabilities(
        state_policy=ResponsesStatePolicy(
            store=False,
            send_cache_key=True,
            include_encrypted_reasoning=True,
        ),
        strip_when_reasoning=("presence_penalty", "frequency_penalty", "stop"),
    ),
    "qwen-responses": ResponsesCapabilities(
        state_policy=ResponsesStatePolicy(store=None, drop_cache_key=True),
        request_headers=(("x-dashscope-session-cache", "enable"),),
        supports_previous_response_id=True,
        previous_response_retry_statuses=frozenset({400, 404, 409, 410, 422}),
    ),
    "hunyuan-responses": ResponsesCapabilities(
        state_policy=ResponsesStatePolicy(store=None, drop_cache_key=True),
        supports_previous_response_id=True,
        previous_response_retry_statuses=frozenset({400, 404, 409, 410, 422}),
    ),
    "qianfan-responses": ResponsesCapabilities(
        state_policy=ResponsesStatePolicy(store=True, drop_cache_key=True),
        supports_previous_response_id=True,
        previous_response_retry_statuses=frozenset({400, 404, 409, 410, 422}),
        reasoning_wire="thinking-toggle",
    ),
    "groq-responses": ResponsesCapabilities(
        state_policy=ResponsesStatePolicy(
            store=None,
            drop_cache_key=True,
            drop_previous_response_id=True,
        ),
    ),
}


def responses_capabilities(transport: str) -> ResponsesCapabilities:
    """Return immutable capabilities for one installed Responses transport."""

    return _RESPONSES_CAPABILITIES.get(str(transport or "").strip().lower(), _DEFAULT_RESPONSES)


def responses_transport_ids() -> tuple[str, ...]:
    """Installed Responses transports backed by the shared wire engine."""

    return tuple(_RESPONSES_CAPABILITIES)
