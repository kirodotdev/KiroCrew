"""What a ``via_app`` token may reach.

``/api/token/local`` mints, for an app granted ``dashboard_proxy``, an owner token carrying
``via_app=<app>`` and the id of the grant it was minted under. It reads the dashboard like
its owner (chat, sessions, running agents) and reaches no credential, no host file, no whole
export and no control-plane write. Chat is read-only through the tunnel: the token starts no
agent turn, so nothing it sends runs as the owner.

Two rules, both deny by default:

* A READ (GET, HEAD) needs the route to be tagged ``allow`` in
  :mod:`kiro_crew.dashboard.delegated_route_tags`; a route with no tag is denied.
* A WRITE (any other method) needs the route to be in :data:`ALLOW_WRITES` below, and the
  tag table holds no write entry. A write is open only where this short list opens it.

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
#: consent, provider, MCP, sharing or egress, or start, queue or steer an agent turn or run
#: a tool. Adding a row is a security decision, and a row whose handler can start an agent
#: turn, queue a message or run a tool needs a security review first
#: (``test_allow_writes_is_exactly_the_reviewed_set`` pins this set).
ALLOW_WRITES: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/refresh"): "Rotates this delegated session's own refresh chain.",
    ("POST", "/api/auth/logout"): "Ends this delegated session only.",
    (
        "*",
        "/apps/{name}/api/{path}",
    ): "Calls the token's own app backend (any other app is refused).",
    ("*", "/app-frame-fetch/{name}/{path}"): "Fetches through the token's own app frame only.",
    (
        "POST",
        "/api/chat/slots/{slot}/stop",
    ): "No data reach; worst case stops the owner's own turn of one chat slot.",
    (
        "POST",
        "/api/chat/slots/{slot}/interrupt",
    ): "No data reach; worst case cancels the owner's own turn of one chat slot.",
    (
        "POST",
        "/api/chat/pinned-order",
    ): "No data reach; saves the order of the pinned chats (UI state).",
    (
        "POST",
        "/api/chat/folders/reorder",
    ): "No data reach; saves the order of the chat folders (UI state).",
    (
        "POST",
        "/api/chat/manual-order",
    ): "No data reach; saves the manual order of the chat list (UI state).",
}

#: Credential-looking names a delegated GET may still return, each with why it is no
#: credential. Any other allowed GET whose handler names a secret, token, password,
#: credential, cookie, authorization or key field fails ``test_delegated_route_tags``.
READ_REVIEWED: dict[str, str] = {
    "/api/apps/{name}/uninstall/preview": "app_token_forbidden is an error code, not a value.",
    "/api/auth/me": "says whether the caller's own token was accepted; never returns it.",
    "/api/aws/consent": "credentialSource is a label for where the profile came from.",
    "/api/memory/records": "token is an allowed query parameter name, not returned.",
    "/api/mobile-connect/methods": "app_token_* are error codes, not values.",
    "/api/usage/kiro": "tokens are model usage counts.",
    "/app-frame/{name}/{point}/{point_id}/{frame_id}/{token}": (
        "the path token is the frame capability the caller already holds in the URL."
    ),
}

#: Allowed GETs whose path or handler looks like a file read, a listing or an export, each
#: with why it reaches no host file and no whole-install or memory export. A file read, a
#: directory listing outside the app or an export that is not listed here is ``deny``.
#: ``test_a_get_that_serves_files_or_an_export_is_denied_or_reviewed`` walks the route table.
CONTENT_READ_REVIEWED: dict[str, str] = {
    "/api/apps/auto-improvement/findings": "lists the app's own findings records, no path input.",
    "/api/apps/auto-improvement/findings/{fp}": "one of the app's own findings, keyed by id.",
    "/api/apps/auto-research/campaigns/{id}/grill-tree": "the app's own campaign data by id.",
    "/api/apps/crew-companion/appearances/export": "the app's own appearance pack, not an install export.",
    "/api/apps/design-critique/method": "the app's packaged method text, no path input.",
    "/api/apps/mochi/packs/{pack_id}/export": "one of the app's own packs, keyed by id.",
    "/api/apps/mochi/packs/{pack_id}/file/{filename}": "a file inside the app's own pack.",
    "/api/apps/pptx-maker/preview/{deck_id}/{subpath}": "a preview inside the app's own deck.",
    "/api/chat/slots/{slot}/export": "one chat's transcript, the data the chat read returns.",
    "/api/kiro-hooks": "the hook entries of the agent config through the hardened reader.",
    "/api/logs/level": "the log level only, no log content.",
    "/api/outbox": "lists the outbox names; each file read is /api/outbox/{filename}, denied.",
    "/api/themes/{slug}": "one theme's manifest from the installed themes, keyed by slug.",
    "/apps/{name}/ui/{path}": "an installed app's static UI files, served to any browser.",
    "/favicon.ico": "a packaged static image.",
    "/feature-videos/{release}/{name}": "a packaged release video, keyed by release.",
    "/logo.png": "a packaged static image.",
    "/{name}": "the dashboard page shell, no file input.",
}

#: Allowed routes that reach an app's backend, its frame page (which carries the frame
#: capability) or its stored config and manifest by ``{name}``: a delegated token may use
#: only the app it was minted for, never another installed app.
_OWN_APP_ONLY = frozenset(
    {
        "/apps/{name}/api/{path}",
        "/app-frame-fetch/{name}/{path}",
        "/app-frame/{name}/{point}/{point_id}/{frame_id}/{token}",
        "/api/apps/{name}/config",
        "/api/apps/{name}/manifest",
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


def _log(request: web.Request, via_app: str, canonical: str, outcome: str, reason: str) -> None:
    """One SEL row. It names the route TEMPLATE, never the raw path: a raw path can carry a
    frame capability or another secret in its segments."""
    from kiro_crew.sel import sel

    sel().log_api_access(
        caller=via_app,
        operation="delegated_scope",
        outcome=outcome,
        source="token_auth",
        resources=f"{request.method} {canonical or '(no route)'} {reason}".rstrip(),
        error="delegated token out of scope" if outcome == "denied" else "",
    )


def _refuse(request: web.Request, via_app: str, canonical: str, reason: str) -> web.Response:
    _log(request, via_app, canonical, "denied", reason)
    return web.json_response(
        {"error": "not permitted for a delegated token", "code": REFUSED_CODE}, status=403
    )


async def enforce_delegated(request: web.Request, path: str) -> web.Response | None:
    """A 403 when the request's validated token is delegated and out of scope, else ``None``.

    No-op for any token without ``via_app``. Reads the claims from ``request["auth_token"]``,
    the credential the middleware actually validated. *path* is the raw request path; it is
    never logged (see :func:`_log`). An allowed write leaves an ``allowed`` row naming the
    app; the app routes (``*`` rows) do not, since an app calls its own backend all day.
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

    resource = request.match_info.route.resource
    canonical = resource.canonical if resource is not None else ""
    claim = token_via_grant_unverified(token)
    if not await asyncio.to_thread(dashboard_proxy.grant_current, via_app, claim):
        return _refuse(request, via_app, canonical, "grant-ended")
    if not allowed(request.method, canonical):
        read = request.method.upper() in _READ_METHODS
        return _refuse(
            request, via_app, canonical, "untagged-or-denied" if read else "write-not-listed"
        )
    if canonical in _OWN_APP_ONLY and request.match_info.get("name") != via_app:
        return _refuse(request, via_app, canonical, "other-app")
    request["via_app"] = via_app
    if request.method.upper() not in _READ_METHODS and ("*", canonical) not in ALLOW_WRITES:
        _log(request, via_app, canonical, "allowed", "")
    return None
