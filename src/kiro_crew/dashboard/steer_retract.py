"""Taking back a mid-turn steer the running turn has not read yet.

A steer is handed to kiro-cli the moment it is sent, and kiro-cli holds it until
the turn reaches a point where it can read it. Until then it can be taken back:
``_session/steer/clear`` drops every steer kiro-cli is holding, and kiro-cli reports
that it did with ``AgentExecutionUserMessageCleared``, which reaches the turn as
``EVENT_STEER_DROPPED`` (``ACP_BACKENDS_STEER_RETRACT``).

The person picks what happens to the text: ``unsend`` discards it, ``edit`` hands
it back to their composer (the dashboard does that, from the row it already
draws), and ``queue`` puts it in the slot's queue to run after the turn.

The outcome is decided by what the turn reports after the clear is written, never
by the clear's own answer, which is ``{cleared: true}`` whether anything was held
or not:

* ``EVENT_STEER_DROPPED`` while the steer is still pending: kiro-cli dropped it
  unread, so it is taken back (:func:`on_dropped`).
* the steer settled by ``EVENT_STEER_CONSUMED`` first: the turn read it, and there
  is nothing to take back (:func:`on_settled`).
* the turn ending with the steer still pending: it never reached the turn, so it
  is taken back too (:func:`on_turn_ending`).
* a hard Stop ending the turn outside the runner: kiro-cli's report that the turn
  read or dropped the steer may still be queued, so the take-back is released and
  its row left unchanged (:func:`on_hard_stop`).

The person sees which one happened on the steer's own row: ``retracted`` (with
the action), ``consumed``, or ``requeued`` for a ``queue`` take-back the turn's end
overtook, since that requeue is exactly what was asked. The route only arms the
take-back and writes the clear, and answers before any of that is known.

kiro-cli drops EVERY steer it holds, not only the one asked for. The steers
pending when the clear was written are its batch, and the ones the drop took
besides the taken-back steer are sent again, in their order. A steer sent while a
clear is in flight waits for the clear's answer first (:func:`wait_for_clear`), so
the clear cannot drop it. A deny notice the turn steered in and kiro-cli still held
is dropped too; it is not in the batch, and the turn's refusal recovery delivers it
when the turn ends, the fallback for any notice the turn never confirmed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard import responsive_chat
from kiro_crew.dashboard.chat_delivery import (
    STEER_RETRACTABLE_META,
    STEER_STATE_RETRACTED,
    STEER_STATE_WRITTEN,
    attachment_meta,
    find_written_steer_row,
    queue_for_next_turn,
    quote_meta,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

RETRACT_UNSEND = "unsend"
RETRACT_EDIT = "edit"
RETRACT_QUEUE = "queue"
RETRACT_ACTIONS = frozenset({RETRACT_UNSEND, RETRACT_EDIT, RETRACT_QUEUE})

#: The action a taken-back steer's row records, so the row says what became of
#: the text.
RETRACT_ACTION_META = "steerRetractAction"

#: Set beside ``steerRetract: false`` when :func:`withdraw_stale_offers` withdrew
#: the offer because the steer's turn ended without settling it. A page holding a
#: take-back of that row reads the turn as over, not as a refusal.
RETRACT_EXPIRED_META = "steerRetractExpired"

# What :func:`begin` answers.
RETRACT_ACCEPTED = "accepted"
RETRACT_NOT_PENDING = "not_pending"
RETRACT_BUSY = "busy"
RETRACT_UNSUPPORTED = "unsupported"

#: What the route answers a refused take-back, by :func:`begin`'s outcome:
#: ``(error, code)``.
REFUSALS: dict[str, tuple[str, str]] = {
    RETRACT_NOT_PENDING: ("this steer is no longer waiting for the agent", "steer_not_pending"),
    RETRACT_BUSY: ("another message is being sent or taken back; try again", "steer_retract_busy"),
    RETRACT_UNSUPPORTED: ("this agent cannot take back a steer", "steer_retract_unsupported"),
}

#: How long a steer sent during a take-back waits for the clear's answer. The
#: clear's own wait is the bound that matters; this one only stops a steer
#: waiting on a clear whose writer died without saying so.
CLEAR_GATE_WAIT_SECS = 6.0


@dataclass
class SteerRetract:
    """A steer being taken back, until the turn says what became of it."""

    #: The steer's text: its key in ``_pending_steers`` and its lockstep maps.
    message: str
    #: Its transcript row.
    mid: str
    action: str
    #: The containment in effect when the person asked to queue this steer.
    admission: dict[str, Any] | None = None
    #: Whether the session's own human asked for this from the composer.
    directive_user_origin: bool = True
    #: The turn it was asked during (``slot.task``). A record of a turn that is
    #: over is stale, whatever path ended that turn.
    turn: object = None


@dataclass
class SteerClear:
    """A ``_session/steer/clear`` whose drop has not been reported yet."""

    #: Every steer pending when the clear was written, in send order: what the
    #: clear can have dropped.
    batch: tuple[str, ...]
    #: The turn it was written during (``slot.task``).
    turn: object = None
    #: Set once the clear's answer is in (or never will be), so a steer written
    #: after it cannot be dropped by it.
    answered: asyncio.Event = field(default_factory=asyncio.Event)


def _row(slot: "_ChatSlot", mid: str) -> dict[str, Any] | None:
    """The transcript row whose ``mid`` is *mid*, searched newest first."""
    for row in reversed(slot.messages):
        meta = row.get("meta")
        if isinstance(meta, dict) and meta.get("mid") == mid:
            return row
    return None


def _drop_stale(slot: "_ChatSlot") -> None:
    """Forget a take-back whose turn is over.

    The turn's own teardown settles a take-back (:func:`on_turn_ending`). This is
    the backstop for an exit that skipped it: a record left behind would make
    every later take-back on the slot answer busy, and a clear left unanswered
    would hold every later steer at :func:`wait_for_clear`.
    """
    record, clear = slot._steer_retract, slot._steer_clear
    if record is not None and record.turn is not slot.task:
        slot._steer_retract = None
    if clear is not None and clear.turn is not slot.task:
        clear.answered.set()
        slot._steer_clear = None


def pending_steer_row(slot: "_ChatSlot", mid: str) -> dict[str, Any] | None:
    """The row of a steer that can still be taken back, by its ``mid``."""
    for row in reversed(slot.messages):
        meta = row.get("meta")
        if not isinstance(meta, dict) or meta.get("mid") != mid:
            continue
        if (
            row.get("role") == "user"
            and meta.get("steer") is True
            and meta.get("steerState") == STEER_STATE_WRITTEN
            and meta.get(STEER_RETRACTABLE_META) is True
        ):
            return row
        return None
    return None


async def begin(state: "DashboardState", slot: "_ChatSlot", mid: str, action: str) -> str:
    """Start taking back the pending steer whose row is *mid*; return a ``RETRACT_*``.

    ``accepted`` means the take-back is armed and the clear was written, or its
    answer is still owed; the outcome reaches the row later. Anything else
    changes nothing. Only one take-back runs at a time on a slot, and none while
    a steer is still being written, because a clear written beside a steer could
    land on either side of it. None starts while a hard Stop is discarding the
    turn and its pending steers.
    """
    if slot._stop_state == "killing":
        return RETRACT_NOT_PENDING
    row = pending_steer_row(slot, mid)
    message = row.get("content") if row is not None else None
    if (
        not isinstance(message, str)
        or message not in slot._pending_steers
        or find_written_steer_row(slot, message) is not row
        or message in slot._steer_possibly_delivered
    ):
        return RETRACT_NOT_PENDING
    client = getattr(slot, "_acp_client", None)
    if client is None or getattr(client, "supports_steer_retract", False) is not True:
        return RETRACT_UNSUPPORTED
    _drop_stale(slot)
    if slot._steer_retract is not None or slot._steer_clear is not None:
        return RETRACT_BUSY
    if slot._steer_rpc_in_flight:
        return RETRACT_BUSY
    from kiro_crew.dashboard import session_control

    record = SteerRetract(
        message=message,
        mid=mid,
        action=action,
        admission=(
            session_control.containment_meta(state, slot) if action == RETRACT_QUEUE else None
        ),
        directive_user_origin=True,
        turn=slot.task,
    )
    clear = SteerClear(batch=tuple(slot._pending_steers), turn=slot.task)
    slot._steer_retract = record
    slot._steer_clear = clear
    try:
        answered = await client.clear_pending_steers()
    except Exception as exc:
        # Whether the frame reached kiro-cli is unknown, so the take-back stays
        # armed: a drop it did cause is still reported, and the turn's end
        # settles it otherwise.
        logger.warning("steer clear for slot %s failed: %s", slot.key, type(exc).__name__)
        answered = None
    finally:
        clear.answered.set()
    if answered is False:
        if slot._steer_clear is clear:
            slot._steer_clear = None
        if slot._steer_retract is not record:
            # The row already carries the outcome. Acceptance keeps browser recovery
            # until the browser reads that settled row.
            return RETRACT_ACCEPTED
        # Refused while still armed: nothing was dropped, so the row stops
        # offering the take-back.
        slot._steer_retract = None
        _withdraw_offer(state, slot, record)
        return RETRACT_UNSUPPORTED
    return RETRACT_ACCEPTED


async def wait_for_clear(slot: "_ChatSlot") -> None:
    """Hold a steer about to be written until an in-flight clear is answered."""
    _drop_stale(slot)
    clear = slot._steer_clear
    if clear is None or clear.answered.is_set():
        return
    try:
        await asyncio.wait_for(clear.answered.wait(), CLEAR_GATE_WAIT_SECS)
    except asyncio.TimeoutError:
        logger.warning("steer for slot %s sent without the clear's answer", slot.key)


async def on_dropped(state: "DashboardState", slot: "_ChatSlot") -> None:
    """kiro-cli dropped the steers it held: settle the take-back, resend the rest."""
    clear = slot._steer_clear
    slot._steer_clear = None
    if clear is None:
        # No clear of ours is waiting on this, so nothing can say which steers it
        # names. Those still pending are requeued by the turn's teardown.
        return
    possibly_delivered = slot._steer_possibly_delivered
    dropped = [
        message
        for message in clear.batch
        if message in slot._pending_steers and message not in possibly_delivered
    ]
    record = slot._steer_retract
    if record is not None and record.message in possibly_delivered:
        slot._steer_retract = None
    if record is not None and record.message in dropped:
        slot._steer_retract = None
        dropped.remove(record.message)
        take_back(state, slot, record)
    await _send_again(slot, dropped)


def on_settled(slot: "_ChatSlot") -> None:
    """The turn read some steers: a take-back of one of them is over."""
    record = slot._steer_retract
    if record is not None and (
        record.message not in slot._pending_steers
        or record.message in slot._steer_possibly_delivered
    ):
        # The turn read it first, or its delivery is unknown (a recovery, an
        # ambiguous write or a partly echoed collision marked it possibly delivered).
        slot._steer_retract = None
        logger.info("steer take-back for slot %s overtaken: the turn may have read it", slot.key)
    clear = slot._steer_clear
    if clear is not None and not any(m in slot._pending_steers for m in clear.batch):
        # Everything the clear could have dropped was read, so it drops nothing
        # and kiro-cli sends no report for it.
        slot._steer_clear = None


def on_turn_ending(state: "DashboardState", slot: "_ChatSlot") -> None:
    """The turn is over: a steer still being taken back never reached it.

    Runs before the teardown requeues the pending steers. ``unsend`` and ``edit``
    are taken back here, so the requeue does not run the text. ``queue`` is left
    pending: the requeue puts it at the head of the queue, which is what was
    asked.
    """
    record = slot._steer_retract
    clear = slot._steer_clear
    slot._steer_retract = None
    slot._steer_clear = None
    if clear is not None:
        clear.answered.set()
    if (
        record is None
        or record.message not in slot._pending_steers
        or record.message in slot._steer_possibly_delivered
    ):
        return
    if record.action == RETRACT_QUEUE:
        if record.admission is not None:
            slot._steer_admissions[record.message] = record.admission
        slot._steer_user_origin[record.message] = record.directive_user_origin
        return
    take_back(state, slot, record)


def on_hard_stop(slot: "_ChatSlot") -> None:
    """Release a take-back when a hard Stop ends the turn outside the runner.

    The runner may still hold kiro-cli's report that the turn read or dropped the
    steer, so nothing confirms the take-back. The steer is discarded with the
    other pending steers and its row is left as the hard Stop leaves theirs. The
    page hands an Edit's or a Queue instead's text back.
    """
    clear = slot._steer_clear
    slot._steer_retract = None
    slot._steer_clear = None
    if clear is not None:
        clear.answered.set()


def withdraw_stale_offers(state: "DashboardState", slot: "_ChatSlot") -> None:
    """A new turn is starting: a take-back still offered belongs to a turn that is over.

    A steer stays ``written`` and offered when its turn ends without settling it:
    a hard Stop discards the pending steers without touching their rows, and a
    gateway restart mid-turn leaves them as persisted. The row would then offer
    Queue instead, Edit and Unsend for every later turn, and each click would be
    refused with ``steer_not_pending``. So each such row stops offering it here, its
    state and text unchanged, and is marked expired (``RETRACT_EXPIRED_META``): a
    page holding a take-back on it then hands an Edit's or a Queue instead's text
    back as for any turn that ended with the steer unread, instead of reporting
    that the agent refused.

    Only while nothing is pending or being taken back: then no row still offered
    can belong to the turn starting now. Any page holding a take-back on one of
    these rows has already seen its turn end.
    """
    if slot._pending_steers or slot._steer_retract is not None or slot._steer_clear is not None:
        return
    stale = [
        meta["mid"]
        for row in slot.messages
        if row.get("role") == "user"
        and isinstance(meta := row.get("meta"), dict)
        and meta.get(STEER_RETRACTABLE_META) is True
        and meta.get("steer") is True
        and meta.get("steerState") == STEER_STATE_WRITTEN
        and isinstance(meta.get("mid"), str)
        and meta["mid"]
    ]
    expired = {STEER_RETRACTABLE_META: False, RETRACT_EXPIRED_META: True}
    for mid in stale:
        _patch_row_meta(state, slot, mid, expired)
    if stale:
        logger.info(
            "steer take-back withdrawn from %d ended steer(s) on slot %s", len(stale), slot.key
        )


def take_back(state: "DashboardState", slot: "_ChatSlot", record: SteerRetract) -> None:
    """Take *record*'s steer out of the turn and do what the person asked with it."""
    held = dict(slot._steer_attachment_meta.get(record.message) or {})
    _forget(slot, record.message)
    _mark_retracted(state, slot, record)
    if record.action == RETRACT_QUEUE:
        # The queue admission is the audience that held when the person asked,
        # before the clear could suspend and let that audience change.
        queue_for_next_turn(
            state,
            slot,
            record.message,
            directive_user_origin=record.directive_user_origin,
            attachments=attachment_meta(held),
            quote=quote_meta(held).get("quote"),
            admission=record.admission,
        )
    logger.info("steer taken back for slot %s (%s)", slot.key, record.action)


