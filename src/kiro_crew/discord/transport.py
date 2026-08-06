"""Layer 1 -- Discord as a concrete :class:`MessagingTransport`.

Wraps the low-level :class:`DiscordClient` (Gateway WebSocket + REST) in the
channel-neutral transport contract, so the Discord channel rides the shared
``TurnDriver`` (credential/exfil redaction + tool-approval ladder + SEL audit)
instead of a hand-rolled turn loop.

Dependency direction is ``discord -> messaging`` (allowed); the neutral
``messaging`` package never imports ``discord``.

Security: :meth:`authorize` is **deny-by-default**. A Discord bot can be DM'd
by anyone who shares a server with it, so an empty ``allowed_user_ids`` MUST
authorize nobody. Guild traffic additionally requires an exact thread-ID
allow-list match and a Discord-confirmed thread channel type; normal guild
channels are always denied.
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
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel


@dataclass
class DiscordInboundMessage(InboundMessage):
    """Inbound message enriched with the raw Discord message id so a mid-turn
    steer can ack via reaction on the user's message (mirrors Telegram).

    Discord-local: the neutral ``InboundMessage`` stays unchanged; consumers
    read the id via ``getattr(msg, "message_id", "")``.
    """

    message_id: str = ""


# A dispatch callback consumes a normalized, already-authorized message and
# drives a turn. The gateway supplies the real implementation.
DispatchFn = Callable[[InboundMessage], Awaitable[None]]

# Discord's capabilities: edit-based streaming, a 2000-char cap (we chunk at
# 1900 for headroom), up to 5 buttons per action row, emoji reactions (used
# for steer-ack receipts), native markdown rendering, and allow-listed server
# threads (represented by Discord as channels). Single source of truth for the
# renderer's degradation decisions.
DISCORD_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    reactions=True,  # add_reaction — used for the steer-ack receipt
    # Inbound only: attachments are ingested (discord/attachments.py), but no
    # upload path exists — file_send reaches Slack alone. The old single
    # files=True conflated the two directions and over-promised outbound.
    files_inbound=True,
    files_outbound=False,
    rich_blocks=False,
    threads=True,
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
        allowed_thread_ids: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze both allow-lists as snowflake strings so they
        # cannot mutate under an in-flight authorization decision.
        self._allowed: frozenset[str] = frozenset(str(u) for u in allowed_user_ids)
        self._allowed_threads: frozenset[str] = frozenset(str(t) for t in allowed_thread_ids)
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

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets = [
            ConfiguredChannelTarget(f"user:{user_id}", f"Discord DM · {user_id}")
            for user_id in sorted(self._allowed)
        ]
        targets.extend(
            ConfiguredChannelTarget(f"thread:{thread_id}", f"Discord thread · {thread_id}")
            for thread_id in sorted(self._allowed_threads)
        )
        return targets

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, separator, value = target_id.partition(":")
        if not separator or not value:
            return None
        if kind == "user" and value in self._allowed:
            return await self.resolve_conversation(value), None
        if kind == "thread" and value in self._allowed_threads:
            # Keep outbound dashboard links on the same disclosure boundary as
            # inbound guild traffic: an allow-listed snowflake is not enough
            # if Discord reports that it is a normal shared channel.
            if await self._client.is_thread_channel(value):
                return value, None
        return None

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
        authorized message to the turn dispatcher. Attachment-only messages
        continue through the same authorized path; sticker-only messages do not.
        """
        if not isinstance(raw_envelope, DiscordInbound):
            return
        inbound = raw_envelope
        if not inbound.text and not inbound.attachments:
            return
        thread_id: str | None = None
        if inbound.guild_id:
            # Discord's guild intents deliver every visible channel message.
            # Unrelated chatter is expected background traffic, not a security
            # event: discard it silently unless an approved user tried to use
            # an unapproved thread. Messages in configured threads still pass
            # through the normal user authorization audit below.
            if inbound.channel_id not in self._allowed_threads:
                if inbound.user_id in self._allowed:
                    sel().log_api_access(
                        caller=inbound.user_id,
                        operation="discord_transport.receive",
                        outcome="denied_unapproved_thread",
                        source="discord",
                    )
                return
            thread_id = inbound.channel_id
        msg = DiscordInboundMessage(
            channel_type="discord",
            user_id=inbound.user_id,
            conversation_id=inbound.channel_id,
            text=inbound.text,
            thread_id=thread_id,
            message_id=inbound.message_id,
            attachments=list(inbound.attachments),
        )
        if not self.authorize(msg):
            return
        if thread_id and not await self._client.is_thread_channel(thread_id):
            sel().log_api_access(
                caller=inbound.user_id,
                operation="discord_transport.receive",
                outcome="denied_non_thread_channel",
                source="discord",
            )
            return
        if self._dispatch is not None:
            await self._dispatch(msg)
