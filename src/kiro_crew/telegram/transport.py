"""Layer 1 -- Telegram as a concrete :class:`MessagingTransport`.

Wraps the low-level :class:`TelegramClient` (Bot API long-polling + send/edit)
in the channel-neutral transport contract, so the Telegram channel rides the
shared ``TurnDriver`` (credential/exfil redaction + tool-approval ladder + SEL
audit) instead of a hand-rolled turn loop.

Dependency direction is ``telegram -> messaging`` (allowed); the neutral
``messaging`` package never imports ``telegram``.

Security: :meth:`authorize` is **deny-by-default** and owner-only. A Telegram
bot is globally reachable by @username, so an empty ``allowed_user_ids`` MUST
authorize nobody (fail closed), never everybody.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kiro_crew.messaging.transport import (
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.telegram.client import (
    TELEGRAM_CHUNK_LIMIT,
    TelegramClient,
    TelegramInbound,
)


@dataclass
class TelegramInboundMessage(InboundMessage):
    """Inbound message enriched with the raw Telegram ``message_id`` so a
    mid-turn steer can thread its continuation under the user's message (M1).

    Telegram-local: the neutral ``InboundMessage`` stays unchanged; consumers
    read the id via ``getattr(msg, "message_id", 0)``.
    """

    message_id: int = 0


# A dispatch callback consumes a normalized, already-authorized message and
# drives a turn. The gateway supplies the real implementation.
DispatchFn = Callable[[InboundMessage], Awaitable[None]]

# Telegram's capabilities: edit-based streaming, a 4096-char cap (we chunk at
# 4000 for headroom), ~8 inline buttons/row, emoji reactions (setMessageReaction,
# used for steer-ack receipts), and no threads in a bot DM. Single source of
# truth for the renderer's degradation decisions.
TELEGRAM_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    reactions=True,  # setMessageReaction — used for the steer-ack receipt
    files=False,
    rich_blocks=False,
    threads=False,
    max_message_chars=TELEGRAM_CHUNK_LIMIT,
    max_buttons=8,
    supports_proactive_send=True,
)


class TelegramTransport(MessagingTransport):
    """Concrete Telegram transport over the low-level ``TelegramClient``."""

    channel_type = "telegram"

    def __init__(
        self,
        client: TelegramClient,
        *,
        allowed_user_ids: Iterable[int] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the allow-list as strings (to match
        # InboundMessage.user_id) so it can't mutate under an in-flight decision.
        self._allowed: frozenset[str] = frozenset(str(u) for u in allowed_user_ids)
        self._dispatch = dispatch
        self.capabilities = TELEGRAM_CAPABILITIES

    @property
    def client(self) -> TelegramClient:
        """The underlying Bot API client (held + exposed, not hidden)."""
        return self._client

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        mid = await self._client.send_message(int(conversation_id), content)
        return str(mid or "")

    async def resolve_conversation(self, user_id: str) -> str:
        # In a Telegram private chat the chat_id equals the user_id.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # The Bot API cannot page arbitrary DM history; sessions persist via
        # conversation_log instead.
        return []

    # -- Lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        await self._client.start()

    async def disconnect(self) -> None:
        await self._client.close()

    # -- Inbound adapter ----------------------------------------------------
    def authorize(self, msg: InboundMessage) -> bool:
        """Owner-only, deny-by-default. Empty allow-list authorizes nobody."""
        allowed = bool(msg.user_id) and msg.user_id in self._allowed
        if not allowed:
            # Audit ALL denials (including empty/missing user_id) so
            # deny-by-default is observable, mirroring SlackTransport.
            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="telegram_transport.authorize",
                outcome="denied",
                source="telegram",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize -> authorize -> dispatch.

        The low-level client long-polls and normalizes updates into
        ``TelegramInbound``; this adapter maps that onto the neutral
        ``InboundMessage``, enforces deny-by-default auth, and hands an
        authorized message to the turn dispatcher. Non-text updates
        (photos/stickers) are dropped, matching prior behavior.
        """
        if not isinstance(raw_envelope, TelegramInbound):
            return
        inbound = raw_envelope
        if not inbound.text:
            return
        # Private-chat-only, fail closed. A bot added to a group receives
        # messages; even from an allow-listed user, running a turn would reply
        # in the group (conversation_id == group chat_id at renderer time),
        # exposing tool output to non-authorized members. resolve_conversation
        # also assumes chat_id == user_id, which only holds in private chats.
        # Deny anything not explicitly a private chat and audit it.
        if inbound.chat_type != "private":
            sel().log_api_access(
                caller=str(inbound.user_id) or "unknown",
                operation="telegram_transport.receive",
                outcome="denied_non_private_chat",
                source="telegram",
            )
            return
        msg = TelegramInboundMessage(
            channel_type="telegram",
            user_id=str(inbound.user_id),
            conversation_id=str(inbound.chat_id),
            text=inbound.text,
            thread_id=None,
            message_id=inbound.message_id,
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(msg)
