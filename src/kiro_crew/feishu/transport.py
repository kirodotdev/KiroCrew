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
denied with a SEL audit record. Inside an allowed group a message is served
only when it @-mentions this app's bot (``LarkInbound.bot_mentioned``).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from kiro_crew.feishu.client import CHAT_GROUP, CHAT_P2P, LarkClient, LarkInbound
from kiro_crew.feishu.renderer import FEISHU_UPLOAD_LIMITS
from kiro_crew.messaging.attachments import channel_reads_no_attachments
from kiro_crew.messaging.dispatch import inbound_permitted
from kiro_crew.messaging.outbound_files import ExtractLimits, OutboundFile
from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Redelivery-dedup window. Crossing ``_SEEN_MAX`` trims back to
#: ``_SEEN_KEEP`` in one pass rather than evicting one id per arrival.
_SEEN_MAX = 500
_SEEN_KEEP = 200

# A dispatch callback consumes an authorised LarkInbound and drives a turn.
# The gateway supplies the real implementation.
DispatchFn = Callable[["LarkInbound"], Awaitable[None]]

# Feishu capabilities: no edit-in-place streaming (replies are one-shot REST
# calls), no interactive buttons in this v1 integration. A proactive send is a
# new message to an allow-listed open_id or group chat_id (``send_message``).
# Inbound files are read (transport_dispatch.py downloads them after every gate).
# max_message_chars reflects the safe 4 000-char cap defined in client.py.
# files_outbound: the renderer uploads a local image the answer references and
# replies with it as an image message (renderer.py::on_done).
FEISHU_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=False,
    reactions=False,
    files_inbound=True,
    files_outbound=True,
    rich_blocks=False,
    threads=False,
    max_message_chars=4000,
    max_buttons=0,
    supports_proactive_send=True,
    # This transport reads no message id back, so an empty return is SUCCESS and a
    # failure raises (see ``send_message``). Same contract as WeCom's.
    returns_message_id=False,
)

