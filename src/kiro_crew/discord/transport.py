"""Layer 1 -- Discord as a concrete :class:`MessagingTransport`.

Wraps the low-level :class:`DiscordClient` (Gateway WebSocket + REST) in the
channel-neutral transport contract, so the Discord channel rides the shared
``TurnDriver`` (credential/exfil redaction + tool-approval ladder + SEL audit)
instead of a hand-rolled turn loop.

Dependency direction is ``discord -> messaging`` (allowed); the neutral
``messaging`` package never imports ``discord``.

Security: :meth:`authorize` is **deny-by-default**. A Discord bot can be DM'd
by anyone who shares a server with it, so an empty ``allowed_user_ids`` MUST
authorize nobody. Guild traffic additionally requires either an exact thread-ID
allow-list match or an approved channel whose message can be promoted into a
new thread; turns never run directly in a normal guild channel.
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
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.messaging.tables import TABLE_POLICY_AUTO
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
# 1900 for headroom), up to 5 buttons per action row, emoji reactions (steer-ack
# receipts and the phase ladder), native markdown rendering, and allow-listed server
# threads (represented by Discord as channels). Single source of truth for the
# renderer's degradation decisions.
DISCORD_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    # Two readers: the mid-turn steer-ack receipt (add_reaction on the user's own
    # message) and the renderer's phase ladder, which checks this flag before it
    # arms. A capability is a claim other code trusts, so both are named here.
    reactions=True,
    # Both directions are wired: attachments are ingested
    # (discord/attachments.py), and a sealed segment's local images are uploaded
    # as multipart attachments (renderer -> client.send_message_with_files). The
    # renderer READS files_outbound before extracting, so this flag is the switch
    # rather than a description of one.
    files_inbound=True,
    files_outbound=True,
    rich_blocks=False,
    threads=True,
    # Discord renders pipe tables literally. ``auto`` keeps grids only when
    # they fit a phone-sized monospace viewport and cards wider tables.
    table_mode=TABLE_POLICY_AUTO,
    max_message_chars=DISCORD_CHUNK_LIMIT,
    # 25 = TOTAL interactive choices (5 buttons/row x 5 action rows -- the
    # platform max the renderer actually ships). The previous 5 was the
    # per-row layout number, not a total. Enforced via apply_options_cap in
    # the renderer; overflow degrades to a numbered text list.
    max_buttons=25,
    supports_proactive_send=True,
    # The one transport whose inbound path resolves the mirror binding: a message
    # in a bound conversation routes to the owning session via
    # `DiscordSessionResume.resumed_session`, so a dashboard connect here can
    # honestly claim `accepts_inbound`.
    supports_session_resume=True,
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
        allowed_channel_ids: Iterable[str] = (),
        auto_thread: bool = True,
        on_thread_created: Callable[[str], None] | None = None,
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze both allow-lists as snowflake strings so they
        # cannot mutate under an in-flight authorization decision.
        self._allowed: frozenset[str] = frozenset(str(u) for u in allowed_user_ids)
        # Mutable: an approved user's message in an allowed channel can promote
        # itself into a brand-new thread at runtime (see ``receive`` below), and
        # that thread must immediately become valid for the user's own follow-up
        # replies -- not just for button interactions (tracked separately on the
        # dispatcher's own allow-set). A frozenset here would silently strand
        # every reply the user sends into the thread the bot just created.
        self._allowed_threads: set[str] = {str(t) for t in allowed_thread_ids}
        self._allowed_channels: frozenset[str] = frozenset(str(c) for c in allowed_channel_ids)
        self._auto_thread = auto_thread
        self._on_thread_created = on_thread_created
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

    async def send_document(
        self,
        conversation_id: str,
        file: OutboundFile,
        *,
        caption: str = "",
        thread_id: str | None = None,
    ) -> str:
        """Send one validated file, keeping its admitted name. Returns the message id.

        The transport-level upload verb, and the name-preserving counterpart of the
        renderer's extraction upload (``DiscordClient.send_message_with_files``),
        whose sanitizer is aimed at LLM-authored reference paths and would deliver
        ``report.pdf`` as ``report.bin``. A caller here has already gated the name
        (``file_send``), so the real basename is pinned onto the multipart part.
        ``file`` carries validated bytes (the ``OutboundFile`` contract — the path
        is provenance, never re-opened).

        ``thread_id``, when present, IS the destination: a Discord thread's
        snowflake is its channel id, which is why the persisted link is built as
        ``ChannelLink("discord", channel_id=...)`` with no thread id at all (see
        :meth:`may_send_to`). The parameter exists for cross-transport parity, and
        honouring it costs nothing because the value it would carry is a channel.
        """
        mid = await self._client.send_document(
            thread_id or conversation_id,
            file,
            caption=caption or None,
        )
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

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Re-check the roster the ROUTE belongs to. Fails closed on both.

        Discord keeps two rosters because it has two audiences, so this dispatches
        on the route rather than testing one id against the wrong set.

        A **thread** route is recognised by its conversation id being in
        ``_allowed_threads``, the same set ``receive`` gates inbound on. Matched on
        the conversation id and NOT on ``thread_id``: a Discord thread's snowflake IS
        its channel id, and the persisted link is built as
        ``ChannelLink("discord", channel_id=...)`` with no thread id at all, so a
        check keyed on ``thread_id`` never fires and every thread would fall to the
        DM arm and be refused for want of a principal. Snowflakes are unique, so a
        DM channel id cannot collide into this set.

        Consulting the thread set keeps outbound exactly as tight as inbound, which
        also settles the auto-created case: those ids are registered in memory only,
        so after a restart such a thread can no longer drive a turn either, and
        continuing to post into it would make outbound the more permissive of the two.
        A thread REMOVED from the roster falls through to the DM arm, where a forum
        session key names no principal, so revocation still refuses it.

        A **DM** route is checked against ``_allowed`` via *principal*, and refuses
        when there is none. The conversation id cannot answer that one: a DM link
        persists the channel id returned by ``create_dm_channel``, which is
        unrelated to the user snowflake the roster holds, and re-deriving the
        pairing is a POST a synchronous per-send seam cannot make. So with no
        principal there is nothing left to consult, and an unidentifiable DM
        recipient is exactly the case that must not be waved through: this is a
        network egress boundary, and the caller audits the refusal.

        The one route that reaches that refusal is a ``unified`` DM bucket, whose
        key names no peer by design. Refusing costs an unattended notice there and
        is the correct trade: that bucket deliberately collapses SEVERAL peers into
        one session, so nothing available to this seam establishes which of them the
        link currently points at. Sessions under the default ``per-channel-peer``
        scope carry their peer in the key and are unaffected. Serving it needs a
        ``dm_channel_id -> user_id`` pairing persisted when the DM is opened, which
        is a Discord-owned schema change.
        """
        if not conversation_id:
            return False
        if conversation_id in self._allowed_threads:
            return True
        return bool(principal) and principal in self._allowed

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
        conversation_id = inbound.channel_id
        if inbound.guild_id:
            # Discord's guild intents deliver every visible channel message.
            # Unrelated chatter is expected background traffic, not a security
            # event: discard it silently unless an approved user tried to use
            # an unapproved thread. Messages in configured threads still pass
            # through the normal user authorization audit below.
            if inbound.channel_id in self._allowed_channels:
                if inbound.user_id not in self._allowed:
                    # Reuse the normal denial audit without creating a shared
                    # channel thread for an unauthorized sender.
                    self.authorize(
                        DiscordInboundMessage(
                            channel_type="discord",
                            user_id=inbound.user_id,
                            conversation_id=inbound.channel_id,
                            text=inbound.text,
                        )
                    )
                    return
                if not self._auto_thread or not inbound.message_id:
                    return
                # Re-check the same runtime channels-governance gate that
                # ``DiscordDispatcher.handle_message`` enforces, but *before* the
                # REST call below: creating the thread is itself a visible,
                # irreversible side effect (a real public thread appears in the
                # server), so a policy that denies Discord inbound after connect
                # must stop it from happening at all -- not just stop the turn
                # that would have followed it.
                if not await channel_inbound_permitted("discord"):
                    sel().log_api_access(
                        caller=inbound.user_id,
                        operation="discord_transport.receive",
                        outcome="denied_by_channels_governance",
                        source="discord",
                    )
                    return
                title = " ".join(inbound.text.split())[:90] or "Kiro Crew"
                created = await self._client.create_thread_from_message(
                    inbound.channel_id, inbound.message_id, title
                )
                if not created:
                    sel().log_api_access(
                        caller=inbound.user_id,
                        operation="discord_transport.receive",
                        outcome="thread_create_failed",
                        source="discord",
                    )
                    return
                thread_id = created
                conversation_id = created
                # Authorize the thread transport-side FIRST: this is the set
                # ``receive`` itself checks for every subsequent message
                # (`elif inbound.channel_id not in self._allowed_threads` below).
                # The dispatcher's own copy (button interactions) is updated via
                # the callback right after.
                #
                # Audited because this is a GRANT, not a denial: a new authorized
                # disclosure boundary appears at runtime, readable by every member
                # who can view the thread, and every refusal on this path already
                # leaves a record. Without it the audit log shows the turns that
                # ran in the thread but never the decision that admitted it, so
                # reconstructing which surfaces the agent was reachable in means
                # inferring it from traffic.
                #
                # The set is deliberately unbounded: each entry is a thread an
                # ALREADY-approved user created, and evicting one would silently
                # stop answering in a conversation they are still holding: worse
                # than the memory, which is bounded in practice by that user's
                # own thread count.
                self._allowed_threads.add(created)
                sel().log_api_access(
                    caller=inbound.user_id,
                    operation="discord_transport.auto_thread",
                    outcome="thread_authorized",
                    source="discord",
                    resources=f"channel={inbound.channel_id},thread={created}",
                )
                if self._on_thread_created is not None:
                    self._on_thread_created(created)
            elif inbound.channel_id not in self._allowed_threads:
                if inbound.user_id in self._allowed:
                    sel().log_api_access(
                        caller=inbound.user_id,
                        operation="discord_transport.receive",
                        outcome="denied_unapproved_thread",
                        source="discord",
                    )
                return
            else:
                thread_id = inbound.channel_id
        msg = DiscordInboundMessage(
            channel_type="discord",
            user_id=inbound.user_id,
            conversation_id=conversation_id,
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
