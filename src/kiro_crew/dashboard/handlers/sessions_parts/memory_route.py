"""``GET /api/sessions/memory``: the memory footprint of each session and task.

Holds the process's one :class:`SessionMemorySampler`, which carries the CPU
jiffy baseline and the rolling load window between requests.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes
from kiro_crew.dashboard.session_memory import SessionMemorySampler
from kiro_crew.dashboard.state import DashboardState

# One sampler per process: it carries the CPU jiffy baseline and the rolling load
# window, both of which are meaningless if rebuilt per request (a fresh baseline
# always reports CPU as unknown, and a fresh window is always empty).
_memory_sampler = SessionMemorySampler()


async def api_sessions_memory(request: web.Request) -> web.Response:
    """GET /api/sessions/memory — per-session and per-task memory footprint."""
    state: DashboardState = request.app["state"]
    # Built on the loop, not in the sampling thread: it walks live slot objects,
    # and it is pure dict work. `hasattr` because a stub state in the suite may not
    # carry the method at all; validating the VALUE is `_spend_for_session`'s job,
    # so it is not repeated here — one owner for that rule.
    aliases = state.spend_slot_by_session() if hasattr(state, "spend_slot_by_session") else None
    payload = await _memory_sampler.sample(
        state.sessions,
        getattr(state, "subagents", None),
        get_slot=state.get_slot,
        spend_slot_by_session=aliases,
        mcp_gateway=getattr(state, "_mcp_gateway_manager", None),
        breakdown=True,
    )
    return web.json_response(payload)


guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset(),
)