# The refused-inbound spool's route markers, naming the DM sender or group chat
# an entry came from. The restart notice is sent to that DM or group (see
# ``FeishuTransport._address``).
SPOOL_DM_ROUTE_PREFIX = "inbound-spool:dm:"
SPOOL_GROUP_ROUTE_PREFIX = "inbound-spool:group:"


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
        # Seen message-id window for redelivery dedup (lark's WS can redeliver).
        # An OrderedDict gives ARRIVAL-ORDERED eviction -- a plain set's
        # iteration order is arbitrary, so trimming it would drop random ids and
        # let an old redelivery through while evicting a fresh one. Only written
        # from ``receive``, i.e. on the event loop, so it needs no lock.
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self.capabilities = FEISHU_CAPABILITIES

    @property
    def client(self) -> LarkClient:
        """The underlying Lark client (held + exposed, not hidden)."""
        return self._client

    # -- Live config --------------------------------------------------------

    def reconfigure(self, section: Any) -> None:
        """Adopt a reloaded ``feishu`` config section's authorization fields.

        Called by the dispatcher's config applier so an allow-list or group-gate
        edit from the dashboard, the CLI or ``$EDITOR`` takes effect on the next
        frame instead of the next restart. Each frozenset is REPLACED wholesale
        so an in-flight authorisation decision keeps reading one consistent set.

        Fails closed on shape: a list-typed field that is not a list, or an
        ``allow_group`` that is not a bool, keeps the PREVIOUS value and logs at
        WARNING. The group gate stays a CONJUNCTION -- both ``allow_group`` and
        an allow-listed ``chat_id`` -- so widening one half alone still admits no
        group, and every change is SEL-audited by count rather than by id.
        """
        allowed, allowed_delta = self._reload_id_set(
            section, "allowed_open_ids", self._allowed, "open_id"
        )
        groups, groups_delta = self._reload_id_set(
            section, "allowed_group_ids", self._allowed_group_ids, "group chat_id"
        )
        # ADOPT before AUDIT, both sets: the audit is best-effort bookkeeping and
        # ``sel()`` can raise (its trust root is validated on first use), so an
        # audit that ran first would leave a revoked id authorized until SEL
        # recovered -- every retry re-hitting the same failure.
        self._allowed = allowed
        self._allowed_group_ids = groups
        self._audit_roster(allowed_delta, allowed, "allow_list_changed")
        self._audit_roster(groups_delta, groups, "group_allow_list_changed")
        allow_group = getattr(section, "allow_group", None)
        if not isinstance(allow_group, bool):
            logger.warning(
                "feishu: allow_group is not a bool in the reloaded config; keeping %r",
                self._allow_group,
            )
        elif allow_group != self._allow_group:
            self._allow_group = allow_group
            logger.warning("feishu: allow_group flipped to %r via config reload", allow_group)
            sel().log_api_access(
                caller="config",
                operation="feishu_transport.reconfigure",
                outcome="allow_group_enabled" if allow_group else "allow_group_disabled",
                source="feishu",
            )

    def _reload_id_set(
        self,
        section: Any,
        field_name: str,
        current: frozenset[str],
        label: str,
    ) -> tuple[frozenset[str], tuple[int, int] | None]:
        """One reloaded id allow-list: rebuilt, or *current* kept on a bad shape.

        Returns the set to adopt and its ``(added, removed)`` delta, or ``None``
        when nothing changed -- the caller adopts first and audits after.
        """
        raw = getattr(section, field_name, None)
        if not isinstance(raw, (list, tuple)):
            logger.warning(
                "feishu: %s is not a list in the reloaded config; keeping the previous "
                "allow-list (%d %s(s))",
                field_name,
                len(current),
                label,
            )
            return current, None
        updated = frozenset(str(x) for x in raw if x)
        if updated == current:
            return current, None
        added = len(updated - current)
        removed = len(current - updated)
        logger.info("feishu: %s reloaded (+%d/-%d %s(s))", field_name, added, removed, label)
        return updated, (added, removed)

    @staticmethod
    def _audit_roster(delta: tuple[int, int] | None, adopted: frozenset[str], outcome: str) -> None:
        """SEL-audit an adopted roster change by count. Runs AFTER adoption."""
        if delta is None:
            return
        added, removed = delta
        sel().log_api_access(
            caller="config",
            operation="feishu_transport.reconfigure",
            outcome=outcome,
            source="feishu",
            resources=f"added={added} removed={removed} size={len(adopted)}",
        )

    # -- Tier-1 core --------------------------------------------------------

    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        # Always a NEW message, to the address ``_address`` reads off the live
        # rosters; an id it cannot place raises rather than guess.
        #
        # RAISE on a refused send rather than returning. This transport reads no
        # message id back, so the return value cannot express failure -- which is
        # what ``returns_message_id=False`` tells a caller, and it makes "nothing
        # raised" the ONLY delivery signal there is. Discarding the client's answer
        # left a dropped message looking exactly like a delivered one, which is how
        # a mirror leg persists a link and reports success for a reply the user
        # never saw. The renderer's own send already raises for this reason.
        receive_id_type, receive_id = self._address(conversation_id, thread_id)
        if not receive_id_type:
            raise RuntimeError("not a configured Feishu DM or group")
        if not await self._client.send_new(receive_id_type, receive_id, content):
            raise RuntimeError(f"Feishu message was not delivered ({receive_id_type})")
        return ""

    async def outbound_image_limits(
        self, conversation_id: str, thread_id: str | None = None
    ) -> ExtractLimits | None:
        """The budgets an upload to this conversation is extracted under, or None.

        Asked by the dashboard -> channel reply mirror BEFORE it extracts, so a
        conversation that takes no upload keeps its references as text, exactly
        as a channel-driven turn there does. The renderer's own budgets, for an
        allow-listed DM only: the dispatcher allows uploads for a ``p2p`` chat
        alone, because a group's members are not all on ``allowed_open_ids``.
        """
        if self._address(conversation_id, thread_id)[0] != "open_id":
            return None
        return FEISHU_UPLOAD_LIMITS

    async def send_outbound_image(
        self, conversation_id: str, file: OutboundFile, *, thread_id: str | None = None
    ) -> bool:
        """Send one extracted image as a NEW message. True only when it landed.

        The proactive counterpart of the renderer's image reply, used by the
        dashboard -> channel reply mirror. *file* carries the bytes extraction
        validated; its path is never re-opened. Re-decided against the live
        rosters at the send, as :meth:`send_message` is: an address that is not
        an allow-listed DM (see :meth:`outbound_image_limits`) answers False.
        """
        receive_id_type, receive_id = self._address(conversation_id, thread_id)
        if receive_id_type != "open_id" or not file.data:
            return False
        return await self._client.send_new_image(receive_id_type, receive_id, file.data)

    def _address(self, conversation_id: str, thread_id: str | None) -> tuple[str, str]:
        """``(receive_id_type, receive_id)`` a proactive send goes to, or ``("", "")``.

        Without a ``thread_id``, ``conversation_id`` is the address. With an
        inbound-spool route marker, the marker names the DM sender or group and
        the send goes THERE, never as a reply to the spooled message id: a link is
        rewritable, and a message id names no chat this transport can check.
        """
        if not thread_id:
            receive_id, wanted = conversation_id, ""
        elif thread_id.startswith(SPOOL_DM_ROUTE_PREFIX):
            receive_id, wanted = thread_id[len(SPOOL_DM_ROUTE_PREFIX) :], "open_id"
        elif thread_id.startswith(SPOOL_GROUP_ROUTE_PREFIX):
            receive_id, wanted = thread_id[len(SPOOL_GROUP_ROUTE_PREFIX) :], "chat_id"
        else:
            return "", ""
        kind = self._receive_id_type(receive_id)
        return (kind, receive_id) if kind and wanted in ("", kind) else ("", "")

    def _receive_id_type(self, receive_id: str) -> str:
        """``"open_id"`` for an allow-listed DM, ``"chat_id"`` for an allowed group, else ``""``.

        Read from the live rosters, so an id revoked since its link was written has
        no address at all.
        """
        if receive_id in self._allowed:
            return "open_id"
        if self._allow_group and receive_id in self._allowed_group_ids:
            return "chat_id"
        return ""

    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Re-decide a proactive send against the live rosters. Fails closed.

        The same placement :meth:`send_message` sends to, so the two cannot
        disagree; *principal* is not needed because the address IS the roster id.
        """
        return bool(self._address(conversation_id, thread_id)[0])

    async def resolve_conversation(self, user_id: str) -> str:
        # A DM is addressed by the person's open_id itself (receive_id_type=open_id).
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # The Feishu Bot API does not expose readable DM history; sessions
        # persist via conversation_log instead.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets = [
            ConfiguredChannelTarget(f"user:{oid}", f"Feishu DM · {oid}")
            for oid in sorted(self._allowed)
        ]
        if self._allow_group:
            targets += [
                ConfiguredChannelTarget(f"group:{cid}", f"Feishu group · {cid}")
                for cid in sorted(self._allowed_group_ids)
            ]
        return targets

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        """Re-validate an advertised target against the live rosters.

        Checked here, not only where the id was minted: the id travels through the
        browser and the LLM, and the config may have narrowed since.
        """
        kind, _, value = target_id.partition(":")
        wanted = {"user": "open_id", "group": "chat_id"}.get(kind)
        return (value, None) if wanted and self._receive_id_type(value) == wanted else None

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
        (fail-closed for non-p2p contexts, and for a group message that does
        not mention the bot), then the user allow-list, and finally hand an
        authorised message to the dispatcher.
        """
        if not isinstance(raw_envelope, LarkInbound):
            return
        inbound = raw_envelope
        # An unreadable message (``unsupported_type`` set, ``text`` empty) is
        # NOT dropped here: it runs the same gates a turn would and is answered
        # at the bottom, and a message carrying files drives a turn like text.
        # Everything else with no text still stops -- a frame whose body
        # resolved to nothing has no instruction and nothing to say about it.
        if not inbound.text and not inbound.unsupported_type and not inbound.resources:
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
            # Addressed-to-the-bot gate (fail closed). An app that holds a
            # broader read grant receives every message in the group, and the
            # sender allow-list says who MAY drive a turn, not that this
            # message asked for one. Only a message whose mentions name this
            # bot's open_id is served; an unresolved bot id reads as "not
            # mentioned". Chatter not addressed to the bot is no access
            # attempt, so it is dropped before ``authorize`` and leaves no
            # audit record.
            if not inbound.bot_mentioned:
                logger.debug(
                    "Feishu group message ignored: the bot is not mentioned (message_id=%s)",
                    inbound.message_id,
                )
                return

        # The neutral InboundMessage carries everything authorize() reads. The
        # routing fields (chat_type / chat_id / message_id) stay on the
        # LarkInbound the dispatcher receives, so there is nothing for a
        # channel-specific subclass to add here.
        msg = InboundMessage(
            channel_type="feishu",
            user_id=inbound.open_id,
            conversation_id=inbound.message_id,
            text=inbound.text,
        )
        if not self.authorize(msg):
            return

        # Redelivery dedup, deliberately the LAST gate. lark's WS may redeliver
        # an event, and a repeated turn re-runs tool side effects that are not
        # idempotent -- so this has to exist. But it is placed after every drop
        # decision on purpose: recording earlier lets traffic that will never
        # drive a turn (an unauthorized sender, a group the bot merely sits in,
        # a sticker) consume the window and evict an AUTHORIZED id, which is the
        # redelivery this is supposed to catch.
        #
        # No lock: ``receive`` is driven on the event loop (the WS thread hands
        # off via ``run_coroutine_threadsafe``), so the loop's single thread is
        # what makes this atomic -- provided there is NO ``await`` between the
        # membership test and the insert. Keep it that way.
        msg_id = inbound.message_id
        if msg_id:
            if msg_id in self._seen:
                return
            self._seen[msg_id] = None
            if len(self._seen) > _SEEN_MAX:
                # popitem(last=False) evicts the OLDEST arrival.
                while len(self._seen) > _SEEN_KEEP:
                    self._seen.popitem(last=False)

        # Answered here, and only here, for three reasons that are each a gate
        # above this line: an unauthorised sender learns nothing (``authorize``
        # already returned), a group the bot merely sits in stays silent (the
        # group gate), and a redelivered frame does not answer twice (the dedup
        # window, which this deliberately sits after).
        if inbound.unsupported_type:
            # Per-message governance ceiling. The reply ``return``s before
            # ``self._dispatch``, so it never reaches the dispatcher's own
            # ``inbound_permitted("feishu")`` gate -- without this an
            # administratively-disabled channel would still emit a reply.
            # Rechecked here (not just at connect) so a host-profile deny
            # added while the transport is live silences the reply too.
            if not await inbound_permitted("feishu"):
                return
            try:
                # The client's reply verb: ``send_message`` only ever sends a
                # new message to a configured DM or group.
                if not await self._client.send_reply(
                    inbound.message_id,
                    channel_reads_no_attachments(inbound.unsupported_type),
                ):
                    raise RuntimeError("Feishu reply was not delivered")
            except Exception:
                # LOG the failure -- do not re-raise. ``_deliver`` dispatches
                # us through ``run_coroutine_threadsafe`` without awaiting the
                # future, so the WS frame is acked the moment that handler
                # returns; a re-raise here would surface only in a future
                # nothing reads, and no redelivery is held back for it to
                # drive. Re-dropping ``_seen[msg_id]`` to court a redelivery
                # would therefore chase a frame that never comes. Logging is
                # the one trace a failed reply can actually leave.
                logger.exception(
                    "feishu: failed to answer unreadable attachment (message_id=%s, type=%s)",
                    msg_id,
                    inbound.unsupported_type,
                )
            return

        if self._dispatch is not None:
            # Received from a person: its start is FOREGROUND (kiro_crew.start_priority).
            inbound.person_origin = True
            await self._dispatch(inbound)
