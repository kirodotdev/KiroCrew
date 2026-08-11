"""Layer 2 -- abstract output events + the ``Renderer`` contract.

The ``TurnDriver`` consumes provider events and emits the channel-neutral
``OutputEvent`` stream defined here. Each transport supplies a ``Renderer``
that maps those abstract events onto its native surface.

``prompt_choice`` is a FIRST-CLASS event (not generic "permission text"):
each Renderer maps it to its native interactive widget. ``[OPTIONS: a | b]``
trailers are the TEXT path: each widget-capable renderer re-parses the
trailer from its own accumulated text and MUST route the parsed list through
:func:`apply_options_cap` before building widgets, so at most
``capabilities.max_buttons`` choices render interactively and the remainder
degrades to a numbered text list the user can answer by typing. The cap is
ENFORCED (see ``test/test_capability_ledger.py``) and pinned per channel by
the cross-channel contract test in ``test/test_options_cap_contract.py`` —
a widget-capable renderer that skips the helper fails that test.
Channels declaring ``max_buttons=0`` render no widget and today strip the
trailer entirely; the numbered-text fallback for them lands with the
approval-ladder work.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

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


def cap_choices(
    choices: list[str], capabilities: TransportCapabilities
) -> tuple[list[str], list[str]]:
    """Split a parsed ``[OPTIONS:]`` list at ``capabilities.max_buttons``.

    Returns ``(kept, overflow)``. ``max_buttons <= 0`` keeps nothing (the
    zero-widget channels own their trailer handling). Pure — callers that
    must transform choices before display (Slack redacts at the sink) split
    here and format overflow themselves via :func:`format_overflow`.
    """
    n = capabilities.max_buttons
    if n <= 0:
        return [], choices
    return choices[:n], choices[n:]


def _default_redactor(text: str) -> str:
    """The same pair ``TurnDriver`` streams provider text through.

    Module scope on purpose: ``security`` is a pure-regex module with no vendor
    dependencies, and ``messaging.driver`` already imports it from here, so this
    adds no import-time cost and nothing that could touch an event loop.
    """
    out, _ = redact_exfiltration_urls(text or "")
    out, _ = redact_credentials(out)
    return out


def _display_safe(choice: str) -> str:
    """Redact *choice* against what the platform will SHOW, then defang mentions.

    Order matters. Redaction runs FIRST, on the canonical display form, because
    the ZWSP insertion below is itself a transformation applied after the scan
    -- exactly the class of reassembly hazard the display redactor exists to
    close, and inserting the ZWSP first could split a key so the regex stops
    matching it while the platform still renders it whole.
    """
    safe, _ = redact_for_display(choice or "", _default_redactor)
    return safe.replace("@", "@\u200b").replace("<!", "<\u200b!")


def format_overflow(overflow: list[str], start: int) -> str:
    """Number overflow choices continuing after ``start`` widget slots.

    Widget + text form ONE list: ``start=3`` yields ``4. …``. The user
    answers an overflow choice by typing it — a typed reply is a plain
    message on every channel, so no reply-parser is required.

    Two sanitisations happen at this sink, both because overflow lands in the
    message BODY while the widget path put the same text in a plain-text
    label:

    * **credentials, in DISPLAY form.** The body is markdown-parsed, so a key
      split by a code span or emphasis (``AKIA`` + backtick + rest) is whole on
      screen while the driver's byte-level stream redactor saw it broken.
      Slack's widget path already routes choices through the display redactor
      for this reason; overflow must not be the hole that reopens it on
      Telegram and Discord, which have no display-state pass of their own.
      Enforcing it HERE rather than per renderer is the same argument that put
      the cap in shared code: a channel cannot forget what it does not call.
    * **mention syntax.** Widget labels render as plain text, but the body is
      where the platforms parse mentions — a prompt-injected ``@everyone`` /
      ``<!channel>`` choice would otherwise mass-notify. ZWSP insertion
      matches the precedent in ``discord/session_resume.py``: ``@\\u200b``
      breaks discord/telegram @-mentions and slack ``<@U…>``; ``<\\u200b!``
      breaks slack broadcast ranges (``<!channel>``, ``<!here>``,
      ``<!everyone>``).
    """
    return "\n".join(f"{start + i + 1}. {_display_safe(c)}" for i, c in enumerate(overflow))


def apply_options_cap(
    body: str, choices: list[str], capabilities: TransportCapabilities
) -> tuple[str, list[str]]:
    """Enforce ``capabilities.max_buttons`` on a parsed ``[OPTIONS:]`` list.

    The ``max_buttons`` analogue of :func:`chunk_text`. Widget-capable
    renderers call this between parsing the trailer and building the native
    widget, so the cap lives in shared code and the per-channel contract
    test can pin it.

    Returns ``(body, kept_choices)``:

    * ``len(choices) <= max_buttons`` — byte-identical pass-through.
    * overflow — the first ``max_buttons`` choices are kept for the widget;
      the remainder is appended to ``body`` as a numbered text list
      (numbering continues after the widget slots). Previously overflow was
      silently dropped: the user never learned those choices existed.
    * ``max_buttons <= 0`` — returns ``(body, [])``; zero-widget channels
      own their trailer handling (today: strip).
    """
    if capabilities.max_buttons <= 0:
        return body, []
    kept, overflow = cap_choices(choices, capabilities)
    if not overflow:
        return body, kept
    lines = format_overflow(overflow, start=len(kept))
    sep = "\n\n" if body and not body.endswith("\n") else "\n" if body else ""
    return f"{body}{sep}{lines}", kept


class Renderer(ABC):
    """Maps abstract ``OutputEvent``s onto a transport's native surface."""

    channel_type: str = ""

    def __init__(self, capabilities: TransportCapabilities) -> None:
        self.capabilities = capabilities

    async def on_turn_start(self) -> None:
        """Called once before the provider stream begins. Default no-op."""
        return None

    async def close(self) -> None:
        """Release whatever the renderer opened for this turn. Default no-op.

        Declared here because the shared pipeline's ``finally`` awaits it
        (``messaging/dispatch.py``) — before this existed, that await reached
        through an ``Any`` for a method the contract never mentioned, so a
        channel could change its signature without anything noticing. Telegram
        did: its override takes an extra optional ``failure_reason``, which is a
        legal widening of this contract and stays a channel-local concern until
        the pipeline has a reason to carry one.

        Two rules for implementers:

        * It runs in a ``finally`` and is BEST-EFFORT. A caller must never let a
          failure here skip the session release — see the guard in
          ``drive_turn``, and note that the semaphore is keyed by SESSION, so a
          lost release wedges every later message in that conversation rather
          than only this turn.
        * It must tolerate being called when the turn never really started
          (``get_or_create`` can raise before the semaphore is held), so
          finalizing a placeholder that does not exist is not an error.
        """
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
