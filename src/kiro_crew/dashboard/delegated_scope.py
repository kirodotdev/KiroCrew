"""What a ``via_app`` token may reach.

``/api/token/local`` mints, for an app granted ``dashboard_proxy``, an owner token carrying
``via_app=<app>`` and the id of the grant it was minted under. It reads the dashboard like
its owner (chat, sessions, running agents) and reaches no credential and no control-plane
write.

Two rules, both deny by default:

* A READ (GET, HEAD) needs the route to be tagged ``allow`` in
  :mod:`kiro_crew.dashboard.delegated_route_tags`; a route with no tag is denied.
* A WRITE (any other method) needs the route to be in :data:`ALLOW_WRITES` below, and the
  tag table holds no write entry. A write is open only where this short list opens it, and
  the two chat routes also hold the body to :data:`BODY_FIELDS` (:func:`body_refusal`).

Each request also re-reads the grant and the owner-enable marker for the app, off the
event loop, and compares the grant id the token carries with the current grant, so a
disable, an uninstall, a reinstall or a revoke ends the token at once, with no token to revoke.

Imported by the token middleware, which asks :func:`enforce_delegated` after it has
authenticated the request.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS, under_mcp_tool_prefix

#: The token claim naming the app a delegated token was minted for.
VIA_APP_CLAIM = "via_app"

#: The token claim carrying ``dashboard_proxy.grant_claim`` of the grant it was minted under.
VIA_GRANT_CLAIM = "via_grant"

#: The ``code`` of every refusal here.
REFUSED_CODE = "delegated_credential_refused"

#: ``(method, canonical path)`` -> what the write does. ``"*"`` is every write method.
#: Nothing here may touch a file, config, credential, install, grant, approval, terminal,
#: consent, provider, MCP, sharing or egress. Adding a row is a security decision.
ALLOW_WRITES: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/refresh"): "Rotates this delegated session's own refresh chain.",
    ("POST", "/api/auth/logout"): "Ends this delegated session only.",
    (
        "*",
        "/apps/{name}/api/{path}",
    ): "Calls the token's own app backend (any other app is refused).",
    ("*", "/app-frame-fetch/{name}/{path}"): "Fetches through the token's own app frame only.",
    ("POST", "/api/chat"): "Sends a message into a chat session.",
    ("POST", "/api/chat/slots"): "Creates an empty chat slot.",
    ("POST", "/api/chat/slots/{slot}/stop"): "Stops the running turn of one chat slot.",
    ("POST", "/api/chat/slots/{slot}/interrupt"): "Cancels the running turn of one chat slot.",
    ("POST", "/api/chat/pinned-order"): "Saves the order of the pinned chats (UI state).",
    ("POST", "/api/chat/folders/reorder"): "Saves the order of the chat folders (UI state).",
    ("POST", "/api/chat/manual-order"): "Saves the manual order of the chat list (UI state).",
}

#: Credential-looking names a delegated GET may still return, each with why it is no
#: credential. Any other allowed GET whose handler names a secret, token, password,
#: credential, cookie, authorization or key field fails ``test_delegated_route_tags``.
READ_REVIEWED: dict[str, str] = {
    "/api/apps/aws-control/drive/{account}/list": "token is the list's paging cursor, an input.",
    "/api/apps/blob": "refuses the sandbox credential paths by name; returns no credential.",
    "/api/apps/{name}/uninstall/preview": "app_token_forbidden is an error code, not a value.",
    "/api/auth/me": "says whether the caller's own token was accepted; never returns it.",
    "/api/aws/consent": "credentialSource is a label for where the profile came from.",
    "/api/memory/records": "token is an allowed query parameter name, not returned.",
    "/api/mobile-connect/methods": "app_token_* are error codes, not values.",
    "/api/outbox/{filename}": "flags files that look like a credential and refuses them.",
    "/api/usage/kiro": "tokens are model usage counts.",
    "/app-frame/{name}/{point}/{point_id}/{frame_id}/{token}": (
        "the path token is the frame capability the caller already holds in the URL."
    ),
}

#: Allowed routes that reach an app's backend (or its frame page, which carries the
#: frame capability) by ``{name}``: a delegated token may use only the app it was minted
#: for, never another installed app.
_OWN_APP_ONLY = frozenset(
    {
        "/apps/{name}/api/{path}",
        "/app-frame-fetch/{name}/{path}",
        "/app-frame/{name}/{point}/{point_id}/{frame_id}/{token}",
    }
)

_READ_METHODS = frozenset({"GET", "HEAD"})


def route_tag(method: str, canonical: str) -> str | None:
    """``"allow"``, ``"deny"``, or ``None`` for a route (or method) no tag names."""
    entry = ROUTE_TAGS.get(canonical)
    if entry is None:
        return "deny" if under_mcp_tool_prefix(canonical) else None
    verb = "GET" if method.upper() == "HEAD" else method.upper()
    return entry.get(verb) or entry.get("*")


def write_allowed(method: str, canonical: str) -> bool:
    """Whether :data:`ALLOW_WRITES` opens this write route to a delegated token."""
    verb = method.upper()
    return (verb, canonical) in ALLOW_WRITES or ("*", canonical) in ALLOW_WRITES


def allowed(method: str, canonical: str) -> bool:
    """The one decision: a read needs an ``allow`` tag, a write needs an ``ALLOW_WRITES`` row."""
    if method.upper() in _READ_METHODS:
        return route_tag(method, canonical) == "allow"
    return write_allowed(method, canonical)


#: The body fields a delegated token may set on the two chat routes that start work. Any
#: other field is refused whole, so a field a later change adds is refused until it is
#: listed. ``/api/chat/slots`` takes no mode, agent, model, working directory or work item;
#: ``/api/chat`` takes a message and the slot it goes to.
BODY_FIELDS: dict[str, frozenset[str]] = {
    "/api/chat/slots": frozenset({"name", "title", "folder_id", "memory_mode"}),
    "/api/chat": frozenset({"message", "slot", "send_now", "base_revision"}),
}


def body_refusal(request: web.Request, body: dict) -> web.Response | None:
    """A 403 when a delegated token's body on a chat route sets more than it may.

    Called by the two chat handlers after they parse the body. No-op for any request that
    is not delegated (no ``via_app`` mark). A send whose message opens with ``/`` is a
    chat command, not a plain message, and is refused.
    """
    via_app = request.get("via_app")
    if not via_app:
        return None
    resource = request.match_info.route.resource
    canonical = resource.canonical if resource is not None else ""
    fields = BODY_FIELDS.get(canonical)
    if fields is None:
        return None
    extra = sorted(str(key) for key in body if key not in fields)
    if extra:
        return _refuse(request, via_app, canonical, f"body-field {extra[0][:40]}")
    message = body.get("message")
    if canonical == "/api/chat" and isinstance(message, str) and message.lstrip().startswith("/"):
        return _refuse(request, via_app, canonical, "slash-command")
    return None


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

    No-op for any token without ``via_app``. Reads the claims from ``request["auth_token"]``,
    the credential the middleware actually validated.
    """
    from kiro_crew.dashboard.token_auth_tokens import (
        token_via_app_unverified,
        token_via_grant_unverified,
    )

    token = request.get("auth_token")
    if not isinstance(token, str):
        return None
    via_app = token_via_app_unverified(token)
    if not via_app:
        return None
    from kiro_crew.apps import dashboard_proxy

    claim = token_via_grant_unverified(token)
    if not await asyncio.to_thread(dashboard_proxy.grant_current, via_app, claim):
        return _refuse(request, via_app, path, "grant-ended")
    resource = request.match_info.route.resource
    canonical = resource.canonical if resource is not None else ""
    if not allowed(request.method, canonical):
        read = request.method.upper() in _READ_METHODS
        return _refuse(request, via_app, path, "untagged-or-denied" if read else "write-not-listed")
    if canonical in _OWN_APP_ONLY and request.match_info.get("name") != via_app:
        return _refuse(request, via_app, path, "other-app")
    request["via_app"] = via_app
    return None
