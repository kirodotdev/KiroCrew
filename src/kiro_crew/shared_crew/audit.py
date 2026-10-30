"""Audit consequential shared actions into the security event log.

An :class:`~kiro_crew.shared_crew.contracts.ActionRecord` is written to the
existing tamper-evident security event log (``kiro_crew.sel``) as one
``shared_crew_action`` event. That log already owns integrity (an HMAC chain),
rotation and age-based retention, and is read through its existing access path;
this module adds no second store.

What a record carries: the action type and id, the session key, the initiator
and responder as a reduced principal (id, kind, provenance and login, never an
issuer subject beyond that or a display name), the target id, the outcome, the
correlation id and the causal link. Two references ride in the record's
``extra`` and the event metadata:

* ``workload_identity_ref``: always ``unavailable``, since this build has no
  Crew workload identity, so every record names the human and the Crew
  workload separately;
* ``trigger``: for work no person started (a schedule, a webhook, the Crew), the
  trigger behind its session (:mod:`kiro_crew.shared_crew.trigger`). It is never
  set for a human initiator and never stands in for one.

A ``memory_change`` record adds a third, ``memory_change``: the category and the
clipped, redacted one-line summaries of the lessons replaced and the lesson that
replaced them (:mod:`kiro_crew.shared_crew.memory_changes`).

It never carries prompt text, message bodies, tool input, tokens or cookies,
because :class:`ActionRecord` has no field for them.

Causal join: ``action_id``, ``causal_action_id``, ``session_key``, ``target``
(the work id for work actions) and the initiator id join a record to the
accepted work and session it acted on. The actor side of that join is the
record's own initiator, kept apart from the work's owner (``budget_key``): a
collaborator may cancel work someone else started, and the record names who
acted while the work names who started it.

Write-failure rule: :func:`record_action` writes synchronously off the event
loop with the log's fail-closed ``critical`` mode and returns whether the record
landed. The action itself still applies when it did not, and the caller says so
in its response (``audited: false``); a refused audit must not wedge a turn and
must not be reported as recorded.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from kiro_crew.shared_crew.contracts import (
    ActionOutcome,
    ActionRecord,
    ActionType,
    ActorContext,
    Principal,
    PrincipalKind,
)
from kiro_crew.shared_crew.crew_mode import WORKLOAD_UNAVAILABLE
from kiro_crew.shared_crew.trigger import trigger_of

logger = logging.getLogger(__name__)

EVENT_TYPE = "shared_crew_action"
SOURCE = "shared_crew"
#: How long a caller waits for the audit write before treating it as not
#: recorded. The write may still land afterwards; the caller reports what it
#: could confirm.
AUDIT_TIMEOUT_SECONDS = 5.0
_ID_MAX = 256


def _clip(value: str) -> str:
    return value[:_ID_MAX]


def audit_principal(principal: Principal | None) -> dict[str, Any] | None:
    """The reduced principal an audit record stores."""
    if principal is None:
        return None
    return {
        "id": principal.id,
        "kind": principal.kind.value,
        "provenance": principal.provenance.value,
        "login": principal.display.login,
    }


#: ``extra`` keys an action record carries beside the frozen fields.
EXTRA_WORKLOAD_REF = "workload_identity_ref"
EXTRA_TRIGGER = "trigger"
EXTRA_MEMORY_CHANGE = "memory_change"


def _unattended_trigger(initiator: Principal, session_key: str) -> dict[str, Any] | None:
    if initiator.kind is PrincipalKind.HUMAN:
        return None
    found = trigger_of(key=session_key)
    if found is None:
        return None
    body = found.to_public_dict()
    return {"kind": body["kind"], "ref": body["ref"], "name": body["name"]}


def new_action_record(
    action_type: ActionType,
    *,
    session_key: str,
    initiator: ActorContext,
    outcome: ActionOutcome,
    target: str = "",
    responder: ActorContext | None = None,
    causal_action_id: str = "",
    extra: dict[str, Any] | None = None,
) -> ActionRecord:
    """Build a record for an action *initiator* asked for.

    *extra* adds action-specific keys (:data:`EXTRA_MEMORY_CHANGE`); it cannot
    replace the workload reference or the trigger.
    """
    extra = {**(extra or {})}
    extra[EXTRA_WORKLOAD_REF] = WORKLOAD_UNAVAILABLE
    extra.pop(EXTRA_TRIGGER, None)
    trigger = _unattended_trigger(initiator.principal, session_key)
    if trigger is not None:
        extra[EXTRA_TRIGGER] = trigger
    return ActionRecord(
        action_id=uuid.uuid4().hex,
        action_type=action_type,
        session_key=_clip(session_key),
        initiator=initiator.principal,
        at=time.time(),
        outcome=outcome,
        responder=None if responder is None else responder.principal,
        causal_action_id=_clip(causal_action_id),
        correlation_id=_clip(initiator.correlation_id),
        target=_clip(target),
        extra=extra,
    )


def security_event(record: ActionRecord) -> Any:
    """The security event *record* is written as."""
    from kiro_crew.sel import SecurityEvent

    return SecurityEvent(
        event_id=record.action_id[:16],
        timestamp=datetime.fromtimestamp(record.at, tz=timezone.utc).isoformat(),
        event_type=EVENT_TYPE,
        caller_identity=record.initiator.id,
        agent="",
        source=SOURCE,
        operation=f"shared_crew.{record.action_type.value}",
        outcome=record.outcome.value,
        resources=record.target or record.session_key,
        request_id=record.correlation_id,
        metadata={
            "schema_version": 1,
            "action_id": record.action_id,
            "action_type": record.action_type.value,
            "session_key": record.session_key,
            "turn_id": record.turn_id,
            "initiator": audit_principal(record.initiator),
            "responder": audit_principal(record.responder),
            "runtime_ref": record.runtime_ref,
            "causal_action_id": record.causal_action_id,
            "target": record.target,
            EXTRA_WORKLOAD_REF: str(record.extra.get(EXTRA_WORKLOAD_REF) or WORKLOAD_UNAVAILABLE),
            EXTRA_TRIGGER: record.extra.get(EXTRA_TRIGGER),
            **(
                {EXTRA_MEMORY_CHANGE: record.extra[EXTRA_MEMORY_CHANGE]}
                if EXTRA_MEMORY_CHANGE in record.extra
                else {}
            ),
        },
    )


def _default_writer(event: Any) -> None:
    from kiro_crew.sel import sel

    sel().log(event, critical=True)


async def record_action(
    record: ActionRecord,
    *,
    writer: Callable[[Any], None] | None = None,
    timeout: float = AUDIT_TIMEOUT_SECONDS,
) -> bool:
    """Write *record* and return whether it is confirmed recorded.

    Never raises. ``False`` means the caller must not claim the action was
    audited.
    """
    write = writer or _default_writer
    try:
        event = security_event(record)
        await asyncio.wait_for(asyncio.to_thread(write, event), timeout=timeout)
    except Exception as exc:
        logger.warning(
            "shared crew audit write failed for %s %s: %s",
            record.action_type.value,
            record.action_id,
            type(exc).__name__,
        )
        return False
    return True


_pending_audits: set[asyncio.Task[bool]] = set()


def schedule_action_audit(
    action_type: ActionType,
    *,
    session_key: str,
    initiator: ActorContext,
    outcome: ActionOutcome,
    target: str = "",
    responder: ActorContext | None = None,
    causal_action_id: str = "",
    extra: dict[str, Any] | None = None,
) -> ActionRecord | None:
    """Record an action without making its caller wait for the write.

    For an action whose own durable row already names the actor (a message row
    carries its author), so the audit is a second record and the response
    makes no audit claim. A failed write is logged by :func:`record_action`.
    Returns the record, or ``None`` when no event loop is running.
    """
    record = new_action_record(
        action_type,
        session_key=session_key,
        initiator=initiator,
        outcome=outcome,
        target=target,
        responder=responder,
        causal_action_id=causal_action_id,
        extra=extra,
    )
    try:
        task = asyncio.get_running_loop().create_task(record_action(record))
    except RuntimeError:
        return None
    _pending_audits.add(task)
    task.add_done_callback(_pending_audits.discard)
    return record
