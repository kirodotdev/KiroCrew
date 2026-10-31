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
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone

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


def build_auth_url(
    port: int,
    state: str,
    challenge: str,
    *,
    signin_methods: str = "",
    external_idp: Mapping[str, str] | None = None,
) -> str:
    """The portal ``/signin`` URL for this listener.

    *signin_methods* narrows the choices the portal offers (``external_idp`` for the
    organization sign-in). *external_idp* adds the portal's optional lookup hints
    (``external_idp_domain``, ``external_idp_start_url``, ``external_idp_region``);
    only those keys are sent, and only when non-empty.
    """
    redirect_base = f"http://localhost:{port}"
    url = (
        f"{portal_url()}/signin"
        f"?state={state}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
        f"&redirect_uri={urllib.parse.quote(redirect_base, safe='')}"
        f"&redirect_from=kirocli"
    )
    extra: dict[str, str] = {}
    if signin_methods:
        extra["signin_methods"] = signin_methods
    for key in EXTERNAL_IDP_HINT_KEYS:
        value = (external_idp or {}).get(key, "")
        if value:
            extra[key] = value
    if extra:
        url += "&" + urllib.parse.urlencode(extra)
    return url


#: The portal's optional organization-lookup hints for the external_idp sign-in.
EXTERNAL_IDP_HINT_KEYS = ("external_idp_domain", "external_idp_start_url", "external_idp_region")

#: ``login_option`` value of the portal's external_idp hand-off leg.
EXTERNAL_IDP_LOGIN_OPTION = "external_idp"

#: Callback path the customer IdP redirects back to with the authorization code.
IDP_CALLBACK_PATH = "/oauth/callback"

#: Answers the portal's external_idp leg: given its query, return the IdP
#: authorization URL to redirect the browser to and the ``state`` that URL carries.
IdpHandoff = Callable[[Mapping[str, str]], Awaitable[tuple[str, str]]]


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
    *,
    on_idp_handoff: IdpHandoff | None = None,
) -> dict:
    """Serve the pre-bound loopback socket until the portal redirects back.

    Accepts GET on ``/oauth/callback`` or ``/signin/callback`` (kiro-cli honours
    both), returning ``{login_option, code, state}`` for the token exchange. Raises
    PortalAuthError on a portal-reported error, a state mismatch (the CSRF check —
    someone other than our browser tab hit the listener), or timeout. The socket is
    taken over by aiohttp for the duration and closed with the listener.

    With *on_idp_handoff* the listener runs the two-leg organization sign-in. The
    first hit must be the portal's ``login_option=external_idp`` leg (our state, no
    code); it is answered with a 302 to the URL *on_idp_handoff* returns, and from
    then on only the state that URL carries is accepted. The second hit is the
    customer IdP's redirect with the code. A code before the hand-off, or a second
    hand-off, fails the login.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[dict] = loop.create_future()
    expected = [expected_state]
    handed_off = [False]
    # Serialises hits: the hand-off awaits discovery, and a second request must not
    # read the state or the hand-off flag while the first is still answering.
    serial = asyncio.Lock()

    def _fail(message: str) -> None:
        if not result.done():
            result.set_exception(PortalAuthError(message))

    async def _idp_leg(query: Mapping[str, str]) -> None:
        """The two-leg branch. Continues the flow by raising the 302 to the IdP."""
        assert on_idp_handoff is not None
        if not handed_off[0]:
            if query.get("code") or query.get("login_option") != EXTERNAL_IDP_LOGIN_OPTION:
                _fail("portal callback was not the organization sign-in hand-off")
                return
            try:
                redirect_url, next_state = await on_idp_handoff(query)
            except Exception as err:  # noqa: BLE001 - every hand-off failure ends the login
                _fail(f"organization sign-in hand-off failed: {err}")
                return
            handed_off[0] = True
            expected[0] = next_state
            raise web.HTTPFound(redirect_url)
        if not query.get("code"):
            _fail("identity provider callback carried no code")
            return
        result.set_result(
            {
                "login_option": EXTERNAL_IDP_LOGIN_OPTION,
                "code": query["code"],
                "state": query["state"],
                "path": IDP_CALLBACK_PATH,
            }
        )

    async def _handle(request: web.Request) -> web.Response:
        # First terminal outcome wins; later hits (refresh, favicon retry) are inert.
        async with serial:
            if not result.done():
                query = request.query
                error = query.get("error")
                if error:
                    _fail(f"portal returned error: {error[:200]}")
                elif query.get("state") != expected[0]:
                    _fail("state mismatch on portal callback")
                elif on_idp_handoff is not None:
                    await _idp_leg(query)
                elif not query.get("code"):
                    _fail("portal callback carried no code")
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
