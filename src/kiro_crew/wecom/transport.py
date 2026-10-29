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
import ntpath
import posixpath
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from kiro_crew.messaging.outbound_files import OutboundFile
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
    WECOM_MAX_REPLY_BYTES,
    WECOM_SAFE_REPLY_CHARS,
    WeComClient,
    WeComInbound,
)
from kiro_crew.wecom.media_upload import MAX_BYTES_BY_TYPE

logger = logging.getLogger(__name__)


# Upper bound on the number of LEARNED (non-roster) warm peers kept in
# ``WeComTransport._warm_chats``. Warmth is only an availability HINT in
# ``configured_targets``; it gates no send (``_may_push`` authorizes on the
# roster alone), so evicting a learned peer loses nothing — it is re-learned the
# moment it writes again. Without this bound an ``allow_all`` bot would
# accumulate one entry per distinct org DMer for the life of the gateway. Roster
# members (owner / allow-list) are exempt from this cap, so it bounds only the
# learned subset.
WECOM_WARM_CHATS_MAX = 512

# Upper bound on the LENGTH of a learned userid retained as a ``_warm_chats`` key.
# ``_dispatch_callback`` reads ``userid`` straight off the WS frame with no length
# check, so under ``allow_all`` a peer could present a megabyte-long id; a count
# cap alone would then still let 512 such keys hold arbitrary memory. A non-roster
# userid longer than this is NOT stored as warm (the message is still served) — so
# the bound holds on both the entry COUNT and each entry's SIZE. Roster identities
# (owner / allow-list) are config-authored and bounded, so they are exempt.
WECOM_MAX_RETAINED_USERID_LEN = 256


class WeComSendError(RuntimeError):
    """An outbound WeCom send did not reach the platform."""


def allowed_userids_from_config(entries: object) -> list[str] | None:
    """Flatten ``wecom.allowed_users`` (``[{userid, name}, ...]``) to userids.

    The ONE reading of that field's shape, shared by the boot factory and the
    live reconfigure path so the two can never disagree about who is on the
    roster. Entries that are not a dict, or carry no ``userid``, are skipped.
    Returns ``None`` when the whole value is not a list -- the caller decides
    what a malformed roster means (boot: nobody; reload: keep the old set).
    """
    if not isinstance(entries, list):
        return None
    out: list[str] = []
    for u in entries:
        uid = u.get("userid") if isinstance(u, dict) else None
        if isinstance(uid, str) and uid:
            out.append(uid)
    return out


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
    rich_blocks=False,
    threads=False,
    # ``files_outbound`` gates ONE thing: whether a renderer pulls a local image
    # reference out of a sealed reply segment and uploads it inline (the flag the
    # Discord/Teams/Telegram renderers read before extracting). WeCom ships no such
    # renderer extraction path, so the flag stays False and an inline reference
    # keeps printing its path — the honest degradation. This is INDEPENDENT of the
    # ``file_send`` document path this change adds: that path is gated by
    # ``upload_destination.DOCUMENT_CHANNELS`` + the ``send_document`` verb, never
    # by this flag, so declaring True here would be functionally inert while making
    # the capability ledger claim an extraction WeCom does not do.
    files_outbound=False,
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
    max_message_bytes=WECOM_MAX_REPLY_BYTES,
    max_buttons=0,
    supports_proactive_send=True,
    # ``aibot_send_msg`` answers with no message id, so an empty return is SUCCESS
    # here and a failure RAISES. A caller reading the empty id as "undelivered"
    # would report a landed cron result as lost, leave its dedup hash unadvanced,
    # and repeat the same result on the next tick.
    returns_message_id=False,
)


#: Extension -> WeCom media ``type`` (aibot_upload_media_init body.type). Anything
#: not mapped is an ordinary ``file``. An extension only maps to a richer type
#: when both hold: WeCom's own ``type`` accepts that format, AND the file clears
#: ``security.BINARY_MIME_ALLOWLIST`` in ``_gate_upload_file`` — a format the gate
#: refuses never reaches here, so mapping it would be dead. WeCom's ``image`` type
#: is JPG/PNG only, so gif/bmp/webp stay ``file`` (they are allowlisted, upload
#: fine, and render as a downloadable file). WeCom ``voice`` is AMR-only and
#: ``audio/amr`` is not allowlisted, so voice is unreachable and unmapped. Only
#: the allowlisted video containers (mp4/webm) map to ``video``.
_WECOM_TYPE_BY_EXT: dict[str, str] = {
    # image — WeCom image type is JPG/PNG only
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    # video — only the allowlisted containers
    ".mp4": "video",
    ".webm": "video",
}


