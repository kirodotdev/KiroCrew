"""Enterprise sign-in-only gateway: a signed-out first run gets a sign-in screen.

Design: ``docs/request-for-change/rfc-crew-enterprise-edition.md``, section
"Enterprise gate". When an enterprise build's rules could not load and the only
reason is that nobody is signed in, the gateway does not exit at once. It serves a
small app on 127.0.0.1 that holds only the sign-in screen and the sign-in API and
waits for a sign-in. Then it ends the sign-in page's session and exits 0, telling
the user to start Kiro Crew again; the next start loads the rules with that
sign-in. No gateway starts in the sign-in process, so nothing hands a port over.

The app composes no platform and starts no agent, cron, channel or session: it is
a separate aiohttp application, not the dashboard with a gate closed. Its routes
are an allow-list (:data:`ALLOWED_ROUTES`); every other path answers 404 before
any handler or middleware runs, and the allowed routes still pass the dashboard's
own Host, CSRF and token checks. An ``oss`` build never reaches this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from types import SimpleNamespace
from typing import Any

from aiohttp import web

from kiro_crew import edition

logger = logging.getLogger(__name__)

#: The only address the sign-in app binds.
BIND_HOST = "127.0.0.1"

_GET = frozenset({"GET", "HEAD"})
_POST = frozenset({"POST"})

#: Exact paths the sign-in app serves, path -> methods. Nothing else is reachable.
ALLOWED_ROUTES: dict[str, frozenset[str]] = {
    # The SPA shell and the static files it draws the sign-in screen with.
    "/": _GET,
    "/logo.png": _GET,
    "/favicon.ico": _GET,
    "/api/theme/boot": _GET,
    # Liveness only. No readiness: this gateway is not ready for app traffic.
    "/api/health": _GET,
    "/api/live": _GET,
    # Tells the SPA to show the sign-in screen (``enterprise_sign_in_only``).
    "/api/kiro-prerequisite": _GET,
    # Crew's own organisation sign-in (Identity Center device code). Sign-out and
    # the browser-redirect flow, which serves only personal accounts, are not offered.
    "/api/kas-login": _GET,
    "/api/kas-login/device": _POST,
    "/api/kas-login/poll": _POST,
    "/api/kas-login/cancel": _POST,
}

#: Static bundle prefixes (GET/HEAD only).
ALLOWED_PREFIXES: tuple[str, ...] = ("/assets/", "/fonts/")

_KAS_ROUTES = {
    "/api/kas-login": "api_kas_login_status",
    "/api/kas-login/device": "api_kas_login_begin_device",
    "/api/kas-login/poll": "api_kas_login_poll",
    "/api/kas-login/cancel": "api_kas_login_cancel",
}

#: Seconds between checks for a stored sign-in.
_WATCH_INTERVAL_SECS = 2.0
#: Seconds left for the poll answer that stored the sign-in to reach the browser.
_DRAIN_SECS = 1.0
#: Longest wait for the page to read "signed in" before the app stops anyway.
_SEEN_WAIT_SECS = 15.0
#: Seconds before a printed link stops working at which a fresh one is printed.
_LINK_MARGIN_SECS = 30.0

_PREFIX_TAIL_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def allowed(method: str, path: str) -> bool:
    """Whether the sign-in app serves *method* *path*."""
    methods = ALLOWED_ROUTES.get(path)
    if methods is not None:
        return method in methods
    if method not in _GET or ".." in path:
        return False
    return path.startswith(ALLOWED_PREFIXES) and bool(_PREFIX_TAIL_RE.match(path))


def signed_in() -> bool:
    """Whether a sign-in the rules fetch can use exists now.

    Reads the one enterprise sign-in (:func:`kiro_crew.enterprise_sign_in.select`),
    the same answer the boot login check and the rules fetch read. Only its "no
    sign-in" refusal is "signed out": an unreadable vault, a lapsed sign-in or one
    with no profile counts as signed in, so it errors and exits as before instead of
    showing a sign-in screen that cannot fix it.
    """
    from kiro_crew import enterprise_sign_in as sign_in

    try:
        sign_in.select()
    except sign_in.SignInError as exc:
        return exc.cause != sign_in.CAUSE_NO_SIGN_IN
    except Exception:
        logger.debug("sign-in check failed; treated as signed in", exc_info=True)
    return True


#: Set by the desktop app on the gateway it spawns. Its window has no way yet to
#: mint a token from the sign-in app, so a desktop start still exits and shows the
#: refusal in its start-failure window.
DESKTOP_GATEWAY_ENV = "KIROCREW_DESKTOP_GATEWAY"


def applies(exc: BaseException) -> bool:
    """Whether the boot failure *exc* should get the sign-in screen, not an exit.

    Only when the failure itself is the missing sign-in (its recorded cause) AND
    nobody is signed in now. Any other composition failure exits as before.
    """
    from kiro_crew import enterprise_sign_in as sign_in
    from kiro_crew.platform.context import PlatformCompositionError

    if not edition.is_enterprise() or not isinstance(exc, PlatformCompositionError):
        return False
    # A process inside a Crew agent sandbox never serves a gateway of its own.
    if os.environ.get("KIROCREW_SANDBOX_ACTIVE") or os.environ.get(DESKTOP_GATEWAY_ENV):
        return False
    from kiro_crew.platform.enterprise_policy_fetch import last_failure_cause

    if last_failure_cause(str(exc)) != sign_in.CAUSE_NO_SIGN_IN:
        return False
    return not signed_in()


@web.middleware
async def _allow_list_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Outermost layer: anything off the allow-list is 404, including WebSockets."""
    if request.headers.get("Upgrade", "").lower() == "websocket" or not allowed(
        request.method, request.path
    ):
        raise web.HTTPNotFound(text='{"error":"not found"}', content_type="application/json")
    return await handler(request)


