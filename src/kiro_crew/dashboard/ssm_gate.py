"""The SSM listener: a loopback port for AWS Systems Manager port forwards.

A Session Manager port forward delivers each connection to the gateway as a
plain loopback connection with no forwarding header, which is exactly what the
owner's own browser looks like. So the forward gets a port of its own
(``dashboard.shared_crew.ssm_port``), bound on ``127.0.0.1`` beside the
dashboard's, and a request is an SSM request because of the port it arrived on,
never because of anything it says.

Every request on that port is decided here and never reaches the token flow:

* the only credential is a Crew invite (``shared_crew.ssm_invites``), presented
  once as ``?ssm_invite=<id>.<secret>`` and kept in an ``HttpOnly`` cookie;
* a dashboard token, an app token and the internal secret mean nothing on this
  port, so a forwarded connection can never be the owner (the owner subject is
  never assigned here, and ``Provenance.SSM_INVITE`` never satisfies an owner
  gate);
* strict internal paths are refused;
* an invite that is revoked stops working on the next request, and an open
  WebSocket or event stream is closed within :data:`REVERIFY_SECS`.

The middleware chain is built before the bind with an empty :class:`GateRef`,
as for the direct tailnet listener.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.shared_crew import ssm, ssm_invites
from kiro_crew.shared_crew.contracts import SSM_GUEST_REQUEST_KEY, SSM_REQUEST_KEY

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.config.sections_shared_crew import SharedCrewConfig

logger = logging.getLogger(__name__)

COOKIE_PREFIX = "mc_ssm_"
INVITE_QUERY = "ssm_invite"
#: What an issued credential looks like (hex id, dot, URL-safe secret). Anything else is
#: refused before it is looked up, so no other text is ever written into the cookie.
_CREDENTIAL_SHAPE = re.compile(r"[0-9a-f]{1,64}\.[A-Za-z0-9_-]{1,128}")
#: How often an open WebSocket or event stream checks that its invite is still live.
REVERIFY_SECS = 15.0
CODE_REFUSED = "ssm_refused"

REFUSE_NOT_INVITED = "not_invited"
REFUSE_INTERNAL = "internal_path"

_REFUSAL_TEXT = {
    REFUSE_NOT_INVITED: "Open the invite link the Crew owner sent you.",
    REFUSE_INTERNAL: "This path is not available here.",
}


@dataclass
class SsmGate:
    """Decides every request that arrives on the SSM listener."""

    port: int

    def owns(self, request: web.Request) -> bool:
        """Whether *request* arrived on the SSM listener's loopback port."""
        transport = getattr(request, "transport", None)
        if transport is None:
            return False
        try:
            info = transport.get_extra_info("sockname")
        except Exception:  # noqa: BLE001 - an unreadable socket address owns nothing
            return False
        if not isinstance(info, (tuple, list)) or len(info) < 2:
            return False
        host, port = info[0], info[1]
        if not isinstance(host, str) or port != self.port:
            return False
        try:
            return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            return False


@dataclass
class GateRef:
    """The SSM gate the middleware chain reads, filled in once the port is bound."""

    gate: SsmGate | None = None
    #: Set, never cleared, when a live ``dashboard_origins`` policy stops permitting the
    #: SSM origins after the bind. The gate stays: a request on this port must still be
    #: judged by it, never fall through to the loopback owner path. Only the origin
    #: trust and the management routes are withdrawn (``origin.withdraw_refused_origins``).
    withdrawn: bool = False


def refusal_response(request: web.Request, reason: str) -> web.Response:
    """A 403 that says what to do, as a page for a browser and JSON for everything else."""
    import html

    from aiohttp import web

    message = _REFUSAL_TEXT.get(reason, _REFUSAL_TEXT[REFUSE_NOT_INVITED])
    if request.method in ("GET", "HEAD") and "text/html" in request.headers.get("Accept", ""):
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>Kiro Crew: access refused</title></head>"
            '<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem">'
            '<h1 style="font-size: 1.25rem">You can\'t open this Crew</h1>'
            f"<p>{html.escape(message)}</p></body></html>"
        )
        return web.Response(status=403, text=body, content_type="text/html")
    return web.json_response(
        {"error": message, "code": CODE_REFUSED, "reason": reason},
        status=403,
        headers={"Cache-Control": "no-store"},
    )


