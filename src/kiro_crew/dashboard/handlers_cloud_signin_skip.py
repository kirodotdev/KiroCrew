"""``POST /api/cloud/launch/{id}/signin/skip``: go on to Connect without the sign-in.

Its own module so ``handlers_cloud`` stays under the file-size target; it uses that
module's guard, store and audit helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.cloud import launch_job as lj
from kiro_crew.dashboard.cloud_launch_controls import request_skip
from kiro_crew.dashboard.handlers_cloud import _astore, _audit, _guard, _in_executor

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState


async def api_cloud_launch_signin_skip(request: web.Request) -> web.Response:
    """POST /api/cloud/launch/{id}/signin/skip — go on to Connect without the sign-in.

    Sets the running launch's skip event; its worker stops the remote login,
    marks the step skipped and registers the crew. ``409 signin_skip_unavailable``
    once the worker leaves sign-in, before Connect starts. The worker closes
    acceptance atomically with requests, even when this route read an older job.
    """
    denied = _guard(request, "launch_signin_skip")
    if denied is not None:
        return denied
    state: "DashboardState" = request.app["state"]
    store = await _astore(state)
    job = await _in_executor(store.get, request.match_info["id"])
    if job is None:
        return web.json_response({"error": "not found", "code": "launch_job_not_found"}, status=404)
    connected = job.step(lj.STEP_CONNECT).state == lj.STEP_DONE
    if job.terminal or connected or not request_skip(state, job.id):
        _audit("launch_signin_skip", "denied", request_id=job.id, error="not skippable")
        return web.json_response(
            {"error": "this launch is not signing in", "code": "signin_skip_unavailable"},
            status=409,
        )
    _audit("launch_signin_skip", "success", request_id=job.id)
    return web.json_response(job.to_dict(), status=202)
