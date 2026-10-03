"""Mid-turn hand-off of a CHANNEL's message into a RESUMED dashboard session's slot.

A channel conversation bound to a dashboard session (``!sessions`` on Discord, the
dashboard's mirror menu) sends its messages into that session. While the session
is mid-turn the channel's OWN mid-turn machinery cannot take them: the channel
queue is drained only at the tail of a turn that channel drove, and its replay
skips resume routing, so a message enqueued there would sit until some later
channel turn and then run in the channel's NATIVE session. The dashboard slot has
its own steer path (the injection the composer uses) and its own queue (drained
by the dashboard turn loop, so ordering is the dashboard's). This module hands the
message to those, and says when it cannot.

It lives in the dashboard package because it drives the dashboard's delivery
seams; ``messaging`` may not import the dashboard, and a channel dispatcher
reaches this module through a deferred import, the way it reaches the live-slot
projection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from kiro_crew.dashboard.chat_delivery import (
    MAX_PENDING_STEERS,
    STEER_REQUEUED,
    STEER_STEERED,
    _queued_entry_id,
    _row_has_delivery_id,
    queue_for_next_turn,
    steer_into_running_turn,
)
from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES
from kiro_crew.messaging.upload_gate import live_dashboard_slot

logger = logging.getLogger(__name__)


#: The message cut into the slot's running turn.
HANDOFF_STEERED = "steered"
#: The message waits in the slot's queue for the dashboard turn loop to drain.
HANDOFF_QUEUED = "queued"
#: The slot could not take the message; ``reason`` says why.
HANDOFF_REFUSED = "refused"

# Why a hand-off was refused. The channel words each for its own surface.
#: The key resolves to no open dashboard slot (no tab, or not a ``dashboard:`` key).
REFUSED_NO_SLOT = "no_slot"
REFUSED_CLOSING = "closing"
#: The slot runs its turns on a remote crew, which has no local drain.
REFUSED_REMOTE = "remote"
#: The lease is held, but not by the dashboard turn loop: the slot itself is idle.
REFUSED_IDLE = "idle"
#: The message carries attachments, which neither arm can carry.
REFUSED_ATTACHMENTS = "attachments"
#: The key stopped resolving to the slot the steer was handed to while the RPC
#: was suspended: the slot was closed, or closed and recreated under the same key.
REFUSED_MOVED = "moved"
#: The slot's live queue holds ``MAX_LIVE_QUEUE_ENTRIES`` entries. Refused rather
#: than appended past the bound or evicting a waiting entry: every producer that
#: guards the live queue refuses or evicts at this one size, and a channel human's
#: text is a one-shot the author still holds and can resend.
REFUSED_QUEUE_FULL = "queue_full"

# Where the text stands once the key stopped resolving to the slot it was handed
# to (:func:`standing_after_move`). Carried as a QUEUED outcome's ``reason`` so the
# channel can word each one; the stranded case is ``REFUSED_MOVED``.
#: The successor slot under the same key holds the text in its queue.
QUEUED_ON_SUCCESSOR = "successor_queue"
#: The successor slot under the same key ran (or is running) the text as its own turn.
RAN_ON_SUCCESSOR = "successor_turn"
#: The slot is closing and the queue the close ARCHIVED carries the text, so it
#: runs when the session is next resumed.
QUEUED_BY_CLOSE = "closing_queue"
#: The slot is closing and the text is held only in memory -- registered, or
#: requeued after the close's save ran -- so the archive may not carry it. Refused
#: with wording that says so, rather than reported queued on a record nothing will
#: persist.
REFUSED_UNSAVED_CLOSE = "unsaved_close"


@dataclass(frozen=True)
class ResumedBusyOutcome:
    """What became of one mid-turn message handed to a resumed session's slot."""

    kind: str
    reason: str = ""

    @property
    def refused(self) -> bool:
        return self.kind == HANDOFF_REFUSED


def _refused(reason: str) -> ResumedBusyOutcome:
    return ResumedBusyOutcome(HANDOFF_REFUSED, reason)


#: Hand-offs in flight, held STRONGLY. A hand-off runs as its own task, awaited
#: through ``asyncio.shield`` so the channel handler's cancellation cannot cut it
#: off between the steer RPC and its reconciliation (see
#: :func:`hand_to_resumed_slot`). The loop keeps only weak references to its
#: tasks, and once the awaiter is cancelled and unwinds nothing else names this
#: one, so this set is what keeps it alive to finish; each task discards itself
#: on completion.
_HANDOFFS_IN_FLIGHT: set[asyncio.Task[ResumedBusyOutcome]] = set()


