"""``/api/spawn/{agent_id}/resume`` and ``/api/spawn/lanes`` (RFC overload-resilience §6, §14.3).

The blocking ``spawn_sub_agents`` tool holds its result until the PARENT run
holds an execution slot again: its children finishing wakes the parent in the
store, but the slot comes back through admission's pump, by capacity. Between
the wake and the grant the parent must not be handed its tool result -- it
would run on without a slot, which is exactly the accounting the wait exists
to keep honest. The tool long-polls this endpoint; the answer is event-driven
(``admission.wait_resume_granted``), so a grant is seen at once and a quiet
wait costs one held request, not a poll interval.

``/api/spawn/lanes`` exposes the fairness dispatcher's per-lane view for the
Tasks/Health panel and ``kirocrew doctor``.
"""

from __future__ import annotations

import math

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState

#: Longest server-side hold per request. The MCP client's GET timeout is 10 s;
#: staying well under it keeps a held request from ever reading as a hang.
MAX_HOLD_SECS = 8.0


#: ``wait_secs`` that does not parse as a finite, non-negative number.
INVALID_WAIT_CODE = "invalid_wait_secs"


def _wait_secs(request: web.Request) -> float | None:
    """The clamped hold for this request, or ``None`` for a value that is not
    a finite, non-negative number (``nan``, ``inf``, ``1e400``, ``-1``,
    ``abc``) -- request input never reaches a comparison as NaN or infinity."""
    raw = request.query.get("wait_secs", "0")
    try:
        secs = float(str(raw))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(secs) or secs < 0:
        return None
    return min(MAX_HOLD_SECS, secs)


async def api_spawn_resume(request: web.Request) -> web.Response:
    """GET /api/spawn/{agent_id}/resume?wait_secs=N -- is the run's slot granted?

    ``known`` is False for an id this gateway has no live run for (a chat-turn
    parent, a finished run, another incarnation): there is nothing to hold
    for, and ``granted`` is True so a holder proceeds. ``wait_secs`` (0..8)
    holds the response until the grant or the bound, whichever is first.
    """
    state: DashboardState = request.app["state"]
    manager = state.subagents
    if not manager:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"}, status=503
        )
    agent_id = request.match_info["agent_id"]
    info = manager._agents.get(agent_id)
    if info is None or info.done:
        return web.json_response(
            {"id": agent_id, "known": False, "granted": True, "slot_released": False}
        )
    wait = _wait_secs(request)
    if wait is None:
        return web.json_response(
            {
                "error": f"wait_secs must be a finite number in 0..{MAX_HOLD_SECS:g}",
                "code": INVALID_WAIT_CODE,
            },
            status=400,
        )
    admission = manager._admission
    granted = admission.resume_granted(agent_id)
    if not granted and wait > 0:
        granted = await admission.wait_resume_granted(agent_id, timeout=wait)
    live = manager._agents.get(agent_id)
    return web.json_response(
        {
            "id": agent_id,
            "known": True,
            "granted": bool(granted),
            "slot_released": bool(live is not None and live._slot_released),
            "resume_pending": bool(live is not None and live._resume_pending),
            "done": bool(live is None or live.done),
        }
    )


async def api_spawn_lanes(request: web.Request) -> web.Response:
    """GET /api/spawn/lanes -- per-lane depth, running/waiting counts and the cap view."""
    state: DashboardState = request.app["state"]
    manager = state.subagents
    if not manager:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"}, status=503
        )
    return web.json_response(await manager._admission.lane_snapshot_async())


def setup_spawn_resume_routes(app: web.Application) -> None:
    """Register the resume-hold and lanes routes (under the ``/api/spawn`` prefix)."""
    # ``lanes`` is a literal segment; the caller registers these BEFORE the
    # ``/api/spawn/{agent_id}`` route so it is not read as a run id.
    app.router.add_get("/api/spawn/lanes", api_spawn_lanes)
    app.router.add_get("/api/spawn/{agent_id}/resume", api_spawn_resume)
