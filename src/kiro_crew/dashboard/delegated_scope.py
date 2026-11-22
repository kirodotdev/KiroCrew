"""What a ``via_app`` token may reach.

``/api/token/local`` mints, for an app granted ``dashboard_proxy``, an owner token carrying
``via_app=<app>``. It drives the dashboard like its owner (chat, sessions, running agents,
files) and reaches no credential and no control-plane write. The scope is the route table
in :mod:`kiro_crew.dashboard.delegated_route_tags`: every route is tagged ``allow`` or
``deny``, and a route with no tag is denied here at runtime.

Each request also re-reads the grant and the owner-enable marker for the app, off the
event loop, so a disable or an uninstall ends the token at once, with no token to revoke.

Imported by the token middleware, which asks :func:`enforce_delegated` after it has
authenticated the request.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS

#: The token claim naming the app a delegated token was minted for.
VIA_APP_CLAIM = "via_app"

#: The ``code`` of every refusal here.
REFUSED_CODE = "delegated_credential_refused"


#: Allowed routes that reach an app's backend by ``{name}``: a delegated token may use
#: only the app it was minted for, never another installed app.
_OWN_APP_ONLY = frozenset({"/apps/{name}/api/{path}", "/app-frame-fetch/{name}/{path}"})


def route_tag(method: str, canonical: str) -> str | None:
    """``"allow"``, ``"deny"``, or ``None`` for a route (or method) no tag names."""
    entry = ROUTE_TAGS.get(canonical)
    if entry is None:
        return None
    verb = "GET" if method.upper() == "HEAD" else method.upper()
    return entry.get(verb) or entry.get("*")


def _refuse(request: web.Request, via_app: str, path: str, reason: str) -> web.Response:
    from kiro_crew.sel import sel

    sel().log_api_access(
        caller=via_app,
        operation="delegated_scope",
        outcome="denied",
        source="token_auth",
        resources=f"{request.method} {path} {reason}",
        error="delegated token out of scope",
    )
    return web.json_response(
        {"error": "not permitted for a delegated token", "code": REFUSED_CODE}, status=403
    )


async def enforce_delegated(request: web.Request, path: str) -> web.Response | None:
    """A 403 when the request's validated token is delegated and out of scope, else ``None``.

    No-op for any token without ``via_app``. Reads the claim from ``request["auth_token"]``,
    the credential the middleware actually validated.
    """
    from kiro_crew.dashboard.token_auth_tokens import token_via_app_unverified

    token = request.get("auth_token")
    via_app = token_via_app_unverified(token) if isinstance(token, str) else ""
    if not via_app:
        return None
    from kiro_crew.apps import dashboard_proxy

    if not await asyncio.to_thread(dashboard_proxy.granted, via_app):
        return _refuse(request, via_app, path, "grant-ended")
    resource = request.match_info.route.resource
    canonical = resource.canonical if resource is not None else ""
    if route_tag(request.method, canonical) != "allow":
        return _refuse(request, via_app, path, "untagged-or-denied")
    if canonical in _OWN_APP_ONLY and request.match_info.get("name") != via_app:
        return _refuse(request, via_app, path, "other-app")
    request["via_app"] = via_app
    return None
