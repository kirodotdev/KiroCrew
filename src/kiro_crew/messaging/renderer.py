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
from kiro_crew.messaging.tables import render_tables, render_tables_with_metadata
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


def display_safe(text: str) -> str:
    """Redact *text* against what the platform will SHOW, then defang mentions.

    The shared outbound display sink: every surface that renders untrusted text
    into a channel message goes through here, so one text cannot be sanitized two
    ways. Used by this module's overflow list and by the dashboard's channel
    notices.

    Order matters. Redaction runs FIRST, on the canonical display form, because
    the ZWSP insertion below is itself a transformation applied after the scan
    -- exactly the class of reassembly hazard the display redactor exists to
    close, and inserting the ZWSP first could split a key so the regex stops
    matching it while the platform still renders it whole.

    The defang covers both mention grammars because the callers are
    channel-neutral: ``@`` for Discord/Telegram users and ``@everyone``, ``<!``
    for Slack's ``<!channel>``.
    """
    safe, _ = redact_for_display(text or "", _default_redactor)
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
    return "\n".join(f"{start + i + 1}. {display_safe(c)}" for i, c in enumerate(overflow))


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
      (numbering continues after the widget slots) rather than dropped, so
      the user still learns those choices exist.
    * ``max_buttons <= 0`` — returns ``(body, [])``; zero-widget channels
      own their trailer handling (today: strip).
    """
    if capabilities.max_buttons <= 0:
        return body, []
    kept, overflow = cap_choices(choices, capabilities)
    if not overflow:
        return body, kept
    lines = format_overflow(overflow, start=len(kept))
    if not body:
        sep = ""
    elif body.endswith("\n"):
        sep = "\n"
    else:
        sep = "\n\n"
    return f"{body}{sep}{lines}", kept


class Renderer(ABC):
    """Maps abstract ``OutputEvent``s onto a transport's native surface."""

    channel_type: str = ""

    def __init__(self, capabilities: TransportCapabilities) -> None:
        self.capabilities = capabilities

    def redact_for_target(self, text: str) -> str:
        """Redact text against the form a target will display."""
        safe, _ = redact_for_display(text, _default_redactor)
        return safe

    def render_tables_for_target(
        self,
        text: str,
        *,
        final: bool = True,
        policy: str | None = None,
    ) -> str:
        """Apply a table policy to text about to be sent to this target.

        Call it on outbound bytes only. The turn's canonical text (what
        ``TurnDriver.run`` returns, and what the transcript and dashboard show)
        must not pass through here, or the conversion stops being a
        per-target presentation choice and becomes a rewrite of the answer.

        ``policy`` normally defaults to this target's declared ``table_mode``.
        A channel may override it for delivery framing (for example, changing
        an over-cap generated grid to cards), but must keep that fallback in
        this helper so post-transform display redaction cannot be bypassed.

        ``final=False`` while a turn is still streaming: a table whose last row
        may not have arrived yet is left raw rather than frozen half-built.
        """
        rendered, _ = self.render_tables_for_target_with_metadata(
            text,
            final=final,
            policy=policy,
        )
        return rendered

    def render_tables_for_target_with_metadata(
        self,
        text: str,
        *,
        final: bool = True,
        policy: str | None = None,
    ) -> tuple[str, bool]:
        """Render tables and report whether conversion generated a grid."""
        rendered, generated_grid = render_tables_with_metadata(
            text,
            policy=self.capabilities.table_mode if policy is None else policy,
            native_tables=self.capabilities.native_tables,
            final=final,
        )
        if rendered == text:
            return rendered, generated_grid

        # Cards join headers and values that the stream redactor saw on
        # separate table lines. Re-scan the display form at this last outbound
        # transform so a label/value pair cannot assemble an Authorization
        # header (or a formatted URL) after the channel-neutral pass.
        return self.redact_for_target(rendered), generated_grid

    def safe_raw_table_fallback(
        self,
        text: str,
        *,
        final: bool = True,
        policy: str | None = None,
    ) -> str | None:
        """Return display-safe raw text only when rendering reveals no new secret."""
        safe_raw = self.redact_for_target(text)
        rendered_safe_raw = render_tables(
            safe_raw,
            policy=self.capabilities.table_mode if policy is None else policy,
            native_tables=self.capabilities.native_tables,
            final=final,
        )
        if self.redact_for_target(rendered_safe_raw) != rendered_safe_raw:
            return None
        return safe_raw

    async def on_turn_start(self) -> None:
        """Called once before the provider stream begins. Default no-op."""
        return None

    async def close(self) -> None:
        """Release whatever the renderer opened for this turn. Default no-op.

        Declared here because the shared pipeline's ``finally`` awaits it
        (``messaging/dispatch.py``, through a ``ChannelTurn.renderer`` still typed
        ``Any``). Naming it in the contract is what makes a channel's override
        signature checked, rather than a method the ABC never mentions that a
        channel could reshape with nothing noticing. Telegram's override takes an
        extra optional ``failure_reason``, which is a legal widening of this
        contract and stays a channel-local concern until the pipeline has a
        reason to carry one.

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
    async def on_prompt_choice(self, options: list[dict[str, Any]], request_id: str | int) -> None:
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


class SilentRenderer(Renderer):
    """Renders nothing. The enforcement half of a dashboard channel disconnect.

    Disconnecting a channel means "stop talking to me there". Slack enforces that
    on its own dedicated streaming mirror, via the ``slack_mirror_is_paused``
    gates in the dashboard turn loop. Every OTHER channel drives its turns
    through the shared inbound pipeline instead, where the reply is written by
    the channel's own :class:`Renderer` — a path the dashboard never touches. So
    without this substitution a stored pause for a non-Slack conversation has
    nothing to gate, and a disconnected channel keeps answering as if it were
    still connected.

    ``dispatch.drive_turn`` substitutes this for the real renderer when the
    conversation is disconnected. The turn STILL RUNS and the inbound message
    still lands in the session: the binding is retained by design, and the
    dashboard is where that user is now working. Only the writes back to the
    muted conversation are dropped. ``on_turn_start`` inherits the base no-op, so
    no typing indicator is ever opened; ``close`` overrides only to tolerate a
    widened signature, because there is nothing to finalize either way.

    ``on_prompt_choice`` is dropped like the rest, matching the Slack gate that
    withholds the linked approval prompt from a disconnected thread: the
    dashboard renders the same prompt, and soliciting a decision in the
    conversation the user just left would ask where they are no longer looking.
    """

    def __init__(self, capabilities: Any = None, channel_type: str = "") -> None:
        # Typed loosely and defaulted, unlike the base: this is a SUBSTITUTE built
        # from whatever renderer the channel supplied, and it must not fail to
        # substitute because that object lacks `capabilities`. Nothing here reads
        # the value -- every handler is a no-op -- so it is only carried so the
        # object still satisfies the base contract for anyone who inspects it.
        super().__init__(capabilities)
        self.channel_type = channel_type

    async def close(self, *args: Any, **kwargs: Any) -> None:
        """Tolerate a channel's WIDENED close signature.

        The base declares a no-arg ``close``, but widening it is legal and
        Telegram does exactly that (``close(failure_reason=...)``) -- and its
        ``finally`` calls it that way unconditionally. Since this class stands in
        for whatever renderer the channel built, a strict signature here would
        turn a disconnected Telegram turn into a ``TypeError`` in a ``finally``.
        There is nothing to finalize either way: nothing was ever opened.
        """
        return None

    async def on_text_chunk(self, text: str) -> None:
        return None

    async def on_thinking(self, text: str) -> None:
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        return None

    async def on_prompt_choice(self, options: list[dict[str, Any]], request_id: str | int) -> None:
        return None

    async def on_compaction(self, context_usage_pct: float) -> None:
        return None

    async def on_done(self, stop_reason: str = "") -> None:
        return None
