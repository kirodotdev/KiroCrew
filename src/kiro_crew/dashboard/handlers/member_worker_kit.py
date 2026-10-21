"""Owner-only worker-kit routes: ``/api/agents/{name}/worker-kit``.

``GET`` reads which template the crewmate's worker kit was last copied from;
``POST .../preview`` says what copying a template would change in the
crewmate's own file; ``PUT`` copies it, once, after that preview. All disk work
runs off the event loop through :mod:`kiro_crew.mate_worker_kit`.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.chat_utils import drained_to_thread
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.mate_worker_kit import WorkerKitError


async def api_member_worker_kit(request: web.Request) -> web.Response:
    from kiro_crew import mate_worker_kit

    operation = "member_worker_kit." + request.method.lower()
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    name = request.match_info["name"]
    try:
        if request.method == "GET":
            return web.json_response(await drained_to_thread(mate_worker_kit.view, name))
        try:
            body = await request.json()
        except ValueError:
            raise WorkerKitError("invalid_json", 400) from None
        if request.method == "POST":
            return web.json_response(await drained_to_thread(mate_worker_kit.preview, name, body))
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        async with _get_config_lock():
            result = await drained_to_thread(mate_worker_kit.apply, name, body)
        request.app["state"].push_refresh("agents")
        return web.json_response(result)
    except WorkerKitError as exc:
        return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
