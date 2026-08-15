"""Layer 1 — WhatsApp (QR-linked personal account) as a ``MessagingTransport``.

Wraps :class:`kiro_crew.whatsapp.client.WhatsAppClient` in the channel-neutral
transport contract. Dependency direction is ``whatsapp -> messaging``
(allowed); ``messaging`` never imports ``whatsapp``.

The inbound pipeline in :meth:`WhatsAppTransport.receive` is a gauntlet every
event must survive, in order:

1. **shape**: only text-bearing message events pass;
2. **flood gate**: messages older than the connection moment are history
   replay after a reconnect — never answer a backlog (marks nothing read);
3. **echo gate**: a ``from_me`` message whose ID we sent is our own echo;
   a ``from_me`` message we did NOT send is the operator typing (self-chat
   command surface);
4. **group gate**: group chats are dropped unless configured, then gated by
   mode/mention/cooldown (:mod:`kiro_crew.whatsapp.group_gate`);
5. **authorize**: deny-by-default DM policy (``self`` default) with SEL audit
   on every denial.

Capabilities honesty (personal account over the Web protocol): no streaming,
no edits, no buttons (interactive messages are Business-API surface), 4096
char cap, and — unlike the Business Cloud API — **no 24h proactive-send
window**, so ``supports_proactive_send=True`` and reminders work.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.whatsapp.client import WhatsAppClient
from kiro_crew.whatsapp.echo import EchoTracker
from kiro_crew.whatsapp.group_gate import GroupGate, GroupVerdict
from kiro_crew.whatsapp.jids import (
    is_group_jid,
    jid_to_str,
    jid_user,
    normalize_jid,
    wa_id_to_user_jid,
)

logger = logging.getLogger(__name__)

#: seconds of pre-connection history still answered after (re)connect. Events
#: older than ``connected_at - GRACE`` are reconnect replay, not live traffic.
_REPLAY_GRACE_S = 60.0

WHATSAPP_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=False,
    reactions=False,
    files_inbound=False,
    files_outbound=False,
    rich_blocks=False,
    threads=False,
    max_message_chars=4096,
    max_buttons=0,
    supports_proactive_send=True,
)

DM_POLICY_SELF = "self"
DM_POLICY_ALLOWLIST = "allowlist"
DM_POLICY_OPEN = "open"
DM_POLICY_DISABLED = "disabled"


class WhatsAppTransport(MessagingTransport):
    """WhatsApp personal-account transport (see module docstring)."""

    channel_type = "whatsapp"

    @property
    def client(self) -> WhatsAppClient:
        """The low-level client (dashboard pairing handlers read state/QR)."""
        return self._client

    def __init__(
        self,
        client: WhatsAppClient,
        dispatch: Callable[[InboundMessage], Awaitable[None]],
        *,
        dm_policy: str = DM_POLICY_SELF,
        allowed_wa_ids: list[str] | None = None,
        groups: list[dict] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.capabilities = WHATSAPP_CAPABILITIES
        self._client = client
        self._dispatch = dispatch
        self._dm_policy = (dm_policy or "").strip().lower()
        # Frozen at construction so an in-flight decision can't see a mutation.
        self._allowed = frozenset(
            normalize_jid(wa_id_to_user_jid(w)) for w in (allowed_wa_ids or []) if str(w).strip()
        )
        self.echo = EchoTracker()
        self.group_gate = GroupGate(groups)
        self._clock = clock or time.time
        #: verdict metadata for the dispatcher, keyed by id(InboundMessage) —
        #: set in receive() immediately before the dispatch await.
        self.pending_verdicts: dict[int, GroupVerdict] = {}
        client.on_message = self.receive

    # -- Tier-1 core ---------------------------------------------------

    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        """Chunked send; every ID is remembered for echo discipline BEFORE
        the next chunk goes out, so an echo can never race the tracker."""
        jid = normalize_jid(conversation_id)
        last_id = ""
        for message_id in await self._send_tracked(jid, content):
            last_id = message_id
        return last_id

    async def _send_tracked(self, jid: str, content: str) -> list[str]:
        ids = await self._client.send_text(jid, content)
        for message_id in ids:
            self.echo.remember(jid, message_id)
        return ids

    async def resolve_conversation(self, user_id: str) -> str:
        return normalize_jid(wa_id_to_user_jid(user_id))

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        return []  # sessions persist channel-side; no history replay

    # -- Lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    # -- Authorization ---------------------------------------------------

    def authorize(self, msg: InboundMessage) -> bool:
        """Deny-by-default DM policy. ``self`` (default) admits only the
        linked account's own messages; unknown policy values deny everyone."""
        sender_jid = wa_id_to_user_jid(msg.user_id)
        is_self = self._client.me.matches(sender_jid)

        if self._dm_policy == DM_POLICY_SELF:
            allowed = is_self
        elif self._dm_policy == DM_POLICY_ALLOWLIST:
            allowed = is_self or normalize_jid(sender_jid) in self._allowed
        elif self._dm_policy == DM_POLICY_OPEN:
            allowed = bool(msg.user_id)
        elif self._dm_policy == DM_POLICY_DISABLED:
            allowed = False
        else:
            logger.warning(
                "whatsapp: unknown dm_policy %r denies everyone (fail closed)",
                self._dm_policy,
            )
            allowed = False

        if not allowed:
            from kiro_crew.sel import sel

            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="whatsapp_transport.authorize",
                outcome="denied",
                source="whatsapp",
            )
        return allowed

    # -- Inbound adapter ---------------------------------------------------

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize one neonize MessageEv through the gauntlet (module doc)."""
        info = getattr(raw_envelope, "Info", None)
        source = getattr(info, "MessageSource", None) if info is not None else None
        if info is None or source is None:
            return

        text = self._extract_text(raw_envelope)
        if not text.strip():
            return  # media/reactions/system events: out of v1 scope

        chat = normalize_jid(jid_to_str(getattr(source, "Chat", None)))
        sender = normalize_jid(jid_to_str(getattr(source, "Sender", None)))
        sender_alt = normalize_jid(jid_to_str(getattr(source, "SenderAlt", None)))
        message_id = str(getattr(info, "ID", "") or "")
        from_me = bool(getattr(source, "IsFromMe", False))
        is_group = bool(getattr(source, "IsGroup", False)) or is_group_jid(chat)
        if not chat or not message_id:
            return

        # 2. Reconnect-replay flood gate.
        stamp = float(getattr(info, "Timestamp", 0) or 0)
        connected_at = self._client.connected_at
        if connected_at is not None and stamp and stamp < connected_at - _REPLAY_GRACE_S:
            logger.debug("whatsapp: dropping replayed history message %s", message_id)
            return

        # 3. Echo gate.
        if from_me and self.echo.is_own_echo(chat, message_id):
            return

        # 4. Group gate (before authorize: an unconfigured group must not
        #    even produce an audit row per message).
        verdict: GroupVerdict | None = None
        sender_is_operator = from_me or self._client.me.matches(sender, sender_alt)
        if is_group:
            verdict = self.group_gate.evaluate(
                chat,
                sender_is_operator=sender_is_operator,
                addressed=self._is_addressed(raw_envelope, chat, sender_is_operator),
            )
            if not verdict.respond:
                logger.debug("whatsapp: group %s drop (%s)", chat, verdict.reason)
                return

        # user_id: the operator's own commands attribute to the operator
        # (self-chat + fromMe in any chat); group members keep their number.
        user_id = jid_user(sender if not from_me else self._client.me.jid)
        msg = InboundMessage(
            channel_type="whatsapp",
            user_id=user_id,
            conversation_id=chat,
            text=text,
            is_mention=bool(verdict and not verdict.unprompted and is_group),
        )

        # 5. Authorize. Group flow authorizes the *conversation surface*:
        #    configured groups accept member questions (answer-only), so the
        #    DM policy applies to DMs and to group steering, not group Q&A.
        if is_group:
            if verdict is not None and not verdict.may_steer:
                from kiro_crew.whatsapp.commands import parse_command

                if parse_command(text):
                    return  # commands from non-operators die silently
        elif not self.authorize(msg):
            return

        if verdict is not None:
            self.pending_verdicts[id(msg)] = verdict
        try:
            await self._dispatch(msg)
        finally:
            self.pending_verdicts.pop(id(msg), None)

    def _extract_text(self, event: Any) -> str:
        content = getattr(event, "Message", None)
        if content is None:
            return ""
        conversation = str(getattr(content, "conversation", "") or "")
        if conversation:
            return conversation
        extended = getattr(content, "extendedTextMessage", None)
        if extended is not None:
            return str(getattr(extended, "text", "") or "")
        return ""

    def _is_addressed(self, event: Any, chat: str, sender_is_operator: bool) -> bool:
        """Mentioned (@-tag of the linked account) or replying to the agent's
        own message. The operator addressing their own agent in a group is
        always 'addressed'."""
        if sender_is_operator:
            return True
        content = getattr(event, "Message", None)
        extended = getattr(content, "extendedTextMessage", None) if content else None
        ctx = getattr(extended, "contextInfo", None) if extended is not None else None
        if ctx is None:
            return False
        me = self._client.me
        for mentioned in getattr(ctx, "mentionedJID", []) or []:
            if me.matches(str(mentioned)):
                return True
        participant = str(getattr(ctx, "participant", "") or "")
        stanza_id = str(getattr(ctx, "stanzaID", "") or "")
        if participant and me.matches(participant):
            return True  # replying to one of the agent's/operator's messages
        if stanza_id and chat and self.echo.is_own_echo(chat, stanza_id):
            return True  # quoted message ID is one we sent
        return False

    # -- Configured outbound targets ----------------------------------------

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets: list[ConfiguredChannelTarget] = []
        available = self._client.is_connected
        reason = "" if available else "WhatsApp is not connected (pair from Settings)"
        me = self._client.me.wa_id
        if me:
            targets.append(
                ConfiguredChannelTarget(
                    f"user:{me}", "WhatsApp · yourself", available, reason
                )
            )
        for jid in sorted(self._allowed):
            wa_id = jid_user(jid)
            targets.append(
                ConfiguredChannelTarget(
                    f"user:{wa_id}", f"WhatsApp DM · {wa_id}", available, reason
                )
            )
        return targets

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, sep, value = (target_id or "").partition(":")
        if kind != "user" or not sep or not value.strip():
            return None
        return await self.resolve_conversation(value.strip()), None
