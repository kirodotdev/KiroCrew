"""One session-side entry point for text a non-human sender delivers to a slot.

The sender names WHO it is (:class:`Origin`); this module decides HOW the text
lands. An idle slot gets the origin's row and a turn; a running slot gets the
origin's running-path delivery (a steer for an automation wake, the queue for a
cron notification or an app message). The row's role and meta come from the
origin alone (:func:`origin_row`), so the same sender never draws as two
different rows depending on whether the slot happened to be busy. A peer
session's ``session_send`` is a ``user`` row on both paths: a steer when it asks
for one and the slot is running, else the queue-or-run path.

Callers keep their own authorization, redaction and audit: everything here runs
after the caller's gate has cleared the delivery. The person's own typing does
not come through here; it has its own composer path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: Who sent the text. ``automation`` is a monitor loop's wake, ``cron`` a cron
#: job's notification, ``app`` an embedded MCP App's message.
ORIGIN_AUTOMATION = "automation"
ORIGIN_CRON = "cron"
ORIGIN_APP = "app"
#: Another session's ``session_send``: its row is a ``user`` row, written by the
#: steer or the queue-or-run path like a typed message, and never the human stamp.
ORIGIN_PEER = "peer"

#: What :func:`deliver` did with the text.
DELIVERED_TURN = "turn"
DELIVERED_QUEUED = "queued"
DELIVERED_STEERED = "steered"
DELIVERED_REQUEUED = "requeued"
NOT_DELIVERED = "unavailable"
QUEUE_FULL = "queue_full"


@dataclass(frozen=True)
class Origin:
    """The sender of a delivery and what its row names.

    ``label`` is the cron job or app label the row shows; ``nudge`` is the
    ``meta.nudge`` (``{"cycle", "loop_id"}``) an automation wake's row carries.
    Build one with :func:`cron`, :func:`app` or :func:`automation`.
    """

    kind: str
    label: str = ""
    nudge: dict | None = None
    steer: bool = False


def cron(label: str) -> Origin:
    return Origin(ORIGIN_CRON, label=label)


def peer(*, steer: bool) -> Origin:
    """A peer session's text.

    ``steer=True`` is the steer arm only: it never queues or starts a turn, so a
    caller whose turn ended before the steer ran gets :data:`NOT_DELIVERED` and
    makes the one queue-or-run delivery itself (``steer=False``).
    """
    return Origin(ORIGIN_PEER, steer=steer)


def app(label: str) -> Origin:
    return Origin(ORIGIN_APP, label=label)


def automation(cycle: int, loop_id: str) -> Origin:
    return Origin(ORIGIN_AUTOMATION, nudge={"cycle": cycle, "loop_id": loop_id})


def origin_row(origin: Origin) -> tuple[str, str, dict[str, Any]]:
    """The ``(role, cls, meta)`` of the row *origin*'s text is drawn as.

    The one place a sender maps to a row. A cron row's ``cls`` is the JSON the
    frontend reads its label from, and the label also rides in ``meta`` because
    ``cls`` is not persisted for an ``inject`` row. An app row comes from
    ``app_inject_row``, the builder its queued twin's drain uses too. An
    automation row is the nudge chip the idle autonudge fire writes.
    """
    if origin.kind == ORIGIN_CRON:
        return (
            "inject",
            json.dumps({"cronLabel": origin.label}),
            {"injectKind": "cron", "cronLabel": origin.label},
        )
    if origin.kind == ORIGIN_APP:
        from kiro_crew.dashboard.chat_utils import app_inject_row

        return app_inject_row(origin.label)
    if origin.kind == ORIGIN_AUTOMATION:
        return ("nudge", "msg msg-nudge", {"nudge": dict(origin.nudge or {})})
    raise ValueError(f"unknown delivery origin: {origin.kind!r}")


def write_origin_row(
    slot: "_ChatSlot", text: str, origin: Origin, *, extra_meta: dict | None = None
) -> dict[str, Any]:
    """Append *origin*'s row (:func:`origin_row`) with *text*; return the row.

    The one writer the idle paths share: :func:`deliver`'s own, and the idle
    autonudge fire, which writes its nudge row when its turn is accepted.
    *extra_meta* adds sender facts the origin does not carry (a monitor
    wake's ``monitor`` record) and never replaces the origin's own keys.
    """
    role, cls, meta = origin_row(origin)
    return slot.append(role, text, cls, meta={**(extra_meta or {}), **meta})


async def deliver(
    state: "DashboardState",
    slot: "_ChatSlot",
    text: str,
    origin: Origin,
    *,
    admission: dict | None = None,
    queue_meta: dict | None = None,
    evict_oldest: bool = False,
) -> str:
    """Deliver *text* from *origin* into *slot*; return a ``DELIVERED_*`` outcome.

    Idle slot: a cron or app message is queued and the queue drain starts it
    (:func:`_deliver_idle`); a slot that is closing or the gateway is shutting
    down answers :data:`NOT_DELIVERED` and the caller falls back. Running slot: an
    automation wake is steered into the turn (its row is the same nudge row,
    written by ``steer_into_running_turn``), and a cron or app message is queued
    behind it with a ``queued`` card, so an injection never starts a second
    turn. An idle automation wake returns :data:`NOT_DELIVERED`: that turn is the
    autonudge fire's own path (``_fire_dashboard_nudge``), which owns its
    completion hook.

    *admission* is the containment snapshot the caller's gate held: the steer
    records it for a requeue, and an app message's queue entry carries it so the
    drain re-checks it. :data:`QUEUE_FULL` means the live queue had no room and
    nothing was written. *evict_oldest* makes a cron notification on a full live
    queue drop the oldest queued cron notification no person edited, and its card,
    so the newest report waits; with none, nothing else gives way, nothing is
    written and the answer is :data:`QUEUE_FULL`, which each cron caller turns into
    its out-of-session fallback.
    """
    if origin.kind == ORIGIN_PEER:
        return await _deliver_peer(state, slot, text, origin, admission, queue_meta)
    if slot.running:
        return await _deliver_running(state, slot, text, origin, admission, evict_oldest)
    if origin.kind == ORIGIN_AUTOMATION:
        return NOT_DELIVERED
    return await _deliver_idle(state, slot, text, origin, admission, evict_oldest)


async def _deliver_idle(
    state: "DashboardState",
    slot: "_ChatSlot",
    text: str,
    origin: Origin,
    admission: dict | None,
    evict_oldest: bool,
) -> str:
    """The idle-slot half of :func:`deliver` for a cron or app message.

    The message is queued as the running-slot half queues it, then the queue
    drain starts it, so it takes the checks every queued message takes before
    its turn: memory preparation still running, a Stop being resolved, a sign-in
    hold and an entry already waiting ahead of it. When one of them refuses, the
    card waits for the next drain. A slot that is closing, was replaced while
    this waited for its lock, or receives the message during gateway shutdown
    takes nothing: its queued card would not survive this process, so the caller
    falls back as for a gone session.
    """
    from kiro_crew.dashboard.chat_runner import (
        _gateway_shutdown_requested,
        _start_next_queued_turn,
        idle_slot_admits_drain,
    )

    async with slot._lock:
        if (
            slot.is_closing
            or state.get_slot(slot.key) is not slot
            or _gateway_shutdown_requested(state)
        ):
            return NOT_DELIVERED
        outcome = _queue_entry(state, slot, text, origin, admission, evict_oldest)
        if outcome != DELIVERED_QUEUED or not idle_slot_admits_drain(state, slot):
            return outcome
        slot._turn_admission_reserved = True
        try:
            started = await _start_next_queued_turn(state, slot)
        finally:
            slot._turn_admission_reserved = False
    return DELIVERED_TURN if started else DELIVERED_QUEUED


async def _deliver_running(
    state: "DashboardState",
    slot: "_ChatSlot",
    text: str,
    origin: Origin,
    admission: dict | None,
    evict_oldest: bool = False,
) -> str:
    """The running-slot half of :func:`deliver`."""
    if origin.kind == ORIGIN_AUTOMATION:
        from kiro_crew.dashboard.chat_delivery import (
            STEER_REQUEUED,
            STEER_STEERED,
            steer_into_running_turn,
        )

        outcome = await steer_into_running_turn(
            state,
            slot,
            text,
            user_origin=False,
            admission=admission,
            nudge_meta=dict(origin.nudge or {}),
        )
        if outcome == STEER_STEERED:
            return DELIVERED_STEERED
        if outcome == STEER_REQUEUED:
            return DELIVERED_REQUEUED
        return NOT_DELIVERED
    return _queue_entry(state, slot, text, origin, admission, evict_oldest)


def _queue_entry(
    state: "DashboardState",
    slot: "_ChatSlot",
    text: str,
    origin: Origin,
    admission: dict | None,
    evict_oldest: bool,
) -> str:
    """Queue a cron or app message as a card and return the delivery outcome."""
    if origin.kind == ORIGIN_CRON:
        from kiro_crew.dashboard.cron_inject import queue_cron_notification

        if evict_oldest:
            from kiro_crew.dashboard.chat_utils import (
                CRON_NOTIFICATION_KIND,
                _remove_queued_by_id,
            )
            from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

            if len(slot._queue) >= MAX_LIVE_QUEUE_ENTRIES:
                # Only another cron notification gives way: a person's prompt, an
                # app's message or a peer's send is never dropped by automation,
                # and neither is a cron card a person edited (its words are theirs
                # now, though it keeps the cron kind).
                index = next(
                    (
                        i
                        for i, entry in enumerate(slot._queue)
                        if entry.get("kind") == CRON_NOTIFICATION_KIND
                        and not entry.get("_directive_user_origin")
                        and not entry.get("_directive_channel_origin")
                    ),
                    None,
                )
                if index is None:
                    logger.warning(
                        "Queue full for slot %s with no cron notification to evict: "
                        "dropping this one",
                        slot.key,
                    )
                    return QUEUE_FULL
                evicted = slot.queue_pop(index)
                logger.warning(
                    "Queue full for slot %s: evicting the oldest cron notification", slot.key
                )
                _remove_queued_by_id(slot.messages, evicted["id"])
                # Open dashboards render queue cards from WebSocket frames, not
                # from this slot's message list.
                state.broadcast_ws("queue_cancel", {"slot": slot.key, "queue_id": evicted["id"]})
        queue_cron_notification(slot, text, origin.label)
        return DELIVERED_QUEUED
    if origin.kind == ORIGIN_APP:
        from kiro_crew.dashboard.chat_utils import MCP_APP_MESSAGE_KIND
        from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

        if len(slot._queue) >= MAX_LIVE_QUEUE_ENTRIES:
            return QUEUE_FULL
        # The label rides the entry meta beside the containment stamp: the drain
        # builds the inject row from it. The card's cls is plain CSS, because a
        # JSON cls replaces the stored meta on an HTTP rebuild.
        qid = slot.queue_append(
            text,
            kind=MCP_APP_MESSAGE_KIND,
            meta={"appLabel": origin.label, **(admission or {})},
        )
        slot.append(
            "queued",
            text,
            "msg msg-queued",
            meta={"kind": MCP_APP_MESSAGE_KIND, "appLabel": origin.label, "queueId": qid},
        )
        return DELIVERED_QUEUED
    raise ValueError(f"unknown delivery origin: {origin.kind!r}")


async def _deliver_peer(
    state: "DashboardState",
    slot: "_ChatSlot",
    text: str,
    origin: Origin,
    admission: dict | None,
    queue_meta: dict | None,
) -> str:
    """A peer's text: the steer arm when asked, else queue or run it.

    The steer writes the ``user`` steer row; the queue-or-run path writes the
    ``user`` row a queued or started send gets. ``user_origin=False`` on both, so
    a peer never inherits the person's exemption from the drain's linked drop.
    *queue_meta* rides the queue entry only (``enqueue_or_run_prompt``'s
    ``extra_meta``), and *admission* is the steer's requeue baseline.
    """
    if origin.steer:
        if not slot.running:
            # The turn ended before this ran. Never fall through to queue-or-run
            # here: the caller's fallback delivers it, and two arms would run it twice.
            return NOT_DELIVERED
        from kiro_crew.dashboard.chat_delivery import (
            STEER_REQUEUED,
            STEER_STEERED,
            steer_into_running_turn,
        )

        outcome = await steer_into_running_turn(
            state, slot, text, user_origin=False, admission=admission
        )
        if outcome == STEER_STEERED:
            return DELIVERED_STEERED
        if outcome == STEER_REQUEUED:
            return DELIVERED_REQUEUED
        return NOT_DELIVERED
    from kiro_crew.dashboard.chat_runner import _run_chat

    started = slot.enqueue_or_run_prompt(text, _run_chat, state, extra_meta=queue_meta)
    return DELIVERED_TURN if started else DELIVERED_QUEUED
