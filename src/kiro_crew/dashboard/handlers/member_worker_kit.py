"""Owner-only worker-kit routes: ``/api/agents/{name}/worker-kit``.

``GET`` reads the crewmate's own file revision, or answers 503
``capabilities_unavailable`` when that read has not finished in time;
``POST .../preview`` says what adding another agent's abilities would add to
that file, and what it skips as already present; ``PUT`` adds them, once,
after that preview. All disk work runs off the event loop through
:mod:`kiro_crew.mate_worker_kit`.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.agent_name_segment import agent_route_name, invalid_agent_name_response
from kiro_crew.dashboard.chat_utils import drained_to_thread
from kiro_crew.dashboard.handlers._bounded_read import TIMED_OUT, bounded_read
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.mate_worker_kit import WorkerKitError


async def api_member_worker_kit(request: web.Request) -> web.Response:
    from kiro_crew import mate_worker_kit

    operation = "member_worker_kit." + request.method.lower()
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    name = agent_route_name(request)
    if name is None:
        return invalid_agent_name_response()
    try:
        if request.method == "GET":
            # Not ``drained_to_thread``: a GET mutates nothing, so a read still
            # blocked at the deadline is left to its thread and answered 503.
            try:
                view = await bounded_read(mate_worker_kit.view, name, what="Worker-kit")
            except TimeoutError:
                return web.json_response(
                    {
                        "error": "capabilities_unavailable",
                        "code": "capabilities_unavailable",
                        "detail": "",
                        "reason": TIMED_OUT,
                    },
                    status=503,
                )
            return web.json_response(view)
        try:
            body = await request.json()
        except ValueError:
            raise WorkerKitError("invalid_json", 400) from None
        if request.method == "POST":
            return web.json_response(await drained_to_thread(mate_worker_kit.preview, name, body))
        from kiro_crew.dashboard.handlers.agent_capabilities import (
            routed_entries,
            save_and_refresh_overlays,
        )

        policy_changed = False
        routed: dict = {}

        def _add() -> tuple[dict, dict]:
            nonlocal policy_changed, routed
            view, policy_changed, added = mate_worker_kit.apply_reporting(name, body)
            routed = routed_entries(added)
            return view, routed

        # Under the overlay refresh, so a gateway-routed server the add copies
        # reaches the Bot's overlay, or the answer says a restart is needed.
        result = await save_and_refresh_overlays(_add)
        request.app["state"].push_refresh("agents")
        if policy_changed or routed:
            # A live worker keeps the KAS permission batch it registered and the
            # servers it mounted, so it is reloaded onto the file, as the
            # capabilities save reloads it, after a new restriction or a copied
            # gateway-routed server (``crewmate_worker_reload``; a busy one at its
            # turn end).
            from kiro_crew.dashboard.crewmate_worker_reload import schedule_reload_workers

            schedule_reload_workers(request.app["state"], name)
        return web.json_response(result)
    except WorkerKitError as exc:
        # ``detail`` names the field or rule a refusal is about ("" when none).
        return web.json_response(
            {"error": exc.code, "code": exc.code, "detail": exc.detail}, status=exc.status
        )
