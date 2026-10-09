"""Server-side default patrol for a conductor that dispatched work.

A conductor hands an item to a worker with ``work_ledger_record action=bind``.
Its prompt tells it to arm a ``monitor_start`` loop on itself afterwards, but
nothing enforced that: a conductor that forgot left its workers reporting to a
ledger nobody read. Two pieces close the gap:

* :func:`ensure_patrol` -- called by the bind route after a bind commits. When
  the conductor's slot has NO loop at all, it arms a ``watch="work-ledger"``
  patrol through the same chokepoint an agent's own ``monitor_start`` uses
  (``autonudge_authz.authorize_and_add_nudge``), create-only, so an existing
  loop -- active, paused or stopped and retained by a person -- is never
  displaced or stacked. A refusal is logged at WARNING and never fails the bind.
  The loop is tagged ``default_patrol``: the conductor's own later
  ``monitor_start`` replaces it rather than meeting a 409.
* :func:`has_active_patrol` -- read by ``work_ledger_read`` to flag each open
  item ``unpatrolled`` while the conductor holds no active ``work-ledger`` watch,
  so the next turn sees it.

Provenance: the arm is the GATEWAY'S, not the session's -- no
``initiator_slot_key``. The bind route knows which session called it, not which
turn: a cron injection, an app-driven turn or a sub-agent sharing the slot sends
the same ``X-Session-Key`` as the session's own turn, and only the
session-directive consumer can tell them apart, so this module never claims a
self-arm. A crew/member conductor's slot admits it anyway, as the one arm that
carries nothing of an outsider's: the authorizer pins the text to
:data:`PATROL_MESSAGE` and the watch to the slot's own ledger
(``autonudge_authz.is_gateway_patrol``) and writes the gateway-patrol trust
entry the fire-time guard requires. That guard pins the STORED row the same way
(:func:`is_patrol_loop`) before it reads the entry, because the loop store is
agent-writable and the entry names only an id and a slot: a rewritten
``message`` under the patrol's own id is refused like any other outside text.
The flag that names this arm, ``default_patrol=``, is passed by this module
alone (a test scans the tree).
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

#: The text the authorizer admits on a crew/member slot, byte for byte
#: (``autonudge_authz.is_gateway_patrol``), and the text the fire-time guard
#: reads a STORED crew/member row against (:func:`is_patrol_loop`). An edit here
#: is therefore an edit to what such a slot accepts AND to what every patrol
#: already in a store must say: a row armed under the old text is refused at
#: every fire until its conductor arms its own loop (which replaces it) or
#: stops it, after which the next bind arms a fresh default; its budget does
#: not end it while the ledger holds open items. A digest test pins the text so
#: the edit is made knowingly; the member-conductor test pins that the arm
#: still lands. On a
#: crew/member slot the sentence "tune this loop with monitor_update" covers the
#: bounds, not this text, the watch or a banner: the update chokepoint refuses
#: those three with the arm that replaces the patrol instead.
PATROL_MESSAGE = (
    "Conductor patrol (armed by the gateway when you bound a worker and had no "
    "loop). Run work_ledger_read with compact=true. For each item with status "
    "done, read its bar with a full work_ledger_read, pipe the done-only "
    "accept_batch into the goal-conductor skill's accept_eval.py, and record the "
    "answer with work_ledger_record action=verdict. Answer question and blocked "
    "items with session_send. Ignore progress. Tune this loop with "
    "monitor_update; call autonudge_stop when every item is terminal or the user "
    "says stop."
)

#: Rides the bind reply whenever no ``work-ledger`` watch is active after the
#: bind -- a refused arm (an incognito or temporary slot, an audit that could not
#: be written), a disabled service, or a slot whose one loop is stopped or
#: watches something else -- so the conductor is told in the same turn that
#: nobody is patrolling and what to call.
ARM_YOURSELF_NOTE = (
    "No patrol runs on your session. Arm one yourself with monitor_start "
    'watch="work-ledger" before you end this turn, or your workers\' reports go unread.'
)

#: Outcomes :func:`ensure_patrol` returns, surfaced on the bind reply as ``patrol``.
ARMED = "armed"
EXISTING = "existing"
REFUSED = "refused"
UNSUPPORTED = "unsupported"


def is_patrol_loop(loop: Any) -> bool:
    """Whether a STORED loop record carries the default patrol, content and all.

    The twin of ``autonudge_authz.is_gateway_patrol`` for the record the store
    hands back at fire time. The arm-time check pins the REQUEST: flag, exact
    :data:`PATROL_MESSAGE`, ``work-ledger`` watch. This pins the ROW the same
    way, because the store (``autonudge.json``) is agent-writable and the trust
    entry the authorizer wrote names only the loop's id and slot. A row whose
    ``message`` was rewritten under an intact id, slot and flag would otherwise
    ride the patrol's admission into a crew/member thread -- which is the one
    thing that admission exists to rule out. So: the flag is the boolean True,
    the text is the fixed one byte for byte, and the monitor is this slot's own
    ``work-ledger`` watch (``kind`` is the watch name, ``target`` the slot key,
    the shape ``probes.targets.work_ledger_target`` builds). The row must also
    keep the SHAPE the patrol is armed in, because the other fields that put
    text in front of the model live in the same store: ``gate`` is True (the
    patrol is a gated loop; ungated, a persisted claim is dispatched as a
    structured envelope), the monitor's ``wake_instructions`` is empty (that
    envelope's action line), and ``banner`` is empty (a banner is what an
    interrupted wake restores as the instruction). ``ensure_patrol`` sets none
    of the three, so a row carrying any of them was written by something else.
    Anything else is not the patrol, whatever the flag says. Total: reads
    attributes only, never raises on a malformed row.
    """
    if getattr(loop, "default_patrol", False) is not True:
        return False
    if getattr(loop, "gate", False) is not True:
        return False
    if str(getattr(loop, "message", "") or "").strip() != PATROL_MESSAGE:
        return False
    if str(getattr(loop, "banner", "") or "").strip():
        return False
    monitor = getattr(loop, "monitor", None)
    if monitor is None:
        return False
    if str(getattr(monitor, "wake_instructions", "") or "").strip():
        return False
    slot_key = str(getattr(loop, "slot_key", "") or "").strip()
    return bool(
        slot_key
        and str(getattr(monitor, "kind", "") or "") == PATROL_WATCH
        and str(getattr(monitor, "target", "") or "") == slot_key
    )


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


async def ensure_patrol(state: Any, conductor_key: str) -> str:
    """Arm the default patrol on the conductor's slot when it holds no loop.

    Never raises: every failure is logged and answered as an outcome string,
    because the bind that called this has already committed.
    """
    try:
        svc, loop, slot = _loop_on(state, conductor_key)
        if svc is None or slot is None:
            logger.warning(
                "conductor patrol not armed for %s: %s",
                conductor_key,
                "auto-nudge is disabled" if svc is None else "session cannot host a loop",
            )
            return UNSUPPORTED
        # Any record is left alone -- active, approval-held, or stopped by a
        # person -- with one exception: OUR OWN default patrol that the system
        # stopped (its runtime budget or cycle cap ran out) is re-armed, because a
        # new bind means there is new work to patrol. ``_stopped_row_is_replaceable``
        # is the same allowlist the directive re-arm uses, so a person's stop of
        # the default is still retained as evidence.
        rearm = bool(
            loop is not None
            and getattr(loop, "default_patrol", False) is True
            and not getattr(loop, "active", False)
            and _stopped_row_is_replaceable(loop)
        )
        if loop is not None and not rearm:
            return EXISTING
        from kiro_crew.autonudge import is_channel_key
        from kiro_crew.autonudge_authz import authorize_and_add_nudge
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
            source="work-ledger-bind",
            caller="conductor-patrol",
            # Create-only: a loop that appeared since the read above wins, and
            # nothing is ever stacked beside it. Stopped-row displacement only for
            # the system-stopped default above.
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
        return REFUSED
    if error is not None:
        logger.warning(
            "conductor patrol arm refused for %s: %s [status %s]", conductor_key, error, status
        )
        return REFUSED
    logger.info("conductor patrol armed on %s", slot)
    return ARMED
