"""Who a cross-surface delivery may reach and the delivery itself: the recipient principal
ladder, the channel target, and the linked-Slack and cross-surface legs."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils import slack_mirror_is_paused
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.egress import channel_egress_parts, send_channel_parts
from kiro_crew.messaging.link import CHAT_TYPE_DIRECT, parse_session_key
from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.mirror_admission import admission_required, verify_mirror_admission
from kiro_crew.platform import redact_via_context

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _RECIPIENT_LOGGED,
        _RECIPIENT_LOGGED_CAP,
        SLACK_NAMESPACE,
        logger,
        mirror_is_paused,
        sel,
    )


def _session_principal(session_key: str) -> str:
    """The platform user id a DIRECT session key names, or ``""``.

    The persisted ``ChannelLink`` records a conversation, and a conversation id is
    what made a revoked recipient unanswerable for the transports whose conversation
    id is not their user id. The session KEY carries the peer: the canonical grammar is
    ``{surface}:{agent}:{chat_type}:{scope…}`` and for a 1:1 DM the scope is exactly
    the peer's platform id. Parsing goes through ``messaging.link.parse_session_key``
    because that module is the ONE canonical address parser (RFC §9 rule 4); a second
    decomposition here would drift from the grammar the keys are built with.

    Derived from the KEY ALONE, deliberately. Two other records name a peer and
    neither is safe here, because a principal is only usable if it describes the
    conversation the link points at:

    * the session's stored channel value (``{namespace}:{user_id}``) is written ONCE,
      when the session is created, while the origin/mirror link is rewritten on
      later turns. Under a ``unified`` bucket -- which collapses several peers' 1:1
      DMs into one session on purpose -- the two therefore drift: the attribution can
      name the peer who created the session while the link points at a different
      peer's conversation. Authorizing against it would check the wrong person and
      pass, which is worse than declining to name one.
    * a **forum or group** scope is ``(chat_id, thread_id)``, so its audience is a
      room and no single principal owns it. Returning ``scope[0]`` would hand a
      supergroup id to a check that tests USER rosters.

    Empty therefore means "the key does not name one principal", never "no principal
    is authorized". A transport whose other rosters can still judge the route (a
    Discord thread against its thread allow-list) uses them; one with nothing left to
    consult refuses, because this feeds a network egress boundary.

    A third source IS consulted, but by the ladder rather than here, and only when
    this returns ``""``: the peer the mirror link recorded when the GATEWAY admitted
    it (``ChannelLink.principal``), under a MAC the gateway alone can mint over the
    session key and the whole location (``ChannelLink.admission``, see
    :func:`_recipient_principal`). That record is free of the drift above because
    it is written in the same write as the conversation id it describes and signed
    together with it, so a row rewritten, moved or unsigned by anything but the
    gateway fails to verify; the transport's own record of the conversation
    (``MessagingTransport.direct_peer_of``) must agree with it whenever the
    transport has one. That is what serves a dashboard-born session mirrored to a
    Discord DM, whose key names nobody and whose DM channel id cannot be tested
    against a user roster.
    """
    parsed = parse_session_key(session_key)
    if parsed is None or parsed.chat_type != CHAT_TYPE_DIRECT or len(parsed.scope) != 1:
        return ""
    return parsed.scope[0]


def _link_principal(link: Any) -> str:
    """The peer a mirror link records as the one its conversation was admitted for,
    or ``""`` when the link names none (or predates the field). Trusted only under
    the row's gateway admission: see :func:`_recipient_principal`."""
    return str(getattr(link, "principal", None) or "")