def wecom_media_type_for(filename: str) -> str:
    """The WeCom media ``type`` for *filename*, by extension; ``file`` by default."""
    dot = filename.rfind(".")
    ext = filename[dot:].lower() if dot != -1 else ""
    return _WECOM_TYPE_BY_EXT.get(ext, "file")


def _basename(path: str) -> str:
    """Basename of *path* regardless of the OS that produced it.

    ``OutboundFile.path`` is an OS-native path from the caller: a Windows send
    yields a backslash path (``C:\\Users\\<user>\\file.pdf``), so splitting on
    ``/`` alone (or the container's own ``posixpath``) would leave the whole
    absolute path — disclosing the local username and directory layout to WeCom
    in the upload's ``filename`` field. Strip both separators, whichever the
    running OS uses, so only the leaf name is transmitted.
    """
    return posixpath.basename(ntpath.basename(path))


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
        # its learned peers. Only 1:1 chats can be warm, because group traffic is
        # refused inbound and so never becomes addressable.
        #
        # A bounded ordered set (``OrderedDict`` used as an ordered set, values
        # unused): under ``allow_all`` the key space is every org DMer ever, so an
        # unbounded container would grow for the life of the gateway. The number
        # of LEARNED (non-roster) peers is capped at ``WECOM_WARM_CHATS_MAX``,
        # keeping only the most recent; roster members (owner / allow-list) are
        # never evicted and do not count toward that cap (see ``note_warm_chat``).
        # Keeping only recent learned peers costs nothing — warmth is a hint, not
        # a gate (see ``_may_push``), and an evicted peer is re-learned the instant
        # it writes again. The ``in`` membership tests and ``sorted(...)``
        # iterations below read dict keys unchanged.
        self._warm_chats: OrderedDict[str, None] = OrderedDict()
        # Count of drop EVENTS on the warm-peer bound: learned peers evicted by the
        # count cap, plus inbound frames whose over-length userid was not retained.
        # Consumed by ``configured_targets``, which discloses the truncation on
        # every snapshot while it is nonzero, so a truncated tail never reads as a
        # population that never named those peers (rule
        # a-bound-bounds-every-field-it-retains). It counts events, not distinct
        # peers, so nothing operator-facing states it as a peer count.
        self._warm_dropped = 0

    @property
    def client(self) -> WeComClient:
        """The underlying WeCom WS client (held + exposed, not hidden)."""
        return self._client

    # -- Live config ---------------------------------------------------------
    def reconfigure(self, section: Any) -> None:
        """Adopt a reloaded ``wecom`` config section's authorization fields.

        Called by the dispatcher's config applier when ``config.json`` changes
        under ``wecom``, so an allow-list edit from the dashboard, the CLI or
        ``$EDITOR`` takes effect on the next frame instead of the next restart.
        The roster is rebuilt with the same dict->userid flattening the boot
        factory uses, and the frozenset is REPLACED wholesale (never mutated) so
        an in-flight ``authorize`` keeps reading one consistent set.

        Fails closed on shape: a roster that is not a list, or an ``allow_all``
        that is not a bool, keeps the PREVIOUS value and logs at WARNING --
        never widens to "everybody" and never silently empties the list. The
        allow-all flip is the widest grant this channel has (the whole org
        tenant), so each flip and each roster change is SEL-audited by count,
        not by id.
        """
        userids = allowed_userids_from_config(getattr(section, "allowed_users", None))
        if userids is None:
            logger.warning(
                "wecom: allowed_users is not a list in the reloaded config; keeping the "
                "previous allow-list (%d userid(s))",
                len(self._allowed),
            )
        else:
            new_allowed = frozenset(userids)
            if new_allowed != self._allowed:
                added = len(new_allowed - self._allowed)
                removed = len(self._allowed - new_allowed)
                self._allowed = new_allowed
                logger.info("wecom: allow-list reloaded (+%d/-%d userid(s))", added, removed)
                # Removing userids demotes those peers' retained warm entries from
                # roster (exempt) to learned (capped). No trim is forced here: the
                # next inbound's ``_prune_learned_peers_to_cap`` drops every excess
                # entry in one pass (it is not one-at-a-time), so the learned set
                # converges on the next DM. Until then it may sit transiently over
                # the cap — harmless, since warmth is only an availability hint and
                # is never a send gate. Keeping the trim here would be a second
                # eviction site fixing nothing the inbound prune does not.
                sel().log_api_access(
                    caller="config",
                    operation="wecom_transport.reconfigure",
                    outcome="allow_list_changed",
                    source="wecom",
                    resources=f"added={added} removed={removed} size={len(new_allowed)}",
                )
        allow_all = getattr(section, "allow_all_users", None)
        if not isinstance(allow_all, bool):
            logger.warning(
                "wecom: allow_all_users is not a bool in the reloaded config; keeping %r",
                self._allow_all,
            )
        elif allow_all != self._allow_all:
            self._allow_all = allow_all
            outcome = "allow_all_enabled" if allow_all else "allow_all_disabled"
            logger.warning("wecom: allow_all_users flipped to %r via config reload", allow_all)
            sel().log_api_access(
                caller="config",
                operation="wecom_transport.reconfigure",
                outcome=outcome,
                source="wecom",
            )

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

    async def send_document(
        self,
        conversation_id: str,
        document: OutboundFile,
        *,
        caption: str = "",
        thread_id: str | None = None,
    ) -> str | None:
        """Deliver ONE file to *conversation_id* as native WeCom media.

        The generic-file counterpart of :meth:`send_message`, resolved by
        ``upload_destination.resolve_channel`` for the ``file_send`` MCP tool and
        called by ``api_channel_upload_file`` with ``caption`` + ``thread_id``.
        WeCom has no thread concept, so ``thread_id`` is accepted for the shared
        transport contract and ignored. Runs the temporary-material upload
        handshake (``upload_media``) then a proactive file send
        (``send_file_proactive``) — the ``aibot_send_msg`` path, matching
        :meth:`send_message`'s mirror/cron semantics.

        A non-empty ``caption`` is delivered as a companion text push AFTER the
        file lands (WeCom's media frame carries no text field, unlike
        Telegram/Discord which fold the caption into the upload). It is
        best-effort: the file is already delivered, so a failed caption push is
        logged and does not turn a landed file into a reported failure — the
        alternative, silently dropping the user's typed description, is what this
        addresses.

        Takes an :class:`OutboundFile` whose ``data`` the file_send gates already
        validated; the upload uses those bytes, never re-opening the path.
        ``conversation_id`` is the userid (see :meth:`resolve_conversation`).

        Returns a non-empty marker (the WeCom ``media_id``, or ``"sent"``) on a
        confirmed send — WeCom carries no message id (``returns_message_id=False``),
        but the channel-upload caller treats a falsy return as a delivery failure,
        so success must be non-empty — and ``None`` on a delivery failure, so the
        caller falls back to its dashboard-link path.
        """
        if not conversation_id or not document.data:
            return None
        # Re-authorize at the send boundary, same as send_message: a persisted
        # binding outlives the allow-list entry that justified it.
        if not self._may_push(conversation_id):
            logger.warning("wecom send_document: conversation not authorized")
            return None
        filename = _basename(document.path) or "file"
        # Pick the WeCom media type by extension so an image renders as an image
        # message and an mp4/webm as video — not all as a generic file. Only
        # formats WeCom accepts AND `security.BINARY_MIME_ALLOWLIST` admits are
        # mapped (JPG/PNG image, mp4/webm video); anything else, including gif and
        # AMR voice, sends as an ordinary `file`.
        wecom_type = wecom_media_type_for(filename)
        # A rich type carries a tighter platform cap than `file` (image/voice 2 MB
        # vs file/video 20 MB). A body that WeCom's image/video type would refuse
        # for size still delivers as a downloadable `file`, so downgrade rather
        # than fail the whole send — degrading to `file` is strictly better than
        # the dashboard-link fallback the caller would otherwise report.
        size = len(document.data)
        type_cap = MAX_BYTES_BY_TYPE.get(wecom_type, MAX_BYTES_BY_TYPE["file"])
        file_cap = MAX_BYTES_BY_TYPE["file"]
        if wecom_type != "file" and type_cap < size <= file_cap:
            wecom_type = "file"
        try:
            media_id = await self._client.upload_media(document.data, wecom_type, filename)
            # Re-authorize AFTER the upload, before the push. The check above and
            # the push below straddle the chunked upload handshake — a multi-second,
            # multi-await window on a large body — and the allow-list can be
            # replaced inside it by a live `reconfigure` (a departing/compromised
            # userid revoked mid-upload). A media frame cannot be recalled once the
            # ACK lands, so the authorization decision must be fresh at each send
            # boundary, not carried across the upload.
            if not self._may_push(conversation_id):
                logger.warning("wecom send_document: conversation deauthorized during upload")
                return None
            if not await self._client.send_file_proactive(
                conversation_id, media_id, media_type=wecom_type
            ):
                logger.warning("wecom send_document: proactive send not acknowledged")
                return None
        except Exception as exc:  # noqa: BLE001 — best-effort leg; caller falls back
            # Never log the exception payload (may carry the path / errmsg); the
            # class is enough for an operator, matching the client's own logging.
            logger.warning("wecom send_document failed: %s", type(exc).__name__)
            return None
        # The caller's typed description rides alongside the file. WeCom's media
        # frame (media_type:{media_id}) has no text field — unlike Telegram/Discord
        # which fold the caption into the upload — so it must go as a separate
        # aibot_send_msg text push. Best-effort AFTER the confirmed file send: the
        # file already reached the user, so a dropped caption is logged and the
        # send still counts as delivered. Dropping it silently (the prior
        # behaviour) is what made the user's description vanish without a trace.
        if caption:
            # The caption is a SECOND delivery, past its own await (the file
            # push + ACK), so it needs its own fresh authorization: the roster
            # can be revoked between the file landing and this push, and the
            # caption must not reach a recipient who lost access in that gap.
            if not self._may_push(conversation_id):
                logger.warning("wecom send_document: conversation deauthorized before caption")
            else:
                try:
                    if not await self._client.send_proactive(conversation_id, caption):
                        logger.warning("wecom send_document: caption push not acknowledged")
                except Exception as exc:  # noqa: BLE001 — companion leg; file already sent
                    logger.warning(
                        "wecom send_document caption push failed: %s", type(exc).__name__
                    )
        # WeCom carries no message id on aibot_send_msg (returns_message_id=False),
        # but the channel-upload caller treats a FALSY return (`""`/None) as a
        # delivery failure (`if not mid`). A confirmed proactive send above IS a
        # delivery, so return a non-empty marker — the media_id — rather than the
        # empty string, which would be misread as "delivery_reported_no_message_id"
        # even though the file already reached the user.
        return media_id or "sent"

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
        if self._warm_dropped:
            # Say the overflow out loud in EVERY snapshot while it holds: the
            # warm-peer bound has shed learned peers, so this list is a truncated
            # tail, not every peer ever seen. Without the marker a reader cannot
            # tell a capped list from one those peers never joined. Mirrors the
            # weixin ``known_users_truncated`` sentinel. It is unavailable, so it
            # is never a send target, and its label states truncation rather than
            # a peer count (the tally counts drop events, not distinct peers).
            targets.append(
                ConfiguredChannelTarget(
                    "wecom:warm-peers-overflow",
                    f"WeCom · recent-peer list truncated at the {WECOM_WARM_CHATS_MAX}-peer cap",
                    available=False,
                    unavailable_reason="warm_peers_truncated",
                )
            )
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

        Deny-by-default and evaluated fresh, so neither a persisted binding nor an
        already-advertised target can outlive the permission behind it: a userid
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
        """Record that *chat_id* has written to the bot, so it can be pushed to.

        Maintains the bounded warm set: the peer is (re)inserted as the most
        recent, and the number of LEARNED (non-roster) peers is held at
        ``WECOM_WARM_CHATS_MAX`` by evicting the oldest learned peer. Evicting a
        learned peer's warmth is harmless — it is only an availability hint in
        ``configured_targets``, never an authorization check (``_may_push``), and
        the peer is simply re-warmed when it writes again.

        The cap bounds the LEARNED subset ALONE, independently of roster members
        (the owner and allow-listed userids), for two reasons:

        * A roster member is never evicted. ``configured_targets`` advertises
          every roster identity whether or not it is warm and sets ``available``
          from warmth, so evicting a roster member that has written would flip its
          row to unavailable with the reason "none seen since this gateway
          started" — false, and it disables the dashboard's mirror toggle for a
          reachable, authorized DM.
        * Roster warmth must not consume the learned-peer budget. If the cap
          counted roster entries, a roster of >= the cap that has all written
          would leave no room for any learned peer: every newly inserted learned
          peer would be the only evictable entry and be dropped again at once, so
          an active authorized peer could never appear in ``configured_targets``.
          Counting only learned entries means a learned peer is evicted only when
          a NEWER learned peer displaces it, never by roster warmth.

        The roster is itself bounded by config size, so the whole map stays
        bounded at roster-size + ``WECOM_WARM_CHATS_MAX``.
        """
        if not chat_id:
            return
        # Bound the retained key SIZE, not only the entry count: a non-roster
        # userid longer than the cap is not stored as warm (the message is still
        # served — this method's only job is the availability hint). Roster
        # identities are config-authored and exempt. The omission is counted and
        # disclosed by the ``configured_targets`` truncation marker alongside
        # evictions, so a dropped peer is never silent.
        if len(chat_id) > WECOM_MAX_RETAINED_USERID_LEN and not self._is_roster_member(chat_id):
            self._warm_dropped += 1
            return
        self._warm_chats[chat_id] = None
        self._warm_chats.move_to_end(chat_id)
        self._prune_learned_peers_to_cap()

    def _is_roster_member(self, chat_id: str) -> bool:
        """Whether *chat_id* is a configured identity (the owner or allow-listed).

        Roster membership — not ``allow_all`` — is what exempts a warm entry from
        the cap: under ``allow_all`` every peer is authorized, so exempting on
        authorization would exempt everyone and defeat the cap. Only identities
        ``configured_targets`` advertises in their own right (and whose row's
        ``available`` flag reads warmth) are exempt.
        """
        return bool(chat_id) and (chat_id == self._owner_id or chat_id in self._allowed)

    def _prune_learned_peers_to_cap(self) -> int:
        """Hold the LEARNED (non-roster) warm peers at ``WECOM_WARM_CHATS_MAX``.

        Returns the number of entries dropped, so callers can account for the
        drop — a bounded structure that silently sheds entries is exactly what
        the ``a-bound-bounds-every-field-it-retains`` rule forbids, because under
        ``allow_all`` the warm peers ARE the whole ``configured_targets`` list and
        a silently shortened list is indistinguishable from one those users never
        joined.

        Counts only non-roster entries and removes the oldest ones (least-recent
        first) until at most ``WECOM_WARM_CHATS_MAX`` remain. Roster entries are
        neither counted nor removed, so roster warmth cannot push a learned peer
        out and a just-inserted learned peer is dropped only when an OLDER learned
        peer exists to displace instead.

        Removes EVERY excess entry in one pass, not just one, so the learned set
        converges to the cap from any starting state — in particular after a
        ``reconfigure`` that shrinks the allow-list converts many retained roster
        entries to learned ones at once. A one-at-a-time eviction on each inbound
        would then never catch up (one added, one removed per message), leaving
        the oversized set resident and scanned on every frame; dropping every
        excess entry in one pass means the next inbound after such a shrink fully
        converges the set.
        """
        learned = [c for c in self._warm_chats if not self._is_roster_member(c)]
        # The length bound holds wherever a learned entry is retained, not only at
        # insert: a roster member demoted by ``reconfigure`` was stored exempt, so
        # an over-length one is shed here on its first pass as a learned entry.
        oversized = [c for c in learned if len(c) > WECOM_MAX_RETAINED_USERID_LEN]
        for chat_id in oversized:
            del self._warm_chats[chat_id]
        if oversized:
            learned = [c for c in learned if len(c) <= WECOM_MAX_RETAINED_USERID_LEN]
        excess = max(0, len(learned) - WECOM_WARM_CHATS_MAX)
        # learned is in oldest-first order (OrderedDict iteration); drop the oldest
        # `excess` of them.
        for chat_id in learned[:excess]:
            del self._warm_chats[chat_id]
        dropped = len(oversized) + excess
        self._warm_dropped += dropped
        return dropped

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
                # Received from a person: its start is FOREGROUND (kiro_crew.start_priority).
                inbound.person_origin = True
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
        allow-list exist -- the same reasoning as iMessage's group fail-closed.
        Those two are exactly what Webex has (a ``space:{room_id}`` session plus
        ``webex.allowed_room_ids``), which is why it can admit a space and this
        cannot.
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
