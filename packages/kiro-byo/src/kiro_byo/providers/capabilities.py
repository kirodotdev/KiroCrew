"""Provider-transport capability descriptors.

Concrete provider identity belongs at the adapter boundary.  Shared wire engines
consume capability records instead of branching on provider brand/type names.
Keeping those differences here makes transport modules mechanical and keeps new
provider quirks from leaking back into shared request/stream code.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


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


@dataclass(frozen=True)
class AnthropicThinkingCapabilities:
    """Known Claude Messages wire behavior (not relay/third-party guessing).

    ``off_value=None`` means the API does not permit a thinking-off request;
    the caller must omit a stale ``thinking.type=disabled`` config instead.
    Unknown aliases return no capability record and keep their own metadata.
    """

    mode: str  # manual | adaptive
    off_value: str | None = "disabled"
    forbid_sampling: bool = False


def anthropic_thinking_capabilities(model_id: str) -> AnthropicThinkingCapabilities | None:
    """Normalize documented model-family transitions independently of cached UI metadata.

    Only names in Anthropic's public Claude ID family are classified. Custom
    relays and other providers keep their model-specific declared semantics.
    """

    name = str(model_id or "").strip().lower()
    if name == "claude-mythos-preview":
        return AnthropicThinkingCapabilities(mode="adaptive", off_value=None, forbid_sampling=True)
    match = re.match(r"^claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?(?:-|$)", name)
    if match is None:
        return None
    family, major_string, minor_string = match.groups()
    major, minor = int(major_string), int(minor_string or 0)
    if major < 4:
        # Older generations are intentionally left to their existing metadata.
        return None
    if major == 4 and minor <= 5:
        return AnthropicThinkingCapabilities(mode="manual")
    if major == 4:
        return AnthropicThinkingCapabilities(mode="adaptive", forbid_sampling=minor >= 7)
    # Claude 5+ removed manual extended thinking. Do not assume unknown future
    # generations support a binary off switch (which can cause request 400s).
    if family in {"fable", "mythos"} or major > 5:
        off_value = None
    elif family == "opus" and minor >= 5:
        off_value = None
    elif family == "sonnet" and minor == 5:
        off_value = "between_tools"
    elif family == "sonnet" and minor > 5:
        off_value = None
    elif family in {"opus", "sonnet", "haiku"}:
        off_value = "disabled"
    else:
        off_value = None
    return AnthropicThinkingCapabilities(mode="adaptive", off_value=off_value, forbid_sampling=True)


def reconcile_anthropic_reasoning(reasoning: dict, model_id: str) -> dict:
    """Align UI/runtime thinking facts with a known official Claude wire model.

    This is a read-time projection: never mutate a user's persisted cache.
    Caller must verify that the endpoint uses native Anthropic Messages.
    """
    policy = anthropic_thinking_capabilities(model_id)
    if policy is None:
        return reasoning
    result = dict(reasoning)
    thinking = dict(result["thinking"])
    thinking.update({
        "supported": True,
        "can_disable": policy.off_value is not None,
        "default_enabled": True if policy.off_value is None else thinking["default_enabled"],
        "disable_value": policy.off_value or "",
        "enable_value": "enabled" if policy.mode == "manual" else "adaptive",
        "auto_value": "adaptive" if policy.mode == "adaptive" else "",
        "supports_auto": policy.mode == "adaptive",
        "control": "switchable" if policy.off_value is not None else "fixed_on",
    })
    result["thinking"] = thinking
    if policy.mode == "adaptive":
        result["mode"] = "adaptive"
    for key in ("supported", "can_disable", "default_enabled", "disable_value", "enable_value", "auto_value"):
        result[key] = thinking[key]
    return result
