"""Layer 1 -- Feishu (Lark) as a concrete :class:`MessagingTransport`.

Wraps the low-level :class:`LarkClient` (WebSocket receive + REST reply)
in the channel-neutral transport contract, so the Feishu channel rides the
shared ``TurnDriver`` (credential/exfil redaction + tool-approval ladder +
SEL audit) instead of a hand-rolled turn loop.

Dependency direction is ``feishu -> messaging`` (allowed); the neutral
``messaging`` package never imports ``feishu``.

Security: :meth:`authorize` is **deny-by-default** and owner-only.  An empty
``allowed_open_ids`` MUST authorise nobody (fail closed), never everybody.
Group-chat access is an explicit opt-in gated on both ``allow_group=True``
AND the group's ``chat_id`` appearing in ``allowed_group_ids``; every other
context (unknown group, or a group chat when ``allow_group`` is False) is
denied with a SEL audit record.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from kiro_crew.feishu.client import CHAT_GROUP, CHAT_P2P, LarkClient, LarkInbound
from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel

# A dispatch callback consumes an authorised LarkInbound and drives a turn.
# The gateway supplies the real implementation.
DispatchFn = Callable[["LarkInbound"], Awaitable[None]]

# Feishu capabilities: no edit-in-place streaming (replies are one-shot REST
# calls), no interactive buttons in this v1 integration, no proactive send
# (a reply is always anchored to an inbound message_id).
# max_message_chars reflects the safe 4 000-char cap defined in client.py.
FEISHU_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=False,
    reactions=False,
    files_inbound=False,
    files_outbound=False,
    rich_blocks=False,
    threads=False,
    max_message_chars=4000,
    max_buttons=0,
    supports_proactive_send=False,
)


class FeishuTransport(MessagingTransport):
    """Concrete Feishu transport over the low-level :class:`LarkClient`."""

    channel_type = "feishu"

    def __init__(
        self,
        client: LarkClient,
        *,
        allowed_open_ids: Iterable[str] = (),
        allow_group: bool = False,
        allowed_group_ids: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the allow-list as immutable so it can't
        # mutate under an in-flight authorisation decision.
        self._allowed: frozenset[str] = frozenset(u for u in allowed_open_ids if u)
        # Group-chat gate: both ``allow_group`` AND an explicit chat_id
        # allow-list must be satisfied.  An empty allow-list with
        # ``allow_group=True`` would still deny every group -- fail closed.
        self._allow_group = bool(allow_group)
        self._allowed_group_ids: frozenset[str] = frozenset(g for g in allowed_group_ids if g)
        self._dispatch = dispatch
        self.capabilities = FEISHU_CAPABILITIES

    @property
    def client(self) -> LarkClient:
        """The underlying Lark client (held + exposed, not hidden)."""
        return self._client

    # -- Tier-1 core --------------------------------------------------------

    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        # ``conversation_id`` carries the inbound ``message_id`` (set by
        # ``receive`` via ``FeishuInboundMessage.conversation_id``), so this
        # is a contextual reply rather than a free-standing send.
        if conversation_id:
            await self._client.send_reply(conversation_id, content)
        return ""

    async def resolve_conversation(self, user_id: str) -> str:
        # Feishu replies are anchored to an inbound message; without one there
        # is no proactive DM path in this v1 integration.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # The Feishu Bot API does not expose readable DM history; sessions
        # persist via conversation_log instead.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        return [
            ConfiguredChannelTarget(
                f"user:{oid}",
                f"Feishu DM · {oid}",
                available=False,
                unavailable_reason=(
                    "Feishu only allows replies to an inbound message " "(no proactive DM in v1)"
                ),
            )
            for oid in sorted(self._allowed)
        ]

    # -- Lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        await self._client.start()

    async def disconnect(self) -> None:
        await self._client.close()

    # -- Inbound adapter ----------------------------------------------------

    def authorize(self, msg: InboundMessage) -> bool:
        """Owner-only, deny-by-default.  Empty allow-list authorises nobody."""
        uid = msg.user_id
        allowed = bool(uid) and uid in self._allowed
        if not allowed:
            sel().log_api_access(
                caller=uid or "unknown",
                operation="feishu_transport.authorize",
                outcome="denied",
                source="feishu",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize -> gate -> authorize -> dispatch.

        The low-level client parses inbound WS frames into ``LarkInbound`` and
        routes them here (already in the async event loop via
        ``run_coroutine_threadsafe``).  We apply the group-chat gate first
        (fail-closed for non-p2p contexts), then the user allow-list, and
        finally hand an authorised message to the dispatcher.
        """
        if not isinstance(raw_envelope, LarkInbound):
            return
        inbound = raw_envelope
        if not inbound.text:
            return

        # Chat-type gate (fail closed). The two served contexts are named
        # EXPLICITLY rather than treating "not group" as a DM: an absent value,
        # or a chat type Feishu adds later, would otherwise take the ungated
        # p2p path and let a turn run — and reply — in a context whose
        # authorisation was never evaluated.
        if inbound.chat_type not in (CHAT_P2P, CHAT_GROUP):
            sel().log_api_access(
                caller=inbound.open_id or "unknown",
                operation="feishu_transport.receive",
                outcome="denied_unknown_chat_type",
                source="feishu",
            )
            return

        # Group-chat gate (fail closed).  A Feishu bot added to a group
        # receives every message in that group -- only serve a group whose
        # chat_id is explicitly allow-listed AND ``allow_group`` is True.
        if inbound.chat_type == CHAT_GROUP:
            if not (self._allow_group and inbound.chat_id in self._allowed_group_ids):
                sel().log_api_access(
                    caller=inbound.open_id,
                    operation="feishu_transport.receive",
                    outcome="denied_group_not_allowed",
                    source="feishu",
                )
                return

        # The neutral InboundMessage carries everything authorize() reads. The
        # routing fields (chat_type / chat_id / message_id) stay on the
        # LarkInbound the dispatcher receives, so there is nothing for a
        # channel-specific subclass to add here.
        msg = InboundMessage(
            channel_type="feishu",
            user_id=inbound.open_id,
            # ``conversation_id`` doubles as the reply anchor so that
            # ``send_message(conversation_id, ...)`` can reply contextually.
            conversation_id=inbound.message_id,
            text=inbound.text,
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(inbound)