def _attested_peer(transport: Any, conversation_id: str) -> str:
    """What the transport itself attests about who *conversation_id* belongs to.

    ``MessagingTransport.direct_peer_of`` -- for Discord the ``dm_channel_id ->
    user_id`` pairing its client learns when it opens a DM or an authorized message
    arrives in one. ``""`` for a transport without the hook, one that keeps no such
    record, an id it never placed, and a hook that raised: "not on record", which the
    ladder treats as no contradiction rather than as a refusal, since the admitted
    record is what it is checking against. Defense in depth over that record, never
    a substitute for it.
    """
    attest = getattr(transport, "direct_peer_of", None)
    if attest is None or not conversation_id:
        return ""
    try:
        return str(attest(conversation_id) or "")
    except Exception:
        logger.debug(
            "cross-surface: DM peer attestation failed; treating as unrecorded", exc_info=True
        )
        return ""


def _recipient_principal(session_key: str, link: Any, transport: Any) -> str:
    """The principal the per-send recipient check is handed for *link*.

    The session KEY first, exactly as :func:`_session_principal` reads it, so every
    key that names a peer keeps its established reading. When the key names nobody
    -- a dashboard-born ``chat-*`` key, the shape a mirror made from the dashboard
    menu or a Discord ``!sessions`` pick hangs off -- the answer is the peer the
    link recorded when the gateway admitted it (:func:`_link_principal`), and it is
    handed in only under two conditions:

    * the row's ADMISSION verifies (:func:`verify_mirror_admission`): a MAC the
      gateway alone can mint, over this session key and the whole location. The
      session map is writable by in-sandbox code, so without it a row rewritten to
      name an allow-listed user for a revoked user's DM, or to aim one session's
      replies at another allow-listed user's DM, would be authorized against the
      wrong person and pass. A row with no admission, one that does not verify, and
      one signed under a rotated key are refused, audited, and logged once, and the
      remedy is named: re-link the session, which mints a fresh record;
    * the transport's own record of the conversation, when it has one, AGREES
      (:func:`_attested_peer`, defense in depth): a pairing the client learned on its
      authorized paths that names someone else refuses the send whatever the record
      says. A transport that knows nothing -- the ordinary state right after a
      restart, before the peer has written into the DM -- confirms nothing and
      contradicts nothing, so the verified record stands and this leg admits the
      mirror across a restart with no inbound message. (Discord's REST ladder keeps
      its own mid-send re-check over the pairing alone, so a send that hits one of
      that ladder's waits before the pairing is re-learned is still refused there;
      a send that never waits is delivered.)

    The roster still decides, per send, whether the named peer is admitted.
    """
    named = _session_principal(session_key)
    if named:
        return named
    hint = _link_principal(link)
    if not hint:
        return ""
    channel_type = str(getattr(link, "channel_type", "") or "channel")
    channel_id = str(getattr(link, "channel_id", "") or "")
    if not verify_mirror_admission(session_key, link):
        _audit_admission_refusal(session_key, link)
        _log_once(
            ("unadmitted", channel_type, channel_id),
            logging.WARNING,
            "cross-surface: the %s mirror link names a peer but carries no valid gateway "
            "admission (an unsigned or rewritten row, or a rotated signing key); refusing "
            "the send. Re-link the session from the dashboard to restore delivery",
            channel_type,
        )
        return ""
    attested = _attested_peer(transport, channel_id)
    if attested and attested != hint:
        # The transport learned, on its own authorized paths, that this conversation
        # belongs to someone else. Its record outranks the admitted one -- a wrong
        # record here is stale at best -- and the disagreement is worth a louder
        # line than the ordinary refusal. Ids stay out of the log, as everywhere on
        # this path.
        _audit_admission_refusal(session_key, link, outcome="contradicted")
        _log_once(
            ("contradicted", channel_type, channel_id),
            logging.WARNING,
            "cross-surface: the %s mirror link's admitted peer disagrees with the "
            "transport's own record of the conversation; refusing the send",
            channel_type,
        )
        return ""
    return hint