def start_command(start_args: tuple[str, ...] = (), *, platform: str | None = None) -> str:
    """The command that starts this install's gateway with *start_args*, ready to paste.

    It names this start's data home when one was chosen (``KIROCREW_HOME``), so the
    restart reads the vault that holds the new sign-in.
    """
    import shlex

    from kiro_crew import enterprise_sign_in as sign_in

    command = sign_in.render_command(
        [sign_in._launcher(), "gateway", *start_args], platform=platform
    )
    home = os.environ.get("KIROCREW_HOME", "")
    if not home:
        return command
    if (platform or sys.platform) == "win32":
        return f"$env:KIROCREW_HOME = {sign_in._ps_quote(home)}; {command}"
    return f"KIROCREW_HOME={shlex.quote(home)} {command}"


#: App key: the launch flags the restart command repeats.
_START_ARGS_KEY = "enterprise_start_args"


#: App key: an :class:`asyncio.Event` set once a sign-in is stored.
_SIGNED_IN_KEY = "enterprise_signed_in"
#: App key: an :class:`asyncio.Event` set once the page has read "signed in".
_SEEN_KEY = "enterprise_signed_in_seen"


async def _prerequisite_status(request: web.Request) -> web.Response:
    """GET /api/kiro-prerequisite: no data, only "show the sign-in screen".

    Once a sign-in is stored it also says ``enterprise_signed_in_restart``, so the
    page tells the user to start Kiro Crew again, and marks that it was read.
    """
    signed = request.app[_SIGNED_IN_KEY].is_set()
    extra: dict[str, str] = {}
    if signed:
        request.app[_SEEN_KEY].set()
        # The command that starts Kiro Crew again, for the page to show with a
        # copy button: the sign-in app is gone by the time the user acts.
        extra["enterprise_start_command"] = start_command(request.app.get(_START_ARGS_KEY, ()))
    return web.json_response(
        {
            **extra,
            "ready": False,
            "authenticated": False,
            "installed": True,
            "initial_setup_complete": False,
            "setup_allowed": True,
            "enterprise_login_required": True,
            "enterprise_sign_in_only": True,
            "enterprise_signed_in_restart": signed,
        }
    )


#: The only sign-in that yields an organisation profile the rules fetch accepts.
_ORGANISATION_PROVIDER = "idc"