def cookie_name(request: web.Request) -> str:
    """``mc_ssm_<port>``, keyed by the port the browser connects to.

    A browser scopes cookies by host, not port, so a teammate who joins two Crews
    through localhost forwards would otherwise have the second invite replace the
    first. The Host header carries the browser-visible port; the listener's own
    port stands in when it does not.
    """
    from kiro_crew.dashboard.token_auth_tokens import _cookie_port_from_host

    info = None
    transport = getattr(request, "transport", None)
    if transport is not None:
        try:
            info = transport.get_extra_info("sockname")
        except Exception:  # noqa: BLE001 - an unreadable socket address falls back to 0
            info = None
    own = info[1] if isinstance(info, (tuple, list)) and len(info) >= 2 else 0
    return f"{COOKIE_PREFIX}{_cookie_port_from_host(request, own)}"


def _invite_redirect(request: web.Request, credential: str) -> web.Response:
    """Keep the invite in a cookie and send the browser to the same page without it."""
    from aiohttp import web

    # A path that opens with two slashes would read as another host to the browser.
    target = request.rel_url.with_query(
        {k: v for k, v in request.rel_url.query.items() if k != INVITE_QUERY}
    ).with_path("/" + request.rel_url.path.lstrip("/"))
    response = web.Response(
        status=302,
        headers={
            "Location": str(target),
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )
    # Lax, not Strict: an invite is opened by a click from mail or chat, and a browser
    # withholds a Strict cookie from the redirected request of a cross-site navigation, so
    # the page it lands on would refuse the guest. Lax still holds back the cookie from
    # cross-site POSTs and subresources; the origin allowlist covers writes as before.
    response.set_cookie(cookie_name(request), credential, httponly=True, samesite="Lax", path="/")
    return response


async def _run_with_reverify(
    request: web.Request,
    credential: str,
    guest: ssm_invites.SsmGuest,
    handler: Callable[[web.Request], Awaitable[Any]],
    *,
    interval: float | None = None,
) -> Any:
    """Run *handler*, closing the connection when *guest*'s invite stops verifying."""

    async def _watch() -> None:
        while True:
            await asyncio.sleep(REVERIFY_SECS if interval is None else interval)
            if ssm_invites.verify(credential) != guest:
                logger.info("ssm invite no longer verifies; closing its connection")
                transport = getattr(request, "transport", None)
                if transport is not None:
                    transport.close()
                return

    watcher = asyncio.create_task(_watch())
    try:
        return await handler(request)
    finally:
        watcher.cancel()


async def admit(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[Any]],
    *,
    strict_internal: bool,
    log_auth: Callable[[web.Request, str, str, str], None],
    publish: Callable[[web.Request], Awaitable[Any]],
) -> web.StreamResponse:
    """Decide *request*, which arrived on the SSM port, and run *handler* when it is admitted.

    *strict_internal* is whether the path is machine-to-machine; *log_auth*
    writes the audit record; *publish* publishes the Shared Crew actor.
    """
    from kiro_crew.dashboard.origin import mark_audit_claimed

    if strict_internal or "X-Internal-Secret" in request.headers:
        mark_audit_claimed(request)
        log_auth(request, "", "denied", "ssm: internal path")
        return refusal_response(request, REFUSE_INTERNAL)
    offered = request.query.get(INVITE_QUERY) or ""
    if offered:
        opened = ssm_invites.verify(offered) if _CREDENTIAL_SHAPE.fullmatch(offered) else None
        if opened is None:
            mark_audit_claimed(request)
            log_auth(request, "", "denied", "ssm: invite refused")
            return refusal_response(request, REFUSE_NOT_INVITED)
        # Redeeming an invite mints the guest's cookie, so it is a grant in its own
        # right. The redirect it returns never reaches the audit middleware.
        log_auth(request, f"ssm:{opened.invite_id}", "granted", "ssm invite redeemed")
        await asyncio.to_thread(ssm_invites.mark_joined, opened.invite_id)
        return _invite_redirect(request, offered)
    credential = request.cookies.get(cookie_name(request), "")
    guest = ssm_invites.verify(credential)
    if guest is None:
        mark_audit_claimed(request)
        log_auth(request, "", "denied", "ssm: no valid invite")
        return refusal_response(request, REFUSE_NOT_INVITED)
    subject = f"ssm:{guest.invite_id}"
    request["user"] = subject
    request["app"] = ""
    request["auth_token"] = ""
    request[SSM_REQUEST_KEY] = True
    request[SSM_GUEST_REQUEST_KEY] = guest
    request["is_dashboard_user"] = True
    request["auth_from_query_token"] = False
    log_auth(request, subject, "granted", "ssm invite")
    await publish(request)
    return await _run_with_reverify(request, credential, guest, handler)  # type: ignore[no-any-return]


