"""Slack as a concrete :class:`MessagingTransport`.

This wraps the existing ``SlackClientOps`` surface in the channel-neutral
transport contract. Nothing in the live gateway path constructs it: the Slack
path routes through ``slack.transport_dispatch`` (TurnDriver + SlackRenderer)
and only ``SlackTransport.channel_type`` is read, by ``handlers_system``.

Direction of dependency is ``slack -> messaging`` (allowed): the neutral
``messaging`` package never imports Slack.

Security note: :meth:`SlackTransport.authorize` is **deny-by-default** and
owner-only. An unconfigured transport (empty ``allowed_users``) authorizes
nobody. Bot-authored events are dropped unless their ``bot_id`` positively
matches the ``trusted_bot_ids`` allow-list (empty by default, so an
unconfigured transport drops every bot), mirroring the Socket Mode drop site
in ``slack/events.py`` so the two inbound paths agree.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from kiro_crew.messaging.tables import TABLE_POLICY_OFF
from kiro_crew.messaging.transport import (
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.enterprise import validated_self_bot_id
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
    # Read by SlackRenderer._uploads_enabled before it extracts local image
    # references out of a sealed reply and uploads them (slack/files.py ->
    # files_upload_v2). It is the flag the capability ledger defines, not the
    # dashboard's /api/slack/upload-file route, which is a human upload flow no
    # renderer consults: declaring the flag for that route is what made this a
    # mislabel. Flipping it to False makes the renderer keep printing the
    # markdown path, which is the honest degradation, never a silent drop.
    files_outbound=True,
    rich_blocks=True,
    threads=True,
    # Slack's established format pipeline already flattens tables byte-for-byte.
    table_mode=TABLE_POLICY_OFF,
    max_message_chars=SLACK_MSG_LIMIT,
    # 10 = the platform cap on a checkboxes element's options[] — the widget
    # Slack actually renders for [OPTIONS:]. The previous 5 was copied from
    # the 5-buttons-per-actions-row limit, which does not govern checkboxes.
    # Enforced at build_options_blocks (slack/format.py) via cap_choices.
    max_buttons=10,
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
        trusted_bot_ids: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: copy into a frozenset so the allow-list cannot be
        # mutated out from under an in-flight authorization decision.
        self._allowed_users: frozenset[str] = frozenset(allowed_users)
        # Second allow-list, for peer bots (slack.trusted_bot_ids). Same
        # frozen-snapshot rationale; empty default drops every bot event.
        self._trusted_bot_ids: frozenset[str] = frozenset(trusted_bot_ids)
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

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Never consulted -- permits, and says why.

        An override with a reason rather than an inherited default, so a reader
        does not have to infer this transport's stance. The shared send ladder
        (``chat_runner._resolve_channel_target``) returns early for
        ``SLACK_NAMESPACE`` before any transport call: Slack's proactive traffic
        goes through the gateway's own client and streaming path, which is not
        registered in ``channel_transports``. Nothing routes here, so there is no
        decision this method can enforce.

        It could not answer anyway: a Slack link persists a **channel** id
        (``D…``/``C…``) while the roster holds user ids, so the same
        conversation-id-is-not-a-principal problem applies. Slack's own proactive
        paths do their allow-list checks at their own call sites.
        """
        return bool(conversation_id)

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
        """ack -> trusted-bot gate -> normalize -> authorize -> dispatch.

        Drops anything not authored by an allowed human or an allow-listed
        peer bot; only an authorized, normalized message reaches the
        dispatch callback.
        """
        event = raw_envelope.get("event", raw_envelope) if isinstance(raw_envelope, dict) else None
        if not isinstance(event, dict):
            return
        # A bot-authored event is admitted ONLY on a positive match of its
        # bot_id against the trusted_bot_ids allow-list (deny-by-default:
        # the empty default drops every bot-authored event). That second
        # allow-list exists precisely to admit bot ids, which the owner-only
        # user allow-list never contains. Mirrors the Socket Mode drop site
        # (slack/events.py) so the two inbound paths agree:
        # - The gateway's own bot id is never trusted even when listed --
        #   admitting it would make every reply re-enter as fresh input,
        #   a self-reply loop.
        # - An unverified self identity (startup auth.test unavailable)
        #   admits no bots: a configured trust feature whose self-exclusion
        #   cannot be applied fails closed and trusts nobody.
        # - The trust decision runs BEFORE the subtype filter because a
        #   bot-authored message commonly carries subtype == "bot_message":
        #   an untrusted denial must be audited (not silently
        #   subtype-dropped), and a trusted bot's bot_message must not be
        #   eaten by the subtype gate below.
        # Loop bounding (the per-thread trusted-bot turn cap) is the
        # dispatch layer's job; this transport decides admissibility only.
        bot_id = event.get("bot_id") or ""
        self_bot_id = validated_self_bot_id()
        from_trusted_bot = (
            bool(bot_id)
            and bool(self_bot_id)
            and bot_id != self_bot_id
            and bot_id in self._trusted_bot_ids
        )
        if bot_id and not from_trusted_bot:
            if bot_id == self_bot_id and bot_id in self._trusted_bot_ids:
                deny_error = "own_bot_id_never_trusted"
            elif not self_bot_id and bot_id in self._trusted_bot_ids:
                deny_error = "trusted_bot_requires_verified_self_id"
            else:
                deny_error = "untrusted_bot"
            sel().log_api_access(
                caller=bot_id,
                operation="slack_transport.receive",
                outcome="denied",
                source="slack",
                error=deny_error,
            )
            return
        if event.get("subtype") == "bot_message" and not from_trusted_bot:
            return
        msg = InboundMessage(
            channel_type="slack",
            user_id=event.get("user") or (bot_id if from_trusted_bot else ""),
            conversation_id=event.get("channel") or "",
            text=event.get("text") or "",
            thread_id=event.get("thread_ts") or event.get("ts"),
            is_mention=bool(event.get("is_mention")),
        )
        if from_trusted_bot:
            # The positive allow-list match IS the bot's authorization (a
            # bot id is never in the owner-only allow-list); audit the
            # admission so the decision basis stays traceable.
            sel().log_api_access(
                caller=msg.user_id,
                operation="slack_transport.receive",
                outcome="allowed",
                source="slack",
                resources="trusted_bot",
            )
        elif not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(msg)
