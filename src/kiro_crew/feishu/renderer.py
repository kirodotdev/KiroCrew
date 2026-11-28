"""Layer 2b -- Feishu ``Renderer``.

Two modes, chosen per turn by the dispatcher:

**Buffered (default).** Maps the channel-neutral ``OutputEvent`` stream onto a
single Feishu REST reply anchored to the inbound message_id. This is the original
behaviour and remains the fallback for everything.

**Streaming card** (``streaming=True``, DM only). Opens a CardKit card on turn
start so the user sees the reply forming immediately, pushes the cumulative
answer into it on a throttle, and seals it on ``on_done``. See
:mod:`kiro_crew.feishu.streaming_card`. Any failure at any point falls back to
the buffered reply, so the worst case is the old behaviour rather than a lost
answer.

Event handling in both modes:

* ``on_turn_start``   -- opens the card in streaming mode; no-op otherwise.
  Called TWICE per turn by the pipeline, so it must stay idempotent.
* ``on_text_chunk``   -- buffers text; in streaming mode also pushes a live
  frame. A trailing ``[OPTIONS:]`` trailer becomes a numbered text list in the
  FINAL text (Feishu renders no tappable chips) and is withheld from live
  frames, where a half-arrived marker is still reserved protocol.
* ``on_tool_call``    -- updates a transient tool footer; forces a live frame,
  because a tool call is often followed by a long silence.
* ``on_prompt_choice``-- no-op: Feishu has no interactive buttons (the driver
  only dispatches this for INTERACTIVE + a decider, and FeishuDispatcher runs
  decider-less).
* ``on_compaction``   -- logged only (threshold notices go post-turn).
* ``on_done``         -- seals the card, or sends the complete text as one reply,
  then each local image it references as its own image reply.
* ``close``           -- idempotent finalisation (sends on error if needed).

Dependency direction is ``feishu -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from kiro_crew.feishu.streaming_card import StreamingCardSession
from kiro_crew.messaging.outbound_files import (
    ExtractLimits,
    OutboundFile,
    extract_local_refs_off_loop,
)
from kiro_crew.messaging.renderer import (
    Renderer,
    count_redaction_tags,
    redaction_notice,
    render_options_as_text,
    split_options_trailer,
)
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.feishu.client import LarkClient

logger = logging.getLogger(__name__)

#: Feishu takes images up to 10 MB each; ten is the most one answer sends.
FEISHU_UPLOAD_LIMITS = ExtractLimits(max_files=10, max_file_bytes=10 * 1024 * 1024)

#: Refusal lines appended to an answer before the rest collapse into a count.
_MAX_REJECTION_LINES = 3


class FeishuRenderer(Renderer):
    """Buffers a turn and sends one Feishu reply, or streams it into a card."""

    channel_type = "feishu"

    def __init__(
        self,
        client: "LarkClient",
        message_id: str,
        capabilities: TransportCapabilities,
        *,
        uploads_allowed: bool = False,
        streaming: bool = False,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._message_id = message_id
        self._buf: list[str] = []
        self._tool: str = ""
        self._finalized = False
        # Images go out only when the dispatcher allows them for this turn (an
        # unrestricted DM) and the provider's cwd was authorized as the upload root.
        self._uploads_allowed = uploads_allowed
        self._upload_root = ""
        self._scratch_root = ""
        #: Streaming is opt-in per turn. Keyword-only with a False default so
        #: every existing caller -- and the cross-channel contract tests --
        #: keep the original buffered behaviour untouched.
        self._streaming_enabled = bool(streaming)
        self._card: StreamingCardSession | None = None
        self._card_opened = False

    # -- Streaming plumbing --------------------------------------------------

    async def _open_card(self) -> None:
        """Open the live card once per turn. Never raises."""
        if self._card_opened or not self._streaming_enabled:
            return
        # Latch BEFORE awaiting: on_turn_start is called twice and the two calls
        # can overlap, which would otherwise create two cards for one turn.
        self._card_opened = True
        session = StreamingCardSession(
            self._client, self._message_id, screen=self.redact_for_target
        )
        try:
            started = await session.start()
        except Exception:  # pragma: no cover - start() already swallows
            logger.debug("Feishu: streaming card start raised", exc_info=True)
            started = False
        if started:
            self._card = session
        else:
            if session.anchor_gone:
                # The card reply failed because the inbound message is gone. The
                # verdict lives on the session, so dropping it here would lose
                # it: ``on_done`` would then send a buffered reply to a recalled
                # anchor, which fails and is raised as a delivery error for
                # something the user did on purpose. Keep the session -- it is
                # not live, so it takes no frames and finishes as undelivered;
                # all it still carries is that verdict.
                self._card = session
            logger.info("Feishu: no streaming card for this turn; falling back to a buffered reply")

    async def _push_live(self, *, force: bool = False) -> None:
        """Push the cumulative answer into the live card. Never raises."""
        card = self._card
        if card is None or not card.live:
            return
        tool = self._tool

        def _frame() -> str:
            # Built only when the card is due a push (see ``push``), so a long
            # many-chunk answer is not re-split and re-scanned on every chunk.
            #
            # hide_partial=True: the text is still arriving, so a half-written
            # "[OPTIONS" really may be a marker mid-flight. Safe here precisely
            # because the next frame re-renders from the full buffer.
            body, _choices = split_options_trailer(self.raw_text(), hide_partial=True)
            frame = body
            if tool:
                footer = f"🔧 {tool}"
                frame = f"{frame}\n\n{footer}" if frame else footer
            # Redact here as well as in :meth:`text`. A live frame reaches the
            # screen BEFORE the send-boundary scrub in ``text()`` runs, and the
            # channel-neutral stream pass upstream is a literal byte scan, so a
            # credential split by markdown (``AKIA**REST**``) or hidden in a
            # link is reassembled by the card's own markdown rendering and
            # shown. The session redacts its prepared body again.
            return self.redact_for_target(frame) if frame else ""

        size = sum(map(len, self._buf)) + (len(tool) + 4 if tool else 0)
        try:
            await card.push(_frame, force=force, size=size)
        except Exception:  # pragma: no cover - the session classifies its own errors
            logger.debug("Feishu: live frame push raised", exc_info=True)

    # -- Output event handlers ----------------------------------------------

    async def on_turn_start(self) -> None:
        await self._open_card()

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        self._tool = ""  # text resumed -> clear transient tool footer
        await self._push_live()

    async def on_thinking(self, text: str) -> None:
        # Feishu does not surface reasoning inline.
        return None

    async def on_tool_call(
        self,
        tool_call_id: str,
        title: str,
        tool_kind: str = "",
        tool_purpose: str = "",
    ) -> None:
        self._tool = title or tool_kind or "工具"
        # Forced: a tool call is frequently followed by a long quiet period, and
        # the whole point of the live card is that the user sees progress.
        await self._push_live(force=True)

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        # Feishu has no interactive buttons.  The driver dispatches this only for
        # INTERACTIVE + a decider; FeishuDispatcher runs decider-less, so this is
        # never reached -- kept as a safe no-op to satisfy the Renderer contract.
        logger.debug("Feishu: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        # Threshold notices are surfaced post-turn by the dispatcher; a
        # mid-turn frame would corrupt the single-shot answer bubble.
        logger.debug("Feishu: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        ok = stop_reason != "error"
        body = self._rendered()
        images: list[OutboundFile] = []
        if self.capabilities.files_outbound and self._uploads_allowed and self._upload_root:
            body, images = await self._extract_uploads(body)
        content = self.redact_for_target(body)
        if not content and not images:
            content = "…" if ok else "⚠️ 出错了，请重试"

        card = self._card
        if card is not None:
            # The card needs a body even when the answer was nothing but images:
            # its live frames showed the references, which extraction took out.
            delivered = await card.finish(content or "…")
            if delivered:
                # The notice is a plain reply on the same anchor, exactly as in
                # buffered mode, counted over the body the card actually shows:
                # that body is redacted once more after the card transforms.
                await self._send_redaction_notice(card.shown or content)
                await self._send_images(images, answer_landed=True)
                return
            if card.anchor_gone:
                # The inbound message was recalled or deleted. A text reply to
                # the same anchor fails too, so stop rather than raising a
                # delivery error for something the user did on purpose.
                logger.info("Feishu: anchor message gone; nothing to reply to")
                return
            logger.info("Feishu: card delivered nothing; sending the buffered reply instead")

        # An answer that was nothing but images sends only the images.
        if content and not await self._client.send_reply(self._message_id, content):
            # A dropped reply must NOT be recorded as a delivered turn: the user
            # sees nothing while history and the session claim success. Raising
            # routes it through the driver's failure path instead.
            raise RuntimeError(f"Feishu reply was not delivered (message_id={self._message_id})")
        await self._send_redaction_notice(content)
        await self._send_images(images, answer_landed=bool(content))

    async def _send_redaction_notice(self, content: str) -> None:
        """Follow a delivered, rewritten answer with one notice reply.

        Counted over the DELIVERED body, and best-effort by the shared
        contract: the answer is already out, so a failed notice send is logged,
        never raised. ``send_reply`` reports failure as ``False`` rather than
        raising, so both shapes are covered.
        """
        cred_count, url_count = count_redaction_tags(content)
        if cred_count or url_count:
            await self._best_effort_reply(
                redaction_notice(cred_count, url_count), "the redaction notice"
            )

    async def _send_images(self, images: list[OutboundFile], *, answer_landed: bool) -> None:
        """Send each extracted image as its own reply after the answer.

        Extraction cut these references out of the answer, so a failed upload
        names each path rather than leaving a reply that mentions no picture.
        When neither the answer text nor any image landed, the failed-upload
        list is the only reply, so its failure is raised: nothing of this answer
        reached the user and it must not pass as delivered.
        """
        failed = [
            image.path
            for image in images
            if not await self._client.send_image_reply(self._message_id, image.data)
        ]
        if not failed:
            return
        note = self.redact_for_target("⚠️ Couldn't upload:\n" + "\n".join(failed))
        if answer_landed or len(failed) < len(images):
            await self._best_effort_reply(note, "the failed-upload list")
        elif not await self._client.send_reply(self._message_id, note):
            raise RuntimeError(f"Feishu reply was not delivered (message_id={self._message_id})")

    async def close(self) -> None:
        """Idempotent teardown -- finalise if ``on_done`` was never reached.

        Runs from the driver's ``finally``, so a failed send here is logged
        rather than raised: raising would replace whatever error brought the
        turn down with a delivery error and lose the real cause.
        """
        if not self._finalized:
            try:
                await self.on_done(stop_reason="error")
            except Exception:
                logger.warning(
                    "Feishu: could not deliver the error reply for %s",
                    self._message_id,
                    exc_info=True,
                )

    # -- Helpers ------------------------------------------------------------

    async def _best_effort_reply(self, text: str, what: str) -> None:
        """Send one more reply after the answer; log a failure, never raise it."""
        try:
            if await self._client.send_reply(self._message_id, text):
                return
            logger.warning("feishu: could not deliver %s (answer already sent)", what)
        except Exception:
            logger.warning(
                "feishu: could not deliver %s (answer already sent)", what, exc_info=True
            )

    def authorize_upload_root(self, root: str, scratch_root: str = "") -> None:
        """Authorize the provider's resolved cwd as the upload root.

        A non-absolute root disables uploads rather than widening them, and the
        session's own ``$KIROCREW_SCRATCH`` counts only beside a valid root.
        """
        self._upload_root = root if os.path.isabs(root) else ""
        self._scratch_root = (
            scratch_root if self._upload_root and os.path.isabs(scratch_root) else ""
        )

    async def _extract_uploads(self, body: str) -> tuple[str, list[OutboundFile]]:
        """Pull the answer's local image references out for upload, fail-soft.

        A refused reference keeps its markup and gains a ``⚠️ … not sent`` line;
        an extraction error costs the pictures, never the answer.
        """
        try:
            result = await extract_local_refs_off_loop(
                body,
                within_root=self._upload_root,
                limits=FEISHU_UPLOAD_LIMITS,
                scratch_root=self._scratch_root,
            )
        except Exception:
            logger.warning("feishu: outbound image extraction failed", exc_info=True)
            return body, []
        if result.rejections:
            sel().log_api_access(
                caller="feishu",
                operation="feishu_renderer.upload_files",
                outcome="denied",
                source="feishu",
                resources=f"{len(result.rejections)} rejection(s)",
                # Only the closed reason CODES, never the LLM-authored destination.
                error=",".join(sorted({item.reason for item in result.rejections})),
            )
        if result.files:
            sel().log_api_access(
                caller="feishu",
                operation="feishu_renderer.upload_files",
                outcome="allowed",
                source="feishu",
                resources=f"{len(result.files)} file(s)",
            )
        text = result.rewritten_text.strip()
        if result.rejections:
            lines = [f"⚠️ {rejection}" for rejection in result.rejections[:_MAX_REJECTION_LINES]]
            if len(result.rejections) > _MAX_REJECTION_LINES:
                lines.append(f"⚠️ …and {len(result.rejections) - _MAX_REJECTION_LINES} more")
            notes = "\n".join(lines)
            text = f"{text}\n\n{notes}" if text else notes
        return text, result.files

    def _rendered(self) -> str:
        """The answer with ``[OPTIONS:]`` as numbered text, before redaction."""
        return render_options_as_text(self.raw_text(), self.capabilities)

    def raw_text(self) -> str:
        """The accumulated answer with no options handling applied.

        Live frames need this because they do their own ``hide_partial=True``
        split; :meth:`text` would already have rendered a mid-flight marker as
        numbered prose.
        """
        return "".join(self._buf).strip()

    def text(self) -> str:
        """The complete answer so far, with ``[OPTIONS:]`` as numbered text.

        ``on_done`` sends the same body, scrubbed by the same pass, after taking
        out the image references it uploads. History is persisted separately by
        ``messaging.dispatch`` from the driver's own accumulated text, so the two
        are not guaranteed to agree — a difference that only shows up in what the
        transcript records, never in what the user is shown.

        The body is scrubbed render-aware here, at the send boundary, the same as
        every channel that renders markdown itself does at its own send. Feishu
        renders the body as markdown, and the channel-neutral stream pass upstream
        is a literal byte scan, so a credential split by emphasis
        (``AKIA**REST**``) or a link survives it and is reassembled on screen. The
        numbered ``[OPTIONS:]`` choices are already display-safe (``apply_options_cap``
        redacts them); ``redact_for_target`` is idempotent, so covering them again
        with the body costs nothing.
        """
        return self.redact_for_target(self._rendered())
