"""The owner's routes for the pages an app's agent may browse (``apps/browser_sites.py``).

``GET /api/apps/{name}/browser-sites``
    Every listed site as ``{site, source, status}`` and the ones in force now
    (``active``). ``status`` is ``approved``, ``pending``, ``disabled`` or ``removed``
    (a declared site the owner removed, which only ``add`` acts on). Read-only, so
    the app's own token (its backend), the owner's dashboard and an internal caller
    may all read it: an app's agent learns from it where it may look.

``POST /api/apps/{name}/browser-sites`` with ``{"action": "add"|"remove"|"disable", "site": ...}``
    The owner's edit. Refused with 403 for an app token, for an internal caller (an
    agent's MCP tools and kiro-cli), and for any session that is not the owner's
    dashboard. ``add`` widens what the app's agent may open,
    so the dashboard asks the owner in its own dialog before it sends one; that
    dialog is drawn in the page an app's UI also runs in, so it guards against an
    app's page by convention only. An app's backend can mint an owner token through
    the local owner mint and reach this route without the dialog, as it can reach
    ``/enable`` (``docs/architecture/app-platform-trust-model.md``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps import browser_sites
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The largest body the edit route reads: one action and one site entry.
MAX_BODY_BYTES = 4096

_STATUS_FOR_CODE = {"not_found": 404, "not_listed": 404, "too_many": 409}


def _audit(request: web.Request, outcome: str, error: str = "") -> None:
    try:
        sel().log_api_access(
            caller=str(request.get("app") or request.get("user") or "unknown"),
            operation="app_browser_sites_change",
            outcome=outcome,
            source="app_routes",
            resources=request.path,
            error=error,
        )
    except Exception:  # noqa: BLE001 - the audit never changes the outcome
        logger.debug("SEL audit for an app's browser sites failed", exc_info=True)


def _refused(code: str, error: str, status: int) -> web.Response:
    return web.json_response({"code": code, "error": error}, status=status)


def _list_body(name: str) -> dict[str, Any] | None:
    rows = browser_sites.app_site_rows(name)
    if rows is None:
        return None
    return {"sites": rows, "active": list(browser_sites.app_browser_sites(name))}


async def handle_list_sites(request: web.Request) -> web.Response:
    """``GET /api/apps/{name}/browser-sites``: the list and the sites in force."""
    name = request.match_info["name"]
    try:
        body = await asyncio.to_thread(_list_body, name)
    except (OSError, ValueError):
        logger.warning("could not read an app's browser sites", exc_info=True)
        return _refused("unreadable", "this app's site list could not be read", 500)
    if body is None:
        return _refused("not_found", "app is not installed", 404)
    return web.json_response(body)


async def _owner_refusal(request: web.Request) -> web.Response | None:
    """A 403 unless the caller is the owner's own dashboard session."""
    if request.get("app"):
        _audit(request, "denied", "app token")
        return _refused("app_token_forbidden", "only the owner may change an app's sites", 403)
    if request.get("internal_auth") is True:
        _audit(request, "denied", "internal caller")
        return _refused(
            "dashboard_session_required", "only the owner may change an app's sites", 403
        )
    from kiro_crew.dashboard.handlers._shared import (  # noqa: PLC0415
        require_owner_dashboard_request,
    )

    return await require_owner_dashboard_request(request, "app_browser_sites_change")


async def handle_change_site(request: web.Request) -> web.Response:
    """``POST /api/apps/{name}/browser-sites``: the owner adds, removes or turns off a site."""
    denied = await _owner_refusal(request)
    if denied is not None:
        return denied
    name = request.match_info["name"]
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return _refused("body_too_large", "the request is too large", 413)
    try:
        payload = json.loads(await request.text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _refused("bad_request", "the body must be JSON", 400)
    if not isinstance(payload, dict):
        return _refused("bad_request", "the body must be a JSON object", 400)
    action, site = payload.get("action"), payload.get("site")
    if not isinstance(action, str) or not isinstance(site, str):
        return _refused("bad_request", "action and site must be strings", 400)
    try:
        rows = await asyncio.to_thread(browser_sites.site_change, name, action, site)
        active = await asyncio.to_thread(browser_sites.app_browser_sites, name)
    except browser_sites.SiteChangeError as exc:
        _audit(request, "denied", exc.code)
        return _refused(exc.code, str(exc), _STATUS_FOR_CODE.get(exc.code, 400))
    except (OSError, ValueError):
        logger.warning("could not change an app's browser sites", exc_info=True)
        return _refused("unreadable", "this app's site list could not be changed", 500)
    _audit(request, "granted", action)
    return web.json_response({"sites": rows, "active": list(active)})


def register_browser_sites_routes(app: web.Application) -> None:
    """Mount both routes; called before the app route registry's catch-all."""
    app.router.add_get("/api/apps/{name}/browser-sites", handle_list_sites)
    app.router.add_post("/api/apps/{name}/browser-sites", handle_change_site)


__all__ = ["handle_change_site", "handle_list_sites", "register_browser_sites_routes"]
