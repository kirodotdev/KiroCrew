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

from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.messaging.tables import TABLE_POLICY_NATIVE
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
    # The sender's Telegram @handle, already narrowed by ``prompt_safe_handle``
    # so it is safe to interpolate into the prompt. Empty when the account has no
    # username, which is common — a display name is a nice-to-have, never a
    # precondition for serving the turn.
    username: str = ""
    # Sender of the message this one replies to, or 0. Replying to the bot is how a
    # Telegram participant addresses it without typing its @handle, so the
    # activation gate reads this; the comparison against the bot's own id happens in
    # the dispatcher, the only layer that knows it.
    reply_to_user_id: int = 0
    # Handles Telegram marked as ``mention`` entities, lowercased and without the ``@``, plus
    # whether the update carried an entity list at all. The activation gate reads
    # these instead of scanning the text: Telegram classifies a handle inside a URL
    # as a ``url`` entity rather than a ``mention``, and a text scan cannot tell the
    # two apart. ``has_entities`` keeps "nobody was mentioned" distinguishable from
    # "this message was synthesized and never had entities".
    mentions: tuple[str, ...] = ()
    has_entities: bool = False
    # True when this message was SYNTHESIZED from a press on the bot's own inline
    # keyboard rather than typed. Tapping a widget the bot posted is addressing the
    # bot by construction, so the activation gate serves it unconditionally.
    #
    # An explicit provenance flag rather than a forged `reply_to_user_id`: a press is
    # not a reply, and pretending otherwise would put a lie where the audit trail and
    # the reply-threading decision both read. A future gate gets to see what this
    # actually was.
    from_widget: bool = False


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
# max_buttons=25: TOTAL interactive choices per prompt (the renderer packs 2
# per row -> up to 13 scrollable rows), parity with discord's platform-
# practical total. Enforced via apply_options_cap; overflow degrades to a
# numbered text list. The genuinely unbounded keyboard was the defect this
# closes (huge lists 400); 9-25 choice keyboards worked before and still do.
# (The old declared 8 was a mislabeled per-row number, never a chosen total.)
TELEGRAM_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    reactions=True,  # setMessageReaction — used for the steer-ack receipt
    files_inbound=True,  # photos/documents ingested via telegram/attachments.py
    # Local image references in a reply are extracted by the shared
    # messaging/outbound_files.py and uploaded by multipart sendPhoto /
    # sendMediaGroup, so the agent's chart arrives as a picture instead of a
    # filesystem path. Rasters only: the extractor decides type from the leading
    # bytes and refuses anything else.
    files_outbound=True,
    # sendRichMessage (Bot API 10.1+) renders structured markdown natively —
    # tables, headings, code blocks, lists — and the renderer routes every
    # table-bearing seal through it; inline keyboards carry the interactive half.
    rich_blocks=True,
    threads=True,
    # sendRichMessage renders a GFM pipe table as a real table; the plain-HTML
    # fallback monospaces the run (telegram/renderer.py::_seal_table_fallback).
    table_mode=TABLE_POLICY_NATIVE,
    native_tables=True,
    max_message_chars=TELEGRAM_CHUNK_LIMIT,
    max_buttons=25,
    supports_proactive_send=True,
)


#: Telegram's own username grammar: 5-32 of ``[A-Za-z0-9_]``. Re-derived here
#: rather than trusted, because this value is interpolated into the prompt.
_HANDLE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_HANDLE_MAX = 32


def prompt_safe_handle(raw: str) -> str:
    """*raw* as an ``@handle`` safe to interpolate into the prompt, or ``""``.

    The context builder writes the sender as a bare ``[CURRENT USER] {name}``
    line immediately above ``[CURRENT USER REQUEST — respond to this]``, so a name
    carrying a newline could forge that boundary and everything after it would
    read as instructions rather than as a name. Telegram's platform grammar
    already excludes every character that would let it — but a guard that relies
    on a remote service continuing to enforce its own documented rule is not a
    guard, so the grammar is re-checked here.

    Whole-value: a handle that does not match is dropped rather than stripped down
    to its legal characters. Rewriting it would put a *different* identity in front
    of the model, which is worse than showing none — the numeric id remains in the
    session key either way.

    Telegram's ``first_name`` is deliberately NOT a fallback: it is unconstrained
    free text, so it carries exactly the injection surface this function exists to
    refuse.
    """
    handle = raw.strip().lstrip("@")
    if not handle or len(handle) > _HANDLE_MAX:
        return ""
    if not set(handle) <= _HANDLE_CHARS:
        return ""
    return f"@{handle}"


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

    async def send_document(
        self,
        conversation_id: str,
        file: "OutboundFile",
        *,
        caption: str = "",
        thread_id: str | None = None,
    ) -> str:
        """Send one validated file into this conversation. Returns the message id.

        The transport-level upload verb, Discord's ``send_document``
        counterpart: a caller holding a transport does not reach past it into
        the client. ``file`` carries validated bytes (the ``OutboundFile``
        contract — the path is provenance, never re-opened).
        """
        mid = await self._client.send_document(
            int(conversation_id),
            file,
            caption=caption or None,
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

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Re-decide a proactive send against the live roster. Fails closed.

        Telegram can answer this authoritatively, which is why it does: a private
        ``chat_id`` IS the peer's ``user_id``, so the persisted link carries the
        very principal ``authorize`` checks. A forum Topic carries a supergroup
        ``chat_id`` instead, so it is routed through the same
        :func:`forum_gate_outcome` predicate the inbound and callback paths use --
        a third call site rather than a second copy, so an outbound send can never
        be permitted into a Topic that inbound would refuse.

        ``thread_id`` is what distinguishes the two: Telegram private chats carry
        no ``message_thread_id``, so a link with one is a forum Topic.
        """
        if not conversation_id:
            return False
        if thread_id:
            # Forum Topic. int() because the shared predicate matches numeric
            # chat_ids; a non-numeric id is malformed, and refusing is the
            # fail-closed answer at an egress boundary.
            try:
                chat_id, topic_id = int(conversation_id), int(thread_id)
            except (TypeError, ValueError):
                return False
            return (
                forum_gate_outcome(
                    "supergroup",
                    chat_id,
                    topic_id,
                    allow_forum=self._allow_forum,
                    allowed_forum_chat_ids=self._allowed_forum_chat_ids,
                )
                is None
            )
        return conversation_id in self._allowed

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
        authorized message to the turn dispatcher. Attachment-only messages
        (no text/caption) are accepted; sticker-only messages are dropped.
        """
        if not isinstance(raw_envelope, TelegramInbound):
            return
        inbound = raw_envelope
        if not inbound.text and not inbound.attachments:
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
            username=prompt_safe_handle(inbound.username),
            reply_to_user_id=inbound.reply_to_user_id,
            attachments=list(inbound.attachments),
            mentions=inbound.mentions,
            has_entities=inbound.has_entities,
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(msg)
