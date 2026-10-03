"""Who a cross-surface delivery may reach: the recipient principal ladder and the channel target."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _RECIPIENT_LOGGED,
        _RECIPIENT_LOGGED_CAP,
        CHAT_TYPE_DIRECT,
        SLACK_NAMESPACE,
        logger,
        parse_session_key,
        sel,
        verify_mirror_admission,
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
    ``handlers/messaging._deliver_channel_dm`` is the case: it addresses a
    ``configured_targets()`` entry rather than a conversation, so its link carries a
    ``user:<id>`` target id and its session key is a host sentinel that names nobody.
    Deriving from that key would yield no principal and refuse a send whose
    recipient came off the transport's own allow-list. ``None`` means derive;
    a string is used verbatim.

    *check_recipient* lets the ONE caller whose link does not yet name a
    conversation opt out of the recipient leg: the mirror-link creation
    pre-check (``chat_mirror.api_chat_slot_mirror_link``) runs this ladder on the
    CONFIGURED-TARGET spelling (``user:<id>``) because channel-scope governance
    must precede ``resolve_configured_target``'s possible network side effect —
    but ``may_send_to`` is a recipient predicate over conversation ids, so that
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
