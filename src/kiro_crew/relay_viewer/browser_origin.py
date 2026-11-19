"""Relay Crews in a browser tab: the gateway relays each open on an origin of its own.

A browser cannot add the ``Kiro-Relay-Authorization`` header the relay admits a
viewer by, and the page must never read the token. So for a browser the gateway
holds the grant and relays the Crew itself, at ``http://kcr-<sid>.localhost:<port>``
on the dashboard's own listener, routed by ``Host`` like an app web origin
(``apps/app_web_origin.py``). No listener is added.

* One site per open. ``sid`` is 32 random base32 characters, never reused, so a
  page never loads storage an earlier open, or an earlier account, left behind.
  The name is a site of its own, apart from the dashboard and from every other
  open: no cookie crosses between them, and the dashboard's own ``Origin`` and
  ``Host`` checks refuse a request from it.
* An open is redeemed once, by the first cookie-less ``GET`` within
  :data:`REDEEM_SECONDS`: that answer sets a host-only ``HttpOnly``
  ``SameSite=Strict`` cookie and reloads the same URL (a same-site hop, so the
  cookie is sent). Every later request needs that cookie; nothing else is relayed.
* Only a request straight from this machine is served (a loopback peer with no
  forwarding header, not on the Session Manager listener, on a dashboard port).
* The upstream is always the Crew URL the control plane listed for this open.
  Nothing in the request picks the host. The gateway adds the relay header and
  drops the page's own copy, the dashboard's cookies, this module's cookie,
  ``Authorization``, ``Referer`` and every gateway header. A non-``GET`` from any
  other ``Origin`` is refused, and so is a service worker script, so nothing in
  the browser outlives an open.
* The account: an open keeps the sign-in marker its mint was made under. Each
  document load and each WebSocket compare it with the vault's, and the token is
  renewed with it before it ends. A change, a sign-out, or :func:`close_all`
  (called on sign-out and sign-in) drops every open and cuts every relayed
  WebSocket; a request for a dropped open gets a short page that says so and
  clears the origin's cache and storage.
* The tab learns of a close at once. Each relayed page gets one same-origin
  script, :data:`WATCH_PATH`, which holds a request on :data:`ALIVE_PATH` open;
  the gateway answers it when the open is dropped (or its sign-in changed), and
  the script then loads the closed page in place of the old one. The dashboard
  that opened the tab cannot do this: it cut the tab's ``opener``, so the Crew's
  page cannot reach it, and with it its own handle to close or move the tab.

Never logs a token, a cookie or a ``sid``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import yarl
from aiohttp import web
from multidict import CIMultiDict

if TYPE_CHECKING:
    from kiro_crew.dashboard.ssm_gate import GateRef

logger = logging.getLogger(__name__)

#: Every relayed Crew's host name is ``kcr-<sid>.localhost``: one label under
#: ``localhost``, which the Public Suffix List's default rule makes a public
#: suffix, so each open is a site of its own and no page can set a cookie that
#: another open (or the dashboard) receives.
RELAY_ORIGIN_PREFIX = "kcr-"
RELAY_ORIGIN_SUFFIX = ".localhost"
#: The redeemed open's cookie, host-only.
COOKIE = "kc_relay"
#: How long a fresh open waits to be redeemed by its tab.
REDEEM_SECONDS = 60.0
#: A token is renewed this long before the control plane's renewal time.
_RENEW_EARLY_SECONDS = 15.0
_SID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz234567")
_SID_LENGTH = 32
_RELAY_HEADER = "Kiro-Relay-Authorization"
#: The renewal answer: ``(access token, seconds until renewal, seconds until it
#: ends, sign-in marker)``.
Renew = Callable[[Optional[str]], Awaitable[tuple[str, float, float, Optional[str]]]]
#: Refusals that may pass: the open keeps its current token while that is still
#: good and tries again after this wait, growing per failure.
_TRANSIENT = frozenset({"UNREACHABLE", "SERVICE_ERROR", "THROTTLED", "BAD_ANSWER"})
_RETRY_SECONDS = (5.0, 15.0, 30.0, 60.0)
Marker = Callable[[], Awaitable[Optional[str]]]
#: The gateway's own paths on a relayed origin; never relayed to the Crew.
WATCH_PATH = "/.kcr/watch.js"
ALIVE_PATH = "/.kcr/alive"
#: How long one watch request is held before it is answered "still open".
ALIVE_SECONDS = 25.0
#: Holds a watch request open and, on a 401 (the open is gone), loads the closed
#: page in place of this one; any other answer or a network error asks again.
_WATCH_JS = (
    "(()=>{const go=()=>fetch('" + ALIVE_PATH + "',{cache:'no-store'}).then("
    "r=>{if(r.status===401)location.replace('/');else setTimeout(go,r.ok?0:5000)},"
    "()=>setTimeout(go,5000));go()})();"
)
_WATCH_TAG = f'<script src="{WATCH_PATH}"></script>'.encode()


@dataclass
class _Open:
    endpoint_id: str
    crew_origin: yarl.URL
    token: str
    renew_at: float
    expires_at: float
    marker: Optional[str]
    cookie_value: str
    deadline: float
    renew: Renew
    marker_now: Marker
    redeemed: bool = False
    failures: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    gone: asyncio.Event = field(default_factory=asyncio.Event)


_opens: dict[str, _Open] = {}
_epoch = 0


def epoch() -> int:
    """The current generation; :func:`close_all` moves it on."""
    return _epoch


def _drop(sid: str) -> None:
    opened = _opens.pop(sid, None)
    if opened is None:
        return
    opened.gone.set()
    for task in list(opened.tasks):
        task.cancel()


def close_all() -> None:
    """Drop every open and cut every relayed WebSocket (sign-out, sign-in, account change)."""
    global _epoch
    _epoch += 1
    for sid in list(_opens):
        _drop(sid)


def register(
    at_epoch: int,
    endpoint_id: str,
    crew_url: str,
    token: str,
    renew_in: float,
    expires_in: float,
    marker: Optional[str],
    renew: Renew,
    marker_now: Marker,
) -> Optional[str]:
    """Hold one open's grant and answer its ``sid``, or ``None`` when the epoch moved.

    *at_epoch* is :func:`epoch` from before the mint: a :func:`close_all` during
    the mint means the grant may belong to an account that is gone, so nothing is
    stored. An earlier open of the same Crew is dropped, so one token stands per Crew.
    """
    if at_epoch != _epoch:
        return None
    origin = yarl.URL(crew_url)
    for sid, opened in list(_opens.items()):
        if opened.endpoint_id == endpoint_id:
            _drop(sid)
    sid = "".join(secrets.choice(sorted(_SID_CHARS)) for _ in range(_SID_LENGTH))
    now = time.monotonic()
    _opens[sid] = _Open(
        endpoint_id=endpoint_id,
        crew_origin=origin.origin(),
        token=token,
        renew_at=now + max(0.0, renew_in - _RENEW_EARLY_SECONDS),
        expires_at=now + max(0.0, expires_in),
        marker=marker,
        cookie_value=secrets.token_urlsafe(32),
        deadline=now + REDEEM_SECONDS,
        renew=renew,
        marker_now=marker_now,
    )
    return sid


def relay_host(sid: str) -> str:
    """The host name the open *sid* is served on."""
    return f"{RELAY_ORIGIN_PREFIX}{sid}{RELAY_ORIGIN_SUFFIX}"


def parse_relay_host(raw_host: str) -> tuple[str, str] | None | bool:
    """``(sid, port)`` for ``kcr-<sid>.localhost[:port]``; ``None`` for any other name.

    ``False`` for a ``kcr-`` name under ``localhost`` that is not one well-formed
    ``sid``, so it is refused here and reaches no dashboard layer.
    """
    from kiro_crew.dashboard.origin import _canonical_host, _split_authority

    parts = _split_authority(raw_host)
    if parts is None:
        lowered = (raw_host or "").lower()
        return (
            False if lowered.startswith(RELAY_ORIGIN_PREFIX) and ".localhost" in lowered else None
        )
    host = _canonical_host(parts[0])
    label = host[: -len(RELAY_ORIGIN_SUFFIX)] if host.endswith(RELAY_ORIGIN_SUFFIX) else ""
    if not label.startswith(RELAY_ORIGIN_PREFIX):
        return None
    sid = label[len(RELAY_ORIGIN_PREFIX) :]
    if len(sid) != _SID_LENGTH or not set(sid) <= _SID_CHARS:
        return False
    return sid, parts[1][1:] or "80"


_NO_STORE = {"Cache-Control": "no-store"}


def _closed() -> web.Response:
    """The answer for an open that is gone: says so, and clears what the tab stored.

    ``"cookies"`` is left out: that type clears the whole registrable domain, so it
    would end every other open Crew too. The cookie is dead server-side anyway.
    """
    return web.Response(
        status=401,
        content_type="text/html",
        text=(
            "<!doctype html><meta charset=utf-8><title>Kiro Crew</title>"
            "<p>This Crew tab is closed. Open the Crew again from your dashboard.</p>"
        ),
        headers={
            **_NO_STORE,
            "Clear-Site-Data": '"cache", "storage"',
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        },
    )


async def _audit_decision(request: web.Request, outcome: str, reason: str) -> None:
    """One SEL row per decision, as an app web origin's; it names no ``sid`` or cookie."""
    from kiro_crew.apps.app_web_origin import _audit
    from kiro_crew.dashboard.origin import mark_audit_claimed

    mark_audit_claimed(request)
    await _audit(request, outcome=outcome, resources="relayed Crew origin", error=reason)


