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
* Proactive send is available (``aibot_send_msg``), but WeCom only delivers into
  a conversation the user has already written to. The transport tracks which peers
  are WARM so ``configured_targets`` can say so honestly; the SEND path gates on
  authorization only, because warmth is process-local while a mirror binding is
  persisted — after a restart warmth is unknown, not false, and the platform's own
  refusal (awaited on the ACK) is the authority on deliverability.

Security: :meth:`authorize` is **deny-by-default** and owner-only. An empty
allow-list authorizes nobody (fail closed), never everybody. The only path to
"everybody" is the explicit ``allow_all`` opt-in (config
``wecom.allow_all_users``), which still denies frames without a userid.

Two further inbound gates run in :meth:`receive`, both BEFORE a turn is
dispatched and in this order:

* **1:1 chats only** — group traffic is refused (``_chat_is_direct``). Sessions
  are keyed on ``userid``, so a group message would run inside the sender's
  private DM session and publish its history and tool output to the whole room.
* **Redelivery suppression** — WeCom may repeat a callback, and each repeat would
  run the turn again. Checked after authorization so unauthorized traffic cannot
  evict entries from the bounded window.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from kiro_crew.messaging.tables import TABLE_POLICY_OFF
from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.wecom.client import (
    CHAT_TYPE_SINGLE,
    WECOM_SAFE_REPLY_CHARS,
    WeComClient,
    WeComInbound,
)

logger = logging.getLogger(__name__)


class WeComSendError(RuntimeError):
    """An outbound WeCom send did not reach the platform."""


# A dispatch callback consumes an authorized WeCom inbound (carrying the WS
# routing keys ``req_id`` / ``response_url`` that the neutral InboundMessage
# cannot hold) and drives a turn. The gateway supplies the real implementation.
DispatchFn = Callable[["WeComInbound"], Awaitable[None]]

