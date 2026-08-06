"""Slack as a concrete :class:`MessagingTransport`.

This wraps the existing ``SlackClientOps`` surface in the channel-neutral
transport contract. Nothing in the live gateway path constructs it: the Slack
path routes through ``slack.transport_dispatch`` (TurnDriver + SlackRenderer)
and only ``SlackTransport.channel_type`` is read, by ``handlers_system``.

Direction of dependency is ``slack -> messaging`` (allowed): the neutral
``messaging`` package never imports Slack.

Security note: :meth:`SlackTransport.authorize` is **deny-by-default** and
owner-only. An unconfigured transport (empty ``allowed_users``) authorizes
nobody, and bot-authored events are dropped before authorization.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from kiro_crew.messaging.transport import (
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.format import SLACK_MSG_LIMIT

# A dispatch callback consumes a normalized, already-authorized message and
# drives a turn. The gateway supplies the real implementation later.
DispatchFn = Callable[[InboundMessage], Awaitable[None]]

# Slack's capabilities — the SINGLE declaration (the renderer imports this
# object; it was previously declared twice, an un-DRY drift hazard).
# max_message_chars matches the SHIPPED send path: slack/format.py splits at
# SLACK_MSG_LIMIT (3900), not the platform's ~40000 ceiling that was declared
# before. Declaring the ceiling was a lie waiting for a capability-aware
# caller to trust it and emit messages 10x larger than the renderer ever sends.
SLACK_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    reactions=True,
    files_inbound=True,  # slack/files.py -> messaging/attachments.py ingestion
    files_outbound=True,  # /api/slack/upload-file external-upload flow
    rich_blocks=True,
    threads=True,
    max_message_chars=SLACK_MSG_LIMIT,
    max_buttons=5,
    supports_proactive_send=True,
)


class SlackTransport(MessagingTransport):
    """Concrete Slack transport over the existing ``SlackClientOps`` client."""

    channel_type = "slack"

    def __init__(
        self,
        client: SlackClientOps,
        *,
        allowed_users: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: copy into a frozenset so the allow-list cannot be
        # mutated out from under an in-flight authorization decision.
        self._allowed_users: frozenset[str] = frozenset(allowed_users)
        self._dispatch = dispatch
        # Single source of truth (the renderer imports this same object).
        self.capabilities = SLACK_CAPABILITIES

    # -- G2: the transport holds and exposes its client --------------------
    @property
    def client(self) -> SlackClientOps:
        """The underlying Slack client (G2: held + exposed, not hidden)."""
        return self._client

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        return await self._client.post_message(conversation_id, content, thread_id)

    async def resolve_conversation(self, user_id: str) -> str:
        return await self._client.open_dm(user_id)

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        if not thread_id:
            return []
        replies = await self._client.fetch_thread_replies(conversation_id, thread_id)
        out: list[InboundMessage] = []
        for m in replies:
            out.append(
                InboundMessage(
                    channel_type="slack",
                    user_id=m.get("user") or m.get("bot_id") or "",
                    conversation_id=conversation_id,
                    text=m.get("text") or "",
                    thread_id=thread_id,
                )
            )
        return out

    # -- Inbound adapter ----------------------------------------------------
    def authorize(self, msg: InboundMessage) -> bool:
        """Owner-only, deny-by-default. Empty allow-list authorizes nobody."""
        allowed = bool(msg.user_id) and msg.user_id in self._allowed_users
        if not allowed:
            # Audit ALL denials, including empty/missing user_id (deny-by-default
            # must be observable), mirroring interactions.py's caller fallback.
            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="slack_transport.authorize",
                outcome="denied",
                source="slack",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """ack -> drop bots -> normalize -> authorize -> dispatch.

        Drops anything not authored by an allowed human; only an authorized,
        normalized message reaches the dispatch callback.
        """
        event = raw_envelope.get("event", raw_envelope) if isinstance(raw_envelope, dict) else None
        if not isinstance(event, dict):
            return
        # Drop bot-authored events before authorization (defense in depth:
        # a bot id is never in the owner-only allow-list anyway).
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return
        msg = InboundMessage(
            channel_type="slack",
            user_id=event.get("user") or "",
            conversation_id=event.get("channel") or "",
            text=event.get("text") or "",
            thread_id=event.get("thread_ts") or event.get("ts"),
            is_mention=bool(event.get("is_mention")),
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(msg)
