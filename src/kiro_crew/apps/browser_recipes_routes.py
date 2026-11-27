"""The owner's route that turns off one watch recipe's run-by-itself yes.

``POST /api/browser/recipe-watch-off`` with ``{"app": <name>, "recipe": <grant entry>}``
    Removes that entry from the app's ``browserRecipeWatch`` approvals
    (``browser_recipes.withdraw_watch``): the recipe stays approved for the app
    page to run on demand, and the app stays on. Answers ``{"ok": true, "removed"}``.
    Turning it on again is the app page's approval (``/enable`` with that family).
    Refused with 403 for an app token, for an internal caller (an agent's MCP tools
    and kiro-cli), and for any session that is not the owner's dashboard. A running
    watch reads the approvals again on its next tick, so it stops clicking then.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The largest body the route reads: one grant entry (a recipe is a few hundred bytes).
MAX_BODY_BYTES = 8192


def _audit(request: web.Request, outcome: str, error: str = "") -> None:
    try:
        sel().log_api_access(
            caller=str(request.get("app") or request.get("user") or "unknown"),
            operation="app_browser_recipe_watch_off",
            outcome=outcome,
            source="app_routes",
            resources=request.path,
            error=error,
        )
    except Exception:  # noqa: BLE001 - the audit never changes the outcome
        logger.debug("SEL audit for a browser recipe watch-off failed", exc_info=True)


def _refused(code: str, error: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "code": code, "error": error}, status=status)


async def _owner_refusal(request: web.Request) -> web.Response | None:
    """A 403 unless the caller is the owner's own dashboard session."""
    if request.get("app") or request.get("internal_auth") is True:
        _audit(request, "denied", "not the owner's dashboard")
        return _refused("dashboard_session_required", "only the owner may change this", 403)
    from kiro_crew.dashboard.handlers._shared import (  # noqa: PLC0415
        require_owner_dashboard_request,
    )

    return await require_owner_dashboard_request(request, "app_browser_recipe_watch_off")


async def handle_watch_off(request: web.Request) -> web.Response:
    """``POST /api/browser/recipe-watch-off``: see the module docstring."""
    denied = await _owner_refusal(request)
    if denied is not None:
        return denied
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return _refused("body_too_large", "the request is too large", 413)
    try:
        payload = json.loads(await request.text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _refused("bad_request", "the body must be JSON", 400)
    if not isinstance(payload, dict):
        return _refused("bad_request", "the body must be a JSON object", 400)
    name, entry = payload.get("app"), payload.get("recipe")
    if not isinstance(name, str) or not name or not isinstance(entry, str) or not entry:
        return _refused("bad_request", "app and recipe (its grant entry) are required", 400)
    from kiro_crew.apps.browser_recipes import withdraw_watch  # noqa: PLC0415

    try:
        removed = await asyncio.to_thread(withdraw_watch, name, entry)
    except LookupError:
        return _refused("not_found", "app is not installed", 404)
    except (OSError, ValueError):
        logger.warning("could not turn off a browser recipe watch", exc_info=True)
        return _refused("unreadable", "this app's approvals could not be changed", 500)
    _audit(request, "granted", "removed" if removed else "not_held")
    return web.json_response({"ok": True, "removed": removed})


def register_browser_recipes_routes(app: web.Application) -> None:
    """Mount the route; called before the app route registry's catch-all."""
    # Outside ``/api/apps/{name}/``, so it reserves no segment an app's own routes use.
    app.router.add_post("/api/browser/recipe-watch-off", handle_watch_off)


__all__ = ["handle_watch_off", "register_browser_recipes_routes"]
