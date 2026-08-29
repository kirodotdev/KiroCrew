"""Turn rendering for the WhatsApp channel.

The reply STREAMS: the first bubble is sent as soon as there is something worth
reading, then edited in place as more text arrives, and a new bubble is opened
when the text outgrows one message. The Web protocol does expose an edit (the
Business Cloud API does not), which is why this channel can do what iMessage and
Weixin cannot.

Two things bound it, and both are about the operator's own phone number rather
than about looking tidy:

- **A throttle, not a per-token edit.** Every edit is a full end-to-end encrypted
  send to every device of every participant. neonize's own example edits once per
  character; doing that from a personal account is exactly the traffic pattern
  that gets a number rate-limited or banned. Edits are coalesced to at most one
  per :data:`_EDIT_INTERVAL_S`, single-flight, keeping only the newest pending
  text: a burst of chunks costs one edit, not one each.
- **The edit window.** WhatsApp accepts an edit for ``client.EDIT_WINDOW_S``
  after the original send and then refuses. A turn that runs longer stops editing
  and seals, so a long agent run degrades to sequential bubbles instead of
  silently dropping its own progress.

While the turn runs it also holds the "composing" presence.

Channel-specific twists:

- Delivery goes through ``WhatsAppTransport.send_message`` rather than the raw
  client, so every sent chunk's ID lands in the echo tracker atomically.
- An unprompted rules-mode group turn whose entire answer is the silence
  sentinel (``group_gate.SILENCE_SENTINEL``) is suppressed: nothing is
  delivered, and ``suppressed`` tells the dispatcher not to start the group
  cooldown.

Dependency direction is ``whatsapp -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.constants import (
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
    strip_action_markers,
)
from kiro_crew.messaging.approval import (
    TIMEOUT_NOTICE,
    abandon_approval,
    build_approval_prompt,
    open_approval,
)
from kiro_crew.messaging.outbound_files import Rejection, hide_local_refs
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.whatsapp import client as wa_client
from kiro_crew.whatsapp.files import (
    REASON_UPLOAD_FAILED,
    plan_uploads_off_loop,
    rejection_note,
)
from kiro_crew.whatsapp.group_gate import SILENCE_SENTINEL
from kiro_crew.whatsapp.renderer import display_safe_text, render_chunks_off_loop

if TYPE_CHECKING:
    from kiro_crew.whatsapp.client import WhatsAppClient
    from kiro_crew.whatsapp.transport import WhatsAppTransport

logger = logging.getLogger(__name__)

#: Refresh cadence for the composing indicator. Neither whatsmeow nor neonize
#: documents or enforces a presence TTL, and no authoritative WhatsApp figure was
#: sourced, so this is a chosen cadence rather than a measured expiry: it is
#: frequent enough that the indicator does not visibly lapse, and the state is
#: always cleared explicitly when the turn ends.
_TYPING_REFRESH_S = 8.0

#: Minimum seconds between edits of the live bubble. Chosen for send-rate safety
#: on a personal account, not for smoothness: each edit is a full E2E send fanned
#: out to every device of every participant.
_EDIT_INTERVAL_S = 2.5

#: Don't open a bubble for a couple of characters. A turn that answers in one
#: short sentence should arrive as one message, not as a stub that is then edited.
_MIN_FIRST_FLUSH_CHARS = 24

_ERROR_TEXT = "Something went wrong on my side. Please try again."


def _strip_options(text: str) -> str:
    """Drop the dashboard-only marker trailers (no buttons on this transport).

    BOTH heads, because "inert on a channel that cannot render it" has to mean
    stripped, not passed through: WhatsApp emits the buffered turn as plain text, so
    an unstripped ``[OPTION-ACTIONS: …]`` is posted to the user as raw protocol.

    Actions go first, and via the LINE form, matching the sibling site in
    ``messaging.renderer``. Both choices are load-bearing. ``OPTIONS_RE_TRAILER`` is
    ``\\Z``-anchored, so while a trailing action marker is still present the content
    marker cannot reach that anchor and would survive; removing actions first clears
    the way. The LINE form is the sibling's choice and keeps all three strip sites on
    one rule — it requires the marker to END its line, so a marker sitting
    mid-sentence is left alone as prose. That is deliberate (see
    ``_MARKER_TAIL_LINE``): it is what stops a sentence discussing the syntax from
    being mangled, and it is shared with the sibling rather than particular to here.

    Actions go through ``strip_action_markers`` rather than the raw pattern, so a
    span nested in an UNCLOSED marker stays visible: it is not a marker, and the
    broken syntax is the reader's only cue one was intended.
    """
    return OPTIONS_RE_TRAILER.sub("", strip_action_markers(text)).strip()


class WhatsAppRenderer(Renderer):
    """Buffers a turn; emits once on completion (see module docstring)."""

    channel_type = "whatsapp"

    def __init__(
        self,
        transport: "WhatsAppTransport",
        client: "WhatsAppClient",
        chat_jid: str,
        capabilities: TransportCapabilities,
        *,
        unprompted: bool = False,
        session_key: str = "",
        approval_session_key: str | None = None,
        upload_root: "Callable[[], str] | None" = None,
    ) -> None:
        super().__init__(capabilities)
        self._transport = transport
        self._client = client
        self._chat = chat_jid
        self._unprompted = unprompted
        self._session_key = session_key
        #: Set only when the dispatcher built a decider for this turn, i.e. the
        #: sender may approve. ``None`` keeps ``on_prompt_choice`` silent rather
        #: than posting a prompt nobody present is allowed to answer.
        self._approval_session_key = approval_session_key
        #: Resolves the only tree an outbound file reference may name: the
        #: provider's own cwd. A CALLABLE because the shared pipeline owns the
        #: provider and acquires it inside the turn, so the root is not knowable
        #: when this renderer is constructed. Absent or non-absolute disables
        #: uploads: reply text is untrusted (a prompt-injected agent chooses what
        #: it writes), and an unbounded root would let it name any host file.
        self._upload_root = upload_root
        self._last_tool = ""
        self._last_purpose = ""
        self._buf: list[str] = []
        self._started = False
        self._finalized = False
        self._typing_task: asyncio.Task[None] | None = None
        #: True when a rules-mode turn chose silence; the dispatcher reads it
        #: to skip cooldown recording and history persistence of the sentinel.
        self.suppressed = False
        #: True once a reply actually reached the chat. The group cooldown is
        #: gated on THIS rather than on ``not suppressed``, because a muted
        #: conversation never runs this renderer at all: ``drive_turn``
        #: substitutes ``SilentRenderer`` into its LOCAL name, so ``on_done``
        #: and ``close`` are called on that object and ``suppressed`` stays
        #: False here. Reading it would then start a cooldown for a reply
        #: nobody received, silencing the next unprompted turn that had
        #: something to say. A failed send lands in the same place, correctly.
        self.delivered = False
        #: True when the turn ended on an error, which is NOT the complement of
        #: ``delivered``: an errored turn delivers the apology notice, so that
        #: flag is True by the time the dispatcher reads it. The phase reaction
        #: needs the OUTCOME, and only this renderer sees the stop reason, so it
        #: is recorded here rather than re-derived from what reached the chat.
        self.failed = False
        #: Index of the earliest chunk a STREAMING send failed to place, or None.
        #: The count still advances past a failure so the flush loop terminates: a
        #: chunk that cannot be sent must not be retried on every later flush,
        #: which would spin for the rest of the turn. That makes "counted" stop
        #: implying "delivered", so the final pass reopens from HERE rather than
        #: from the count, and a transient failure costs a repeated bubble instead
        #: of a silently missing one.
        self._undelivered_from: int | None = None
        #: The live bubble being edited: its platform id and when it was sent
        #: (monotonic), because the edit window is measured from the send.
        self._live_id = ""
        self._live_sent_at = 0.0
        #: What the live bubble currently shows, so an edit is skipped when the
        #: rendered text has not actually changed.
        self._live_text = ""
        #: How many rendered chunks are already delivered and final. A COUNT,
        #: deliberately not a character offset: the buffer holds raw markdown while
        #: the delivered text is dialect-converted, and conversion changes length
        #: (``**x**`` becomes ``*x*``), so slicing raw text by a converted length
        #: drifts and re-sends what the reader already has. The shared splitter is
        #: prefix-stable on the converted text, so chunk *i* never changes once
        #: chunk *i+1* exists, which is what makes a count sufficient.
        self._sealed_count = 0
        self._flush_task: asyncio.Task[None] | None = None
        self._last_edit_at = 0.0
        #: The approval prompt's own message id, kept so a timeout can resolve the
        #: bubble the operator is still looking at instead of leaving it answerable.
        self._prompt_id = ""

    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        self._resume_typing()

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        # The agent is producing again, so the indicator is true again. Restarted
        # lazily HERE rather than by the approval path because nothing tells this
        # renderer when a decision landed: the driver dispatches PROMPT_CHOICE and
        # only then awaits the decider, so the next event is the first news of it.
        self._resume_typing()
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        """Arm one coalescing flush. Single-flight by construction.

        A live task is left alone rather than replaced: it will pick up whatever
        the buffer holds when its throttle elapses, so a burst of chunks costs one
        edit instead of one each. Replacing it would either cancel work already in
        flight or stack a task per chunk, which is the per-token edit storm the
        module docstring exists to prevent.
        """
        if self._finalized or self._unprompted:
            # An unprompted group turn may end up choosing silence, and a
            # streamed prefix cannot be unsent. It stays buffered to the end.
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._flush_after_throttle())

    async def _flush_after_throttle(self) -> None:
        wait = self._last_edit_at + _EDIT_INTERVAL_S - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await self._render_live()
        except Exception:  # noqa: BLE001: progress rendering must not fail a turn
            logger.debug("whatsapp: streaming flush failed", exc_info=True)

    async def _render_live(self) -> None:
        """Show the newest text, opening or editing exactly one bubble.

        Splitting decides where a bubble ENDS. The shared splitter is
        prefix-stable, so every chunk but the last is final: those are sealed as
        their own messages and only the tail stays editable.
        """
        if self._finalized:
            return
        chunks = await self._rendered_chunks()
        if not chunks or self._sealed_count >= len(chunks):
            return
        # Every chunk before the last is final: the splitter is prefix-stable, so
        # later text can never revise one.
        while self._sealed_count < len(chunks) - 1:
            await self._seal_chunk(chunks[self._sealed_count])
        tail = chunks[-1]
        if not self._live_id and len(tail) < _MIN_FIRST_FLUSH_CHARS and not self._sealed_count:
            return  # too early to be worth a bubble
        # The tail is NOT final -- the splitter can still revise it -- so this is
        # the one placement that does not advance the count. ``_show`` owns every
        # question about which bubble it lands in.
        await self._show(tail)

    async def _rendered_chunks(self) -> list[str]:
        """The turn's text as the chunks that would actually be delivered.

        The WHOLE body is converted and split, never just the unsealed remainder:
        the remainder is only addressable as a raw-text offset, and the delivered
        text is dialect-converted, so mixing the two drifts.

        ``hide_local_refs`` rather than extraction: a live frame must not show
        ``![chart](/tmp/chart.png)`` markup the final send replaces with the
        picture, and it cannot afford extraction's filesystem work per frame. It
        covers a half-arrived reference too, so a path cannot surface for one frame
        while its closing paren is still streaming.

        **The protocol suffix is detached, and that is what keeps this
        MONOTONIC.** ``_strip_options`` only removes a COMPLETE trailer, so a
        still-arriving ``[OPTIONS: yes | n`` renders raw into the live bubble --
        and then vanishes, taking its length with it. That is not only a flicker:
        4,089 visible characters plus that fragment is two chunks, while the
        completed ``[OPTIONS: yes | no]`` strips back to one, so a flush landing in
        that window leaves ``_sealed_count`` permanently above ``len(chunks)``.
        Every later flush then returns at ``_render_live``'s guard and ``on_done``
        computes an empty pending slice: the fragment stays on screen and the rest
        of the reply is dropped. Discord and Telegram detach the same suffix at the
        same point, for the first half of that reason.
        """
        visible, _protocol_suffix = split_trailing_protocol_suffix(self.text())
        body = await asyncio.to_thread(hide_local_refs, visible)
        if not body:
            return []
        return await render_chunks_off_loop(body, self.capabilities.max_message_chars)

    def _edit_window_closed(self) -> bool:
        if not self._live_sent_at:
            return False
        elapsed = asyncio.get_running_loop().time() - self._live_sent_at
        return elapsed >= wa_client.EDIT_WINDOW_S

    async def _open_live(self, text: str) -> None:
        message_id = await self._transport.send_message(self._chat, text)
        now = asyncio.get_running_loop().time()
        self._live_id = message_id
        self._live_sent_at = now
        self._last_edit_at = now
        self._live_text = text
        self.delivered = True

    async def _edit_live(self, text: str) -> None:
        if text == self._live_text:
            return
        ok = await self._client.edit_text(self._chat, self._live_id, text)
        self._last_edit_at = asyncio.get_running_loop().time()
        if ok:
            self._live_text = text
            return
        # A refused edit must not be retried forever against the same bubble, so
        # the bubble is closed and an empty ``_live_id`` reports to ``_show`` that
        # the text never landed. Placing it elsewhere is NOT decided here: this
        # method answers one question, and every caller that has to know whether
        # the text is on screen goes through ``_show``.
        await self._close_live()

    async def _show(self, text: str) -> bool:
        """Leave *text* on screen, and report whether it actually got there.

        THE answer to "is this visible", and the only thing a caller may advance
        ``_sealed_count`` on. Three call sites reached the same wrong conclusion
        independently -- a closed edit window, a refused streaming edit, and a
        refused final edit each counted a chunk that never arrived -- because each
        decided delivery from its own local branch. There is exactly one branchy
        fact here (an edit can be refused, and a refusal closes the bubble), so it
        is answered once, here.

        Three placements, in order of preference: edit the live bubble; past the
        edit window, or after a refusal, open a fresh one. A fresh bubble repeats
        whatever prefix the old one still shows, which is the right trade -- a
        duplicated sentence is visible and recoverable, a silently missing one is
        neither.

        Returns True when *text* is on screen, and for blank text, where there is
        nothing to place and nothing to lose. A send that FAILS raises rather than
        returning False: the callers disagree about what a failure means (a
        streaming flush must not retry forever, a final send must fail the turn),
        so that decision stays with them.
        """
        if not text.strip():
            return True
        if self._live_id and not self._edit_window_closed():
            await self._edit_live(text)
            if self._live_id:
                return True
        elif self._live_id:
            # Past the window every edit is refused, so stop asking. The fresh
            # bubble's own window starts at its send (``_open_live`` restamps it).
            await self._close_live()
        await self._open_live(text)
        return True

    async def _seal_chunk(self, text: str) -> None:
        """Deliver one chunk as its own message and count it final.

        The count advances unconditionally, INCLUDING when the send raised: a
        chunk that could not be delivered must not be retried on every later
        flush, which would loop for the rest of the turn. What it must never
        advance over is a chunk nobody finished trying to place, which is why the
        placement is ``_show``'s and not this method's.
        """
        index = self._sealed_count
        try:
            await self._show(text)
        except Exception:
            # Counted but NOT delivered. Remembered so the final pass can reopen
            # from here; re-raised so the flush wrapper still logs it.
            if self._undelivered_from is None:
                self._undelivered_from = index
            raise
        finally:
            await self._close_live()
            self._sealed_count += 1

    async def _close_live(self) -> None:
        """Stop editing the current bubble, leaving its text as delivered."""
        self._live_id = ""
        self._live_sent_at = 0.0
        self._live_text = ""

    async def on_thinking(self, text: str) -> None:
        return None  # one bubble per turn; reasoning would double the noise

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        """Remember the tool so an approval prompt can name it.

        Nothing is SENT here: one bubble per turn is the channel's shape, and a
        per-tool message on a phone is noise. These values are the FALLBACK only:
        ``PROMPT_CHOICE`` carries its own ``tool_title`` and ``tool_purpose``, and
        ``on_prompt_choice`` prefers those, because a remembered name belongs to
        whichever tool call came last and a permission request not immediately
        preceded by its own titled call would otherwise name a different tool.
        """
        self._last_tool = title or self._last_tool
        self._last_purpose = tool_purpose or ""
        # A tool call is the agent working, so the indicator is honest again after
        # an approval paused it. See ``on_text_chunk``.
        self._resume_typing()

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        """Ask for approval as a numbered message the operator answers by typing.

        Only reached when the dispatcher supplied a decider, which it does only
        for a sender allowed to approve -- so this never renders a prompt whose
        answer would be refused. The entry is opened HERE because the driver
        dispatches this event before it awaits the decider; ``open_approval`` is
        idempotent so the decider then waits on this same request.

        The event's own ``tool_title`` wins over the remembered one, and the title
        and purpose are taken as a PAIR: a permission is not always preceded by its
        own titled tool call, so a remembered name can be the previous tool's, and
        pairing a supplied title with a remembered purpose would describe two
        different tools in one prompt.

        The prompt's own message id is KEPT, because the ball is now in the
        operator's court and this channel declares ``edit=True``: on a timeout the
        bubble they are looking at is edited into its resolved form rather than
        left looking answerable. The wait is the decider's, so the notice is
        delivered through the entry's ``on_timeout`` hook.
        """
        if self._approval_session_key is None:
            logger.debug("whatsapp: prompt_choice with no approval route; ignoring")
            return
        title = tool_title or self._last_tool
        purpose = tool_purpose if tool_title else self._last_purpose
        open_approval(
            self._approval_session_key,
            request_id,
            tool_title=title,
            conversation_id=self._chat,
            on_timeout=self._announce_approval_timeout,
        )
        try:
            # Screened, not sent through ``_send``: the tool title and purpose are
            # model-authored and reach the chat with no scan of their own, but this
            # is a prompt rather than the reply, so it must not set ``delivered``
            # and start a group cooldown for an answer nobody has given yet.
            self._prompt_id = await self._transport.send_message(
                self._chat,
                display_safe_text(build_approval_prompt(title, purpose)),
            )
        except Exception:
            # The entry is opened before the send because the send is what can
            # fail. Leaving it behind would let the NEXT message the operator
            # types answer a prompt they never saw, for a turn that has already
            # unwound. Drop it, then let the failure fail the turn as before.
            abandon_approval(self._approval_session_key, request_id)
            raise
        # The agent is now waiting on the OPERATOR, so "composing" asserts the
        # opposite of the truth -- and for the whole 300-second window, since the
        # refresh loop is otherwise only stopped by ``on_done``. Nothing reports a
        # decision back to a renderer, so it restarts on the next event instead.
        await self._pause_typing()

    async def _announce_approval_timeout(self) -> None:
        """Resolve the prompt in place when the window closed with no answer.

        Editing beats posting: the stale prompt is what the operator would answer,
        and a "1" typed after the window finds no open entry. A refused or
        window-closed edit falls back to a fresh message, because the notice
        matters more than where it appears.
        """
        notice = display_safe_text(TIMEOUT_NOTICE)
        if self._prompt_id and await self._client.edit_text(self._chat, self._prompt_id, notice):
            self._prompt_id = ""
            return
        self._prompt_id = ""
        try:
            await self._transport.send_message(self._chat, notice)
        except Exception:  # noqa: BLE001: the tool is already denied
            logger.warning("whatsapp: could not deliver the approval timeout", exc_info=True)

    async def on_compaction(self, context_usage_pct: float) -> None:
        logger.debug("whatsapp: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        # Set BEFORE the final render so a flush that is still parked on its
        # throttle returns without touching a bubble this method is finishing.
        self._finalized = True
        await self._cancel_flush()
        await self._stop_typing()
        ok = stop_reason != "error"
        self.failed = not ok
        body = self.text()
        if self._unprompted and (not body or body == SILENCE_SENTINEL):
            # The model chose silence (or produced nothing): deliver nothing.
            # Nothing was streamed either: _schedule_flush never arms for an
            # unprompted turn, precisely because a streamed prefix cannot be
            # unsent once the model turns out to have chosen silence.
            self.suppressed = True
            logger.debug("whatsapp: unprompted group turn suppressed")
            return
        if not body:
            body = "..." if ok else _ERROR_TEXT

        # Deliver only what streaming has not already shown, counted in the same
        # chunks it delivered. Re-sending the whole body would repeat every word
        # the reader watched arrive, and measuring the raw body against converted
        # output is the drift a count exists to avoid.
        # The fallback is screened rather than raw: a body that yields no chunks
        # never passed through ``to_whatsapp_text``, so it never met the display
        # screen either, and this branch would put the one form of the reply that
        # was scanned only as literal bytes into the chat.
        chunks = await self._rendered_chunks() or [display_safe_text(body)]
        # Not the count: a streaming send that raised advanced it without landing,
        # and this is the last pass, so starting at the count would drop that
        # chunk for good. Re-sending from the earliest unlanded one can repeat a
        # later chunk that did land, which is the trade this module takes
        # everywhere: a duplicated sentence is visible and recoverable.
        start = self._sealed_count
        if self._undelivered_from is not None:
            start = min(start, self._undelivered_from)
        pending = chunks[start:]
        for index, chunk in enumerate(pending):
            is_last = index == len(pending) - 1
            # This is the last pass over the text, so there is no later flush to
            # recover a chunk that did not land -- which is exactly what ``_show``
            # is responsible for, and why the count advances only after it.
            await self._show(chunk)
            if not is_last:
                await self._close_live()
            self._sealed_count += 1
        if not self.delivered and body.strip():
            # Nothing reached the chat at all: the whole reply still has to.
            await self._send(body)
        await self._deliver_uploads()

    async def _deliver_uploads(self) -> None:
        """Send the pictures the reply referenced, then name any it could not.

        Runs AFTER the text, so the caption-bearing image lands next to the words
        that introduced it. Extraction is deliberately not done per streamed
        frame: it reads and decodes files, and the streaming path only ever hides
        the markup (see ``_render_live``).

        A silently dropped picture is worse than a sentence saying why, so a
        refusal is reported rather than swallowed.
        """
        if not self.capabilities.files_outbound or self._upload_root is None:
            return
        try:
            root = self._upload_root()
        except Exception:  # noqa: BLE001: no root means no uploads, never a failure
            logger.debug("whatsapp: could not resolve the upload root", exc_info=True)
            return
        if not root or not os.path.isabs(root):
            # Mirrors the sibling channel: a relative root would be resolved
            # against the gateway's cwd, which is not the agent's workspace.
            return
        plan = await plan_uploads_off_loop(self.text(), within_root=root)
        rejections = list(plan.rejections)
        for outbound in plan.files:
            try:
                # The caption is the reference's markdown alt text, so it is
                # model-authored and lands in the chat where WhatsApp collapses
                # markup exactly as it does in a body. Discord screens the same
                # field for the same reason.
                await self._transport.send_image(
                    self._chat, outbound.data, display_safe_text(outbound.alt or "")
                )
            except Exception:  # noqa: BLE001: the words already landed
                # A failed upload becomes a REJECTION, not just a log line.
                # Extraction has already removed the markdown reference from the
                # delivered text, so staying quiet here leaves the user with
                # neither the picture nor the path nor any hint one existed: the
                # agent silently answered a question about an image it never sent.
                logger.warning("whatsapp: image upload failed", exc_info=True)
                rejections.append(Rejection(outbound.path, REASON_UPLOAD_FAILED, ""))
        note = rejection_note(rejections)
        if note:
            await self._send(note)

    async def _cancel_flush(self) -> None:
        task = self._flush_task
        self._flush_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def close(self) -> None:
        """Idempotent teardown from the dispatcher's ``finally``; a delivery
        failure here is logged, not raised (the turn is already unwinding)."""
        if not self._finalized:
            try:
                await self.on_done(stop_reason="error")
            except Exception:
                logger.warning("whatsapp: final send failed during teardown", exc_info=True)
        await self._stop_typing()

    def text(self) -> str:
        """The turn's visible answer (OPTIONS stripped); also persisted."""
        return _strip_options("".join(self._buf).strip())

    async def _send(self, body: str) -> None:
        """Deliver via the transport (echo-tracked). Raises on failure so the
        dispatcher records a failed turn instead of persisting an undelivered
        reply as success; ``close()`` is the suppressing teardown path.

        Screened here because this is the sink for text that did NOT come from
        ``render_chunks``: the whole-reply fallback and a file-rejection note. A
        screen on the chunk path alone leaves those two as the bypass, and a note
        must keep its dialect, which is why it is the screen without the
        conversion.
        """
        try:
            await self._transport.send_message(self._chat, display_safe_text(body))
        except Exception:
            logger.warning("whatsapp: send failed, failing the turn", exc_info=True)
            raise
        self.delivered = True

    async def _hold_typing(self) -> None:
        try:
            while True:
                await self._client.send_typing(self._chat, True)
                await asyncio.sleep(_TYPING_REFRESH_S)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("whatsapp: typing loop ended", exc_info=True)

    def _resume_typing(self) -> None:
        """Arm the refresh loop if it is not already running. Idempotent.

        Called from every event that means the agent is working again, because an
        approval pause has no completion signal of its own to restart on.
        """
        if self._finalized:
            return
        if self._typing_task is not None and not self._typing_task.done():
            return
        self._typing_task = asyncio.create_task(self._hold_typing())

    async def _pause_typing(self) -> None:
        """Drop the indicator, keeping the turn alive. The waiting-on-you state.

        Distinct from :meth:`_stop_typing` only in intent, which is why they share
        the teardown: this one is expected to be followed by ``_resume_typing``.
        """
        await self._stop_typing()

    async def _stop_typing(self) -> None:
        task = self._typing_task
        self._typing_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._client.send_typing(self._chat, False)