async def _closed_audited(request: web.Request, reason: str) -> web.Response:
    await _audit_decision(request, "denied", reason)
    return _closed()


async def _refuse(
    request: web.Request, status: int, reason: str, text: Optional[str] = None
) -> web.Response:
    """A refusal recorded once, as an app web origin's; the record names no ``sid``."""
    await _audit_decision(request, "denied", reason)
    if text is None:
        text = "misdirected request" if status == 421 else "not found"
    return web.Response(status=status, text=text, headers=_NO_STORE)


def _redeemed(value: str) -> web.Response:
    resp = web.Response(
        content_type="text/html",
        text='<!doctype html><meta charset=utf-8><meta http-equiv="refresh" content="0">',
        headers={**_NO_STORE, "Content-Security-Policy": "default-src 'none'"},
    )
    resp.set_cookie(COOKIE, value, httponly=True, samesite="Strict", path="/")
    return resp


async def _fresh(sid: str, opened: _Open, recheck: bool) -> bool:
    """Is *opened* still good to relay: same account, a token not yet due. Renews it if due."""
    from kiro_crew.dashboard.handlers.relay_viewer import SIGN_IN_CHANGED
    from kiro_crew.relay_viewer import cps

    async with opened.lock:
        if _opens.get(sid) is not opened:
            return False
        if recheck:
            marker = await opened.marker_now()
            # A sign-out may have dropped it while the vault was read.
            if _opens.get(sid) is not opened:
                return False
            if marker != opened.marker:
                logger.info(
                    "relay browser %s: sign-in changed, closing every open", opened.endpoint_id
                )
                close_all()
                return False
        now = time.monotonic()
        # A token past its end is never used, whatever the retry wait says.
        if now < opened.renew_at and now < opened.expires_at:
            return True
        try:
            token, renew_in, expires_in, marker = await opened.renew(opened.marker)
        except cps.CpsRefused as exc:
            logger.info("relay browser %s: renewal -> %s", opened.endpoint_id, exc.code)
            if exc.code in (cps.SIGNED_OUT, SIGN_IN_CHANGED):
                close_all()
                return False
            now = time.monotonic()
            # A passing failure keeps the open on the token it holds, while that lasts.
            if exc.code in _TRANSIENT and now < opened.expires_at and _opens.get(sid) is opened:
                wait = _RETRY_SECONDS[min(opened.failures, len(_RETRY_SECONDS) - 1)]
                opened.failures += 1
                opened.renew_at = now + wait
                return True
            _drop(sid)
            return False
        if _opens.get(sid) is not opened:
            return False
        now = time.monotonic()
        opened.token = token
        opened.marker = marker
        opened.failures = 0
        opened.renew_at = now + max(0.0, renew_in - _RENEW_EARLY_SECONDS)
        opened.expires_at = now + max(0.0, expires_in)
        return True