def _is_origin_location(state: Any, session_key: str, link: Any) -> bool:
    """Whether *link* is the conversation the gateway saw *session_key* read in.

    ``SessionManager.get_origin_link`` is held in gateway memory and written only
    by a transport's inbound path from the conversation's real send target, so it
    is out of reach of in-sandbox code. Compared by location; any failure to read
    it answers ``False``.
    """
    getter = getattr(getattr(state, "sessions", None), "get_origin_link", None)
    if getter is None:
        return False
    try:
        origin = getter(session_key)
    except Exception:
        return False
    if origin is None:
        return False
    try:
        return all(
            getattr(origin, name, None) == getattr(link, name, None)
            for name in ("channel_type", "channel_id", "thread_id")
        )
    except Exception:
        return False


def _key_named_row_admitted(state: Any, session_key: str, link: Any) -> bool:
    """Whether a send to *link* on behalf of *session_key* may proceed past the row.

    Answers ``True`` outright for a session whose key names nobody (a dashboard
    ``chat-*`` key): its row is judged by :func:`_recipient_principal`. For a
    KEY-NAMED session (``mirror_admission.admission_required``) the recipient check
    is handed the peer off the KEY, so it cannot tell which conversation the row
    points at, and the session map is writable by in-sandbox code: a row whose
    ``channel_id`` was rewritten to another allow-listed user's DM would pass and
    deliver this session's replies, cron results and notices there. Such a row is
    admitted only when

    * its admission verifies (:func:`verify_mirror_admission`): a MAC the gateway
      minted over this key and this whole location when the conversation's own peer
      bound it or a dashboard / ``!sessions`` link authorized it; or
    * it IS the conversation the gateway saw this session read in
      (:func:`_is_origin_location`), a record no agent can write; or
    * it addresses the key's own peer by id, on the key's own channel, with no
      thread: the peer the roster check is handed is then the very destination,
      so a rewrite cannot move it.

    Anything else -- an unsigned row from before rows were signed, a rewritten or
    moved one, one signed under a rotated key -- is refused, audited, and logged
    once naming the remedy: re-bind it from the conversation or the dashboard.
    """
    if not admission_required(session_key):
        return True
    if verify_mirror_admission(session_key, link):
        return True
    if _is_origin_location(state, session_key, link):
        return True
    peer = _session_principal(session_key)
    parsed = parse_session_key(session_key)
    if (
        peer
        and parsed is not None
        # A user id means that user only on its own surface: the same digits on
        # another channel are someone else, so a cross-channel row stays signed.
        and getattr(link, "channel_type", None) == parsed.surface
        and not getattr(link, "thread_id", None)
        and getattr(link, "channel_id", None) == peer
    ):
        # Addressed to the key's own peer by id: where a conversation id IS the
        # user id (Telegram, iMessage) and the cron leg's user-id rung, which
        # resolves the postable DM through the transport for exactly this peer.
        return True
    channel_type = str(getattr(link, "channel_type", "") or "channel")
    _audit_admission_refusal(session_key, link)
    _log_once(
        ("unadmitted-key-named", channel_type, str(getattr(link, "channel_id", "") or "")),
        logging.WARNING,
        "cross-surface: a %s channel session's mirror row carries no valid gateway "
        "admission (written before rows were signed, rewritten outside the gateway, or "
        "signed under a rotated key); refusing the send. Re-bind it: send a message or "
        "the channel's link command in that conversation, or re-link from the dashboard",
        channel_type,
    )
    return False


def _audit_admission_refusal(session_key: str, link: Any, *, outcome: str = "unverified") -> None:
    """SEL-record a mirror admission that did not hold. Guarded like every other
    audit write on this path: a failed audit must not crash the send leg.

    ``caller`` and ``source`` are in-tree constants, never values off the row: the
    row comes from a file in-sandbox code can write, and those two fields land in
    the append-only log verbatim, so a credential planted in a ``channel_id`` would
    be published unredacted. The identifiers go into ``resources``, which is
    redacted and clipped on the way in.
    """
    try:
        sel().log_api_access(
            caller="cross-surface",
            operation="channel.mirror_admission",
            outcome=outcome,
            source="session_map",
            resources=(
                f"{session_key} -> {getattr(link, 'channel_type', '')}:"
                f"{getattr(link, 'channel_id', '') or 'unknown'}"
            ),
        )
    except Exception:
        logger.debug("SEL logging failed for a mirror admission refusal", exc_info=True)


