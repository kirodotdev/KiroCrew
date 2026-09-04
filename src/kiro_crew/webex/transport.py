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
* Group spaces are opt-in and named, fail closed: in a space the bot only sees
  @mentions, but a reply lands in the space -- exposing tool output to every
  member, including people the email allow-list excludes. So a space is answered
  only when the operator enabled group rooms AND named that room; every other
  room type is denied and audited. A direct room needs neither.
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
from kiro_crew.webex.cards import MAX_CARD_ACTIONS
from kiro_crew.webex.client import WEBEX_MAX_TEXT, WebexClient, WebexInbound

logger = logging.getLogger(__name__)

DispatchFn = Callable[[WebexInbound], Awaitable[None]]

# Webex room types, named rather than inlined so the gate reads as membership.
ROOM_DIRECT = "direct"
ROOM_GROUP = "group"

# How many messages ``fetch_history`` reads. Webex itself caps ``max`` at 100
# when a bot filters a group room by mention, so this stays under that.
_HISTORY_MAX = 50

# Webex capabilities: no streaming (10-edit cap per message), but edits exist
# (budgeted status placeholder), tappable chips DO exist as Adaptive Card
# Action.Submit buttons (capped at MAX_CARD_ACTIONS, past which the shared
# messaging.renderer.apply_options_cap degrades the remainder to numbered text),
# and proactive send is fine (a bot may post to a room / person at any time). The
# numbered text always ships alongside a card, because the inbound half of a
# press travels over the undocumented device websocket.
#
# Webex caps messages in UTF-8 BYTES, and ``max_message_chars`` is a CHARACTER
# count, so the only SAFE char value is the byte budget over four — the worst
# case for a 4-byte code point. That value is 4x pessimistic for ASCII, which is
# why ``max_message_bytes`` exists: a caller that can measure bytes uses it and
# gets the real capacity, while the char floor stays correct for one that cannot.
WEBEX_SAFE_MESSAGE_CHARS = WEBEX_MAX_TEXT // 4

