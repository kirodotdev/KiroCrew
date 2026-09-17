"""HTTP routes for the Plane C wake queue and inspect-by-handle autonudge read.

These are the sidecar side of the Phase 4 bridge's ``monitor/wake`` (long-poll),
``wake/ack``, and ``monitor/inspect`` methods. They are reachable only to a
supervised internal-secret caller carrying ``X-Session-Key: kiro-cli:<id>`` (the
admission set gates that); the owner of every wake IS that session key, so these
handlers never read a body-supplied owner — the queue is scoped to the
authenticated caller and cannot reach another session's wakes.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.autonudge import is_structured_monitor_loop
from kiro_crew.crew_wakes import MAX_LONG_POLL_SECS
from kiro_crew.dashboard.handlers.autonudge import (
    _autonudge_get,
    _serialize_for_legacy_reader,
)
from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def _owner_or_denied(request: web.Request) -> tuple[str, web.Response | None]:
    """Resolve the wake owner from the authenticated internal caller.

    A wake owner is the supervising session key, never a body field. The route
    is admitted only for an internal-secret caller in supervised mode, but this
    handler still refuses a request that reached it without the internal grant
    or without a session key: a queue keyed by a caller-controlled value with no
    proven identity would let any admitted caller drain any owner's wakes.
    """
    if request.get("internal_auth") is not True:
        return "", web.json_response(
            {"error": "internal caller required", "code": "internal_required"}, status=403
        )
    owner = request.headers.get("X-Session-Key", "").strip()
    if not owner:
        return "", web.json_response(
            {"error": "X-Session-Key required", "code": "session_required"}, status=400
        )
    return owner, None


async def api_crew_wakes_poll(request: web.Request) -> web.Response:
    """GET /api/crew/wakes — long-poll this owner's queue.

    Query ``wait`` (seconds, clamped to ``[0, MAX_LONG_POLL_SECS]``) is how long
    to hold the request open when the queue is empty; it returns early the moment
    a wake is enqueued. The response lists every live (un-acked, un-expired) wake
    in FIFO order. Delivery does not remove a wake — the caller acks each once its
    turn is submitted, so a crash between poll and submit redelivers it.
    """
    owner, denied = _owner_or_denied(request)
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    raw_wait = request.query.get("wait", "25")
    try:
        wait = float(raw_wait)
    except (TypeError, ValueError):
        return web.json_response(
            {"error": "wait must be a number", "code": "invalid_wait"}, status=400
        )
    wq = state.wake_queue()
    wakes = await wq.long_poll(owner, wait)
    return web.json_response(
        {
            "wakes": [w.to_dict() for w in wakes],
            "maxWait": MAX_LONG_POLL_SECS,
        }
    )


async def api_crew_wakes_ack(request: web.Request) -> web.Response:
    """POST /api/crew/wakes/{id}/ack — remove a delivered wake.

    Body: ``{outcome: 'submitted' | 'dropped'}``. ``submitted`` = the CLI ran it
    as a turn; ``dropped`` = the CLI declined it (its session is gone). Both
    remove the wake so it is not redelivered; a double-ack (unknown id) answers
    ``{acked: false}`` rather than an error so the CLI's retry is idempotent.
    """
    owner, denied = _owner_or_denied(request)
    if denied is not None:
        return denied
    wake_id = request.match_info["wake_id"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any parse failure on an untrusted body is a 400
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    outcome = body.get("outcome")
    if outcome not in ("submitted", "dropped"):
        return web.json_response(
            {"error": "outcome must be 'submitted' or 'dropped'", "code": "invalid_outcome"},
            status=400,
        )
    state: DashboardState = request.app["state"]
    wq = state.wake_queue()
    acked = await wq.ack(owner, wake_id, outcome)
    return web.json_response({"acked": acked})


async def api_autonudge_get_by_id(request: web.Request) -> web.Response:
    """GET /api/autonudge/{loop_id} — inspect one loop by its handle.

    ``monitor/inspect`` names a loop by the handle the CLI holds (the loop id
    ``monitor/start`` returned), not by slot key, so ``slot/{slot_key}`` cannot
    serve it — the CLI does not know the slot. Returns the same
    entitlement-scoped reduction the list route publishes (presence, cadence,
    liveness, state; never the watched subject), and ``null`` when no loop
    carries that id. A structured monitor is included as an armed row so a reader
    cannot mistake a running monitor for nothing armed.
    """
    svc = _autonudge_get()
    loop_id = request.match_info["loop_id"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_id(loop_id)
    return web.json_response(
        {
            "enabled": True,
            "loop": _serialize_for_legacy_reader(loop) if loop is not None else None,
            "isMonitor": bool(loop is not None and is_structured_monitor_loop(loop)),
        }
    )
