"""Hardened proactive Slack egress. One hardened chain, three shared names.

WHAT IS SHARED TODAY, as a count rather than an intention:
``channel_egress_permitted`` (the compaction notice and this module's own chain),
``OFFERED_ACTIVATIONS`` (``chat_slack``) and ``audit_egress`` (the note mirror's
two refusal sites plus this chain). The hardened chain itself,
``_deliver_slack_governed``, has exactly ONE consumer -- the note mirror's Slack
leg -- so this module is a shared HOME for the gates and a single-consumer home
for the chain. Naming that plainly here rather than calling the whole module
"shared across features": a reader who takes the chain for a repo-wide invariant
will assume protection that four direct senders and seven resolve-then-send
sites do not have.

A *proactive* Slack send has no inbound message to answer -- a cron result, a
compaction notice, a note mirrored to the session's channel -- so it names its own
recipient, and nothing in the request proves that recipient is still authorized.
This module is where the ``/note`` mirror chain decides. It is NOT the single
gate for proactive Slack generally: other proactive senders still reach
``slack_client.post_message`` directly (compaction notices, channel replies and
DMs, chat-runner mirrors), so treat the guarantees below as scoped to callers that
actually route through ``_deliver_slack_governed`` rather than as a repo-wide
invariant. Those senders are named, tier by tier, at their own send sites and in
the census that pins them, and the consolidation they are waiting on is owned by
``docs/request-for-change/rfc-proactive-slack-egress-consolidation.md`` rather than
by this paragraph.

It lives here rather than beside any one caller BY DESIGN. It began inside the
compaction-notice module because that was its first consumer, which made a
cross-feature boundary look like a feature detail: the next caller either misses
it or copies it, and a copied egress check is one that stops being re-verified.
The boundary is the module.

The sequence a caller gets, in order, is what makes it hardened:

* **recipient authorization** -- ``_slack_recipient_basis`` names WHICH authority
  permits this recipient (session principal, tracked channel, configured channel,
  owner DM) and fails closed when none does. Four, not three: a channel the
  picker OFFERED is a valid destination for a note, and omitting it here would
  describe a narrower authority set than the module grants.
* **governance** -- ``channel_egress_permitted`` vets the send against the
  ``channels`` scope and records a SEL decision for grant AND denial.
* **a synchronous last word** -- both checks above await, so the decision cannot
  be carried across them. Only the NAME of the authority is, and it is
  re-asserted synchronously immediately before the send.
* **per-chunk revalidation** -- a long message is chunked, and the whole chain is
  re-asked per chunk, because revocation mid-send must stop the remainder.

Any binding disagreement is a REFUSAL, never a retarget: delivering to the
replacement conversation is the exposure the checks exist to prevent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.messaging.link import SLACK_NAMESPACE, parse_session_key
from kiro_crew.messaging.renderer import chunk_text, display_safe, send_parts_revalidating
from kiro_crew.platform.context import PlatformCompositionError, governance_generation
from kiro_crew.platform.governance_profiles import vet_and_audit
from kiro_crew.sel import sel
from kiro_crew.slack.format import SLACK_MSG_LIMIT

logger = logging.getLogger(__name__)

#: Which authority permitted a Slack recipient. Returned by
#: ``_slack_recipient_basis`` and re-asserted SYNCHRONOUSLY immediately before the
#: send, because both that function and the governance re-check after it await --
#: so the decision itself cannot be carried across them, only the name of the
#: authority that made it.
#: The tool name every send through this module is audited under. A parameter here
#: only ever received one value from its single consumer, so it was folded to a
#: constant; reintroduce the parameter with the second adopter, which is also when
#: the "shared module" framing starts being true. ``channel_egress_permitted``
#: keeps its own ``tool_name`` argument -- that one has two real consumers.
EGRESS_TOOL_NAME = "chat.note"

#: Operation name on every Slack-send audit row. Module-level because a SECOND
#: site now audits a refusal on this leg -- ``chat_note_mirror._deliver_slack``
#: rejects a note whose session had no Slack thread when it was written -- and an
#: operation string spelled twice is one rename away from splitting the audit
#: stream into two buckets an operator has to know to union.
OP_SLACK_SEND = f"{EGRESS_TOOL_NAME}.slack_send"

_BASIS_PRINCIPAL = "session_principal"
_BASIS_TRACKED = "tracked_channel"
_BASIS_CONFIGURED = "configured_channel"
_BASIS_OWNER_DM = "owner_dm"


#: The activation modes ``chat_slack.list_slack_channels`` OFFERS in the dashboard's
#: Slack link picker. The authority is deliberately keyed to this exact set rather
#: than to "is configured at all": ``review`` and ``off`` channels appear in
#: ``slack_channels`` but are never presented as destinations, so honouring them
#: would let a note reach somewhere the product does not let a user bind to.
#:
#: This is the ONE spelling of that set. ``chat_slack.list_slack_channels`` imports
#: it rather than repeating the literal, because two copies drift apart silently and
#: either widens note egress past what the picker offers or starves it below.
OFFERED_ACTIVATIONS = frozenset({"always", "mention", "observe"})


def _configured_channel_active(channel_id: str) -> bool:
    """Is *channel_id* a configured Slack channel in an OFFERED activation mode?

    Synchronous by design — callers run it through ``asyncio.to_thread``, the same
    convention ``_egress_permitted`` uses, because ``KiroCrewConfig.load`` does real
    filesystem work: a two-``stat`` fingerprint and, when that misses, a full read
    and parse. That is the "unbounded on slow or networked storage" cost this module
    already refuses to pay on the event loop, so no caller may invoke this directly
    from a coroutine. The re-read is still a genuine revocation check: editing the
    config busts the fingerprint, so a deactivated channel stops authorizing on the
    next read.

    Fails closed: any error reading the config is "cannot tell", and this boundary
    refuses those.
    """
    try:
        cfg = KiroCrewConfig.load()
        entry = cfg.slack_channels.get(channel_id)
    except Exception:
        logger.debug(
            "slack egress: configured-channel lookup failed for %s; denying (fail-closed)",
            channel_id,
            exc_info=True,
        )
        return False
    return entry is not None and getattr(entry, "activation", "") in OFFERED_ACTIVATIONS


def _send_time_authorities(
    session_key: str, channel_id: str, *, tool_name: str, read_configured: bool
) -> tuple[bool, bool | None, int | None]:
    """Both send-time authorities, resolved in ONE off-loop read.

    They are read together because reading them in two awaits makes whichever runs
    first go stale for the duration of the second, and there is no ordering of two
    awaits that avoids it -- put the config read after the governance re-check and
    the governance permit is stale; put it before and the config permit is. One hop
    removes the choice: nothing suspends between the two reads, so the synchronous
    tail re-asserts a config value that no await separates from ``post_message``.

    Governance is evaluated FIRST and the config read second, deliberately: that
    makes the config value the LATEST fact this function has, so a deactivation
    landing while governance is being evaluated is still caught by this same call
    rather than deferred to the next chunk.

    THE GENERATION IS WHY THAT ORDERING IS SAFE. One hop removes the two-await
    choice but not the window INSIDE the hop: the config read runs after the permit
    is decided, and ``policy_distribution.apply_ceiling`` can install a new ceiling
    during it, which would leave ``permitted`` describing a ceiling no longer
    installed. So the generation is sampled BEFORE the permit and returned with it,
    and the synchronous tail refuses if it has moved by the time the send is
    adjacent. ``None`` means the counter could not be read at all, which the tail
    treats as a refusal rather than as unchanged -- an authority that cannot be
    re-confirmed has not been re-confirmed.

    Runs in a worker thread. ``tool_name`` is passed in because it is a closure
    value in ``_deliver_slack_governed`` and this function is module-level so the
    thread target carries no captured state.
    """
    try:
        generation: int | None = governance_generation()
    except Exception:
        logger.warning("slack egress: governance generation unreadable", exc_info=True)
        generation = None
    permitted = channel_egress_permitted(session_key, SLACK_NAMESPACE, tool_name=tool_name)
    configured = _configured_channel_active(channel_id) if read_configured else None
    return permitted, configured, generation


def _governance_ceiling_unchanged(observed: int | None) -> bool:
    """Whether the ceiling behind a permit is still the installed one.

    SYNCHRONOUS, and that is the point: it is the last confirmation before
    ``post_message`` and a coroutine here would reopen the window it exists to
    close. ``governance_generation`` is a counter read under a lock, so there is no
    I/O to move off the event loop.

    Both failure arms refuse. An *observed* of ``None`` means the sample never
    happened, and a raising re-read means this call cannot answer; neither is
    evidence the permit still holds, and treating either as unchanged would send on
    exactly the stale authority the sample exists to detect. The counter is opaque
    and comparison-only, so this tests equality and never magnitude.
    """
    if observed is None:
        return False
    try:
        return governance_generation() == observed
    except Exception:
        logger.warning("slack egress: governance generation re-read failed", exc_info=True)
        return False


def channel_egress_permitted(session_key: str, channel_type: str, *, tool_name: str) -> bool:
    """Vet an outbound notice against the ``channels`` governance scope.

    The non-Slack leg inherits this from ``_resolve_channel_target``. Slack is
    deliberately absent from ``channel_transports`` and so never reaches that
    ladder, which would leave its notice as the one unvetted, unaudited egress
    in this module. Fail-closed: a degraded evaluation denies rather than
    degrading to permit, matching the ladder and the other ``channels``-scope
    gates. ``vet_and_audit`` records a SEL decision for both grant and denial.

    Shared with ``chat_note_mirror``, whose Slack leg has the same shape and the
    same missing-ladder problem. *tool_name* is what distinguishes the two in the
    SEL record, so an operator can tell a compaction notice from a background
    note; it is a caller identity, never a permission input. Duplicating a
    fail-closed gate is how the two copies drift, so this is one function with a
    parameter rather than two that look alike.

    Synchronous by design — callers run it through ``asyncio.to_thread`` because
    the gate reads the profile directory.
    """
    try:
        decision = vet_and_audit(
            "channels",
            channel_type,
            session_key=session_key,
            tool_name=tool_name,
            fail_closed=True,
        )
    except PlatformCompositionError:
        # An invalid governance ceiling is not an ordinary skip: the ladder
        # deliberately re-raises rather than degrading, and so does this gate.
        raise
    except Exception:
        logger.debug(
            "compact notice: governance check failed for %s; denying (fail-closed)",
            session_key,
            exc_info=True,
        )
        return False
    # Default False: a Decision without ``permitted`` is an unusable answer from
    # a gate and must not read as permission.
    return bool(getattr(decision, "permitted", False))


def audit_egress(
    *,
    channel_id: str,
    operation: str,
    session_key: str,
    outcome: str,
    reason: str,
    label: str = "reason",
) -> None:
    """Record ONE Slack-egress decision, allow or deny, with a per-case reason.

    One function rather than a closure per caller for the reason the rest of this
    module is one copy: an audit that exists in one branch and not its sibling is
    worse than none, because it makes the log look complete.

    *reason* is a stable CODE, not prose: an operator filters rows by it to answer
    "what refused this", so two different refusals must never share one.

    *label* names what the code MEANS, and the two callers genuinely differ. An
    authorization row reports the ``basis`` that named the recipient — which
    authority said yes — while a refusal reports the ``reason`` it stopped. Folding
    both under one word would cost the operator that distinction, so it stays a
    parameter rather than being normalised away.

    Never raises. A SEL outage must not convert a refusal into an exception on a
    path whose whole job is to fail closed quietly.
    """
    try:
        sel().log_api_access(
            caller=channel_id or "unknown",
            operation=operation,
            outcome=outcome,
            source=SLACK_NAMESPACE,
            resources=f"session={session_key} {label}={reason}",
        )
    except Exception:
        logger.warning("slack egress: SEL logging failed for %s", operation, exc_info=True)


async def _deliver_slack_governed(
    state: Any,
    session_key: str,
    text: str,
    *,
    thread_ts: str,
    channel_id: str,
    relink: Callable[[], tuple[str, str]],
    is_paused: Callable[[], bool],
) -> None:
    """The hardened Slack send, used by the one proactive caller that adopts it.

    Covers exactly the ``/note`` channel mirror. It is NOT what every proactive
    non-turn Slack egress goes through: reading it as a repo-wide guarantee would
    let the next reader assume a site is protected because this function exists.
    Four proactive Slack sends post directly instead, and adopting the chain at any
    of them widens that site's refusal set, which is a behaviour change to a surface
    this chain does not otherwise touch, so each belongs in a change where the
    tightening is the subject under review rather than a rider.

    Which four, and the seven resolve-then-send siblings, and
    ``upload_destination._slack_egress_permitted`` as a third copy of the shared
    gate, are enumerated ONCE by symbol in
    ``docs/request-for-change/rfc-proactive-slack-egress-consolidation.md``, which
    owns the consolidation. The tier map is pinned mechanically by the census test
    and marked at each site, so this docstring restates no part of it.

    Slack is deliberately absent from ``channel_transports``, so it never reaches
    ``_resolve_channel_target``'s ladder and gets none of that ladder's protection
    for free. This is where that protection lives instead, in ONE copy: governance
    gate, recipient authorization, coordinate and pause revalidation after every
    await, per-transport chunking, and abort-on-revocation between chunks.

    The flush-time mirror is now CLOSED: a note held while a turn runs carries the
    destinations it was authored for on its held record, and ``flush_deferred_notes``
    dispatches them once both halves commit. That covers the mid-turn case background
    senders mostly hit, which was the largest remaining gap in this list.

    This function is PRIVATE, and deliberately so: it has exactly one consumer
    (``chat_note_mirror._deliver_slack``). The module's shared surface is
    ``channel_egress_permitted`` and ``OFFERED_ACTIVATIONS`` -- the two names with
    counted cross-module consumers. The tool identity is a module constant rather
    than a parameter for the same reason: one caller only ever passed one value, so
    the parameter was scaffolding for an adopter that does not exist yet.
    Reintroduce both the parameter and the public name with the second adopter,
    which is also when "shared across features" starts being a count rather than a
    promise. ``channel_egress_permitted`` beside it is the measured case for this
    file existing at all: two consumers in two modules (this chain and the
    compaction notice), so the alternative is a duplicated fail-closed gate, which
    is how the two copies drift -- the unhardened twin is exactly what happened
    while this lived inside the note module.

    *relink* re-reads the binding for the post-await coordinate checks -- the
    caller knows where a link comes from, and re-reading is the whole point, so it
    cannot be a captured value. *is_paused* is REQUIRED, not optional: a send that
    cannot answer "was this paused underneath me" has no business completing, so
    the check is part of the contract rather than something a call site can leave
    off. An optional predicate makes omitting it an accident; a required one makes
    it impossible.

    RETURNS NOTHING. Its one consumer discarded the bool, and every outcome this
    chain can reach already files a SEL row naming it, so the value duplicated the
    audit trail for no reader. A future adopter that genuinely needs the verdict
    should take it from the row rather than reintroduce a second source of truth.
    """
    # Lazy: chat_runner imports state at module scope, and state imports THIS
    # module, so either at top level would close a cycle. The module already
    # avoids the same one for `_resolve_channel_target`.
    from kiro_crew.dashboard.chat_runner import _session_principal
    from kiro_crew.dashboard.state import _split_namespaced_channel_id
    from kiro_crew.slack.handler import is_allowed_user, is_tracked_channel

    _OP_SEND = OP_SLACK_SEND
    _OP_AUTHZ = f"{EGRESS_TOOL_NAME}.slack_authorize"

    def _refuse(reason: str) -> None:
        """Audit an early refusal, then refuse.

        The checks below run AHEAD of the governance gate and the recipient
        authorization, both of which audit their own outcomes. Returning from here
        unaudited left the two earliest refusals as the only silent ones on the
        path, so a note stopped by either was absent from the log entirely rather
        than present as a denial — the state an operator cannot tell apart from a
        note that was never written.

        Returns nothing: each call site reads ``_refuse(...)`` then ``return``,
        keeping the audit and the refusal impossible to separate in a later edit.
        """
        audit_egress(
            channel_id=channel_id,
            operation=_OP_SEND,
            session_key=session_key,
            outcome="denied",
            reason=reason,
        )
        return

    client = getattr(state, "slack_client", None)
    if client is None:
        # NOT audited, deliberately: no Slack transport is configured at all, so
        # nothing was refused about THIS request and no destination was ever
        # evaluated. A row here would fire on every note in a Slack-less
        # deployment and dilute the denials the log exists to surface.
        return
    # A disconnected thread is muted for outbound. Background egress is not
    # strictly what a disconnect aims at -- but it is the user saying "not into
    # this conversation", which covers a message ABOUT it as much as a reply IN
    # it, and the dashboard transcript still carries the line either way. Asked
    # before resolving so a muted thread costs no lookup. REQUIRED, not optional:
    # a send that cannot answer "was this paused underneath me" has no business
    # completing, so the parameter is typed non-optional and asked unconditionally.
    if is_paused():
        _refuse("thread_disconnected")
        return
    # A CHANNEL is required; a THREAD is not. The guard this replaces
    # (``_is_genuine_slack_link``) demanded both, which is right for the note leg
    # and wrong here: a session bound to a channel with no thread posts top-level,
    # and requiring a thread would have silently dropped its notices. What the
    # check is actually FOR is refusing another channel's legacy namespaced id, and
    # that has nothing to do with threads -- so the namespace half lives here,
    # shared, and the thread half stays with the caller that needs it.
    if not channel_id:
        _refuse("no_channel_id")
        return
    _namespaced = _split_namespaced_channel_id(channel_id)
    if _namespaced is not None and _namespaced[0] != SLACK_NAMESPACE:
        _refuse("foreign_namespace")
        return

    def _coordinates_still_valid(after: str) -> bool:
        """Re-read the destination and the pause state. Synchronous BY DESIGN.

        Called ONCE, after the last await and before the send, because that is
        where the guarantee lives: every await above is a window in which the user
        can unlink the thread or disconnect the mirror, and both are recorded by
        rewriting the state this function re-reads. A fresh read here therefore
        catches a change made during ANY of them. Posting on captured coordinates
        would deliver into a conversation that was revoked before the message left
        — the audience those controls exist to remove.

        It must contain no await itself, or it would open the very window it
        closes: the guarantee this function provides is only ever "nothing has
        yielded between this check and the send", and one await inside here would
        void that for its own caller.

        Any disagreement is a refusal, never a retarget — the caller asked to
        reach the conversation that was authorized, not whichever one replaced it.
        """
        fresh_ts, fresh_channel = relink()
        if (fresh_ts, fresh_channel) != (thread_ts, channel_id):
            sel().log_api_access(
                caller=channel_id or "unknown",
                operation=_OP_SEND,
                outcome="denied",
                source=SLACK_NAMESPACE,
                error=f"link changed during {after}",
            )
            logger.info(
                "slack egress: slack link for %s changed during %s; refusing", session_key, after
            )
            return False
        if is_paused():
            sel().log_api_access(
                caller=channel_id or "unknown",
                operation=_OP_SEND,
                outcome="denied",
                source=SLACK_NAMESPACE,
                error=f"thread disconnected during {after}",
            )
            return False
        return True

    async def _egress_permitted() -> bool:
        """Ask the ``channels`` governance gate. AWAITS, so the coordinate rung
        before the send is what covers it.

        Off-loop: the gate walks the profile directory (iterdir + stat, with a
        possible reload), which is unbounded on slow or networked storage. One
        helper rather than two spelled-out calls, because the two must ask the
        SAME question -- a copy that drifted on ``tool_name`` would file the
        second decision under a different caller identity. ``vet_and_audit``
        records its own SEL decision for grant AND denial, so this adds none.
        """
        return bool(
            await asyncio.to_thread(
                channel_egress_permitted,
                session_key,
                SLACK_NAMESPACE,
                tool_name=EGRESS_TOOL_NAME,
            )
        )

    def _authority_still_holds(basis: str, *, configured_active: bool | None) -> bool:
        """Re-assert the authority that permitted this recipient. SYNCHRONOUS.

        This is the terminating answer to a chain that would otherwise never
        close: ``_slack_recipient_authorized`` awaits, and so does the governance
        re-check that follows it, so each new async check staled the one before it
        and adding a further await would only push the window one layer down.
        Three of the four authorities can be re-asked with NO I/O at all -- both
        roster reads are in-memory, and the last is an identity comparison against a
        DM id that was already resolved. The configured-channel read is the one that
        touches the filesystem, so it is NOT performed here: the caller re-reads it
        off the loop and passes the result in as *configured_active*, leaving this
        function free of both awaits and blocking work. So the last word before the
        send is synchronous, which is the only shape that leaves no window at all.

        The owner-DM basis is re-asserted only as ``channel_id`` still being a
        ``D…`` while an owner is still configured. It does NOT re-check that the
        owner is the same one that authorized the send: the check reads the CURRENT
        ``owner_id`` and tests it for non-emptiness, so an owner swapped between
        authorization and send still passes. Detecting that would mean comparing
        against the authorizing value or re-resolving the DM, and re-resolving needs
        an await, which would reopen the very window this function closes.
        """
        if basis == _BASIS_PRINCIPAL:
            principal = _session_principal(session_key)
            return bool(principal) and is_allowed_user(principal)
        if basis == _BASIS_TRACKED:
            # Untracking the channel is the revocation, and this read is a set
            # membership test -- exactly the check the lane asked to be last.
            return is_tracked_channel(channel_id)
        if basis == _BASIS_CONFIGURED:
            # Deactivating the channel (or removing it) is the revocation, and the
            # config fingerprint makes the caller's re-read see it. Re-read per send
            # rather than captured at authorization, so a mid-send deactivation
            # stops the remaining chunks. ``None`` means the caller did not read it
            # -- "cannot tell" -- and this boundary refuses those.
            return configured_active is True
        if basis == _BASIS_OWNER_DM:
            return bool(getattr(state, "owner_id", "") or "") and channel_id.startswith("D")
        # An unrecognised basis is "cannot tell", and this boundary refuses those.
        return False

    async def _permitted_to_send(before: str) -> bool:
        """The whole permission chain, re-asked. False means do not send *before*.

        Ordered so that the LAST awaits happen before the last checks, never
        between them and the send. The governance gate is the final await;
        everything after it is synchronous, so no permission's confirmation is
        separated from the send by a yield.

        ONE COORDINATE RUNG, NOT ONE PER AWAIT. Earlier revisions re-read the
        coordinates after every await on this path. Those intermediate rungs were
        subsumed: the rung below runs after the last await and before the send, and
        it compares a FRESH ``relink()`` against the coordinates captured at entry,
        so a change during ANY of the awaits above is caught there just the same.
        What the intermediates added was a fail-fast, not a guarantee -- and the
        guarantee is the whole reason this chain exists, so the rungs that did not
        carry it are chain the declared second adopter would inherit for nothing.
        The one window no rung can close is the ``post_message`` await itself, which
        is why the loop re-asks per chunk rather than once before it.
        """
        if not await _egress_permitted():
            return False
        # Awaits too: the owner-DM basis resolves the owner's DM through
        # `conversations.open`, a network call. Returns the BASIS, not a verdict,
        # because the verdict cannot survive the awaits that follow it.
        basis = await _slack_recipient_basis(state, session_key, channel_id, operation=_OP_AUTHZ)
        if not basis:
            return False
        # GOVERNANCE AND THE CONFIGURED-CHANNEL AUTHORITY ARE ONE AWAIT. The gate
        # further up ran BEFORE `_slack_recipient_authorized`, which awaits
        # `conversations.open` for the owner-DM basis -- so an admin narrowing the
        # `channels` scope during that call was invisible here: the coordinate rung
        # below compares the thread and channel, not the egress permission, and
        # the note would post after the revocation. Asking again is what turns that
        # window into a refusal. The configured-channel read rides in the SAME hop
        # rather than a second await, because two awaits leave whichever ran first
        # stale for the duration of the other and no ordering of two avoids that;
        # with one hop the synchronous tail below re-asserts a config value that
        # nothing suspends between reading and sending. Same shape as the sibling
        # leg's per-chunk re-resolve: re-ask, then refuse -- never retarget.
        permitted, configured_active, permit_ceiling = await asyncio.to_thread(
            _send_time_authorities,
            session_key,
            channel_id,
            tool_name=EGRESS_TOOL_NAME,
            read_configured=(basis == _BASIS_CONFIGURED),
        )
        if not permitted:
            sel().log_api_access(
                caller=channel_id or "unknown",
                operation=_OP_SEND,
                outcome="denied",
                source=SLACK_NAMESPACE,
                error=f"slack egress revoked during recipient authorization before {before}",
            )
            logger.info(
                "slack egress: slack egress for %s was revoked during recipient "
                "authorization; refusing before %s",
                session_key,
                before,
            )
            return False
        # THE LAST THREE CHECKS, AND ALL ARE SYNCHRONOUS. This is what closes the
        # window the governance re-check above would otherwise have opened: that
        # await could carry a tracked-channel revocation, and a coordinate check
        # alone would not see it -- it compares the thread and channel, not whether
        # the recipient is still an authorized destination. Re-asserting the
        # authority here, with nothing yielding between this line and
        # `post_message`, is the only ordering in which every permission's last
        # confirmation is adjacent to the send.
        if not _coordinates_still_valid(f"the governance re-check before {before}"):
            return False
        if not _authority_still_holds(basis, configured_active=configured_active):
            sel().log_api_access(
                caller=channel_id or "unknown",
                operation=_OP_SEND,
                outcome="denied",
                source=SLACK_NAMESPACE,
                error=(
                    f"recipient authorization ({basis}) revoked during the governance "
                    f"re-check before {before}"
                ),
            )
            logger.info(
                "slack egress: slack recipient authorization for %s (%s) was revoked "
                "during the governance re-check; refusing before %s",
                session_key,
                basis,
                before,
            )
            return False
        if not _governance_ceiling_unchanged(permit_ceiling):
            sel().log_api_access(
                caller=channel_id or "unknown",
                operation=_OP_SEND,
                outcome="denied",
                source=SLACK_NAMESPACE,
                error=(
                    f"governance ceiling changed after the egress permit was read, "
                    f"before {before}"
                ),
            )
            logger.info(
                "slack egress: the governance ceiling behind %s's egress permit moved "
                "before %s; refusing rather than sending on the superseded permit",
                session_key,
                before,
            )
            return False
        return True

    # Defang broadcast mentions. The note body is caller-controlled — a cron or an
    # app writes it — and Slack PARSES ``<!channel>`` / ``<!everyone>``, so an
    # unescaped one turns a background note into a mass notification for everyone
    # in the thread's channel. Applied ON TOP of the platform-aware redaction
    # above rather than instead of it: ``display_safe`` re-runs only the OSS
    # baseline pair, so using it alone would drop a companion's extra regexes.
    safe = display_safe(text)
    # Chunked, because Slack TRUNCATES past its limit and still answers with a ts —
    # a delivery this function would otherwise report as complete. The transport
    # leg gets this from the shared helper; without it here a long note (a diff or
    # a log tail) arrived whole on Telegram and cut short on Slack alone.
    parts = chunk_text(safe, SLACK_MSG_LIMIT) or [safe]

    async def _still_permitted(index: int) -> bool:
        if await _permitted_to_send(f"part {index + 1}/{len(parts)}"):
            return True
        if index:
            sel().log_api_access(
                caller=channel_id or "unknown",
                operation=_OP_SEND,
                outcome="denied",
                source=SLACK_NAMESPACE,
                error=(
                    f"revoked mid-send; aborted before part {index + 1} of "
                    f"{len(parts)}, {index} already delivered"
                ),
            )
            logger.warning(
                "slack egress: slack destination for %s revoked mid-send; "
                "aborted before part %d of %d (%d already delivered)",
                session_key,
                index + 1,
                len(parts),
                index,
            )
        return False

    async def _send_part(_index: int, part: str) -> bool:
        try:
            ts = await client.post_message(channel_id, part, thread_ts or None)
        except Exception:
            # Terminal, so it files a row: the transport leg audits the same outcome
            # as ``error``/``transport_error`` and a silent sibling breaks that parity.
            audit_egress(
                channel_id=channel_id,
                operation=_OP_SEND,
                session_key=session_key,
                outcome="error",
                reason="slack_error",
            )
            logger.debug("slack egress: slack delivery failed for %s", session_key, exc_info=True)
            return False
        # An empty ts is Slack's refusal shape, and reporting it as delivered is
        # the same false-success the transport leg's ``delivery_confirmed`` exists
        # to prevent.
        if not ts:
            audit_egress(
                channel_id=channel_id,
                operation=_OP_SEND,
                session_key=session_key,
                outcome="error",
                reason="empty_ts",
            )
            logger.debug("slack egress: slack returned no ts for %s", session_key)
            return False
        return True

    if not await send_parts_revalidating(
        parts, still_permitted=_still_permitted, send_part=_send_part
    ):
        return
    # EVERY terminal leg files exactly one row -- this success and both failures
    # above -- so an operator answers "did this note reach Slack" from a row.
    audit_egress(
        channel_id=channel_id,
        operation=_OP_SEND,
        session_key=session_key,
        outcome="completed",
        reason="delivered",
    )
    logger.info("slack egress: delivered to %s for %s", channel_id, session_key)


async def _slack_recipient_basis(
    state: Any, session_key: str, channel_id: str, *, operation: str
) -> str:
    """Which authority permits this Slack conversation, or ``""`` if none does.

    Returns the BASIS rather than a bare bool, because the caller has to re-assert
    it synchronously immediately before sending: this function awaits (the
    owner-DM branch resolves ``conversations.open``), and so does the governance
    re-check that follows it, so a bool captured here is stale by send time. The
    basis names which authority to re-ask, and three of the FOUR -- session
    principal, tracked channel and owner DM -- can be re-asked with no await and no
    I/O at all. The fourth, the configured channel, is read off the loop in the same
    hop as the governance re-check and passed in, so the re-assertion itself stays
    synchronous -- see ``_authority_still_holds``.

    The transport leg gets recipient re-authorization from the shared ladder
    (``transport.may_send_to``). Slack does not: it is absent from the transport
    registry, and ``SlackTransport.may_send_to`` documents that it could not
    answer anyway, because a Slack link persists a CONVERSATION id (``D…``/``C…``)
    while the roster holds USER ids. So the check has to be made here, against the
    authorities that can actually judge it — and it must be made at SEND time, not
    at link time: a link is persisted once and outlives the roster that authorized
    it, so a recipient removed from the allow-list afterwards keeps receiving every
    note until something re-asks.

    Four authorities, tried in order:

    1. the principal the SESSION KEY names, for a 1:1 channel-born session —
       ``_session_principal`` exists precisely because the link records a
       conversation rather than a person.
    2. the tracked-channel roster, for a conversation configured as a
       destination. Untracking it is the revocation.
    3. the CONFIGURED channel, for a channel the dashboard's own link picker
       OFFERS in an activation mode from ``OFFERED_ACTIVATIONS``. Such a channel
       carries no principal, is not on the separate tracking roster, and is not a
       ``D…`` owner DM, so without this authority the other three all decline and
       a note the product invited the user to bind is dropped silently.
       Deactivating the channel in config is the revocation.
    4. the OWNER'S DM. A dashboard-created link is the default case and lands
       here: it carries no principal (a ``dashboard:`` key names no peer) and its
       ``D…`` id is not a tracked channel, and it is not a configured channel
       either, so all three authorities above decline a conversation that is in
       fact the intended destination. Resolved through the
       owner identity that created the link — ``conversations.open`` is idempotent
       and returns the same ``D…`` for the same user, so comparing against it is an
       identity check rather than a guess. Revocation still works: changing
       ``owner_id`` changes the DM this resolves to.

    EVERY return is audited, allow and deny alike. A denial-only audit answers
    "was anything refused" but not "who was this note authorized to reach", and on
    a security control the successful decision is the one an operator needs when
    reconstructing what left the building.

    FAIL-CLOSED: no authority answering is a refusal, not a pass. This feeds a
    network egress boundary, so "cannot tell" and "not authorized" must have the
    same outcome — the stance ``_resolve_channel_target`` also takes when its
    allow-list check raises.
    """

    def _audit(outcome: str, reason: str) -> None:
        audit_egress(
            channel_id=channel_id,
            operation=operation,
            session_key=session_key,
            outcome=outcome,
            reason=reason,
            label="basis",
        )

    # Lazy for the same cycle reason as its caller.
    from kiro_crew.dashboard.chat_runner import _session_principal
    from kiro_crew.slack.handler import is_allowed_user, is_tracked_channel

    principal = _session_principal(session_key)
    # A principal is a PLATFORM user id, and `is_allowed_user` is SLACK's roster, so
    # the two only line up when the key's own surface IS Slack. A telegram or discord
    # direct session that is additionally linked to Slack names ITS platform's id, and
    # testing that against Slack's user roster asks a question about the wrong
    # namespace -- it cannot match, so the answer is meaningless rather than merely
    # negative.
    #
    # The damage is the RETURN, not the miss: this branch answers for the whole
    # function, so a cross-surface key consumed the decision and the tracked-channel
    # and owner-DM authorities never ran, refusing a destination one of them would
    # have authorized. Gating restores them for exactly the keys the principal cannot
    # speak for, and changes nothing for a Slack-origin key.
    #
    # Surface comes from `parse_session_key`, the one canonical address parser, rather
    # than a second decomposition here -- splitting the key by hand is precisely the
    # drift `_session_principal` warns about. A legacy or ungrammatical key parses to
    # None and already yields no principal, so this only ever narrows the grammar-
    # addressable case.
    parsed = parse_session_key(session_key)
    if principal and parsed is not None and parsed.surface == SLACK_NAMESPACE:
        allowed = is_allowed_user(principal)
        _audit("allowed" if allowed else "denied", "session_principal")
        return _BASIS_PRINCIPAL if allowed else ""
    if is_tracked_channel(channel_id):
        _audit("allowed", "tracked_channel")
        return _BASIS_TRACKED
    # A channel configured in ``slack_channels`` is a destination the dashboard's
    # own link picker OFFERS (``chat_slack.list_slack_channels``), so a user can
    # bind a session to one. It carries no principal, is not on the SEPARATE
    # tracking roster, and is not a ``D…`` owner DM, so without this the three
    # other authorities all decline and a note the product invited is dropped
    # silently. Read off the loop: the config load is filesystem work, and this
    # ladder is a coroutine.
    if await asyncio.to_thread(_configured_channel_active, channel_id):
        _audit("allowed", "configured_channel")
        return _BASIS_CONFIGURED
    owner = getattr(state, "owner_id", "") or ""
    client = getattr(state, "slack_client", None)
    if owner and client is not None and channel_id.startswith("D"):
        try:
            owner_dm = await client.open_dm(owner)
        except Exception:
            # Cannot establish identity -> refuse. An unresolvable owner DM is the
            # "cannot tell" case, and this boundary treats that as not authorized.
            logger.debug("slack egress: owner DM resolution failed", exc_info=True)
            _audit("denied", "owner_dm_unresolvable")
            return ""
        if owner_dm and owner_dm == channel_id:
            _audit("allowed", "owner_dm")
            return _BASIS_OWNER_DM
        _audit("denied", "not_owner_dm")
        return ""
    _audit("denied", "no_authority_names_recipient")
    logger.info(
        "slack egress: refusing Slack delivery for %s -- no authority names this recipient",
        session_key,
    )
    return ""
