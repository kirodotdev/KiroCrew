"""Layer 2 -- abstract output events + the ``Renderer`` contract.

The ``TurnDriver`` consumes provider events and emits the channel-neutral
``OutputEvent`` stream defined here. Each transport supplies a ``Renderer``
that maps those abstract events onto its native surface.

``prompt_choice`` is a FIRST-CLASS event (not generic "permission text"):
each Renderer maps it to its native interactive widget. NOTE: despite the
existence of ``capabilities.max_buttons``, no renderer reads it today — the
caps are hardcoded per channel (Slack ``choices[:10]``, Discord
``options[:25]``, Telegram uncapped) and the no-widget channels strip the
trailer unconditionally. Capping on the declared value and degrading to a
numbered text reply is the INTENDED contract, tracked in the capability
ledger (``test/test_capability_ledger.py``); do not write code that assumes
it is already enforced.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.messaging.transport import TransportCapabilities

# Abstract output event kinds.
TEXT_CHUNK = "text_chunk"
THINKING = "thinking"
TOOL_CALL = "tool_call"
PROMPT_CHOICE = "prompt_choice"
COMPACTION = "compaction"
DONE = "done"
STEER_CONSUMED = "steer_consumed"  # kiro-cli folded a mid-turn steer at a boundary

OUTPUT_KINDS = frozenset(
    {TEXT_CHUNK, THINKING, TOOL_CALL, PROMPT_CHOICE, COMPACTION, DONE, STEER_CONSUMED}
)


@dataclass
class OutputEvent:
    """A channel-neutral output event emitted by the TurnDriver."""

    kind: str
    text: str = ""  # text_chunk / thinking
    tool_call_id: str = ""  # tool_call
    title: str = ""  # tool_call (tool name / "Running: X")
    tool_kind: str = ""  # tool_call (e.g. "read"/"execute" — drives phase emoji)
    tool_purpose: str = ""  # tool_call (human-readable purpose -> task title)
    options: list[dict[str, Any]] = field(default_factory=list)  # prompt_choice
    request_id: str | int = ""  # prompt_choice correlation
    context_usage_pct: float = 0.0  # compaction
    stop_reason: str = ""  # done

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "tool_call_id": self.tool_call_id,
            "title": self.title,
            "tool_kind": self.tool_kind,
            "tool_purpose": self.tool_purpose,
            "options": [dict(o) for o in self.options],
            "request_id": self.request_id,
            "context_usage_pct": self.context_usage_pct,
            "stop_reason": self.stop_reason,
        }


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into chunks no longer than ``max_chars``.

    Pure helper used by Renderers to honor ``capabilities.max_message_chars``.
    Returns ``[]`` for empty input. A non-positive ``max_chars`` disables
    chunking (returns the text as a single chunk).
    """
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


class Renderer(ABC):
    """Maps abstract ``OutputEvent``s onto a transport's native surface."""

    channel_type: str = ""

    def __init__(self, capabilities: TransportCapabilities) -> None:
        self.capabilities = capabilities

    async def on_turn_start(self) -> None:
        """Called once before the provider stream begins. Default no-op."""
        return None

    @abstractmethod
    async def on_text_chunk(self, text: str) -> None:
        """Render a streamed assistant text chunk."""

    @abstractmethod
    async def on_thinking(self, text: str) -> None:
        """Render a reasoning/thinking update."""

    @abstractmethod
    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        """Render a tool call.

        Mirrors the native uniform ``EVENT_TOOL_CALL`` semantics: each call
        marks the previous task complete and starts a new in-progress task.
        """

    @abstractmethod
    async def on_prompt_choice(
        self, options: list[dict[str, Any]], request_id: str | int
    ) -> None:
        """Render an interactive approval/choice prompt (first-class)."""

    @abstractmethod
    async def on_compaction(self, context_usage_pct: float) -> None:
        """Render a context-compaction notice."""

    @abstractmethod
    async def on_done(self, stop_reason: str = "") -> None:
        """Finalize the turn (close any open stream)."""

    async def on_steer_consumed(self, summary: str = "") -> None:
        """kiro-cli folded a mid-turn steer at a generation boundary.

        ``summary`` is parsed from the suppressed inline protocol marker. The
        default is a no-op; channels that split the continuation can render a
        native acknowledgement without ever receiving the raw marker text.
        """
        return None

    async def dispatch(self, event: OutputEvent) -> None:
        """Route ``event`` to the matching ``on_*`` handler."""
        if event.kind == TEXT_CHUNK:
            await self.on_text_chunk(event.text)
        elif event.kind == THINKING:
            await self.on_thinking(event.text)
        elif event.kind == TOOL_CALL:
            await self.on_tool_call(
                event.tool_call_id, event.title, event.tool_kind, event.tool_purpose
            )
        elif event.kind == PROMPT_CHOICE:
            await self.on_prompt_choice(event.options, event.request_id)
        elif event.kind == COMPACTION:
            await self.on_compaction(event.context_usage_pct)
        elif event.kind == DONE:
            await self.on_done(event.stop_reason)
        elif event.kind == STEER_CONSUMED:
            await self.on_steer_consumed(event.text)
        else:
            raise ValueError(f"unknown output event kind: {event.kind!r}")