def _kas_handler(name: str) -> Any:
    async def _dispatch(request: web.Request) -> web.StreamResponse:
        from kiro_crew.dashboard.handlers import kas_login

        if name == "api_kas_login_begin_device":
            try:
                body = await request.json()
            except Exception:
                body = None
            provider = body.get("provider") if isinstance(body, dict) else None
            if provider != _ORGANISATION_PROVIDER:
                # A personal account has no organisation profile, so the boot after
                # it would only exit. Refused before any sign-in starts.
                return web.json_response(
                    {
                        "error": "Sign in with your organisation's Company SSO.",
                        "code": "organisation_sign_in_required",
                    },
                    status=400,
                )
        return await getattr(kas_login, name)(request)

    return _dispatch


async def build_app(port: int, owner_id: str) -> web.Application:
    """The sign-in app for *port*: allow-listed routes behind the dashboard's checks."""
    # Through the dashboard facade: it composes the runtime namespace these
    # middleware and static helpers read at request time.
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard import server as facade
    from kiro_crew.dashboard.token_auth import token_auth_middleware
    from kiro_crew.dashboard.token_auth_tokens import warm_auth_singletons
    from kiro_crew.dashboard.urls import build_allowed_origins
    from kiro_crew.sel import warm_sel_singleton

    app = web.Application()
    app[_SIGNED_IN_KEY] = asyncio.Event()
    app[_SEEN_KEY] = asyncio.Event()
    # Read by the kas-login owner check (``is_owner_dashboard_request``).
    app["state"] = SimpleNamespace(owner_id=owner_id)
    app["allowed_origins"] = build_allowed_origins(port, True)
    app["local_only"] = True
    app["port"] = port
    await warm_auth_singletons()
    await warm_sel_singleton()
    caller = "dashboard_user"
    app.middlewares[:] = [
        _allow_list_middleware,
        facade._make_deny_audit_middleware(caller),
        facade._make_host_validation_middleware(caller),
        facade._make_csrf_middleware(caller),
        token_auth_middleware(port=port, local_only=True, spa_shell_handler=handlers.index),
    ]
    app.router.add_get("/", handlers.index)
    app.router.add_get("/logo.png", handlers.logo)
    app.router.add_get("/favicon.ico", handlers.logo)
    app.router.add_get("/api/theme/boot", handlers.api_theme_boot)
    app.router.add_get("/api/health", handlers.api_health)
    app.router.add_get("/api/live", handlers.api_live)
    app.router.add_get("/api/kiro-prerequisite", _prerequisite_status)
    for path, name in _KAS_ROUTES.items():
        add = app.router.add_get if ALLOWED_ROUTES[path] == _GET else app.router.add_post
        add(path, _kas_handler(name))
    from kiro_crew.dashboard.handlers.core import _DIST_DIR as dist_dir

    app.router.add_get("/assets/{tail:.+}", facade._dist_file_handler(dist_dir, "assets"))
    app.router.add_get("/fonts/{tail:.+}", facade._dist_file_handler(dist_dir, "fonts"))

    async def _close_kas(app_: web.Application) -> None:
        service = app_.get("kas_login_service")
        if service is not None:
            await service.close()

    app.on_cleanup.append(_close_kas)
    return app


