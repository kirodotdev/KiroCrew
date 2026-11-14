"""App web origins: an opted-in app's own site on the dashboard's port.

An app whose manifest sets ``backend.webOrigin: true`` is served at
``http://<app>.apps.localhost:<dashboard port>``. The browser resolves every
``*.localhost`` name to loopback on its own machine without DNS, so a request for
that name reaches whatever listener carries the dashboard there: the gateway
itself, or the one ``ssh -L`` forward that carries the dashboard from another
machine. The gateway tells it apart from a dashboard request by ``Host`` and
relays it, HTTP and WebSocket, to the app's backend. No listener is added.

The name is a different site from every dashboard name (``localhost`` is not on
the Public Suffix List), so the dashboard's host-only cookies are never sent to
it and a page served there cannot script the dashboard. That is what lets an app
frame a full third-party site, with real cookies and storage, next to the
dashboard instead of inside its origin.

The trust model is the app backend's own loopback port's, unchanged:

* Only a request straight from this machine is served (a loopback peer with no
  forwarding header, not relayed), so no peer that cannot reach the backend's
  port today reaches it this way. A request that arrived on the Session Manager
  listener's port is refused, checked by listener because this router runs
  before that listener's gate.
* The request reaches no dashboard layer: no route, no session auth, no CSRF and
  no dashboard response header. The app's own credential is the only one here.
* The gateway forwards no credential of its own and signs nothing. A backend
  trusts ``X-KiroCrew-Proxy`` as proof that the dashboard's API proxy forwarded a
  call, and a page on this origin is the app's framed content (possibly a third
  party's scripts), so a signature here would let that page call the backend's
  gateway-only API. Every ``X-KiroCrew-*`` header, the dashboard's cookies and
  ``Authorization`` are dropped.
* The response may set only host-only cookies, cannot clear the dashboard's site
  data, and may be framed only by the dashboard's own loopback origins.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

import aiohttp
import yarl
from aiohttp import web
from multidict import CIMultiDict

from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.ssm_gate import GateRef

logger = logging.getLogger("kiro_crew.apps.app_web_origin")

#: The suffix every app web origin's host name carries. Two labels below
#: ``localhost`` keep it apart from the single-label ``*.localhost`` names the
#: Remote Crew panes use and from ``kirocrew.localhost``.
APP_WEB_ORIGIN_SUFFIX = ".apps.localhost"

#: The CSP source the dashboard admits in ``frame-src`` and ``connect-src`` for app
#: web origins. Only names under the suffix match, which a browser resolves to its
#: own loopback; the port is a wildcard because the browser's port is whatever
#: local port carries the dashboard. Kept apart from the dashboard's loopback
#: preview sources, whose client mirror decides Remote Crew pane embedding.
APP_WEB_ORIGIN_CSP_SOURCE = " http://*" + APP_WEB_ORIGIN_SUFFIX + ":*"

#: One DNS label: what an app name must be to name a host. App names are
#: kebab-case but may run to 128 characters, and a label stops at 63.
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

#: The dashboard host names whose origins may frame an app web origin, and whose
#: ports an app web origin may be reached on. IPv6 loopback is excluded because
#: CSP ``frame-ancestors`` cannot express a bracketed IPv6 host source.
_DASHBOARD_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "kirocrew.localhost"})

#: Connecting to the backend. The backend is on loopback, so this only bounds a
#: backend that accepts nothing.
_CONNECT_TIMEOUT = 30.0

#: Longest silence on a relayed response, its first byte included. There is no
#: total: a framed page can wait this long for its first response while the
#: backend's own upstream sign-in runs, and a stream does not end. A backend that
#: stops producing is still cut.
_IDLE_TIMEOUT = 120.0

#: Largest WebSocket message relayed, in either direction (aiohttp refuses one of
#: this size or more and closes with 1009). aiohttp's 4 MiB default would close a
#: live document sync on one large frame. aiohttp checks the size once a frame is
#: complete, so this bounds a message built from many frames or a compressed one;
#: a single frame is read whole first, as on every aiohttp WebSocket in the gateway.
_MAX_WS_MESSAGE_BYTES = 64 * 1024 * 1024

#: Request headers never forwarded, matched lower-case. Hop-by-hop headers, the
#: credential the dashboard reads (``authorization``) and the WebSocket handshake
#: headers the client library writes itself. ``cookie`` is rebuilt, not dropped.
_DROP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "cookie",
        "authorization",
        "x-internal-secret",
        "x-session-token",
        "x-session-key",
        "x-internal-caller",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-accept",
        "sec-websocket-protocol",
    }
)

#: Header-name prefixes never forwarded: every gateway-authored header, so a page
#: cannot present one as the gateway's.
_DROP_REQUEST_PREFIXES = ("x-kirocrew-",)

#: Response headers never relayed. ``Clear-Site-Data`` is dropped so a framed
#: page cannot ask the browser to wipe storage on the user's behalf.
_DROP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "clear-site-data",
    }
)

#: The dashboard's own cookie names (``dashboard/refresh_tokens.py``). They are
#: host-only today, so a browser never sends them to an app host; dropping them by
#: name keeps that true if one is ever set with a ``Domain``.
_DASHBOARD_COOKIE_PREFIXES = ("mc_token", "mc_refresh_")


def parse_app_host(raw_host: str) -> tuple[str, str] | None | bool:
    """Classify a ``Host`` header.

    ``None`` when the name is not under :data:`APP_WEB_ORIGIN_SUFFIX` (the request
    is the dashboard's, and goes on unchanged). ``False`` when it is under the
    suffix but names no well-formed app, so it is refused here and never reaches
    a dashboard layer. Otherwise ``(app name, port)``, both canonical, as the
    dashboard's own ``Host`` barrier reads them: the name lower-cased with one
    trailing root dot dropped, and a missing port read as the HTTP default, ``80``
    (a browser leaves the default port out of ``Host``).
    """
    from kiro_crew.dashboard.origin import _canonical_host, _split_authority

    parts = _split_authority(raw_host)
    if parts is None:
        lowered = (raw_host or "").lower().rstrip(".")
        return False if APP_WEB_ORIGIN_SUFFIX in lowered else None
    host = _canonical_host(parts[0])
    if not host.endswith(APP_WEB_ORIGIN_SUFFIX):
        return None
    label = host[: -len(APP_WEB_ORIGIN_SUFFIX)]
    port = parts[1][1:] or "80"
    if not _LABEL_RE.match(label):
        return False
    return label, port


def dashboard_loopback_origins(allowed_origins: Iterable[str]) -> list[str]:
    """The dashboard's loopback origins: its own names, on every port it serves.

    Read from the live CSRF allowlist, so the gateway's bound port and every port
    the operator opted in through ``KIROCREW_ALLOWED_LOOPBACK_PORTS`` count, and
    nothing else does. Sorted, so a header built from them is stable.
    """
    found: set[str] = set()
    for origin in allowed_origins:
        url = yarl.URL(origin) if "://" in origin else None
        if url is None or url.scheme != "http" or url.explicit_port is None:
            continue
        authority = url.raw_authority.lower()
        host = authority.rsplit(":", 1)[0]
        if host in _DASHBOARD_LOOPBACK_HOSTS:
            found.add(f"http://{authority}")
    return sorted(found)


def _served_ports(origins: Iterable[str]) -> set[str]:
    return {origin.rsplit(":", 1)[1] for origin in origins}


def forwarded_cookie(raw: str) -> str:
    """The ``Cookie`` header with every dashboard cookie removed.

    Pairs are kept byte for byte and in order; only a pair whose name is a
    dashboard cookie is dropped, so the app's own cookies reach it unchanged.
    """
    kept = []
    for pair in raw.split(";"):
        name = pair.split("=", 1)[0].strip()
        if not name or name.startswith(_DASHBOARD_COOKIE_PREFIXES):
            continue
        kept.append(pair.strip())
    return "; ".join(kept)


def _forwarded_request_headers(request: web.Request, host: str) -> CIMultiDict[str]:
    from kiro_crew.dashboard.relay_headers import is_relay_header

    headers: CIMultiDict[str] = CIMultiDict()
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered in _DROP_REQUEST_HEADERS or lowered.startswith(_DROP_REQUEST_PREFIXES):
            continue
        if is_relay_header(key):
            continue
        headers.add(key, value)
    cookie = forwarded_cookie("; ".join(request.headers.getall("Cookie", [])))
    if cookie:
        headers["Cookie"] = cookie
    # The backend sees the name the browser used, so its own Host check and the
    # absolute URLs it builds keep meaning what they mean to the browser.
    headers["Host"] = host
    return headers


def host_only_set_cookie(value: str) -> str:
    """One ``Set-Cookie`` value with its ``Domain`` attribute removed.

    A cookie with a ``Domain`` would be sent to every name under it: on
    ``apps.localhost`` that is every other app's site. Without one the browser
    keeps it host-only, on this app's name alone.
    """
    parts = value.split(";")
    kept = [parts[0]] + [p for p in parts[1:] if p.split("=", 1)[0].strip().lower() != "domain"]
    return ";".join(kept)


def _without_frame_ancestors(policy: str) -> str:
    policies: list[str] = []
    for serialized_policy in policy.split(","):
        directives = [directive.strip() for directive in serialized_policy.split(";")]
        kept = [
            directive
            for directive in directives
            if directive and directive.split(None, 1)[0].lower() != "frame-ancestors"
        ]
        if kept:
            policies.append("; ".join(kept))
    return ", ".join(policies)


def hardened_response_headers(
    upstream: Iterable[tuple[str, str]], ancestors: str
) -> CIMultiDict[str]:
    """The backend's response headers as the browser may receive them.

    Hop-by-hop headers and ``Clear-Site-Data`` dropped, every ``Set-Cookie``
    host-only, and ``frame-ancestors`` replaced by *ancestors*: the backend's own
    directive is removed from each policy it sent, and one policy carrying only
    the gateway's directive is added. Policies are enforced together, so the rest
    of the backend's CSP still applies.
    """
    headers: CIMultiDict[str] = CIMultiDict()
    for key, value in upstream:
        lowered = key.lower()
        if lowered in _DROP_RESPONSE_HEADERS:
            continue
        if lowered == "set-cookie":
            headers.add(key, host_only_set_cookie(value))
        elif lowered == "content-security-policy":
            rest = _without_frame_ancestors(value)
            if rest:
                headers.add(key, rest)
        else:
            headers.add(key, value)
    headers.add("Content-Security-Policy", f"frame-ancestors {ancestors}")
    return headers


def _refusal(request: web.Request, status: int, reason: str) -> web.Response:
    """A refusal, recorded once; the body names nothing about the app."""
    from kiro_crew.dashboard.origin import mark_audit_claimed

    mark_audit_claimed(request)
    sel().log_api_access(
        caller="app_web_origin",
        operation=f"{request.method} app web origin",
        outcome="denied",
        source="app_web_origin",
        resources=request.headers.get("Host", "") or "",
        error=reason,
    )
    text = "misdirected request" if status == 421 else "not found"
    return web.Response(status=status, text=text)


def _record_grant(request: web.Request, app_host: str) -> None:
    """The serve decision, recorded as every refusal is, before the relay starts."""
    sel().log_api_access(
        caller="app_web_origin",
        operation=f"{request.method} app web origin",
        outcome="granted",
        source="app_web_origin",
        resources=f"{app_host}{request.path}",
    )


async def _backend_port(name: str) -> int | None:
    """The tracked port of *name*'s backend when the app may be served, else None.

    Installed, enabled, opted in, gateway-managed and running. Every failing
    condition answers the same, so a page cannot tell which apps are installed.
    """
    from kiro_crew.apps.backend import get_app_backend_port
    from kiro_crew.apps.manager import get_app_manifest, is_app_enabled

    manifest = await asyncio.to_thread(get_app_manifest, name)
    if manifest is None or not manifest.backend.webOrigin:
        return None
    if not await asyncio.to_thread(is_app_enabled, name):
        return None
    port = get_app_backend_port(name)
    return int(port) if port else None


def _client_session(request: web.Request) -> tuple[aiohttp.ClientSession, bool]:
    session = request.app.get("_proxy_session")
    if session is None or session.closed:
        return aiohttp.ClientSession(auto_decompress=False), True
    return session, False


async def _relay_http(
    request: web.Request, target: yarl.URL, headers: CIMultiDict[str], ancestors: str
) -> web.StreamResponse:
    timeout = aiohttp.ClientTimeout(total=None, connect=_CONNECT_TIMEOUT, sock_read=_IDLE_TIMEOUT)
    session, owns = _client_session(request)
    resp: web.StreamResponse | None = None
    try:
        async with session.request(
            request.method,
            target,
            headers=headers,
            data=request.content if request.body_exists else None,
            timeout=timeout,
            allow_redirects=False,
        ) as upstream:
            resp = web.StreamResponse(
                status=upstream.status,
                reason=upstream.reason,
                headers=hardened_response_headers(upstream.headers.items(), ancestors),
            )
            try:
                await resp.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
            except (ConnectionResetError, ConnectionAbortedError):
                pass
            return resp
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.info("app web origin relay to %s failed: %s", target.port, exc)
        if resp is not None and resp.prepared:
            from kiro_crew.apps.routes import _end_relay_midbody

            return _end_relay_midbody(request, resp)
        return web.Response(status=502, text="app backend unreachable")
    finally:
        if owns:
            await session.close()


#: Close codes a peer may report but no endpoint may send in a close frame
#: (RFC 6455 section 7.4.1): no status, abnormal closure, TLS failure.
_UNSENDABLE_CLOSE_CODES = frozenset({1005, 1006, 1015})


def _close_code_to_relay(code: int | None) -> int:
    """The code to close both legs with, from the side that ended first.

    A code that side's peer chose is passed on as it is. A close with no status
    (aiohttp reports an empty close frame as 0) is a normal closure, a connection
    that dropped is "going away", and a code outside the sendable 1000-4999 range
    is a protocol error.
    """
    if not code or code == 1005:
        return int(aiohttp.WSCloseCode.OK)
    if code in _UNSENDABLE_CLOSE_CODES:
        return int(aiohttp.WSCloseCode.GOING_AWAY)
    if not 1000 <= code <= 4999:
        return int(aiohttp.WSCloseCode.PROTOCOL_ERROR)
    return code


def _ended_with(pumps: Sequence[asyncio.Future[int | None]]) -> int | None:
    """The close code of the first pump, page side first, that finished on its own."""
    for task in pumps:
        if task.done() and not task.cancelled():
            return 1006 if task.exception() is not None else task.result()
    return None


async def _pump(source: Any, sink: Any) -> int | None:
    """Copy WebSocket messages from *source* to *sink*; answer the code *source* ended with.

    The code comes from the message that ended it: the peer's close frame, or the
    error aiohttp raised (1009 for a message over the cap). Only a socket aiohttp
    already reports closing or closed is read through its own ``close_code``.
    """
    while True:
        msg = await source.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            await sink.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await sink.send_bytes(msg.data)
        elif msg.type == aiohttp.WSMsgType.CLOSE:
            return msg.data if isinstance(msg.data, int) else None
        elif msg.type == aiohttp.WSMsgType.ERROR:
            code = getattr(msg.data, "code", None)
            return code if isinstance(code, int) else 1006
        elif msg.type in (aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            return source.close_code if isinstance(source.close_code, int) else None


async def _relay_websocket(
    request: web.Request, target: yarl.URL, headers: CIMultiDict[str]
) -> web.StreamResponse:
    offered = tuple(
        p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()
    )
    session, owns = _client_session(request)
    try:
        try:
            upstream = await session.ws_connect(
                target.with_scheme("ws"),
                headers=headers,
                protocols=offered,
                timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10.0),
                max_msg_size=_MAX_WS_MESSAGE_BYTES,
                autoclose=False,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.info("app web origin WebSocket to %s failed: %s", target.port, exc)
            return web.Response(status=502, text="app backend unreachable")
        async with upstream:
            browser = web.WebSocketResponse(
                protocols=(upstream.protocol,) if upstream.protocol else (),
                max_msg_size=_MAX_WS_MESSAGE_BYTES,
                autoclose=False,
            )
            await browser.prepare(request)
            pumps = [
                asyncio.ensure_future(_pump(browser, upstream)),
                asyncio.ensure_future(_pump(upstream, browser)),
            ]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                # Read in `finally`, so a handler cancelled during the wait still
                # relays the code of a pump that had already ended.
                code = _close_code_to_relay(_ended_with(pumps))
                for task in pumps:
                    task.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
                await browser.close(code=code)
                await upstream.close(code=code)
            return browser
    finally:
        if owns:
            await session.close()


def _is_websocket_upgrade(request: web.Request) -> bool:
    connection = {t.strip().lower() for t in request.headers.get("Connection", "").split(",")}
    return request.headers.get("Upgrade", "").lower() == "websocket" and "upgrade" in connection


async def serve_app_web_origin(
    request: web.Request, name: str, port: str, ssm_gate: GateRef | None
) -> web.StreamResponse:
    """Serve one request for ``<name>.apps.localhost:<port>``, or refuse it.

    *ssm_gate* holds the Session Manager listener's gate, the one token auth is
    built with. This runs before that gate has judged the request, so its mark is
    not set yet and the listener is read from the socket's local port instead.
    """
    from kiro_crew.dashboard.origin import (
        frame_ancestors_value,
        is_direct_local_request,
        is_proxied_request,
    )

    gate = ssm_gate.gate if ssm_gate is not None else None
    if gate is not None and gate.owns(request):
        return _refusal(request, 421, "arrived on the Session Manager listener")
    if is_proxied_request(request) or not is_direct_local_request(request):
        return _refusal(request, 421, "not a direct local request")
    origins = dashboard_loopback_origins(request.app.get("allowed_origins") or ())
    if port not in _served_ports(origins):
        return _refusal(request, 421, "port is not a dashboard port")
    backend_port = await _backend_port(name)
    if backend_port is None:
        return _refusal(request, 404, "no app web origin for this name")
    target = yarl.URL.build(
        scheme="http",
        host="127.0.0.1",
        port=backend_port,
        encoded=True,
        path=request.rel_url.raw_path,
        query_string=request.rel_url.raw_query_string,
    )
    app_host = f"{name}{APP_WEB_ORIGIN_SUFFIX}:{port}"
    headers = _forwarded_request_headers(request, app_host)
    _record_grant(request, app_host)
    if _is_websocket_upgrade(request):
        return await _relay_websocket(request, target, headers)
    return await _relay_http(request, target, headers, frame_ancestors_value(origins))


def build_app_web_origin_middleware(ssm_gate: GateRef | None) -> Any:
    """The app web origin router, built with the chain's Session Manager gate holder.

    *ssm_gate* is the holder the chain passes to token auth. The gateway fills it
    before the Session Manager listener accepts a connection, so this router
    refuses that listener's requests from the first one on.
    """

    @web.middleware  # type: ignore[misc]
    async def app_web_origin_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        """Hand an app web origin's request to its app; every other request goes on.

        Placed ahead of the dashboard's host canonicalization and ``Host`` barrier,
        so a name under :data:`APP_WEB_ORIGIN_SUFFIX` is decided here and never by a
        dashboard layer: served, or refused.
        """
        parsed = parse_app_host(request.headers.get("Host", ""))
        if parsed is None:
            return await handler(request)  # type: ignore[no-any-return]
        if parsed is False:
            return _refusal(request, 404, "malformed app web origin host")
        assert isinstance(parsed, tuple)
        name, port = parsed
        return await serve_app_web_origin(request, name, port, ssm_gate)

    return app_web_origin_middleware


__all__ = [
    "APP_WEB_ORIGIN_CSP_SOURCE",
    "APP_WEB_ORIGIN_SUFFIX",
    "build_app_web_origin_middleware",
    "dashboard_loopback_origins",
    "forwarded_cookie",
    "hardened_response_headers",
    "host_only_set_cookie",
    "parse_app_host",
    "serve_app_web_origin",
]
