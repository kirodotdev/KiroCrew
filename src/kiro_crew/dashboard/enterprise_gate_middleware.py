"""Gateway middleware for the enterprise gate: deny all but login while closed.

Design: ``docs/request-for-change/rfc-crew-enterprise-edition.md``, section
"Enterprise gate" ("While the gate is closed, gateway middleware denies every API
except login"). The gate itself is ``kiro_crew.enterprise_gate``.

Only an enterprise build installs any of this; :func:`install_enterprise_gate`
and :func:`enterprise_gate_middleware_for` are no-ops in an ``oss`` build, so its
middleware chain is unchanged.

An enterprise gateway with no kiro login never gets this far: it exits before
the bind (``kiro_crew.enterprise_boot``). The gate therefore starts open, and is
closed only by a login lost or expired while the gateway runs.

While the gate is closed:

* A request matching :data:`ALLOWED_WHILE_CLOSED` (method AND path) passes on.
  That is the login status route, the session-cookie routes the dashboard needs
  to call it, the liveness probes, local shutdown, and the static files a login
  screen is drawn with.
* A GET/HEAD document navigation the SPA owns gets the static SPA shell
  directly, never the route's own handler, so a reload on any page shows the
  login screen. The shell is static and carries no data.
* Everything else, including any route not named here and every WebSocket
  upgrade, is refused 403 with code ``enterprise_gate_closed``. The list is an
  allowlist: a route added later is denied until someone adds it here.

When the gate closes, every connection that carried a gated request while it
was open is closed (a WebSocket, an event stream, a download still sending),
so nothing keeps streaming data after a login expires.

While closed, ``GET /api/kiro-prerequisite`` reports
``enterprise_login_required: true`` and ``initial_setup_complete: false`` (see
:func:`login_screen_required`), so the dashboard's setup gate shows its sign-in
screen instead of handing an established install the app it cannot use.

The middleware sits inner to token auth, so it only ever narrows: a request
token auth refused never reaches it, and one it lets through must still pass
token auth first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from kiro_crew import edition
from kiro_crew.dashboard.token_auth_paths import _is_spa_shell_request
from kiro_crew.enterprise_gate import (
    GATE_CLOSED_CODE,
    EnterpriseGate,
    GateStatus,
    LoginSource,
    LoginWatch,
)

logger = logging.getLogger(__name__)

GATE_APP_KEY = "enterprise_gate"
LOGIN_WATCH_APP_KEY = "enterprise_login_watch"

_GET = frozenset({"GET", "HEAD"})
_POST = frozenset({"POST"})

#: Exact paths served while the gate is closed, path -> methods.
ALLOWED_WHILE_CLOSED: dict[str, frozenset[str]] = {
    # The login status the setup screen polls; it names the sign-in command.
    "/api/kiro-prerequisite": _GET,
    # The two remedies that screen offers before sign-in can work: rewrite Kiro
    # Crew's agent specs, and update a kiro-cli too old for `acp`. Owner-only
    # POSTs (origin-checked, audited) that answer the same status snapshot.
    "/api/kiro-prerequisite/repair-specs": _POST,
    "/api/kiro-prerequisite/update-cli": _POST,
    # Dashboard session cookie: who the browser is, and keeping that cookie
    # fresh. Without them the status call above is refused by token auth.
    "/api/auth/me": _GET,
    "/api/auth/refresh": _POST,
    # Session revocation stays reachable: a lost kiro login must not stop a
    # user (or `kirocrew logout`, loopback plus the local secret) from ending a
    # dashboard session that refresh above could otherwise keep renewing.
    "/api/auth/logout": _POST,
    "/api/logout": _POST,
    # Local token mint the desktop app and CLI open the dashboard with; the
    # handler requires loopback plus the local secret.
    "/api/token/local": _GET,
    # Liveness and coarse readiness: the desktop app and `kirocrew` restart
    # supervise the gateway through these and would kill a gateway they cannot
    # reach. They carry no user data.
    "/api/health": _GET,
    "/api/live": _GET,
    "/api/ready": _GET,
    # Local shutdown (loopback plus the local secret), so a closed gateway can
    # still be stopped.
    "/api/shutdown": _POST,
    # The SPA shell and the static files it loads before the login screen.
    "/": _GET,
    "/api/theme/boot": _GET,
    "/logo.png": _GET,
    "/favicon.ico": _GET,
    "/manifest.json": _GET,
    "/sw.js": _GET,
}

#: Static bundle prefixes served while the gate is closed (GET/HEAD only).
ALLOWED_PREFIXES_WHILE_CLOSED: tuple[str, ...] = ("/assets/", "/fonts/")

_PWA_ICON_RE = re.compile(r"^/icon-\d+\.png$")

ERROR_CODE = GATE_CLOSED_CODE

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def allowed_while_closed(method: str, path: str) -> bool:
    """Whether *method* *path* is served while the gate is closed."""
    methods = ALLOWED_WHILE_CLOSED.get(path)
    if methods is not None:
        return method in methods
    if method not in _GET:
        return False
    return path.startswith(ALLOWED_PREFIXES_WHILE_CLOSED) or bool(_PWA_ICON_RE.match(path))


def _is_websocket_upgrade(request: web.Request) -> bool:
    return request.headers.get("Upgrade", "").lower() == "websocket"


def _closed_error(status: GateStatus) -> web.HTTPForbidden:
    body = {
        "error": "Kiro Crew is locked until you sign in to Kiro.",
        "code": ERROR_CODE,
        "reason": status.reason,
    }
    return web.HTTPForbidden(text=json.dumps(body), content_type="application/json")


class _AdmittedRequests:
    """Connections that carried a gated request while the gate was open.

    Tracked per CONNECTION, not per handler call, and kept until the connection
    itself closes: aiohttp prepares and sends a returned response (a
    ``FileResponse`` download, say) after the middleware has returned, so
    dropping the entry when the handler returns would let that transfer run on
    after the gate closes. An idle keep-alive connection is closed too; the
    browser simply opens a new one.
    """

    def __init__(self) -> None:
        # Keyed by id(): the entry holds the transport, so the id cannot be
        # reused while present. Closed transports are pruned on each add.
        self._transports: dict[int, asyncio.BaseTransport] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, request: web.Request) -> None:
        transport = request.transport
        if transport is None:
            return
        for key, held in list(self._transports.items()):
            if held.is_closing():
                del self._transports[key]
        self._transports[id(transport)] = transport

    def on_gate_change(self, status: GateStatus) -> None:
        if status.open:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._close_all)

    def _close_all(self) -> None:
        transports, self._transports = list(self._transports.values()), {}
        closed = 0
        for transport in transports:
            if not transport.is_closing():
                transport.close()
                closed += 1
        if closed:
            logger.info("Enterprise gate closed %d open connection(s)", closed)


def login_screen_required(app: web.Application) -> bool:
    """Whether *app*'s enterprise gate is closed, so the UI must show sign-in.

    False in an ``oss`` build and whenever no gate is installed on *app*.
    """
    gate = app.get(GATE_APP_KEY)
    return isinstance(gate, EnterpriseGate) and not gate.is_open


def install_enterprise_gate(app: web.Application, login_source: LoginSource) -> None:
    """Build the open gate and its login watch on *app*. No-op in ``oss``.

    The boot login check (``kiro_crew.enterprise_boot.require_kiro_login``) has
    already seen a login on *login_source*, or the gateway exited before getting
    here, so the gate starts OPEN; it closes only when that login is later lost.
    Raises when *login_source* holds no login, so a caller that skipped the boot
    check fails at boot rather than serving a gateway that starts locked.

    Must run before the middleware chain is installed (the chain reads
    :data:`GATE_APP_KEY`) and before ``runner.setup()`` freezes the signal
    lists. The watch starts with the app and makes its first check at once.
    """
    if not edition.is_enterprise():
        return
    if not login_source.authenticated:
        raise RuntimeError("enterprise build: the boot login check has not seen a kiro login")
    gate = EnterpriseGate(closed_for=())
    watch = LoginWatch(gate, login_source)
    admitted = _AdmittedRequests()
    gate.add_listener(admitted.on_gate_change)
    app[GATE_APP_KEY] = gate
    app[LOGIN_WATCH_APP_KEY] = watch
    app["enterprise_gate_admitted"] = admitted

    async def _start(_app: web.Application) -> None:
        admitted.bind_loop(asyncio.get_running_loop())
        watch.start()

    async def _stop(_app: web.Application) -> None:
        await watch.stop()

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)


def enterprise_gate_middleware_for(
    app: web.Application,
    *,
    spa_shell_handler: Handler | None,
) -> Callable[..., Any] | None:
    """The gate middleware for *app*'s chain, or ``None`` in an ``oss`` build.

    Raises in an enterprise build when :func:`install_enterprise_gate` has not
    run, so a wiring mistake fails at boot instead of serving an ungated app.
    The middleware holds the gate object itself rather than looking it up per
    request, so a chain copied onto another app (the tunnel) shares this gate.
    """
    if not edition.is_enterprise():
        return None
    gate = app.get(GATE_APP_KEY)
    watch = app.get(LOGIN_WATCH_APP_KEY)
    admitted = app.get("enterprise_gate_admitted")
    if (
        not isinstance(gate, EnterpriseGate)
        or not isinstance(watch, LoginWatch)
        or not isinstance(admitted, _AdmittedRequests)
    ):
        raise RuntimeError("enterprise build: the enterprise gate is not installed")
    return make_enterprise_gate_middleware(
        gate, watch, admitted=admitted, spa_shell_handler=spa_shell_handler
    )


def make_enterprise_gate_middleware(
    gate: EnterpriseGate,
    watch: LoginWatch,
    *,
    admitted: _AdmittedRequests,
    spa_shell_handler: Handler | None,
) -> Callable[..., Any]:
    @web.middleware  # type: ignore[misc]
    async def enterprise_gate_middleware(
        request: web.Request,
        handler: Handler,
    ) -> web.StreamResponse:
        watch.sync()
        method, path = request.method, request.path
        login_route = allowed_while_closed(method, path) and not _is_websocket_upgrade(request)
        status = gate.status()
        if status.open:
            if login_route:
                return await handler(request)
            admitted.add(request)
            return await handler(request)
        if login_route:
            return await handler(request)
        if (
            spa_shell_handler is not None
            and not _is_websocket_upgrade(request)
            and _is_spa_shell_request(request)
        ):
            return await spa_shell_handler(request)
        raise _closed_error(status)

    return enterprise_gate_middleware