def origins(port: int) -> set[str]:
    """The browser origins the SSM listener serves, trusted only for a request that arrived on its port."""
    from kiro_crew.dashboard.urls import dashboard_origin

    return {
        o for o in (dashboard_origin(f"http://{h}:{port}") for h in ("localhost", "127.0.0.1")) if o
    }


def _origins_permitted(port: int) -> bool:
    """Whether the ``dashboard_origins`` policy lets every SSM origin be trusted. Blocking."""
    from kiro_crew.dashboard.urls import origin_permitted

    return all(origin_permitted(o, audit_tool="ssm_listener") for o in sorted(origins(port)))


def resolve(cfg: SharedCrewConfig, dashboard_port: int) -> tuple[ssm.Listener, SsmGate | None]:
    """Decide whether the SSM listener runs. Blocking: it may read instance metadata."""
    if not cfg.ssm:
        return ssm.Listener(ssm.STATE_OFF, ssm.REASON_DISABLED), None
    port = cfg.ssm_port
    if port == dashboard_port:
        return ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_PORT_IN_USE, port), None
    if not _origins_permitted(port):
        # An enterprise ``dashboard_origins`` policy that does not name the SSM
        # origins is a ceiling: no listener, and no origin added to the allowlists.
        return ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_GOVERNED, port), None
    # Request-path checks answer from memory, so the store is read here, off the loop.
    ssm_invites.warm()
    target = ssm.detect_target()
    if target is None:
        return ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_NO_TARGET, port), None
    return ssm.Listener(ssm.STATE_ACTIVE, "", port, target), SsmGate(port=port)


async def start_after_bind(
    app: web.Application,
    runner: web.AppRunner,
    cfg: SharedCrewConfig,
    dashboard_port: int,
    ref: GateRef,
) -> list[web.BaseSite]:
    """After the dashboard bind: resolve the listener, bind ``127.0.0.1:<ssm_port>`` and record the result.

    Never raises: the listener is an addition, and its failure adds nothing.
    """
    from aiohttp import web

    try:
        answer, gate = await asyncio.to_thread(resolve, cfg, dashboard_port)
    except Exception:  # noqa: BLE001
        logger.warning("ssm listener could not be resolved", exc_info=True)
        answer, gate = ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_NO_TARGET, cfg.ssm_port), None
    if gate is not None:
        # The gate is set before the site accepts a connection and withdrawn if
        # the bind fails, so a connection on this port is never served unchecked.
        ref.gate = gate
        site = web.TCPSite(runner, "127.0.0.1", gate.port, reuse_address=os.name == "posix")
        try:
            await site.start()
        except OSError as exc:
            logger.warning("ssm listener could not bind 127.0.0.1:%d: %s", gate.port, exc)
            ref.gate = None
            answer = ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_BIND_FAILED, gate.port)
        else:
            # The origin and Host checks read these per request, only for a request that
            # arrived on this port (``origin._ssm_origins_for``), never the shared set.
            app["ssm_gate_ref"] = ref
            logger.info("ssm listener on 127.0.0.1:%d (invite-only)", gate.port)
            ssm.note_listener(answer)
            return [site]
    ssm.note_listener(answer)
    if answer.state != ssm.STATE_OFF:
        logger.info("ssm listener %s (%s)", answer.state, answer.reason)
    return []


__all__ = [
    "COOKIE_PREFIX",
    "GateRef",
    "INVITE_QUERY",
    "SSM_REQUEST_KEY",
    "SsmGate",
    "admit",
    "cookie_name",
    "origins",
    "refusal_response",
    "resolve",
    "start_after_bind",
]
