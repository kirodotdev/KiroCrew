"""Pending steers: a steer that could not be injected yet waits for the next tool boundary.

``session_send(steer=true)`` and ``session_broadcast(mode=steer)`` ask for a message to
cut into the target's RUNNING turn. When injection is unavailable at the instant of the
send (no steer-capable client yet, no prompt in flight, a /compact turn, an identical
steer in flight, the pending cap), the gateway holds the message here instead of
queueing it behind the whole turn.

* The stream loop calls :func:`flush_at_boundary` after every tool result. Each held
  steer is retried there, every boundary, until it is delivered.
* If the turn ends first, ``_hand_off_queue`` calls :func:`requeue_all` before it drains,
  so the held steers join the head of the end-of-turn queue, in send order, and the
  next turn starts in the same hand-off.
* A steer is identified by ``sender + target + message``. Sending the same text again
  while it is held returns the same steer and delivers nothing a second time. Once it
  is settled, the same text is a new message.

What a held steer DOES at a boundary (re-authorize, steer, check the audience) is a
closure the sender builds in ``session_messaging``, where those gates live; this module
only holds, retries, settles and reports.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: What a boundary attempt reports.
DELIVERED = "delivered"
QUEUED = "queued"
RETRY = "retry"
DROPPED = "dropped"

#: Receipt states. ``delivered`` and ``queued`` carry ``@<iso ts>`` when settled.
PENDING = "pending-steer"

#: What :func:`hold` reports.
HELD = "held"
DUPLICATE = "duplicate"
ENDED = "ended"
FULL = "full"

#: Seconds a turn's hand-off waits for a running flush before cancelling it.
FLUSH_SETTLE_SECS = 10.0

#: Held steers per slot; past it a send is refused rather than quietly queued.
MAX_DEFERRED_STEERS = 8

#: Settled receipts kept per slot, for the status read-out.
MAX_RECEIPTS = 32


@dataclass
class DeferredSteer:
    steer_id: str
    sender: str
    created_ts: float
    #: One boundary attempt, handed this very entry so that it can tell, after any await,
    #: whether it is still the held one (a replacement of the same steer id is not).
    #: Returns DELIVERED, RETRY or DROPPED. May raise.
    attempt: Callable[["DeferredSteer"], Awaitable[str]] = field(repr=False)
    #: The end-of-turn path for a steer the turn outlived.
    to_queue: Callable[..., Awaitable[bool]] = field(repr=False)
    #: The sender's stamp (``send_origin_meta``) and the text, for the notice a hard
    #: stop or a refusal owes the sender.
    origin_meta: dict = field(default_factory=dict, repr=False)
    text: str = field(default="", repr=False)
    attempts: int = 0


def steer_id_for(sender: str, target_key: str, message: str) -> str:
    """The dedup key: one id per (sender, target, message)."""
    raw = "\x00".join((sender, target_key, message)).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _stamp(state: str) -> str:
    return f"{state}@{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def known_steer(slot: "_ChatSlot", steer_id: str) -> "dict[str, Any] | None":
    """The reply facts of a held steer; None when *steer_id* is not held."""
    if any(held.steer_id == steer_id for held in slot._deferred_steers):
        return {"pending": True, "state": PENDING}
    return None


def same_steer_reply(slot: "_ChatSlot", steer_id: str) -> dict[str, Any]:
    """The send reply for a steer that is still held, or still on its way into this turn."""
    seen = known_steer(slot, steer_id) or {"pending": False, "state": "steering"}
    return {
        "ok": True,
        "target": slot.key,
        "started": False,
        "steered": False,
        "pending_steer": seen["pending"],
        "steer_id": steer_id,
        "steer_state": seen["state"],
    }


def hold(
    slot: "_ChatSlot",
    *,
    steer_id: str,
    sender: str,
    attempt: Callable[["DeferredSteer"], Awaitable[str]],
    to_queue: Callable[..., Awaitable[bool]],
    origin_meta: dict | None = None,
    text: str = "",
) -> str:
    """Hold a steer for the next boundary; return ``HELD`` or why it was not held.

    Every check and the append run with no await between them, so two overlapping
    sends of one steer cannot both be held.
    """
    if known_steer(slot, steer_id) is not None:
        return DUPLICATE
    marked_text = slot._deferred_steer_delivered.get(steer_id)
    if marked_text is not None and marked_text in slot._pending_steers:
        # The marked steer is injected and still pending in this turn: a send of the
        # same id is that steer, whatever its prompt text (another verb carries
        # another provenance line), and its mark stays for the teardown to honour.
        return DUPLICATE
    if slot._turn_generation == slot._deferred_steer_ended_gen:
        # The turn is in its hand-off: nothing would hand a held steer on.
        return ENDED
    if len(slot._deferred_steers) >= MAX_DEFERRED_STEERS:
        return FULL
    slot._deferred_steers.append(
        DeferredSteer(
            steer_id=steer_id,
            sender=sender,
            created_ts=time.time(),
            attempt=attempt,
            to_queue=to_queue,
            origin_meta=dict(origin_meta or {}),
            text=text,
        )
    )
    slot._deferred_steer_receipts.pop(steer_id, None)
    # A mark that reaches here names text that is not pending: that steer is settled.
    # Kept, it would make this fresh entry read as delivered.
    slot._deferred_steer_delivered.pop(steer_id, None)
    slot._deferred_steer_injected.pop(steer_id, None)
    return HELD


def _settle(slot: "_ChatSlot", held: DeferredSteer, state: str) -> None:
    if held in slot._deferred_steers:
        slot._deferred_steers.remove(held)
    receipts = slot._deferred_steer_receipts
    receipts[held.steer_id] = {"state": _stamp(state)}
    while len(receipts) > MAX_RECEIPTS:
        receipts.pop(next(iter(receipts)))


def _settle_refused(state: "DashboardState", slot: "_ChatSlot", held: DeferredSteer) -> None:
    """Settle *held* ``refused`` and tell its sender, who was told ``pending-steer``.

    Best-effort, as the hard stop's notice is: a failing notice is logged and the
    steer is still settled.
    """
    from kiro_crew.dashboard.session_control import (
        SEND_DISCARD_REFUSED_STEER,
        notify_send_origin_discarded,
    )

    try:
        notify_send_origin_discarded(
            state,
            entry_meta=held.origin_meta,
            target_slot=slot,
            text=held.text,
            cause=SEND_DISCARD_REFUSED_STEER,
        )
    except Exception:  # noqa: BLE001 - the refusal stands; the notice is best-effort
        logger.warning("refused notice for steer %s failed", held.steer_id, exc_info=True)
    _settle(slot, held, "refused")


def settle_held(slot: "_ChatSlot", steer_id: str, state: str) -> None:
    """Settle the held entry named *steer_id* (no-op when it is not held)."""
    for held in list(slot._deferred_steers):
        if held.steer_id == steer_id:
            _settle(slot, held, state)


def mark_unconfirmed(slot: "_ChatSlot", text: str) -> None:
    """The turn ended with a boundary-injected steer *text* still unconfirmed.

    Its receipt reads ``unconfirmed@<time>`` instead of ``delivered@<time>``: the
    harness took it but never confirmed it, and it is not queued back. Every marked
    steer is recorded injected too, so the injected record names both kinds.
    """
    for steer_id, marked_text in slot._deferred_steer_injected.items():
        if marked_text == text:
            slot._deferred_steer_receipts[steer_id] = {"state": _stamp("unconfirmed")}


def receipts(slot: "_ChatSlot") -> list[dict[str, str]]:
    """Held steers of *slot*, then its settled ones newest first, for the status row.

    The status line shows the first few, so the newest settled steer is never the one
    that is cut.
    """
    rows = [{"steer_id": held.steer_id, "state": PENDING} for held in slot._deferred_steers]
    rows += [
        {"steer_id": steer_id, "state": receipt["state"]}
        for steer_id, receipt in reversed(slot._deferred_steer_receipts.items())
    ]
    return rows


def schedule_flush(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Start one flush task unless one is running, so the stream loop keeps reading.

    A steer's acknowledgement is read by the same loop that calls this; awaiting
    the flush inline would leave that acknowledgement unread until a timeout.
    A boundary that arrives while the task runs is recorded, and the task runs one
    more pass for it when the current one finishes.
    """
    task = slot._deferred_flush_task
    if task is None or task.done():
        slot._deferred_flush_task = asyncio.get_running_loop().create_task(
            _flush_passes(state, slot)
        )
    else:
        slot._deferred_flush_dirty = True