def _marker_digest(marker: tuple[str, str, str]) -> str:
    """The fixed-length key the said-once set retains for *marker*."""
    material = b"\x00".join(part.encode("utf-8", "surrogatepass") for part in marker)
    return hashlib.sha256(material).hexdigest()


def _log_once(marker: tuple[str, str, str], level: int, message: str, *args: Any) -> None:
    """Log *message* at *level* the first time *marker* is seen, at DEBUG after."""
    key = _marker_digest(marker)
    if key in _RECIPIENT_LOGGED:
        logger.debug(message, *args)
        return
    if len(_RECIPIENT_LOGGED) >= _RECIPIENT_LOGGED_CAP:
        _RECIPIENT_LOGGED.clear()
    _RECIPIENT_LOGGED.add(key)
    logger.log(level, message, *args)


def _authorize_recipient(
    transport: Any,
    channel_type: str,
    conversation_id: str,
    thread_id: str | None,
    *,
    principal: str,
    session_key: str,
    audit_allowed: bool = False,
) -> bool:
    """Decide and SEL-audit RECIPIENT authorization for one proactive target.

    The ONE spelling of the recipient decision, shared by the ladder's own
    recipient leg and the mirror-link creation handler's post-resolve
    re-decision, so the two cannot drift: hardening the check (principal
    derivation, thread semantics, audit shape) lands on both paths at once,
    and a caller that opts out of the ladder leg is handed the exact function
    it is obligated to call against the resolved id.

    Fails closed on a raising transport — an allow-list check that errored has
    authorized nobody, and this feeds a network egress boundary. A denial is
    always SEL-audited (``channel.proactive_send_authorize`` / ``denied``): a
    revoked recipient silently losing its messages looks exactly like an idle
    agent. ``audit_allowed=True`` records the ALLOWED outcome too — the
    mirror-link creation contract, where the decision sits beside the
    resolver's both-outcome audit and admits a recipient once per link. The
    per-send ladder legs keep denial-only, deliberately: they run per delivered
    unit (a mirror backfill re-enters the ladder for every message), so an
    allowed record there would write an audit row per mirrored message.

    The SEL write itself is guarded: an audit-log failure must not turn a
    decided outcome into a crashed send path, and the miss is logged.
    """
    try:
        permitted = bool(transport.may_send_to(conversation_id, thread_id, principal=principal))
    except Exception:
        logger.warning(
            "outbound recipient authorization check failed for %s; refusing (fail-closed)",
            channel_type,
            exc_info=True,
        )
        permitted = False
    if not permitted or audit_allowed:
        try:
            sel().log_api_access(
                caller=str(conversation_id or "unknown"),
                operation="channel.proactive_send_authorize",
                outcome="allowed" if permitted else "denied",
                source=channel_type,
                resources=f"{session_key} -> {channel_type}",
            )
        except Exception:
            logger.debug("SEL logging failed for outbound authz decision", exc_info=True)
    return permitted


