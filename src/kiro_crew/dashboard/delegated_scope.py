"""What a ``via_app`` token may reach.

``/api/token/local`` mints, for an app granted ``dashboard_proxy``, an owner token carrying
``via_app=<app>`` and the id of the grant it was minted under. It shows the owner's
dashboard shell and stops one running chat turn. It reaches no credential, no host file, no
export, no chat message, and no control-plane write.

One rule, deny by default, for EVERY method and EVERY channel (a plain read, a write, a
WebSocket upgrade, an SSE stream): a request is allowed only when :data:`READ_ALLOW` has a
row for its method and its route. A route nobody listed is refused, so a route added later
(by the core, an edition or an app) is refused until someone lists it on purpose, and
``test_delegated_read_allow`` walks every registered route to prove it.

Each request also re-reads the grant and the owner-enable marker for the app, off the
event loop, and compares the grant id the token carries with the current grant, so a
disable, an uninstall, a reinstall or a revoke ends the token at once, with no token to
revoke. A request that can stay open (a row with ``stream``) is also registered here, and a
sweep ends it when its grant ends.

The words the owner reads before granting (``apps.dashboard_proxy.prompt_text`` and the
card on the app page) say what these rows allow. If a row is added, those words change in
the same commit; ``test_delegated_read_allow`` pins them together.

Imported by the token middleware, which asks :func:`enforce_delegated` after it has
authenticated the request.
"""

from __future__ import annotations

import asyncio
from typing import Any, NamedTuple

from aiohttp import web

#: The token claim naming the app a delegated token was minted for.
VIA_APP_CLAIM = "via_app"

#: The token claim carrying ``dashboard_proxy.grant_claim`` of the grant it was minted under.
VIA_GRANT_CLAIM = "via_grant"

#: The ``code`` of every refusal here.
REFUSED_CODE = "delegated_credential_refused"

#: Seconds between two checks of the grant held by each open request that may stay open.
STREAM_RECHECK_SECS = 2.0


class Row(NamedTuple):
    """One thing a delegated token may do."""

    #: ``src/``-relative ``file:line`` of the route's handler (its ``def`` or first decorator).
    handler: str
    #: What the request can do at most, in one line a reviewer can check against the handler.
    effect: str
    #: A request with a query string is refused (the route reads none, or one that widens it).
    no_query: bool = False
    #: A request with a body is refused.
    no_body: bool = False
    #: ``{name}`` must be the token's own app.
    own_app: bool = False
    #: The response may stay open: the request is registered and ended with its grant.
    stream: bool = False


#: ``(method, canonical path)`` -> what it does. ``"*"`` is every method; HEAD follows GET.
#: Everything else is refused: a write, a read, an upgrade, a stream, on any route.
#: Adding a row is a security decision: the handler of a GET row must answer a plain
#: response (never a stream, a socket or a file), and the prompt and card text must grow with
#: it (``test_read_allow_is_exactly_the_reviewed_set`` pins this set).
#: A row that lists MCP servers must mask each server's launch arguments, which can carry
#: ``--token`` values.
#:
#: The GETs are the calls the dashboard page makes to boot (``website/src``): the shell, the
#: refresh scheduler, the first-run gate, the branding, the theme and language and the
#: chat list (a bare projection for this token). The stored UI preferences are not listed:
#: they name host paths, and the shell boots on its defaults when that GET answers 403. No
#: WebSocket and no SSE stream is listed; no chat message body is read.
READ_ALLOW: dict[tuple[str, str], Row] = {
    ("GET", "/"): Row(
        "src/kiro_crew/dashboard/handlers/core.py:228",
        "Serves the static dashboard page shell (no per-request data, no file input).",
    ),
    ("GET", "/api/auth/me"): Row(
        "src/kiro_crew/dashboard/handlers/auth_refresh.py:343",
        "Says only whether the caller's own token was accepted: no user id, principal id, display name or expiry; never returns the token.",
    ),
    ("GET", "/api/kiro-prerequisite"): Row(
        "src/kiro_crew/dashboard/handlers/kiro_prerequisite.py:190",
        "Latched readiness in the non-owner shape: no platform, path or probe detail (KiroPrerequisiteGate); a query would force a host probe.",
        no_query=True,
    ),
    ("GET", "/api/dashboard/branding"): Row(
        "src/kiro_crew/dashboard/handlers/core.py:292",
        "The dashboard name and logo choice (useBranding.tsx).",
    ),
    ("GET", "/api/theme/boot"): Row(
        "src/kiro_crew/dashboard/handlers/core.py:536",
        "The theme and language the page starts with (LanguageProvider.tsx); public at boot.",
    ),
    ("GET", "/api/chat/slots"): Row(
        "src/kiro_crew/dashboard/chat_api/slot_detail.py:53",
        "The list of chats as key, title, running and last activity only (api/client/chat.ts chatSlots).",
    ),
    ("POST", "/api/auth/refresh"): Row(
        "src/kiro_crew/dashboard/handlers/auth_refresh.py:465",
        "Rotates this session's own refresh chain; the new pair keeps the same via_app and grant.",
    ),
    ("POST", "/api/auth/logout"): Row(
        "src/kiro_crew/dashboard/handlers/auth_refresh.py:919",
        "Ends this delegated session only.",
    ),
    ("POST", "/api/chat/slots/{slot}/stop"): Row(
        "src/kiro_crew/dashboard/chat_api/turn_stop.py:1020",
        "No data reach; worst case stops the owner's own running turn of one chat; no body, no query.",
        no_query=True,
        no_body=True,
    ),
    ("*", "/apps/{name}/api/{path}"): Row(
        "src/kiro_crew/apps/routes_proxy.py:274",
        "Calls the token's own app backend (any other app is refused); may stream.",
        own_app=True,
        stream=True,
    ),
    ("*", "/app-frame-fetch/{name}/{path}"): Row(
        "src/kiro_crew/apps/app_frame.py:843",
        "Fetches through the token's own app frame only; may stream.",
        own_app=True,
        stream=True,
    ),
}

