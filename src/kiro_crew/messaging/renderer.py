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
Channels declaring ``max_buttons=0`` render no widget and route the whole
trailer through :func:`render_options_as_text`, which reaches the same helper
with zero widget slots: every choice becomes a numbered line the user answers by
typing, rather than being deleted along with the trailer.

Webex is the widget channel that ALSO always ships the numbered text: it declares
Adaptive Card actions, but the inbound half of a press rides an undocumented
websocket, so the typed form has to stay answerable on its own. It reaches
:func:`apply_options_cap` directly — the widget-channel path, which returns the
kept choices for the card as well as the body — rather than
:func:`render_options_as_text`, which keeps only the body.
"""

from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.constants import OPTIONS_RE_TRAILER, strip_control_comments
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
    # ``title``/``tool_purpose`` describe a tool on BOTH kinds that carry one:
    # tool_call announces it, prompt_choice asks permission for it. Carrying them
    # on the prompt is what lets a renderer name the tool the request is actually
    # about instead of the last one it happened to see.
    title: str = ""  # tool_call / prompt_choice (tool name / "Running: X")
    tool_kind: str = ""  # tool_call (e.g. "read"/"execute" — drives phase emoji)
    tool_purpose: str = ""  # tool_call / prompt_choice (human-readable purpose)
    options: list[dict[str, Any]] = field(default_factory=list)  # prompt_choice
    request_id: str | int = ""  # prompt_choice correlation
    # prompt_choice: the tool's own arguments, redacted, so a renderer can show
    # WHAT is being approved rather than only its name. "" when the provider
    # supplied none, which a renderer must treat as "no detail available" —
    # never as "no arguments".
    tool_input: str = ""
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
            "tool_input": self.tool_input,
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


def chunk_for_transport(text: str, capabilities: TransportCapabilities) -> list[str]:
    """Split *text* into parts the transport will accept, in ITS unit.

    Prefers ``max_message_bytes`` when the platform declares one, because a
    character count cannot express a byte cap without being wrong in one
    direction or the other: the only safe char value is the byte budget over four
    (the worst case for a 4-byte code point), which cuts an ASCII reply into
    quarters, while the true char cap would let a CJK reply exceed the byte limit
    and be truncated on send.

    BOTH paths are fence-aware: the byte path via
    :func:`~kiro_crew.messaging.split.split_markdown_bytes`, the char path via
    :func:`~kiro_crew.messaging.split.split_markdown_safe`. A blind fixed-width
    slice through a code block leaves the second chunk with no opener, so every
    line in it renders as prose and a channel's markdown-dialect converter
    rewrites the ``**``/``#``/``- `` INSIDE the code -- and a sub-agent diff or
    cron log dump is exactly that shape. Callers that want a raw fixed-width cut
    reach for :func:`chunk_text` directly.
    """
    # Local imports: split.py is a heavier pure-Python module and only these
    # paths need it, so the renderer contract stays cheap to import.
    #
    # ``getattr`` with the field's own ``0`` default, not attribute access: the
    # real ``TransportCapabilities`` always carries ``max_message_bytes``, but a
    # capabilities-shaped object from before the field existed must degrade to the
    # char path (``0`` = "no byte cap") rather than raising -- the same honest
    # default the dataclass declares.
    max_bytes = getattr(capabilities, "max_message_bytes", 0)
    if max_bytes > 0:
        from kiro_crew.messaging.split import split_markdown_bytes

        return split_markdown_bytes(text, max_bytes)
    from kiro_crew.messaging.split import split_markdown_safe

    return split_markdown_safe(text, capabilities.max_message_chars)


def cap_choices(
    choices: list[str], capabilities: TransportCapabilities
) -> tuple[list[str], list[str]]:
    """Split a parsed ``[OPTIONS:]`` list at ``capabilities.max_buttons``.

    Returns ``(kept, overflow)``. ``max_buttons <= 0`` keeps nothing and
    overflows everything, which is what makes a zero-widget channel the
    all-overflow case rather than a special case. Pure — callers that must
    transform choices before display (Slack redacts at the sink) split here and
    format overflow themselves via :func:`format_overflow`.
    """
    n = capabilities.max_buttons
    if n <= 0:
        return [], choices
    return choices[:n], choices[n:]


def display_safe_for(text: str, capabilities: TransportCapabilities) -> str:
    """:func:`display_safe`, with the mention defang applied only where it belongs.

    The channel-NEUTRAL proactive sinks (the dashboard's channel-addressed send and
    the owner-DM leg) render untrusted text into a message body on whichever
    transport they were handed, so they need the display-form credential redaction
    unconditionally -- and the broadcast-mention defang only on a platform that
    actually parses one.

    Webex is why this is a capability rather than a constant: it has no broadcast
    grammar AND its allow-list IS email addresses, so defanging inserts a
    zero-width space after every ``@`` and every address the agent prints becomes
    uncopyable. Its own renderer already avoids that (``webex_display_safe``); the
    neutral sinks read the declaration instead of importing a channel symbol,
    which is what keeps them neutral.

    Control-tag comments are stripped first, same as :func:`display_safe` —
    the deterministic backstop against a dashboard-authored control tag
    reaching channel users as literal text (#7948).
    """
    text = strip_control_comments(text or "")
    safe, _ = redact_for_display(text, _default_redactor)
    if not capabilities.mention_grammars:
        return safe
    return safe.replace("@", "@\u200b").replace("<!", "<\u200b!")


def session_provenance_tag(session_key: str) -> str:
    """A short, stable, non-reversible tag for the session that posted a widget.

    Option buttons can outlive the conversation that rendered them. The tag lets
    a dispatcher compare the posting session with the conversation's current
    target before model-authored choice text enters a turn. A digest keeps the
    internal key out of client-visible callback data; it is deterministic so the
    check survives a gateway restart. This is an equality gate, not an authority
    token: forging it grants no capability beyond typing the same text.
    """
    if not session_key:
        return ""
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]


def new_approval_nonce() -> str:
    """A per-prompt token that makes a STALE widget's press unusable.

    Shared because the hazard is: ACP request ids restart at 1 in every provider
    process, so an approve/deny control still sitting in a chat from a previous run
    names a request id that is live again for a DIFFERENT tool. Every channel with a
    clickable approval has to mint one, compare it on resolve, and retire it with the
    prompt -- and three independent copies of that is how one of them ends up with a
    weaker token or none at all. The session picker (``PickerRegistry.mint``) mints
    from here too: a press on a stale list of sessions is the same hazard wearing a
    different label, so it is not a reason for a second generator.

    ``token_urlsafe(8)`` is ~11 chars of 64 bits, which fits inside Telegram's
    64-BYTE ``callback_data`` cap alongside the request id and the decision.
    """
    return secrets.token_urlsafe(8)


def _default_redactor(text: str) -> str:
    """The same pair ``TurnDriver`` streams provider text through.

    Module scope on purpose: ``security`` is a pure-regex module with no vendor
    dependencies -- the same module ``messaging.driver`` imports directly for its
    own stream redaction -- so this adds no import-time cost and nothing that
    could touch an event loop.
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

    Control-tag comments are stripped first (fence/inline-code aware): channel
    formatters render HTML comments literally, so a dashboard-authored
    ``<!-- keep-visible -->`` (#7948) or ``deliver:``/``plan_task_id:`` tag
    delivered to a channel would otherwise reach end users as visible text.
    The prompt rule only contains the emitter; this is the deterministic
    backstop on the message itself.
    """
    text = strip_control_comments(text or "")
    safe, _ = redact_for_display(text, _default_redactor)
    return safe.replace("@", "@\u200b").replace("<!", "<\u200b!")


def _choice_display_safe(text: str, capabilities: TransportCapabilities | None) -> str:
    """The choice-label display sink, target-aware when the target is known.

    ``None`` means "no declaration to consult", which defangs unconditionally --
    the conservative direction, because a needless defang mangles text cosmetically
    while a missing one lets a prompt-injected ``@everyone`` mass-notify.
    """
    if capabilities is None:
        return display_safe(text)
    return display_safe_for(text, capabilities)


def format_overflow(
    overflow: list[str], start: int, capabilities: TransportCapabilities | None = None
) -> str:
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

    *capabilities* makes the mention half target-aware. Omitting it defangs
    unconditionally, which is the safe direction and what the three callers that
    render onto one known channel already rely on; a caller holding the target's
    declaration passes it so a platform with no broadcast grammar is not defanged
    into unusable text (see :func:`display_safe_for`). The credential half is
    unconditional either way -- no capability turns it off.
    """
    return "\n".join(
        f"{start + i + 1}. {_choice_display_safe(c, capabilities)}" for i, c in enumerate(overflow)
    )


def apply_options_cap(
    body: str, choices: list[str], capabilities: TransportCapabilities
) -> tuple[str, list[str]]:
    """Enforce ``capabilities.max_buttons`` on a parsed ``[OPTIONS:]`` list.

    The ``max_buttons`` analogue of :func:`chunk_text`. Widget-capable
    renderers call this between parsing the trailer and building the native
    widget, so the cap lives in shared code and the per-channel contract
    test can pin it.

    Returns ``(body, kept_choices)``: the first ``max_buttons`` choices are kept
    for the widget and the remainder is appended to ``body`` as a numbered text
    list, numbering continued after the widget slots, rather than dropped — so
    the user still learns those choices exist. A list that fits leaves ``body``
    byte-identical; the kept choices are still redacted (see below).

    ``max_buttons <= 0`` needs no branch of its own: :func:`cap_choices` keeps
    nothing and overflows everything, so a button-less channel is the
    all-overflow case and every choice becomes a numbered line through the same
    sanitising sink. Dropping the list there would delete the answers to a
    question the agent just asked and leave the user no way to see what was
    offered.

    **The KEPT choices are redacted, not just the overflow.** A choice label is
    LLM-authored text rendered into a channel, exactly like the overflow list, so
    redacting only the overflow half would leave a markup-split credential intact
    on the button -- and again in the press echo, which quotes the label back. On a
    forum Topic that is every allow-listed participant. Slack redacts at this same
    point (``slack/format.py``'s ``_redact_choices``); doing it here covers every
    widget channel at once rather than per renderer, so a channel added later
    cannot miss it.

    Both halves go through :func:`display_safe_for` rather than :func:`display_safe`,
    so the mention defang honours ``capabilities.mention_grammars`` -- which this
    function already holds. Redaction is unconditional; only the defang is target
    aware. Webex is the case that makes the difference visible: it parses no
    broadcast grammar and its allow-list IS email addresses, so defanging its
    Adaptive Card labels and numbered fallback would render every address the agent
    offers uncopyable, which is the cost the capability exists to avoid.
    """
    kept, overflow = cap_choices(choices, capabilities)
    kept = [display_safe_for(c, capabilities) for c in kept]
    if not overflow:
        return body, kept
    lines = format_overflow(overflow, start=len(kept), capabilities=capabilities)
    if not body:
        sep = ""
    elif body.endswith("\n"):
        sep = "\n"
    else:
        sep = "\n\n"
    return f"{body}{sep}{lines}", kept


def split_options_trailer(text: str, *, hide_partial: bool = False) -> tuple[str, list[str]]:
    """Split a trailing ``[OPTIONS:]`` marker off *text* into ``(body, choices)``.

    The ONE parse of that marker. Widget-capable renderers need both halves and
    :func:`render_options_as_text` returns only the body, so both reach the marker
    through here rather than every caller repeating the same steps: search the
    shared trailer regex, ``rstrip`` the body, split the group on ``|``, drop the
    blanks, and decide what to do with an unfinished marker. A parse duplicated per
    channel is a parse that drifts per channel, silently, because each copy looks
    right in isolation.

    Only a COMPLETE, end-anchored marker yields choices, and both halves of that
    matter:

    * A quoted ``[OPTIONS:`` mid-answer cannot swallow the body between it and
      some later ``]`` -- the end-of-buffer anchor is what prevents that.
    * An unfinished ``[OPTIONS`` tail is not a marker yet, so what to do with it
      is the CALLER's question, which is why it is a parameter rather than a
      policy baked in here.

    *hide_partial* is that question, and the channels genuinely answer it
    differently:

    * ``True`` -- a STREAMING surface (Discord, Telegram, Teams, WeCom, and Webex's
      status frame). The text is still arriving, so a partial marker really may be
      a marker mid-flight, and showing reserved protocol as raw text is the cost
      being avoided. Safe there precisely because the frame is transient: the next
      frame, or the sealed answer, re-renders from the full buffer.
    * ``False`` -- a BUFFERED surface that sends once (Slack's extraction, Webex's
      final answer, and this module's own zero-widget path). Such a caller cannot
      tell a live fragment from the assistant's prose, and cutting prose is
      PERMANENT data loss: a reply ending ``see the [OPTIONS section`` must keep
      its last four words.

    The default is ``False`` because the two failure directions are not
    symmetric -- a needless keep flashes reserved markup for one frame, a needless
    cut deletes text nobody can recover -- so a caller that forgets degrades
    toward the cosmetic failure. Every streaming caller states ``True``
    explicitly, which is also what makes the data-loss choice greppable.

    Stripping a genuine steering frame is ``TurnDriver``'s job and happens before
    a renderer sees the text.
    """
    match = OPTIONS_RE_TRAILER.search(text)
    if match:
        choices = [c.strip() for c in match.group(1).split("|") if c.strip()]
        return text[: match.start()].rstrip(), choices
    if hide_partial:
        idx = text.rfind("[OPTIONS")
        if idx != -1 and "]" not in text[idx:]:
            return text[:idx].rstrip(), []
    return text, []


def render_options_as_text(text: str, capabilities: TransportCapabilities) -> str:
    """Rewrite a trailing ``[OPTIONS:]`` trailer in *text* as numbered text.

    The whole trailer handling for a channel that renders no widget, so every
    channel that renders none shares one implementation instead of a copy each.
    Returns the body only; the widget half of :func:`apply_options_cap` has
    nothing to keep at ``max_buttons == 0``.

    Parsing is :func:`split_options_trailer`, at its buffered default: this path's
    callers do not stream — they buffer a whole turn and send once — so an
    unfinished ``[OPTIONS`` tail is the assistant's prose here and is kept. The one
    zero-widget channel that DOES stream (WeCom) asks for ``hide_partial=True`` in
    its own ``wecom.renderer._render_options_as_text``, where the cost is a
    transient flash whose next frame replaces the bubble anyway.

    ``apply_options_cap`` is reached unconditionally rather than behind an
    ``if not choices`` guard, which would NOT be equivalent: a matched-but-EMPTY
    trailer (``[OPTIONS: ]``) must still have the marker stripped. With no match
    ``split_options_trailer`` hands back the text unchanged, and the cap is the
    identity on an empty choice list, so one call covers all three cases.
    """
    body, choices = split_options_trailer(text)
    return apply_options_cap(body, choices, capabilities)[0]


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
    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        """Render an interactive approval/choice prompt (first-class).

        ``tool_title`` is the tool THIS request asks about, taken from the
        permission event itself, and ``tool_purpose`` is the purpose the matching
        ``tool_call`` declared. Name the tool from these, not from a remembered
        earlier ``on_tool_call``: a permission is not always immediately preceded
        by its own titled tool call, so a remembered name is the PREVIOUS tool's,
        and the operator would be consenting to something other than what they
        read. Both are defaulted, so a renderer that has no name to show stays
        valid; a renderer that keeps its own fallback should prefer these when
        they are non-empty and must not pair a supplied title with a remembered
        purpose from a different tool.

        ``tool_input`` is the tool's own arguments, already redacted, so a renderer
        can show WHAT is being approved rather than only its name. Declared with a
        safe default and passed unconditionally: a renderer that has nowhere to put
        it ignores the argument, which keeps the call site free of a capability
        probe. ``""`` means the provider supplied no detail, never "no arguments".
        """

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
            await self.on_prompt_choice(
                event.options,
                event.request_id,
                event.title,
                event.tool_purpose,
                event.tool_input,
            )
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

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        return None

    async def on_compaction(self, context_usage_pct: float) -> None:
        return None

    async def on_done(self, stop_reason: str = "") -> None:
        return None