def _resolve_channel_target(
    state: Any,
    session_key: str,
    link: Any,
    *,
    principal: str | None = None,
    check_recipient: bool = True,
) -> Any:
    """Resolve ``(link, transport)`` through the cross-surface send ladder.

    *principal* lets a caller that has ALREADY established the recipient
    authoritatively supply it, instead of having it derived from *session_key*.
    ``session_control``'s drop notice is the case: it supplies the peer the
    dropped entry's own metadata recorded alongside the conversation id.
    ``None`` means derive; a string is used verbatim.

    *check_recipient* lets a caller whose link does not yet name a conversation
    opt out of the recipient leg. Two run this ladder on the CONFIGURED-TARGET
    spelling (``user:<id>``) because channel-scope governance must precede
    ``resolve_configured_target``'s possible network side effect: the
    mirror-link creation pre-check (``chat_mirror.api_chat_slot_mirror_link``)
    and the proactive owner DM (``handlers/messaging._deliver_channel_dm``). But
    ``may_send_to`` is a recipient predicate over conversation ids, so that
    spelling can never match a roster of bare ids and the leg would refuse
    every allow-listed recipient. ``False`` skips ONLY the recipient leg;
    governance and transport capability still gate the resolve, and the caller
    MUST re-decide recipient authorization against the resolved conversation id
    via :func:`_authorize_recipient` — the same function this leg runs — or
    revocation stops being enforced on that path.
    Every persisted-link caller keeps the default.

    This is the shared capability/governance seam for both actual mirror
    delivery and the dashboard's read-only ``links[].live`` projection.  It
    intentionally skips Slack, whose dedicated client and streaming path are
    not registered in ``channel_transports``.
    """
    if link is None or link.channel_type == SLACK_NAMESPACE or not link.channel_id:
        return None
    try:
        from kiro_crew.platform.context import PlatformCompositionError
        from kiro_crew.platform.governance_profiles import vet_and_audit

        # vet_and_audit == governance_permits + a SEL governance-decision record
        # for BOTH grant and denial. Every call here is a real send/link
        # decision (the read-only links[].live projection uses the in-memory
        # state._channel_link_is_live instead), so a governance decision at this
        # egress chokepoint MUST land in the SEL trail — the security contract
        # requires every permission decision to be audited.
        decision = vet_and_audit(
            "channels",
            link.channel_type,
            session_key=session_key,
            tool_name="chat.channel_mirror",
            # fail_closed=True: this is an EGRESS chokepoint on a network
            # surface, so a degraded governance evaluation must DENY rather than
            # degrade-to-permit. vet_and_audit forwards this to
            # governance_permits, which swallows its own internal errors and
            # returns a non-permissive Decision under fail_closed. Matches the
            # other "channels"-scope gates: messaging/identity.py,
            # slack/gateway.py, dashboard/handlers_system.py.
            fail_closed=True,
        )
        # Default False, not True: a Decision without ``permitted`` is an
        # unusable answer from a gate, and must not read as permission.
        if not getattr(decision, "permitted", False):
            logger.info(
                "cross-surface: outbound to %s denied by governance policy; " "skipping mirror",
                link.channel_type,
            )
            return None
    except PlatformCompositionError:
        # A composition error means the governance ceiling itself is invalid.
        # governance_permits deliberately re-raises it rather than degrading;
        # swallowing it here would defeat that contract and let a broken
        # ceiling read as an ordinary skip.
        raise
    except Exception:
        logger.debug(
            "cross-surface: governance check failed for %s; skipping mirror " "(fail-closed)",
            link.channel_type,
            exc_info=True,
        )
        return None
    transport = state.get_channel_transport(link.channel_type)
    if transport is None or not transport.capabilities.supports_proactive_send:
        logger.debug(
            "cross-surface: skip mirror to %s (transport=%s, proactive=%s)",
            link.channel_type,
            transport is not None,
            getattr(
                getattr(transport, "capabilities", None),
                "supports_proactive_send",
                None,
            ),
        )
        return None
    # Re-decide RECIPIENT authorization, not just channel-scope governance. The
    # link is persisted, so it outlives the roster that authorized it: dropping a
    # recipient from a channel's allow-list and restarting leaves every proactive
    # leg (cron result, compaction notice, subagent completion) still resolving
    # and still sending. Governance above answers "may this session use the
    # telegram channel at all", which is a different question and stays permitted.
    #
    # Fail closed on a raising transport: an allow-list check that errored has not
    # authorized anybody, and this is a network egress boundary.
    #
    # Skipped only under check_recipient=False (see the docstring): a link that
    # carries a configured-target id instead of a conversation id cannot be
    # judged here, and its caller re-decides against the resolved id.
    #
    # The derived principal reads the session key first and, when that names
    # nobody, the peer the GATEWAY admitted on the link -- trusted only under the
    # row's admission MAC, with the transport's own pairing as defense in depth
    # (``_recipient_principal``): a dashboard-born key names nobody, and for a
    # Discord DM the conversation id alone cannot answer, so without that record
    # every dashboard-driven reply into such a mirror would be refused right here.
    if not check_recipient:
        return link, transport
    # A KEY-NAMED session's principal comes off its key, so the roster check
    # below cannot tell WHICH conversation the row points at: the row itself must
    # be one the gateway wrote (``_key_named_row_admitted``). A caller-supplied
    # principal came with its own in-memory conversation id and is judged as is.
    if principal is None and not _key_named_row_admitted(state, session_key, link):
        return None
    if not _authorize_recipient(
        transport,
        link.channel_type,
        link.channel_id,
        link.thread_id,
        principal=(
            _recipient_principal(session_key, link, transport) if principal is None else principal
        ),
        session_key=session_key,
    ):
        logger.info(
            "cross-surface: outbound to %s refused - recipient not allow-listed",
            link.channel_type,
        )
        return None
    return link, transport


