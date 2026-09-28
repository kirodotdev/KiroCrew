"""Same-origin relay for the Playwright CLI browser view.

``/browser-view/{token}/{tail}`` on the dashboard's own port relays HTTP and
WebSocket traffic to the loopback view server (``playwright-cli show``,
supervised by :mod:`kiro_crew.browser_cli.view`). The Browser panel frames
this path instead of ``http://127.0.0.1:<port>/``, so the live view rides the
same origin — and therefore the same port forward or tunnel — that delivers
the dashboard itself.

Why a relay at all: the view server binds the gateway host's loopback, which a
browser on another machine cannot reach. The old remedy (pin
``dashboard.browser_view_port``, forward a second port) requires knowing an
undocumented knob and does not work through tunnels that publish only the
dashboard port. Riding the dashboard origin removes the second port entirely.

The view SPA is served with root-absolute URLs, so three server-side rewrites
make it work under a path prefix — no JavaScript is modified. Because the
prefix embeds the capability token, the rewrites are also what propagate the
token to every follow-up request:

1. **Redirects.** ``/`` answers ``302 /index.html?ws=<wstoken>``; the Location
   is re-rooted under the prefix AND the ``ws`` param value becomes
   ``browser-view/<token>/<wstoken>``. The SPA builds its WebSocket URL as
   ``'/' + <ws param>``, so prefixing the *param value* steers the socket back
   through this route, token and all.
2. **HTML.** Root-absolute ``src="/…"`` / ``href="/…"`` references are
   re-rooted under the prefix.
3. **CSS.** Root-absolute ``url(/…)`` references likewise.

Everything else — scripts, fonts, images — streams through untouched.

Security posture:

* **No SSRF surface.** The upstream host is always loopback and the port comes
  only from the supervised view server's own recorded state
  (:func:`kiro_crew.browser_cli.view.relay_authorize`), never from anything in
  the request.
* **Capability-token authenticated, authentication first.** The framed
  dashboard carries full remote mouse/keyboard input into a browser holding
  the operator's logged-in sessions — but the frame must NOT run on the
  dashboard's origin with ambient authority, so the panel sandboxes it into
  an opaque origin (no ``allow-same-origin``), and an opaque origin sends no
  cookies. The relay therefore authenticates each request with the
  per-instance capability token embedded in the path: minted fresh for every
  view-server start (:mod:`kiro_crew.browser_cli.view`), disclosed only
  through the cookie-authed, owner-gated status payload, and constant-time
  compared against a LOCK-FREE token snapshot before the supervisor lock is
  touched at all — a tokenless or bad-token request costs one compare and
  can never contend the lock, the shared thread pool, or the OS-level
  probes, no matter how long a concurrent ``ensure_running`` holds the lock
  across its startup poll. Only a caller already holding the current token
  proceeds to the bounded lock wait (``view._AUTHORIZE_ACQUIRE_TIMEOUT_S``);
  if a start window holds the lock past the bound, that caller is refused as
  retryable (``busy`` → 503 + ``Retry-After``) rather than parking — safe to
  distinguish from the uniform 404 precisely because possession of the token
  is what reached this branch. The ownership probes themselves run OUTSIDE
  the lock on a consistent snapshot and are single-flighted inside the
  prover: concurrent asset fetches share one proof run's cached verdict
  instead of serializing behind the supervisor lock or each spawning their
  own ``lsof``. Possession proves the caller
  passed the owner gate at disclosure time. The path prefix itself is on
  ``token_auth``'s bypass list (prefixed and bare, so the bare path gets this
  handler's uniform 404, not the middleware's 403) — the token is the gate.
* **Proof→connect race closed.** The pre-connect ownership proof authorizes
  the lookup, and every established upstream connection is vouched for by a
  proof completed AFTER the connect, before any byte or frame is sent
  downstream (the re-proof carries a monotonic fence, so the prover's
  single-flight verdict cache can never satisfy it with pre-connect
  evidence; concurrent connections may share one post-connect proof that
  postdates all of their connects). The child binds its listener once at
  startup and holds it until death, so a post-connect proof that passes for
  the same port proves the listener was continuously the child's across the
  connect — a squatter requires the child dead first, which the re-proof
  catches (and a restarted view mints a new port/token, so a
  same-``ok``-different-port answer marks the held connection stale). Once
  the connection is established to the true child, the kernel pins the socket
  pair to that process; a later death mid-stream resets the stream rather
  than handing it to a squatter. At most one re-proof per established
  connection — never per frame or chunk — and only ever for a caller who
  already passed the token compare.
* **Isolated even off-panel.** Every relayed response except script types is
  stamped with ``Content-Security-Policy: sandbox`` (opaque origin, scripts
  allowed) plus ``X-Content-Type-Options: nosniff``, so relayed content
  cannot reach dashboard state — DOM, cookies, storage, authenticated
  ``fetch`` — even when the relay URL is opened as a full tab instead of the
  sandboxed iframe, and regardless of the content type a squatted or
  compromised upstream answers with (HTML, XHTML, SVG, XML, or a sniffable
  unknown).
* **Uniform refusal.** A missing or wrong token answers 404, and so does
  "view not running" — an unauthenticated probe learns nothing about whether
  a logged-in browser is up. Only a token-bearing caller can distinguish a
  dead upstream (502).
* **Audited.** Every token allow/deny decision emits a SEL audit record
  (outcome plus a fixed reason, never the token or the request path/query),
  so probing the relay is visible in the security event log while the wire
  response stays uniform.
* **Bounded.** Proxied WebSocket messages are size-capped, plain responses are
  streamed in chunks, and only GET/HEAD are routed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import aiohttp
from aiohttp import web

from kiro_crew.browser_cli import view as browser_cli_view
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The dashboard-origin path the Browser panel frames (with the capability
#: token appended: ``/browser-view/<token>/``). No trailing slash.
ROUTE_PREFIX = "/browser-view"

#: Stamped on every relayed response except script types (see
#: :func:`_stamp_relay_headers`). The CSP ``sandbox`` directive forces an
#: opaque origin at the SERVER, so the isolation holds even when the relay URL
#: is opened as a full tab rather than inside the panel's sandboxed iframe —
#: for ANY document type the upstream answers with, not just the SPA's HTML.
#: The keyword set mirrors the panel iframe's ``sandbox`` attribute minus
#: ``allow-same-origin`` — identical capabilities framed or not. Note
#: ``allow-popups`` grants nothing beyond the frame: absent
#: ``allow-popups-to-escape-sandbox``, a popup inherits this sandbox — opaque
#: origin included.
_DOCUMENT_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"

#: Upper bound for one proxied WebSocket message, both legs. Screencast frames
#: arrive as base64 text well under 1 MiB; the margin covers trace payloads
#: without letting a runaway frame hold unbounded memory.
_WS_MAX_MSG_BYTES = 32 * 1024 * 1024

#: Per-request budget for plain HTTP proxying (loopback hop; generous).
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

#: Streaming chunk size for passthrough bodies.
_CHUNK_BYTES = 64 * 1024

#: App-storage key for the shared upstream client session.
_SESSION_KEY = "browser_view_relay_client"

#: Request headers forwarded upstream. Allowlist, not blocklist: the upstream
#: is a static file server plus one redirect, and forwarding credentials
#: (Cookie, Authorization) would hand the dashboard session to a child process
#: that has no use for it.
_FORWARD_REQUEST_HEADERS = (
    "Accept",
    "Accept-Language",
    "If-None-Match",
    "If-Modified-Since",
    "Range",
)

#: Response headers copied back downstream on STREAMED bodies — the other half
#: of the request allowlist above. Forwarding ``Range`` upstream while
#: dropping ``Content-Range`` would relay a 206 whose partial body the
#: browser must discard as a protocol violation, and dropping the validators
#: (``ETag``/``Last-Modified``) makes the forwarded conditionals dead weight.
#: Streamed bodies pass through byte-identical, so upstream's headers describe
#: exactly what we send. REWRITTEN bodies (HTML/CSS) deliberately get none of
#: these: the relay transforms those bodies, so upstream's validators and
#: ranges describe content we did not send — and with no validator ever
#: emitted, the browser never sends a conditional for them, keeping that
#: branch's forwarded conditionals inert rather than wrong.
_FORWARD_RESPONSE_HEADERS = (
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
)

#: Response content types whose bodies are rewritten rather than streamed.
_REWRITE_HTML = "text/html"
_REWRITE_CSS = "text/css"

#: The one content-type class EXEMPT from the CSP ``sandbox`` stamp. A worker
#: created from a relayed script URL is governed by the CSP delivered on the
#: SCRIPT response, so stamping scripts would sandbox the view SPA's own
#: workers — and a script type never renders as a document on navigation, so
#: the exemption reopens nothing.
_SCRIPT_TYPES = frozenset(
    (
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/ecmascript",
    )
)


def _client(app: web.Application) -> aiohttp.ClientSession:
    """The app-scoped upstream session, created on first use.

    Lazy so the gateway boot path does no new work; closed by the browser-view
    cleanup hook in ``server.py``. One session keeps a warm connection to the
    view server instead of a fresh TCP handshake per asset.
    """
    session = app.get(_SESSION_KEY)
    if session is None or session.closed:
        session = aiohttp.ClientSession(auto_decompress=True)
        app[_SESSION_KEY] = session
    return session


async def close_relay_client(app: web.Application) -> None:
    """Close the upstream session (registered with the view cleanup hook)."""
    session = app.get(_SESSION_KEY)
    if session is not None and not session.closed:
        await session.close()


def _split_token(request: web.Request) -> tuple[str | None, str]:
    """``(token, upstream path+query)`` from the raw request path.

    Derived from ``raw_path`` (never the decoded ``match_info``) so what the
    browser encoded stays encoded on the wire. The first segment after the
    prefix is the capability token (``token_urlsafe`` alphabet, so never
    percent-encoded); everything after it is passed upstream verbatim. A
    suffix can never steer the HOST: it is joined onto a fixed
    ``http://127.0.0.1:<port>`` authority, where a leading ``//`` is still
    just a path.
    """
    raw = request.raw_path
    rest = raw[len(ROUTE_PREFIX) :] if raw.startswith(ROUTE_PREFIX) else raw
    if not rest.startswith("/"):
        return None, "/"  # bare `/browser-view` (or `?query`): no token segment
    rest = rest[1:]
    token, sep, tail = rest, "", ""
    for index, char in enumerate(rest):
        if char in "/?":
            token, sep, tail = rest[:index], char, rest[index + 1 :]
            break
    suffix = "/" if sep != "/" else "/" + tail
    if sep == "?":
        suffix = f"/?{tail}"
    return (token or None), suffix


def _audit(outcome: str, resources: str) -> None:
    """SEL-audit a token allow/deny decision (sanitized: never the token).

    Mirrors the sibling capability handlers (``sandbox_doc``, ``artifacts``).
    ``resources`` is a fixed reason enum plus at most the upstream port —
    never the request path or query, both of which embed credentials here:
    the path carries the relay's capability token, and the rewritten ``ws``
    query param carries the view server's socket token.
    """
    try:
        sel().log_api_access(
            caller="dashboard:browser-view-relay",
            operation="browser_view_relay.serve",
            outcome=outcome,
            source="dashboard",
            resources=resources[:512],
        )
    except Exception:  # pragma: no cover - auditing must never break serving
        logger.debug("browser-view-relay: audit failed", exc_info=True)


async def _upstream_still_ours(candidate: str, port: int) -> tuple[bool, str]:
    """Post-connect ownership re-proof for an established upstream connection.

    Closes the proof→connect race: the pre-connect proof releases the
    supervisor lock before the connect happens, so a child dying in that gap
    frees the port for a local squatter our authenticated, input-forwarding
    frame would then render. Re-proving AFTER the connect — before anything
    is sent downstream — pins the gap shut: the child holds its listener from
    startup to death, so a passing re-proof for the same port means the
    listener was continuously the child's across the connect. ``ok`` with a
    DIFFERENT port means the view restarted after we connected: the held
    connection went to the freed old port and must be refused as stale.

    Runs :func:`~kiro_crew.browser_cli.view.relay_authorize` (off the event
    loop — the supervisor lock can be held for seconds by a concurrent
    start), so a definitive ownership failure also tears down relay state and
    invalidates the token, exactly like the pre-connect path. The prover
    caches verdicts single-flight, so the fence below is what keeps this
    re-proof meaningful: ``proof_not_before`` is captured at entry — after
    the connection was established — so only a verdict recorded AFTER the
    connect can vouch for it; the pre-connect proof's cached verdict is
    always older than the fence and never consumed here. Concurrent requests
    still share — one request's post-connect proof is fresh enough for a
    sibling whose connect completed earlier. Cost: at most one re-proof per
    established connection, never per frame or chunk, and only for callers
    who already passed the token compare.

    Returns ``(ok, reason)`` where ``reason`` feeds the audit record.
    """
    fence = time.monotonic()
    outcome, current = await asyncio.to_thread(
        browser_cli_view.relay_authorize, candidate, proof_not_before=fence
    )
    if outcome != "ok":
        return False, f"stale_upstream:{outcome}"
    if current != port:
        return False, "stale_upstream:port_moved"
    return True, "ok"


def _rewrite_location(location: str, prefix: str) -> str:
    """Re-root an upstream redirect under the prefix, steering the WS with it.

    Only a path-only Location is honored. The upstream is our own supervised
    child and only ever redirects within itself, so anything carrying a scheme
    or host is unexpected — it degrades to the relay root (fail closed) rather
    than sending the browser off-origin.

    The ``ws`` query param value is prefixed with ``browser-view/<token>/``
    because the view SPA constructs its WebSocket URL as ``'/' + <ws param>``:
    rewriting the value is what lands the socket back on this route, carrying
    the capability token.
    """
    parts = urlsplit(location)
    if parts.scheme or parts.netloc:
        logger.warning("browser view relay refused off-origin redirect: %r", location)
        return f"{prefix}/"
    path = parts.path if parts.path.startswith("/") else "/" + parts.path
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    rewritten = [
        (key, f"{prefix.lstrip('/')}/{value}" if key == "ws" else value)
        for key, value in query_pairs
    ]
    query = urlencode(rewritten)
    return f"{prefix}{path}" + (f"?{query}" if query else "")


#: Injected into relayed documents, before their own scripts. In a sandboxed
#: (opaque-origin) document, merely READING ``window.localStorage`` throws a
#: SecurityError, and the view SPA has a bare access that would kill its boot.
#: Redefining the accessor with an in-memory stand-in is permitted even in the
#: sandbox, and per-document storage is exactly the isolation the sandbox
#: promises — nothing persists, nothing is shared with the dashboard. A
#: non-sandboxed context (the loopback fallback URL) keeps its real storage:
#: the shim only installs where the native read throws. Persistence cost,
#: audited against the shipped view SPA bundle: it stores exactly one
#: preference — the ``theme`` key — so a relayed view re-reads the default
#: theme per load. Cosmetic, and cheaper than granting the frame any real
#: origin to persist into.
_STORAGE_SHIM = (
    "<script>(function(){try{void window.localStorage}catch(_){"
    "var mk=function(){var s=Object.create(null);return{"
    "getItem:function(k){return k in s?s[k]:null},"
    "setItem:function(k,v){s[k]=String(v)},"
    "removeItem:function(k){delete s[k]},"
    "clear:function(){s=Object.create(null)},"
    "key:function(i){return Object.keys(s)[i]||null},"
    "get length(){return Object.keys(s).length}}};"
    "Object.defineProperty(window,'localStorage',{value:mk(),configurable:true});"
    "Object.defineProperty(window,'sessionStorage',{value:mk(),configurable:true});"
    "}})()</script>"
)


def _rewrite_html(text: str, prefix: str) -> str:
    """Re-root root-absolute ``src``/``href`` references; arm the storage shim.

    The shim goes right after ``<head>`` so it runs before the SPA's own
    module scripts touch storage.
    """
    for attr in ("src", "href"):
        text = text.replace(f'{attr}="/', f'{attr}="{prefix}/')
    return text.replace("<head>", f"<head>{_STORAGE_SHIM}", 1)


def _rewrite_css(text: str, prefix: str) -> str:
    """Re-root root-absolute ``url(/…)`` references (the codicon font)."""
    return text.replace("url(/", f"url({prefix}/")


def _not_found() -> web.Response:
    """The uniform refusal: bad token, missing token, and no view all match."""
    return web.json_response({"error": "not found", "code": "not_found"}, status=404)


def _busy() -> web.Response:
    """Retryable refusal for a supervisor-lock timeout — token holders only.

    ``busy`` is reachable ONLY by a caller whose candidate matched the live
    token (the lock-free pre-check refuses everyone else without touching the
    lock), so answering it differently from the uniform 404 leaks nothing to
    a prober. And it MUST be answered differently: a 404 on a validly-tokened
    stylesheet is permanent — the browser never refetches it — turning a
    transient start-window lock hold into a broken document. A 503 with
    ``Retry-After`` is the truthful, self-healing answer.
    """
    return web.json_response(
        {"error": "view busy", "code": "busy"},
        status=503,
        headers={"Retry-After": "2"},
    )


def _bad_gateway(code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=502)


def _stamp_relay_headers(response: web.StreamResponse, base_type: str) -> None:
    """The relay's own response headers: CORS-open, everything else sandboxed.

    ``Access-Control-Allow-Origin: *`` because the framed SPA runs in an
    opaque origin (``Origin: null``) and Vite emits ``crossorigin`` script and
    stylesheet tags, which fetch in CORS mode — without the header the
    sandboxed document cannot load its own assets. Opening CORS here cedes
    nothing: the capability token in the path is what gates ACCESS, CORS only
    gates cross-origin READABILITY, and a token-holder already has full access
    by definition (the WebSocket leg was never CORS-gated to begin with).

    The CSP ``sandbox`` stamp goes on EVERY response except script types, not
    just ``text/html``: any scriptable type the upstream can emit —
    ``application/xhtml+xml``, ``image/svg+xml``, the XML+XSLT class, or an
    unknown type a browser may sniff — would otherwise execute on the
    dashboard's real origin when navigated top-level (the port-squat /
    compromised-bundle scenario). On subresources the header is inert, so
    over-stamping costs nothing; script types are exempt because a worker
    created from a relayed script URL is governed by the CSP delivered on the
    script response itself, and a script type never renders as a document.
    ``X-Content-Type-Options: nosniff`` closes the sniffing corner for the
    exempted class.
    """
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if base_type not in _SCRIPT_TYPES:
        response.headers["Content-Security-Policy"] = _DOCUMENT_CSP


async def _relay_ws(
    request: web.Request, candidate: str, port: int, suffix: str
) -> web.WebSocketResponse:
    """Pump one WebSocket bidirectionally between the browser and the view.

    Both legs cap message size; ping/pong stays autopiloted per leg (aiohttp's
    autoping), so only data and close frames cross. Either side closing tears
    down the other.
    """
    downstream = web.WebSocketResponse(max_msg_size=_WS_MAX_MSG_BYTES)
    await downstream.prepare(request)
    target = f"http://{browser_cli_view.LOOPBACK_HOST}:{port}{suffix}"
    session = _client(request.app)
    try:
        upstream = await session.ws_connect(target, max_msg_size=_WS_MAX_MSG_BYTES)
    except (aiohttp.ClientError, OSError) as exc:
        logger.warning("browser view relay could not reach the view WebSocket: %s", exc)
        await downstream.close(code=aiohttp.WSCloseCode.TRY_AGAIN_LATER)
        return downstream

    # Post-connect re-proof BEFORE any frame crosses in either direction: the
    # WebSocket carries the operator's live mouse/keyboard, so a squatter that
    # won the proof→connect race must be refused before the first frame, not
    # discovered mid-stream. One re-proof for the connection's lifetime.
    still_ours, reason = await _upstream_still_ours(candidate, port)
    if not still_ours:
        _audit("denied", reason)
        with contextlib.suppress(Exception):
            await upstream.close()
        await downstream.close(code=aiohttp.WSCloseCode.TRY_AGAIN_LATER)
        return downstream

    async def _pump_down() -> None:
        async for msg in upstream:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await downstream.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await downstream.send_bytes(msg.data)
            else:
                break

    async def _pump_up() -> None:
        async for msg in downstream:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await upstream.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await upstream.send_bytes(msg.data)
            else:
                break

    try:
        down = asyncio.create_task(_pump_down())
        up = asyncio.create_task(_pump_up())
        done, pending = await asyncio.wait({down, up}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Surface a pump failure (a send that raised) rather than swallowing it;
        # a normal close ends the iterator without raising, so this re-raises
        # only genuine transport errors.
        for task in done:
            failure = task.exception()
            if failure is not None and not isinstance(failure, asyncio.CancelledError):
                raise failure
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await downstream.close()
    return downstream


async def api_browser_view_relay(request: web.Request) -> web.StreamResponse:
    """GET/HEAD ``/browser-view/{token}/{tail}`` — the panel's same-origin view.

    Authenticated by the per-instance capability token in the path (see the
    module docstring): the cookie/owner gate cannot apply here because the
    panel frames this surface in an opaque-origin sandbox that sends no
    cookies. Authentication comes FIRST: a request with no token segment is
    refused before the view supervisor is consulted at all, and
    :func:`~kiro_crew.browser_cli.view.relay_authorize` compares the token
    against a lock-free snapshot before touching the supervisor lock — the
    OS-level probes that keep a port-squatter from inheriting the relay (and
    the lock those probes' state snapshot needs) are paid for only by
    callers who already hold the token. The proof runs TWICE per connection:
    once to authorize the lookup, and again after the upstream connection is
    established — before anything is sent downstream — so a child dying
    between proof and connect cannot hand the connection to a squatter (see
    :func:`_upstream_still_ours`). No HTTP health probe anywhere on this
    path: the proxied request itself is the probe, and failing it answers 502
    with a machine-readable code the panel's status poll then reconciles.
    """
    candidate, suffix = _split_token(request)
    # Uniform 404 whether the token is missing, wrong, or there is nothing to
    # relay: an unauthenticated caller must not learn whether a logged-in
    # browser is up. The RESPONSE is uniform; the AUDIT record distinguishes
    # why, because SEL is not readable by the unauthenticated caller — and the
    # distinction is what makes probing visible.
    if candidate is None:
        _audit("denied", "no_token")
        return _not_found()
    # Invalid candidates are refused by relay_authorize's lock-free token
    # pre-check without ever touching the supervisor lock, so a pre-auth
    # flood cannot contend it (or the shared thread pool) at all. Only a
    # caller holding the current token can reach the lock; its bounded wait
    # ("busy") therefore gets a retryable 503 — see :func:`_busy` — while
    # every unauthenticated miss stays the uniform 404.
    outcome, port = await asyncio.to_thread(browser_cli_view.relay_authorize, candidate)
    if outcome == "busy":
        _audit("denied", outcome)
        return _busy()
    if outcome != "ok" or port is None:
        _audit("denied", outcome)
        return _not_found()
    _audit("allowed", f"port:{port}")

    if (
        request.headers.get("Upgrade", "").lower() == "websocket"
        and "upgrade" in request.headers.get("Connection", "").lower()
    ):
        return await _relay_ws(request, candidate, port, suffix)

    prefix = f"{ROUTE_PREFIX}/{candidate}"
    target = f"http://{browser_cli_view.LOOPBACK_HOST}:{port}{suffix}"
    headers = {
        name: request.headers[name] for name in _FORWARD_REQUEST_HEADERS if name in request.headers
    }
    session = _client(request.app)
    try:
        async with session.get(
            target, headers=headers, allow_redirects=False, timeout=_HTTP_TIMEOUT
        ) as upstream:
            # Connection established — re-prove ownership BEFORE any branch
            # sends a byte downstream (redirect, rewritten document, or
            # stream). A stale connection answers the same uniform 404 as the
            # pre-connect denials — on the wire it is indistinguishable from
            # "no view"; the audit record carries the difference. The one
            # exception is a busy re-proof (supervisor lock held past the
            # bound): the caller demonstrably holds the current token, so it
            # gets the retryable 503, matching the pre-connect gate.
            still_ours, reason = await _upstream_still_ours(candidate, port)
            if not still_ours:
                _audit("denied", reason)
                if reason == "stale_upstream:busy":
                    return _busy()
                return _not_found()

            if upstream.status in (301, 302, 303, 307, 308):
                location = upstream.headers.get("Location", "/")
                redirect = web.Response(
                    status=upstream.status,
                    headers={"Location": _rewrite_location(location, prefix)},
                )
                # A redirect is a relayed non-script response like any other:
                # CORS-mode asset fetches (Vite's ``crossorigin`` tags) re-check
                # every hop, so a bare 302 would strand them, and the posture
                # claim — every relayed non-script response is stamped — must
                # stay true on this branch too.
                _stamp_relay_headers(redirect, "")
                return redirect

            content_type = upstream.headers.get("Content-Type", "")
            base_type = content_type.split(";", 1)[0].strip().lower()
            if upstream.status == 200 and base_type in (_REWRITE_HTML, _REWRITE_CSS):
                text = await upstream.text()
                rewritten = (
                    _rewrite_html(text, prefix)
                    if base_type == _REWRITE_HTML
                    else _rewrite_css(text, prefix)
                )
                document = web.Response(
                    status=upstream.status,
                    text=rewritten,
                    content_type=base_type,
                    charset="utf-8",
                )
                _stamp_relay_headers(document, base_type)
                return document

            response = web.StreamResponse(status=upstream.status)
            if content_type:
                response.headers["Content-Type"] = content_type
            for name in _FORWARD_RESPONSE_HEADERS:
                if name in upstream.headers:
                    response.headers[name] = upstream.headers[name]
            # Streamed bodies get the stamp too — XHTML/SVG/XML (and unknown,
            # sniffable types) are documents a browser will execute top-level,
            # exactly the port-squat payoff the stamp exists to close.
            _stamp_relay_headers(response, base_type)
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(_CHUNK_BYTES):
                await response.write(chunk)
            await response.write_eof()
            return response
    except (aiohttp.ClientError, OSError) as exc:
        logger.warning("browser view relay could not reach the view server: %s", exc)
        return _bad_gateway("browser_view_unreachable", "the browser view did not answer")
