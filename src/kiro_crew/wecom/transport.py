"""Layer 1 -- WeCom (企业微信) as a concrete ``MessagingTransport``.

Wraps the low-level :class:`WeComClient` (WebSocket long-connection + WS
streaming reply / one-shot ``response_url`` fallback) in the channel-neutral
transport contract, so the WeCom channel rides the shared ``TurnDriver``
(credential/exfil redaction + tool-approval ladder + SEL audit) instead of a
hand-rolled turn loop.

Dependency direction is ``wecom -> messaging`` (allowed); the neutral
``messaging`` package never imports ``wecom``.

WeCom differs from Slack/Telegram in three ways, all absorbed INSIDE this
transport (the neutral layers are untouched):

* Persistent outbound WebSocket (``connect``/``disconnect`` lifecycle) -- not
  push (Slack) or long-poll (Telegram).
* The turn dispatch runs as a background task (started by the client) so the
  single WS keeps sending ACK/pong during a long streaming turn -- an inline
  await would starve the ping loop and trip a false disconnect.
* No proactive send: a reply is bound to the inbound message's WS ``req_id``
  (or its one-shot ``response_url``), so ``supports_proactive_send`` is False.

Security: :meth:`authorize` is **deny-by-default** and owner-only. An empty
allow-list authorizes nobody (fail closed), never everybody. The only path to
"everybody" is the explicit ``allow_all`` opt-in (config
``wecom.allow_all_users``), which still denies frames without a userid.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.wecom.client import _REPLY_MAX_CHARS, WeComClient, WeComInbound

# A dispatch callback consumes an authorized WeCom inbound (carrying the WS
# routing keys ``req_id`` / ``response_url`` that the neutral InboundMessage
# cannot hold) and drives a turn. The gateway supplies the real implementation.
DispatchFn = Callable[["WeComInbound"], Awaitable[None]]

# WeCom AI-bot capabilities: WS streaming (each frame REPLACES the bubble ->
# edit=True), a generous char cap, no tappable chips (max_buttons=0 records
# the fact; the renderer's [OPTIONS:] drop is unconditional and does not read
# it), and NO proactive send (a reply is bound to the inbound message's
# req_id / one-shot response_url).
WECOM_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    reactions=False,
    files_inbound=False,
    files_outbound=False,
    rich_blocks=False,
    threads=False,
    max_message_chars=_REPLY_MAX_CHARS,
    max_buttons=0,
    supports_proactive_send=False,
)


class WeComTransport(MessagingTransport):
    """Concrete WeCom transport over the low-level ``WeComClient`` (WebSocket)."""

    channel_type = "wecom"

    def __init__(
        self,
        client: WeComClient,
        *,
        allowed_users: Iterable[str] = (),
        allow_all: bool = False,
        owner_id: str = "",
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the allow-list so it can't mutate under an
        # in-flight decision. owner_id (when set) is always allowed.
        self._allowed: frozenset[str] = frozenset(u for u in allowed_users if u)
        # Explicit opt-in only (config wecom.allow_all_users): every org
        # member may DM the bot. This is a deliberate toggle, NEVER inferred
        # from an empty allow-list — an empty list stays fail-closed. A WeCom
        # AI bot is reachable only inside the org tenant, which is what makes
        # this a reasonable (if broad) grant.
        self._allow_all = bool(allow_all)
        self._owner_id = owner_id
        self._dispatch = dispatch
        self.capabilities = WECOM_CAPABILITIES

    @property
    def client(self) -> WeComClient:
        """The underlying WeCom WS client (held + exposed, not hidden)."""
        return self._client

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        # WeCom has no stable conversation-addressed send: a reply is bound to
        # an inbound req_id (WS stream) or its one-shot response_url. Streaming
        # goes through the renderer; here conversation_id is treated as a
        # response_url fallback target for out-of-band one-shot sends.
        if conversation_id:
            await self._client.send_reply(conversation_id, content)
        return ""

    async def resolve_conversation(self, user_id: str) -> str:
        # No addressable DM channel id; the userid is the logical conversation.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # WeCom AI-bot cannot page DM history; sessions persist via
        # conversation_log instead.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        identities = set(self._allowed)
        if self._owner_id:
            identities.add(self._owner_id)
        targets = [
            ConfiguredChannelTarget(
                f"user:{user_id}",
                f"WeCom DM · {user_id}",
                available=False,
                unavailable_reason="WeCom only allows replies to an inbound message",
            )
            for user_id in sorted(identities)
        ]
        if self._allow_all and not targets:
            targets.append(
                ConfiguredChannelTarget(
                    "policy:all",
                    "WeCom · organization users",
                    available=False,
                    unavailable_reason="WeCom only allows replies to an inbound message",
                )
            )
        return targets

    # -- Lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        await self._client.start()  # launches the WS connect/serve background loop

    async def disconnect(self) -> None:
        await self._client.close()

    # -- Inbound adapter ----------------------------------------------------
    def authorize(self, msg: InboundMessage) -> bool:
        """Owner-only, deny-by-default. Empty allow-list authorizes nobody.

        ``allow_all`` (explicit config opt-in) authorizes any inbound with a
        non-empty userid — a missing/empty userid is denied even then, so an
        anonymous or malformed frame never reaches dispatch.
        """
        uid = msg.user_id
        allowed = bool(uid) and (
            self._allow_all
            or (bool(self._owner_id) and uid == self._owner_id)
            or uid in self._allowed
        )
        if not allowed:
            # Audit ALL denials (including empty/missing userid) via the helper
            # that fills event_id + timestamp, so the row survives SEL prune (a
            # raw log() with an empty timestamp sorts before the cutoff and is
            # dropped on the next prune).
            sel().log_api_access(
                caller=uid or "unknown",
                operation="wecom_transport.authorize",
                outcome="denied",
                source="wecom",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize -> authorize -> dispatch.

        The low-level client parses inbound WS frames into ``WeComInbound`` and
        invokes this as its ``on_message`` (already wrapped in a background task
        so the WS receive loop keeps breathing during a long turn). We map onto
        the neutral ``InboundMessage`` for the deny-by-default authorize
        contract, then hand the richer ``WeComInbound`` (carrying ``req_id`` /
        ``response_url``) to the turn dispatcher.
        """
        if not isinstance(raw_envelope, WeComInbound):
            return
        inbound = raw_envelope
        if not inbound.text:
            return
        msg = InboundMessage(
            channel_type="wecom",
            user_id=inbound.userid,
            conversation_id=inbound.userid,
            text=inbound.text,
            thread_id=None,
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(inbound)
