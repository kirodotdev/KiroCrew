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
    ConfiguredChannelTarget,
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
    mid-turn steer can thread its continuation under the user's message.

    Telegram-local: the neutral ``InboundMessage`` stays unchanged; consumers
    read the id via ``getattr(msg, "message_id", 0)``.
    """

    message_id: int = 0
    # Originating chat type ("private" | "group" | "supergroup"). The forum
    # Topic id, when present, rides the base ``thread_id`` field. Consumers read
    # it via ``getattr(msg, "chat_type", "private")`` so the neutral message
    # stays channel-agnostic.
    chat_type: str = "private"


# A dispatch callback consumes a normalized, already-authorized message and
# drives a turn. The gateway supplies the real implementation.
DispatchFn = Callable[[InboundMessage], Awaitable[None]]

# Telegram's capabilities: edit-based streaming, a 4096-char cap (we chunk at
# 4000 for headroom), inline buttons, emoji reactions (setMessageReaction, used
# for steer-ack receipts), and threads=True because forum Topics ARE threads
# and this transport handles them end to end: send_message forwards
# message_thread_id, receive() populates InboundMessage.thread_id, and
# forum_gate_outcome authorizes on it. (This was previously declared False —
# wrongly; declarations must match the code, not the DM-only common case.)
# max_buttons=8 is Telegram's per-row limit; NOTE the renderer does not yet
# apply it (see the capability ledger — the field is aspirational).
TELEGRAM_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    reactions=True,  # setMessageReaction — used for the steer-ack receipt
    files_inbound=False,  # photos/stickers are dropped in receive(), matching prior behavior
    files_outbound=False,
    rich_blocks=False,
    threads=True,
    max_message_chars=TELEGRAM_CHUNK_LIMIT,
    max_buttons=8,
    supports_proactive_send=True,
)


def forum_gate_outcome(
    chat_type: str,
    chat_id: int,
    message_thread_id: int | None,
    *,
    allow_forum: bool,
    allowed_forum_chat_ids: Iterable[int],
) -> str | None:
    """Fail-closed forum authZ predicate shared by the inbound and callback paths.

    Returns ``None`` when the chat is authorized to drive a turn/callback, else
    the SEL audit ``outcome`` string the caller logs before dropping. Only a
    message inside a **real forum Topic** (``supergroup`` AND a truthy
    ``message_thread_id``) of an allow-listed chat is authorized:
      * ``private``            -> ``None`` (1:1 DM, always allowed past auth).
      * ``supergroup`` + Topic  -> ``None`` ONLY when forum topics are enabled
        AND the chat_id is allow-listed; otherwise ``"denied_forum_not_allowed"``.
      * everything else -> ``"denied_non_private_chat"``. This DENIES ordinary
        groups (which cannot have Topics) AND the supergroup **General** chat
        (no ``message_thread_id``) -- serving those would post to the whole
        group's readable main chat (non-private exposure) and exceeds the
        "forum topics" scope.

    One predicate, two call sites (``TelegramTransport.receive`` +
    ``TelegramDispatcher.on_callback``) so the security decision can never drift
    between the inbound and callback paths. Each site passes its OWN allow-list
    source -- the transport freezes it at construction, the dispatcher reads live
    cfg -- and that difference is deliberate (see the call sites).
    """
    if chat_type == "private":
        return None
    if chat_type == "supergroup" and message_thread_id:
        if allow_forum and int(chat_id) in set(allowed_forum_chat_ids):
            return None
        return "denied_forum_not_allowed"
    return "denied_non_private_chat"


class TelegramTransport(MessagingTransport):
    """Concrete Telegram transport over the low-level ``TelegramClient``."""

    channel_type = "telegram"

    def __init__(
        self,
        client: TelegramClient,
        *,
        allowed_user_ids: Iterable[int] = (),
        allow_forum: bool = False,
        allowed_forum_chat_ids: Iterable[int] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the allow-list as strings (to match
        # InboundMessage.user_id) so it can't mutate under an in-flight decision.
        self._allowed: frozenset[str] = frozenset(str(u) for u in allowed_user_ids)
        # Forum-topic gate (fail closed): serve supergroup forum Topics only when
        # explicitly enabled AND the supergroup's chat_id is allow-listed. The
        # chat_ids are numeric ints (matched against the raw ``TelegramInbound``).
        self._allow_forum = bool(allow_forum)
        self._allowed_forum_chat_ids: frozenset[int] = frozenset(
            int(c) for c in allowed_forum_chat_ids
        )
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
        mid = await self._client.send_message(
            int(conversation_id),
            content,
            message_thread_id=int(thread_id) if thread_id else None,
        )
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

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        return [
            ConfiguredChannelTarget(f"user:{user_id}", f"Telegram DM · {user_id}")
            for user_id in sorted(self._allowed)
        ]

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, separator, value = target_id.partition(":")
        if kind != "user" or not separator or value not in self._allowed:
            return None
        return await self.resolve_conversation(value), None

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
        (photos/stickers) are dropped.
        """
        if not isinstance(raw_envelope, TelegramInbound):
            return
        inbound = raw_envelope
        if not inbound.text:
            return
        # Chat-type gate (fail closed). A bot added to a group receives every
        # message and its replies land in that chat, so we serve ONLY a real
        # forum Topic (supergroup + message_thread_id) whose chat_id is
        # allow-listed; ordinary groups, the supergroup General chat (no
        # thread), channels and other types are always denied. The decision
        # lives in the shared ``forum_gate_outcome`` predicate so it can't drift
        # from the callback path (on_callback). The allow-list source here is
        # the construction-time FROZEN copy (self._allow_forum /
        # self._allowed_forum_chat_ids) -- DELIBERATE, so the gate can't mutate
        # under an in-flight decision (mirrors the frozen user-allowlist); the
        # dispatcher's on_callback reads live cfg instead.
        outcome = forum_gate_outcome(
            inbound.chat_type,
            inbound.chat_id,
            inbound.message_thread_id,
            allow_forum=self._allow_forum,
            allowed_forum_chat_ids=self._allowed_forum_chat_ids,
        )
        if outcome is not None:
            sel().log_api_access(
                caller=str(inbound.user_id) or "unknown",
                operation="telegram_transport.receive",
                outcome=outcome,
                source="telegram",
            )
            return
        msg = TelegramInboundMessage(
            channel_type="telegram",
            user_id=str(inbound.user_id),
            conversation_id=str(inbound.chat_id),
            text=inbound.text,
            thread_id=(str(inbound.message_thread_id) if inbound.message_thread_id else None),
            message_id=inbound.message_id,
            chat_type=inbound.chat_type,
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(msg)
