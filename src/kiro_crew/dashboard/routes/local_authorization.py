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
import logging
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web
from multidict import MultiDict

from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller
from kiro_crew.dashboard.origin import check_origin, is_direct_local_request
from kiro_crew.dashboard.token_auth_paths import LOCAL_AUTH_AUTHORIZE_PATH as AUTHORIZE_PATH
from kiro_crew.dashboard.token_auth_paths import LOCAL_AUTH_REVOKE_PATH as REVOKE_PATH
from kiro_crew.dashboard.token_auth_paths import LOCAL_AUTH_TOKEN_PATH as TOKEN_PATH
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_NO_STORE = {"Cache-Control": "no-store"}
#: The largest form body the token and revoke routes read: their fields together, each
#: at most ``PRESENTED_MAX``, fit with room.
_BODY_MAX = 16 * 1024
_FORM_TYPE = "application/x-www-form-urlencoded"


def _error(error: str, description: str, status: int = 400, retry_after: int = 0) -> web.Response:
    headers = dict(_NO_STORE)
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    # ``error`` is the OAuth code (RFC 6749 section 5.2); ``code`` repeats it for the
    # gateway's own error readers.
    return web.json_response(
        {"error": error, "error_description": description, "code": error},
        status=status,
        headers=headers,
    )


async def _refused(request: web.Request) -> web.Response | None:
    """404 unless *request* came straight from this machine."""
    if is_direct_local_request(request) and published_relay_caller(request) is None:
        return None
    await _audit("local_auth.admission", "denied", "not-direct-local")
    return _error("not_found", "local authorization is only served on loopback", 404)


async def _read_form(request: web.Request) -> MultiDict[str] | web.Response:
    """The urlencoded form, read at most ``_BODY_MAX`` bytes deep, or a 413.

    A declared length over the cap is refused before any byte is read. A chunked body
    is read in pieces and refused as soon as it passes the cap; the rest is never
    read. Any other content type is an empty form, so the route's own 400 answers.
    """
    too_large = _error("invalid_request", "the body is larger than 16 KiB", 413)
    length = request.content_length
    if length is not None and length > _BODY_MAX:
        return too_large
    if request.content_type != _FORM_TYPE:
        return MultiDict()
    body = bytearray()
    while chunk := await request.content.read(_BODY_MAX + 1 - len(body)):
        body += chunk
        if len(body) > _BODY_MAX:
            return too_large
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return _error("invalid_request", "the body is not UTF-8")
    return MultiDict(parse_qsl(text, keep_blank_values=True))


def _issuer(request: web.Request) -> str:
    return str(request.url.origin())


async def _audit(operation: str, outcome: str, resources: str) -> None:
    """Best-effort route audit: SEL failure must not hide a committed token rotation."""

    def write() -> None:
        # Only ids the gateway made and fixed labels, never an app's free text. The
        # caller is not "local-app": that is the owner's own subject before an owner
        # is configured, and these rows are a program's, or a refused request's.
        sel().log_api_access(
            caller="local-process",
            operation=operation,
            outcome=outcome,
            source="local-authorization",
            resources=resources,
        )

    try:
        # Cold singleton construction and synchronous writer fallback may both do I/O.
        await asyncio.to_thread(write)
    except Exception:
        logger.warning("Local authorization route audit failed")


async def _run(method: str, *args: Any) -> Any:
    """Construct the store and run its named method off-loop; an unreadable record refuses."""
    try:
        return await asyncio.to_thread(
            lambda: getattr(la.gateway_local_authorization(), method)(*args)
        )
    except la.AuthError as exc:
        return _error(exc.error, exc.description, exc.status, exc.retry_after)
    except (OSError, ValueError, KeyError, TypeError):
        return _error("server_error", "local authorization is unavailable", 503)


async def oauth_metadata(request: web.Request) -> web.Response:
    """GET /.well-known/oauth-authorization-server (RFC 8414)."""
    if (refused := await _refused(request)) is not None:
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
            # Where the owner answers a request on this Kiro Crew: the Owner routes.
            "crew_consent_modes": ["owner_api"],
        }
    )


