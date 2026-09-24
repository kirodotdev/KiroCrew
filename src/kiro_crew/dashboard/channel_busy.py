"""Hand a channel message to the dashboard turn that is driving its resumed session.

A Discord, Telegram or Teams conversation can be bound to a dashboard session
(``dashboard:<slot>``) through the channel's session-switch command. Its turns
then run under that key, and so do the dashboard's own: the composer, a
``session_send`` from a peer session, a cron injection. When a channel message
arrives while such a turn is in flight, WHO holds the turn decides where the
message can wait.

* The channel itself (or a sibling turn the channel started) holds it: the
  channel's own mid-turn machinery applies -- ``_session/steer`` into the live
  provider, or the channel's ``SessionManager`` queue, drained from the tail of
  the channel turn that is running. Nothing here is involved.
* The DASHBOARD holds it: the channel's queue is the wrong place to wait. That
  queue is drained only from the tail of a channel-driven turn, and the dashboard
  turn loop knows nothing of it, so an entry left there would sit until some later
  channel turn on the same session and then run out of order -- or never, if the
  user keeps working from the dashboard. The dashboard slot already has a queue the
  running turn's teardown drains (``chat_delivery.queue_for_next_turn`` /
  ``_start_next_queued_turn``), so the message is handed to THAT queue and runs as
  the next dashboard turn in arrival order behind whatever the composer queued.
  Its reply reaches the channel through the session's outbound mirror, the same
  leg every dashboard turn on a linked session already takes.

The queue arm only, deliberately. The dashboard's steer path
(``steer_into_running_turn``) keeps its requeue bookkeeping in per-message maps
that record the composer's provenance and not a channel's, so a steer that the
turn never consumed would be requeued as dashboard-authored text and run with
the wider dashboard authority (``_directive_channel_origin`` is the narrower
credential boundary). Queueing carries the channel provenance and the
admission-time containment on the entry itself, which is what the drain's
re-validation reads -- and both survive a gateway restart under the gateway's own
proof (``slot_queue_repository.ORIGIN_PROOF_KEY``: an HMAC over the entry, its
address and its snapshot), which the restore verifies before handing either back,
so a hand-off queued before a restart is still released and reported after it,
while an edited or hand-written stamp comes back as nothing.

Attachments cannot ride this hand-off: a channel attachment is a transport
descriptor the CHANNEL turn downloads and ingests, and the dashboard queue has no
reader for it. Such a message is refused with a resend prompt rather than queued
without its files, which is the one outcome worse than refusing.

The precedent is Slack's linked-thread intercept (``slack/handler.py``), which
hands a linked thread's message to the slot queue with ``directive_user_origin``
and ``directive_channel_origin`` both set and lets the dashboard drain run it. It
consumes the same producer with the same origin stamp, so this module's drain
contract -- drop once the binding is gone, tell the conversation -- covers the
Slack thread too. This module is that contract for every channel that resumes a
dashboard session.

Dependency direction is ``<channel> -> dashboard``: the messaging package imports
nothing from the dashboard, so the channel dispatchers reach this module at call
time, the way they already reach ``channel_slots.project_channel_turn_live``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.constants import SLACK_NAMESPACE
from kiro_crew.dashboard.chat_delivery import queue_for_next_turn
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.upload_gate import live_dashboard_slot

logger = logging.getLogger(__name__)

#: Queue-entry ``meta`` key naming the channel CONVERSATION a handed-off message
#: came from (a serialized :class:`ChannelLink`). Three readers: the drain's
#: binding check (:func:`channel_binding_released`), the drop notice
#: (:func:`notify_channel_origin_dropped`), which has no other way to reach that
#: conversation once the binding it rode in on is gone, and the drain itself,
#: which hands the mapping to the turn (``_run_chat``'s ``_channel_origin``) so a
#: runner recovery requeues the message still stamped and the turn's
#: publications can ask whether that conversation is still a mirror room
#: (:func:`channel_origin_still_bound`). The same mapping carries
#: :data:`CHANNEL_ORIGIN_PRINCIPAL_KEY`, the platform user the dispatcher's
#: allow-list admitted the message from. Across a restart the stamp is worth
#: only what the gateway's proof over it says: the restore strips it and puts it
#: back when the proof verifies (``slot_queue_repository.restore_queue_provenance``).
CHANNEL_ORIGIN_META_KEY = "channel_origin"

#: Key INSIDE the :data:`CHANNEL_ORIGIN_META_KEY` mapping: the admitted sender's
#: platform id. The drop notice walks the governed send ladder, whose recipient
#: leg judges a DM against the transport's USER roster and refuses when it has
#: no principal to judge -- and a ``dashboard:`` session key names none. The
#: dispatcher established this principal when it admitted the message, so the
#: ladder is asked with it; ``may_send_to`` still re-checks the roster at send
#: time, so a recipient dropped from the allow-list meanwhile gets nothing.
CHANNEL_ORIGIN_PRINCIPAL_KEY = "principal"

#: The drain-time constraint recorded when the conversation that queued the entry
#: does not resume the session any more: the binding was released (``/unlink``, ``!new``)
#: or moved to another session while the entry waited. Named like the
#: ``session_control`` constraints so the audit row and the notice read the same.
CHANNEL_UNLINKED_CONSTRAINT = "unlinked"

#: What the channel user is told when their handed-off message is dropped at the
#: drain. ``{reason}`` is :func:`session_control.describe_containment_change`'s
#: phrase. Same shape as the channel drains' own dropped-replay notice.
CHANNEL_DROP_NOTICE = (
    "⚠️ Dropped your queued message: {reason} after it was queued, "
    "so it can no longer run where you sent it. Send it again if it still applies."
)

#: The message is in the dashboard slot's queue and runs as the next dashboard
#: turn; its reply mirrors to the channel.
HANDOFF_QUEUED = "queued"
#: The dashboard is driving, but the message carries attachments the dashboard
#: queue cannot hold. The caller answers with a resend prompt; nothing was queued.
HANDOFF_ATTACHMENTS_REFUSED = "attachments_refused"
#: No dashboard turn holds this session (no open slot, or the slot is idle from
#: the dashboard's point of view because a channel turn holds the lease). The
#: caller's own mid-turn machinery applies.
HANDOFF_NOT_DASHBOARD_TURN = ""


def dashboard_turn_in_progress(dashboard_state: Any, session_key: str) -> bool:
    """Whether a DASHBOARD turn is running on *session_key*'s open slot.

    ``slot.running`` (a live task or a stage controller) or the between-stages
    marker ``_in_stage_execution`` -- the same predicate the peer send path reads,
    because between a plan's stages the slot's lease is briefly free while the
    plan is still live, and a message admitted then must still wait in the slot
    queue rather than start a concurrent turn. A channel-driven turn on the same
    key sets neither: it holds the ``SessionManager`` lease and projects its
    result into the slot afterwards, so for it this answers False.
    """
    slot = live_dashboard_slot(dashboard_state, session_key)
    if slot is None:
        return False
    return bool(getattr(slot, "running", False) or getattr(slot, "_in_stage_execution", False))


def hand_to_dashboard_turn(
    dashboard_state: Any,
    session_key: str,
    text: str,
    *,
    has_attachments: bool = False,
    origin: ChannelLink | None = None,
    principal: str = "",
) -> str:
    """Queue *text* behind the dashboard turn running on *session_key*, if there is one.

    Returns one of the ``HANDOFF_*`` values. :data:`HANDOFF_NOT_DASHBOARD_TURN`
    means nothing was done and the caller keeps its own busy handling; the other
    two mean the caller must reply and return without driving a turn.

    The entry is stamped ``directive_user_origin`` (an allow-listed human typed it
    into the conversation bound to this session) and ``directive_channel_origin``
    (that conversation is a channel, so a directive derived from it keeps channel
    authority), and it records the containment that holds now
    (``containment_meta``): the session is linked by construction, so the drain's
    re-validation sees ``linked`` as admitted and drops the entry only for a
    constraint that appears AFTER this point.

    *origin* is the channel conversation the text came from. It rides the entry
    (:data:`CHANNEL_ORIGIN_META_KEY`) so the drain can tell the conversation when
    the entry is dropped, and it marks the entry as one whose reply route IS the
    session's binding: :func:`channel_binding_released` drops it once that binding
    is gone, because running it would answer into a session the user has left and
    the reply has nowhere to land. *principal* is the platform user the caller's
    allow-list admitted the text from; it rides the same stamp
    (:data:`CHANNEL_ORIGIN_PRINCIPAL_KEY`) so the drop notice can be authorized
    for a DM, whose roster is one of users.

    Synchronous, like ``queue_for_next_turn``: the enqueue, the broadcast and the
    persist kick-off are loop-side bookkeeping.
    """
    slot = live_dashboard_slot(dashboard_state, session_key)
    if slot is None or not dashboard_turn_in_progress(dashboard_state, session_key):
        return HANDOFF_NOT_DASHBOARD_TURN
    if has_attachments:
        return HANDOFF_ATTACHMENTS_REFUSED
    address: dict[str, Any] | None = None
    if origin is not None:
        address = origin.to_dict()
        if principal:
            address[CHANNEL_ORIGIN_PRINCIPAL_KEY] = principal
    queue_for_next_turn(
        dashboard_state,
        slot,
        text,
        directive_user_origin=True,
        channel_origin=True,
        channel_address=address,
    )
    logger.info(
        "channel message queued behind the dashboard turn on %s (slot %s)",
        session_key,
        getattr(slot, "key", "?"),
    )
    return HANDOFF_QUEUED


def channel_origin_principal(entry_meta: Any) -> str:
    """The platform user a handed-off entry was admitted from, or ``""``.

    Read from the same untrusted mapping as :func:`channel_origin_address`; only
    a non-empty string under :data:`CHANNEL_ORIGIN_PRINCIPAL_KEY` answers. Empty
    means the ladder derives its principal from the session key -- which for a
    dashboard key is nobody, so a DM notice is refused rather than sent unjudged.
    """
    if channel_origin_address(entry_meta) is None:
        return ""
    raw = entry_meta[CHANNEL_ORIGIN_META_KEY].get(CHANNEL_ORIGIN_PRINCIPAL_KEY)
    return raw if isinstance(raw, str) else ""


def channel_origin_address(entry_meta: Any) -> ChannelLink | None:
    """The conversation a handed-off entry came from, or None for any other entry.

    *entry_meta* is untrusted plumbing (any shape); only a mapping carrying a
    channel type and a conversation id under :data:`CHANNEL_ORIGIN_META_KEY`
    answers a link.
    """
    if not isinstance(entry_meta, dict):
        return None
    raw = entry_meta.get(CHANNEL_ORIGIN_META_KEY)
    if not isinstance(raw, dict):
        return None
    link = ChannelLink.from_dict(raw)
    if not link.channel_type or not link.channel_id:
        return None
    return link


def channel_binding_released(now: dict[str, Any], entry_meta: Any) -> bool:
    """Whether a handed-off entry's conversation stopped resuming the session.

    *now* is the drain-time ``containment_snapshot``. A channel resumes a
    dashboard session through a mirror link (``set_mirror_link(...,
    accepts_inbound=True)``), so from the slot's side the binding IS the entry's
    own room among the session's mirror rooms: the room the stamp names
    (``session_control.mirror_room``) must still be one of the rooms the probe
    names now (``now["mirror_identity"]``, read as ``mirror_audience``). Asked of
    THAT room, not of whether any mirror survives: a session can hold two rooms
    (``_probe_channel_mirror`` joins the mirror row and the Slack thread), and
    the sender's room leaving while the other stays -- a Telegram conversation
    ``/unlink``ed beside a Slack thread that remains -- keeps ``mirrored`` true
    and reads to ``newly_held_constraints`` as a narrowing, while for this entry
    it is the reply route gone and the admission revoked. A mirror moved to a
    DIFFERENT conversation drops here for the same reason (the origin room is not
    among the rooms), beside the ``mirror_retarget`` the identity arm reports.
    Only the probe-failure snapshot, which omits ``mirror_identity`` and fails
    closed on ``mirrored`` through ``newly_held_constraints``, is read as the
    boolean. Entries without the channel stamp -- composer text, peer sends,
    automation -- are never affected: for them a mirror disappearing is not a
    lost reply route.
    """
    address = channel_origin_address(entry_meta)
    if address is None:
        return False
    identity = now.get("mirror_identity")
    if not isinstance(identity, str):
        return not bool(now.get("mirrored"))
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard import session_control as _sc

    return _sc.mirror_room(address) not in _sc.mirror_audience(identity)


def channel_origin_still_bound(dashboard_state: Any, slot: Any, origin: Any) -> bool:
    """Whether the conversation a hand-off came from is still one of the session's
    mirror rooms -- asked at PUBLICATION, against the rooms held then.

    *origin* is the stamp the drain handed to the turn (``_run_chat``'s
    ``_channel_origin``). The drain re-validated the binding when the entry was
    consumed, but the channel-neutral reply leg resolves the mirror LIVE when it
    delivers, so a rebind that lands while the turn runs -- the sending
    conversation releases the session and another one resumes it -- would route
    this conversation's reply to the new audience. The turn's publications
    therefore go to the conversation that sent the message or nowhere: its room
    (``session_control.mirror_room``) must still be among the rooms the probe
    names now (``mirror_audience``). A room bound BESIDE it (a Slack thread on a
    Telegram-mirrored session) is not a change, exactly as the drain reads it.

    Fails closed: a stamp naming no conversation, no mirror at all, or a store
    that could not be read all answer False, and the transcript keeps the text.
    """
    address = channel_origin_address({CHANNEL_ORIGIN_META_KEY: origin})
    if address is None:
        return False
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard import session_control as _sc

    identity = _sc._probe_channel_mirror(dashboard_state, slot)
    if identity is None:
        return False
    return _sc.mirror_room(address) in _sc.mirror_audience(identity)


async def notify_channel_origin_dropped(
    dashboard_state: Any,
    session_key: str,
    origin: ChannelLink,
    *,
    reason: str,
    principal: str = "",
) -> bool:
    """Tell the channel conversation that queued a handed-off entry that it was dropped.

    The dashboard drain's own drop notice lands on the slot transcript, which the
    channel user is not reading, and its sender notice keys on a sender SLOT,
    which a channel has none of. The receipt they got promised nothing more than
    a wait, so without this the one outcome they most need -- the message will
    never run -- is the one they are never told.

    Addressed from the entry's own stamp rather than the session's current link,
    because the case that drops the entry is exactly the one where that link is
    gone. Delivery still walks the governed cross-surface ladder
    (``_resolve_channel_target``): the channels governance scope and the
    transport's recipient allow-list are re-checked at send time, so a
    conversation the policy refuses at that moment gets nothing. The ladder is
    asked with *principal*, the platform user the dispatcher admitted the entry
    from (:func:`channel_origin_principal`), because a ``dashboard:`` session key
    names no one and a DM roster is a roster of users: without it every DM notice
    is refused for want of a principal. Empty leaves the ladder to derive one,
    which for a dashboard key is the fail-closed refusal. A Slack thread
    (the linked-thread intercept's stamp) is the one address that ladder answers
    None for by design -- Slack's dedicated client is not a registered transport
    -- so it is posted through that client instead, after the same channels-scope
    governance decision the ladder takes (``chat_runner.channel_egress_permitted``,
    SEL-audited, fail-closed). Best-effort: the drop is
    the authorization decision and never waits on this report, so a refused or
    failed delivery sends nothing and raises nothing -- with one exception. A
    ``PlatformCompositionError`` means the governance ceiling itself is invalid;
    the gate re-raises it rather than denying, and this function lets it through
    for the same reason, so the caller's task logs it instead of a debug line
    reading it as a routine skip. Returns whether a notice was sent.
    """
    notice = CHANNEL_DROP_NOTICE.format(reason=reason)
    # Lazy: chat_runner imports this module for the drain, so a top-level import
    # here would close the cycle (the compaction notice takes the same shape).
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target, channel_egress_permitted
    from kiro_crew.platform.context import PlatformCompositionError

    if origin.channel_type == SLACK_NAMESPACE:
        slack_client = getattr(dashboard_state, "slack_client", None)
        if slack_client is None or not origin.channel_id or not origin.thread_id:
            return False
        # The one egress the ladder cannot serve still asks the ladder's governance
        # gate (SEL-audited, fail-closed): a session whose profile denies the
        # channels scope for Slack is not posted into by the dedicated client either.
        try:
            permitted = await asyncio.to_thread(
                channel_egress_permitted, session_key, origin.channel_type
            )
        except PlatformCompositionError:
            # The governance ceiling itself is invalid. The gate re-raises this one
            # deliberately instead of denying, and a best-effort notice is no place
            # to swallow it: caught here, a broken ceiling would read as an ordinary
            # skipped notice with no warning and no traceback anywhere.
            raise
        except Exception:
            logger.debug("channel drop notice: slack governance failed for %s", session_key)
            return False
        if not permitted:
            return False
        try:
            await slack_client.post_message(origin.channel_id, notice, origin.thread_id)
        except Exception:
            logger.debug(
                "channel drop notice: slack delivery failed for %s", session_key, exc_info=True
            )
            return False
        return True

    try:
        # Off-loop: the ladder's governance gate walks the profile directory,
        # which is unbounded on slow storage; a notice is never worth stalling
        # the loop for.
        target = await asyncio.to_thread(
            _resolve_channel_target,
            dashboard_state,
            session_key,
            origin,
            principal=principal or None,
        )
    except PlatformCompositionError:
        # Same rule as the Slack leg above: the ladder re-raises an invalid
        # governance ceiling on purpose, and only this arm may not eat it.
        raise
    except Exception:
        logger.debug("channel drop notice: resolve failed for %s", session_key, exc_info=True)
        return False
    if target is None:
        return False
    resolved, transport = target
    try:
        await transport.send_message(
            resolved.channel_id,
            notice,
            thread_id=resolved.thread_id,
        )
    except Exception:
        logger.debug(
            "channel drop notice: %s delivery failed for %s",
            resolved.channel_type,
            session_key,
            exc_info=True,
        )
        return False
    return True
