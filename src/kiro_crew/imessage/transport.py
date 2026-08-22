"""Layer 1 -- iMessage as a concrete ``MessagingTransport``.

Wraps :class:`IMessageClient` (the local ``imsg`` bridge) in the
channel-neutral transport contract, so the iMessage channel rides the shared
``TurnDriver`` (credential/exfil redaction + tool-approval ladder + SEL audit)
instead of a hand-rolled turn loop.

Dependency direction is ``imessage -> messaging`` (allowed); the neutral
``messaging`` package never imports ``imessage``.

Access control here is stricter than on the bot channels, because the identity
on the other end is a phone number rather than a workspace member:

* **Handle allowlist, deny-by-default.** An empty allowlist authorizes nobody.
  There is no org boundary to fall back on -- anyone who knows the user's
  number can send to it.
* **Own messages are ignored**, on two signals rather than one. The watch is an
  all-chat stream and sees the agent's own replies; without this the channel
  answers itself in a loop. The bridge's ``is_from_me`` covers the rows it has
  already attributed to us, and the client's ledger of what it sent covers the
  self-chat case, where the allow-listed handle is the identity the agent sends
  as and that flag is not dependable on its own (issue #5246).
* **DM only, fail closed.** A reply in a group chat would deliver tool output
  to members who are not on the allowlist, the same reasoning that already
  makes Telegram and Webex direct-only.
* **Unauthorized inbound is dropped with no reply**, so an unknown sender
  learns nothing about what they reached.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from kiro_crew.imessage.client import (
    IMessageClient,
    IMessageInbound,
    normalize_handle,
    redact_handle,
)
from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

DispatchFn = Callable[[IMessageInbound], Awaitable[None]]

# iMessage publishes no maximum message length, and this field is a claim other
# code trusts (the dashboard mirror leg chunks against it), so it is declared
# conservatively rather than guessed high: under-declaring costs an extra
# message, over-declaring risks a send the platform silently refuses. It is a
# deliverability-safe chunk size, not a measured platform cap.
IMESSAGE_SAFE_MESSAGE_CHARS = 4000

# Nothing about a sent iMessage can be changed after the fact -- no edit, no
# reactions, no tappable choices -- so the only progress affordance is the
# typing indicator, which the renderer drives directly rather than through a
# capability flag. Group chats and attachments are out of scope for v1, and
# session resume is not honoured because inbound routing keys off the handle
# rather than a mirrored session binding.
IMESSAGE_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=False,
    reactions=False,
    files_inbound=False,
    files_outbound=False,
    rich_blocks=False,
    threads=False,
    max_message_chars=IMESSAGE_SAFE_MESSAGE_CHARS,
    max_buttons=0,
    supports_proactive_send=True,
    supports_session_resume=False,
)


class IMessageTransport(MessagingTransport):
    """Concrete iMessage transport over the local ``imsg`` bridge."""

    channel_type = "imessage"

    def __init__(
        self,
        client: IMessageClient,
        *,
        allowed_handles: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the normalized allow-list so it cannot
        # mutate under an in-flight decision.
        self._allowed: frozenset[str] = frozenset(
            normalized for h in allowed_handles if (normalized := normalize_handle(h))
        )
        self._dispatch = dispatch
        self.capabilities = IMESSAGE_CAPABILITIES

    @property
    def client(self) -> IMessageClient:
        """The underlying bridge client (held + exposed, not hidden)."""
        return self._client

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        return await self._client.send(conversation_id, content)

    async def resolve_conversation(self, user_id: str) -> str:
        # The handle IS the conversation: the bridge's send path takes a
        # recipient handle and opens or reuses the 1:1 chat itself.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # Sessions persist via conversation_log instead. Reading history back
        # out of the Messages database would also pull in messages the user
        # never addressed to the agent.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        return [
            ConfiguredChannelTarget(f"user:{handle}", f"iMessage · {handle}")
            for handle in sorted(self._allowed)
        ]

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, separator, value = target_id.partition(":")
        if kind != "user" or not separator or normalize_handle(value) not in self._allowed:
            return None
        return await self.resolve_conversation(value), None

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Re-check the handle roster before a proactive send. Fails closed.

        Answerable exactly because the handle IS the conversation here, so a
        persisted link carries the same principal ``authorize`` checks -- and it
        is normalized the same way, so a link stored in one spelling
        (``+1 555 0100``) still matches a roster entry in another.
        """
        handle = normalize_handle(conversation_id)
        return bool(handle) and handle in self._allowed

    # -- Lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        await self._client.start()

    async def disconnect(self) -> None:
        await self._client.close()

    # -- Inbound adapter ----------------------------------------------------
    def authorize(self, msg: InboundMessage) -> bool:
        """Handle allow-list, deny-by-default. Empty allow-list authorizes nobody."""
        handle = normalize_handle(msg.user_id)
        allowed = bool(handle) and handle in self._allowed
        if not allowed:
            # Audit ALL denials (including an empty handle) so deny-by-default
            # is observable, mirroring the other transports.
            sel().log_api_access(
                caller=redact_handle(msg.user_id),
                operation="imessage_transport.authorize",
                outcome="denied",
                source="imessage",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize -> authorize -> dispatch.

        The client parses a bridge watch notification into
        :class:`IMessageInbound`; this adapter maps that onto the neutral
        ``InboundMessage``, enforces own-message suppression,
        direct-chats-only and deny-by-default auth, then hands the richer
        ``IMessageInbound`` (carrying the chat selector) to the dispatcher.
        """
        if not isinstance(raw_envelope, IMessageInbound):
            return
        inbound = raw_envelope
        # The all-chat watch echoes the agent's own replies back. Not a
        # security denial -- just this channel's own traffic -- so it is
        # dropped without an audit event, which would otherwise log one entry
        # per outbound message.
        if inbound.is_from_me:
            return
        if not inbound.text:
            return
        # DM only, fail closed: replying in a group chat would expose tool
        # output to members who are not on the allow-list.
        if inbound.is_group:
            sel().log_api_access(
                caller=redact_handle(inbound.handle),
                operation="imessage_transport.receive",
                outcome="denied_group_chat",
                source="imessage",
            )
            return
        msg = InboundMessage(
            channel_type="imessage",
            user_id=inbound.handle,
            conversation_id=inbound.handle,
            text=inbound.text,
            thread_id=None,
        )
        if not self.authorize(msg):
            return
        # LAST gate, and its position is load-bearing in both directions.
        #
        # After `is_from_me`, because a copy the bridge already attributes to us
        # must not consume the ledger record that the UNattributed echo needs --
        # consuming it there would restore the loop while looking fixed.
        #
        # After the group and allow-list gates, because this one has a side
        # effect: it consumes the record. A row that is going to be dropped
        # anyway -- a group message, an unlisted sender -- must not be able to
        # spend the record on its way out, or the real echo that follows finds
        # nothing and is answered.
        #
        # `is_from_me` alone cannot carry this: in a self-chat the allow-listed
        # sender IS the identity the agent replies as, and the bridge writes that
        # flag asynchronously (its 500ms watch debounce exists so a correction
        # can land first). See `is_own_echo`.
        if self._client.is_own_echo(inbound):
            return
        if self._dispatch is not None:
            await self._dispatch(inbound)