async def _flush_passes(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Run boundary flushes, one at a time, while boundaries keep arriving during them.

    A pass walks the held list it copied at its start, so a steer held during the
    pass is tried by the next one. The turn's hand-off stops new passes: it reads
    the held list once this task is settled. A cancel ends the loop with no pass.
    """
    while True:
        slot._deferred_flush_dirty = False
        await flush_at_boundary(state, slot)
        if not slot._deferred_flush_dirty:
            return
        if slot._turn_generation == slot._deferred_steer_ended_gen:
            slot._deferred_flush_dirty = False
            return


async def _settle_flush_task(slot: "_ChatSlot") -> None:
    """Let a running flush finish (bounded) before the turn's hand-off reads the list."""
    task = slot._deferred_flush_task
    if task is not None and not task.done():
        await asyncio.wait({task}, timeout=FLUSH_SETTLE_SECS)
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=FLUSH_SETTLE_SECS)


async def flush_at_boundary(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Retry every held steer of *slot* now. Called at each tool-call boundary."""
    # A delivered mark only matters while its steer is still pending; drop the rest,
    # so the marks never outgrow the pending list.
    for marks in (slot._deferred_steer_delivered, slot._deferred_steer_injected):
        for steer_id, text in list(marks.items()):
            if text not in slot._pending_steers:
                marks.pop(steer_id, None)
    for held in list(slot._deferred_steers):
        if held not in slot._deferred_steers:
            continue
        held.attempts += 1
        try:
            outcome = await held.attempt(held)
        except Exception:  # noqa: BLE001 - one bad steer must not break the stream loop
            # Kept held: the next boundary tries again, and the turn's end queues it.
            # An authorization refusal is the attempt's own DROPPED answer, not this.
            logger.warning("deferred steer %s failed at a boundary", held.steer_id, exc_info=True)
            outcome = RETRY
        if held not in slot._deferred_steers:
            # A hard stop discarded it while the attempt was awaiting: its receipt
            # already reads "discarded" and must not be overwritten.
            continue
        if outcome in (DELIVERED, QUEUED):
            _settle(slot, held, outcome)
        elif outcome == DROPPED:
            _settle_refused(state, slot, held)


async def requeue_all(state: "DashboardState", slot: "_ChatSlot") -> None:
    """The turn ended first: hand every held steer to the end-of-turn queue.

    Called by ``_hand_off_queue`` before it drains, so the next turn starts in the
    same call. The queue entry carries the same text and provenance as a steer would,
    and joins the HEAD of the queue, as an unconsumed steer does: a held steer runs
    ahead of messages queued meanwhile. The held list is walked newest first, so
    after the head inserts the held steers run in the order they were sent.
    A steer already marked delivered (its injection may have reached the harness) is
    settled without a queue entry, so it never runs a second time.
    """
    slot._deferred_steer_ended_gen = slot._turn_generation
    await _settle_flush_task(slot)
    for held in reversed(slot._deferred_steers[:]):
        if held.steer_id in slot._deferred_steer_delivered:
            _settle(slot, held, DELIVERED)
            continue
        try:
            queued = await held.to_queue(lambda: held in slot._deferred_steers, at_head=True)
        except Exception:  # noqa: BLE001 - a refused entry must not strand the others
            logger.warning("deferred steer %s could not be queued", held.steer_id, exc_info=True)
            queued = False
        if held in slot._deferred_steers:
            if queued:
                _settle(slot, held, QUEUED)
            else:
                _settle_refused(state, slot, held)
        # else: a hard stop discarded it during the await; its receipt stays "discarded".


def discard_all(slot: "_ChatSlot", notify: Callable[["DeferredSteer"], object]) -> None:
    """A hard stop discards every held steer: tell each sender, settle each receipt.

    *notify* is called once per held steer, before it is settled ``discarded``; a
    notice that fails does not stop the discard.
    """
    for held in list(slot._deferred_steers):
        try:
            notify(held)
        except Exception:  # noqa: BLE001 - the stop already decided; the notice is best-effort
            logger.warning("discard notice for steer %s failed", held.steer_id, exc_info=True)
        # A steer already marked delivered may have run: its receipt says so.
        _settle(
            slot,
            held,
            "unconfirmed" if held.steer_id in slot._deferred_steer_delivered else "discarded",
        )
    slot._deferred_steer_delivered.clear()
    slot._deferred_steer_injected.clear()
