"""Server-side default patrol for a conductor that dispatched work.

A conductor hands an item to a worker with ``work_ledger_record action=bind``.
Its prompt tells it to arm a ``monitor_start`` loop on itself afterwards, but
nothing enforced that: a conductor that forgot left its workers reporting to a
ledger nobody read. Two pieces close the gap:

* :func:`ensure_patrol` -- called by the bind route after a bind commits, and by
  the ``session_create`` route after a create commits. When
  the conductor's slot has no ACTIVE loop, it arms a ``watch="work-ledger"``
  patrol through the same chokepoint an agent's own ``monitor_start`` uses
  (``autonudge_authz.authorize_and_add_nudge``). A loop the system stopped
  counts as none and is replaced; an active loop is never displaced or
  stacked; a loop a person stopped is kept and the arm is refused with
  ``user-stopped``. A refusal is logged at WARNING and never fails the bind; the shutdown refusal a dispatch racing the gateway's teardown meets is logged at DEBUG instead.
  The loop is tagged ``default_patrol``: the conductor's own later
  ``monitor_start`` replaces it rather than meeting a 409.
* :func:`has_active_patrol` -- read by ``work_ledger_read`` to flag each open
  item ``unpatrolled`` while the conductor holds no active ``work-ledger`` watch,
  so the next turn sees it.

Provenance: the arm is EXTERNAL -- no ``initiator_slot_key``. The bind route
knows which session called it, not which turn: a cron injection, an app-driven
turn or a sub-agent sharing the slot sends the same ``X-Session-Key`` as the
session's own turn, and only the session-directive consumer can tell them apart.
So a crew/member conductor's slot refuses this arm as it refuses any outside
arm; the refusal is logged and the ``unpatrolled`` flag carries the signal.
The armed message is fixed text authored here, not agent input.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Inside the conductor's 300..900 s band; with the two bounds below it passes
#: the goal-conductor skill's ``patrol_budget.py check`` (pinned by a test).
PATROL_INTERVAL_SECS = 600
PATROL_MAX_CYCLES = 300
PATROL_MAX_RUNTIME_SECS = 86400

PATROL_WATCH = "work-ledger"

PATROL_MESSAGE = (
    "Conductor patrol (armed by the gateway when you dispatched work and had no "
    "active loop). Run work_ledger_read with compact=true. For each item with status "
    "done, read its bar with a full work_ledger_read, pipe the done-only "
    "accept_batch into the goal-conductor skill's accept_eval.py, and record the "
    "answer with work_ledger_record action=verdict. Answer question and blocked "
    "items with session_send. Ignore progress. Tune this loop with "
    "monitor_update; call autonudge_stop when every item is terminal or the user "
    "says stop."
)

#: Rides the bind reply whenever no ``work-ledger`` watch is active after the
#: bind -- a refused arm (a crew/member slot), a disabled service, or a slot whose
#: one loop is stopped or watches something else -- so the conductor is told in
#: the same turn that nobody is patrolling and what to call.
ARM_YOURSELF_NOTE = (
    "No patrol runs on your session. Arm one yourself with monitor_start "
    'watch="work-ledger" before you end this turn, or your workers\' reports go unread.'
)

#: Rides a dispatch reply when the caller holds no work ledger: a work-ledger
#: watch would have nothing to read, so none is armed and the caller is told
#: what fits instead.
NO_LEDGER_NOTE = (
    "No patrol runs on your session and you hold no work ledger. Before you end "
    "this turn, arm a monitor_start loop that reads the session you opened with "
    "session_read_message, or watches the one PR you wait on."
)

#: Outcomes :func:`ensure_patrol` returns, surfaced on the bind reply as ``patrol``.
ARMED = "armed"
EXISTING = "existing"
REFUSED = "refused"
UNSUPPORTED = "unsupported"


def reply_line(resp: dict[str, Any]) -> str:
    """The patrol outcome a dispatch reply ends with, always present.

    A dispatch route answers ``patrol``, ``patrol_reason`` and, when no
    work-ledger watch is active afterwards, ``patrol_note``. A reply missing
    ``patrol`` reads ``unknown`` with the arm-yourself note, so a dispatch never
    looks patrolled when nothing says it is.
    """
    patrol = str(resp.get("patrol") or "")
    if not patrol:
        return f" patrol=unknown. {ARM_YOURSELF_NOTE}"
    reason = str(resp.get("patrol_reason") or "unspecified")
    note = str(resp.get("patrol_note") or "")
    return f" patrol={patrol} ({reason})" + (f". {note}" if note else "")


def nudge_slot_for(state: Any, conductor_key: str) -> str | None:
    """The autonudge binding key of *conductor_key*, or ``None`` if none exists.

    The ledger key is the dashboard-prefix-stripped spelling, which for a
    dashboard session IS the bare slot key loops bind to; channel keys map
    through ``autonudge.binding_key_for``. Anything else (a ``cron:`` or
    ``subagent:`` key, a slot that is gone) has no loop to arm or read.
    """
    from kiro_crew.autonudge import binding_key_for

    key = (conductor_key or "").strip()
    if not key:
        return None
    channel = binding_key_for(key)
    if channel is not None:
        return channel
    try:
        return key if state.get_slot(key) is not None else None
    except Exception:  # noqa: BLE001 - a slot-table read must not fail the caller
        logger.debug("slot lookup failed for %s", key, exc_info=True)
        return None


def _stopped_row_is_replaceable(loop: Any) -> bool:
    from kiro_crew.autonudge_service.model import _stopped_row_is_replaceable as replaceable

    try:
        return bool(replaceable(loop))
    except Exception:  # noqa: BLE001 - an unreadable row is evidence, not replaceable
        return False


def _loop_on(state: Any, conductor_key: str) -> tuple[Any, Any, str | None]:
    from kiro_crew.autonudge import get_instance

    svc = get_instance()
    slot = nudge_slot_for(state, conductor_key)
    if svc is None or slot is None:
        return svc, None, slot
    try:
        return svc, svc.get_by_slot(slot), slot
    except Exception:  # noqa: BLE001 - a store read must not fail the caller
        logger.debug("loop lookup failed for %s", slot, exc_info=True)
        return svc, None, slot


def has_active_patrol(state: Any, conductor_key: str) -> bool:
    """Whether the conductor's slot holds an ACTIVE ``work-ledger`` watch.

    Keyed on the watch, not on any loop: a slot holds one loop, so a conductor
    whose loop watches a pull request has nobody reading its ledger. Unreadable
    reads False.
    """
    svc, loop, _slot = _loop_on(state, conductor_key)
    if svc is None or loop is None or not getattr(loop, "active", False):
        return False
    try:
        return bool(svc._observes_work_ledger(loop))
    except Exception:  # noqa: BLE001 - a store read must not fail the caller
        logger.debug("work-ledger watch check failed for %s", conductor_key, exc_info=True)
        return False


#: Why :func:`ensure_patrol` answered what it did, surfaced as ``patrol_reason``.
REASON_NO_LOOP = "no-loop"
REASON_REPLACED = "replaced-system-stopped"
REASON_ACTIVE_LEDGER = "active-work-ledger"
REASON_ACTIVE_OTHER = "active-other-watch"
REASON_USER_STOPPED = "user-stopped"
REASON_OUTSIDE_ARM = "outside-arm-refused"
REASON_CANNOT_HOST = "session-cannot-host-loop"
REASON_ARM_FAILED = "arm-failed"
REASON_DISABLED = "autonudge-disabled"
REASON_NO_LEDGER = "no-ledger"


def patrol_note(
    state: Any, conductor_key: str, reason: str, *, has_ledger: bool = True
) -> str | None:
    """The note a dispatch reply carries, or ``None`` when the caller is patrolled.

    With a ledger, patrolled means an active work-ledger watch. Without one, any
    active loop is the patrol: it watches the session or PR the caller opened.
    """
    if not has_ledger:
        return None if reason in (REASON_ACTIVE_LEDGER, REASON_ACTIVE_OTHER) else NO_LEDGER_NOTE
    if has_active_patrol(state, conductor_key):
        return None
    return ARM_YOURSELF_NOTE


def _refusal_reason(state: Any, slot: str, status: object) -> str:
    from kiro_crew.autonudge_authz import _EXTERNAL_ARM_REFUSED_MODES

    try:
        current = state.get_slot(slot)
    except Exception:  # noqa: BLE001 - a slot-table read must not fail the caller
        current = None
    if str(getattr(current, "mode", "") or "") in _EXTERNAL_ARM_REFUSED_MODES:
        return REASON_OUTSIDE_ARM
    if status == 403:
        return REASON_CANNOT_HOST
    return REASON_ARM_FAILED


async def ensure_patrol(
    state: Any,
    conductor_key: str,
    *,
    has_ledger: bool = True,
    source: str = "work-ledger-bind",
) -> tuple[str, str]:
    """Leave the conductor's slot with a work-ledger patrol, or say why not.

    Answers ``(outcome, reason)``: *outcome* is one of ``ARMED``, ``EXISTING``,
    ``REFUSED``, ``UNSUPPORTED`` and *reason* one of the ``REASON_*`` tokens.

    A slot holds one loop. An ACTIVE loop is left alone. An inactive loop the
    SYSTEM stopped -- its subject went terminal, a bound ran out, whoever armed
    it -- counts as no loop and is replaced, because a new bind is new work to
    patrol. An inactive loop a PERSON stopped is retained evidence and is
    refused with ``user-stopped``. The split is ``_stopped_row_is_replaceable``,
    the allowlist a directive re-arm uses, so this path cannot revive a stop a
    directive could not.

    A caller with no work ledger (*has_ledger* False, which the caller reads
    through the store's own route seam) gets nothing armed (``no-ledger``): the
    watch would read nothing and fire on its timer for a day.

    Never raises: every failure is logged and answered as an outcome, because
    the dispatch that called this has already committed.
    """
    try:
        svc, loop, slot = _loop_on(state, conductor_key)
        if svc is None or slot is None:
            logger.warning(
                "conductor patrol not armed for %s: %s",
                conductor_key,
                "auto-nudge is disabled" if svc is None else "session cannot host a loop",
            )
            return UNSUPPORTED, REASON_DISABLED if svc is None else REASON_CANNOT_HOST
        rearm = False
        if loop is not None:
            if getattr(loop, "active", False):
                try:
                    ledger = bool(svc._observes_work_ledger(loop))
                except Exception:  # noqa: BLE001 - a store read must not fail the caller
                    ledger = False
                return EXISTING, REASON_ACTIVE_LEDGER if ledger else REASON_ACTIVE_OTHER
        if not has_ledger:
            return UNSUPPORTED, REASON_NO_LEDGER
        if loop is not None:
            if not _stopped_row_is_replaceable(loop):
                logger.warning(
                    "conductor patrol not armed for %s: its loop was stopped by a person",
                    conductor_key,
                )
                return REFUSED, REASON_USER_STOPPED
            rearm = True
        from kiro_crew.autonudge import is_channel_key
        from kiro_crew.autonudge_authz import authorize_and_add_nudge
        from kiro_crew.autonudge_service.model import SERVICE_SHUTTING_DOWN_MESSAGE
        from kiro_crew.monitoring.models import MonitorCreationSurface

        _armed, error, status = await authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key=slot,
            message=PATROL_MESSAGE,
            idle_secs=PATROL_INTERVAL_SECS,
            max_cycles=PATROL_MAX_CYCLES,
            max_runtime_secs=PATROL_MAX_RUNTIME_SECS,
            watch=PATROL_WATCH,
            gate=True,
            # The audit tag: which dispatch route armed this patrol.
            source=source,
            caller="conductor-patrol",
            # Create-only: a loop that appeared since the read above wins, and
            # nothing is ever stacked beside it. Stopped-row displacement only for
            # the system-stopped row above.
            replace_existing=False,
            replace_stopped=rearm,
            # The tag that lets the conductor's own ``monitor_start`` replace this
            # loop instead of meeting a 409 (``NudgeLoop.default_patrol``).
            default_patrol=True,
            creation_surface=(
                MonitorCreationSurface.CHANNEL
                if is_channel_key(slot)
                else MonitorCreationSurface.DASHBOARD
            ),
        )
    except Exception:  # noqa: BLE001 - the bind already committed
        logger.warning("conductor patrol arm failed for %s", conductor_key, exc_info=True)
        return REFUSED, REASON_ARM_FAILED
    if error is not None:
        if (
            status == 503
            and error == SERVICE_SHUTTING_DOWN_MESSAGE
            and getattr(svc, "accepting_mutations", True) is False
        ):
            # The gateway is shutting down: ``add`` returned its canonical admission
            # refusal after closure. Other 503s (audit or credential outages) remain
            # warnings even when they race the same closed-admission window.
            logger.debug(
                "conductor patrol not armed for %s: auto-nudge is shutting down", conductor_key
            )
            return REFUSED, _refusal_reason(state, slot, status)
        logger.warning(
            "conductor patrol arm refused for %s: %s [status %s]", conductor_key, error, status
        )
        return REFUSED, _refusal_reason(state, slot, status)
    logger.info("conductor patrol armed on %s", slot)
    return ARMED, REASON_REPLACED if rearm else REASON_NO_LOOP
