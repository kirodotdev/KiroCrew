"""Dispatch glue: WhatsAppTransport -> shared drive_turn -> WhatsAppRenderer.

Mirrors the weixin dispatcher, plus the WhatsApp group flow: an unprompted
rules-mode turn injects the group's rules and the silence contract, delivers
nothing when the model answers the sentinel, and only starts the group
cooldown after an actually-delivered unprompted reply.

Dependency direction is ``whatsapp -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.conversation import ConversationState
from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn, inbound_permitted
from kiro_crew.messaging.link import build_dm_session_key
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.whatsapp.commands import parse_command
from kiro_crew.whatsapp.group_gate import build_silence_contract
from kiro_crew.whatsapp.jids import is_group_jid
from kiro_crew.whatsapp.transport import WHATSAPP_CAPABILITIES, WhatsAppTransport
from kiro_crew.whatsapp.turn_renderer import WhatsAppRenderer

if TYPE_CHECKING:
    from kiro_crew.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "kirocrew"


class WhatsAppDispatcher:
    """Owns per-conversation state and drives turns for the WhatsApp channel."""

    def __init__(
        self,
        cfg: Any,
        sessions: Any,
        ctx_builder: Any,
        *,
        approval_mode: str,
        agent: str = "",
    ) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.approval_mode = approval_mode
        self.agent = agent
        self.client: "WhatsAppClient | None" = None
        self.transport: WhatsAppTransport | None = None
        self.conv_log: Any = None
        self._conv: ConversationState[str] = ConversationState()

    async def handle_message(self, inbound: InboundMessage) -> None:
        """Transport dispatch callback: one normalized inbound message."""
        if not await inbound_permitted("whatsapp"):
            return
        assert self.transport is not None
        verdict = self.transport.pending_verdicts.get(id(inbound))
        group = is_group_jid(inbound.conversation_id)
        may_steer = verdict.may_steer if verdict is not None else not group

        command = parse_command(inbound.text)
        if command is not None:
            if not may_steer:
                return
            await self._handle_command(inbound, command)
            return

        await self._drive(inbound, verdict)

    async def _handle_command(self, inbound: InboundMessage, command: str) -> None:
        scope = inbound.conversation_id
        if command == "new":
            self._conv.bump_gen(scope)
            await self._say(scope, "Started a fresh session.")
        elif command == "compact":
            self._conv.set_awaiting(scope)
            await self._say(scope, "I'll compact context on the next message.")

    async def _drive(self, inbound: InboundMessage, verdict: Any) -> None:
        assert self.transport is not None and self.client is not None
        transport = self.transport
        client = self.client
        scope = inbound.conversation_id
        session_key = self._session_key(scope)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(inbound, session_key)
            return
        m = self.cfg.messaging
        self._conv.maybe_rotate(
            scope,
            time.time(),
            idle_minutes=m.idle_reset_minutes,
            daily_reset_hour=m.daily_reset_hour,
        )
        session_key = self._session_key(scope)
        agent = self.agent or self.cfg.agent.default_agent or _DEFAULT_AGENT
        unprompted = bool(verdict is not None and verdict.unprompted)
        renderer = WhatsAppRenderer(
            transport,
            client,
            inbound.conversation_id,
            WHATSAPP_CAPABILITIES,
            unprompted=unprompted,
            session_key=session_key,
        )
        user_text = inbound.text
        if unprompted:
            user_text = build_silence_contract(verdict.rules) + "\n\n" + user_text
        await drive_turn(
            ChannelTurn(
                channel_type="whatsapp",
                session_key=session_key,
                conversation_id=f"whatsapp:{scope}",
                agent=agent,
                user_text=user_text,
                renderer=renderer,
                approval_mode=self.approval_mode,
                decider=None,
                persist=(
                    None
                    if unprompted
                    else lambda u, r, n: self._persist_turn(session_key, u, r, n)
                ),
                audit_caller=f"whatsapp:{inbound.user_id}",
            ),
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
        )
        if unprompted and not renderer.suppressed:
            transport.group_gate.record_unprompted_reply(scope)

    async def _handle_busy(self, inbound: InboundMessage, session_key: str) -> None:
        if not self.sessions.is_busy(session_key):
            await self.handle_message(inbound)
            return
        provider = self.sessions.get_provider(session_key)
        steer = getattr(provider, "steer", None)
        has_active = getattr(provider, "has_active_turn", None)
        live = has_active is None or bool(has_active())
        ok = bool(
            live
            and getattr(provider, "supports_steer", False)
            and steer is not None
            and await steer(inbound.text)
        )
        if ok:
            note = "Folded into the current reply."
        else:
            note = "Still working on the last message; please resend shortly."
        await self._say(inbound.conversation_id, note)

    def _persist_turn(
        self, session_key: str, user_text: str, reply_text: str, is_new: bool
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart)."""
        if self.conv_log is None:
            return
        self.conv_log.append(session_key, "user", user_text)
        if reply_text:
            self.conv_log.append(session_key, "assistant", reply_text)
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "WhatsApp"
            self.conv_log.set_title(session_key, title)

    async def _say(self, chat_jid: str, text: str) -> None:
        if self.transport is None:
            return
        try:
            await self.transport.send_message(chat_jid, text)
        except Exception:
            logger.warning("whatsapp: out-of-band send failed", exc_info=True)

    def _session_key(self, scope: str) -> str:
        from kiro_crew.messaging.link import CHAT_TYPE_DIRECT, CHAT_TYPE_FORUM

        agent = self.agent or self.cfg.agent.default_agent or _DEFAULT_AGENT
        chat_type = CHAT_TYPE_FORUM if is_group_jid(scope) else CHAT_TYPE_DIRECT
        return build_dm_session_key(
            "whatsapp",
            agent,
            scope,
            gen=self._conv.current_gen(scope),
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=chat_type,
        )