def audience_fence_key(admission: dict[str, Any]) -> str:
    """The fence key for *admission*: the AUDIENCE it names, not the message.

    ``slot._steer_audience_fences`` is retained for the whole turn (its teardown
    clears it), while every other per-steer map empties as the turn consumes the
    steer -- so a record per message would grow for as long as the turn runs.
    Keyed by the containment snapshot instead, one turn holds one record per
    distinct audience however many messages a channel sends into it, and
    re-recording the same audience is a no-op. A stable digest of the snapshot,
    since the publisher compares the snapshot's content and two admissions that
    agree on it are one fence. Deterministic on purpose: the peer path's keys are
    random tokens (one per delivery, popped by the sender), and a channel's
    audience keys never collide with them.
    """
    from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

    snapshot = admission.get(QUEUED_CONTAINMENT_META_KEY)
    encoded = json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    return "audience:" + hashlib.sha256(encoded).hexdigest()[:24]


def _hold_fence(slot: Any, fence: str, admission: dict[str, Any]) -> None:
    """Record *admission* under *fence* (a no-op for a recorded audience) and count a holder."""
    slot._steer_audience_fences.setdefault(fence, admission)
    holders = slot._steer_audience_fence_holders
    holders[fence] = holders.get(fence, 0) + 1


def _release_fence(slot: Any, fence: str) -> None:
    """Drop this steer's hold on *fence*; pop the record once no holder remains.

    Called on every outcome where the text does not enter the running turn --
    declined or unavailable before or after the RPC, or requeued by the turn's
    teardown to run as its own turn. A record another steer under the same
    audience landed with keeps its holder and stays, so a sibling's decline never
    publishes a leg the landed steer's fence withholds; a record nothing holds is
    removed, so a turn that never received the channel text is not withheld
    against an audience change on its behalf. Tolerant of a record the turn's
    teardown already cleared.
    """
    holders = getattr(slot, "_steer_audience_fence_holders", None)
    if not isinstance(holders, dict):
        return
    remaining = holders.get(fence, 0) - 1
    if remaining > 0:
        holders[fence] = remaining
        return
    holders.pop(fence, None)
    fences = getattr(slot, "_steer_audience_fences", None)
    if isinstance(fences, dict):
        fences.pop(fence, None)


def slot_unable_to_take(slot: Any) -> str:
    """The ``REFUSED_*`` reason *slot* cannot take a mid-turn message, or ``""``.

    Every read fails CLOSED on an attribute the slot does not carry: a slot that
    cannot say it is running is refused rather than written to. An incognito or
    temporary slot (``is_restricted``) is NOT refused: those modes keep their
    transcript and queue in History and withhold only what is DERIVED from the
    chat (consolidation, lessons, memory injection), which neither arm here
    produces -- the steer row and the queue entry are the same records the slot's
    own composer writes in that mode.

    ``running`` is the predicate every producer that must not start a concurrent
    turn reads. The lease the channel observed as busy can
    be held by something other than the dashboard turn loop -- the channel's own
    turn on the resumed key is the live case -- and then the slot has no published
    client to steer into and no drain coming: a queue entry would strand until an
    unrelated later dashboard turn, and the queue-or-run admission would START a
    turn against a lease another driver holds.
    """
    if slot is None:
        return REFUSED_NO_SLOT
    if getattr(slot, "is_closing", False):
        return REFUSED_CLOSING
    if getattr(slot, "is_remote", False) or getattr(slot, "executor", "") == "remote":
        return REFUSED_REMOTE
    if not getattr(slot, "running", False):
        return REFUSED_IDLE
    return ""