def _resolve_mirror_target(state: Any, session_key: str) -> Any:
    """Resolve a session's outbound mirror through the shared send ladder."""
    return _resolve_channel_target(
        state,
        session_key,
        state.sessions.get_mirror_link(session_key),
    )


def cross_surface_withheld(state: Any, slot: Any) -> bool:
    """Whether *slot*'s turn must NOT publish its reply to a linked channel.

    True when the containment holding NOW is not the containment an admission this
    turn rests on was made under -- a peer steered this turn, or this turn was
    ADMITTED to a session-control verb or a work-ledger route (the caller-side
    gates record the caller's own containment through
    ``session_control.record_audience_admission``, since what those return -- a
    peer's transcript, a roster, a ledger -- becomes part of the reply). Asked by
    the Slack legs, synchronously with the publication each guards, which is the
    only place the answer cannot go stale: a link bound at any point before this
    moment is effective, and a reply already sent cannot be recalled. The
    channel-neutral leg (``_deliver_cross_surface_reply``) does not ask it: that leg
    resolves its target from one read of the binding and judges THAT row through
    ``session_control.publication_withheld`` -- the same comparison -- so its
    decision and its delivery cannot straddle a retarget.

    Neither writer can answer this on its own behalf. A sender records the admission
    before its RPC and keeps it for the whole turn, because a check it runs when the
    RPC returns says nothing about a mirror bound between then and the reply; a reader
    records and returns, and its reply publishes later still. So the writer's job is
    to record (and, for a steer, to stop the turn on what it can see); the decision
    about publishing belongs to the publisher.

    Costs the channel audience nothing when nothing moved -- the comparison is exact
    rather than precautionary. Withholds only when a constraint that
    ``authorize_target`` refuses newly holds, and then the transcript still keeps the
    reply.
    """
    fences = getattr(slot, "_steer_audience_fences", None)
    if not fences:
        return False
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import containment_snapshot, newly_held_constraints

    now = containment_snapshot(state, slot, on_probe_failure=True)
    return any(newly_held_constraints(now, admission) for admission in fences.values())


def slack_publication_withheld(state: Any, slot: Any, channel_id: Any, thread_ts: Any) -> bool:
    """Whether *slot*'s turn must NOT publish to the Slack thread its legs cached.

    The Slack legs post to the ``(channel, thread)`` they read ONCE at turn start, so
    the room they publish to is not the binding holding now. A Slack thread unlinked
    mid-turn is therefore in neither side of :func:`cross_surface_withheld`'s
    comparison -- the admission a gate recorded after the unlink names no Slack room,
    and the live binding names none -- while the cached destination still receives the
    reply: an operator who unlinks the shared thread and mirrors the tab to their own
    DM before a transcript read would have that read posted into the thread they just
    left. Judged here as the room the leg actually posts to
    (``session_control.publication_withheld`` over a binding naming only that thread):
    a room no recorded admission saw withholds the leg. Asked BESIDE
    :func:`cross_surface_withheld`, never instead of it -- that one still catches a
    constraint the live binding newly holds. Nothing recorded, or no destination,
    withholds nothing.
    """
    fences = getattr(slot, "_steer_audience_fences", None)
    if not fences or not channel_id or not thread_ts:
        return False
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import publication_withheld

    return publication_withheld(state, slot, (None, str(thread_ts), str(channel_id)))


