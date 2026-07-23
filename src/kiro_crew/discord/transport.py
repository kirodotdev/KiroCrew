"""Layer 1 -- Discord as a concrete :class:`MessagingTransport`.

Wraps the low-level :class:`DiscordClient` (Gateway WebSocket + REST) in the
channel-neutral transport contract, so the Discord channel rides the shared
``TurnDriver`` (credential/exfil redaction + tool-approval ladder + SEL audit)
instead of a hand-rolled turn loop.

Dependency direction is ``discord -> messaging`` (allowed); the neutral
``messaging`` package never imports ``discord``.

Security: :meth:`authorize` is **deny-by-default** and owner-only. A Discord
bot can be DM'd by anyone who shares a server with it, so an empty
``allowed_user_ids`` MUST authorize nobody (fail closed), never everybody.
DM-only: guild messages are denied outright (mirrors Telegram's
private-chat-only rule) so tool output can never leak into a shared channel.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kiro_crew.discord.client import (
    DISCORD_CHUNK_LIMIT,
    DiscordClient,
    DiscordInbound,
)
from kiro_crew.messaging.transport import (
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel


@dataclass
class DiscordInboundMessage(InboundMessage):
    """Inbound message enriched with the raw Discord message id so a mid-turn
    steer can ack via reaction on the user's message (mirrors Telegram's M1).

    Discord-local: the neutral ``InboundMessage`` stays unchanged; consumers
    read the id via ``getattr(msg, "message_id", "")``.
    """

    message_id: str = ""


# A dispatch callback consumes a normalized, already-authorized message and
# drives a turn. The gateway supplies the real implementation.
DispatchFn = Callable[[InboundMessage], Awaitable[None]]

# Discord's capabilities: edit-based streaming, a 2000-char cap (we chunk at
# 1900 for headroom), up to 5 buttons per action row, emoji reactions (used
# for steer-ack receipts), native markdown rendering, and no threads in a DM.
# Single source of truth for the renderer's degradation decisions.
DISCORD_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    reactions=True,  # add_reaction — used for the steer-ack receipt
    files=False,
    rich_blocks=False,
    threads=False,
    max_message_chars=DISCORD_CHUNK_LIMIT,
    max_buttons=5,  # per action row (max 5 rows -> 25 total)
    supports_proactive_send=True,
)


class DiscordTransport(MessagingTransport):
    """Concrete Discord transport over the low-level ``DiscordClient``."""

    channel_type = "discord"

    def __init__(
        self,
        client: DiscordClient,
        *,
        allowed_user_ids: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the allow-list as strings (Discord snowflake
        # ids) so it can't mutate under an in-flight decision.
        self._allowed: frozenset[str] = frozenset(str(u) for u in allowed_user_ids)
        self._dispatch = dispatch
        self.capabilities = DISCORD_CAPABILITIES

    @property
    def client(self) -> DiscordClient:
        """The underlying Gateway/REST client (held + exposed, not hidden)."""
        return self._client

    @property
    def dispatcher(self) -> Any:
        """The ``DiscordDispatcher`` whose bound ``handle_message`` was wired
        as ``dispatch``, or ``None`` when unwired (tests) or wired to a plain
        function.

        Public surface for out-of-band injectors (AutoNudge fire path, the
        REST loop-create endpoint): they need the dispatcher's authorization
        and session-key contract (``is_authorized`` / ``current_session_key``
        / ``handle_message``), and this property is the one sanctioned way to
        reach it — reaching into ``_dispatch`` from outside this class is a
        rename-away from silently killing active loops.
        """
        return getattr(self._dispatch, "__self__", None)

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        mid = await self._client.send_message(conversation_id, content)
        return str(mid or "")

    async def resolve_conversation(self, user_id: str) -> str:
        # Proactive sends need a DM channel; the client's create_dm_channel
        # POSTs /users/@me/channels to create (or return) it for a user id.
        return await self._client.create_dm_channel(user_id)

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # Sessions persist via conversation_log instead (mirrors Telegram).
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
            # deny-by-default is observable, mirroring TelegramTransport.
            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="discord_transport.authorize",
                outcome="denied",
                source="discord",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize -> authorize -> dispatch.

        The low-level client's Gateway loop normalizes MESSAGE_CREATE into
        ``DiscordInbound``; this adapter maps that onto the neutral
        ``InboundMessage``, enforces deny-by-default auth, and hands an
        authorized message to the turn dispatcher. Non-text messages
        (attachments/stickers only) are dropped.
        """
        if not isinstance(raw_envelope, DiscordInbound):
            return
        inbound = raw_envelope
        if not inbound.text:
            return
        # DM-only, fail closed. A bot in a server receives guild messages;
        # even from an allow-listed user, running a turn would reply in the
        # guild channel, exposing tool output to non-authorized members. Deny
        # anything carrying a guild_id and audit it (mirrors Telegram's
        # private-chat-only rule).
        if inbound.guild_id:
            sel().log_api_access(
                caller=inbound.user_id or "unknown",
                operation="discord_transport.receive",
                outcome="denied_guild_message",
                source="discord",
            )
            return
        msg = DiscordInboundMessage(
            channel_type="discord",
            user_id=inbound.user_id,
            conversation_id=inbound.channel_id,
            text=inbound.text,
            thread_id=None,
            message_id=inbound.message_id,
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(msg)
