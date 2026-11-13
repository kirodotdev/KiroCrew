"""``/api/shared-crew/capacity``, ``/api/shared-crew/work`` and work cancellation.

Read and write access follows the existing dashboard auth: these handlers run
behind ``token_auth_middleware`` like every other ``/api`` route and add no
authorization of their own, because every authorized collaborator has equal
rights. The exceptions: cancelling a planned task or a schedule run keeps the
owner gate its own cancel route has, and an app token may neither cancel work nor
list another session's work (403 ``app_token_not_allowed``). Cancellation is compare-and-set on the record's ``revision``; a stale
or illegal request answers 409 with the server's ``current`` record.

Shapes:

``GET /api/shared-crew/capacity``
    ``CapacityResponse.to_dict()`` for the viewer's principal, plus
    ``effective`` (every enforced limit with ``auto`` resolved), ``containment``
    (``ContainmentReport.to_dict()``) and ``fairness`` (algorithm, quantum and
    the starvation bound).
``GET /api/shared-crew/work?session_key=<key>``
    ``{"items": [AcceptedWork.to_public_dict(), ...]}`` for that session, or for
    the viewer's own work when ``session_key`` is absent. The key may be the
    session key or the dashboard tab's slot key (what the chat holds): a slot
    key is read as the session its turns run on.
``POST /api/shared-crew/work/{work_id}/cancel`` ``{"expected_revision": int}``
    200 with the next public record; 404 ``not_found``; 409 ``stale_revision``
    or ``invalid_transition`` with ``current``.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew.shared_crew import admission_gate
from kiro_crew.shared_crew.admission import (
    InvalidTransition,
    OperationKind,
    ReasonCode,
    StaleRevision,
    error_body,
    http_status,
)
from kiro_crew.shared_crew.admission_gate import coded_json_response
from kiro_crew.shared_crew.contracts import ActionOutcome, ActionType
from kiro_crew.shared_crew.scheduler import DEFAULT_QUANTUM

logger = logging.getLogger(__name__)

CAPACITY_PATH = "/api/shared-crew/capacity"
WORK_PATH = "/api/shared-crew/work"
CANCEL_PATH = "/api/shared-crew/work/{work_id}/cancel"
_OWNER_ONLY_CANCEL = frozenset({OperationKind.TASK, OperationKind.CRON})

FAIRNESS_DESCRIPTION: dict[str, Any] = {
    "algorithm": "deficit_round_robin",
    "quantum": DEFAULT_QUANTUM,
    "max_weight": 1,
    "within_session": "fifo",
    "starvation_bound": (
        "a continuously eligible principal is admitted within (P - 1) * (quantum + "
        "max_weight - 1) admissions of others, P = principals with eligible queued work"
    ),
}


async def api_capacity(request: web.Request) -> web.Response:
    service = await admission_gate.current_service()
    # Durable work a restart left is reconciled before capacity is read, so a
    # runtime still alive is counted rather than reported as free.
    await service.ensure_ready()
    budget = admission_gate.actor_budget(request)
    body = service.capacity(budget).to_dict()
    body["effective"] = service.limits.to_dict()
    report = await service.containment_report() if service.enabled else None
    body["containment"] = None if report is None else report.to_dict()
    body["fairness"] = FAIRNESS_DESCRIPTION
    return web.json_response(body)


def work_session_keys(state: Any, key: str) -> list[str]:
    """The stored session keys a work-list *key* names: itself, plus a slot's session.

    Chat work is keyed by the session its turns run on (``dashboard:<slot>``,
    or a channel key for a channel-born tab) while the chat addresses the tab
    by its slot key. Reads in-memory state only.
    """
    from kiro_crew.dashboard.chat_utils import _history_key_for, effective_session_key

    keys = [key]
    slots = getattr(state, "_slots", None)
    slot = slots.get(key) if isinstance(slots, dict) else None
    if slot is not None:
        keys.append(effective_session_key(slot))
    elif ":" not in key:
        keys.append(_history_key_for(key))
    return list(dict.fromkeys(k for k in keys if k))


def _app_refusal(request: web.Request) -> web.Response | None:
    """An app token may not read or cancel other sessions' work.

    The ownership checks that guard ``/stop`` and ``/interrupt`` are not on these
    routes, so an app caller gets only its own work list and no cancellation.
    """
    if not request.get("app"):
        return None
    return web.json_response(
        {"error": "apps cannot use this route", "code": "app_token_not_allowed"}, status=403
    )


async def api_work(request: web.Request) -> web.Response:
    service = await admission_gate.current_service()
    session_key = request.query.get("session_key") or None
    if session_key is not None and (denied := _app_refusal(request)) is not None:
        return denied
    if session_key is not None and len(session_key) > 512:
        return web.json_response(
            {"error": "session_key too long", "code": "invalid_filter"}, status=400
        )
    if session_key is None:
        items = await service.list_work(budget_key=admission_gate.actor_budget(request))
    else:
        found: dict[str, Any] = {}
        for key in work_session_keys(request.app.get("state"), session_key):
            for work in await service.list_work(session_key=key):
                found.setdefault(work.work_id, work)
        items = sorted(found.values(), key=lambda w: (w.accepted_at, w.work_id))
    return web.json_response({"items": [w.to_public_dict() for w in items]})


async def api_work_cancel(request: web.Request) -> web.Response:
    if (denied := _app_refusal(request)) is not None:
        return denied
    service = await admission_gate.current_service()
    work_id = request.match_info.get("work_id", "")
    try:
        body = await request.json()
    except Exception:
        body = None
    expected = body.get("expected_revision") if isinstance(body, dict) else None
    if not isinstance(expected, int) or isinstance(expected, bool):
        return web.json_response(
            {"error": "expected_revision must be an integer", "code": "invalid_request"},
            status=400,
        )
    target = await service.current_work(work_id)
    if target is not None and target.operation in _OWNER_ONLY_CANCEL:
        from kiro_crew.dashboard.handlers._shared import (
            require_owner_for_dashboard_caller,
        )

        # Planned tasks and schedules are the owner's: their own cancel routes refuse others.
        denied = await require_owner_for_dashboard_caller(request, "shared_crew.work_cancel")
        if denied is not None:
            return denied
    try:
        work = await service.cancel(work_id, expected_revision=expected)
    except KeyError:
        reason = ReasonCode.NOT_FOUND
        return coded_json_response(error_body(reason, "no such work"), status=http_status(reason))
    except (StaleRevision, InvalidTransition) as exc:
        reason = (
            ReasonCode.STALE_REVISION
            if isinstance(exc, StaleRevision)
            else ReasonCode.INVALID_TRANSITION
        )
        current = await service.current_work(work_id)
        _audit_cancel(request, work_id, current, ActionOutcome.CONFLICT)
        return coded_json_response(
            error_body(reason, str(exc), current=current), status=http_status(reason)
        )
    _audit_cancel(request, work_id, work, ActionOutcome.APPLIED)
    return web.json_response(work.to_public_dict())


def _audit_cancel(request: web.Request, work_id: str, work: Any, outcome: ActionOutcome) -> None:
    """Record a cancellation attempt on the Shared Crew audit trail. Never raises."""
    try:
        from kiro_crew.shared_crew.audit import schedule_action_audit
        from kiro_crew.shared_crew.identity import actor_of

        schedule_action_audit(
            ActionType.CANCEL,
            session_key=str(getattr(work, "session_key", "") or ""),
            initiator=actor_of(request),
            outcome=outcome,
            target=work_id,
        )
    except Exception:
        logger.debug("shared crew cancel audit failed", exc_info=True)


def register(app: web.Application) -> None:
    """Register the Shared Crew admission routes on *app* (wired by integration)."""
    app.router.add_get(CAPACITY_PATH, api_capacity)
    app.router.add_get(WORK_PATH, api_work)
    app.router.add_post(CANCEL_PATH, api_work_cancel)


__all__ = ["CANCEL_PATH", "CAPACITY_PATH", "WORK_PATH", "register", "work_session_keys"]