def _forget(slot: "_ChatSlot", message: str) -> None:
    """Drop *message* from the pending steers and every map kept in lockstep."""
    if message in slot._pending_steers:
        slot._pending_steers.remove(message)
    responsive_chat.forget_steer(slot, message)
    for held in (
        slot._steer_delivery_ids,
        slot._steer_send_ids,
        slot._steer_user_origin,
        slot._steer_channel_origin,
        slot._steer_admissions,
        slot._steer_attachment_meta,
        slot._steer_decision_strips,
        slot._steer_automation,
    ):
        held.pop(message, None)
    slot._steer_act_now.discard(message)
    slot._steer_possibly_delivered.discard(message)


def _patch_row_meta(
    state: "DashboardState",
    slot: "_ChatSlot",
    mid: str,
    changes: dict,
    *,
    content: str | None = None,
) -> None:
    """Merge *changes* into the meta of the row *mid* and tell open clients.

    *content*, when given, replaces the row's text as well. Keyed on ``mid``, the
    row's own identity: ``ts`` is not one, since two rows minted in the same clock
    tick share it.
    """
    row = _row(slot, mid)
    if row is None:
        return
    ts = str(row.get("ts") or "")
    meta = {**(row.get("meta") or {}), **changes}
    if slot.update_message(ts, content=content, meta=meta, mid=mid) is None:
        return
    frame: dict[str, Any] = {"slot": slot.key, "ts": ts, "mid": mid, "meta": meta}
    if content is not None:
        frame["content"] = content
    state.broadcast_ws("chat_message_update", frame)


