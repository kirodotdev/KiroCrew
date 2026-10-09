from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import sel as _sel_mod
from kiro_crew.dashboard.handlers._shared import read_bounded_json

logger = logging.getLogger(__name__)

_APPLY_LOCK = asyncio.Lock()


def _session_key(request: web.Request) -> str:
    return request.headers.get("X-Session-Key") or ""


def _audit(request: web.Request, tool: str, outcome: str, resources: str = "") -> None:
    try:
        _sel_mod.sel().log_tool_invocation(
            session_key=_session_key(request),
            source="api",
            tool_name=tool,
            outcome=outcome,
            resources=resources,
        )
    except Exception:
        logger.debug("SEL audit failed for %s", tool, exc_info=True)


def _require_dashboard_user(request: web.Request, tool: str) -> web.Response | None:
    if request.get("app") != "":
        _audit(request, tool, "denied")
        return web.json_response({"code": "forbidden"}, status=403)
    return None


def _load_view(run_id: str | None) -> dict[str, Any] | None:
    from kiro_crew.personal_insights.insights_runs import RunRepository
    from kiro_crew.personal_insights.insights_view import run_view

    repo = RunRepository()
    try:
        return run_view(repo, run_id)
    finally:
        repo.close()


async def api_personal_insights_latest(request: web.Request) -> web.Response:
    refusal = _require_dashboard_user(request, "personal_insights.read")
    if refusal is not None:
        return refusal
    view = await asyncio.to_thread(_load_view, None)
    if view is None:
        return web.json_response({"code": "no_runs"}, status=404)
    return web.json_response(view)


async def api_personal_insights_run(request: web.Request) -> web.Response:
    refusal = _require_dashboard_user(request, "personal_insights.read")
    if refusal is not None:
        return refusal
    from kiro_crew.personal_insights.insights_view import RUN_ID_RE

    run_id = request.match_info.get("run_id", "")
    if not RUN_ID_RE.match(run_id):
        return web.json_response({"code": "invalid_run_id"}, status=400)
    view = await asyncio.to_thread(_load_view, run_id)
    if view is None:
        return web.json_response({"code": "not_found"}, status=404)
    return web.json_response(view)


def _apply(action_id: str, force: bool) -> dict[str, Any]:
    from kiro_crew.personal_insights.insights_apply import do_it
    from kiro_crew.personal_insights.insights_runs import RunRepository
    from kiro_crew.personal_insights.insights_view import action_summary

    repo = RunRepository()
    try:
        return action_summary(do_it(action_id, repo=repo, force=force))
    finally:
        repo.close()


def _undo(action_id: str) -> dict[str, Any]:
    from kiro_crew.personal_insights.insights_apply import undo
    from kiro_crew.personal_insights.insights_runs import RunRepository
    from kiro_crew.personal_insights.insights_view import action_summary

    repo = RunRepository()
    try:
        return action_summary(undo(action_id, repo=repo))
    finally:
        repo.close()


async def _mutate(request: web.Request, tool: str, success_state: str) -> web.Response:
    refusal = _require_dashboard_user(request, tool)
    if refusal is not None:
        return refusal
    from kiro_crew.personal_insights.insights_view import ACTION_ID_RE

    action_id = request.match_info.get("action_id", "")
    if not ACTION_ID_RE.match(action_id):
        return web.json_response({"code": "invalid_action_id"}, status=400)
    body: dict[str, Any] = {}
    parsed, bad = await read_bounded_json(request, allow_absent=True)
    if bad is not None:
        return bad
    if isinstance(parsed, dict):
        body = parsed
    force = body.get("force") is True
    async with _APPLY_LOCK:
        try:
            if tool == "personal_insights.do_it":
                result = await asyncio.to_thread(_apply, action_id, force)
            else:
                result = await asyncio.to_thread(_undo, action_id)
        except Exception as exc:
            logger.warning("%s failed for %s: %s", tool, action_id, exc)
            _audit(request, tool, "error", action_id)
            return web.json_response({"code": "apply_failed", "message": str(exc)}, status=500)
    outcome = "ok" if result.get("state") == success_state else "held"
    _audit(request, tool, outcome, action_id)
    return web.json_response(result, status=200 if outcome == "ok" else 409)


async def api_personal_insights_do_it(request: web.Request) -> web.Response:
    return await _mutate(request, "personal_insights.do_it", "applied_verified")


async def api_personal_insights_undo(request: web.Request) -> web.Response:
    return await _mutate(request, "personal_insights.undo", "undone")


def setup_personal_insights_routes(app: web.Application) -> None:
    app.router.add_get("/api/personal-insights/latest", api_personal_insights_latest)
    app.router.add_get("/api/personal-insights/runs/{run_id}", api_personal_insights_run)
    app.router.add_post(
        "/api/personal-insights/actions/{action_id}/do-it", api_personal_insights_do_it
    )
    app.router.add_post(
        "/api/personal-insights/actions/{action_id}/undo", api_personal_insights_undo
    )
