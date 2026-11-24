"""Social login via the loopback-callback (PKCE) flow — desktop / local install shapes.

Used when the browser and the gateway share a machine (or a container with the callback
port mapped to host loopback). The flow drives Kiro's own portal exactly as kiro-cli does
and exchanges the returned code at the social service:

  open  {portal}/signin?state=…&code_challenge=…&code_challenge_method=S256
                        &redirect_uri=http://localhost:<port>&redirect_from=kirocli
  callback  http://localhost:<port>/oauth/callback?login_option=<p>&code=…&state=…
  exchange  POST {service}/oauth/token  {code, code_verifier, redirect_uri}

The callback listener binds only a Cognito-allowlisted loopback port (the auth service
rejects any other redirect URI). ``redirect_uri`` sent to /oauth/token must equal the
callback URL rebuilt as ``http://localhost:<port><path>?login_option=<p>`` (kiro-cli
portal.rs handle_social_callback).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import logging
import secrets
import socket
import string
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import TypeVar

import aiohttp
from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.auth.login.endpoints import (
    CALLBACK_PORTS,
    USER_AGENT,
    portal_url,
    social_service_url,
)
from kiro_crew.auth.store import KasToken, SocialProvider
from kiro_crew.http_proxy import resolve_proxy

logger = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json", "User-Agent": USER_AGENT}

_T = TypeVar("_T")

# The tab's answer once the code exchange has run. Fixed words: the exchange error
# can carry the auth service's response body, which never reaches the page.
_SIGNED_IN_PAGE = (
    "Login complete. You can close this window and go back to the Kiro Crew dashboard."
)
_EXCHANGE_FAILED_PAGE = (
    "Sign-in could not finish: the sign-in service did not complete it. Go back to "
    "the Kiro Crew dashboard and finish signing in with the code shown there."
)
# A hit after the first outcome, and a tab of a sign-in a newer one replaced.
_FINISHED_PAGE = (
    "This sign-in has already finished. Go back to the Kiro Crew dashboard to see the result."
)
_REPLACED_PAGE = (
    "This sign-in was replaced by a newer one. You can close this window and finish "
    "signing in from the Kiro Crew dashboard."
)
# Every page the tab lands on is titled, so the tab does not read as an untitled page.
_PAGE_HEAD = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='color-scheme' content='light dark'><title>Kiro Crew sign-in</title>"
    "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:36em;margin:3em auto;"
    "padding:0 1em}</style></head><body>"
)

# Close tasks of signed-in listeners still answering late tabs (held against GC).
_CLOSING: set[asyncio.Task[None]] = set()

# The failed page's reason for each RFC 6749 section 4.1.2.1 error code, in fixed
# words: the callback request is untrusted and error_description is developer text,
# so nothing the request carries is put on the page.
_PORTAL_ERROR_REASONS = {
    "access_denied": "the sign-in was cancelled or not approved",
    "server_error": "the sign-in service ran into a problem",
    "temporarily_unavailable": "the sign-in service is temporarily unavailable",
    **dict.fromkeys(
        ("invalid_request", "unauthorized_client", "unsupported_response_type", "invalid_scope"),
        "the sign-in service did not accept the request",
    ),
}


class PortalAuthError(Exception):
    """Loopback portal login failed."""


class PortalTimeoutError(PortalAuthError):
    """No callback arrived before the listener deadline.

    Distinct so the caller can degrade to the device-code flow (the browser is
    probably not on this machine) rather than report a generic failure.
    """


class PortalAddressError(PortalAuthError):
    """The browser came back with this sign-in's state to a path not listened on.

    Distinct so the caller can say so: the browser did reach this machine.
    """


def generate_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state(n: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def bind_allowed_port() -> tuple[socket.socket, int]:
    """Bind the first free Cognito-allowlisted loopback port (kiro-cli bind_allowed_port)."""
    last_err: OSError | None = None
    for port in CALLBACK_PORTS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if platform_compat.IS_POSIX:
            # Lets a restart rebind through TIME_WAIT; a live listener still refuses it.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # On Windows two listeners that both set SO_REUSEADDR share one address, so the
            # browser's callback can reach either. SO_EXCLUSIVEADDRUSE makes a held port fail
            # the bind, and the loop moves on to the next allowlisted port.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            # Listen immediately: the browser can be redirected back before the
            # serving task has started, and a bound-but-not-listening socket
            # refuses that connection instead of queueing it in the backlog.
            sock.listen(8)
            return sock, port
        except OSError as err:
            last_err = err
            sock.close()
    raise PortalAuthError(f"all callback ports in use; last error: {last_err}")


def build_auth_url(port: int, state: str, challenge: str) -> str:
    redirect_base = f"http://localhost:{port}"
    return (
        f"{portal_url()}/signin"
        f"?state={state}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
        f"&redirect_uri={urllib.parse.quote(redirect_base, safe='')}"
        f"&redirect_from=kirocli"
    )


def _page(text: str, status: int = 200) -> web.Response:
    """The tab's answer: *text* on the titled sign-in page."""
    body = f"{_PAGE_HEAD}<p>{html.escape(text)}</p></body></html>"
    return web.Response(status=status, text=body, content_type="text/html")