_READ_METHODS = frozenset({"GET", "HEAD"})


def row_for(method: str, canonical: str) -> Row | None:
    """The :data:`READ_ALLOW` row that opens *method* on *canonical*, or ``None``."""
    verb = "GET" if method.upper() == "HEAD" else method.upper()
    return READ_ALLOW.get((verb, canonical)) or READ_ALLOW.get(("*", canonical))


def allowed(method: str, canonical: str) -> bool:
    """The one decision, by method and route alone: is there a row for it."""
    return row_for(method, canonical) is not None


#: Open requests of a delegated token that may stay open -> the app and grant claim they ride on.
_STREAMS: dict[asyncio.Task[Any], tuple[str, str]] = {}
_sweeper: asyncio.Task[None] | None = None


def _track_stream(via_app: str, claim: str) -> None:
    global _sweeper
    task = asyncio.current_task()
    if task is None:
        return
    _STREAMS[task] = (via_app, claim)
    task.add_done_callback(lambda done: _STREAMS.pop(done, None))
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.get_running_loop().create_task(_sweep())


def reset() -> None:
    """Cancel the sweeper and forget every tracked stream (the test guard calls this)."""
    global _sweeper
    sweeper, _sweeper = _sweeper, None
    _STREAMS.clear()
    if sweeper is not None and not sweeper.done():
        try:
            sweeper.cancel()
        except RuntimeError:  # its loop is already closed: nothing is left to cancel
            pass


async def end_stale_streams() -> None:
    """Cancel every tracked open request whose grant is not the one the app holds now."""
    from kiro_crew.apps import dashboard_proxy

    for task, (via_app, claim) in list(_STREAMS.items()):
        if task.done():
            _STREAMS.pop(task, None)
        elif not await asyncio.to_thread(dashboard_proxy.grant_current, via_app, claim):
            _STREAMS.pop(task, None)
            task.cancel()


async def _sweep() -> None:
    while _STREAMS:
        await asyncio.sleep(STREAM_RECHECK_SECS)
        await end_stale_streams()


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
    app; the app routes do not, since an app calls its own backend all day.
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
    row = row_for(request.method, canonical)
    if row is None:
        return _refuse(request, via_app, canonical, "not-listed")
    # ``token`` is the sign-in link's own parameter, read by the middleware, never a handler.
    if row.no_query and any(key != "token" for key in request.query):
        return _refuse(request, via_app, canonical, "query-not-allowed")
    if row.no_body and request.body_exists:
        return _refuse(request, via_app, canonical, "body-not-allowed")
    if row.own_app and request.match_info.get("name") != via_app:
        return _refuse(request, via_app, canonical, "other-app")
    request["via_app"] = via_app
    if row.stream:
        _track_stream(via_app, claim)
    elif request.method.upper() not in _READ_METHODS:
        _log(request, via_app, canonical, "allowed", "")
    return None
