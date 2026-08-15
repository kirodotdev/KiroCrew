"""Turn rendering for the WhatsApp channel.

No edit/streaming primitive exists on the Web protocol: every send creates a
NEW bubble, so this renderer BUFFERS the whole turn and emits it once on
``on_done`` (the Weixin pattern). While the turn runs it holds the
"composing" presence, the only progress affordance WhatsApp offers.

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
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.whatsapp.group_gate import SILENCE_SENTINEL

if TYPE_CHECKING:
    from kiro_crew.whatsapp.client import WhatsAppClient
    from kiro_crew.whatsapp.transport import WhatsAppTransport

logger = logging.getLogger(__name__)

# Refresh the composing indicator on this cadence; WhatsApp expires it ~10s.
_TYPING_REFRESH_S = 8.0

_ERROR_TEXT = "Something went wrong on my side. Please try again."


def _strip_options(text: str) -> str:
    """Drop the dashboard-only [OPTIONS: ...] trailer (no buttons here)."""
    return OPTIONS_RE_TRAILER.sub("", text).strip()


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
    ) -> None:
        super().__init__(capabilities)
        self._transport = transport
        self._client = client
        self._chat = chat_jid
        self._unprompted = unprompted
        self._session_key = session_key
        self._buf: list[str] = []
        self._started = False
        self._finalized = False
        self._typing_task: asyncio.Task[None] | None = None
        #: True when a rules-mode turn chose silence; the dispatcher reads it
        #: to skip cooldown recording and history persistence of the sentinel.
        self.suppressed = False

    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        self._typing_task = asyncio.create_task(self._hold_typing())

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)

    async def on_thinking(self, text: str) -> None:
        return None  # one bubble per turn; reasoning would double the noise

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        return None  # no in-place edit; typing presence is the progress cue

    async def on_prompt_choice(
        self, options: list[dict[str, Any]], request_id: str | int
    ) -> None:
        # Unreachable: this channel runs decider-less (deny-by-default) and
        # max_buttons=0 strips [OPTIONS:] trailers into plain text upstream.
        logger.debug("whatsapp: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        logger.debug("whatsapp: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        await self._stop_typing()
        ok = stop_reason != "error"
        body = self.text()
        if self._unprompted and (not body or body == SILENCE_SENTINEL):
            # The model chose silence (or produced nothing): deliver nothing.
            self.suppressed = True
            logger.debug("whatsapp: unprompted group turn suppressed")
            return
        if not body:
            body = "..." if ok else _ERROR_TEXT
        await self._send(body)

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
        reply as success; ``close()`` is the suppressing teardown path."""
        try:
            await self._transport.send_message(self._chat, body)
        except Exception:
            logger.warning("whatsapp: send failed, failing the turn", exc_info=True)
            raise

    async def _hold_typing(self) -> None:
        try:
            while True:
                await self._client.send_typing(self._chat, True)
                await asyncio.sleep(_TYPING_REFRESH_S)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("whatsapp: typing loop ended", exc_info=True)

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
