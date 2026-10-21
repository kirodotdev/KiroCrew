"""Stamping the hand-back a conductor's ``session_send`` makes to a waiting worker.

The waiting item a send would answer, read before delivery and identified by the
report it answers, and the ``decide`` stamp written through the conductor's
commit-and-record path once the message is delivered.

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew import session_ledger, work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.handlers.work_ledger_conductor import _record_conductor_write
from kiro_crew.dashboard.handlers.work_ledger_recording import _acting_unit
from kiro_crew.dashboard.handlers.work_ledger_refusals import _refuse_400
from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")


@dataclasses.dataclass(frozen=True)
class SendHandback:
    """The waiting item a conductor's ``session_send`` would answer, read BEFORE delivery.

    ``report`` is the identity of the report the conductor is answering. The stamp
    re-reads the item under the board lock and writes only when that identity is
    unchanged, so a NEW ``blocked`` / ``question`` the worker files while the
    message is in flight is never marked answered by the message that preceded it.
    """

    conductor_key: str
    worker_key: str
    item_id: str
    report: tuple[Any, ...]


# The audit name of the hand-back stamp: its skips and writes are bookkeeping
# beside a delivered send, so they never read as a denied ``session_send``.
_HANDBACK_OPERATION = "session_send_handback"


def _report_identity(item: Any) -> tuple[Any, ...]:
    """What distinguishes one worker report from the next on *item*.

    Every worker-owned field a report writes, so two reports in the same second
    still differ when either changed anything. ``artifacts`` is serialized so the
    identity holds an immutable copy rather than the item's own mapping.
    """
    return (
        getattr(item, "last_report_at", None),
        getattr(item, "status", None),
        getattr(item, "reason", None),
        getattr(item, "summary", None),
        json.dumps(getattr(item, "artifacts", None), sort_keys=True, default=str),
        getattr(item, "pr", None),
    )


def _awaits_answer(item: Any, worker_key: str) -> bool:
    """*item* is open, bound to *worker_key*, waiting on its conductor, and not yet answered."""
    return (
        item is not None
        and not item.is_terminal
        and item.worker_session_key == worker_key
        and item.status in work_ledger.WAITING_STATUSES
        # A report with a ``reason`` (``approval`` / ``needs_human``) waits on a
        # person, not on the conductor's answer: a message about it hands nothing back.
        and not getattr(item, "reason", None)
        # One answer per report: a follow-up ("any update?") to an item already
        # handed back must not replace the recorded decision or restart the window.
        and work_ledger._decided_after_report(item) is None
    )


async def _read_waiting_item(conductor_key: str, worker_key: str) -> "tuple[str, Any] | None":
    """The ``(item_id, item)`` *worker_key* is bound to on *conductor_key*'s board, if waiting."""
    binding = await asyncio.to_thread(work_ledger.read_binding, worker_key)
    if binding is None:
        return None
    bound_conductor, item_id = binding
    if bound_conductor != conductor_key:
        # Another board's worker: the caller's message is not that item's answer.
        return None
    item = await asyncio.to_thread(work_ledger.read_work_item, bound_conductor, item_id)
    if not _awaits_answer(item, worker_key):
        return None
    return item_id, item


async def read_send_handback(caller_key: str, target_key: str) -> SendHandback | None:
    """The item a send from *caller_key* to *target_key* would answer, or ``None``.

    Read before the message is delivered, so the stamp answers the report the
    conductor saw. Every condition is the store's own identity, never the
    request's word: the TARGET must be a session whose binding names a live item
    (folded like every key that reaches the store), the CALLER must be the
    conductor that binding resolves to (a bystander messaging the worker hands
    nothing back), and the item must still be WAITING on that conductor and not
    yet answered since its last report. Never raises: a send must not fail over
    bookkeeping.
    """
    conductor_key = session_ledger.ledger_key(caller_key)
    worker_key = session_ledger.ledger_key(target_key)
    if not conductor_key or not worker_key:
        return None
    try:
        found = await _read_waiting_item(conductor_key, worker_key)
    except Exception:  # noqa: BLE001 - bookkeeping never fails a send
        logger.debug("session_send hand-back read failed for %s", worker_key, exc_info=True)
        return None
    if found is None:
        return None
    item_id, item = found
    return SendHandback(conductor_key, worker_key, item_id, _report_identity(item))


def _handback_decision(message: str) -> str | None:
    """The sent *message* as a ``decision``: redacted first, like every ledger write.

    The record and report routes redact before they commit, so the cache and its
    crew-log entry hold byte-identical text; the stamp keeps that rule. A message
    over ``MAX_DECISION_CHARS`` is clipped with a visible marker, because the full
    text already reached the worker and the decision only names the hand-back.
    """
    try:
        cleaned = crew_log_emit.safe_work_fields({"decision": message.strip()})["decision"]
    except crew_log_emit.WorkFieldError:
        return None
    if not isinstance(cleaned, str) or not cleaned:
        return None
    limit = work_ledger.MAX_DECISION_CHARS
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "\u2026"
    return cleaned


async def stamp_send_handback(
    state: DashboardState, handback: SendHandback, delivered_to: str, message: str
) -> bool:
    """The server half of the charter's "answer it, then record it".

    When a conductor's ``session_send`` reaches the worker session an OPEN item
    reported ``blocked`` / ``question`` on, that message IS the decision the
    charter then tells the conductor to record with ``work_ledger_record
    action=decide`` -- a second call an LLM conductor can forget, and a forgotten
    one left the item pointing at the conductor forever: never handed back, so
    :func:`work_ledger.is_stale` never flagged the worker that never resumed.
    This stamps that ``decided_at`` hand-back on the delivery itself, with the
    sent message as the ``decision`` -- the same write the forgotten call would
    have made, through the same commit-and-record pipeline.

    *handback* is what :func:`read_send_handback` saw before delivery. Under the
    board lock the item is read again and stamped only when it is the same
    waiting, unanswered report on the same binding; anything else is a plain
    message and moves nothing.

    Best-effort, and ``False`` for every skip or refusal: the message has
    already been delivered, so a bookkeeping miss must never read back as a
    failed send. A stamp that commits but cannot be recorded is undone, the
    same ``_refuse_unrecorded`` rule the routes keep.
    """
    conductor_key = handback.conductor_key
    item_id = handback.item_id
    if session_ledger.ledger_key(delivered_to) != handback.worker_key:
        return False
    decision = _handback_decision(message)
    if decision is None:
        return False
    unit, refusal = _acting_unit(state, conductor_key, _HANDBACK_OPERATION, item_id)
    if refusal is not None:
        return False
    assert unit is not None

    async def _still_answering() -> web.Response | None:
        # Re-read under the lock every report write also holds: ``decide`` only
        # checks that the item is open, so this is what fences the stamp to the
        # report the conductor answered.
        current = await _read_waiting_item(conductor_key, handback.worker_key)
        if (
            current is None
            or current[0] != item_id
            or _report_identity(current[1]) != handback.report
        ):
            return _refuse_400("handback_report_changed", "the answered report changed")
        return None

    try:
        response = await _record_conductor_write(
            state,
            conductor_key,
            unit,
            "decide",
            {"action": "decide", "item_id": item_id, "decision": decision},
            operation=_HANDBACK_OPERATION,
            before_write=_still_answering,
        )
    except Exception:  # noqa: BLE001 - bookkeeping never fails a delivered send
        logger.warning("session_send hand-back stamp failed for %s", item_id, exc_info=True)
        return False
    return response.status == 200
