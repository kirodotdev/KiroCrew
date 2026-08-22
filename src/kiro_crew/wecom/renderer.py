"""Layer 2b -- WeCom ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream (routed by the base
:class:`Renderer`'s ``dispatch``) onto WeCom AI-bot ``aibot_respond_msg`` WS
frames:

* ``on_turn_start`` -- a "🤔 …" placeholder stream frame.
* ``on_text_chunk`` -- throttled full-text stream frames (each frame REPLACES
  the bubble content), with any trailing ``[OPTIONS:]`` markup stripped (WeCom
  can't render tappable chips).
* ``on_tool_call`` -- a transient ``🔧 正在运行：{tool}…`` footer.
* ``on_prompt_choice`` -- no-op: WeCom has no interactive buttons, and the
  driver only dispatches this for INTERACTIVE + a decider; WeCom runs
  decider-less (deny-by-default), so this never fires.
* ``on_compaction`` -- logged only (a mid-turn out-of-band frame would pollute
  the single answer bubble; the dispatcher surfaces threshold notices).
* ``on_done`` -- the final ``finish=true`` frame (locks the bubble), falling
  back to a one-shot ``response_url`` POST if the WS stream is
  unavailable/expired.

Dependency direction is ``wecom -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.renderer import Renderer, format_overflow
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.wecom.client import WECOM_SAFE_REPLY_CHARS, new_stream_id

if TYPE_CHECKING:
    from kiro_crew.wecom.client import WeComClient

logger = logging.getLogger(__name__)

# WeCom meters a conversation at 30 messages/minute (and 1000/hour), shared
# between replies and proactive pushes, and every stream refresh spends that
# budget. 0.7s (~85/min) was ~3x over it on every streaming turn.
WECOM_QUOTA_PER_MIN = 30

# Frames that deliberately IGNORE the throttle, and therefore have to be reserved
# out of the budget rather than assumed free: the turn-start placeholder, the first
# tool footer of each bubble, the final sealing frame, and headroom for an overflow
# push. Pacing to the full quota and then adding these lands at ~33/minute, where
# the platform starts refusing — and a refusal on the FINAL frame leaves a partial
# answer delivered.
_UNTHROTTLED_FRAME_BUDGET = 6

# Min seconds between intermediate stream frames, DERIVED from what is left. Kept
# as arithmetic rather than a tuned literal so the two numbers above stay the
# things a reader has to check, and so a quota change cannot leave a stale pace
# behind. Currently 2.5s, which still reads as live typing.
_STREAM_THROTTLE_S = 60.0 / (WECOM_QUOTA_PER_MIN - _UNTHROTTLED_FRAME_BUDGET)

# WeCom ends a stream bubble ~10 minutes after its first frame and refuses later
# writes with errcode 846608. An agentic turn doing tool work routinely runs
# longer, so the bubble is rotated BEFORE the wall rather than after the refusal:
# a proactive roll costs one extra bubble, while learning from the refusal costs
# whatever the throttle was holding back. Tencent's own OpenClaw plugin caps a
# turn at 6 minutes for the same reason.
_STREAM_MAX_AGE_S = 8 * 60.0

# Placeholder shown immediately while the agent is still generating. The
# ``<think></think>`` wrapper is a WeCom-native affordance: the client renders
# what is inside it as a collapsed reasoning block rather than as the answer.
_THINKING = "<think>…</think>"

# Trailing "[OPTIONS: a | b | c]" chip trailer (a dashboard convention WeCom
# can't render as tappable chips). Matched only at the very END of the message,
# so use the DOTALL/trailer canonical parser. Defined once in constants.py
# (shared with the Slack/dashboard/Discord/Telegram surfaces) so the
# ReDoS-hardened grammar can never drift; see OPTIONS_RE_TRAILER for the full
# rationale. Per-choice whitespace is stripped by the caller.
_OPTIONS_RE = OPTIONS_RE_TRAILER


def _render_options_as_text(text: str) -> str:
    """Turn a trailing ``[OPTIONS: a | b | c]`` trailer into a numbered list.

    WeCom renders no tappable chips, but deleting the trailer outright -- what
    this did before -- meant the user never learned the choices existed at all,
    and the shipped doc claimed they arrive "as plain text". A numbered list is
    the honest degradation: the user answers by typing, which is a plain message
    on every channel, so no reply parser is needed.

    ``format_overflow`` is the shared sink for exactly this, so the numbering,
    the display-form credential redaction and the mention defanging are not
    re-derived here -- a choice is LLM-authored text landing in a message body,
    where an ``@everyone`` would otherwise mass-notify.

    A still-streaming partial ``[OPTIONS…`` fragment (no closing ``]``) is hidden
    rather than rendered, so protocol markup never flashes as raw text mid-stream.
    """
    m = _OPTIONS_RE.search(text)
    if m:
        body = text[: m.start()].rstrip()
        choices = [c.strip() for c in m.group(1).split("|") if c.strip()]
        if not choices:
            return body
        listing = format_overflow(choices, start=0)
        return f"{body}\n\n{listing}" if body else listing
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip()
    return text


class WeComRenderer(Renderer):
    """Streams a turn to WeCom via WS ``aibot_respond_msg`` frames + fallback.

    Sends a "thinking" placeholder on ``on_turn_start``, throttled full-text
    updates (each frame carries the full accumulated text, replacing the
    bubble) on ``on_text_chunk``, and a final ``finish=true`` frame on
    ``on_done``. If the WS stream path is unavailable/expired it falls back to a
    single one-shot POST to the inbound message's ``response_url``.
    """

    channel_type = "wecom"

    def __init__(
        self,
        client: "WeComClient",
        req_id: str,
        response_url: str,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
        chat_id: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._req_id = req_id
        self._response_url = response_url
        self._session_key = session_key
        # The conversation to PUSH to when an answer outgrows one bubble. A stream
        # frame's acceptance cannot be confirmed (see _deliver_overflow), so the
        # tail rides aibot_send_msg instead.
        self._chat_id = chat_id
        # (stream_id, head) of a sealing frame put on the wire but not yet known
        # accepted. Re-checked at close(), see _recover_unconfirmed_seal.
        self._unconfirmed_seal: tuple[str, str] | None = None
        # Tail chunks held until close() knows whether the head landed, so a
        # recovered head cannot arrive after the text it precedes.
        self._pending_overflow: list[str] = []
        self._stream_id = new_stream_id()
        self._buf: list[str] = []
        self._last_send = 0.0
        # Stream only if we have a req_id to correlate; else go straight to the
        # one-shot response_url fallback at on_done.
        self._stream_ok = bool(req_id)
        self._tool = ""
        self._started = False
        self._finalized = False
        # How much of the answer earlier bubbles already carry. A WeCom stream
        # frame REPLACES its bubble with the full text it is given, so after
        # rolling to a fresh bubble the continuation must send only what comes
        # after this offset — otherwise the reader sees the whole answer twice.
        self._carried = 0
        # Absolute answer lengths carried by the last two frames put on the wire.
        # Two, not one, because a refusal is only observed on a LATER push, so the
        # newest frame is the one that may have been rejected — see
        # ``_roll_if_sealed``.
        self._sent_abs = 0
        self._prev_sent_abs = 0
        # Reasoning chunks, rendered inside WeCom's native <think> block until the
        # answer itself starts arriving.
        self._reasoning: list[str] = []
        # When the CURRENT bubble was opened, so it can be rotated before the
        # platform's ~10-minute stream lifetime seals it (see _STREAM_MAX_AGE_S).
        self._stream_opened_at = time.monotonic()
        # Whether this bubble has already shown a tool footer, so the first one is
        # immediate and the rest are paced (see on_tool_call).
        self._tool_shown = False

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        if self._stream_ok:
            self._stream_ok = await self._client.send_stream(
                self._req_id, self._stream_id, _THINKING, finish=False
            )
        self._last_send = time.monotonic()

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        self._tool = ""  # text resumed -> drop the transient tool footer
        self._reasoning.clear()  # the answer supersedes the reasoning preview
        await self._push(force=False)

    async def on_thinking(self, text: str) -> None:
        """Surface reasoning in WeCom's own collapsed ``<think>`` block.

        Unlike every sibling channel, WeCom has a NATIVE affordance for this: the
        client renders whatever sits inside ``<think></think>`` in ``stream.content``
        as a reasoning block, separate from the answer. So reasoning does not have
        to be dropped (as it was) or faked as body text -- it goes where the
        platform already puts it, and collapses on its own once the answer starts.
        """
        if not text:
            return None
        self._reasoning.append(text)
        await self._push(force=False)
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        self._tool = title or tool_kind or "工具"
        # The FIRST tool footer of a bubble is forced, so a turn that goes straight
        # into a slow tool shows what it is doing instead of sitting on the
        # placeholder. Later ones are throttled: a tool-heavy turn calls this many
        # times in quick succession, and each forced frame spends the
        # conversation's 30-messages/minute budget -- the quota the throttle exists
        # to respect. Forcing every one of them was ~3x over it on a busy turn,
        # while dropping all of them would lose the only progress signal a single
        # long tool call ever produces.
        first = not self._tool_shown
        self._tool_shown = True
        await self._push(force=first)

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        # WeCom has no interactive buttons. The driver only dispatches
        # prompt_choice for INTERACTIVE + a decider, and WeCom runs decider-less
        # (deny-by-default), so this is never reached -- kept as a safe no-op to
        # satisfy the Renderer contract.
        logger.debug("WeCom: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        # A mid-turn out-of-band frame would pollute the single answer bubble;
        # the dispatcher surfaces soft/hard threshold notices post-turn instead.
        logger.debug("WeCom: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        self._tool = ""  # never lock a tool footer into the final answer
        ok = stop_reason != "error"
        answer = self.text()
        # The final frame is the one that MUST land, so give it a live bubble: a
        # turn that outran the platform's stream lifetime has a sealed one, and
        # writing the answer there delivers it nowhere.
        #
        # Roll only when there is something left to deliver. If the sealed bubble
        # already carries the whole answer, opening a fresh one just to seal it
        # would post a bubble with no content in it — the seal is cosmetic
        # (it locks the message), and an unsealed bubble holding the full answer
        # is strictly better than a spurious empty one.
        # Give the final frame a live bubble. ``_roll_if_sealed`` evaluates BOTH
        # conditions -- refused, and expired-but-not-yet-refused -- and no-ops when
        # neither holds, so a live bubble is never rotated for nothing. Checking
        # only ``stream_is_dead`` here missed the aged case: a turn that spent >10
        # minutes in a tool call wrote its answer into an expired bubble and it
        # disappeared. Whether anything is actually POSTED is decided below, by the
        # remainder, so a roll that leaves nothing to say costs no message.
        self._roll_if_sealed()
        remainder = answer[self._carried :]
        if not answer:
            # Routed through _send_final_chunk like any other seal, so a refusal is
            # recovered rather than merely recorded. This is the branch that carries
            # the ERROR notice, and `drive_turn` swallows a provider failure — so if
            # this frame is refused and nothing recovers it, the user is told nothing
            # at all about a turn that failed, while the inbound msgid stays deduped
            # against WeCom's retry. That combination is what makes a lost message
            # look like silence.
            content = "…" if ok else "⚠️ 出错了，请重试"
            if not (self._stream_ok and await self._send_final_chunk(content)):
                await self._client.send_reply(self._response_url, content)
            return
        if not remainder:
            return
        # An answer longer than one bubble is DELIVERED, not truncated. The
        # client's byte guard is a backstop; reaching it would drop the tail
        # silently while `drive_turn` persists the full text, so history and
        # delivery would disagree about what the user was told. Splitting is
        # fence-safe because WeCom renders markdown: a blind cut can sever a code
        # fence and leave the rest of the answer rendered as prose.
        chunks = await asyncio.to_thread(
            split_markdown_safe, remainder, WECOM_SAFE_REPLY_CHARS
        ) or [remainder]
        # Tables convert per CHUNK, and only now that the turn has sealed: a table
        # whose last row was still arriving stayed raw in the streaming frames, so
        # ``final=True`` is the first point it can be rendered whole. Done once,
        # here, so every downstream consumer -- the sealing frame, the overflow
        # pushes and the late head recovery -- carries the identical string. The
        # split ran on the RAW text, and `_render_slice` falls back to raw when a
        # conversion would exceed the cap, so a rendered chunk still fits.
        chunks = [self._render_slice(c, final=True) for c in chunks]
        # The FIRST chunk seals the live bubble the user is already watching. Any
        # OVERFLOW goes out as a proactive push instead of another stream frame,
        # because a push mints its OWN unique req_id and its ACK is therefore
        # exactly correlatable — so acceptance can be CONFIRMED, which a stream
        # frame's cannot be (every frame of a turn replays the one inbound req_id,
        # so a waiter for one can be resolved by another's ACK). That is what makes
        # a long answer's tail either delivered or reported, never silently lost.
        head, overflow = chunks[0], chunks[1:]
        if not (self._stream_ok and await self._send_final_chunk(head)):
            # Stream never started or died mid-turn, so no bubble is showing this
            # text and a push is not a duplicate here -- unlike the sealed-bubble
            # path. Prefer it: it is CONFIRMED, whereas ``send_reply`` returns None
            # and its one-shot POST cannot report a failure at all, so a lost head
            # looked identical to a delivered one.
            delivered = False
            if self._chat_id:
                delivered = await self._client.send_proactive(self._chat_id, head)
            if not delivered:
                # No warm conversation to push into (or the push was refused): the
                # single-use response_url is the last channel left. Its verdict is
                # real now -- `send_reply` reports the platform's answer -- so this is
                # knowledge rather than the "a URL exists, so it must have worked"
                # assumption it replaced, which let a headless tail follow a failed
                # POST.
                delivered = await self._client.send_reply(self._response_url, head)
            if overflow and not delivered:
                # A tail without its head is a fragment the reader cannot tell is
                # incomplete. Withheld and reported, matching how _deliver_overflow
                # stops on a refusal rather than pressing on.
                logger.warning(
                    "WeCom: the answer's head could not be delivered; withholding "
                    "%d tail chunk(s) rather than sending a headless fragment",
                    len(overflow),
                )
                return
            if overflow:
                await self._deliver_overflow(overflow)
            return
        # The head is on the wire but UNACKNOWLEDGED, so it may yet need re-delivery
        # (see _recover_unconfirmed_seal). Sending the tail now would put it ahead of
        # a head recovered later, and the reader would meet the answer's middle
        # before its beginning — with nothing to say the order was scrambled. Held
        # until close(), which recovers the head first and then releases this.
        self._pending_overflow = overflow

    async def _deliver_overflow(self, chunks: list[str]) -> None:
        """Deliver the answer's tail as CONFIRMED proactive pushes.

        Each push carries its own req_id, so ``send_proactive`` waits for the
        platform's verdict and a refusal is visible rather than assumed-delivered.
        Paced, because every message spends the conversation's 30/minute budget.

        No authorization check here: this is the tail of an answer to the person who
        just wrote, in their own conversation, already authorized by
        ``WeComTransport.receive``. The allow-list recheck in
        ``WeComTransport.send_message`` guards a different case — a PERSISTED mirror
        binding that can outlive the permission behind it.

        A chunk the platform refuses is logged with its position, so the operator
        can see exactly how much of the answer arrived. Nothing here can recover a
        refusal: the alternative is pretending the tail landed.
        """
        if not chunks:
            return
        if not self._chat_id:
            logger.warning(
                "WeCom: %d answer chunk(s) undeliverable — no conversation id to push to",
                len(chunks),
            )
            return
        for i, chunk in enumerate(chunks):
            await asyncio.sleep(_STREAM_THROTTLE_S)
            if not await self._client.send_proactive(self._chat_id, chunk):
                logger.warning(
                    "WeCom: answer chunk %d/%d was refused by the platform; "
                    "%d further chunk(s) not attempted",
                    i + 2,
                    len(chunks) + 1,
                    len(chunks) - i - 1,
                )
                return

    async def _send_final_chunk(self, chunk: str) -> bool:
        """Send one sealing frame.

        Its acceptance is NOT awaited, and that is a protocol limit rather than a
        choice: every stream frame of a turn replays the same inbound ``req_id``,
        which is the only correlation key an ACK carries. A waiter registered for
        the final frame can therefore be resolved by a still-outstanding
        intermediate frame's ACK, reporting success for a frame that was refused —
        worse than not asking, because it would suppress the recovery path.

        What covers the dominant failure instead is PROACTIVE rotation: the bubble
        is rolled before the platform's ~10-minute lifetime rather than after the
        refusal (see ``_roll_if_sealed`` and ``_STREAM_MAX_AGE_S``), so the frame
        that must land is written to a bubble known to be young. A refusal for any
        other reason still falls through to the ``response_url`` one-shot below.
        """
        sealed = await self._client.send_stream(self._req_id, self._stream_id, chunk, finish=True)
        if not sealed:
            return False
        # A stream frame's ACK cannot be WAITED on (every frame of a turn replays the
        # one inbound req_id, so a waiter for this frame could be resolved by
        # another's). But a terminal ACK that has ALREADY landed is observable, so
        # the one case that can be checked is checked: if the bubble is now known
        # dead, the seal went nowhere, and the chunk is re-delivered as a CONFIRMED
        # push. A strict improvement over assuming success; the window this cannot
        # see is what the proactive age rotation exists to keep small.
        if self._client.stream_is_dead(self._stream_id) and self._chat_id:
            logger.info("WeCom: the sealing frame was refused; re-delivering it as a push")
            return await self._client.send_proactive(self._chat_id, chunk)
        # No verdict yet. Remember the frame so ``close()`` can ask again once the
        # turn's remaining work has given the ACK time to arrive.
        self._unconfirmed_seal = (self._stream_id, chunk)
        return True

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done."""
        if not self._finalized:
            await self.on_done(stop_reason="error")
        # Order matters: the head's verdict is checked and recovered FIRST, then the
        # tail it precedes is released.
        head_ok = await self._recover_unconfirmed_seal()
        await self._release_pending_overflow(head_ok=head_ok)

    async def _recover_unconfirmed_seal(self) -> bool:
        """Ask once more whether the sealing frame was accepted, and recover if not.

        ``_send_final_chunk`` can only check for a verdict that has ALREADY landed,
        and it checks microseconds after putting the frame on the wire, so the
        common case is that no ACK has arrived yet and a refusal is invisible. This
        is the second look, and it is FREE: ``drive_turn`` calls ``close()`` in its
        ``finally``, after persistence and the post-turn notice, so the ACK has had
        the length of that real work to arrive rather than an artificial delay.

        A bounded sleep-and-poll was the alternative and is worse: it would add
        fixed latency to every turn, and it cannot even be exited early on success,
        because "the ACK said 0" and "no ACK yet" are indistinguishable — WeCom is
        not documented to acknowledge an accepted frame at all.

        Recovery is a CONFIRMED push, which is correlatable where a stream frame is
        not (its own req_id), so this either delivers or reports. Consumed on the
        first call so a second ``close()`` cannot post the answer twice.

        Returns whether the head is in the reader's hands: True when nothing needed
        recovering or the recovery push was accepted, False when it was refused or
        there was no conversation to push into. The caller gates the TAIL on it — a
        tail without its head is a fragment nothing marks as incomplete.
        """
        pending, self._unconfirmed_seal = self._unconfirmed_seal, None
        if pending is None:
            return True
        stream_id, chunk = pending
        if not (
            self._client.stream_is_dead(stream_id) or self._client.stream_had_rejection(stream_id)
        ):
            return True
        if not self._chat_id:
            # Refused, and no warm conversation to recover through. Reported so the
            # caller withholds the tail rather than shipping a headless fragment.
            logger.warning(
                "WeCom: the sealing frame was refused and there is no conversation id "
                "to re-deliver it with"
            )
            return False
        logger.info(
            "WeCom: the sealing frame was refused (verdict arrived after the turn); "
            "re-delivering the answer as a confirmed push"
        )
        return await self._client.send_proactive(self._chat_id, chunk)

    async def _release_pending_overflow(self, *, head_ok: bool = True) -> None:
        """Deliver the tail chunks held back by :meth:`on_done`.

        Deferred rather than sent inline so the head's late verdict can be acted on
        first — a long answer whose sealing frame is refused would otherwise show its
        tail, then its beginning.

        The cost is that the tail lands after persistence instead of before it, so a
        crash in that narrow window leaves history holding an answer the reader only
        partly received. That is the lesser of the two: the window is one local write
        wide and needs a crash to open, whereas the misordering needed only a refused
        frame. The alternative considered was re-sending head AND tail on recovery,
        which keeps the current timing but duplicates every tail chunk.
        """
        chunks, self._pending_overflow = self._pending_overflow, []
        if not chunks:
            return
        if not head_ok:
            # The head was refused and could not be recovered, so the tail would be a
            # fragment the reader cannot tell is incomplete. Withheld and reported --
            # the same rule the no-stream fallback applies, which this path was
            # missing.
            logger.warning(
                "WeCom: the answer's head could not be delivered; withholding %d tail "
                "chunk(s) rather than sending a headless fragment",
                len(chunks),
            )
            return
        await self._deliver_overflow(chunks)

    # -- helpers ------------------------------------------------------------
    def text(self) -> str:
        """The turn's visible answer so far (OPTIONS stripped, no tool footer).

        Used both for the final frame and to persist the reply to history.
        """
        return _render_options_as_text("".join(self._buf).strip())

    def _render_slice(self, body: str, *, final: bool) -> str:
        """Render tables in ONE outgoing slice, never in the whole answer.

        Table conversion changes the string's length, and ``_carried`` /
        ``_sent_abs`` are offsets into the RAW answer (``text()``), so converting
        the whole answer and slicing the result would index a different string —
        dropping or repeating text at every bubble rotation. Converting only the
        slice that is about to be sent keeps one coordinate space for the
        arithmetic and makes the conversion what it actually is: a display
        transform at the send boundary.

        If the conversion alone pushes an otherwise deliverable slice past the cap,
        the raw form wins: losing tail rows to a blind truncation is worse than an
        unconverted table, and ``safe_raw_table_fallback`` is the display-safe way
        to say so. Its ``None`` means "no safe raw candidate", which is why the
        unconverted ``body`` is the last resort rather than the converted overflow.
        """
        converted = self.render_tables_for_target(body, final=final)
        cap = self.capabilities.max_message_chars
        if cap <= 0 or len(converted) <= cap:
            return converted
        safe_raw = self.safe_raw_table_fallback(body, final=final)
        if safe_raw is not None and len(safe_raw) <= cap:
            return safe_raw
        return body

    def _roll_if_sealed(self) -> None:
        """Move to a fresh bubble when WeCom has sealed the current one.

        A bubble stops accepting writes after the platform's 10-minute stream
        lifetime (errcode 846608), or if its req_id becomes unroutable (846605).
        An agentic turn doing tool work routinely runs past ten minutes, and the
        refusal is asynchronous — ``send_stream`` reports only that the frame
        reached the socket — so without this the renderer keeps "succeeding" into
        a sealed bubble and the rest of the answer, including the final frame, is
        never seen.

        Rolling keeps what the sealed bubble already shows and continues after
        it, so the answer is delivered in two bubbles rather than lost.

        Where the continuation RESUMES from is deliberately conservative. The
        refusal is only observed on a later push, so the most recent frame is
        exactly the one that may have been rejected — resuming after it would drop
        text the reader never received. The continuation therefore resumes from
        the frame BEFORE it, which was acknowledged by being followed by another
        accepted send. The cost is that one frame's worth of text can appear
        twice; the alternative is a silent hole in the answer, and a visible
        repeat is the better failure.
        """
        sealed = self._client.stream_is_dead(self._stream_id)
        aged = time.monotonic() - self._stream_opened_at >= _STREAM_MAX_AGE_S
        if not sealed and not aged:
            return
        # An aged roll normally happens with nothing refused, so everything already
        # sent is known-delivered and the continuation resumes exactly after it --
        # which is why age and sealing are not treated alike: making every aged
        # rotation replay a frame would duplicate text on every long turn.
        #
        # The exception is a NON-TERMINAL refusal of this bubble's newest frame.
        # Those are normally self-healing, because each frame carries the bubble's
        # full accumulated text and the next one supersedes the refused one -- but
        # if the bubble rotates for age instead of getting a successor, that frame
        # was never delivered and resuming after it would skip it silently.
        # ``stream_had_rejection`` answers "was everything written here accepted",
        # so the conservative resume is driven by evidence rather than assumed.
        unconfirmed = sealed or self._client.stream_had_rejection(self._stream_id)
        self._carried = self._prev_sent_abs if unconfirmed else self._sent_abs
        # Both offsets belong to the bubble being ABANDONED, and a delivery it
        # accepted says nothing about the replacement. Carrying them forward is how
        # a SECOND refusal loses text: 846605 means the req_id is unroutable, so it
        # refuses every replacement bubble too, and the next roll would then read a
        # `_prev_sent_abs` recorded against the previous bubble and resume PAST the
        # span this one never managed to deliver. Rebased on the new bubble's start,
        # a bubble refused before it accepted anything resumes exactly where it
        # began, so the worst case stays a visible repeat instead of a silent hole.
        self._prev_sent_abs = self._sent_abs = self._carried
        self._stream_id = new_stream_id()
        self._stream_opened_at = time.monotonic()
        self._tool_shown = False
        logger.info(
            "WeCom: continuing the answer in a new bubble (%s)",
            "sealed by the platform" if sealed else "approaching the stream lifetime",
        )

    async def _push(self, *, force: bool) -> None:
        if not self._stream_ok:
            return
        now = time.monotonic()
        if not force and now - self._last_send < _STREAM_THROTTLE_S:
            return
        self._roll_if_sealed()
        # Compose from the answer slice EXPLICITLY, so the number of answer
        # characters this frame carries is known rather than assumed. Recording
        # the full accumulated length while sending a capped frame is how a later
        # rotation came to resume past text that was never delivered.
        answer = self.text()
        body = answer[self._carried :]
        if not body and self._reasoning:
            # Reasoning shows only while the answer is still empty; once real text
            # arrives the answer is what the bubble is for. Nothing of the answer
            # is delivered by this frame, so no progress is recorded for it.
            # Redacted on the JOINED text, not per chunk. The driver already
            # redacts each thinking chunk, but with a plain per-chunk pass rather
            # than the rolling ``StreamRedactor`` it uses for the answer — so a
            # credential split across two chunks survives both halves and is
            # reconstituted right here by the join. Redacting the assembled string
            # at the send boundary closes that, and also covers a credential that
            # was never split. Same placement, and the same reason, as Slack's
            # ``_maybe_post_thinking``.
            reasoning = "".join(self._reasoning)
            reasoning, _ = redact_exfiltration_urls(reasoning)
            reasoning, _ = redact_credentials(reasoning)
            reasoning = f"<think>{reasoning}</think>"
            self._stream_ok = await self._client.send_stream(
                self._req_id, self._stream_id, reasoning, finish=False
            )
            self._last_send = now
            return
        footer = f"🔧 正在运行：{self._tool}…" if self._tool else ""
        cap = self.capabilities.max_message_chars
        if cap > 0 and footer:
            # The footer is transient decoration; the answer is the payload, so
            # the budget is spent on the answer and the footer only rides along
            # when it fits beside it.
            body = body[: max(0, cap - len(footer) - 2)]
        elif cap > 0:
            body = body[:cap]
        # Progress is recorded from the RAW slice, before any table conversion: the
        # offsets index ``text()``, and a converted string has a different length.
        sent_abs = self._carried + len(body)
        # Display transform, applied to the slice actually going out.
        body = self._render_slice(body, final=False)
        content = f"{body}\n\n{footer}" if body and footer else (body or footer)
        if not content:
            return
        self._stream_ok = await self._client.send_stream(
            self._req_id, self._stream_id, content, finish=False
        )
        if self._stream_ok:
            self._prev_sent_abs, self._sent_abs = self._sent_abs, sent_abs
        self._last_send = now
