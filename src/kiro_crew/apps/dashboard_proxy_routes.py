"""``GET/PUT/DELETE /api/apps/{name}/dashboard-proxy``: the owner's ``dashboard_proxy`` grant.

Owner only, app tokens included: the path sits in the app's own namespace
(``token_auth_app_scope._app_owns_path``), so without the owner gate an app could grant
itself. A delegated (``via_app``) token is refused too: no ``READ_ALLOW`` row names this
path (``dashboard.delegated_scope``). ``apps.dashboard_proxy`` says what the grant is.

GET reads the grant and the words to show the owner. PUT grants it, and only with
``{"confirm": true}`` and only for an app the gateway recorded as installed from a registry.
DELETE revokes it. The grant reaches a backend at its next start, because the value is
bound into the backend's sandbox when it is spawned.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.apps import dashboard_proxy
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")

#: Refusals that are the install's state, not a fault of the gateway.
_REFUSED_REASONS = frozenset({"not-registry-verified", "not-enabled", "platform-unsupported"})


async def _owner_denial(request: web.Request, operation: str) -> web.Response | None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


def _view(name: str) -> dict[str, object]:
    return {
        "granted": dashboard_proxy.granted(name),
        "offer": dashboard_proxy.prompt_text(name),
        "platform_supported": dashboard_proxy.platform_supported(),
        "needs_access": dashboard_proxy.needs_access(name),
    }


async def handle_dashboard_proxy(request: web.Request) -> web.Response:
    """GET reads; PUT ``{"confirm": true}`` grants; DELETE revokes."""
    name = request.match_info["name"]
    method = request.method
    operation = {"GET": "read", "PUT": "grant", "DELETE": "revoke"}.get(method, "read")
    denied = await _owner_denial(request, f"apps.dashboard_proxy.{operation}")
    if denied is not None:
        return denied
    from kiro_crew.apps.manager import app_lifecycle_lock, get_app

    if not await asyncio.to_thread(get_app, name):
        return web.json_response(
            {"error": f"app {name!r} not installed", "code": "app_not_installed"}, status=404
        )
    if method == "GET":
        return web.json_response(await asyncio.to_thread(_view, name))
    caller = str(request.get("user", "") or "dashboard")
    async with app_lifecycle_lock(name):
        if method == "DELETE":
            await asyncio.to_thread(dashboard_proxy.revoke, name)
            sel().log_api_access(
                caller=caller,
                operation="apps.dashboard_proxy.revoke",
                outcome="success",
                resources=f"name={name!r}",
            )
            return web.json_response(await asyncio.to_thread(_view, name))
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
        if not isinstance(body, dict) or body.get("confirm") is not True:
            return web.json_response(
                {"error": "the owner must confirm the grant", "code": "confirm_required"},
                status=400,
            )
        ok, reason = await asyncio.to_thread(dashboard_proxy.grant, name)
    sel().log_api_access(
        caller=caller,
        operation="apps.dashboard_proxy.grant",
        outcome="success" if ok else "denied",
        resources=f"name={name!r}" + ("" if ok else f" reason={reason}"),
    )
    if not ok:
        status = 409 if reason in _REFUSED_REASONS else 500
        return web.json_response(
            {"error": "the grant was not recorded", "code": reason or "not_granted"}, status=status
        )
    view = await asyncio.to_thread(_view, name)
    return web.json_response({**view, "restart_required": True})


async def handle_dashboard_proxy_restart(request: web.Request) -> web.Response:
    """POST: restart a granted app's backend so it starts with its derived value.

    Owner only, and only for an app that holds the grant now: the value is bound into
    the sandbox at spawn, so a grant reaches a backend at its next start.
    """
    name = request.match_info["name"]
    denied = await _owner_denial(request, "apps.dashboard_proxy.restart")
    if denied is not None:
        return denied
    from kiro_crew.apps.backend import start_app_backend, stop_app_backend
    from kiro_crew.apps.manager import app_lifecycle_lock, get_app
    from kiro_crew.executors import subprocess_executor

    if not await asyncio.to_thread(get_app, name):
        return web.json_response(
            {"error": f"app {name!r} not installed", "code": "app_not_installed"}, status=404
        )
    if not await asyncio.to_thread(dashboard_proxy.granted, name):
        return web.json_response(
            {"error": "the app holds no dashboard grant", "code": "not_granted"}, status=409
        )
    loop = asyncio.get_running_loop()
    async with app_lifecycle_lock(name):
        await loop.run_in_executor(subprocess_executor(), stop_app_backend, name)
        backend = await loop.run_in_executor(subprocess_executor(), start_app_backend, name)
    sel().log_api_access(
        caller=str(request.get("user", "") or "dashboard"),
        operation="apps.dashboard_proxy.restart",
        outcome="success" if backend is not None else "failed",
        resources=f"name={name!r}",
    )
    return web.json_response(
        {**await asyncio.to_thread(_view, name), "restarted": backend is not None}
    )


__all__ = ["handle_dashboard_proxy", "handle_dashboard_proxy_restart"]
