"""The reverse proxy from an app's dashboard UI to its own backend (``/apps/{name}/api/``).

Resolving the backend URL, the per-app secret cache the proxy signs with
(``invalidate_app_secret_cache``), the principal claim it forwards, the hop-by-hop and
credential headers it strips, event-stream relaying and ``handle_app_api_proxy``.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import logging
import time

import aiohttp
import yarl
from aiohttp import web

from kiro_crew.apps.backend import get_app_backend_port
from kiro_crew.apps.manager import (
    apps_dir,
    get_app_manifest,
    is_app_enabled,
    resolve_mcp_backend_url,
)
from kiro_crew.apps.proxy_auth import (
    PRINCIPAL_AGENT_TOOL,
    PRINCIPAL_APP_TOKEN,
    PRINCIPAL_NONE,
    PRINCIPAL_OWNER_SESSION,
    PROXY_NONCE_HEADER,
    PROXY_PRINCIPAL_HEADER,
    PROXY_V2_HEADER,
    new_proxy_nonce,
    sign_proxy_principal_claim,
    sign_proxy_request_v2,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


# ---------------------------------------------------------------------------
# Reverse proxy — app dashboard UI → app backend (same-origin, avoids CORS)
# ---------------------------------------------------------------------------

_PROXY_TIMEOUT = 30  # seconds

#: Longest silence tolerated on a proxied response body, streaming or not.
#: ``_PROXY_TIMEOUT`` bounds how long an ORDINARY request may take in total; that
#: bound cannot also cover a stream, whose whole purpose is to stay open. An
#: idle bound covers both: an upstream that stops producing is still cut, and one
#: that keeps producing is not. A server-sent-event stream must therefore emit
#: something -- an event or a comment heartbeat -- at least this often.
_PROXY_IDLE_TIMEOUT = 60  # seconds


def _is_event_stream(content_type: str) -> bool:
    """Whether a proxied response is a server-sent-event stream.

    Matches on the media type alone, so a charset or any other parameter does
    not hide the stream and turn the total timeout back on over it.
    """
    return content_type.split(";", 1)[0].strip().lower() == "text/event-stream"


# App secret cache — secrets don't change after install, no need to read
# from disk on every proxied request.  Invalidated on install/uninstall.
_app_secret_cache: dict[str, str] = {}


def _get_app_secret(name: str) -> str:
    """Read the app secret, using an in-memory cache.

    Empty values are NOT cached — the secret may be provisioned after
    the first proxy attempt (e.g. install-from-source race).
    """
    cached = _app_secret_cache.get(name)
    if cached:
        return cached
    path = apps_dir() / name / ".app_secret"
    secret = path.read_text().strip() if path.is_file() else ""
    if secret:
        _app_secret_cache[name] = secret
    return secret


def invalidate_app_secret_cache(name: str) -> None:
    """Remove a cached secret (call on install/uninstall)."""
    _app_secret_cache.pop(name, None)


_PROXY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Strip sensitive auth headers — app backends authenticate via the per-app
# X-KiroCrew-Proxy HMAC (injected below), never via user cookies or the
# gateway's own internal-auth headers. The internal-auth set (X-Internal-Secret,
# X-Session-Token, X-Session-Key, X-Internal-Caller) is stripped because a
# credentialed internal caller — the design_tweak_update_thread MCP tool is the
# first to reach an app backend through this proxy — would otherwise hand the
# gateway-wide secret (which opens every strict internal route) to an app
# subprocess that also serves content from the user's own projects. The HMAC is
# all the backend needs to trust a proxied request, so dropping the rest is a
# pure hardening with no functional loss.
#
# The gateway-authored proxy headers are stripped too. For an app that has
# not opted in to ``backend.signedPrincipal``, a caller-sent
# ``x-kirocrew-principal`` would otherwise reach a backend that never asked for
# one, and could pass for the gateway's. For the proxy signature and nonce
# headers, stripping the caller's copy first keeps the outgoing request from
# depending on which spelling of a header the HTTP client lets win when the
# gateway sets its own value below, so a page can never supply its own nonce.
# Relay identity headers (``x-kiro-relay-*``) are dropped by prefix, ``is_relay_header``.
_PROXY_STRIP_HEADERS = _PROXY_HOP_HEADERS | frozenset(
    {
        "cookie",
        "authorization",
        "x-internal-secret",
        "x-session-token",
        "x-session-key",
        "x-internal-caller",
        "x-kirocrew-proxy",
        "x-kirocrew-proxy-v2",
        "x-kirocrew-proxy-nonce",
        "x-kirocrew-principal",
        # An app frame's capability (apps/app_frame.py): read by
        # the gateway, never handed on to the app's backend.
        "x-kirocrew-capability",
    }
)


def _resolve_app_backend_url(name: str) -> str | None:
    """Resolve the backend URL for an app.

    For gateway-managed apps: use the tracked backend port.
    For self-managed apps: check manifest for backend.url or mcpServers URL.
    """
    # 1. Gateway-managed backend (spawned by backend.py)
    port = get_app_backend_port(name)
    if port:
        return f"http://127.0.0.1:{port}"

    # 2. Self-managed: check manifest for explicit backend URL
    manifest = get_app_manifest(name)
    if not manifest:
        return None

    # backend.routes field contains the base URL for some apps
    if manifest.backend.entryPoint and manifest.backend.port != "auto":
        try:
            return f"http://127.0.0.1:{int(manifest.backend.port)}"
        except ValueError:
            pass

    # 3. Fallback: derive from the MCP server URL (common for self-managed apps)
    # e.g. crew-companion declares mcpServers."crew-companion".url =
    # "http://127.0.0.1:7778/mcp" -> the backend is at http://127.0.0.1:7778
    #
    # Shared with register_builtin_apps(), which uses the SAME function to decide
    # whether to issue the .app_secret this proxy signs with. Keeping one
    # definition is load-bearing: if resolution and secret issuance disagree, an
    # app resolves a backend here and is then refused below with 502 "has no
    # secret", which is not detectable at registration time.
    return resolve_mcp_backend_url(manifest.mcpServers)


def _proxy_principal(request: web.Request) -> tuple[str, str]:
    """The ``(kind, owner_id)`` an opted-in backend's principal claim carries.

    Read only from what ``token_auth_middleware`` already authenticated, never from
    a request header, and checked in this order:

    1. ``agent-tool``: internal authentication granted the call. MCP tools, agents,
       subagents and crons reach admitted routes this way. It is checked first so a
       request that also carries an owner cookie can never inherit the owner's trust.
    2. ``app-token``: the caller holds an app token.
    3. ``owner-session``: a dashboard session cookie authenticated the request, not a
       ``?token=`` link, and the shared owner predicate matches. Only this kind names
       an owner.
    4. ``none``: everything else.
    """
    if request.get("internal_auth") is True:
        return PRINCIPAL_AGENT_TOOL, ""
    if request.get("app"):
        return PRINCIPAL_APP_TOKEN, ""
    if request.get("is_dashboard_user") is True and request.get("auth_from_query_token") is False:
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        if is_owner_dashboard_request(request):
            return PRINCIPAL_OWNER_SESSION, str(request.get("user") or "")
    return PRINCIPAL_NONE, ""


async def handle_app_api_proxy(request: web.Request) -> web.StreamResponse:
    """Reverse proxy: /apps/{name}/api/{path} → app backend.

    Allows dashboard app UIs to call their own backend through the gateway
    (same-origin), avoiding CORS issues. The gateway authenticates the
    request and forwards it to the app's backend.
    """
    name = request.match_info["name"]
    path = request.match_info.get("path", "")

    # Path traversal guard (input validation first)
    if ".." in path:
        return web.json_response({"error": "invalid path"}, status=400)

    # Cross-app guard (CWE-269): if the caller authenticated with an APP token
    # (``request["app"]`` set by token_auth_middleware), it may only proxy into
    # its OWN backend. Dashboard-user requests (empty app identity) are allowed
    # to any app's proxy — that's the in-dashboard app UI calling same-origin.
    # The middleware's app-scope gate already blocks this, but the proxy is a
    # trust boundary (it signs the request with the target app's secret), so we
    # re-check here rather than rely solely on upstream.
    token_app = request.get("app", "")
    if token_app and token_app != name:
        # SEL audit for the permission decision (cross-app escalation attempt),
        # matching the sibling deny paths that emit log_api_access.
        sel().log_api_access(
            caller=token_app,
            operation="app_proxy_cross_app",
            outcome="denied",
            source="app_routes",
            resources=f"/apps/{name}/{path}",
            error="app token cannot access another app's backend",
        )
        return web.json_response(
            {"error": "app token cannot access another app's backend"}, status=403
        )

    # Enablement gate. The checks above prove WHO is calling; this proves the app
    # is allowed to run at all. Without it, an app the user never turned on -- every
    # builtin ships `defaultEnabled: false` -- still had an authenticated, secret-signed
    # proxy to its backend, so a mutation could reach a local app that was never
    # activated. Governance denial is covered transitively: a denied app cannot be
    # activated, so it is never enabled.
    #
    # Deliberately NOT folded into _resolve_app_backend_url: that resolver is shared
    # with register_builtin_apps(), where an app is legitimately not yet enabled, and
    # returning None here would surface refusal as the same misleading 502 "no
    # reachable backend" that sharing the resolver was meant to eliminate. This is an
    # authorization decision, so it sits with the other authorization checks and says
    # so with 403.
    if not await asyncio.to_thread(is_app_enabled, name):
        # SEL audit for the permission decision, matching the sibling deny path
        # above. An authorization denial that leaves no trail is invisible to the
        # audit log, so a repeated probe against a disabled app's backend would be
        # unobservable — which is most of the value of having the gate.
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="app_proxy_disabled_app",
            outcome="denied",
            source="app_routes",
            resources=f"/apps/{name}/{path}",
            error="app is not enabled",
        )
        # `code` is required by test_error_code_contract.py, and is the right shape
        # here regardless: the dashboard renders `error` prose verbatim into a
        # localized page, so the machine-readable identifier is what a client can
        # switch on (and translate) while the sentence stays advisory.
        return web.json_response(
            {"code": "app_not_enabled", "error": f"app {name!r} is not enabled"},
            status=403,
        )

    # Resolve backend URL
    backend_url = _resolve_app_backend_url(name)
    if not backend_url:
        return web.json_response(
            {"error": f"app {name!r} has no reachable backend"},
            status=502,
        )

    # Build target URL — preserve the exact wire encoding of path and query
    # params so the gateway's HMAC signature matches what the backend sees on
    # self.path. `request.query_string` is DECODED by aiohttp, so signing it causes
    # HMAC verification to fail closed (401) whenever query parameters contain
    # percent-encodable characters like spaces, non-ASCII, or '+'.
    raw_qs = request.rel_url.raw_query_string
    target_path = f"/api/{path}" + (f"?{raw_qs}" if raw_qs else "")
    target_url = yarl.URL(f"{backend_url}{target_path}", encoded=True)
    wire_target = target_url.raw_path_qs

    # Forward headers (strip hop-by-hop, inject proxy auth)
    from kiro_crew.dashboard.relay_headers import is_relay_header

    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered not in _PROXY_STRIP_HEADERS and lowered != "host" and not is_relay_header(key):
            headers[key] = value

    # Read request body first so the HMAC can bind it (integrity: prevents
    # a MITM/compromised path from swapping the body under a valid signature).
    body = await request.read() if request.can_read_body else None

    # Sign the proxy request with the app's secret so the backend can
    # verify it came from the gateway. Works on loopback and remote.
    # Header format: X-KiroCrew-Proxy: <timestamp>:<hmac-sha256>
    # The HMAC is computed over "timestamp:method:path[?query]:sha256(body)"
    # using the app secret as key. Backend verifies by recomputing with its
    # copy of the secret and checking the timestamp is recent (±60s).
    try:
        secret = _get_app_secret(name)
        if not secret:
            return web.json_response(
                {"error": f"app {name!r} has no secret — cannot authenticate proxy request"},
                status=502,
            )
        ts = str(int(time.time()))
        body_hash = hashlib.sha256(body or b"").hexdigest()
        msg = f"{ts}:{request.method}:{wire_target}:{body_hash}"
        sig = _hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        headers["X-KiroCrew-Proxy"] = f"{ts}:{sig}"
        # The replay-proof v2 signature rides in separate headers beside the
        # unchanged v1 value, so a backend that only knows v1 ignores them.
        nonce = new_proxy_nonce()
        headers[PROXY_NONCE_HEADER] = nonce
        headers[PROXY_V2_HEADER] = sign_proxy_request_v2(
            secret=secret,
            ts=int(ts),
            nonce=nonce,
            method=request.method,
            target=wire_target,
            body=body or b"",
        )
    except OSError as exc:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs the app name and the OSError only and never the secret value
        logger.warning("Failed to read app secret for %s: %s", name, exc)
        return web.json_response(
            {"error": "proxy auth failed: cannot read app secret"},
            status=502,
        )

    # Opt-in only: an app without ``backend.signedPrincipal`` receives no principal
    # header. Read off the loop, like the enablement gate above, because it is a file.
    manifest = await asyncio.to_thread(get_app_manifest, name)
    if manifest is not None and manifest.backend.signedPrincipal:
        principal_kind, owner_id = _proxy_principal(request)
        try:
            headers[PROXY_PRINCIPAL_HEADER] = sign_proxy_principal_claim(
                kind=principal_kind,
                owner_id=owner_id,
                method=request.method,
                target=wire_target,
                body=body or b"",
                secret=secret,
            )
        except ValueError as exc:
            # The signer refuses a claim outside its one valid shape, such as an owner
            # id with a control character. Forwarding the request without its claim
            # would hand an opted-in backend a request it cannot classify, so refuse.
            logger.warning("Failed to sign the principal claim for %s: %s", name, exc)
            return web.json_response(
                {
                    "code": "proxy_principal_sign_failed",
                    "error": "proxy auth failed: cannot sign principal claim",
                },
                status=502,
            )

    # Set once the relay has begun. The two handlers below consult it: after
    # the head has gone out, no status of the gateway's own can be sent any more.
    resp: web.StreamResponse | None = None
    try:
        # The total bound is enforced HERE rather than handed to aiohttp as
        # ``total``, because ``total`` also covers reading the response body and a
        # stream's body does not end: one clock cannot bound an ordinary request
        # without cutting every stream mid-body, which reaches the browser as a
        # truncated chunked response rather than as an error. Enforced outside the
        # client, the same bound is liftable once the response says it is a stream.
        # ``sock_read`` stays armed for both kinds, so a silent upstream is cut
        # either way.
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=_PROXY_TIMEOUT,
            sock_read=_PROXY_IDLE_TIMEOUT,
        )
        session = request.app.get("_proxy_session")
        owns_session = session is None or session.closed
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            async with asyncio.timeout(_PROXY_TIMEOUT) as total_bound:
                async with session.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    data=body,
                    timeout=timeout,
                    allow_redirects=False,
                ) as upstream:
                    if _is_event_stream(upstream.headers.get("Content-Type", "")):
                        # A stream has no total. Lifted here rather than skipped
                        # up front because only the response says what it is, and
                        # the request's own leg stays bounded either way.
                        total_bound.reschedule(None)
                    # Stream response back
                    resp = web.StreamResponse(
                        status=upstream.status,
                        headers={
                            k: v
                            for k, v in upstream.headers.items()
                            if k.lower() not in _PROXY_HOP_HEADERS
                        },
                    )
                    try:
                        await resp.prepare(request)
                        async for chunk in upstream.content.iter_any():
                            await resp.write(chunk)
                        await resp.write_eof()
                    except (ConnectionResetError, ConnectionAbortedError):
                        # The upstream request may finish after the browser has
                        # already closed its side of the proxy stream. Do not turn
                        # that routine client disconnect into a gateway traceback.
                        pass
                    return resp
        finally:
            if owns_session:
                await session.close()
    except aiohttp.ClientError as exc:
        logger.warning("Proxy to app %s failed: %s", name, exc)
        if resp is not None and resp.prepared:
            return _end_relay_midbody(request, resp)
        return web.json_response(
            {"error": "backend unreachable"},
            status=502,
        )
    except asyncio.TimeoutError:
        if resp is not None and resp.prepared:
            return _end_relay_midbody(request, resp)
        return web.json_response({"error": "backend timeout"}, status=504)


def _end_relay_midbody(request: web.Request, resp: web.StreamResponse) -> web.StreamResponse:
    """End a relayed response whose head the client has already received.

    The upstream failed or went silent after ``resp.prepare()``. A fresh
    ``json_response`` at that point is not a reply: aiohttp writes its status
    line and headers INTO the chunked body already in flight, so the client gets
    a 200 whose body carries a second ``HTTP/1.1 502`` head, and the connection
    then idles on keep-alive. Aborting the transport instead leaves the body
    unterminated -- the one shape every client reads as a failed transfer
    (``ERR_INCOMPLETE_CHUNKED_ENCODING``; an ``EventSource`` reconnects) -- and
    frees the connection nothing more will be written on. Abort, not close:
    abort discards whatever a slow client still has buffered, so the terminating
    chunk the server's own post-handler ``write_eof`` would add can never be
    flushed behind it and make a cut body look complete. ``force_close`` keeps
    the server from offering keep-alive on it, and that ``write_eof`` fails on
    the gone transport as a routine disconnect.
    """
    resp.force_close()
    transport = request.transport
    if transport is not None and not transport.is_closing():
        transport.abort()
    return resp