def rebuild_redirect_uri(port: int, callback_path: str, login_option: str) -> str:
    """Reproduce the redirect_uri kiro-cli sends to /oauth/token (must match exactly)."""
    return (
        f"http://localhost:{port}{callback_path}"
        f"?login_option={urllib.parse.quote(login_option, safe='')}"
    )


async def exchange_code(
    provider: SocialProvider,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    *,
    session: aiohttp.ClientSession,
) -> KasToken:
    """Exchange the authorization code for a token at {service}/oauth/token."""
    url = f"{social_service_url()}/oauth/token"
    payload = {"code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri}
    async with session.post(url, json=payload, headers=_HEADERS, proxy=resolve_proxy(url)) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise PortalAuthError(f"token exchange failed: HTTP {resp.status} {body}")
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as err:
            raise PortalAuthError("token exchange returned an undecodable body") from err
    if not isinstance(data, dict):
        raise PortalAuthError("token exchange returned a non-object body")

    access_token = data.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise PortalAuthError("token exchange returned no access token")
    profile_arn = data.get("profileArn")
    if not profile_arn:
        raise PortalAuthError("token exchange returned no profile ARN")
    try:
        expires_in = int(data.get("expiresIn") or 3600)
    except (TypeError, ValueError) as err:
        raise PortalAuthError("token exchange returned a non-numeric expiry") from err
    return KasToken(
        access_token=access_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        provider=provider.value,
        identity="social",
        refresh_token=data.get("refreshToken"),
        profile_arn=profile_arn,
    )


async def wait_for_callback(
    sock: socket.socket,
    port: int,
    expected_state: str,
    finish: Callable[[dict], Awaitable[_T]],
    timeout_secs: float = 600,
) -> _T:
    """Serve the pre-bound loopback socket until the portal redirects back, then finish.

    Accepts GET on ``/oauth/callback`` or ``/signin/callback`` (kiro-cli honours
    both) and hands ``{login_option, code, state, path}`` to ``finish``, the token
    exchange, returning what it returns. The tab's reply waits for ``finish``, so
    it says "Login complete" only once the exchange has succeeded, and names a
    failure when it raises. ``finish`` runs as its own task, so a tab closed early
    does not stop it. Any other GET path carrying this sign-in's state fails the
    sign-in at once. Raises PortalAuthError on a portal-reported error, a callback
    with no state or no code, or timeout, and whatever ``finish`` raises. A
    callback carrying another sign-in's state (the CSRF check) is answered and
    changes nothing. ``timeout_secs`` bounds the wait for the callback; ``finish``
    brings its own bound. The socket is taken over by aiohttp and closed with the
    listener: at once on a failure, timeout or cancel; after a sign-in at the
    deadline, or sooner by :func:`close_finished_listeners`, so a second tab of
    this sign-in still gets a page.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    # Resolves to the running exchange once a valid callback arrives.
    result: asyncio.Future[asyncio.Future[_T]] = loop.create_future()
    reason = ""

    async def _exchange_page(exchange: asyncio.Future[_T]) -> web.Response:
        try:
            await asyncio.shield(exchange)
        except asyncio.CancelledError:
            if not exchange.cancelled():
                raise
            text = _EXCHANGE_FAILED_PAGE
        except Exception:  # noqa: BLE001 - the caller reports the error itself
            text = _EXCHANGE_FAILED_PAGE
        else:
            text = _SIGNED_IN_PAGE
        return _page(text)

    async def _handle(request: web.Request) -> web.Response:
        nonlocal reason
        # First terminal outcome wins; a later hit (a refresh, a second tab) is told so.
        if result.done():
            return _page(_FINISHED_PAGE)
        query = request.query
        state = query.get("state", "")
        if state and not hmac.compare_digest(state.encode(), expected_state.encode()):
            # An older sign-in's tab, back on this reused port: its code is never
            # exchanged, and this sign-in keeps waiting for its own tab.
            return _page(_REPLACED_PAGE)
        error = query.get("error")
        if error:
            reason = _PORTAL_ERROR_REASONS.get(error, "")
            result.set_exception(PortalAuthError(f"portal returned error: {error}"))
        elif not state:
            result.set_exception(PortalAuthError("portal callback carried no state"))
        elif not query.get("code"):
            result.set_exception(PortalAuthError("portal callback carried no code"))
        else:
            callback = {
                "login_option": query.get("login_option", ""),
                "code": query["code"],
                "state": state,
                "path": request.path,
            }
            result.set_result(asyncio.ensure_future(finish(callback)))
        if result.exception() is None:
            return await _exchange_page(result.result())
        failed = f"Login failed: {reason}." if reason else "Login failed."
        return _page(f"{failed} You can close this window and retry from the Kiro Crew dashboard.")

    async def _unexpected_path(request: web.Request) -> web.Response:
        # The token exchange must echo the exact redirect path, so a near-miss
        # (trailing slash, letter case) cannot be accepted. When the hit carries
        # this sign-in's state it is the portal's redirect, so fail now instead of
        # leaving the dashboard to wait out the timeout. Stray hits without the
        # state (favicon, a port probe) only get the page and change nothing.
        state = request.query.get("state", "")
        if not hmac.compare_digest(state.encode(), expected_state.encode()):
            return web.Response(status=404, text="Not found.", content_type="text/plain")
        if not result.done():
            result.set_exception(
                PortalAddressError(
                    f"the sign-in page sent the browser back to {request.path!r}, "
                    "which is not a callback address this computer listens on"
                )
            )
        return _page(
            "Sign-in could not finish here: the browser came back to an address this "
            "computer does not handle. Go back to the Kiro Crew dashboard and finish "
            "signing in with the code shown there.",
            status=404,
        )

    app = web.Application()
    app.router.add_get("/oauth/callback", _handle)
    app.router.add_get("/signin/callback", _handle)
    app.router.add_get("/{tail:.*}", _unexpected_path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()
    logger.debug("waiting for portal callback on loopback port %d", port)
    exchange: asyncio.Future[_T] | None = None
    signed_in = False
    try:
        try:
            exchange = await asyncio.wait_for(asyncio.shield(result), timeout=timeout_secs)
        except asyncio.TimeoutError:
            raise PortalTimeoutError(
                f"timed out after {timeout_secs:.0f}s waiting for the portal callback"
            ) from None
        finished = await exchange
        signed_in = True
        return finished
    finally:
        if not result.done():
            # A callback landing during shutdown then starts no exchange.
            result.cancel()
        elif exchange is None and not result.cancelled() and result.exception() is None:
            exchange = result.result()
        if exchange is not None:
            # A sign-in abandoned mid-exchange stops it; a no-op once it finished.
            exchange.cancel()
        if signed_in:
            _close_later(runner, deadline - loop.time())
        else:
            # Waits for the in-flight tab reply, so the page the exchange chose goes out.
            await runner.cleanup()


def _close_later(runner: web.AppRunner, delay: float) -> None:
    """Close a signed-in listener ``delay`` seconds from now, off the caller's path."""

    async def _close() -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            await runner.cleanup()

    # Eager: it is inside its ``try`` before anyone can cancel it, so the close runs.
    task = asyncio.eager_task_factory(asyncio.get_running_loop(), _close())
    _CLOSING.add(task)
    task.add_done_callback(_CLOSING.discard)


async def close_finished_listeners() -> None:
    """Close every signed-in listener still answering late tabs, and wait until closed."""
    tasks = list(_CLOSING)
    for task in tasks:
        # A second cancel would interrupt the cleanup a first one started.
        if not task.cancelling():
            task.cancel()
    if tasks:
        await asyncio.wait(tasks)
