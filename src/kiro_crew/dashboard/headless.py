"""Headless mode: the gateway serves its HTTP API and no dashboard.

``kirocrew gateway --headless`` runs the whole gateway for clients that bring their
own interface. After app and relay web-origin dispatch, the headless middleware
refuses the dashboard shell, its PWA entries and unmatched shell navigations before
page fallback or sign-in redirect. Shared public build directories (``/assets/``,
``/fonts/``, ``/vendor/``) stay served; other listed resources stay refused because
they require authentication or can serve files outside the frontend build.
Every other registered route keeps its own authentication and handler, including
API, local authorization, app routes and app-window pages. Token auth and the LTS
gate have no dashboard shell callback. Authentication refusals return JSON instead
of a dashboard sign-in page, with their status and authentication checks unchanged.

Known limit: the embed pages (``/embed/chat/*``, ``/embed/sessions``,
``/embed/settings``) are routes inside the dashboard page, not routes the gateway
registers. A headless gateway answers them 404 ``headless``.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew.dashboard.token_auth_paths import _is_spa_shell_request

HEADLESS_KEY = web.AppKey("headless", bool)
_LOOPBACK = "127.0.0.1"


def bind_address(headless: bool, configured: str) -> str:
    """The address the primary listener binds: loopback, always, when headless.

    *configured* is what an ordinary launch binds (``urls.bind_address_for``, which
    reads ``dashboard.url`` and ``KIROCREW_BIND``). A headless gateway serves programs
    on its own machine; a request from another machine reaches it through an entry
    point in front.
    """
    return _LOOPBACK if headless else configured


def print_client_access(port: int) -> None:
    """Print the API address and credential command, never a sign-in link or credential."""
    print(f"👻 Headless: API on http://{_LOOPBACK}:{port}/v1")
    print("   Credential for a Crew API client: kirocrew token --bearer")


#: Dashboard shell/PWA entries and resources outside the public-build exception.
DASHBOARD_RESOURCES = frozenset(
    {
        "/",
        "/logo.png",
        "/favicon.ico",
        "/{name}",
        "/app-assets/{tail}",
        "/sprites/{tail}",
        "/static",
        "/feature-videos/{release}/{name}",
    }
)


@web.middleware
async def api_only_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Refuse dashboard resources and unmatched shell navigations, not other services."""
    resource = request.match_info.route.resource
    unmatched_shell = isinstance(
        request.match_info.http_exception, web.HTTPNotFound
    ) and _is_spa_shell_request(request)
    if unmatched_shell or (resource is not None and resource.canonical in DASHBOARD_RESOURCES):
        return web.json_response(
            {"error": "This gateway runs headless and serves no dashboard.", "code": "headless"},
            status=404,
        )
    return await handler(request)