def standing_after_move(slot: Any, successor: Any, text: str, delivery_id: str) -> str:
    """Where the steer *delivery_id* stands once its key stopped resolving to *slot*.

    Read from the queue and turn RECORDS by IDENTITY -- never by content, never by
    timing. The id is minted by the hand-off and handed to
    ``steer_into_running_turn``, which writes it into every record the steer
    leaves: the pending registration (``_steer_delivery_ids``), the queue entry a
    teardown requeue writes (``meta.steer_delivery_id``, durable with the queue,
    restored with it), the row a drain writes for that entry, and the row the
    steer persists itself. Content cannot do this job: a slot recreated under the
    same key restores its whole prior transcript, and a short message that recurs
    in it ("continue", "status?") would prove a delivery that never happened.
    *text* is only the key the pending map is indexed by; the comparison is on the
    id it holds.

    * The slot the text was handed to is judged FIRST, successor or not: a steer
      the closing turn accepted left its row there, a teardown requeue left its
      entry, a still-suspended registration its id, and a successor knows none of
      that. Only when that slot holds nothing does a live successor answer: the
      text is queued when the successor's queue carries the id (the one queue a
      drain still reaches) and delivered when its transcript does. Nothing on
      either object is stranded.
    * No successor means the slot is closing. The close archives the popped slot
      WITH its queue (``queued_prompts``) exactly once, and nothing revisits a
      popped object afterwards -- the periodic flush walks only the live registry
      -- so only the ARCHIVED queue can promise the text runs on the next resume.
      "Archived" is read off the persistence layer's own witness,
      ``queue_persist_pending`` (the durable form of the live queue compared with
      the signature the last save wrote): False with the entry present means the
      copy on disk carries this id. An id held only in memory -- the pending
      registration, a queue entry the teardown wrote after the close's save ran,
      or a row appended after it -- is NOT reported queued: the close's teardown
      wait is bounded, so a cleanup that outlasts it lands on an object the
      archive already left behind, and "queued" would name a message the archive
      lacks. That case is refused with wording that says the message was not yet
      saved (``REFUSED_UNSAVED_CLOSE``). A text no record holds at all (a
      declined steer's unwound registration, a hard stop's cleared pending list)
      is stranded (``REFUSED_MOVED``).
    """
    # The slot the text was HANDED TO is judged first, whether or not a successor
    # holds the key: an accepted steer leaves its row there, a teardown requeue
    # its entry, a still-suspended registration its id -- and a successor knows
    # none of that. Refusing on the successor's silence alone would tell the human
    # a delivered or archived message was NOT delivered, and the resend runs it
    # twice.
    if _queued_entry_id(slot, delivery_id):
        # Fail closed on a slot that cannot say whether its queue is on disk.
        if getattr(slot, "queue_persist_pending", True) is False:
            return QUEUED_BY_CLOSE
        return REFUSED_UNSAVED_CLOSE
    registered = getattr(slot, "_steer_delivery_ids", None) or {}
    if registered.get(text) == delivery_id or _row_has_delivery_id(slot, delivery_id):
        return REFUSED_UNSAVED_CLOSE
    if successor is not None:
        if _queued_entry_id(successor, delivery_id):
            return QUEUED_ON_SUCCESSOR
        if _row_has_delivery_id(successor, delivery_id):
            return RAN_ON_SUCCESSOR
    return REFUSED_MOVED