def _mark_retracted(state: "DashboardState", slot: "_ChatSlot", record: SteerRetract) -> None:
    """Record on the steer's row that it was taken back, and what for.

    The row keeps its place but not its text. Everything that later reads the
    transcript as the conversation -- a cold start's history replay, the memory
    consolidator, a summary, a fork, history search -- would otherwise present a
    message the person took back as something they said. An Edit's text went back
    to the composer, and a Queue instead's runs as its own row when it drains.
    """
    _patch_row_meta(
        state,
        slot,
        record.mid,
        {
            "steerState": STEER_STATE_RETRACTED,
            RETRACT_ACTION_META: record.action,
            STEER_RETRACTABLE_META: False,
        },
        content="",
    )


def _withdraw_offer(state: "DashboardState", slot: "_ChatSlot", record: SteerRetract) -> None:
    """The backend refused the clear: the row stops offering a take-back."""
    _patch_row_meta(state, slot, record.mid, {STEER_RETRACTABLE_META: False})


async def _send_again(slot: "_ChatSlot", messages: list[str]) -> None:
    """Resend steers a clear dropped besides the one taken back, in their order.

    Each stays pending throughout, so one whose resend fails, or whose turn ends
    first, is requeued by the turn's teardown like any steer the turn never read.
    """
    client = getattr(slot, "_acp_client", None)
    if client is None:
        return
    for message in messages:
        if message not in slot._pending_steers or getattr(slot, "_acp_client", None) is not client:
            continue
        wire = responsive_chat.turn_state(slot).steers.get(message, message)
        slot._steer_rpc_in_flight.add(message)
        try:
            sent = await client.steer(wire)
        except Exception as exc:
            sent = False
            if getattr(exc, "ambiguous_delivery", False) is True:
                slot._steer_possibly_delivered.add(message)
        finally:
            slot._steer_rpc_in_flight.discard(message)
        if not sent:
            logger.info("steer for slot %s not sent again after a clear", slot.key)
