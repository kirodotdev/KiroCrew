"""Microsoft Teams as a concrete ``MessagingTransport``.

Wraps the low-level :class:`TeamsClient` (Bot Framework webhook inbound + REST
outbound) in the channel-neutral transport contract, so the Teams channel rides
the shared ``TurnDriver`` (credential/exfil redaction + tool-approval ladder +
SEL audit) instead of a hand-rolled turn loop.

Dependency direction is ``teams -> messaging`` (allowed); the neutral
``messaging`` package never imports ``teams``.

Teams is DM-only and fail-closed:

* Direct/personal rooms only: in a channel or group chat the bot's reply would
  land in front of non-authorized members, exposing tool output (same reasoning
  as the Webex/Telegram direct-only gate). Non-personal scopes are denied and
  audited BEFORE authorization.
* No streaming: the renderer posts a typing indicator and delivers the final
  answer in one shot (``streaming=False``); ``max_buttons=0`` so the renderer
  drops ``[OPTIONS:]`` trailers rather than emitting unsupported buttons.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.teams.client import TEAMS_MAX_TEXT, TeamsClient, TeamsInbound

logger = logging.getLogger(__name__)

DispatchFn = Callable[[TeamsInbound], Awaitable[None]]

# Teams capabilities: no token streaming, no in-place edit (MVP), no tappable
# chips (max_buttons=0 records the fact; the renderer's [OPTIONS:] drop is
# unconditional and does not read it), proactive send is fine for a
# conversation we've already seen a service_url for.
TEAMS_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=False,
    reactions=False,
    files_inbound=False,
    files_outbound=False,
    rich_blocks=False,
    threads=False,
    max_message_chars=TEAMS_MAX_TEXT,
    max_buttons=0,
    supports_proactive_send=True,
)


class TeamsTransport(MessagingTransport):
    """Concrete Teams transport over the low-level ``TeamsClient``."""

    channel_type = "teams"

    def __init__(
        self,
        client: TeamsClient,
        *,
        allowed_emails: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the (lowercased) allow-list so it can't
        # mutate under an in-flight decision.
        self._allowed: frozenset[str] = frozenset(e.lower() for e in allowed_emails if e)
        self._dispatch = dispatch
        # conversation_id -> serviceUrl, learned from inbound activities so
        # proactive/outbound sends can reach a known conversation.
        self._service_urls: dict[str, str] = {}
        self._conversations_by_user: dict[str, str] = {}
        self.capabilities = TEAMS_CAPABILITIES

    @property
    def client(self) -> TeamsClient:
        """The underlying Teams client (held + exposed, not hidden)."""
        return self._client

    def service_url_for(self, conversation_id: str) -> str:
        """Return the last-seen serviceUrl for a conversation (or empty)."""
        return self._service_urls.get(conversation_id, "")

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        service_url = self._service_urls.get(conversation_id, "")
        mid = await self._client.send_message(conversation_id, content, service_url)
        return mid or ""

    async def resolve_conversation(self, user_id: str) -> str:
        # DM sessions are keyed on the Teams conversation id, which the inbound
        # activity supplies directly; there is no separate open-DM step.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # Sessions persist via conversation_log instead.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets: list[ConfiguredChannelTarget] = []
        for identity in sorted(self._allowed):
            available = identity in self._conversations_by_user
            targets.append(
                ConfiguredChannelTarget(
                    f"user:{identity}",
                    f"Teams DM · {identity}",
                    available=available,
                    unavailable_reason=(
                        "" if available else "Send a message to Kiro Crew in Teams first"
                    ),
                )
            )
        return targets

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, separator, value = target_id.partition(":")
        identity = value.lower()
        if kind != "user" or not separator or identity not in self._allowed:
            return None
        conversation_id = self._conversations_by_user.get(identity, "")
        if not conversation_id or not self._service_urls.get(conversation_id):
            return None
        return conversation_id, None

    # -- Lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.close()

    # -- Inbound adapter ----------------------------------------------------
    def authorize(self, msg: InboundMessage) -> bool:
        """Allow-list (email or AAD object id), deny-by-default.

        ``msg.user_id`` is the resolved sender identity (email when Teams
        supplies it, else the AAD object id). Empty allow-list authorizes
        nobody.
        """
        allowed = bool(msg.user_id) and msg.user_id.lower() in self._allowed
        if not allowed:
            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="teams_transport.authorize",
                outcome="denied",
                source="teams",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize -> scope gate -> authorize -> dispatch.

        The low-level client hydrates activities into ``TeamsInbound``; this
        adapter enforces direct-rooms-only + deny-by-default auth, maps onto the
        neutral ``InboundMessage``, and hands the richer ``TeamsInbound``
        (carrying ``service_url``) to the turn dispatcher.
        """
        if not isinstance(raw_envelope, TeamsInbound):
            return
        inbound = raw_envelope
        if not inbound.text:
            return
        # Remember the serviceUrl so proactive/outbound sends can reach it.
        if inbound.conversation_id and inbound.service_url:
            self._service_urls[inbound.conversation_id] = inbound.service_url
        # Direct/personal rooms only, fail closed: a reply in a channel/group
        # would expose tool output to non-authorized members. Deny + audit.
        if inbound.conversation_type != "personal":
            sel().log_api_access(
                caller=inbound.user_email or inbound.aad_object_id or "unknown",
                operation="teams_transport.receive",
                outcome="denied_non_personal_scope",
                source="teams",
            )
            return
        # Resolve a stable sender identity: prefer the UPN/email when Teams
        # supplies it, else fall back to the AAD object id (which activities
        # reliably carry, unlike email). The allow-list may hold either form.
        # Fail closed if neither is present.
        identity = inbound.user_email or inbound.aad_object_id
        if not identity:
            sel().log_api_access(
                caller="unknown",
                operation="teams_transport.receive",
                outcome="denied_unresolved_identity",
                source="teams",
            )
            return
        msg = InboundMessage(
            channel_type="teams",
            user_id=identity,
            conversation_id=inbound.conversation_id,
            text=inbound.text,
            thread_id=None,
        )
        if not self.authorize(msg):
            return
        self._conversations_by_user[identity.lower()] = inbound.conversation_id
        if self._dispatch is not None:
            await self._dispatch(inbound)