async def _alive(request: web.Request, sid: str, opened: _Open) -> web.Response:
    """One watch request: held until *opened* is dropped or :data:`ALIVE_SECONDS` pass.

    The sign-in is compared first, so a sign-out made anywhere (another tab, the
    CLI) reaches an open tab within one hold, not only at its next page load.
    """
    if await _fresh(sid, opened, recheck=True):
        try:
            await asyncio.wait_for(opened.gone.wait(), ALIVE_SECONDS)
        except asyncio.TimeoutError:
            return web.Response(status=204, headers=_NO_STORE)
    return await _closed_audited(request, "closed")


def _with_watch(page: bytes) -> bytes:
    """*page* with the watch script first in its ``<head>``, or at its start without one."""
    at = page.lower().find(b"<head")
    end = page.find(b">", at) if at >= 0 else -1
    cut = end + 1 if end >= 0 else 0
    return page[:cut] + _WATCH_TAG + page[cut:]


def _response_headers(
    own: str, crew: str
) -> Callable[[Iterable[tuple[str, str]]], CIMultiDict[str]]:
    from kiro_crew.apps.app_web_origin import hardened_response_headers

    def rewrite(upstream: Iterable[tuple[str, str]]) -> CIMultiDict[str]:
        # 'self': the Crew frames its own documents (artifacts, widgets); no other site may.
        headers = hardened_response_headers(upstream, "'self'")
        for name in [k for k in headers if k.lower().startswith("access-control-")]:
            headers.popall(name, None)
        location = headers.get("Location")
        if location and location.startswith(crew):
            headers["Location"] = own + location[len(crew) :]
        return headers

    return rewrite