WEBEX_CAPABILITIES = TransportCapabilities(
    # No streaming: Webex caps a message at 10 edits, so a typewriter
    # edit-stream is structurally impossible rather than merely unimplemented.
    streaming=False,
    edit=True,
    # No reactions API exists in the Webex Messaging surface at all, so Slack's
    # reaction-as-acknowledgement idiom has no analogue here.
    reactions=False,
    files_inbound=True,
    files_outbound=True,
    # Adaptive Cards are Webex's Block Kit analogue and the renderer sends them.
    rich_blocks=True,
    threads=True,
    # Webex renders pipe tables literally.
    table_mode=TABLE_POLICY_AUTO,
    max_message_chars=WEBEX_SAFE_MESSAGE_CHARS,
    max_message_bytes=WEBEX_MAX_TEXT,
    # No broadcast-mention grammar, and the allow-list IS email addresses: the
    # shared defang would insert a ZWSP after every ``@`` and make every address
    # the agent prints uncopyable. ``webex_display_safe`` already omits it on the
    # renderer path; this is the same fact for the channel-neutral sinks.
    mention_grammars=False,
    # One Action.Submit per choice on an Adaptive Card. Webex's own overview says
    # five; past that the renderer keeps the numbered-text form, which has no cap.
    max_buttons=MAX_CARD_ACTIONS,
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
        allow_group_rooms: bool = False,
        allowed_room_ids: Iterable[str] = (),
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the (lowercased) allow-list so it can't
        # mutate under an in-flight decision.
        self._allowed: frozenset[str] = frozenset(e.lower() for e in allowed_emails if e)
        self._allow_group_rooms = allow_group_rooms
        self._allowed_rooms: frozenset[str] = frozenset(r for r in allowed_room_ids if r)
        self._dispatch = dispatch
        self.capabilities = WEBEX_CAPABILITIES

    @property
    def client(self) -> WebexClient:
        """The underlying Webex client (held + exposed, not hidden)."""
        return self._client

    @property
    def dispatcher(self) -> Any:
        """The ``WebexDispatcher`` whose bound ``handle_message`` was wired as
        ``dispatch``, or ``None`` when unwired (tests) or wired to a plain
        function.

        The one sanctioned way for an out-of-band injector (the AutoNudge fire
        path) to reach the dispatcher's authorization and session-key contract.
        Reaching into ``_dispatch`` from outside this class is a rename away from
        silently killing active loops.
        """
        return getattr(self._dispatch, "__self__", None)

    def is_authorized(self, email: str) -> bool:
        """Whether *email* may drive a turn on this channel. Deny-by-default.

        Exposed for the same reason as :attr:`dispatcher`: a synthetic injection
        bypasses ``receive``, so the injector has to re-run the check itself, and
        the allow-list can shrink after a loop was created.
        """
        return bool(email) and email.lower() in self._allowed

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        # thread_id is HONOURED now that ``threads=True`` is declared: dropping it
        # while declaring the capability is exactly the dishonesty the capability
        # ledger exists to catch. Webex threads are flat, so the id is the root.
        mid = await self._client.send_message(conversation_id, content, parent_id=thread_id)
        return mid or ""

    async def resolve_conversation(self, user_id: str) -> str:
        # The user id IS the email; the client's send path maps an
        # email-shaped conversation id onto toPersonEmail, which opens or
        # reuses the 1:1 space server-side.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        """Recent messages in a room, oldest first.

        Conversation CONTINUITY comes from ``conversation_log``, not from here —
        this exists for a caller that wants the room's own recent context (a
        freshly linked space, say). Bounded by ``_HISTORY_MAX`` because a room can
        hold years of messages and none of the callers want them all.

        A failure yields ``[]`` rather than raising: history is supplementary, and
        a transport read must not be able to fail a turn.
        """
        params = f"?roomId={conversation_id}&max={_HISTORY_MAX}"
        if thread_id:
            params += f"&parentId={thread_id}"
        result = await self._client.list_messages(params)
        out: list[InboundMessage] = []
        # Webex returns newest-first; every caller reads a transcript forwards.
        for item in reversed(result):
            out.append(
                InboundMessage(
                    channel_type="webex",
                    user_id=str(item.get("personEmail") or "").lower(),
                    conversation_id=str(item.get("roomId") or conversation_id),
                    text=str(item.get("text") or ""),
                    thread_id=str(item.get("parentId") or "") or None,
                    attachments=list(item.get("files") or []),
                )
            )
        return out

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets = [
            ConfiguredChannelTarget(f"user:{email}", f"Webex DM · {email}")
            for email in sorted(self._allowed)
        ]
        if self._allow_group_rooms:
            targets += [
                ConfiguredChannelTarget(f"room:{room}", f"Webex space · {room[-12:]}")
                for room in sorted(self._allowed_rooms)
            ]
        return targets

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        """Re-validate an opaque target id at the side-effect boundary.

        The allow-lists are checked HERE, not only where the id was minted: the id
        travels through the browser and the LLM, and the config may have narrowed
        since it was issued.
        """
        kind, separator, value = target_id.partition(":")
        if not separator:
            return None
        if kind == "user" and value.lower() in self._allowed:
            return await self.resolve_conversation(value), None
        if kind == "room" and self._allow_group_rooms and value in self._allowed_rooms:
            return value, None
        return None

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Re-check the roster the ROUTE belongs to. Fails closed on both.

        Webex has two audiences, so this dispatches on the route rather than
        testing one id against the wrong set -- the same shape Discord's two
        rosters take.

        A **space** route is recognised by its conversation id being in
        ``_allowed_rooms`` with ``_allow_group_rooms`` still on, which is exactly
        the pair :meth:`room_permitted` gates inbound on, so outbound stays as
        tight as inbound and no tighter. It has to be checked here because a space
        has no principal to check: its route is keyed ``(chat_id, thread_id)``
        under the ``forum`` namespace, and ``chat_runner._session_principal`` names
        a peer only for a 1:1 ``direct`` key. Refusing for want of a principal
        would therefore drop EVERY proactive send into an allow-listed space --
        the dashboard mirror, cron results, subagent completions, compaction
        notices -- while the in-channel turn path kept answering, so the space
        would look half-alive rather than misconfigured. That is the failure the
        base contract calls out: an empty principal means "the key names no one
        person", not "no one is authorized". Matched case-sensitively, because a
        Webex room id is an opaque blob and :meth:`room_permitted` compares it
        exactly. Turning the group switch off, or dropping a room from the list,
        revokes that space's proactive traffic from the next start: a persisted
        link outlives the config that authorized it, and this re-decides against
        the config THIS PROCESS was built with rather than trusting the link. Both
        sets are frozen in ``__init__`` and the config PATCH reports
        ``restart_required`` for them, so a narrowing takes effect on restart --
        which is also why this can never be looser than inbound, since
        :meth:`room_permitted` reads the very same frozen sets.

        A **direct** route has no such roster: the session binds
        ``inbound.room_id`` (see :meth:`receive`) while the email allow-list holds
        **emails**, and a room id is opaque -- nothing in this process maps one
        back to the person in it. *principal* is that person, so the check reaches
        the same allow-list :meth:`authorize` uses, lowercased the same way, and
        with no principal there is nothing left to consult, so it refuses: an
        unidentifiable recipient must not be posted to from a network egress
        boundary.

        That refusal still catches a ``unified`` DM bucket, which collapses several
        peers into one session on purpose, so nothing available here establishes
        which of them the room currently belongs to -- its conversation is a 1:1 DM
        room, which no operator puts in the SPACE allow-list, so it cannot reach
        the arm above. Sessions under the default ``per-channel-peer`` scope carry
        their peer in the key and are unaffected.
        """
        if not conversation_id:
            return False
        if self._allow_group_rooms and conversation_id in self._allowed_rooms:
            return True
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
        deny-by-default auth + the room gate (``room_permitted``), and hands the
        richer ``WebexInbound`` (carrying ``room_id``) to the turn dispatcher.
        """
        if not isinstance(raw_envelope, WebexInbound):
            return
        inbound = raw_envelope
        # A file-only message is still a message: an uncaptioned screenshot has
        # no text, and dropping it would leave the sender watching a successful
        # send that the agent was never told about. A card press likewise carries
        # no text — its content is the press.
        if not inbound.text and not inbound.file_urls and inbound.card_inputs is None:
            return
        if not self.room_permitted(inbound):
            return
        msg = InboundMessage(
            channel_type="webex",
            user_id=inbound.person_email,
            conversation_id=inbound.room_id,
            text=inbound.text,
            thread_id=inbound.parent_id or None,
            attachments=list(inbound.file_urls),
            is_mention=bool(inbound.mentioned_people),
        )
        if not self.authorize(msg):
            return
        if self._dispatch is not None:
            await self._dispatch(inbound)

    def room_permitted(self, inbound: WebexInbound) -> bool:
        """Whether this ROOM may be answered in. Fail-closed, and audited.

        Two independent gates, and a group message must clear BOTH this and the
        sender's email allow-list. A direct room is always permitted (it is the
        1:1 space with an allow-listed person). A group space is permitted only
        when the operator opted in AND named that space, because a reply there is
        visible to every member — including people the email allow-list excludes.
        Turning the switch on alone therefore grants nothing.

        Expressed as positive membership rather than ``room_type != "direct"``:
        a Webex room type this code does not know about must not inherit whatever
        a direct room gets.

        ONE rule for every envelope, including an Adaptive Card press. A press
        reports no ``roomType`` on the wire, so the client resolves it before
        dispatch (``_room_type_of``) rather than letting this gate special-case
        it: a second branch here would have to guess, and a guess that admits a
        press by room id alone drops every DM press the moment a space is named
        while admitting a space press the group switch never enabled.
        """
        if inbound.room_type == ROOM_DIRECT:
            return True
        permitted = (
            inbound.room_type == ROOM_GROUP
            and self._allow_group_rooms
            and inbound.room_id in self._allowed_rooms
        )
        if not permitted:
            sel().log_api_access(
                caller=inbound.person_email or "unknown",
                operation="webex_transport.receive",
                outcome="denied_room_not_permitted",
                source="webex",
                # The room id is the one thing an operator needs from this record:
                # a Webex room id is an opaque blob with no UI that shows it, so
                # adding the bot and being denied once is how they learn the id to
                # paste into the allow-list.
                resources=f"room_type={inbound.room_type or 'unknown'} room={inbound.room_id}",
            )
        return permitted