# WeCom AI-bot capabilities. ``max_message_chars`` is a CHARACTER budget derived
# from the platform's 20480-BYTE cap so the shared splitter stays byte-safe (see
# WECOM_SAFE_REPLY_CHARS). ``max_buttons=0`` records that no tappable chip is
# rendered; the renderer degrades an [OPTIONS:] trailer to a numbered text list
# instead of deleting it. ``supports_proactive_send`` is True because the long
# connection carries ``aibot_send_msg`` — delivery still requires the peer to have
# messaged the bot once, which is a PER-TARGET availability question answered by
# ``configured_targets``, not a transport-wide one.
WECOM_CAPABILITIES = TransportCapabilities(
    streaming=True,
    edit=True,
    reactions=False,
    files_inbound=True,
    files_outbound=False,
    rich_blocks=False,
    threads=False,
    # Keep canonical tables: an adaptive representation can exceed the cap while
    # the only shorter raw candidate still contains a value that display-form
    # redaction would remove. That reason is independent of the overflow path the
    # renderer now has (a sealed bubble's tail continues as confirmed pushes), so
    # the policy stands on the redaction argument alone.
    table_mode=TABLE_POLICY_OFF,
    # The BYTE cap expressed as a character budget the shared splitter can plan
    # against; ``truncate_utf8`` is the exact guard at the wire. A flat 20000-CHAR
    # cap was ~60 KB of Chinese text against a 20480-BYTE limit.
    max_message_chars=WECOM_SAFE_REPLY_CHARS,
    max_buttons=0,
    supports_proactive_send=True,
    # ``aibot_send_msg`` answers with no message id, so an empty return is SUCCESS
    # here and a failure RAISES. A caller reading the empty id as "undelivered"
    # would report a landed cron result as lost, leave its dedup hash unadvanced,
    # and repeat the same result on the next tick.
    returns_message_id=False,
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
        # Conversations that have written to the bot at least once, which is
        # WeCom's precondition for ``aibot_send_msg``. Learned from authorized
        # inbound only, so an unauthorized sender cannot make itself a target.
        # Same "warm first" shape Teams uses for its service URL and Weixin for
        # its learned peers. A set, not a map: only 1:1 chats can be warm, because
        # group traffic is refused inbound and so never becomes addressable.
        self._warm_chats: set[str] = set()

    @property
    def client(self) -> WeComClient:
        """The underlying WeCom WS client (held + exposed, not hidden)."""
        return self._client

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        """Push *content* into *conversation_id* with no inbound request to answer.

        This is the mirror/cron path, over ``aibot_send_msg``. WeCom returns no
        message id on this command, so the id is empty by contract and a FAILURE
        has to raise rather than return.

        Authorization is rechecked here; warmth is not (see :meth:`_may_push`).
        """
        if not conversation_id:
            raise WeComSendError("no WeCom conversation to send to")
        # Re-authorize at the SEND boundary, not only where the target was
        # advertised. A mirror binding is persisted, so it outlives the allow-list
        # entry that justified it: a userid removed from wecom.allowed_users would
        # otherwise keep receiving this session's replies.
        if not self._may_push(conversation_id):
            raise WeComSendError("WeCom conversation is not currently authorized to receive")
        if not await self._client.send_proactive(conversation_id, content):
            # RAISE rather than return: the mirror caller treats a return as
            # delivery and goes on to persist the link and report success, so a
            # dropped socket would lose the message silently. WeCom returns no
            # message id on this command, so "" is the success value and cannot
            # also mean failure.
            raise WeComSendError("WeCom proactive send failed (no live connection)")
        return ""

    async def resolve_conversation(self, user_id: str) -> str:
        # No addressable DM channel id; the userid is the logical conversation.
        return user_id

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Mirror :meth:`authorize` on the userid the conversation id carries.

        Defence in depth rather than the live gate: WeCom declares
        ``supports_proactive_send=False``, so the shared send ladder refuses this
        transport before asking. Implemented anyway, and fail-closed, so the
        answer is already correct if that capability is ever flipped -- and
        because ``send_message`` also accepts a one-shot ``response_url`` as its
        conversation id, which is not a roster identity and is therefore refused.
        """
        if not conversation_id:
            return False
        if self._allow_all:
            return True
        return conversation_id == self._owner_id or conversation_id in self._allowed

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # WeCom AI-bot cannot page DM history; sessions persist via
        # conversation_log instead.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        """Advertise the DMs a dashboard session may mirror into.

        Availability is per target, not per transport: ``aibot_send_msg`` only
        delivers into a conversation the user has already written to, so an
        allow-listed userid that has never messaged the bot is listed with an
        explicit reason instead of being offered and then failing at send time.
        """
        identities = set(self._allowed)
        if self._owner_id:
            identities.add(self._owner_id)
        targets = [
            ConfiguredChannelTarget(
                f"user:{user_id}",
                f"WeCom DM · {user_id}",
                available=user_id in self._warm_chats,
                unavailable_reason=(
                    ""
                    if user_id in self._warm_chats
                    else (
                        "WeCom delivers only to a user who has written to the bot; "
                        "none seen since this gateway started"
                    )
                ),
            )
            for user_id in sorted(identities)
        ]
        # Under the allow-everyone policy there is no configured list to draw on,
        # so the warm peers ARE the list: they are the only addressable ones.
        if self._allow_all:
            for chat_id in sorted(self._warm_chats):
                if any(x.target_id == f"user:{chat_id}" for x in targets):
                    continue
                targets.append(ConfiguredChannelTarget(f"user:{chat_id}", f"WeCom DM · {chat_id}"))
        return targets

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        """Re-validate an advertised id at the side-effect boundary.

        The browser never supplies a platform conversation id directly, and the
        allow-list may have narrowed since the id was advertised, so MEMBERSHIP is
        rechecked here rather than trusted from the round trip.

        Warmth is deliberately not rechecked — see :meth:`_may_push` for why
        in-memory warmth cannot be a precondition for a persisted binding. An
        unwarmed target is therefore accepted here and refused by WeCom on the ACK,
        which ``send_proactive`` waits for and ``send_message`` turns into a
        ``WeComSendError``: a reported failure at the send boundary rather than a
        silent drop. ``configured_targets`` is where warmth belongs, because there
        it is an availability HINT and being wrong costs a stale label rather than
        an undeliverable session.
        """
        if not target_id.startswith("user:"):
            return None
        user_id = target_id[len("user:") :]
        if not user_id or not self._may_push(user_id):
            return None
        return user_id, None

    def _may_push(self, chat_id: str) -> bool:
        """Whether *chat_id* is AUTHORIZED to be pushed to right now.

        Deny-by-default and evaluated fresh, so neither a persisted binding nor a
        previously-advertised target can outlive the permission behind it: a userid
        removed from the allow-list stops receiving this session's replies.

        Deliberately does NOT require process-local warmth. ``_warm_chats`` is
        in-memory while a mirror binding is persisted, so after a gateway restart
        warmth is UNKNOWN, not known-false — and refusing on that would silently
        disable every mirrored send until the user happened to write again.
        Deliverability is WeCom's answer to give: the push is attempted and a
        refusal comes back on the ACK, which ``send_proactive`` waits for. Warmth
        stays what it is genuinely good for — a truthful availability hint in
        ``configured_targets``.
        """
        return bool(
            self._allow_all
            or (self._owner_id and chat_id == self._owner_id)
            or chat_id in self._allowed
        )

    def note_warm_chat(self, chat_id: str) -> None:
        """Record that *chat_id* has written to the bot, so it can be pushed to."""
        if chat_id:
            self._warm_chats.add(chat_id)

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
        # A MEDIA-ONLY message is a message. Returning early on empty text
        # discarded an uncaptioned screenshot with no reply and no log line: the
        # sender saw a successful send while the agent was never told anything
        # arrived. Emptiness is a reason to drop only when the whole envelope is
        # empty. (Same invariant Weixin had to fix.)
        if not inbound.text and not inbound.attachments:
            return
        if not self._chat_is_direct(inbound):
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
        # Dedupe AFTER authorization, so unauthorized traffic cannot evict
        # genuine entries from the bounded window (see ``already_delivered``).
        if self._client.already_delivered(inbound.msgid):
            logger.info("WeCom: dropping redelivered msgid for %s", inbound.userid)
            return
        # This conversation has now written to the bot, which is WeCom's
        # precondition for pushing into it later. Recorded only for AUTHORIZED
        # inbound, so an unauthorized sender cannot make itself a mirror target.
        self.note_warm_chat(inbound.userid)
        if self._dispatch is not None:
            try:
                await self._dispatch(inbound)
            except BaseException:
                # The window records "delivered", and a turn that never completed was
                # not. Leaving the entry would make WeCom's own retry — the mechanism
                # that exists to recover exactly this — get suppressed as a duplicate,
                # so the user's message is dropped for good with nothing said. Forget
                # it and let the redelivery run. Idempotency still holds for the
                # SUCCESS path, which is what the window is for.
                self._client.forget_msgid(inbound.msgid)
                raise

    def _chat_is_direct(self, inbound: WeComInbound) -> bool:
        """Fail closed on anything that is not a 1:1 chat.

        A WeCom group is a shared disclosure boundary: every member reads whatever
        the bot posts, including tool output and file contents. Sessions here are
        keyed on ``userid`` alone, so a group message would ALSO run inside that
        user's private DM session — echoing that conversation's history and tool
        results into the room, and letting the room steer a session the user
        believes is private. Neither is acceptable, and the userid allow-list does
        not help: the sender is allow-listed, the audience is not.

        So group traffic is refused until per-group sessions and a group
        allow-list exist. Same posture, and the same reasoning, as Webex's
        direct-rooms-only gate and iMessage's group fail-closed.
        """
        if inbound.chattype == CHAT_TYPE_SINGLE:
            return True
        sel().log_api_access(
            caller=inbound.userid or "unknown",
            operation="wecom_transport.receive",
            outcome="denied",
            source="wecom",
            resources="reason=denied_group_chat",
        )
        logger.info("WeCom: dropping non-direct chat message from %s", inbound.userid)
        return False