async def _deliver_linked_slack_message(
    state: Any,
    slot: Any,
    sessions: Any,
    session_key: str,
    message: str,
) -> None:
    """Mirror one system message to a linked Slack thread."""
    slack_client = getattr(state, "slack_client", None)
    if slack_client is None:
        return
    if slack_mirror_is_paused(state, session_key):
        return
    thread_ts = getattr(slot, "_slack_thread_ts", "")
    channel_id = getattr(slot, "_slack_channel", "")
    if (not thread_ts or not channel_id) and sessions is not None:
        thread_ts, channel_id = sessions.get_slack_link(session_key)
    if not (thread_ts and channel_id):
        return
    try:
        await slack_client.post_message(channel_id, message, thread_ts)
    except Exception:
        logger.debug("Failed to deliver a message to the linked Slack thread", exc_info=True)


def _cross_surface_reply_text(segments: list[str], trailing: str) -> str:
    """The text :func:`_deliver_cross_surface_reply` mirrors for a completed turn.

    *segments* are the turn's own reply rows in window order, and the result joins
    all of them, as the transcript shows the answer. *trailing* is
    ``turn.assistant_text``: a tool boundary flushes it to a row and resets it, so a
    turn that answers before its last tool call holds only the fragment after that
    call in it, or nothing. It is used only when no row is named.

    Only the FINAL segment keeps its ``[OPTIONS:]`` trailer, which the egress leg
    renders as numbered choices. An earlier segment's trailer asked a question the
    turn has since moved past, so it is stripped rather than offered: an answer to
    it would arrive as a reply to a superseded ask. The Slack leg follows the same
    rule.
    """
    if not segments:
        return trailing
    earlier = [split_options_trailer(s)[0] for s in segments[:-1]]
    return "\n\n".join([*(e for e in earlier if e), segments[-1]])


async def _deliver_cross_surface_reply(
    state: Any, session_key: str, assistant_text: str, *, slot: Any = None
) -> None:
    """Deliver a completed dashboard reply to a linked NON-Slack channel.

    The channel-neutral leg of cross-surface sync: reads the session's outbound
    mirror link, resolves the registered ``MessagingTransport`` for that channel
    and pushes the reply via ``send_message`` — capability-gated on
    ``supports_proactive_send``. Slack keeps its dedicated rich streaming mirror
    inline in the turn loop, so it is skipped here. Silent no-op when the session
    has no non-Slack mirror, the transport is not registered, or the channel
    cannot send proactively (WhatsApp outside its 24-hour window). A channel whose
    push is per-TARGET rather than blanket answers that in ``send_message`` itself
    — WeCom pushes through ``aibot_send_msg`` but only into a conversation the user
    has already written to. Best-effort: a delivery failure never disrupts the
    dashboard turn.

    The publish decision and the delivery rest on ONE read of the binding
    (``get_mirror_link`` and ``get_slack_link``, through
    ``session_control._read_mirror_binding``): the row is judged against the
    admissions *slot*'s turn recorded (``session_control.publication_withheld``, the
    audience fence) and the send resolves its transport from that same row. The
    mirror-link writer runs off the loop (``chat_mirror._claim_binding`` under
    ``asyncio.to_thread``), so a decision taken on one read and a send resolved from
    another could straddle a retarget and publish a transcript the decision never
    saw into the new room. An unreadable store delivers nothing. *slot* carries the
    record; a caller with no slot has no turn and no admissions, and gets the plain
    delivery.
    """
    if not assistant_text:
        return
    # Disconnected: the binding is retained so a reply there still resolves here,
    # but turn output stops. Asked before resolving so a muted channel costs no
    # transport lookup.
    if mirror_is_paused(state, session_key):
        return
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import _read_mirror_binding, publication_withheld

    sessions = getattr(state, "sessions", None)
    if getattr(sessions, "get_mirror_link", None) is None:
        return
    binding = _read_mirror_binding(sessions, session_key)
    if publication_withheld(state, slot, binding):
        logger.info(
            "withholding cross-surface reply for %s: %d unresolved audience "
            "fence(s) (peer steers into, or transcript reads by, this turn)",
            session_key,
            len(slot._steer_audience_fences),
        )
        return
    if binding is None:
        logger.debug(
            "cross-surface: mirror binding unreadable for %s; skipping mirror", session_key
        )
        return
    target = _resolve_channel_target(state, session_key, binding[0])
    if target is None:
        return
    link, transport = target
    # Strip, display-redact, number the ``[OPTIONS:]`` choices (``send_message``
    # takes none, so the mirror builds no button), chunk and send: the shared
    # unattended-egress leg, ``messaging.egress``. Chunking keeps a long reply
    # whole rather than hard-truncated by the transport (Telegram caps at 4096).
    parts = await channel_egress_parts(assistant_text, transport.capabilities, number_options=True)
    try:
        await send_channel_parts(transport, link.channel_id, parts, thread_id=link.thread_id)
        logger.info(
            "cross-surface: mirrored reply to %s:%s (%d part(s))",
            link.channel_type,
            link.channel_id,
            len(parts),
        )
    except Exception:
        logger.debug("Failed to mirror reply to %s", link.channel_type, exc_info=True)