async def hand_to_resumed_slot(
    state: Any,
    session_key: str,
    text: str,
    *,
    mode: str,
    has_attachments: bool,
    channel_type: str = "",
    conversation_id: str = "",
    principal: str = "",
) -> ResumedBusyOutcome:
    """Route *text*, sent mid-turn into resumed *session_key*, to its slot.

    *state* is the channel dispatcher's ``dashboard_state`` handle: the gateway's
    ``DashboardState`` when a dashboard is attached, ``None`` when none is, which
    resolves no slot and is refused like any other missing slot. Incognito and
    temporary slots are taken like any other: those modes keep their transcript and
    queue and withhold only what is derived from the chat.

    *channel_type*, *conversation_id* and *principal* name the conversation the
    text came from and the platform user the channel authorized on inbound. They
    are stamped on whatever the text becomes -- the queue entry directly, the steer
    through its admission dict, which the requeue copies onto the entry
    (``session_control.channel_recipient_meta``) -- so a drain-time drop of the
    queued text is reported back into that conversation
    (``session_control.notify_channel_recipient_dropped``): the conversation was
    told "queued" and reads neither the target's transcript nor the SEL. Left
    empty, nothing is stamped and a drop is reported nowhere, as for a
    dashboard-typed entry.

    *mode* is the channel's resolved mid-turn mode (``"steer"`` or ``"queue"``,
    per-message override already applied). Steer is attempted only when asked for
    AND the slot's published client can take a steer for THIS author; every other
    case, including a steer the client declines, takes the slot's queue so the text
    is never dropped.

    The text is handed over as the human's own words -- no provenance envelope,
    because the author IS the session's own human: a channel may resume a
    dashboard session only under its owner gate. What the two arms record differs
    by where the text runs:

    * A steer runs INSIDE the dashboard's turn, under that turn's provenance, as
      every steer does (the composer's and a peer's alike). What it records is the
      same audience fence the peer path (``session_control.send_to_target``)
      records: the containment holding at admission, kept on the slot for the
      whole turn, so the publisher's ``cross_surface_withheld`` withholds the
      reply's cross-surface leg when a constraint newly holds. The record lands
      BEFORE the RPC, because ``steer()`` suspends and a fast turn can publish
      before it returns. It is keyed by the AUDIENCE (:func:`audience_fence_key`)
      and bounded: one record per distinct containment snapshot per turn however
      many messages the channel sends, re-recording the same audience is a
      no-op, the set shares ``MAX_PENDING_STEERS`` with the other per-steer stores,
      and the turn's teardown clears it. A new audience arriving at the cap is
      never recorded over an existing fence -- a fence dropped is a cross-surface
      leg published that should have been withheld -- and the message is not
      refused for it either: it takes the queue arm, which records no fence and
      is already where a declined steer goes. The fence follows the message INTO
      the turn: every admission under an audience counts a holder
      (``_steer_audience_fence_holders``), a steer whose text does not enter the
      running turn -- unavailable or declined before or after the RPC, requeued
      by the teardown -- releases its hold, and the record is popped once no
      holder remains. So a fence a landed sibling relies on survives another
      steer's decline, while a fence for text that never reached the turn does
      not withhold that turn's cross-surface leg against an audience change --
      the same release the peer path performs on its per-token record when it
      is not steered. The release is bound to the turn the steer was admitted
      to (``slot._turn_generation``, captured before the RPC, as the peer path
      binds its stop): a steer that wakes after that turn's teardown finds its
      hold already cleared with the maps, and a record under the same audience
      key belongs to the next turn's steers, so it releases nothing. The peer path's containment STOP is not
      repeated here: that stop narrows a delivery a gate authorized against
      containment, and there is no such gate on a human's own message -- the fence
      alone is what protects publication.
    * A queued message runs as its OWN turn, so it carries the provenance a
      channel human's text carries on the Slack linked-thread path: user origin
      (the session's own human typed it, which is what earns the LINKED exemption
      at the drain) and channel origin (channel authority is the narrower
      credential boundary, so a directive that turn issues is filed as
      channel-created). A steer that ends up requeued carries the same two marks
      through the slot's per-steer maps. Both marks together also keep the row and
      the card display-redacted: a channel author is not the dashboard's reader.

    The steer RPC suspends, and the slot can move under it. After the RPC the key
    is resolved AGAIN and compared by object identity, the way
    ``session_control.send_to_target`` re-gates its fallback: a slot closed and
    recreated under the same key compares equal by key while the queue the text
    would land on belongs to a detached object no drain will ever reach. A moved
    slot is not refused outright: the text may already be somewhere that runs it
    -- a close during the RPC cancels the turn, whose teardown requeues the
    pending steer onto the queue the close archives, so the text runs when the
    session is next resumed -- and a refusal there reads as NOT delivered to a
    human who then resends and runs it twice. :func:`standing_after_move` reads
    the queue and turn records of both objects (the one the text was handed to,
    the one the key resolves to now) by the DELIVERY ID this hand-off minted and
    handed to the steer, and the outcome follows them: queued on the successor,
    ran on the successor, queued by the close (the ARCHIVED queue carries the id),
    not yet saved by the close (the id is held only in memory,
    ``REFUSED_UNSAVED_CLOSE``), or -- when no record holds the id -- refused
    (``REFUSED_MOVED``). The fallback never appends
    to the detached object, and on an unmoved slot it re-runs the admission gate,
    because a slot that went idle or started closing during the RPC cannot take
    the text any more.

    The slot's live queue is bounded (``MAX_LIVE_QUEUE_ENTRIES``), and every
    producer that appends to it guards that bound itself. This one REFUSES at the
    cap (``REFUSED_QUEUE_FULL``) rather than appending past it or evicting a
    waiting entry: the author still holds the text and can resend, and the
    channel is told why. It is the one refusal left once the slot has taken the
    message past the admission gate.

    Attachments are refused rather than queued without: ``_session/steer`` carries
    text only, and the slot's queue cannot carry channel attachment material -- it
    is downloaded into temp files owned by the consuming turn, and the dashboard
    drain has no hook to own them. Refusing keeps the files with the user.

    The whole hand-off -- gate, steer RPC, reconciliation, queue fallback -- runs
    as ONE task, held by a strong reference (``_HANDOFFS_IN_FLIGHT``) and awaited
    through ``asyncio.shield``. The caller is a channel's message handler, and a
    transport close cancels those handlers as an ordinary path (Discord gathers
    its handler tasks on close). Awaited inline, that cancellation lands inside
    ``steer_into_running_turn``'s RPC: the pending registration is made, the
    client may already have accepted the text, and everything behind the RPC --
    the transcript row for an accepted steer, the unwind of a declined one, the
    fallback to the queue -- is skipped, so accepted text runs with no row while
    the per-steer maps keep its entry for the slot's lifetime (the
    ``steering_consumed`` settle removes the pending entry and patches an existing
    row; the turn's teardown requeues only UNconsumed steers). Shielded, the
    caller's cancellation cancels the shield's outer future alone: the hand-off
    task completes the RPC, the reconciliation and the fallback, and the caller
    unwinds without the outcome (its confirmation has no live transport to go to).
    The strong reference is released by the task's done callback on every
    completion -- return, error, or cancellation of the task itself, which only
    the loop's own shutdown performs, when the turn the text was written into is
    going down with it. The audience fence recorded before the RPC is not this
    task's to release: it is the audience's record for the turn (above), and the
    turn's teardown clears it.
    """
    loop = asyncio.get_running_loop()
    task = loop.create_task(
        _run_handoff(
            state,
            session_key,
            text,
            mode=mode,
            has_attachments=has_attachments,
            channel_type=channel_type,
            conversation_id=conversation_id,
            principal=principal,
        ),
        name=f"channel-handoff:{session_key}",
    )
    _HANDOFFS_IN_FLIGHT.add(task)
    task.add_done_callback(_HANDOFFS_IN_FLIGHT.discard)
    return await asyncio.shield(task)


