"""Layer 1 -- Webex Messaging as a concrete ``MessagingTransport``.

Wraps the low-level :class:`WebexClient` (device-WebSocket inbound + REST
outbound) in the channel-neutral transport contract, so the Webex channel
rides the shared ``TurnDriver`` (credential/exfil redaction + tool-approval
ladder + SEL audit) instead of a hand-rolled turn loop.

Dependency direction is ``webex -> messaging`` (allowed); the neutral
``messaging`` package never imports ``webex``.

Webex differs from Slack/Telegram in two ways, both absorbed INSIDE this
transport / its renderer (the neutral layers are untouched):

* No streaming: Webex caps a message at 10 edits, so a typewriter
  edit-stream is infeasible. The renderer posts a placeholder, spends a
  small edit budget on tool-progress status, and delivers the final answer
  in one shot (``streaming=False``, ``edit=True``).
* Direct rooms only, fail closed: in a group space the bot only sees
  @mentions, but a reply would land in the group -- exposing tool output to
  non-authorized members (same reasoning as the Telegram private-chat-only
  gate). Non-direct rooms are denied and audited.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from kiro_crew.messaging.tables import TABLE_POLICY_AUTO
from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.webex.client import WEBEX_MAX_TEXT, WebexClient, WebexInbound

logger = logging.getLogger(__name__)

DispatchFn = Callable[[WebexInbound], Awaitable[None]]

# Webex capabilities: no streaming (10-edit cap per message), but edits exist
# (budgeted status placeholder), no tappable chips (max_buttons=0, so the
# renderer routes [OPTIONS:] through the shared numbered-text fallback), and
# proactive send is fine (a bot may post to a room / person at any time).
#
# max_message_chars is a CHARACTER count, but Webex caps messages in UTF-8
# BYTES (WEBEX_MAX_TEXT = 7000 bytes; see chunk_utf8's docstring on why the
# neutral char chunker is unsafe here). This previously declared the raw byte
# limit as a char count — a caller chunking 7000 CHARS of CJK produces up to
# 28000 bytes, and WebexClient.send_message tail-TRUNCATES the overflow
# (silent data loss on the dashboard mirror leg, the one live consumer of
# this field). Declare the worst-case-safe char count instead: 4 bytes/char.
WEBEX_SAFE_MESSAGE_CHARS = WEBEX_MAX_TEXT // 4

WEBEX_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=True,
    reactions=False,
    files_inbound=False,
    files_outbound=False,
    rich_blocks=False,
    threads=False,
    # Webex renders pipe tables literally.
    table_mode=TABLE_POLICY_AUTO,
    max_message_chars=WEBEX_SAFE_MESSAGE_CHARS,
    max_buttons=0,
    supports_proactive_send=True,
)


class WebexTransport(MessagingTransport):
    """Concrete Webex transport over the low-level ``WebexClient``."""

    channel_type = "webex"

    def __init__(
        self,
        client: WebexClient,
        *,
        allowed_emails: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the (lowercased) allow-list so it can't
        # mutate under an in-flight decision.
        self._allowed: frozenset[str] = frozenset(e.lower() for e in allowed_emails if e)
        self._dispatch = dispatch
        self.capabilities = WEBEX_CAPABILITIES

    @property
    def client(self) -> WebexClient:
        """The underlying Webex client (held + exposed, not hidden)."""
        return self._client

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        mid = await self._client.send_message(conversation_id, content)
        return mid or ""

    async def resolve_conversation(self, user_id: str) -> str:
        # The user id IS the email; the client's send path maps an
        # email-shaped conversation id onto toPersonEmail, which opens or
        # reuses the 1:1 space server-side.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # Sessions persist via conversation_log instead.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        return [
            ConfiguredChannelTarget(f"user:{email}", f"Webex DM · {email}")
            for email in sorted(self._allowed)
        ]

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, separator, value = target_id.partition(":")
        if kind != "user" or not separator or value.lower() not in self._allowed:
            return None
        return await self.resolve_conversation(value), None

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Re-check the email roster via the session's principal. Fails closed.

        The conversation id cannot answer this: a session binds
        ``inbound.room_id`` (see :meth:`receive`) while the roster holds
        **emails**, and a room id is opaque -- nothing in this process maps one
        back to the person in it. *principal* is that person, so the check reaches
        the same allow-list :meth:`authorize` uses, lowercased the same way.

        With no principal there is nothing else to consult, so it refuses. Webex is
        direct-rooms-only (a non-direct room is refused inbound and audited), so
        every route this transport serves has exactly one recipient, and an
        unidentifiable one must not be posted to from a network egress boundary.

        That refusal is reached by a ``unified`` DM bucket, which collapses several
        peers into one session on purpose, so nothing available here establishes
        which of them the room currently belongs to. Sessions under the default
        ``per-channel-peer`` scope carry their peer in the key and are unaffected.
        """
        if not conversation_id:
            return False
        return bool(principal) and principal.lower() in self._allowed

    # -- Lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        await self._client.start()

    async def disconnect(self) -> None:
        await self._client.close()

    # -- Inbound adapter ----------------------------------------------------
    def authorize(self, msg: InboundMessage) -> bool:
        """Email allow-list, deny-by-default. Empty allow-list authorizes nobody."""
        allowed = bool(msg.user_id) and msg.user_id.lower() in self._allowed
        if not allowed:
            # Audit ALL denials (including empty/missing user_id) so
            # deny-by-default is observable, mirroring the other transports.
            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="webex_transport.authorize",
                outcome="denied",
                source="webex",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize -> authorize -> dispatch.

        The low-level client hydrates activity events into ``WebexInbound``;
        this adapter maps that onto the neutral ``InboundMessage``, enforces
        deny-by-default auth + direct-rooms-only, and hands the richer
        ``WebexInbound`` (carrying ``room_id``) to the turn dispatcher.
        """
        if not isinstance(raw_envelope, WebexInbound):
            return
        inbound = raw_envelope
        if not inbound.text:
            return
        # Direct rooms only, fail closed: replying in a group space would
        # expose tool output to non-authorized members. Deny + audit.
        if inbound.room_type != "direct":
            sel().log_api_access(
                caller=inbound.person_email or "unknown",
                operation="webex_transport.receive",
                outcome="denied_non_direct_room",
                source="webex",
            )
            return
        msg = InboundMessage(
            channel_type="webex",
            user_id=inbound.person_email,
            conversation_id=inbound.room_id,
            text=inbound.text,
            thread_id=None,
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(inbound)
