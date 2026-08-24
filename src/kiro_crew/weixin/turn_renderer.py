"""Turn rendering for the Weixin (iLink) channel.

iLink has no message-edit / streaming primitive: every ``sendmessage`` creates a
NEW chat bubble. So unlike the WeCom renderer (which replaces one bubble via WS
frames), this renderer BUFFERS the whole turn and emits it once on ``on_done``,
split into chunks by :mod:`kiro_crew.weixin.renderer_chunks`. While the turn
runs it holds the native "typing…" indicator on, which is the only progress
affordance iLink offers.

Dependency direction is ``weixin -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.weixin.client import TYPING_START, TYPING_STOP
from kiro_crew.weixin.renderer import render_chunks

if TYPE_CHECKING:
    from kiro_crew.weixin.client import ContextTokenStore, TypingTicketCache, WeixinClient

logger = logging.getLogger(__name__)

# Delay between chunk sends — iLink drops messages sent too fast.
_CHUNK_DELAY_S = 0.3
# Refresh the typing indicator on this cadence; iLink expires it on its own.
_TYPING_REFRESH_S = 8.0

# Trailing "[OPTIONS: a | b | c]" chip trailer (a dashboard convention iLink
# can't render as tappable chips). Matched only at the very END of the message
# via the canonical trailer parser, defined once in constants.py and shared with
# the Slack/dashboard/Discord/Telegram/WeCom/Webex/Teams surfaces so the
# ReDoS-hardened grammar can never drift. A hand-rolled MULTILINE|DOTALL variant
# is unsafe here: `.*?` spans newlines, so a quoted "[OPTIONS:" earlier in a
# reply could match a "]" far below it and silently delete everything between.
_OPTIONS_RE = OPTIONS_RE_TRAILER

_ERROR_TEXT = "⚠️ 出错了，请重试"


def _strip_options(text: str) -> str:
    """Drop the dashboard-only [OPTIONS: …] affordance — iLink has no buttons."""
    return _OPTIONS_RE.sub("", text).strip()


class WeixinRenderer(Renderer):
    """Buffers a turn and emits it as one (or more) iLink messages on completion.

    ``on_text_chunk`` accumulates; nothing is sent until ``on_done`` because
    iLink cannot edit a message in place. A background task keeps the native
    typing indicator alive so the user sees the agent working.
    """

    channel_type = "weixin"

    def __init__(
        self,
        client: "WeixinClient",
        to_user_id: str,
        capabilities: TransportCapabilities,
        *,
        ctx_store: "ContextTokenStore",
        account_id: str,
        typing_cache: "TypingTicketCache | None" = None,
        session_key: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._to = to_user_id
        self._ctx = ctx_store
        self._account_id = account_id
        self._typing = typing_cache
        self._session_key = session_key
        self._buf: list[str] = []
        self._started = False
        self._finalized = False
        self._typing_task: asyncio.Task[None] | None = None

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        self._typing_task = asyncio.create_task(self._hold_typing())

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)

    async def on_thinking(self, text: str) -> None:
        # iLink surfaces one bubble per turn; reasoning would double the noise.
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        # No in-place edit -> a per-tool bubble would spam the chat. The typing
        # indicator is the progress affordance.
        return None

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
    ) -> None:
        # iLink has no interactive buttons. The driver only dispatches
        # prompt_choice for INTERACTIVE + a decider, and this channel runs
        # decider-less (deny-by-default), so this is unreachable — kept as a
        # safe no-op to satisfy the Renderer contract.
        logger.debug("weixin: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        # Mid-turn text would land as its own bubble; the dispatcher surfaces
        # threshold notices post-turn instead.
        logger.debug("weixin: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        await self._stop_typing()
        ok = stop_reason != "error"
        body = self.text()
        if not body:
            body = "…" if ok else _ERROR_TEXT
        await self._send(body)

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done.

        Runs from the dispatcher's ``finally``, so a delivery failure here is
        logged and suppressed — raising would mask whatever unwound the turn.
        """
        if not self._finalized:
            try:
                await self.on_done(stop_reason="error")
            except Exception:
                logger.warning("weixin: final send failed during teardown", exc_info=True)
        await self._stop_typing()

    # -- helpers ------------------------------------------------------------
    def text(self) -> str:
        """The turn's visible answer (OPTIONS stripped). Also persisted to history."""
        return _strip_options("".join(self._buf).strip())

    async def _send(self, body: str) -> None:
        """Deliver the answer as one or more chat messages.

        Raises on failure. A swallowed error here would let the dispatcher record
        and persist an undelivered reply as a successful turn, so the exception
        must reach ``handle_message``'s except branch (which calls
        ``record_failure``). ``close()`` is the teardown path and suppresses it,
        since by then the turn is already being unwound.
        """
        ctx_token = self._ctx.get(self._account_id, self._to)
        chunks = render_chunks(body, self.capabilities.max_message_chars)
        for i, part in enumerate(chunks):
            if i:
                await asyncio.sleep(_CHUNK_DELAY_S)
            try:
                await self._client.send_message(
                    to=self._to,
                    text=part,
                    context_token=ctx_token,
                    client_id=uuid.uuid4().hex,
                )
            except Exception:
                logger.warning(
                    "weixin: send failed (chunk %d/%d) — failing the turn",
                    i + 1,
                    len(chunks),
                    exc_info=True,
                )
                raise

    async def _hold_typing(self) -> None:
        """Keep the native typing indicator alive for the duration of the turn."""
        try:
            while True:
                await self._typing_signal(TYPING_START)
                await asyncio.sleep(_TYPING_REFRESH_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("weixin: typing loop ended", exc_info=True)

    async def _stop_typing(self) -> None:
        task = self._typing_task
        self._typing_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._typing_signal(TYPING_STOP)

    async def _typing_signal(self, status: int) -> None:
        """Fetch (and cache) a typing ticket, then push the indicator state."""
        try:
            ticket = self._typing.get(self._to) if self._typing else None
            if not ticket:
                cfg = await self._client.get_config(
                    user_id=self._to,
                    context_token=self._ctx.get(self._account_id, self._to),
                )
                ticket = str(cfg.get("typing_ticket") or "")
                if ticket and self._typing:
                    self._typing.set(self._to, ticket)
            if not ticket:
                return
            await self._client.send_typing(
                to_user_id=self._to, typing_ticket=ticket, status=status
            )
        except Exception:
            # Typing is cosmetic — never let it break a turn.
            logger.debug("weixin: typing signal failed", exc_info=True)