def _cross_site(request: web.Request) -> bool:
    """Whether a browser sent *request* for a page of another origin.

    A loopback peer is not enough here: any page in the owner's browser can send a
    credential-free GET, and each one would hold a waiting slot. The app's own top-level
    open says ``Sec-Fetch-Site: none``, a program sends no browser header, and the
    waiting page's Refresh comes from this origin, so all three still pass.
    """
    if request.headers.get("Sec-Fetch-Site", "").lower() in ("cross-site", "same-site"):
        return True
    # No Origin or Referer: check_origin trusts a loopback peer, which _refused proved.
    if not (request.headers.get("Origin") or request.headers.get("Referer")):
        return False
    return not check_origin(request, require=False, fallback_header="Referer")


async def oauth_authorize(request: web.Request) -> web.Response:
    """GET /oauth/authorize: record a grant request, or ask again about one."""
    if (refused := await _refused(request)) is not None:
        return refused
    if _cross_site(request):
        await _audit("local_auth.request", "denied", "cross-site")
        return _error("invalid_request", "open this address from the app, not from a page", 403)
    result = await _run("authorize", dict(request.query))
    if isinstance(result, web.Response):
        return result
    kind, value = result
    if kind == "redirect":
        return web.Response(status=302, headers={"Location": value, **_NO_STORE})
    # The store says whether it recorded a request; the route does not guess from the query.
    if kind == "created":
        await _audit("local_auth.request", "pending", f"request={value['request_id']}")
    if "application/json" in request.headers.get("Accept", ""):
        body = {
            "request_id": value["request_id"],
            "status": "pending",
            "user_code": value["user_code"],
            "expires_at": value["expires_at"],
            "interval": la.POLL_INTERVAL_SECS,
        }
        return web.json_response(body, status=202, headers=_NO_STORE)
    # Plain text and a Refresh header: the page carries no markup, so nothing the app
    # sent can be read as markup either.
    again = request.rel_url.update_query(request_id=value["request_id"])
    text = f"Waiting for the owner of this Kiro Crew to answer.\nCode: {value['user_code']}\n"
    headers = {**_NO_STORE, "Refresh": f"{la.POLL_INTERVAL_SECS + 1}; url={again}"}
    return web.Response(text=text, content_type="text/plain", headers=headers)


async def oauth_token(request: web.Request) -> web.Response:
    """POST /oauth/token: exchange a code, or refresh."""
    if (refused := await _refused(request)) is not None:
        return refused
    form = await _read_form(request)
    if isinstance(form, web.Response):
        return form
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    client_id = fields.get("client_id", "")
    grant_type = fields.get("grant_type", "")
    issuer = _issuer(request)
    if grant_type == "authorization_code":
        code, verifier = fields.get("code", ""), fields.get("code_verifier", "")
        redirect_uri = fields.get("redirect_uri", "")
        if not (client_id and code and verifier and redirect_uri):
            return _error("invalid_request", "client_id, code, redirect_uri and code_verifier")
        result = await _run("exchange_code", client_id, code, redirect_uri, verifier, issuer)
    elif grant_type == "refresh_token":
        refresh_token = fields.get("refresh_token", "")
        if not (client_id and refresh_token):
            return _error("invalid_request", "client_id and refresh_token are required")
        result = await _run("refresh", client_id, refresh_token, fields.get("scope", ""), issuer)
    else:
        return _error("unsupported_grant_type", "authorization_code or refresh_token")
    if isinstance(result, web.Response):
        await _audit("local_auth.token", "denied", f"grant_type={grant_type}")
        return result
    await _audit("local_auth.token", "success", f"grant={result['grant_id']}")
    return web.json_response(result, headers=_NO_STORE)


async def oauth_revoke(request: web.Request) -> web.Response:
    """POST /oauth/revoke (RFC 7009): the app gives up its own grant."""
    if (refused := await _refused(request)) is not None:
        return refused
    form = await _read_form(request)
    if isinstance(form, web.Response):
        return form
    token = form.get("token", "")
    if not isinstance(token, str) or not token:
        return _error("invalid_request", "token is missing")
    result = await _run("revoke_token", token)
    if isinstance(result, web.Response):
        return result
    await _audit("local_auth.revoke", "success" if result else "no_grant", "token")
    return web.Response(status=200, headers=_NO_STORE)


def register(app: web.Application) -> None:
    """Register the local authorization routes on *app*."""
    # Literal paths: test_dashboard_route_table.py reads each slice's paths from source.
    app.router.add_get("/.well-known/oauth-authorization-server", oauth_metadata)
    app.router.add_get("/oauth/authorize", oauth_authorize)
    app.router.add_post("/oauth/token", oauth_token)
    app.router.add_post("/oauth/revoke", oauth_revoke)