async def serve_until_signed_in(
    port: int, owner_id: str, *, no_open: bool, start_args: tuple[str, ...] = ()
) -> int:
    """Serve the sign-in app on 127.0.0.1:*port* until a sign-in is stored.

    *port* 0 picks a free port. Once a sign-in is stored the page is told to start
    Kiro Crew again; the app waits briefly for it to read that, then stops, and its
    socket closes with it. Returns the port served.
    """
    from kiro_crew import enterprise_sign_in as sign_in
    from kiro_crew.dashboard import token_auth_tokens as tokens
    from kiro_crew.dashboard.boot_id import current_boot_id
    from kiro_crew.dashboard.urls import build_dashboard_url
    from kiro_crew.frontend import ensure_dev_dist_symlink

    # A source tree serves its built frontend only once it is staged; the normal
    # gateway does this in ``_gateway``, which does not run here.
    await asyncio.to_thread(ensure_dev_dist_symlink)
    from kiro_crew.dashboard import server as facade

    # The dashboard's own reservation: exclusive on Windows and listening at once,
    # so no other local process can share the port and take the token below.
    sock = await asyncio.to_thread(facade._bind_once, BIND_HOST, port)
    port = sock.getsockname()[1]
    try:
        app = await build_app(port, owner_id)
        signed, seen = app[_SIGNED_IN_KEY], app[_SEEN_KEY]
        app[_START_ARGS_KEY] = start_args
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
    except BaseException:
        sock.close()
        raise
    try:
        await web.SockSite(runner, sock).start()

        def link() -> str:
            # Bound to this process (the ``boot`` claim, carried into the session
            # cookie): once the sign-in process exits, its session is worthless,
            # and no other session in this data home is touched.
            token = tokens.generate_token(
                owner_id or "local-startup",
                ttl_seconds=tokens.MAX_SESSION_TTL_SECS,
                extra={"boot": current_boot_id()},
            )
            # The bound address itself, never ``localhost``: that name can resolve
            # to ``::1``, where another process could listen and take the token.
            return build_dashboard_url(f"http://{BIND_HOST}:{port}", token)

        # A link is accepted only within ``LINK_WINDOW_SECS``. Someone who opens it
        # later (``--no-open``, a headless start) gets the next one printed here.
        renew_every = max(tokens.LINK_WINDOW_SECS - _LINK_MARGIN_SECS, 0.0)
        url = link()
        renew_at = time.monotonic() + renew_every
        print(f"🔑 Sign in to Kiro to start Kiro Crew: {url}", flush=True)
        if not no_open:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        while not await asyncio.to_thread(signed_in):
            await asyncio.sleep(_WATCH_INTERVAL_SECS)
            if time.monotonic() >= renew_at:
                url, renew_at = link(), time.monotonic() + renew_every
                print(f"🔑 The last sign-in link is about to expire. New link: {url}", flush=True)
        # Only a sign-in the next start can use is published as success: a lapsed,
        # profile-less or unreadable one raises ``SignInError`` here instead, and
        # the page is never told "signed in".
        await asyncio.to_thread(sign_in.resolve)
        signed.set()
        # Give the page one status read to show "start again"; the terminal line
        # says the same if it never reads it.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(seen.wait(), _SEEN_WAIT_SECS)
        await asyncio.sleep(_DRAIN_SECS)
    finally:
        # The site owns *sock*: stopping it closes the socket. Closing again is a no-op.
        await runner.cleanup()
        sock.close()
    return port


def sign_in_then_exit(
    exc: BaseException,
    *,
    port: int,
    owner_id: str,
    no_open: bool,
    start_args: tuple[str, ...] = (),
) -> None:
    """Show the sign-in screen for *exc* if it applies; after a sign-in, exit 0.

    Returns, having done nothing, when *exc* is not a signed-out enterprise boot.
    Otherwise it never returns. After a usable sign-in it exits 0, so the next
    start loads the rules with that sign-in; no gateway starts in this process,
    and the sign-in page's session (bound to this process) ends with it.
    """
    if not applies(exc):
        return
    from kiro_crew import enterprise_sign_in as sign_in
    from kiro_crew.enterprise_boot import fix_for, refuse_boot

    try:
        with asyncio.Runner() as loop_runner:
            loop_runner.run(
                serve_until_signed_in(port, owner_id, no_open=no_open, start_args=start_args)
            )
    except sign_in.SignInError as unusable:
        # The sign-in that landed is one the next start could not use either.
        refuse_boot(str(unusable), fix_for(unusable.cause))
    except KeyboardInterrupt:
        # Ctrl-C at the sign-in screen is a plain stop, not a crash.
        print("\n👻 Stopped before sign-in.", file=sys.stderr, flush=True)
        raise SystemExit(130) from None
    except (OSError, OverflowError) as err:
        detail = getattr(err, "strerror", None) or err
        refuse_boot(
            f"the sign-in screen could not open on {BIND_HOST}:{port} ({detail})",
            "Free the port or pass `--port`, then start Kiro Crew again.",
        )
    print(f"✅ Signed in. Start Kiro Crew again: {start_command(start_args)}", flush=True)
    raise SystemExit(0)
