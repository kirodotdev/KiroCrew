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
* ``on_done``         -- seals the card, or sends the complete text as one reply.
* ``close``           -- idempotent finalisation (sends on error if needed).

Dependency direction is ``feishu -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.renderer import (
    Renderer,
    render_options_as_text,
    split_options_trailer,
)
from kiro_crew.messaging.transport import TransportCapabilities

try:
    from kiro_crew.feishu.streaming_card import StreamingCardSession
except ImportError:  # pragma: no cover - only when the module is absent
    # A missing streaming module must degrade to the buffered reply, never take
    # the whole package down: ``kiro_crew.channels`` imports this module during
    # gateway boot, so a hard import here would turn one absent file into a
    # gateway that cannot start AT ALL -- and an absent file is exactly what an
    # update that overwrites ``src/`` leaves behind.
    StreamingCardSession = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from kiro_crew.feishu.client import LarkClient

logger = logging.getLogger(__name__)


class FeishuRenderer(Renderer):
    """Buffers a turn and sends one Feishu reply, or streams it into a card."""

    channel_type = "feishu"

    def __init__(
        self,
        client: "LarkClient",
        message_id: str,
        capabilities: TransportCapabilities,
        *,
        streaming: bool = False,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._message_id = message_id
        self._buf: list[str] = []
        self._tool: str = ""
        self._finalized = False
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
        if StreamingCardSession is None:  # pragma: no cover - module absent
            # Latch so this is reported once per turn, not once per chunk.
            self._card_opened = True
            logger.warning(
                "Feishu: streaming is enabled but the streaming_card module is "
                "missing; this turn falls back to a buffered plain-text reply"
            )
            return
        # Latch BEFORE awaiting: on_turn_start is called twice and the two calls
        # can overlap, which would otherwise create two cards for one turn.
        self._card_opened = True
        session = StreamingCardSession(self._client, self._message_id)
        try:
            started = await session.start()
        except Exception:  # pragma: no cover - start() already swallows
            logger.debug("Feishu: streaming card start raised", exc_info=True)
            started = False
        if started:
            self._card = session
        else:
            logger.info("Feishu: no streaming card for this turn; falling back to a buffered reply")

    async def _push_live(self, *, force: bool = False) -> None:
        """Push the cumulative answer into the live card. Never raises."""
        card = self._card
        if card is None or not card.live:
            return
        # hide_partial=True: the text is still arriving, so a half-written
        # "[OPTIONS" really may be a marker mid-flight. Safe here precisely
        # because the next frame re-renders from the full buffer.
        body, _choices = split_options_trailer(self.raw_text(), hide_partial=True)
        frame = body
        if self._tool:
            footer = f"🔧 {self._tool}"
            frame = f"{frame}\n\n{footer}" if frame else footer
        if not frame:
            return
        try:
            await card.push(frame, force=force)
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
        content = self.text() or ("…" if ok else "⚠️ 出错了，请重试")

        card = self._card
        if card is not None:
            delivered = await card.finish(content)
            if delivered:
                return
            if card.anchor_gone:
                # The inbound message was recalled or deleted. A text reply to
                # the same anchor fails too, so stop rather than raising a
                # delivery error for something the user did on purpose.
                logger.info("Feishu: anchor message gone; nothing to reply to")
                return
            logger.info("Feishu: card delivered nothing; sending the buffered reply instead")

        if not await self._client.send_reply(self._message_id, content):
            # A dropped reply must NOT be recorded as a delivered turn: the user
            # sees nothing while history and the session claim success. Raising
            # routes it through the driver's failure path instead.
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

    def raw_text(self) -> str:
        """The accumulated answer with no options handling applied.

        Live frames need this because they do their own ``hide_partial=True``
        split; :meth:`text` would already have rendered a mid-flight marker as
        numbered prose.
        """
        return "".join(self._buf).strip()

    def text(self) -> str:
        """The complete answer so far, with ``[OPTIONS:]`` as numbered text.

        This is what ``on_done`` sends. History is persisted separately by
        ``messaging.dispatch`` from the driver's own accumulated text, so the two
        are not guaranteed to agree — a difference that only shows up in what the
        transcript records, never in what the user is shown.
        """
        return render_options_as_text("".join(self._buf).strip(), self.capabilities)
