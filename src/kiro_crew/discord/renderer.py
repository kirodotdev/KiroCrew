"""Layer 2b -- Discord ``Renderer`` + interactive approval decider.

``DiscordRenderer`` maps the channel-neutral ``OutputEvent`` stream (routed by
the base :class:`Renderer`'s ``dispatch``) onto Discord's REST API:

* ``on_turn_start`` -- typing indicator loop (Discord's lasts ~10s per trigger)
  plus the status ladder's first reaction on the user's own message.
* ``on_text_chunk`` -- throttled in-place ``edit_message`` streaming, with any
  trailing ``[OPTIONS:]`` markup held back from the visible stream.
* ``on_thinking`` -- a ``-# 💭`` subtext note, only when ``discord.show_thinking``
  is on; off (the default) reasoning is never accumulated.
* ``on_tool_call`` -- a transient ``🔧 {tool}…`` footer on live frames.
* ``on_prompt_choice`` -- Approve/Deny buttons as a SEPARATE message (so
  streaming edits don't clobber them).
* ``on_compaction`` -- a lightweight "compacting…" note.
* ``on_done`` -- the final edit, splitting long output at the capability's
  char cap, attaching the ``[OPTIONS:]`` button rows to the last chunk, and
  closing with the one-line ``-#`` turn footer (elapsed + context usage).

Progress reactions are the shared ladder from
:mod:`kiro_crew.messaging.status_reactions`, driven through a sink bound to the
user's message: this renderer owns the emoji vocabulary and the REST route,
never the phase machine. ``discord.reactions_enabled`` turns the whole ladder
off.

Discord renders standard Markdown natively, so unlike Telegram there is no
HTML translation pass -- the final seal sends the markdown as-is. Steer
rotation (sealing the pre-steer segment and opening a fresh message headed by
a "↪️ steered" chip) mirrors the Telegram renderer.

Length splitting belongs to :func:`kiro_crew.messaging.split.split_markdown_safe`,
the shared fence-safe splitter. This renderer owns no fence grammar: it consumes
the splitter's streaming contract, which is that every chunk but the last is
sealed (a cut inside a fence carries a synthetic closer and the next chunk
reopens the original opener line) while the final chunk is deliberately left
OPEN. So each sealed chunk is posted verbatim and the final one is retained as
the live buffer, with nothing to append and nothing to undo.

``DiscordApprovalDecider`` is the interactive ladder's awaiter: ``__call__``
registers a Future keyed by ``session:request_id`` and awaits a button press,
denying by default on timeout; the interaction handler resolves it via
``resolve_global``.

Dependency direction is ``discord -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import split_trailing_protocol_suffix
from kiro_crew.discord.client import (
    DISCORD_MAX_FILE_BYTES,
    DISCORD_MAX_FILES_PER_MESSAGE,
    DISCORD_MAX_TEXT,
    DISCORD_MAX_TOTAL_UPLOAD_BYTES,
)
from kiro_crew.messaging.approval import APPROVAL_TIMEOUT_S
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.outbound_files import (
    ExtractLimits,
    OutboundFile,
    Rejection,
    extract_local_refs_off_loop,
    hide_local_refs,
    protected_ref_spans,
)
from kiro_crew.messaging.renderer import (
    Renderer,
    apply_options_cap,
    chunk_text,
    new_approval_nonce,
    session_provenance_tag,
    split_options_trailer,
)
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.messaging.status_reactions import (
    PHASE_QUEUED,
    PHASE_THINKING,
    PhaseReactionLadder,
    StallEmojis,
    format_turn_status,
    phase_for_tool_title,
)
from kiro_crew.messaging.tables import TABLE_POLICY_CARDS
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient
    from kiro_crew.providers.base import LLMProvider

logger = logging.getLogger(__name__)

_UPLOAD_LIMITS = ExtractLimits(
    max_files=DISCORD_MAX_FILES_PER_MESSAGE,
    max_total_bytes=DISCORD_MAX_TOTAL_UPLOAD_BYTES,
    max_file_bytes=DISCORD_MAX_FILE_BYTES,
)

_MAX_REJECTION_LINES = 3
_DISCORD_MENTION_AT_RE = re.compile(r"(?:(?<=<)@(?=[!&]?\d+>)|(?<!\w)@(?=(?i:everyone|here)\b))")


def _redact_all(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    return redact_credentials(text)[0]


def _redact_transformed(text: str) -> str:
    text, _ = redact_for_display(text, _redact_all)
    return _DISCORD_MENTION_AT_RE.sub("@\u200b", text)


# Discord's typing indicator lasts ~10s per trigger; refresh just under that
# for the duration of a turn.
_TYPING_REFRESH_S = 8.0

# Min seconds between live streaming edits to one message. Discord rate-limits
# message edits (~5/5s per channel), so we coalesce chunks and edit in place at
# most this often. The final edit always lands regardless of throttle.
_EDIT_THROTTLE_S = 1.2

# Interactive approval wait; deny-by-default when it elapses with no press.
# Owned by messaging.approval so every channel's window is the same one.
_APPROVAL_TIMEOUT_S = APPROVAL_TIMEOUT_S

#: Phase → reaction for the shared status ladder. Unicode only: Discord's
#: reaction route takes the emoji itself as a path segment, so a Slack-style
#: shortcode would be added literally and rejected. The meanings mirror the
#: Slack ladder so someone reading both channels reads one story, and each mark
#: is a single code point, which keeps percent-encoding out of the picture.
DISCORD_PHASE_EMOJIS: dict[str, str | None] = {
    "queued": "👀",
    "thinking": "🤔",
    "coding": "💻",
    "browsing": "🌐",
    "tool": "🔧",
    "done": "🦞",
    "error": "😱",
}
#: Additive marks for a turn that has gone quiet (soft first, then hard).
DISCORD_STALL_EMOJIS = StallEmojis(soft="🥱", hard="😨")

#: Max reasoning surfaced when ``discord.show_thinking`` is on. Subtext is grey
#: and unscannable in bulk, so the note is a preview: the full reasoning stays in
#: the dashboard Activity panel.
_THINKING_PREVIEW_CHARS = 600

# Button style constants (Discord component styles).
_STYLE_PRIMARY = 1
_STYLE_SECONDARY = 2
_STYLE_SUCCESS = 3
_STYLE_DANGER = 4

# Discord component layout limits: an action row holds at most 5 buttons, and a
# legacy (non-Components-V2) message holds at most 5 action rows.
_BUTTONS_PER_ROW = 5
_MAX_ACTION_ROWS = 5
#: Platform hard-limit backstop for any button set. A caller's own cap decides
#: how many choices to offer; this only guarantees the payload Discord accepts.
_MAX_BUTTONS = _BUTTONS_PER_ROW * _MAX_ACTION_ROWS
#: Component-spec ceiling on a button label.
_BUTTON_LABEL_CHARS = 80

# kiro-cli's inline "[STEERING steer-<id>: …]" steer-ack marker (see the
# Telegram renderer for the full rationale — Discord likewise has no parser).
_STEER_MARKER_RE = re.compile(r"\[STEERING\b[^\]\r\n]*\]", re.IGNORECASE)
_STEER_SUMMARY_RE = re.compile(r"\[STEERING\s+steer-[0-9a-f]+\s*:\s*([^\]\r\n]*)\]", re.IGNORECASE)


def _extract_options(text: str) -> tuple[str, list[str]]:
    """Split text into ``(body, options)``, holding back a streamed partial.

    ``hide_partial=True`` because this renderer STREAMS: a still-arriving
    ``[OPTIONS…`` fragment really may be a marker mid-flight, and the next frame
    re-renders from the full buffer, so hiding it costs nothing permanent.
    """
    return split_options_trailer(text, hide_partial=True)


def _strip_steering(text: str) -> str:
    """Remove kiro-cli's inline ``[STEERING …]`` steer-ack marker from output,
    including an UNCLOSED trailing fragment still streaming in (see the
    Telegram renderer for the show-then-vanish rationale)."""
    cleaned = _STEER_MARKER_RE.sub("", text)
    cleaned = re.sub(r"\[STEERING\b[^\]\r\n]*$", "", cleaned)  # unclosed, streaming
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _transform_buries_refs(canonical: str, presented: str) -> bool:
    """True when a presentation transform hides a local ref extraction needs.

    Upload extraction reads the DELIVERED text, so a transform that fences an
    in-cell image leaves no extractable span and the raw filesystem path would
    ship as literal content.

    Compares COUNTS rather than emptiness because loss is per-ref: a message
    carrying a prose image beside a grid-form table keeps the prose span while
    losing the in-cell one, which would upload the first picture and expose the
    second one's path. Any drop is a loss.
    """
    return len(protected_ref_spans(presented)) < len(protected_ref_spans(canonical))


def _as_subtext(text: str) -> str:
    """Render *text* as Discord subtext.

    The ``-# `` marker applies to ONE line, so a multi-line note needs it on
    each; a blank line would end the block, so blank lines are dropped rather
    than emitted as a bare marker.
    """
    lines = (line.strip() for line in text.splitlines())
    return "\n".join(f"-# {line}" for line in lines if line)


def _own_reaction_route(channel_id: str, message_id: str, emoji: str) -> str:
    """REST route for the bot's OWN reaction on a message.

    ``PUT`` adds it and ``DELETE`` removes it, so one route carries both halves
    of the status ladder's swap. The emoji is percent-encoded with nothing safe:
    it is a path segment here, not a query value.
    """
    encoded = urllib.parse.quote(emoji, safe="")
    return f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"


class _MessageReactionSink:
    """The status ladder's emoji sink for one Discord message.

    Routes through the client's public request ladder so both verbs share its
    rate-limit accounting and retry budget. Reaction edits are rate-limited per
    channel, which is what the ladder's debounce exists to respect; a rejected
    edit reports itself and is dropped, since a reaction is never the turn.
    """

    def __init__(self, client: "DiscordClient", channel_id: str, message_id: str) -> None:
        self._client = client
        self._channel_id = channel_id
        self._message_id = message_id

    async def _edit(self, method: str, emoji: str) -> None:
        route = _own_reaction_route(self._channel_id, self._message_id, emoji)
        result = await self._client.api_json(method, route, None)
        if not result:
            logger.debug("discord: reaction %s %s failed (%s)", method, emoji, result.detail)

    async def add(self, emoji: str) -> None:
        await self._edit("PUT", emoji)

    async def remove(self, emoji: str) -> None:
        await self._edit("DELETE", emoji)


def _neutralize_md(raw: str) -> str:
    """Collapse whitespace, cap length, and strip Markdown control chars from a
    steer's text so the chip renders literally (inside a blockquote) and can't
    perturb surrounding formatting."""
    t = " ".join((raw or "").split())[:120]
    return re.sub(r"[*_`\[\]()]", "", t)


def build_option_components(options: list[str], origin_tag: str = "") -> list[dict] | None:
    """Build Discord button action rows from ``[OPTIONS:]`` labels.

    ``custom_id`` is ``opt:<i>:<origin_tag>`` (``opt:<i>`` when no tag is given --
    the pre-provenance legacy shape) -- Discord caps it at 100 chars, which the
    12-hex tag fits comfortably, and the label is recovered from the button text
    at interaction time. Labels cap at 80 chars per the component spec. The
    ``max_buttons`` cap is applied UPSTREAM via ``apply_options_cap`` (overflow
    degrades to numbered text); the slice below is the platform hard-limit
    backstop only.
    """
    if not options:
        return None
    suffix = f":{origin_tag}" if origin_tag else ""
    rows: list[dict] = []
    row: list[dict] = []
    for i, opt in enumerate(options[:_MAX_BUTTONS]):
        row.append(
            {
                "type": 2,  # button
                "style": _STYLE_SECONDARY,
                "label": opt[:_BUTTON_LABEL_CHARS],
                "custom_id": f"opt:{i}{suffix}",
            }
        )
        if len(row) == _BUTTONS_PER_ROW:
            rows.append({"type": 1, "components": row})  # action row
            row = []
    if row:
        rows.append({"type": 1, "components": row})
    return rows


def build_model_components(choices: Sequence[tuple[str, str]], current: str) -> list[dict] | None:
    """Build the ``!model`` button rows from ``(model_id, label)`` choices.

    ``custom_id`` is the INDEX only (``m:<i>``), never the model id: Discord caps
    a custom_id at 100 characters and replays old component ids indefinitely, so
    an id embedded here would both risk the cap and outlive the advertised set it
    came from. The dispatcher's picker registry resolves the index back against
    the exact list it posted, and refuses once that list has expired.

    The current pick is marked with a bullet and rendered in the primary style,
    so the active model is readable without pressing anything.
    """
    if not choices:
        return None
    rows: list[dict] = []
    row: list[dict] = []
    for i, (model_id, label) in enumerate(choices[:_MAX_BUTTONS]):
        picked = model_id == current
        row.append(
            {
                "type": 2,  # button
                "style": _STYLE_PRIMARY if picked else _STYLE_SECONDARY,
                "label": (f"• {label}" if picked else label)[:_BUTTON_LABEL_CHARS],
                "custom_id": f"m:{i}",
            }
        )
        if len(row) == _BUTTONS_PER_ROW:
            rows.append({"type": 1, "components": row})  # action row
            row = []
    if row:
        rows.append({"type": 1, "components": row})
    return rows


def _fit_platform_cap(text: str) -> list[str]:
    """Slice *text* into payloads Discord's message API will accept whole.

    ``split_markdown_safe`` budgets every chunk against :meth:`_limit`, with one
    documented exception: a logical line that admits no cut clean on both sides
    is placed WHOLE rather than cut into a fence delimiter its source never
    contained, and the chunk carries its fence scaffolding — the reopener line
    plus the newline and synthetic closer — on top of the limit. The 100
    characters :meth:`_limit` holds back absorb ordinary scaffolding, but an
    opener line long enough (a several-hundred-backtick run, a huge info string)
    still pushes such a chunk past Discord's hard cap. ``client.send_message``
    truncates to that cap, which drops the tail INCLUDING the synthetic closer,
    so the user reads an unterminated code block missing content and gets no
    signal that anything was lost.

    Blind fixed-width slicing is the right last resort in exactly that regime:
    it keeps every authored character at the price of a boundary Markdown may
    render badly, where truncation keeps neither. Nothing here re-derives fence
    grammar — the splitter owns that, and this only bounds what reaches the API.
    """
    return chunk_text(text, DISCORD_MAX_TEXT) or [text]


class DiscordApprovalDecider:
    """Awaits a button approval for a tool-permission request.

    Process-global Future registry keyed by ``session_key:request_id`` so
    concurrent turns (and users) never resolve each other's prompts. Denies by
    default when the wait elapses.

    Nonce guard: ACP request IDs are reusable (a provider or gateway restart
    resets the sequence), so a stale Approve button whose ``custom_id`` carries
    an old request ID could otherwise resolve a NEW pending request for an
    unrelated tool. Each rendered prompt therefore embeds an unpredictable
    per-prompt nonce (``register_nonce``), and ``resolve_global`` only resolves
    when the pressed button's nonce matches the one registered for that key —
    a press from any earlier prompt (or earlier process) fails closed.
    """

    _REGISTRY: dict[str, "asyncio.Future[bool]"] = {}
    #: key -> the per-prompt nonce embedded in that prompt's buttons.
    _NONCES: dict[str, str] = {}

    def __init__(self, *, session_key: str) -> None:
        self._session_key = session_key

    @staticmethod
    def key(session_key: str, request_id: str | int) -> str:
        return f"{session_key}:{request_id}"

    @classmethod
    def register_nonce(cls, key: str) -> str:
        """Mint + register the per-prompt nonce for *key* (renderer-side)."""
        nonce = new_approval_nonce()
        cls._NONCES[key] = nonce
        return nonce

    async def __call__(self, event: Any) -> bool:
        k = self.key(self._session_key, getattr(event, "request_id", ""))
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[k] = fut
        try:
            return bool(await asyncio.wait_for(fut, _APPROVAL_TIMEOUT_S))
        except asyncio.TimeoutError:
            # Nobody pressed a button for the whole window, so a monitoring loop
            # bound to this session cannot act either -- record it so the loop
            # stops on its next wake instead of spending the rest of its cycle
            # cap being denied. Inert for a session with no loop
            # (``notify_approval_stalled`` resolves by binding key), and
            # best-effort: a monitoring convenience must never change how this
            # turn's denial is reported.
            try:
                from kiro_crew.autonudge import get_instance as _autonudge_get

                _autonudge = _autonudge_get()
                if _autonudge is not None:
                    _autonudge.notify_approval_stalled(self._session_key)
            except Exception:
                logger.debug("autonudge.notify_approval_stalled failed", exc_info=True)
            return False  # deny-by-default on timeout
        finally:
            DiscordApprovalDecider._REGISTRY.pop(k, None)
            # Retire the prompt's nonce with the decision window: a press on
            # the (now stale) buttons can never resolve a future request.
            DiscordApprovalDecider._NONCES.pop(k, None)

    @classmethod
    def resolve_global(cls, key: str, approved: bool, *, nonce: str = "") -> bool:
        """Resolve a pending approval by key. Returns True iff one was waiting
        AND the button's nonce matches the registered per-prompt nonce."""
        expected = cls._NONCES.get(key)
        if not expected or not nonce or not secrets.compare_digest(nonce, expected):
            return False  # stale/foreign button — fail closed
        fut = cls._REGISTRY.get(key)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False


class DiscordRenderer(Renderer):
    """Streams a turn to Discord via in-place message edits + button rows."""

    channel_type = "discord"

    def __init__(
        self,
        client: "DiscordClient",
        channel_id: str,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
        uploads_allowed: bool = False,
        upload_root: str = "",
        react_message_id: str = "",
        reactions_enabled: bool = True,
        show_thinking: bool = False,
        now: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._channel_id = channel_id
        self._session_key = session_key
        # Monotonic clock seam: injectable so the turn footer and the edit
        # throttle are deterministic in tests.
        self._now = now or time.monotonic
        # The user's own message, which the status ladder reacts on. Empty (a
        # caller with no message to mark, e.g. an injected turn) means no ladder.
        self._react_message_id = react_message_id
        self._reactions_enabled = reactions_enabled
        self._ladder: PhaseReactionLadder | None = None
        # Set while the turn waits on a human approval, so the stall watchdog
        # does not mark a turn that is behaving correctly.
        self._ladder_paused = False
        # When discord.show_thinking is on, reasoning is surfaced as its own
        # subtext note. Off (the default) it is never accumulated at all.
        self._show_thinking = show_thinking
        self._thinking = ""
        self._thinking_posted = False
        # Read at turn end for the footer's context chip; unbound until the
        # dispatcher hands over the session's provider.
        self._context_source: "LLMProvider | None" = None
        # Turn clock for the footer. Started at construction rather than at
        # on_turn_start because the renderer is built at the head of the turn and
        # the cold start (spawning/handshaking a session) is time the user waited.
        self._t0 = self._now()
        self._upload_root = upload_root if os.path.isabs(upload_root) else ""
        self._uploads_allowed = uploads_allowed
        self._buf: list[str] = []
        self._segment_uploads_safe = True
        # Delivery transforms never enter the canonical protocol buffer. A
        # snapshot exists only when outbound presentation differs from source.
        self._delivery_text: str | None = None
        self._last_tool = ""
        # Transient tool-activity footer ("🔧 {tool}…") shown ONLY on live
        # streaming frames — never stored in _buf, so seals/finals stay clean.
        self._tool = ""
        self._finalized = False
        self._closed = False
        self._typing_task: "asyncio.Task[None] | None" = None
        # Live edit-streaming state (mirrors the Telegram renderer): the
        # message being edited (None -> next render sends a new one), the last
        # text pushed (skip no-op edits), and the edit throttle timestamp.
        self._stream_mid: str | None = None
        self._shown = ""
        # Delivery accounting for `delivery_failed`: how many seals were tried
        # and how many actually reached Discord.
        self._seals_attempted = 0
        self._seals_landed = 0
        self._last_edit = 0.0
        # A valid table at the end of a stream may still receive rows. While it
        # is pending, keep it in the buffer instead of freezing a partial card
        # or splitting raw pipes across messages; the final pass converts it.
        self._table_pending = False
        self._seal_count = 0  # rotations so far == index into _steer_texts
        # Chip pending from the last rotation, NOT yet in _buf (materializes
        # only when real post-steer text arrives — see the Telegram renderer).
        self._pending_chip = ""
        # User's own mid-turn steer texts (in order), recorded via note_steer.
        self._steer_texts: list[str] = []

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        # Typing indicator plus the "queued" reaction — no placeholder bubble.
        # Idempotent (dispatch + driver both call this).
        if self._typing_task is not None or self._closed:
            return
        self._ensure_ladder()  # set_phase("queued")
        self._typing_task = asyncio.create_task(self._typing_loop())

    # -- status ladder ------------------------------------------------------
    def _ensure_ladder(self) -> PhaseReactionLadder | None:
        """Lazily arm the shared phase ladder at ``queued``.

        Returns ``None`` when reactions are off for this channel, when the
        transport cannot react, or when there is no message to react on: the
        ladder marks the USER's message, and there is nothing to decorate
        without one.
        """
        if self._ladder is not None:
            return self._ladder
        if not self._reactions_enabled or not self._react_message_id:
            return None
        if not self.capabilities.reactions or self._closed:
            # A late event after teardown must not arm a fresh ladder, whose
            # timers would then outlive the turn.
            return None
        self._ladder = PhaseReactionLadder(
            _MessageReactionSink(self._client, self._channel_id, self._react_message_id),
            emojis=DISCORD_PHASE_EMOJIS,
            stall=DISCORD_STALL_EMOJIS,
        )
        self._ladder.set_phase(PHASE_QUEUED)
        return self._ladder

    def _set_phase(self, phase: str) -> None:
        ladder = self._ensure_ladder()
        if ladder is not None:
            ladder.set_phase(phase)
            self._note_progress()

    def _note_progress(self) -> None:
        """Tell the ladder the turn is moving, un-pausing it after an approval."""
        ladder = self._ladder
        if ladder is None:
            return
        if self._ladder_paused:
            self._ladder_paused = False
            ladder.resume_stall_watchdog()
        else:
            ladder.on_progress()

    async def _typing_loop(self) -> None:
        """Keep the 'typing…' indicator alive (~10s per trigger) for the
        duration of the turn. Cancelled by ``_stop_typing``."""
        try:
            while not self._closed:
                try:
                    await self._client.send_typing(self._channel_id)
                except Exception:
                    logger.debug("Discord: typing refresh failed", exc_info=True)
                await asyncio.sleep(_TYPING_REFRESH_S)
        except asyncio.CancelledError:
            pass

    def _stop_typing(self) -> None:
        self._closed = True
        task, self._typing_task = self._typing_task, None
        if task is not None and not task.done():
            task.cancel()

    async def on_text_chunk(self, text: str) -> None:
        self._set_phase(PHASE_THINKING)
        # Land any accumulated reasoning first so it reads above the answer
        # (no-op when show_thinking is off or nothing accumulated).
        await self._flush_thinking()
        self._buf.append(text)
        self._delivery_text = None
        self._tool = ""  # text resumed -> drop the transient tool footer
        # 1) Rotate to a fresh message at each COMPLETE [STEERING …] marker.
        await self._rotate_at_markers()
        # 1b) Materialize the pending chip once real post-steer text exists.
        self._materialize_chip()
        # 1c) Convert finished tables before anything measures the segment.
        await self._convert_tables(final=False)
        # A valid trailing table may still receive rows. Splitting or displaying
        # it now would either freeze a partial card or expose raw pipes; keep the
        # segment buffered until prose terminates the run or the turn finishes.
        if not self._table_pending:
            # 2) Rotate when a segment would exceed one Discord message.
            await self._rotate_on_length()
            # 3) Live-stream the current segment (throttled in-place edit).
            await self._stream_live()

    def _render_tables_for_delivery(self, raw: str, *, final: bool) -> str:
        """Adapt tables without handing a generated fence to message rotation."""
        converted = self.render_tables_for_target(raw, final=final)
        if converted != raw and len(converted) > self._limit():
            # Generated grids can require a longer fence when a cell contains
            # backticks. Prefer cards because they remain independently readable
            # across ordinary text chunks; an unrepresentable card run reports
            # its retained grid so a fitting safe raw table can stay whole.
            forced, generated_grid = self.render_tables_for_target_with_metadata(
                raw,
                policy=TABLE_POLICY_CARDS,
                final=final,
            )
            if generated_grid and len(forced) > self._limit():
                safe_raw = self.safe_raw_table_fallback(
                    raw,
                    policy=TABLE_POLICY_CARDS,
                    final=final,
                )
                if safe_raw is not None and len(safe_raw) <= self._limit():
                    return safe_raw
            return forced
        return converted

    async def _convert_tables(self, *, final: bool) -> None:
        """Refresh outbound table presentation while preserving canonical text.

        Protocol recognition always reads ``_buf``. The rendered snapshot is
        used only for streaming, sizing, and delivery, so card text that happens
        to resemble a control marker remains visible content. A transformed
        segment that already exceeds Discord's cap stays buffered until a
        terminal seal, when its presentation can be split without needing to
        map display offsets back onto still-growing Markdown source.

        A card is abandoned when it would bury a local image ref on a transport
        that can upload one: a delivered picture outranks table presentation,
        and shipping the raw path as text would both lose the upload and expose
        a local path. Such a segment degrades to raw pipes with a working image.
        """
        raw = _strip_steering("".join(self._buf))
        converted = self._render_tables_for_delivery(raw, final=final)
        self._delivery_text = converted if converted != raw else None
        if (
            self._delivery_text is not None
            and self._uploads_enabled()
            and self._segment_uploads_safe
            and await asyncio.to_thread(_transform_buries_refs, raw, converted)
        ):
            self._delivery_text = None
        if final:
            self._table_pending = False
            return

        final_render = self._render_tables_for_delivery(raw, final=True)
        trailing_table = converted != final_render
        transformed_over_limit = self._delivery_text is not None and len(converted) > self._limit()
        self._table_pending = trailing_table or transformed_over_limit

    def _take_canonical_options(self) -> list[str]:
        """Remove an authored OPTIONS trailer before presentation transforms.

        Table cards can join separate cells into text that resembles protocol.
        Extracting once from the canonical buffer ensures only an actual model
        directive becomes controls; generated display text remains content.
        """
        body, options = _extract_options("".join(self._buf))
        self._buf = [body]
        self._delivery_text = None
        return options

    def _materialize_chip(self) -> None:
        """Prepend the pending steer chip to the segment — but only when the
        segment carries real text (an end-of-stream marker never posts a
        chip-only ack bubble)."""
        if self._pending_chip and self._segment_text().strip():
            body = "".join(self._buf).lstrip("\n")
            self._buf = [f"{self._pending_chip}\n\n{body}"]
            self._delivery_text = None
            self._pending_chip = ""

    async def on_steer_consumed(self, summary: str = "") -> None:
        """Seal the pre-steer segment at the driver's structured boundary."""
        self._materialize_chip()
        # Protocol is recognized only in canonical output. A delivery transform
        # may create marker-shaped text, but that remains ordinary content.
        opts = self._take_canonical_options()
        # This segment is terminal, so a trailing table can no longer grow.
        await self._convert_tables(final=True)
        await self._rotate_on_length()
        body_text, opts = apply_options_cap(self._segment_text(), opts, self.capabilities)
        self._buf = []
        self._delivery_text = body_text
        # apply_options_cap may EXPAND the body (numbered overflow lines), and
        # the rotation above ran before that expansion -- re-check, or a
        # near-limit answer with over-cap options seals past the transport cap.
        await self._rotate_on_length()
        components = (
            build_option_components(opts, session_provenance_tag(self._session_key))
            if opts
            else None
        )
        sealed = bool(self._segment_text().strip()) or components is not None
        await self._seal_current(components=components)
        clean_summary = _neutralize_md(summary)
        if clean_summary:
            chip: str | None = "> ↪️ " + clean_summary
        else:
            chip = self._chip_for_seal(self._seal_count)
        self._seal_count += 1
        self._pending_chip = chip or ""
        self._buf = []
        self._segment_uploads_safe = True
        self._delivery_text = None
        if sealed:
            self._open_new_message()

    async def _rotate_at_markers(self) -> None:
        """Defence for callers that bypass TurnDriver and pass raw markers."""
        while True:
            self._materialize_chip()
            raw = "".join(self._buf)
            marker = _STEER_MARKER_RE.search(raw)
            if marker is None:
                return
            self._buf = [raw[: marker.start()]]
            self._delivery_text = None
            summary_match = _STEER_SUMMARY_RE.match(raw, marker.start())
            summary = _neutralize_md(summary_match.group(1)) if summary_match else ""
            await self.on_steer_consumed(summary)
            self._buf = [raw[marker.end() :]]
            self._delivery_text = None

    async def _rotate_on_length(self) -> None:
        """Rotate overlong output while retaining local refs for semantic seals."""
        limit = self._limit()
        delivery_text = self._delivery_text
        presentation_only = delivery_text is not None
        candidate = delivery_text if delivery_text is not None else "".join(self._buf)
        if len(candidate) <= limit:
            return
        if presentation_only:
            # Terminal table presentation has no future source chunks to map
            # back onto. Splitting it here seals the chunks verbatim with
            # extraction disabled, so the fast path is only correct while the
            # presented text carries nothing to extract -- which is not implied
            # by it being a transform, since a card can preserve a ref that a
            # verbatim seal would then ship as a literal path. When something IS
            # extractable, leave the segment whole: the semantic seal extracts
            # once from complete context and bounds the result itself.
            if self._uploads_enabled() and self._segment_uploads_safe:
                if await asyncio.to_thread(protected_ref_spans, candidate):
                    return
            chunks = await asyncio.to_thread(split_markdown_safe, candidate, limit)
            for chunk in chunks[:-1]:
                self._buf = []
                self._delivery_text = chunk
                await self._seal_current(extract_uploads=False)
                self._open_new_message()
            tail = chunks[-1] if chunks else ""
            self._buf = []
            self._delivery_text = tail
            return
        raw, protocol_suffix = split_trailing_protocol_suffix("".join(self._buf))
        # Keep the first complete/still-arriving local image and its suffix in
        # the live tail; splitter-produced chunks are never extraction inputs.
        spans = await asyncio.to_thread(protected_ref_spans, raw)
        if spans:
            hold_at = spans[0][0]
            if hold_at == 0:
                self._buf = [raw + protocol_suffix]
                self._delivery_text = None
                return
            split_source, tail = raw[:hold_at], raw[hold_at:]
            chunks = await asyncio.to_thread(split_markdown_safe, split_source, limit)
            sealed = chunks
        else:
            split_source = raw
            chunks = await asyncio.to_thread(split_markdown_safe, split_source, limit)
            sealed, tail = chunks[:-1], chunks[-1] if chunks else ""
            probe_at = len(prefix := raw.removesuffix(tail))
            probe = prefix + "![x](/tmp/x.png)" + " ".join(re.findall(r"`+", prefix)) + tail
            spans = await asyncio.to_thread(protected_ref_spans, probe) if sealed else []
            lost = bool(sealed) and raw.endswith(tail) and probe_at not in dict(spans)
            dirty_cut = any(len(line) > limit for line in split_source.splitlines(True))
            if dirty_cut or lost:
                self._segment_uploads_safe = False
        for ch in sealed:
            self._buf = [ch]
            self._delivery_text = None
            await self._seal_current(extract_uploads=False)
            self._open_new_message()
        self._buf = [tail + protocol_suffix]
        self._delivery_text = None

    def _open_new_message(self) -> None:
        """Next render creates a fresh message instead of editing the old one."""
        self._stream_mid = None
        self._shown = ""

    def _segment_text(self) -> str:
        """Current outbound text, with protocol removed only from canonical source.

        Presentation snapshots are derived only after canonical protocol
        handling and must be returned verbatim: parsing them again could
        reinterpret table cards as control markers.
        """
        if self._delivery_text is not None:
            return self._delivery_text
        return _strip_steering("".join(self._buf))

    async def _stream_live(self, *, force: bool = False) -> None:
        """Throttled in-place edit of the current segment. Sends the message on
        first render. The transient ``🔧 {tool}…`` footer is appended ONLY here
        (live frames) — seals and the final render never carry it. ``force``
        bypasses the throttle so a tool-call event surfaces immediately."""
        if self._table_pending:
            return
        now = self._now()
        if not force and now - self._last_edit < _EDIT_THROTTLE_S:
            return
        # Hold back trailing [OPTIONS:] markup only when canonical source owns
        # it. Running the parser on presentation alone could hide authored card
        # text that merely resembles an incomplete directive.
        visible = self._segment_text()
        canonical = _strip_steering("".join(self._buf))
        canonical_body, _ = _extract_options(canonical)
        if canonical_body != canonical:
            body, _ = _extract_options(visible)
        else:
            body = visible
        if self._uploads_enabled() and self._segment_uploads_safe:
            # Keep image markup off live frames only while a later seal can
            # actually turn it into an attachment.
            body = await asyncio.to_thread(hide_local_refs, body)
        # Unconditional: display-form redaction is a floor, not a side effect of
        # the upload path. Gating it on ``_uploads_enabled()`` meant a restricted
        # session, an unset upload root, or a channel with ``files_outbound``
        # off streamed model text to Discord with only the LITERAL-form redaction
        # ``TurnDriver`` applies — and the display pass exists precisely for the
        # credential that is invisible until Discord renders the markdown away.
        body = _redact_transformed(body)
        footer = f"-# 🔧 {self._tool}…" if self._tool else ""
        if footer:
            room = self._limit() - len(footer) - 2
            text = f"{body[:room]}\n\n{footer}".strip() if room > 0 else footer
        else:
            text = body[: self._limit()]
        if not text or text == self._shown:
            return
        self._last_edit = now
        self._shown = text
        if self._stream_mid is None:
            mid = await self._client.send_message(self._channel_id, text)
            if mid is not None:
                self._stream_mid = mid
        else:
            await self._client.edit_message(self._channel_id, self._stream_mid, text)

    def authorize_upload_root(self, root: str) -> None:
        """Authorize the provider's resolved cwd; invalid roots disable uploads."""
        self._upload_root = root if os.path.isabs(root) else ""

    def _uploads_enabled(self) -> bool:
        """Require transport capability, an unrestricted session, and a trusted root."""
        return (
            bool(self.capabilities.files_outbound)
            and self._uploads_allowed
            and bool(self._upload_root)
        )

    async def _extract_uploads(self, text: str) -> tuple[str, list[OutboundFile]]:
        """Extract each sealed segment once, off-loop and fail-soft."""
        try:
            result = await extract_local_refs_off_loop(
                text, within_root=self._upload_root, limits=_UPLOAD_LIMITS
            )
        except Exception:
            logger.warning("discord: outbound file extraction failed", exc_info=True)
            return text, []
        if result.rejections:
            sel().log_api_access(
                caller=self._session_key or "discord",
                operation="discord_renderer.upload_files",
                outcome="denied",
                source="discord",
                resources=f"{len(result.rejections)} rejection(s)",
                error=",".join(sorted({item.reason for item in result.rejections})),
            )
        body = result.rewritten_text.strip()
        if not body and not result.files:
            body = text
        if result.rejections:
            body = self._append_rejections(body, result.rejections)
        body = _redact_transformed(body)
        if result.files:
            sel().log_api_access(
                caller=self._session_key or "discord",
                operation="discord_renderer.upload_files",
                outcome="allowed",
                source="discord",
                resources=f"{len(result.files)} file(s)",
            )
        return body, result.files

    def _append_rejections(self, body: str, rejections: list[Rejection]) -> str:
        """Append refusal reasons only when the answer budget permits."""
        for rejection in rejections:
            logger.info("discord: local image not uploaded (%s)", rejection.reason)
        lines = [f"-# ⚠️ {rejection}" for rejection in rejections[:_MAX_REJECTION_LINES]]
        if len(rejections) > _MAX_REJECTION_LINES:
            lines.append(f"-# ⚠️ …and {len(rejections) - _MAX_REJECTION_LINES} more")
        note = "\n".join(lines)
        if len(body) + len(note) + 2 > self._limit():
            return body
        return f"{body}\n\n{note}"

    async def _land_sealed(
        self,
        text: str,
        files: list[OutboundFile],
        components: list[dict] | None,
    ) -> bool:
        """Edit first, then send; fail softly so recovery can restore markup."""
        self._seals_attempted += 1
        try:
            if self._stream_mid is not None:
                if await self._client.edit_message_with_files(
                    self._channel_id, self._stream_mid, text, files, components=components
                ):
                    self._seals_landed += 1
                    return True
                # A missing live message falls through to a fresh send.
                self._stream_mid = None
            landed = (
                await self._client.send_message_with_files(
                    self._channel_id, text, files, components=components
                )
                is not None
            )
            if landed:
                self._seals_landed += 1
            return landed
        except Exception:
            logger.warning("discord: sealing the segment failed", exc_info=True)
            return False

    @property
    def delivery_failed(self) -> bool:
        """True when this renderer tried to land output and NOTHING arrived.

        The dispatcher records a turn's outcome from what the provider produced,
        which says nothing about whether the user ever saw it: a revoked token or
        a lost network makes every send fail while the turn still returns its
        accumulated text, and the turn is then filed as a success with no reply
        anywhere. This is the observable that distinguishes the two.

        Deliberately "attempted and none landed" rather than "any failed": a
        single failed length-rotation whose retry succeeded still reached the
        user, and treating that as a failed turn would trade a silent success for
        a false alarm.
        """
        return self._seals_attempted > 0 and self._seals_landed == 0

    async def _seal_current(
        self,
        *,
        components: list[dict] | None = None,
        extract_uploads: bool = True,
    ) -> None:
        """Land one segment; only semantic seals may extract local images.

        Length rotations pass ``extract_uploads=False`` and seal shared-splitter
        chunks verbatim. Semantic steer/final seals extract once from complete
        source context, then split the transformed text. Every payload is bounded
        again for the shared splitter's documented scaffolding exception.
        """
        source = self._segment_text()
        files: list[OutboundFile] = []
        if extract_uploads and source and self._uploads_enabled() and self._segment_uploads_safe:
            # ``_extract_uploads`` applies the display sink itself, on the text it
            # rewrote — the removal of image markup is one of the transforms that
            # can reassemble a credential, so it has to be redacted after.
            text, files = await self._extract_uploads(source)
        else:
            # Every other route to this sink — a length rotation, a restricted
            # session, an unset upload root — carries model text too, and the
            # display pass is a floor rather than a consequence of extracting
            # files. Skipping it here let a markdown-split credential reach a
            # guild thread that only the literal-form redactor had seen.
            text = _redact_transformed(source)
        if not text.strip() and not files:
            if components is None:
                return
            text = "…"

        chunks = [text]
        if len(text) > DISCORD_MAX_TEXT:
            chunks = await asyncio.to_thread(split_markdown_safe, text, DISCORD_MAX_TEXT)
        chunks = [part for chunk in chunks for part in _fit_platform_cap(chunk)]
        for index, chunk in enumerate(chunks):
            part_files = files if index == 0 else []
            final = index == len(chunks) - 1
            if not await self._land_sealed(chunk, part_files, components if final else None):
                if part_files:
                    break
                continue
            if not final:
                self._open_new_message()
        else:
            return

        # Multipart is all-or-nothing. Restore the source markup, but redact its
        # DISPLAY form before any fallback split/send so formatting cannot hide
        # a credential that Discord reconstructs for the reader.
        logger.warning(
            "discord: upload of %d file(s) failed; re-posting the segment with its markup",
            len(files),
        )
        try:
            source = _redact_transformed(source)
            recovery = [source]
            if len(source) > DISCORD_MAX_TEXT:
                recovery = await asyncio.to_thread(split_markdown_safe, source, DISCORD_MAX_TEXT)
            recovery = [part for chunk in recovery for part in _fit_platform_cap(chunk)]
            landed_any = False
            for index, chunk in enumerate(recovery):
                if await self._client.send_message(
                    self._channel_id,
                    chunk,
                    components=components if index == len(recovery) - 1 else None,
                ):
                    landed_any = True
            if landed_any:
                # This recovery IS a delivery, so it has to answer to
                # `delivery_failed`. Only the LANDED count moves: the seal that
                # sent us here already counted its attempt, and counting a second
                # one would make a recovered turn look like two failures. Without
                # this the reply reaches the user while the turn reports
                # undelivered, and the cron leg then re-alerts over Slack and
                # refuses to advance its dedup hash -- a duplicate for a message
                # that arrived.
                self._seals_landed += 1
        except Exception:
            logger.warning("discord: markup fallback after a failed upload failed", exc_info=True)

    async def on_thinking(self, text: str) -> None:
        # The ladder moves on reasoning regardless: the reaction reports what the
        # agent is doing, which is independent of whether the words are shown.
        self._set_phase(PHASE_THINKING)
        if not self._show_thinking:
            # Reasoning stays private unless the operator opts in (parity with
            # Telegram), so it is not even accumulated.
            return None
        self._thinking += text or ""
        return None

    async def _flush_thinking(self) -> None:
        """Post the accumulated reasoning once, as its own subtext message.

        Its own message rather than the answer bubble: the answer is edited in
        place for the whole turn, so reasoning parked there would be overwritten
        by the next frame. ``show_thinking`` is not re-checked here: ``on_thinking``
        is the single gate, and it accumulates nothing while the toggle is off.
        """
        if self._thinking_posted:
            return
        reasoning, self._thinking = self._thinking.strip(), ""
        if not reasoning:
            return
        self._thinking_posted = True
        # Redact BEFORE the preview cut: trimming first can leave a fragment the
        # credential matchers no longer recognise.
        body = _redact_transformed(reasoning)
        if len(body) > _THINKING_PREVIEW_CHARS:
            body = body[:_THINKING_PREVIEW_CHARS].rstrip() + "…"
        try:
            await self._client.send_message(self._channel_id, _as_subtext(f"💭 {body}"))
        except Exception:
            logger.debug("discord: thinking note send failed", exc_info=True)

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        # Surface mid-turn tool activity as a transient "🔧 {tool}…" footer on
        # the live bubble (force=True so it shows immediately). We deliberately
        # do NOT seal a message here — see the Telegram renderer's rationale.
        self._last_tool = title or tool_kind or "tool"
        self._tool = self._last_tool
        self._set_phase(phase_for_tool_title(self._last_tool, tool_kind))
        await self._stream_live(force=True)

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        # Approve/Deny as a SEPARATE message so ongoing streaming edits to the
        # answer bubble don't clobber the buttons. custom_id carries a
        # per-prompt nonce (a:<request_id>:<nonce>:<1|0>, well under Discord's
        # 100-char cap) so a stale button from a reused request ID can never
        # resolve a later prompt; the interaction handler validates it via
        # ``resolve_global``.
        rid = str(request_id)
        nonce = DiscordApprovalDecider.register_nonce(
            DiscordApprovalDecider.key(self._session_key, rid)
        )
        components = [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": _STYLE_SUCCESS,
                        "label": "✅ Approve",
                        "custom_id": f"a:{rid}:{nonce}:1",
                    },
                    {
                        "type": 2,
                        "style": _STYLE_DANGER,
                        "label": "🚫 Deny",
                        "custom_id": f"a:{rid}:{nonce}:0",
                    },
                ],
            }
        ]
        # The tool name is LLM-authored and Discord renders the message as
        # markdown, so it goes through the same display-form scan as streamed text.
        # The driver's byte-level pass sees `AKIA**…**` as broken while the rendered
        # prompt shows it whole -- the reason _redact_transformed exists.
        # The request's OWN title first: `_last_tool` is the last tool_call seen
        # and is never cleared, so it names the previous tool for any permission
        # that arrives without one of its own. Either source is LLM-authored, so
        # the display-form scan above applies to both.
        tool = await asyncio.to_thread(
            _redact_transformed, tool_title or self._last_tool or "this tool"
        )
        # A turn parked on a human is not a stalled turn: hold the watchdog until
        # the next real activity (``_note_progress``) resumes it, so waiting for
        # an approval never earns the "gone quiet" mark.
        if self._ladder is not None and not self._ladder_paused:
            self._ladder_paused = True
            self._ladder.pause_stall_watchdog()
        await self._client.send_message(
            self._channel_id, f"🔐 Approve `{tool}`?", components=components
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        try:
            await self._client.send_message(self._channel_id, "🗜️ Compacting context…")
        except Exception:
            logger.debug("Discord: compaction notice send failed", exc_info=True)

    def note_steer(self, text: str) -> None:
        """Record the user's own mid-turn steer text (their typed words, NOT
        the redacted backend echo); rendered as an inline "↪️ steered" chip.
        Capped to avoid unbounded growth on a pathological steer burst."""
        t = (text or "").strip()
        if t and len(self._steer_texts) < 50:
            self._steer_texts.append(t)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        self._stop_typing()
        ok = stop_reason != "error"
        if self._ladder is not None:
            self._ladder.finalize(error=not ok)
        # Reasoning that arrived without any answer text still belongs to the
        # user (no-op when show_thinking is off or it already landed).
        await self._flush_thinking()
        # Flush any trailing rotation, then finalize the current segment with
        # the [OPTIONS:] button rows attached to the last chunk.
        await self._rotate_at_markers()
        self._materialize_chip()
        # Consume protocol before table cards can join cells into marker-shaped
        # display text. Presentation output is never reinterpreted as control.
        opts = self._take_canonical_options()
        # The stream is over, so a trailing table run is complete.
        await self._convert_tables(final=True)
        body_text, opts = apply_options_cap(self._segment_text(), opts, self.capabilities)
        self._buf = []
        self._delivery_text = body_text
        components = (
            build_option_components(opts, session_provenance_tag(self._session_key))
            if opts
            else None
        )
        # No-rotation fallback: steers were injected but no marker rotated —
        # prepend one summary chip so they're still shown.
        if self._seal_count == 0 and self._steer_texts:
            quoted = [q for q in (_neutralize_md(t) for t in self._steer_texts) if q]
            if quoted:
                body = self._segment_text().strip()
                summary = "> " + " · ".join(quoted)
                self._delivery_text = summary + ("\n\n" + body if body else "")
        await self._rotate_on_length()
        if not self._segment_text().strip():
            # Nothing to post. Earlier rotated segments carried the turn ->
            # stay silent; otherwise show a placeholder. An extracted button
            # row (options-only body) must ALWAYS reach the user.
            if self._seal_count > 0 and components is None:
                return
            placeholder = "…" if ok else "⚠️ Error — please try again"
            placeholder = self._with_turn_footer(placeholder)
            # Counted, because when no earlier segment sealed, this placeholder
            # (or an options-only button row, which IS the payload) is the turn's
            # ENTIRE delivery. Leaving it unaccounted let a turn whose only output
            # failed to send report as delivered, which is the exact
            # succeeded-with-no-reply case `delivery_failed` exists to catch.
            self._seals_attempted += 1
            if self._stream_mid is not None:
                if await self._client.edit_message(
                    self._channel_id,
                    self._stream_mid,
                    placeholder,
                    components=components,
                ):
                    self._seals_landed += 1
            elif await self._client.send_message(
                self._channel_id, placeholder, components=components
            ):
                self._seals_landed += 1
            return
        # The footer rides on the final segment rather than as its own message:
        # one turn, one bubble, and Discord charges rate budget per message.
        # Appended AFTER the length rotation so it always lands on the LAST
        # chunk, and after the empty-body check so a silent turn stays silent.
        # Written to the DELIVERY snapshot, not to `_buf`: the snapshot is what
        # `_segment_text` answers with once set, and `on_done` sets it above -- so a
        # footer appended to the buffer is simply not what ships. Presentation-only
        # by construction, which is what this snapshot is for; the canonical text
        # history and the transcript read is untouched.
        self._delivery_text = self._with_turn_footer(self._segment_text())
        await self._seal_current(components=components)

    def _context_pct(self) -> float | None:
        """This session's context-window usage, or ``None`` when unknown.

        Unknown is not 0: an unbound or failing provider must not render as a
        reassuring "plenty of room" chip.
        """
        provider = self._context_source
        if provider is None:
            return None
        try:
            return float(provider.context_usage_pct())
        except Exception:
            logger.debug("discord: context usage unavailable", exc_info=True)
            return None

    def _with_turn_footer(self, text: str) -> str:
        """Append the one-line turn footer as Discord subtext.

        Dropped rather than truncated when the segment leaves no room: a clipped
        answer costs the user more than a missing timing line. ``_limit()``
        already holds back headroom below the platform cap for exactly this.
        """
        footer = f"-# {format_turn_status(max(0.0, self._now() - self._t0), self._context_pct())}"
        if len(text) + len(footer) + 2 > DISCORD_MAX_TEXT:
            return text
        return f"{text}\n\n{footer}"

    def bind_context_source(self, provider: "LLMProvider | None") -> None:
        """Authorize the session provider the turn footer reads usage from.

        Bound by the dispatcher once the session exists; read at turn END, so
        the chip reports the window as the user leaves it, not as it started.
        """
        self._context_source = provider

    def _limit(self) -> int:
        # Leave headroom below the 2000-char cap for the chip/footer overhead
        # so a chunk can never overflow and get cut mid-word by the API.
        cap = self.capabilities.max_message_chars or 1900
        return max(500, cap - 100)

    def _chip_for_seal(self, i: int) -> str | None:
        """The steer chip (a "> quote" blockquote of the USER's own words) that
        heads the segment opened by the i-th rotation."""
        if 0 <= i < len(self._steer_texts):
            t = _neutralize_md(self._steer_texts[i])
            return f"> ↪️ {t}" if t else None
        return None

    async def close(self) -> None:
        """Idempotent teardown: stop the typing indicator, finalize the turn if
        it never reached on_done, and drain the status ladder.

        The ladder is closed LAST and awaited: its debounce and stall timers are
        loop callbacks and its emoji edits are tasks, so a turn torn down by an
        exception would otherwise leave both running against a finished turn.
        """
        self._stop_typing()
        if not self._finalized:
            await self.on_done(stop_reason="error")
        ladder, self._ladder = self._ladder, None
        if ladder is not None:
            await ladder.close()