async def _run_handoff(
    state: Any,
    session_key: str,
    text: str,
    *,
    mode: str,
    has_attachments: bool,
    channel_type: str,
    conversation_id: str,
    principal: str,
) -> ResumedBusyOutcome:
    """The hand-off proper; :func:`hand_to_resumed_slot` runs it as a shielded task."""
    slot = live_dashboard_slot(state, session_key)
    blocked = slot_unable_to_take(slot)
    if blocked or slot is None:
        # ``slot is None`` is already ``REFUSED_NO_SLOT`` above; restated so the
        # slot reads as present from here on.
        return _refused(blocked or REFUSED_NO_SLOT)
    if has_attachments:
        return _refused(REFUSED_ATTACHMENTS)

    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import channel_recipient_meta, containment_meta

    recipient = channel_recipient_meta(channel_type, conversation_id, principal)
    # The identity this hand-off's text carries through every record the steer
    # leaves (see ``standing_after_move``). Minted here, not inside the steer, so
    # it is known on this side of the RPC.
    delivery_id = uuid.uuid4().hex

    if mode != "queue":
        client = getattr(slot, "_acp_client", None)
        # codex can drop a steer it already took when a later approval in the
        # turn is denied. The composer accepts that because its human watches the
        # turn and can resend; a channel human cannot see the dashboard's turn,
        # so the text takes the queue instead of an injection that may vanish.
        if getattr(client, "steer_needs_loss_recovery", False) is not True:
            # The recipient rides the admission dict, as the peer path's sender
            # stamp does: the requeue copies that dict onto the entry verbatim,
            # so a steer that ends up queued keeps its drop-notice address. Inert
            # for the fence and the drain, which read only the containment key.
            admission = {**containment_meta(state, slot), **recipient}
            # The audience fence, recorded BEFORE the RPC (see the docstring):
            # one record per audience per turn, and the set shares the cap every
            # other per-steer store has. At the cap -- an audience that changed
            # MAX_PENDING_STEERS times inside one turn -- a NEW audience is not
            # recorded over an existing fence (a fence dropped is a cross-surface
            # leg published that should have been withheld), and the text is not
            # steered without its fence either: it takes the queue arm below, which
            # records no fence and is where a declined steer already goes.
            fence = audience_fence_key(admission)
            fences = slot._steer_audience_fences
            if fence not in fences and len(fences) >= MAX_PENDING_STEERS:
                logger.info(
                    "channel hand-off: audience fence cap reached for slot %s (%d); "
                    "the message takes the queue instead of a steer",
                    getattr(slot, "key", "?"),
                    MAX_PENDING_STEERS,
                )
                return _queue_arm(state, slot, text, recipient)
            _hold_fence(slot, fence, admission)
            # Which turn this steer is going into. ``_turn_generation`` increments
            # on every task assignment, so it names a turn even when a later task
            # object reuses an address. The release below is bound to it: a steer
            # suspended across this turn's teardown (which clears both fence maps)
            # wakes in the NEXT turn, whose steers may hold the same deterministic
            # audience key, and an unbound release would take a hold that is theirs.
            steered_turn_generation = slot._turn_generation
            outcome = await steer_into_running_turn(
                state,
                slot,
                text,
                user_origin=True,
                channel_origin=True,
                admission=admission,
                delivery_id=delivery_id,
            )
            if outcome != STEER_STEERED:
                # The text did not enter the running turn -- unavailable or
                # declined (before or after the RPC), or requeued by the
                # teardown to run as its own turn -- so this steer's hold on the
                # audience record is released; the record itself stays while a
                # landed sibling under the same audience still holds it. ONLY
                # while the turn it was admitted to is still the running one: if
                # that turn ended during the RPC, its teardown already cleared the
                # hold with the maps, and a record under the same key now belongs
                # to the next turn's steers -- releasing it would pop THEIR fence
                # and publish a leg their admission withholds.
                if slot._turn_generation == steered_turn_generation:
                    _release_fence(slot, fence)
                else:
                    logger.info(
                        "channel hand-off: steer into slot %s returned %s after its turn "
                        "ended; its fence hold went with that turn's teardown and the "
                        "next turn's record is left alone",
                        getattr(slot, "key", "?"),
                        outcome,
                    )
            successor = live_dashboard_slot(state, session_key)
            if successor is not slot:
                # Object identity, not key equality: the key resolves to a fresh
                # object, or to nothing. Checked before reading ``outcome``, for
                # every outcome: an accepted steer into a turn the close has
                # cancelled is not "steering", and the teardown that requeues it
                # runs in another coroutine, so ``outcome`` alone cannot say where
                # the text stands. The records can (:func:`standing_after_move`),
                # read by the delivery id this hand-off minted.
                standing = standing_after_move(slot, successor, text, delivery_id)
                if standing in (REFUSED_MOVED, REFUSED_UNSAVED_CLOSE):
                    logger.warning(
                        "channel hand-off: %s stopped resolving to the slot the steer was "
                        "handed to; refused (%s) rather than queued onto the detached "
                        "object",
                        session_key,
                        standing,
                    )
                    return _refused(standing)
                logger.info(
                    "channel hand-off: %s moved while the steer was in flight; the text "
                    "stands as %s",
                    session_key,
                    standing,
                )
                return ResumedBusyOutcome(HANDOFF_QUEUED, standing)
            if outcome == STEER_STEERED:
                return ResumedBusyOutcome(HANDOFF_STEERED)
            if outcome == STEER_REQUEUED:
                # The turn ended while the RPC was suspended and its teardown moved
                # the text onto the queue: it WILL run, and queueing it again here
                # would run it twice.
                return ResumedBusyOutcome(HANDOFF_QUEUED)
            # STEER_UNAVAILABLE: no steer-capable client, an RPC that lost the
            # text, or an identical steer already in flight. Nothing holds the
            # text, so the queue arm below takes it -- against the admission gate
            # re-run on the far side of the suspension.
            blocked = slot_unable_to_take(slot)
            if blocked:
                return _refused(blocked)
    return _queue_arm(state, slot, text, recipient)


def _queue_arm(state: Any, slot: Any, text: str, recipient: dict[str, Any]) -> ResumedBusyOutcome:
    """Append *text* to the slot's queue, refusing at the live queue's bound.

    The bound is read on the object the entry would land on, immediately before
    the append, with nothing suspending between the two. Refused, never evicted:
    a waiting entry is someone's text with no other copy.
    """
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import CHANNEL_RECIPIENT_META_KEY

    if len(getattr(slot, "_queue", None) or []) >= MAX_LIVE_QUEUE_ENTRIES:
        logger.warning(
            "channel hand-off: live queue for slot %s is at its bound (%d); refusing the "
            "message rather than appending past it",
            getattr(slot, "key", "?"),
            MAX_LIVE_QUEUE_ENTRIES,
        )
        return _refused(REFUSED_QUEUE_FULL)
    queue_for_next_turn(
        state,
        slot,
        text,
        directive_user_origin=True,
        directive_channel_origin=True,
        channel_recipient=recipient.get(CHANNEL_RECIPIENT_META_KEY),
    )
    return ResumedBusyOutcome(HANDOFF_QUEUED)
