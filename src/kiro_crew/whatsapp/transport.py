"""Layer 1 — WhatsApp (QR-linked personal account) as a ``MessagingTransport``.

Wraps :class:`kiro_crew.whatsapp.client.WhatsAppClient` in the channel-neutral
transport contract. Dependency direction is ``whatsapp -> messaging``
(allowed); ``messaging`` never imports ``whatsapp``.

The inbound pipeline in :meth:`WhatsAppTransport.receive` is a gauntlet every
event must survive, in order:

1. **shape**: the message is unwrapped (ephemeral, view-once and edited
   messages carry the real one inside) and classified by
   :mod:`kiro_crew.whatsapp.media`. Anything that yields neither text, nor
   ingestible media, nor a note worth showing is dropped;
2. **flood gate**: messages older than the connection moment are history
   replay after a reconnect. Never answer a backlog (marks nothing read);
3. **echo gate**: a ``from_me`` message whose ID we sent is our own echo;
   a ``from_me`` message we did NOT send is the operator typing (self-chat
   command surface);
4. **group gate**: group chats are dropped unless configured, then gated by
   mode/mention/cooldown (:mod:`kiro_crew.whatsapp.group_gate`);
5. **authorize**: deny-by-default DM policy (``self`` default) with SEL audit
   on every denial;
6. **media fetch**, and only here, and only for an INDIVIDUALLY admitted sender.
   Downloading before this point would let anyone who can message the number
   trigger an authenticated fetch on the operator's host with no authorization
   behind it. Step 5 answers that for a DM but not for a group, where it
   authorizes the conversation surface rather than the sender, so the fetch takes
   its own test (:meth:`WhatsAppTransport._may_fetch_media`): the linked account
   or an explicitly allowed number, never ``dm_policy`` alone.

Capabilities (personal account over the Web protocol): the reply streams by
editing one bubble, because this protocol exposes an edit where the Business
Cloud API does not; media rides the shared ingest and upload paths; reactions
carry phase receipts. ``max_buttons=0``, so an ``[OPTIONS:]`` trailer degrades to
a numbered list answered by typing. That is a deliberately conservative choice,
NOT a platform ceiling: the pinned wheel ships a complete interactive-message
builder and a poll builder, and what is unverified is whether a recipient's client
renders a native-flow message sent from a PERSONAL linked device rather than a
Business account. Recording it as impossible would close the door on every future
picker here, so it is recorded as unverified. Unlike
the Business Cloud API there is no 24-hour window, so
``supports_proactive_send=True`` and reminders work.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from kiro_crew.messaging.attachments import append_attachment_context, cleanup
from kiro_crew.messaging.dispatch import inbound_permitted
from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.whatsapp.attachments import ingest_media
from kiro_crew.whatsapp.client import WhatsAppClient
from kiro_crew.whatsapp.commands import parse_command
from kiro_crew.whatsapp.echo import EchoTracker
from kiro_crew.whatsapp.group_gate import GroupGate, GroupVerdict
from kiro_crew.whatsapp.jids import (
    LID_SERVER,
    is_group_jid,
    jid_to_str,
    jid_user,
    normalize_jid,
    wa_id_to_user_jid,
)
from kiro_crew.whatsapp.media import describe, unsupported_note, unwrap_message
from kiro_crew.whatsapp.renderer import WHATSAPP_CHUNK_LIMIT

logger = logging.getLogger(__name__)

#: seconds of pre-connection history still answered after (re)connect. Events
#: older than ``connected_at - GRACE`` are reconnect replay, not live traffic.
_REPLAY_GRACE_S = 60.0

#: Distinct ``@lid`` senders whose phone JID is remembered. An alias is stable
#: for the life of an account, so this only bounds memory against a group with
#: churning membership; a miss costs one resolver round-trip, never a denial.
_ALIAS_CACHE_MAX = 512

#: Said in the group when a member's attachment is not fetched. Chat content, so
#: no catalog path applies; it lives here as the owning module's constant.
NOTE_MEDIA_NOT_ADMITTED = (
    "(Attachment not opened: files are only downloaded for the account owner "
    "and explicitly allowed numbers.)"
)

WHATSAPP_CAPABILITIES = TransportCapabilities(
    # The Web protocol exposes an edit (the Business Cloud API does not), so the
    # renderer streams by editing one bubble and sealing it when the text
    # outgrows a message or the 20-minute edit window closes. Declared True only
    # because the behaviour exists: these flags are a claim other code trusts.
    streaming=True,
    edit=True,
    reactions=True,
    # Images, stickers, voice notes, audio and documents ride the shared ingest
    # path; the fetch happens only after the authorization gauntlet.
    files_inbound=True,
    # The renderer runs the shared outbound extractor at the seal and uploads
    # each raster through client.send_image_bytes; a refusal is reported rather
    # than dropped. Inbound stays False until the download half lands.
    files_outbound=True,
    rich_blocks=False,
    threads=False,
    max_message_chars=WHATSAPP_CHUNK_LIMIT,
    max_buttons=0,
    supports_proactive_send=True,
    # Declared explicitly rather than left to the default. Inbound builds a
    # session key from the chat JID and never resolves a dashboard mirror
    # binding, so a connect from the dashboard is outbound-only; claiming
    # otherwise would promise a two-way link whose replies land in a different
    # session. Spelling it out is also what puts this channel under
    # test_capability_ledger's roster instead of silently outside it.
    supports_session_resume=False,
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
        #: ``@lid`` alias -> phone JID, resolved once per sender (see
        #: ``_canonical_sender``). Confined to the gateway event loop like the
        #: echo tracker, so no lock is taken.
        self._alias: OrderedDict[str, str] = OrderedDict()
        #: verdict metadata for the dispatcher, keyed by id(InboundMessage) —
        #: set in receive() immediately before the dispatch await.
        self.pending_verdicts: dict[int, GroupVerdict] = {}
        #: "this message came from the linked account", decided in receive()
        #: from the full multi-device identity and read back by is_operator().
        #: Same keying and same lifetime as pending_verdicts.
        self.pending_operator: dict[int, bool] = {}
        #: The platform message id of the inbound message, so the dispatcher can
        #: draw a phase reaction on it. InboundMessage carries no id field: it is
        #: a channel-neutral shape and a WhatsApp stanza id means nothing to the
        #: others, so it rides here rather than widening the shared contract.
        self.pending_message_id: dict[int, str] = {}
        client.on_message = self.receive

    # -- Tier-1 core ---------------------------------------------------

    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        """Chunked send. Every id is remembered for echo discipline the moment
        its chunk lands, via the ``on_sent`` callback, so an echo arriving during
        the inter-chunk delay cannot race the tracker."""
        jid = normalize_jid(conversation_id)
        last_id = ""
        for message_id in await self._send_tracked(jid, content):
            last_id = message_id
        return last_id

    async def _send_tracked(self, jid: str, content: str) -> list[str]:
        return await self._client.send_text(
            jid, content, on_sent=lambda message_id: self.echo.remember(jid, message_id)
        )

    async def send_image(self, conversation_id: str, data: bytes, caption: str = "") -> str:
        """Upload one image, echo-tracked like every other send this makes."""
        jid = normalize_jid(conversation_id)
        return await self._client.send_image_bytes(
            jid, data, caption, on_sent=lambda message_id: self.echo.remember(jid, message_id)
        )

    async def react(
        self, conversation_id: str, sender_id: str, message_id: str, emoji: str
    ) -> None:
        """React to a message, echo-tracked.

        A reaction is a MESSAGE on this protocol, so it comes back on the event
        stream with ``from_me`` set like anything else the account sends. Tracking
        its id is what stops the channel answering its own receipt.
        """
        jid = normalize_jid(conversation_id)
        sent_id = await self._client.react(jid, sender_id, message_id, emoji)
        if sent_id:
            self.echo.remember(jid, sent_id)

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

    def _may_fetch_media(self, msg: InboundMessage, *, is_group: bool) -> bool:
        """Whether this sender may cause bytes to be downloaded onto the host.

        A DM sender has already passed :meth:`authorize`, so the DM policy is the
        operator's own answer there and this adds nothing. A GROUP member has not:
        step 5 authorizes the group SURFACE, so membership alone would let anyone
        in a configured group trigger an authenticated whole-blob fetch into the
        gateway's heap at will. In ``rules`` mode an unaddressed message already
        returns ``respond=True``, and the per-group cooldown does not bound it,
        because the cooldown only starts once a reply actually delivered and a
        sentinel-silenced turn never does.

        Group media therefore requires INDIVIDUAL admission: the linked account,
        or a number the operator listed. It deliberately does NOT consult
        ``dm_policy``, because ``open`` resolves to "anyone with a user id" and
        would hand the capability straight back. Being admitted to the
        conversation is not being admitted to the machine.
        """
        if not is_group:
            return True
        sender_jid = wa_id_to_user_jid(msg.user_id)
        if self._client.me.matches(sender_jid):
            return True
        return normalize_jid(sender_jid) in self._allowed

    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Re-decide a PROACTIVE send under the live ``dm_policy``. Fails closed.

        Answerable because the conversation id IS the peer's normalized JID (see
        :meth:`resolve_conversation`), so a persisted link carries the same principal
        :meth:`authorize` checks. Asks the same question that method asks and answers
        it the same way, including denying an unrecognized policy rather than falling
        through to ``open``.

        ``self`` is the default policy and admits only the linked account, so a
        proactive send under it is authorized exactly when the conversation is that
        account's own thread.
        """
        if not conversation_id:
            return False
        jid = normalize_jid(conversation_id)
        if self._client.me.matches(jid):
            return True
        if self._dm_policy == DM_POLICY_OPEN:
            return True
        if self._dm_policy == DM_POLICY_ALLOWLIST:
            return jid in self._allowed
        # DM_POLICY_SELF reached here means a non-self conversation; DISABLED and any
        # unrecognized value deny outright.
        return False

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
            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="whatsapp_transport.authorize",
                outcome="denied",
                source="whatsapp",
            )
        return allowed

    def is_operator(self, msg: InboundMessage) -> bool:
        """Whether *msg* came from the linked account itself.

        The gate for everything that ACTS AS the agent rather than merely talks
        to it: resolving a tool approval, and steering the session with a
        command. Deliberately NARROWER than :meth:`authorize`, and not derived
        from ``dm_policy``: ``open`` admits a stranger to CHAT, and a configured
        group admits its members, but neither should reset the operator's
        session or authorize a command on their machine. "Who may talk to the
        agent" and "who may act as the agent" must not be the same set.

        The command case is reachable on shipped config, not hypothetical: with
        ``messaging.dm_scope = "unified"`` every direct DM collapses into one
        ``unified:{agent}`` bucket, so under ``allowlist`` or ``open`` a peer's
        ``/new`` would bump the generation on the conversation the OPERATOR is
        using.

        Reads the verdict :meth:`receive` already reached rather than re-deriving
        it. ``receive`` decides operator identity from the full multi-device
        picture -- ``IsFromMe`` plus both ``Sender`` and ``SenderAlt`` against
        the account's phone JID *and* its ``@lid`` alias -- while all that
        survives onto ``InboundMessage`` is a bare user part with its server
        dropped. Re-deriving from that string would answer a narrower question
        than the one already answered, and would answer it wrong for an operator
        addressed by an alias.

        Fails closed on an unknown message: an entry is recorded for the exact
        object ``receive`` dispatched, so anything else -- a replay, a synthetic
        message, a caller reaching past the gauntlet -- is not the operator.
        """
        return self.pending_operator.get(id(msg), False)

    LID_SUFFIX = f"@{LID_SERVER}"

    async def _canonical_sender(self, sender: str) -> str:
        """*sender* as a phone JID, resolving an ``@lid`` alias once per sender.

        WhatsApp multi-device addresses a sender either by their phone number
        or by their Linked Identity, and the two user parts are UNRELATED
        strings. Everything downstream that compares a sender against operator
        CONFIG -- the DM allowlist, and the audit trail that has to name the
        same human twice -- is written in phone numbers, so the alias is folded
        here, at the edge, rather than at each comparison. Doing it per
        comparison is how one call site gets the resolution and the next does
        not.

        A failed lookup returns *sender* unchanged, so the allowlist still
        fails CLOSED on an unresolvable alias.
        """
        if not sender.endswith(self.LID_SUFFIX):
            return sender
        cached = self._alias.get(sender)
        if cached is not None:
            self._alias.move_to_end(sender)
            return cached or sender
        phone = normalize_jid(await self._client.phone_for_lid(sender))
        self._alias[sender] = phone
        while len(self._alias) > _ALIAS_CACHE_MAX:
            self._alias.popitem(last=False)
        return phone or sender

    # -- Inbound adapter ---------------------------------------------------

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize one neonize MessageEv through the gauntlet (module doc)."""
        info = getattr(raw_envelope, "Info", None)
        source = getattr(info, "MessageSource", None) if info is not None else None
        if info is None or source is None:
            return

        # Unwrap first: an ephemeral, view-once or edited message carries the
        # real one inside, and every read below (text, media, and the mention
        # context) is blind to it otherwise.
        inner = unwrap_message(getattr(raw_envelope, "Message", None))
        desc = describe(inner)
        text = desc.caption
        skipped_note = unsupported_note(desc)
        if not text.strip() and not desc.has_media and not skipped_note:
            return  # a system event with nothing a turn could act on

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

        # 3b. The operator talking to somebody else. A ``from_me`` message in a
        #     direct chat that is not the self-chat is the operator texting a
        #     contact, not addressing the agent: answering it would put the agent
        #     into their private conversation and reply in that contact's chat.
        #     Groups are exempt: a configured group is where the operator does
        #     address the agent, gated by mention or rules.
        is_group_early = bool(getattr(source, "IsGroup", False)) or is_group_jid(chat)
        if from_me and not is_group_early and not self._client.me.matches(chat):
            logger.debug("whatsapp: own outgoing message to %s is not a command", chat)
            return

        # 4. Group gate (before authorize: an unconfigured group must not
        #    even produce an audit row per message).
        verdict: GroupVerdict | None = None
        # ``from_me`` means the account sent it, which includes the operator
        # texting an ordinary contact from their phone. That is NOT a command: the
        # agent would answer their private conversation and reply into the
        # contact's chat. Operator authority in a DIRECT chat therefore requires
        # the self-chat, which is the command surface; in a configured group the
        # operator addresses the agent normally, gated by mention or rules.
        self_chat = self._client.me.matches(chat)
        sender_is_operator = (from_me or self._client.me.matches(sender, sender_alt)) and (
            is_group or self_chat
        )
        if is_group:
            verdict = self.group_gate.evaluate(
                chat,
                sender_is_operator=sender_is_operator,
                addressed=self._is_addressed(inner, chat, sender_is_operator),
            )
            if not verdict.respond:
                logger.debug("whatsapp: group %s drop (%s)", chat, verdict.reason)
                return

        # user_id: the operator's own commands attribute to the operator
        # (self-chat + fromMe in any chat); group members keep their number.
        # A peer addressed by their ``@lid`` alias is folded to the phone JID
        # first, because the DM allowlist is written in phone numbers and an
        # unresolved alias reads as "this person is not allowed".
        if from_me:
            attributed = self._client.me.jid
        else:
            attributed = await self._canonical_sender(sender)
        user_id = jid_user(attributed)
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
                if parse_command(text):
                    return  # commands from non-operators die silently
        elif not self.authorize(msg):
            return

        # The GOVERNANCE gate, and it has to be here rather than only in the
        # dispatcher: the fetch below is an authenticated download onto the
        # operator's host, and the dispatcher's own check runs after this method
        # has already performed it. A `channels` deny added while the transport is
        # live must stop the download, not merely the reply.
        #
        # `has_media` comes from the ENVELOPE, which is why the cancellation
        # exemption cannot be fooled by a caption: a policy-denied media message
        # captioned `/stop` is still media, so it stays gated and nothing is
        # fetched. Reading `msg.attachments` here instead would exempt it, because
        # nothing has been ingested yet at this point in the method.
        if not await inbound_permitted("whatsapp", text=text, has_attachments=desc.has_media):
            return

        # Media is fetched only now, AFTER the group gate, authorize and the
        # governance gate. Doing it earlier would let any stranger who can message
        # the number trigger an authenticated download on the operator's host,
        # which is a remote-triggered fetch with no authorization behind it.
        temp_paths: list[str] = []
        if desc.has_media and not self._may_fetch_media(msg, is_group=is_group):
            # Refusing is said out loud: silence reads as the agent ignoring a
            # photo the sender believes it received.
            skipped_note = NOTE_MEDIA_NOT_ADMITTED
        elif desc.has_media:
            result = await ingest_media(self._client, inner, desc, message_id)
            temp_paths = list(result.temp_paths)
            msg.text = append_attachment_context(msg.text, result)
            # Populated because downstream readers gate on it (the shared
            # cancellation exemption among them), and a field that is always empty
            # is a guarantee that silently does not hold.
            msg.attachments = list(temp_paths)
        elif skipped_note:
            # Seen and skipped, said out loud: silence reads as the agent
            # ignoring a photo the user believes it received.
            msg.text = f"{msg.text}\n\n{skipped_note}".strip() if msg.text else skipped_note

        if verdict is not None:
            self.pending_verdicts[id(msg)] = verdict
        self.pending_operator[id(msg)] = sender_is_operator
        self.pending_message_id[id(msg)] = message_id
        try:
            await self._dispatch(msg)
        finally:
            # The shared ingest hands path ownership to the caller.
            cleanup(temp_paths)
            self.pending_verdicts.pop(id(msg), None)
            self.pending_operator.pop(id(msg), None)
            self.pending_message_id.pop(id(msg), None)

    def _is_addressed(self, message: Any, chat: str, sender_is_operator: bool) -> bool:
        """Mentioned (@-tag of the linked account) or replying to the agent's
        own message. The operator addressing their own agent in a group is
        always 'addressed'.

        Takes the UNWRAPPED message, not the envelope: ``contextInfo`` carries the
        mention list, and it is invisible when the message arrived inside an
        ephemeral or view-once carrier. Reading the envelope made an @-mention in
        a disappearing group message read as "not addressed", so the agent stayed
        silent exactly when it was called on.
        """
        if sender_is_operator:
            return True
        extended = getattr(message, "extendedTextMessage", None) if message else None
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
                ConfiguredChannelTarget(f"user:{me}", "WhatsApp · yourself", available, reason)
            )
        for jid in sorted(self._allowed):
            wa_id = jid_user(jid)
            targets.append(
                ConfiguredChannelTarget(
                    f"user:{wa_id}", f"WhatsApp DM · {wa_id}", available, reason
                )
            )
        return targets

    def _proactive_targets(self) -> frozenset[str]:
        """Conversation JIDs a CONFIGURED proactive send may address: the linked
        account plus the DM allowlist, and nothing else.

        Deliberately the same two sources :meth:`configured_targets` lists from, so
        what the dashboard OFFERS and what it will ACCEPT cannot drift apart.
        """
        jids = set(self._allowed)
        me = self._client.me.wa_id
        if me:
            jids.add(normalize_jid(wa_id_to_user_jid(me)))
        return frozenset(jids)

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, sep, value = (target_id or "").partition(":")
        if kind != "user" or not sep or not value.strip():
            return None
        conversation = await self.resolve_conversation(value.strip())
        # Deny-by-default on membership, as every sibling transport does. This
        # resolver is the ONLY allowlist check on the dashboard mirror-link path,
        # which round-trips a chosen id back through it: resolving an id the list
        # never offered would let a proactive send open a conversation with an
        # arbitrary phone number, and `dm_policy` never sees that path.
        if conversation not in self._proactive_targets():
            return None
        return conversation, None
