"""The four local authorization routes (the Crew API RFC, "Local authorization").

They sit outside ``/v1`` and take no credential, so each one admits only a request made
directly from this machine: a loopback peer with no forwarding header that the relay did
not deliver. Anything else gets 404, the answer of a path nothing serves. None of them
grants anything: the owner answers a request through the Owner tier, and a code is
redeemed only with the PKCE verifier of the app that asked.

Errors use the OAuth body (``error``, ``error_description``), not the Crew API one.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any

from aiohttp import web

from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller
from kiro_crew.dashboard.origin import is_direct_local_request
from kiro_crew.dashboard.token_auth_paths import LOCAL_AUTH_AUTHORIZE_PATH as AUTHORIZE_PATH
from kiro_crew.dashboard.token_auth_paths import LOCAL_AUTH_REVOKE_PATH as REVOKE_PATH
from kiro_crew.dashboard.token_auth_paths import LOCAL_AUTH_TOKEN_PATH as TOKEN_PATH
from kiro_crew.sel import sel

_NO_STORE = {"Cache-Control": "no-store"}


def _error(error: str, description: str, status: int = 400, retry_after: int = 0) -> web.Response:
    headers = dict(_NO_STORE)
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    body = {"error": error, "error_description": description}
    return web.json_response(body, status=status, headers=headers)


def _refused(request: web.Request) -> web.Response | None:
    """404 unless *request* came straight from this machine."""
    if is_direct_local_request(request) and published_relay_caller(request) is None:
        return None
    return _error("not_found", "local authorization is only served on loopback", 404)


def _issuer(request: web.Request) -> str:
    return str(request.url.origin())


def _audit(operation: str, outcome: str, resources: str) -> None:
    # Only ids the gateway made are written: an app's own client_id is free text.
    sel().log_api_access(
        caller="local-app",
        operation=operation,
        outcome=outcome,
        source="local-authorization",
        resources=resources,
    )


async def _run(call: Any, *args: Any) -> Any:
    """Run one store call off the event loop; an unreadable record refuses."""
    try:
        return await asyncio.to_thread(call, *args)
    except la.AuthError as exc:
        return _error(exc.error, exc.description, exc.status, exc.retry_after)
    except (OSError, ValueError, KeyError, TypeError):
        return _error("server_error", "local authorization is unavailable", 503)


async def oauth_metadata(request: web.Request) -> web.Response:
    """GET /.well-known/oauth-authorization-server (RFC 8414)."""
    if (refused := _refused(request)) is not None:
        return refused
    base = _issuer(request)
    return web.json_response(
        {
            "issuer": base,
            "authorization_endpoint": base + AUTHORIZE_PATH,
            "token_endpoint": base + TOKEN_PATH,
            "revocation_endpoint": base + REVOKE_PATH,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(la.SCOPES),
            "crew_endpoint": base,
        }
    )


async def oauth_authorize(request: web.Request) -> web.Response:
    """GET /oauth/authorize: record a grant request, or ask again about one."""
    if (refused := _refused(request)) is not None:
        return refused
    store = la.gateway_local_authorization()
    result = await _run(store.authorize, dict(request.query))
    if isinstance(result, web.Response):
        return result
    kind, value = result
    if kind == "redirect":
        return web.Response(status=302, headers={"Location": value, **_NO_STORE})
    if "request_id" not in request.query:
        _audit("local_auth.request", "pending", f"request={value['request_id']}")
    if "application/json" in request.headers.get("Accept", ""):
        body = {
            "request_id": value["request_id"],
            "status": "pending",
            "user_code": value["user_code"],
            "expires_at": value["expires_at"],
            "interval": la.POLL_INTERVAL_SECS,
        }
        return web.json_response(body, status=202, headers=_NO_STORE)
    again = request.rel_url.update_query(request_id=value["request_id"])
    page = (
        "<!doctype html><meta charset=utf-8>"
        f'<meta http-equiv="refresh" content="{la.POLL_INTERVAL_SECS + 1};'
        f'url={html.escape(str(again), quote=True)}">'
        "<title>Kiro Crew</title><p>Waiting for the owner of this Kiro Crew to answer.</p>"
        f"<p>Code: <b>{html.escape(value['user_code'])}</b></p>"
    )
    headers = {**_NO_STORE, "Content-Security-Policy": "default-src 'none'"}
    return web.Response(text=page, content_type="text/html", headers=headers)


async def oauth_token(request: web.Request) -> web.Response:
    """POST /oauth/token: exchange a code, or refresh."""
    if (refused := _refused(request)) is not None:
        return refused
    form = await request.post()
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    client_id = fields.get("client_id", "")
    grant_type = fields.get("grant_type", "")
    store = la.gateway_local_authorization()
    issuer = _issuer(request)
    if grant_type == "authorization_code":
        code, verifier = fields.get("code", ""), fields.get("code_verifier", "")
        redirect_uri = fields.get("redirect_uri", "")
        if not (client_id and code and verifier and redirect_uri):
            return _error("invalid_request", "client_id, code, redirect_uri and code_verifier")
        result = await _run(store.exchange_code, client_id, code, redirect_uri, verifier, issuer)
    elif grant_type == "refresh_token":
        refresh_token = fields.get("refresh_token", "")
        if not (client_id and refresh_token):
            return _error("invalid_request", "client_id and refresh_token are required")
        result = await _run(
            store.refresh, client_id, refresh_token, fields.get("scope", ""), issuer
        )
    else:
        return _error("unsupported_grant_type", "authorization_code or refresh_token")
    if isinstance(result, web.Response):
        _audit("local_auth.token", "denied", f"grant_type={grant_type}")
        return result
    _audit("local_auth.token", "success", f"grant={result['grant_id']}")
    return web.json_response(result, headers=_NO_STORE)


async def oauth_revoke(request: web.Request) -> web.Response:
    """POST /oauth/revoke (RFC 7009): the app gives up its own grant."""
    if (refused := _refused(request)) is not None:
        return refused
    token = (await request.post()).get("token", "")
    if not isinstance(token, str) or not token:
        return _error("invalid_request", "token is missing")
    result = await _run(la.gateway_local_authorization().revoke_token, token)
    if isinstance(result, web.Response):
        return result
    _audit("local_auth.revoke", "success", "token")
    return web.Response(status=200, headers=_NO_STORE)


def register(app: web.Application) -> None:
    """Register the local authorization routes on *app*."""
    # Literal paths: test_dashboard_route_table.py reads each slice's paths from source.
    app.router.add_get("/.well-known/oauth-authorization-server", oauth_metadata)
    app.router.add_get("/oauth/authorize", oauth_authorize)
    app.router.add_post("/oauth/token", oauth_token)
    app.router.add_post("/oauth/revoke", oauth_revoke)
