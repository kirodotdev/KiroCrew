"""Layer 2b -- Feishu ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream onto a single Feishu REST
reply anchored to the inbound message_id:

* ``on_turn_start``   -- no-op (no streaming placeholder in v1).
* ``on_text_chunk``   -- buffers text; a trailing ``[OPTIONS:]`` trailer
  becomes a numbered text list (Feishu renders no tappable chips).
* ``on_tool_call``    -- updates a transient tool-footer in the buffer.
* ``on_prompt_choice``-- no-op: Feishu has no interactive buttons in v1
  (the driver only dispatches this for INTERACTIVE + a decider, and
  FeishuDispatcher runs decider-less).
* ``on_compaction``   -- logged only (threshold notices go post-turn).
* ``on_done``         -- sends the complete accumulated text as one reply, then
  each local image it references as its own image reply.
* ``close``           -- idempotent finalisation (sends on error if needed).

Dependency direction is ``feishu -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

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
    """Buffers a complete turn and sends it as one Feishu reply on ``on_done``.

    v1 is intentionally simple: no streaming, no edit-in-place.  A streaming
    variant (send a placeholder, then update with ``lark_client.im.v1.message.
    update``) is a natural follow-up once the core integration is stable.
    """

    channel_type = "feishu"

    def __init__(
        self,
        client: "LarkClient",
        message_id: str,
        capabilities: TransportCapabilities,
        *,
        uploads_allowed: bool = False,
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

    # -- Output event handlers ----------------------------------------------

    async def on_turn_start(self) -> None:
        # No streaming placeholder in v1.  A "thinking" reaction could be added
        # here via ``lark_client.im.v1.message.reactions.create`` once proven
        # useful.
        pass

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        self._tool = ""  # text resumed -> clear transient tool footer

    async def on_thinking(self, text: str) -> None:
        # Feishu v1 does not surface reasoning inline.
        return None

    async def on_tool_call(
        self,
        tool_call_id: str,
        title: str,
        tool_kind: str = "",
        tool_purpose: str = "",
    ) -> None:
        # Record but don't push mid-turn (single-shot reply only).
        self._tool = title or tool_kind or "工具"

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        # Feishu has no interactive buttons in v1.  The driver dispatches this
        # only for INTERACTIVE + a decider; FeishuDispatcher runs decider-less,
        # so this is never reached -- kept as a safe no-op to satisfy the
        # Renderer contract.
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
        # An answer that was nothing but images sends only the images.
        if content and not await self._client.send_reply(self._message_id, content):
            # A dropped reply must NOT be recorded as a delivered turn: the user
            # sees nothing while history and the session claim success. Raising
            # routes it through the driver's failure path instead.
            raise RuntimeError(f"Feishu reply was not delivered (message_id={self._message_id})")
        cred_count, url_count = count_redaction_tags(content)
        if cred_count or url_count:
            # The reply above carries a redaction placeholder, so a follow-up
            # notice tells the reader the text was rewritten. Counted over the
            # DELIVERED body, and best-effort by the shared contract: the
            # answer is already out, so a failed notice send is logged, never
            # raised. ``send_reply`` reports failure as ``False`` rather than
            # raising, so both shapes are covered.
            await self._best_effort_reply(
                redaction_notice(cred_count, url_count), "the redaction notice"
            )
        failed = [
            image.path
            for image in images
            if not await self._client.send_image_reply(self._message_id, image.data)
        ]
        if failed:
            # Extraction cut these references out of the answer, so a failed upload
            # names each path rather than leaving a reply that mentions no picture.
            note = self.redact_for_target("⚠️ Couldn't upload:\n" + "\n".join(failed))
            if content or len(failed) < len(images):
                await self._best_effort_reply(note, "the failed-upload list")
            elif not await self._client.send_reply(self._message_id, note):
                # Nothing of this answer landed, so it must not pass as delivered.
                raise RuntimeError(
                    f"Feishu reply was not delivered (message_id={self._message_id})"
                )

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
        return render_options_as_text("".join(self._buf).strip(), self.capabilities)

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
