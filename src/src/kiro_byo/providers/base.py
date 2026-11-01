"""Provider-neutral inference contracts for Kiro BYO.

ACP/session/tool orchestration must not depend on one upstream wire format.  A
provider adapter is responsible only for model transport: encode messages/tools,
stream visible text, report usage, and yield promptly to host interruption.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import ModelSpec, ProviderSpec


class ProviderError(RuntimeError):
    """Provider response/runtime failure normalized at the adapter boundary."""

    category = "provider"
    retryable = False


class ProviderTransportError(ProviderError):
    """Transient network/HTTP-body transport failure before protocol completion."""

    category = "transport"
    retryable = True


class ProviderInterrupted(ProviderError):
    """An in-flight provider request was interrupted by cancel/steer."""

    category = "control"
    retryable = False

    def __init__(self, partial_text: str = "") -> None:
        super().__init__("upstream turn interrupted by host control-plane request")
        self.partial_text = partial_text


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments_text: str

    def arguments(self) -> dict[str, Any]:
        if not self.arguments_text.strip():
            return {}
        try:
            parsed = json.loads(self.arguments_text)
        except json.JSONDecodeError:
            return {"_raw": self.arguments_text}
        return parsed if isinstance(parsed, dict) else {"value": parsed}


@dataclass(frozen=True)
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    # Distinguish "provider reported zero" from "provider did not expose this
    # counter". Diagnostics should never present unknown cache support as 0%.
    cached_tokens_reported: bool = False
    cache_write_tokens_reported: bool = False
    reasoning_tokens_reported: bool = False


@dataclass(frozen=True)
class ChatTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: UsageInfo = UsageInfo()
    served_model: str = ""
    # Opaque, provider-authentic continuation data (reasoning signatures/items,
    # tool-use blocks, etc.). The session stores this next to the provider-neutral
    # assistant message so native adapters can replay it exactly on later turns.
    provider_context: dict[str, Any] | None = None


class ProviderAdapter(Protocol):
    """Wire adapter selected from ``ProviderSpec.type``."""

    transport: str

    def completion_turn(
        self,
        provider: "ProviderSpec",
        model_id: str,
        messages: list[dict[str, Any]],
        on_text: Callable[[str], None],
        *,
        on_reasoning: Callable[[str], None] | None = None,
        reasoning_effort: str | None = None,
        thinking_mode: str = "",
        model: "ModelSpec | None" = None,
        cache_key: str = "",
        tools: list[dict[str, Any]] | None = None,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> ChatTurn: ...

    def completion(
        self,
        provider: "ProviderSpec",
        model_id: str,
        messages: list[dict[str, Any]],
        on_text: Callable[[str], None],
        *,
        on_reasoning: Callable[[str], None] | None = None,
        reasoning_effort: str | None = None,
        thinking_mode: str = "",
        model: "ModelSpec | None" = None,
        cache_key: str = "",
    ) -> str: ...