async def _deliver_cross_surface_user_message(
    state: Any, session_key: str, user_message: str
) -> None:
    """Mirror the user's dashboard message to a linked NON-Slack channel.

    The user-message half of the channel-neutral cross-surface leg: before the
    turn's reply is delivered, push what the user typed in the dashboard to the
    linked channel so the remote conversation reads coherently (question then
    reply), matching Slack's ``💬 _msg_`` echo. Capability-gated and best-effort,
    mirroring ``_deliver_cross_surface_reply``. Slack is handled by its dedicated
    streaming mirror; the caller guards out slash commands and recovery turns.
    """
    if not user_message:
        return
    # Same gate as the reply leg: these are the two sites that carry turn output,
    # and a disconnect silences both or the remote conversation reads as a
    # question with no answer.
    if mirror_is_paused(state, session_key):
        return
    target = _resolve_mirror_target(state, session_key)
    if target is None:
        return
    link, transport = target
    try:
        await transport.send_message(
            link.channel_id,
            f"💬 {_prepare_mirror_msg(user_message)}",
            thread_id=link.thread_id,
        )
        logger.info(
            "cross-surface: mirrored user message to %s:%s",
            link.channel_type,
            link.channel_id,
        )
    except Exception:
        logger.debug("Failed to mirror user message to %s", link.channel_type, exc_info=True)


_MIRROR_ECHO_LIMIT = 500


def _prepare_mirror_msg(raw_user_message: str) -> str:
    """Prepare a user message for the cross-surface / Slack mirror echo.

    Redacts through the canonical ``redact_via_context`` egress shim over the
    FULL text, then truncates — bounding first can cut a credential at the
    boundary into fragments no redaction regex matches, so it would escape into
    the mirrored echo. The shim's standalone fallback is the OSS baseline
    ``security.redact``, so a standalone host keeps the previous redaction
    behaviour.

    Scanned in DISPLAY form as well, like the assistant leg above and the Slack
    chokepoint: this echo goes to a channel without passing a renderer, and a
    credential the user typed with markdown between its halves is whole once the
    client renders the markup away.
    """
    safe, _ = redact_for_display(raw_user_message or "", redact_via_context)
    if len(safe) <= _MIRROR_ECHO_LIMIT:
        return safe
    # Mark the cut. A bare slice ends mid-word with nothing after it, which
    # reads as a message that broke rather than one that was shortened.
    return safe[: _MIRROR_ECHO_LIMIT - 1] + "…"
