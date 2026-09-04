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
import logging
import secrets
import socket
import string
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

from kiro_crew.auth.login.endpoints import (
    CALLBACK_PORTS,
    USER_AGENT,
    portal_url,
    social_service_url,
)
from kiro_crew.auth.store import KasToken, SocialProvider

logger = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json", "User-Agent": USER_AGENT}


class PortalAuthError(Exception):
    """Loopback portal login failed."""


class PortalTimeoutError(PortalAuthError):
    """No callback arrived before the listener deadline.

    Distinct so the caller can degrade to the device-code flow (the browser is
    probably not on this machine) rather than report a generic failure.
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
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
    async with session.post(url, json=payload, headers=_HEADERS) as resp:
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
    timeout_secs: float = 600,
) -> dict:
    """Serve the pre-bound loopback socket until the portal redirects back.

    Accepts GET on ``/oauth/callback`` or ``/signin/callback`` (kiro-cli honours
    both), returning ``{login_option, code, state}`` for the token exchange. Raises
    PortalAuthError on a portal-reported error, a state mismatch (the CSRF check —
    someone other than our browser tab hit the listener), or timeout. The socket is
    taken over by aiohttp for the duration and closed with the listener.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[dict] = loop.create_future()

    async def _handle(request: web.Request) -> web.Response:
        # First terminal outcome wins; later hits (refresh, favicon retry) are inert.
        if not result.done():
            query = request.query
            error = query.get("error")
            if error:
                result.set_exception(PortalAuthError(f"portal returned error: {error}"))
            elif query.get("state") != expected_state:
                result.set_exception(PortalAuthError("state mismatch on portal callback"))
            elif not query.get("code"):
                result.set_exception(PortalAuthError("portal callback carried no code"))
            else:
                result.set_result(
                    {
                        "login_option": query.get("login_option", ""),
                        "code": query["code"],
                        "state": query["state"],
                        "path": request.path,
                    }
                )
        done = result.done() and not result.cancelled() and result.exception() is None
        text = (
            "Login complete. You can close this window."
            if done
            else "Login failed. You can close this window and retry from the dashboard."
        )
        return web.Response(text=text, content_type="text/html")

    app = web.Application()
    app.router.add_get("/oauth/callback", _handle)
    app.router.add_get("/signin/callback", _handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()
    logger.debug("waiting for portal callback on loopback port %d", port)
    try:
        return await asyncio.wait_for(asyncio.shield(result), timeout=timeout_secs)
    except asyncio.TimeoutError:
        raise PortalTimeoutError(
            f"timed out after {timeout_secs:.0f}s waiting for the portal callback"
        ) from None
    finally:
        await runner.cleanup()
