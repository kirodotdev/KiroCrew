"""HTTP routes for the milestones table (``kiro_crew.milestones``).

Same auth as ``/api/feature-videos/*``: the dashboard's token and CSRF
middleware guard every ``/api/*`` route, so nothing here adds or relaxes a gate.
A temporary or incognito session keeps no state, so the read answers
``{"enabled": false}`` and the writes are accepted and dropped. No route takes a
time or a count from the client: the server stamps ``now`` and the source.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew import milestones
from kiro_crew.dashboard.handlers._shared import _is_restricted_session, read_bounded_json

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

SCOPE = "capabilities.milestones"

_ACTIONS = ("starter_clicked", "skipped", "video_seen", "notice_shown", "panel_opened")
_SURFACES = ("empty_state", "panel")


def _policy_permits() -> bool:
    """Whether the capability policy allows milestones at all. In-memory."""
    return milestones._policy_permits()


def _enabled() -> bool:
    """Policy first, then config. Blocking (config load)."""
    from kiro_crew.config.loader import KiroCrewConfig

    if not _policy_permits():
        return False
    return bool(KiroCrewConfig.load().dashboard.milestones_enabled)


async def _write(op: milestones.Op) -> web.Response:
    """Persist *op*, or say it was not persisted."""
    if not _policy_permits():
        # A managed fleet pinned milestones off: no state write, no metric.
        return web.json_response(
            {"error": "milestones are turned off by policy", "code": "milestones_disabled"},
            status=403,
        )
    try:
        await asyncio.to_thread(milestones.apply_now, op)
    except Exception:
        logger.warning("milestones write failed", exc_info=True)
        return web.json_response(
            {"error": "could not save milestones", "code": "write_failed"}, status=500
        )
    return web.json_response({"ok": True})


def _bad(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


async def api_milestones_get(request: web.Request) -> web.Response:
    """GET /api/milestones -- the table, the top-bar numbers and due notices."""
    state: DashboardState = request.app["state"]
    if _is_restricted_session(state, request):
        return web.json_response({"enabled": False})
    if not await asyncio.to_thread(_enabled):
        return web.json_response({"enabled": False})
    await asyncio.to_thread(milestones.ensure_backfilled)
    return web.json_response(await asyncio.to_thread(milestones.snapshot))


async def _body(request: web.Request, allow_absent: bool = False):
    body, err = await read_bounded_json(request, allow_absent=allow_absent)
    if err is not None:
        return None, err
    return body or {}, None


async def api_milestones_event(request: web.Request) -> web.Response:
    """POST /api/milestones/event -- ``{"id", "action", "surface"?}``."""
    state: DashboardState = request.app["state"]
    body, err = await _body(request)
    if err is not None:
        return err
    milestone_id = body.get("id", "")
    action = body.get("action", "")
    surface = body.get("surface", "empty_state")
    if not isinstance(milestone_id, str) or not isinstance(action, str):
        return _bad("id and action must be strings", "invalid_field_type")
    if action not in _ACTIONS:
        return _bad("unknown action", "invalid_action")
    if surface not in _SURFACES:
        return _bad("unknown surface", "invalid_surface")
    if action in ("starter_clicked", "skipped", "video_seen") and not milestones.is_feature_id(
        milestone_id
    ):
        return _bad("not a feature milestone", "not_a_feature")
    if _is_restricted_session(state, request):
        return web.json_response({"ok": True})

    if action == "panel_opened":
        if _policy_permits():
            milestones.emit_panel_opened()
        return web.json_response({"ok": True})
    if action == "starter_clicked":
        return await _write(milestones.op_starter_clicked(milestone_id, surface))
    if action == "skipped":
        return await _write(milestones.op_skip(milestone_id, surface))
    if action == "notice_shown":
        # The op ignores a key that is not a reached milestone, so the file's key
        # set stays closed to what the server itself wrote.
        return await _write(milestones.op_notice_shown(milestone_id))
    # video_seen
    row = next(r for r in milestones.feature_rows() if r.id == milestone_id)
    if row.video_id:
        from kiro_crew.feature_videos import record_status

        await asyncio.to_thread(record_status, row.video_id, "seen")
    return web.json_response({"ok": True})


async def api_milestones_mark_done(request: web.Request) -> web.Response:
    """POST /api/milestones/mark-done -- ``{"id"}``; feature rows only."""
    state: DashboardState = request.app["state"]
    body, err = await _body(request)
    if err is not None:
        return err
    milestone_id = body.get("id", "")
    if not milestones.is_feature_id(milestone_id):
        return _bad("not a feature milestone", "not_a_feature")
    if _is_restricted_session(state, request):
        return web.json_response({"ok": True})
    return await _write(milestones.op_mark_done(milestone_id))


async def api_milestones_mark_all_done(request: web.Request) -> web.Response:
    """POST /api/milestones/mark-all-done -- every open or skipped feature row."""
    state: DashboardState = request.app["state"]
    _, err = await _body(request, allow_absent=True)
    if err is not None:
        return err
    if _is_restricted_session(state, request):
        return web.json_response({"ok": True})
    return await _write(milestones.op_mark_all_done())


async def api_milestones_skip_all(request: web.Request) -> web.Response:
    """POST /api/milestones/skip-all -- the empty state's "Don't show again"."""
    state: DashboardState = request.app["state"]
    _, err = await _body(request, allow_absent=True)
    if err is not None:
        return err
    if _is_restricted_session(state, request):
        return web.json_response({"ok": True})
    return await _write(milestones.op_skip_all())


def register(app: web.Application) -> None:
    app.router.add_get("/api/milestones", api_milestones_get)
    app.router.add_post("/api/milestones/event", api_milestones_event)
    app.router.add_post("/api/milestones/mark-done", api_milestones_mark_done)
    app.router.add_post("/api/milestones/mark-all-done", api_milestones_mark_all_done)
    app.router.add_post("/api/milestones/skip-all", api_milestones_skip_all)