def _request_headers(request: web.Request, opened: _Open, own: str) -> CIMultiDict[str]:
    from kiro_crew.apps.app_web_origin import _forwarded_request_headers

    headers = _forwarded_request_headers(request, opened.crew_origin.raw_authority)
    for name in ("Referer", _RELAY_HEADER):
        headers.popall(name, None)
    # The app filter drops it for an app backend, but here it is the Crew's own
    # page naming its own chat to its own gateway, as in the desktop pane.
    session_key = request.headers.get("X-Session-Key")
    if session_key is not None:
        headers["X-Session-Key"] = session_key
    kept = [
        pair.strip()
        for pair in headers.get("Cookie", "").split(";")
        if pair.strip() and pair.split("=", 1)[0].strip() != COOKIE
    ]
    headers.popall("Cookie", None)
    if kept:
        headers["Cookie"] = "; ".join(kept)
    if headers.get("Origin") == own:
        headers["Origin"] = str(opened.crew_origin)
    headers[_RELAY_HEADER] = f"Bearer {opened.token}"
    return headers


async def serve_relay_origin(
    request: web.Request, sid: str, port: str, ssm_gate: GateRef | None
) -> web.StreamResponse:
    """Relay one request for ``kcr-<sid>.localhost:<port>`` to its Crew, or refuse it."""
    from kiro_crew.apps.app_web_origin import (
        _is_websocket_upgrade,
        _relay_http,
        _relay_websocket,
        _served_ports,
        dashboard_loopback_origins,
    )
    from kiro_crew.dashboard.origin import is_direct_local_request, is_proxied_request

    gate = ssm_gate.gate if ssm_gate is not None else None
    if gate is not None and gate.owns(request):
        return await _refuse(request, 421, "arrived on the Session Manager listener")
    if is_proxied_request(request) or not is_direct_local_request(request):
        return await _refuse(request, 421, "not a direct local request")
    if port not in _served_ports(
        dashboard_loopback_origins(request.app.get("allowed_origins") or ())
    ):
        return await _refuse(request, 421, "port is not a dashboard port")
    opened = _opens.get(sid)
    if opened is None:
        return await _closed_audited(request, "no such open")
    if request.headers.get("Service-Worker", "").lower() == "script":
        return await _refuse(
            request, 403, "service worker script", "service workers are not relayed"
        )
    websocket = _is_websocket_upgrade(request)
    cookie = request.cookies.get(COOKIE)
    if not opened.redeemed:
        if (
            cookie is None
            and request.method == "GET"
            and not websocket
            and time.monotonic() < opened.deadline
        ):
            opened.redeemed = True
            await _audit_decision(request, "granted", "redeemed")
            return _redeemed(opened.cookie_value)
        return await _closed_audited(request, "not redeemable")
    if cookie is None or not hmac.compare_digest(cookie.encode(), opened.cookie_value.encode()):
        return await _closed_audited(request, "no matching cookie")
    path = request.rel_url.raw_path
    if path == WATCH_PATH:
        return web.Response(text=_WATCH_JS, content_type="text/javascript", headers=_NO_STORE)
    if path == ALIVE_PATH:
        return await _alive(request, sid, opened)
    # A browser leaves the default port out of ``Origin``, so ``own`` does too.
    own = f"http://{relay_host(sid)}" + ("" if port == "80" else f":{port}")
    origin = request.headers.get("Origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None and origin != own:
        return await _refuse(request, 403, "cross-origin write", "cross-origin request refused")
    document = websocket or request.headers.get("Sec-Fetch-Dest", "") == "document"
    if not await _fresh(sid, opened, recheck=document):
        return await _closed_audited(request, "closed")
    target = yarl.URL.build(
        scheme=opened.crew_origin.scheme,
        host=opened.crew_origin.raw_host or "",
        port=opened.crew_origin.explicit_port,
        encoded=True,
        path=request.rel_url.raw_path,
        query_string=request.rel_url.raw_query_string,
    )
    headers = _request_headers(request, opened, own)
    page = document and not websocket and request.method == "GET"
    if page:
        # The page is edited (the watch script), so it must come plain and whole.
        headers["Accept-Encoding"] = "identity"
        for name in ("If-None-Match", "If-Modified-Since"):
            headers.popall(name, None)
    await _audit_decision(request, "granted", "relayed")
    if _opens.get(sid) is not opened:
        return await _closed_audited(request, "closed")
    # Held so close_all cuts it: a WebSocket, or a response still streaming.
    task = asyncio.current_task()
    if task is not None:
        opened.tasks.add(task)
    try:
        if websocket:
            return await _relay_websocket(request, target, headers)
        return await _relay_http(
            request,
            target,
            headers,
            "'self'",
            rewrite=_response_headers(own, str(opened.crew_origin)),
            html=_with_watch if page else None,
        )
    finally:
        if task is not None:
            opened.tasks.discard(task)


def build_relay_origin_middleware(ssm_gate: GateRef | None) -> Any:
    """The relayed-Crew router, placed beside the app web origin router."""

    @web.middleware  # type: ignore[misc]
    async def relay_origin_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        parsed = parse_relay_host(request.headers.get("Host", ""))
        if parsed is None:
            return await handler(request)  # type: ignore[no-any-return]
        if parsed is False:
            return await _refuse(request, 404, "malformed relayed Crew host")
        assert isinstance(parsed, tuple)
        sid, port = parsed
        return await serve_relay_origin(request, sid, port, ssm_gate)

    return relay_origin_middleware


__all__ = [
    "ALIVE_PATH",
    "COOKIE",
    "REDEEM_SECONDS",
    "RELAY_ORIGIN_PREFIX",
    "RELAY_ORIGIN_SUFFIX",
    "build_relay_origin_middleware",
    "close_all",
    "epoch",
    "parse_relay_host",
    "register",
    "relay_host",
    "serve_relay_origin",
    "WATCH_PATH",
]
