"""The same-origin Remote Crew pane relay (`/instance-pane/{capability}/{tail}`).

Two layers are exercised here, both against real sockets so what is asserted is
the wire behaviour a browser sees, not an implementation detail:

* **The relay** (:class:`InstancePaneRelay`) is driven through an aiohttp
  ``TestClient``, with a real loopback stub standing in for a connected peer
  crew's dashboard and a :class:`_FakePeer` that forwards to it exactly the way
  ``SshTunnelManager`` would — building the request headers from scratch and
  adding its own port-scoped session cookie, so the RELAY's header discipline,
  path/query fidelity, redirect and HTML rewrites, SSE streaming, WebSocket
  pumping, and the uniform-404 capability gate are what the tests observe.
* **The issuer** (``api_instances_open_pane``) is driven at its owner boundary:
  the enabled gate, the strict owner gate, the access-mode contract, the
  fail-closed protocol version gate, and the two endpoint variants.

The relay's security contract is: a missing, malformed, unknown, expired, or
stale-generation capability all answer ONE uniform 404 before the peer is
touched; a live capability forwards only allow-listed request headers (never the
browser's cookies, Authorization, Origin, or Referer) and strips the peer's
Set-Cookie downstream; the entry document is relocated by re-rooting its
root-absolute entry markers under the capability prefix (never a ``<base>``, and
never by rewriting JavaScript); redirects stay same-origin or fail closed; and
the capability, channel, and request line never enter a log.
"""

from __future__ import annotations

import contextlib
import json
from typing import AsyncIterator

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.instance_pane_relay import (
    PANE_RELAY_PROTOCOL,
    InstancePaneRelay,
    api_instance_pane_relay,
)
from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

# The relay handler resolves its state with ``request.app["state"]`` (a plain
# string key, the dashboard-wide convention), so this harness's ``app["state"]``
# must mirror it — a typed ``web.AppKey`` would not be found by the production
# lookup. aiohttp's NotAppKeyWarning is therefore inherent to exercising the real
# handler; scope it OFF for this file only (never globally, and never a
# production warning) so the suite's warning output stays signal.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("ignore::aiohttp.web.NotAppKeyWarning"),
]

INSTANCE_ID = "cd-1"

INDEX_HTML = (
    "<!DOCTYPE html><html><head>"
    '<link rel="icon" href="/favicon.ico">'
    '<link rel="modulepreload" href="/assets/chunk-def.js">'
    '<script type="module" src="/assets/index-abc.js"></script>'
    # A RELATIVE reference (a self-locating chunk/asset the relocatable build
    # emits): the rewrite must leave it untouched so it resolves against the
    # document URL under whatever prefix served the entry.
    '<link rel="stylesheet" href="assets/index-abc.css">'
    "</head><body>pane</body></html>"
)
#: A JS body that CONTAINS a root-literal ``/api`` and ``src="/x"`` — the relay
#: must stream it byte-for-byte and never rewrite it (only the HTML is rewritten).
JS_BODY = 'const u="/api/thing";const s=`<img src="/logo.png">`;export default u+s;'


# ── the peer stub: the shapes a connected crew's dashboard serves ────────────


def _stub_upstream(hits: list[dict]) -> web.Application:
    """A real loopback app standing in for the peer crew's dashboard.

    Records every inbound request (path+query and headers) in *hits* so the
    tests can prove exactly what the relay forwarded — and what it did NOT.
    """
    app = web.Application()

    @web.middleware
    async def record(request: web.Request, handler):
        hits.append(
            {
                "path_qs": request.raw_path,
                "method": request.method,
                "headers": dict(request.headers),
            }
        )
        return await handler(request)

    app.middlewares.append(record)

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def js(_request: web.Request) -> web.Response:
        return web.Response(text=JS_BODY, content_type="application/javascript")

    async def echo(request: web.Request) -> web.Response:
        # Echo the exact path+query the peer received, so the test can assert the
        # relay preserved it byte-for-byte through the capability strip.
        return web.json_response({"path_qs": request.raw_path})

    async def session_gate(request: web.Request) -> web.Response:
        # A session-scoped endpoint that FAILS CLOSED without a session identity
        # (exactly what a slot-restricted / incognito-enforcing peer handler does)
        # and otherwise reports the exact key it received — so a test can prove the
        # relay forwarded the dashboard's identity verbatim and did not fabricate,
        # drop, or rewrite it.
        key = request.headers.get("X-Session-Key")
        if not key:
            return web.json_response({"error": "no session"}, status=401)
        return web.json_response({"session_key": key})

    async def setcookie(_request: web.Request) -> web.Response:
        resp = web.Response(body=b"x", content_type="application/octet-stream")
        resp.headers["Set-Cookie"] = "peer_sess=LEAK; Path=/"
        resp.headers["Clear-Site-Data"] = '"cookies"'
        resp.headers["ETag"] = '"v1"'
        return resp

    async def cached_artifact(_request: web.Request) -> web.Response:
        # A peer answering authenticated bytes with a one-YEAR private immutable
        # lifetime — the exact header handlers/artifacts.py emits. Forwarding it
        # would keep the bytes fresh in the browser long after the 15-minute
        # capability expires or its tunnel generation is revoked, with no request
        # for the network gate to reject. The relay must replace it with no-store.
        resp = web.Response(body=b"secret-bytes", content_type="application/octet-stream")
        resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        resp.headers["ETag"] = '"artifact-v1"'
        return resp

    async def sse(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200)
        resp.headers["Content-Type"] = "text/event-stream"
        await resp.prepare(request)
        for i in range(3):
            await resp.write(f"data: event-{i}\n\n".encode())
        await resp.write_eof()
        return resp

    async def big_html(request: web.Request) -> web.Response:
        # An entry document of a caller-chosen byte length, for the bounded-buffer
        # boundary test. Served as text/html at 200 so it takes the relay's
        # rewrite (buffered) path rather than the streamed passthrough.
        n = int(request.query.get("bytes", "0"))
        return web.Response(body=b"x" * n, content_type="text/html")

    async def redir(request: web.Request) -> web.Response:
        # ?to= names the Location so one endpoint covers path-only, absolute, and
        # protocol-relative redirects.
        raise web.HTTPFound(request.query.get("to", "/"))

    async def ws(request: web.Request) -> web.WebSocketResponse:
        server = web.WebSocketResponse()
        await server.prepare(request)
        async for msg in server:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await server.send_str(f"echo:{msg.data}")
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await server.send_bytes(msg.data + b"!")
            else:
                break
        return server

    app.router.add_get("/", index)
    app.router.add_get("/assets/index-abc.js", js)
    app.router.add_route("*", "/echo", echo)
    app.router.add_route("*", "/session-gate", session_gate)
    app.router.add_get("/setcookie", setcookie)
    app.router.add_get("/cached-artifact", cached_artifact)
    app.router.add_get("/sse", sse)
    app.router.add_get("/big-html", big_html)
    app.router.add_get("/redir", redir)
    app.router.add_get("/ws", ws)
    return app


class _FakePeer:
    """A :class:`PaneRelayPeer` that forwards to a real loopback stub.

    Mimics ``SshTunnelManager`` faithfully where it matters to the relay: it
    resolves the target from the instance id alone (never from the request), it
    builds the upstream request headers FROM SCRATCH and adds its own port-scoped
    session cookie (so a browser credential can only reach the peer if the relay
    wrongly forwarded it), and it never follows an upstream redirect. ``epoch``
    and ``port`` model the forward generation the relay binds a lease to.
    """

    #: The one cookie value the manager would send upstream; a browser value
    #: reaching the peer would be a leak the tests catch.
    SESSION = "mc_sess_OK"

    def __init__(self, port: int) -> None:
        self._port = port
        self._epoch = 1
        #: When set, the NEXT forward bumps the generation at its very start —
        #: BEFORE selecting the target and validating ``expected_forward`` —
        #: modelling a rebuild that lands in the window between the relay's lease
        #: resolution and the manager's own url/credential selection. This is the
        #: deterministic interleaving the generation-binding fix must refuse.
        self.bump_on_next_forward = False

    # generation controls the tests drive
    def bump_generation(self) -> None:  # disconnect / rebuild / tunnel replaced
        self._epoch += 1

    def go_down(self) -> None:  # removal / not connected
        self._port = 0

    # PaneRelayPeer surface
    def peer_forward_snapshot(self, instance_id: str) -> tuple[int, int]:
        return (self._port, self._epoch)

    def peer_forward_current(self, instance_id: str, snapshot: tuple[int, int]) -> bool:
        return self._port > 0 and (self._port, self._epoch) == snapshot

    def _require_forward(self, expected_forward) -> None:
        """Mirror the manager: select the target from the LIVE forward, then
        refuse before any byte unless that forward still matches the generation
        the caller's lease named. Selection and this check share one attempt, so
        a rebuild that lands between the relay's resolve and here is caught."""
        if self.bump_on_next_forward:
            self.bump_on_next_forward = False
            self.bump_generation()
        snapshot = self.peer_forward_snapshot("_")
        if expected_forward is not None and snapshot != expected_forward:
            raise ProxyRequestError(
                "proxy_peer_not_connected",
                "forward generation moved since the lease was issued",
                http_status=503,
            )

    @contextlib.asynccontextmanager
    async def proxy_request(
        self,
        instance_id,
        method,
        path,
        *,
        data=None,
        content_type="",
        extra_headers=None,
        expected_forward=None,
    ):
        self._require_forward(expected_forward)
        headers: dict[str, str] = {}
        if extra_headers:
            for key, value in extra_headers.items():
                # The manager floor: a caller can never smuggle a credential or a
                # spoofed authority across the tunnel.
                if key.lower() in ("cookie", "authorization", "host", "content-length"):
                    continue
                headers[key] = value
        if content_type:
            headers["Content-Type"] = content_type
        headers["Cookie"] = f"mc_token_{self._port}={self.SESSION}"
        url = f"http://127.0.0.1:{self._port}/{path.lstrip('/')}"
        session = aiohttp.ClientSession()
        try:
            resp = await session.request(
                method, url, data=data, headers=headers, allow_redirects=False
            )
            try:
                yield resp
            finally:
                resp.release()
        finally:
            await session.close()

    @contextlib.asynccontextmanager
    async def proxy_websocket(
        self, instance_id, path, *, subprotocols=(), max_msg_size=0, expected_forward=None
    ):
        self._require_forward(expected_forward)
        url = f"http://127.0.0.1:{self._port}/{path.lstrip('/')}"
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(
                url,
                protocols=subprotocols,
                max_msg_size=max_msg_size,
                headers={"Origin": f"http://127.0.0.1:{self._port}"},
            )
            try:
                yield ws
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()
        finally:
            await session.close()


class _RelayState:
    """Minimal dashboard-state stand-in carrying the relay."""

    def __init__(self, relay: InstancePaneRelay) -> None:
        self.instance_pane_relay = relay


class _Harness:
    def __init__(self, client: TestClient, peer: _FakePeer, relay: InstancePaneRelay, hits):
        self.client = client
        self.peer = peer
        self.relay = relay
        self.hits = hits

    def issue_path(self) -> str:
        grant = self.relay.issue(INSTANCE_ID)
        assert grant is not None
        return grant.document_path  # /instance-pane/<capability>/


@contextlib.asynccontextmanager
async def _harness(**relay_kwargs) -> "AsyncIterator[_Harness]":
    hits: list[dict] = []
    stub_server = TestServer(_stub_upstream(hits))
    await stub_server.start_server()
    peer = _FakePeer(stub_server.port)
    relay = InstancePaneRelay(peer, **relay_kwargs)

    app = web.Application()
    app["state"] = _RelayState(relay)
    app.router.add_route("*", "/instance-pane", api_instance_pane_relay)
    app.router.add_route("*", "/instance-pane/{tail:.*}", api_instance_pane_relay)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield _Harness(client, peer, relay, hits)
    finally:
        await client.close()
        await stub_server.close()
        await relay.close()


# ── the uniform-404 capability gate ──────────────────────────────────────────


class TestCapabilityGate:
    async def test_admits_a_live_capability(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}assets/index-abc.js")
            assert resp.status == 200
            assert (await resp.text()) == JS_BODY

    async def test_missing_capability_is_404(self) -> None:
        async with _harness() as h:
            bare = await h.client.get("/instance-pane")
            assert bare.status == 404
            # The stub was never consulted for a capability-less request.
            assert h.hits == []

    async def test_unknown_capability_is_404(self) -> None:
        async with _harness() as h:
            resp = await h.client.get("/instance-pane/BOGUScapability/assets/index-abc.js")
            assert resp.status == 404
            assert h.hits == []

    async def test_empty_capability_segment_is_404(self) -> None:
        async with _harness() as h:
            resp = await h.client.get("/instance-pane//assets/index-abc.js")
            assert resp.status == 404

    async def test_expired_capability_is_404(self) -> None:
        async with _harness(ttl_seconds=0) as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}echo")
            assert resp.status == 404
            assert h.hits == []

    async def test_stale_generation_is_404(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            # A rebuild / tunnel replacement advances the generation the lease was
            # bound to. The capability is now dead — before the peer is touched.
            h.peer.bump_generation()
            resp = await h.client.get(f"{base}echo")
            assert resp.status == 404
            assert h.hits == []

    async def test_port_reuse_alone_does_not_revive_a_stale_lease(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            # Disconnect (port zeroed) then reconnect onto the SAME port but a new
            # generation: the port matches, the generation does not, so the lease
            # stays dead. This is the port-reuse window the generation closes.
            reused_port = h.peer._port
            h.peer.go_down()
            h.peer._port = reused_port
            h.peer.bump_generation()
            resp = await h.client.get(f"{base}echo")
            assert resp.status == 404

    async def test_not_connected_is_404(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            h.peer.go_down()
            resp = await h.client.get(f"{base}echo")
            assert resp.status == 404

    async def test_every_refusal_is_byte_equivalent(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            h.peer.bump_generation()  # make the live capability stale
            bodies = []
            for path in ("/instance-pane", "/instance-pane/BOGUS/x", f"{base}echo"):
                resp = await h.client.get(path)
                assert resp.status == 404
                bodies.append(await resp.read())
            assert bodies[0] == bodies[1] == bodies[2]


# ── forwarding fidelity + credential/header discipline ───────────────────────


class TestForwarding:
    async def test_exact_path_and_query_are_preserved(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}echo?a=1&a=2&b=%20&c")
            assert resp.status == 200
            # The peer received the suffix verbatim, capability stripped.
            assert (await resp.json())["path_qs"] == "/echo?a=1&a=2&b=%20&c"

    async def test_method_is_preserved(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.post(f"{base}echo", data=b"payload")
            assert resp.status == 200
            assert h.hits[-1]["method"] == "POST"

    async def test_browser_credentials_never_cross_the_tunnel(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(
                f"{base}echo",
                headers={
                    "Cookie": "hub_sess=SENTINEL_COOKIE",
                    "Authorization": "Bearer SENTINEL_TOKEN",
                    "Origin": "https://hub.example",
                    "Referer": "https://hub.example/dash",
                    "X-Forwarded-For": "1.2.3.4",
                    "Range": "bytes=0-9",
                    "If-None-Match": '"etag-1"',
                    "X-Session-Key": "dashboard:ui",
                },
            )
            assert resp.status == 200
            upstream = h.hits[-1]["headers"]
            # The allow-listed request headers rode through …
            assert upstream.get("Range") == "bytes=0-9"
            assert upstream.get("If-None-Match") == '"etag-1"'
            # … the SPA's session identity (an application header, not a
            # credential) rode through so session-scoped peer behaviour holds …
            assert upstream.get("X-Session-Key") == "dashboard:ui"
            # … the manager's own session cookie reached the peer …
            assert h.peer.SESSION in upstream.get("Cookie", "")
            # … and NONE of the browser's ambient identity did.
            assert "SENTINEL_COOKIE" not in upstream.get("Cookie", "")
            assert "Authorization" not in upstream
            assert "Origin" not in upstream
            assert "Referer" not in upstream
            assert "X-Forwarded-For" not in upstream

    async def test_session_key_reaches_a_session_scoped_peer_endpoint(self) -> None:
        # An endpoint that REFUSES a missing session key must see the exact key
        # the dashboard sent — the relay forwards it verbatim through the narrow
        # application-header allow-list.
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(
                f"{base}session-gate", headers={"X-Session-Key": "dashboard:ui"}
            )
            assert resp.status == 200
            assert (await resp.json())["session_key"] == "dashboard:ui"

    async def test_active_slot_session_key_is_forwarded_verbatim(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(
                f"{base}session-gate", headers={"X-Session-Key": "slot:temporary-7"}
            )
            assert resp.status == 200
            assert (await resp.json())["session_key"] == "slot:temporary-7"

    async def test_missing_session_key_lets_the_peer_fail_closed(self) -> None:
        # The relay never fabricates a session identity: with no X-Session-Key the
        # peer's own fail-closed 401 rides straight back, so restricted-session
        # enforcement is preserved rather than bypassed.
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}session-gate")
            assert resp.status == 401

    async def test_peer_set_cookie_is_stripped_downstream(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}setcookie")
            assert resp.status == 200
            assert "Set-Cookie" not in resp.headers
            assert "Clear-Site-Data" not in resp.headers
            # A validator on the allow-list still rides back.
            assert resp.headers.get("ETag") == '"v1"'

    async def test_response_is_opaque_and_uncacheable(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}setcookie")
            assert resp.headers.get("Access-Control-Allow-Origin") == "null"
            assert "Access-Control-Allow-Credentials" not in resp.headers
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert "sandbox" in resp.headers.get("Content-Security-Policy", "")
            # Every relayed response carries no-store, so no browser cache entry
            # can outlive the capability lease.
            assert resp.headers.get("Cache-Control") == "no-store"

    async def test_peer_one_year_private_cache_is_replaced_with_no_store(self) -> None:
        # The confirmed cache-exceeds-lease gap: a peer's `private,
        # max-age=31536000, immutable` on authenticated bytes must NOT survive
        # onto the relayed response. The relay drops the forwarded Cache-Control
        # and stamps no-store at its boundary, so the browser writes no cache
        # entry that could be read after expiry or generation revocation.
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}cached-artifact")
            assert resp.status == 200
            assert (await resp.read()) == b"secret-bytes"
            cache_control = resp.headers.get("Cache-Control")
            assert cache_control == "no-store"
            assert "max-age=31536000" not in (cache_control or "")
            assert "immutable" not in (cache_control or "")
            # Exactly one Cache-Control header reaches the browser (the peer's is
            # replaced, not appended), so no stale directive rides alongside it.
            assert len(resp.headers.getall("Cache-Control")) == 1
            # The validator still rides through for conditional revalidation,
            # which re-enters the relay and is re-gated.
            assert resp.headers.get("ETag") == '"artifact-v1"'


# ── document rewrite (HTML only, never JS) ────────────────────────────────────


class TestRewrite:
    async def test_entry_html_reroots_markers_without_a_base_and_stays_sandboxed(self) -> None:
        async with _harness() as h:
            base = h.issue_path()  # /instance-pane/<cap>/
            prefix = base.rstrip("/")  # /instance-pane/<cap>
            resp = await h.client.get(base)
            assert resp.status == 200
            text = await resp.text()
            # NO <base> element. A <base> would retarget in-document SVG url(#id)
            # fragments and #hash anchors against the base URL instead of the
            # current document URL (build contract: check-build-output.mjs). The
            # entry markers are re-rooted directly instead.
            assert "<base" not in text
            # Root-absolute entry markers re-rooted under the capability prefix.
            assert f'href="{prefix}/favicon.ico"' in text
            assert f'href="{prefix}/assets/chunk-def.js"' in text
            assert f'src="{prefix}/assets/index-abc.js"' in text
            # A RELATIVE reference is left untouched so it self-locates against
            # the document URL under the prefix — no <base> needed.
            assert 'href="assets/index-abc.css"' in text
            assert f'href="{prefix}/assets/index-abc.css"' not in text
            # The entry document is sandboxed and uncacheable.
            assert "sandbox" in resp.headers.get("Content-Security-Policy", "")
            assert resp.headers.get("Cache-Control") == "no-store"

    async def test_entry_html_injects_the_premodule_bootstrap(self) -> None:
        """The relay entry document carries the pre-module bootstrap, placed
        before the deferred app module so the opaque-origin pane's Storage shims
        install before any imported module reads storage. The injected script is
        classic (no ``type="module"``) so it runs during parse, and it agrees
        with website/src/lib/relayPaneBootstrap.ts on the shared wire tokens."""
        async with _harness() as h:
            base = h.issue_path()
            prefix = base.rstrip("/")
            resp = await h.client.get(base)
            text = await resp.text()
            # The bootstrap is present and is a CLASSIC script (never a module,
            # which would defer past the app bundle it must precede).
            assert "relay pane pre-module bootstrap" in text
            marker = "(function(w,d){"
            assert marker in text
            # The entry app module is INERTED (its ``type="module"`` rewritten to
            # the gate sentinel) so the browser never auto-runs it; the bootstrap
            # releases it once shims are installed. Its ``src`` is still re-rooted.
            module_src = f'src="{prefix}/assets/index-abc.js"'
            assert module_src in text
            assert 'type="application/kc-relay-gated"' in text
            assert 'type="module"' not in text  # the one module tag was inerted
            # The bootstrap precedes (and will release) that gated module.
            assert text.index(marker) < text.index(module_src)
            # It sits at the very top of <head>, before any marker, and there is
            # no <base> element for it to depend on.
            assert "<base" not in text
            assert text.index("<head>") < text.index(marker) < text.index(module_src)
            # Shared contract with the TS bootstrap: envelope tag/version, the
            # channel field, both storage message types, the bootstrap-handshake
            # message types (subsequent-document reseed), the gate sentinel, and
            # the caps. A drift in any of these silently breaks parent<->pane
            # attribution or the reseed, so pin them.
            for token in (
                "__kcRelayPane",
                "mcPaneChannel",
                "mc-relay-storage",
                "mc-relay-storage-update",
                "mc-relay-bootstrap-request",
                "mc-relay-bootstrap-reply",
                "application/kc-relay-gated",
                "__kcRelayPaneContext",
                "maxKeys:200",
                "maxValueBytes:262144",
                # The document-bound downward channel: the child mints a
                # MessageChannel per document and transfers one port to the parent
                # in its request, so downward delivery binds to the authenticated
                # document, never a wildcard post a replacement document receives.
                "MessageChannel",
                "port2",
            ):
                assert token in text, token

    async def test_bootstrap_is_fail_closed_and_document_port_bound(self) -> None:
        """The bootstrap NEVER releases the gated app module with an empty channel,
        and it establishes a document-bound port before any downward authority.

        These pin the two confirmed P1 fixes at the level of the injected runtime:
          1. A handshake that never authenticates leaves the module gated (no
             empty-shim, empty-channel release, and so no empty-storage reload
             loop) — the parent's readiness watchdog owns recovery.
          2. The child hands the parent one port of a ``MessageChannel`` and adopts
             the channel over the REPLY, so a document replacing this one in the
             same iframe (its ``WindowProxy`` survives navigation) gets no port,
             no channel, and no host model.
        """
        from kiro_crew.dashboard.instance_pane_relay import _RELAY_PANE_BOOTSTRAP_SCRIPT as s

        # The document port handshake: a fresh MessageChannel, the request carries
        # the capability documentPath + a nonce, and one port is TRANSFERRED.
        assert "new MessageChannel()" in s
        assert "mc-relay-bootstrap-request" in s
        assert "[mc.port2]" in s  # exactly the transfer that binds downward delivery
        # The reply is validated by the per-document nonce, and finalize adopts the
        # channel/origin from the REPLY (never from window.name, the storage-only seed).
        assert "r.nonce!==nonce" in s
        assert "mc-relay-bootstrap-reply" in s
        # FAIL CLOSED: there is no empty-channel release path. The old 2s fallback
        # that installed empty shims and released the app (``done('','',1,null)``)
        # is gone, and a bootstrap error is swallowed WITHOUT releasing.
        assert "done('','',1,null)" not in s
        assert "installShims('','',1" not in s
        assert "fail closed" in s  # the catch/off-capability comments name the contract
        # The gate is released only through ``finalize`` (the authenticated reply)
        # and the ``released`` latch makes it exactly once.
        assert "function finalize(" in s
        assert "released=true" in s

    async def test_rewrite_without_head_prepends_bootstrap_and_no_base(self) -> None:
        """A degenerate entry with no <head> still gets the bootstrap prepended
        (fail-safe), and still carries no <base>. Exercises the pure rewrite."""
        from kiro_crew.dashboard.instance_pane_relay import _rewrite_entry_html

        out = _rewrite_entry_html("<!DOCTYPE html><html><body>x</body></html>", "/instance-pane/c")
        assert "<base" not in out
        assert out.startswith("<script>/* relay pane pre-module bootstrap")
        assert "relay pane pre-module bootstrap" in out
        # The classic bootstrap precedes the body it guards.
        assert out.index("(function(w,d){") < out.index("<body>")

    async def test_rewrite_bootstrap_is_injected_verbatim(self) -> None:
        """The injected bootstrap carries no ``src``/``href``, so the root-absolute
        re-rooting pass (and the module-inerting pass) must leave it byte-identical
        — including its own ``/instance-pane/`` capability-prefix regex, which is
        NOT a ``src`` and must never be prefixed."""
        from kiro_crew.dashboard.instance_pane_relay import (
            _RELAY_PANE_BOOTSTRAP_SCRIPT,
            _rewrite_entry_html,
        )

        out = _rewrite_entry_html(INDEX_HTML, "/instance-pane/c")
        # The whole bootstrap appears verbatim: the passes ran on the HTML before
        # injection, so nothing rewrote the script's body.
        assert _RELAY_PANE_BOOTSTRAP_SCRIPT in out
        # And the re-rooting never produced a double-prefixed capability path.
        assert "/instance-pane/c/instance-pane/" not in out

    async def test_javascript_body_is_never_rewritten(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}assets/index-abc.js")
            assert resp.status == 200
            # Byte-for-byte: the root-literal /api and src="/logo.png" inside the
            # bundle are untouched, and a script type carries no sandbox CSP.
            assert (await resp.text()) == JS_BODY
            assert "Content-Security-Policy" not in resp.headers


# ── CORS preflight for the opaque-origin pane ────────────────────────────────


class TestCorsPreflight:
    """A relay pane is a sandboxed, opaque-origin document (no allow-same-origin),
    so every request it makes back to the capability path is cross-origin
    (Origin: null). A non-simple request (the SPA sends X-Session-Key and JSON
    bodies) triggers a CORS preflight the relay must answer ITSELF — forwarding it
    to the peer, which does not speak CORS, would fail the preflight and block the
    real request (incident kc-46d84a: the pane's API calls never completed)."""

    async def test_preflight_is_answered_by_the_relay_not_forwarded(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.request(
                "OPTIONS",
                f"{base}api/status",
                headers={
                    "Origin": "null",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "x-session-key, content-type",
                },
            )
            assert resp.status == 204
            # Opaque-origin CORS grant, matching the response path's posture.
            assert resp.headers.get("Access-Control-Allow-Origin") == "null"
            assert "GET" in resp.headers.get("Access-Control-Allow-Methods", "")
            # The requested headers are reflected so the real request proceeds.
            assert "x-session-key" in resp.headers.get("Access-Control-Allow-Headers", "")
            # Deliberately NO credentials grant (the capability gates access).
            assert "Access-Control-Allow-Credentials" not in resp.headers
            # The preflight was answered locally — the peer was never consulted.
            assert h.hits == []

    async def test_preflight_on_a_bad_capability_is_the_uniform_404(self) -> None:
        async with _harness() as h:
            resp = await h.client.request(
                "OPTIONS",
                "/instance-pane/BOGUScapability/api/status",
                headers={"Origin": "null", "Access-Control-Request-Method": "GET"},
            )
            # Capability gate runs first: an unknown capability preflight is the
            # same uniform 404 every other bad-capability request gets.
            assert resp.status == 404
            assert h.hits == []

    async def test_non_preflight_options_still_forwards(self) -> None:
        """A bare OPTIONS without the preflight marker is a real method the holder
        may use; it is forwarded like any other allowed method (not short-circuited)."""
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.request("OPTIONS", f"{base}echo")
            assert resp.status == 200
            assert h.hits and h.hits[-1]["method"] == "OPTIONS"


# ── redirect rules ────────────────────────────────────────────────────────────


class TestRedirects:
    async def test_same_peer_path_redirect_is_rerooted(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            prefix = base.rstrip("/")
            resp = await h.client.get(f"{base}redir?to=/somewhere?x=1", allow_redirects=False)
            assert resp.status == 302
            assert resp.headers["Location"] == f"{prefix}/somewhere?x=1"

    async def test_cross_authority_redirect_fails_closed(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(
                f"{base}redir?to=https://evil.example/x", allow_redirects=False
            )
            assert resp.status == 404

    async def test_protocol_relative_redirect_fails_closed(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}redir?to=//evil.example/x", allow_redirects=False)
            assert resp.status == 404


# ── streaming (SSE) and WebSocket ─────────────────────────────────────────────


class TestStreamingAndWebSocket:
    async def test_sse_streams_through_with_its_content_type(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}sse")
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "text/event-stream"
            body = await resp.text()
            assert "data: event-0" in body and "data: event-2" in body

    async def test_websocket_pumps_text_and_binary_both_ways(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            ws = await h.client.ws_connect(f"{base}ws")
            await ws.send_str("hello")
            assert (await ws.receive()).data == "echo:hello"
            await ws.send_bytes(b"abc")
            assert (await ws.receive()).data == b"abc!"
            await ws.close()

    async def test_websocket_on_a_stale_capability_is_404(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            h.peer.bump_generation()
            # The upgrade is refused at the capability gate with the uniform 404,
            # before any peer WebSocket is opened.
            resp = await h.client.get(
                f"{base}ws",
                headers={"Upgrade": "websocket", "Connection": "Upgrade"},
            )
            assert resp.status == 404


# ── generation binding: the lease snapshot is carried into the forward ────────


class TestGenerationBindingRace:
    """The lease resolves against generation A; a rebuild moves the forward to B
    BEFORE the manager selects the url/credential. The lease's snapshot is carried
    into the forward attempt, so the mismatch is refused there — neither an HTTP
    request nor a WebSocket handshake reaches the replacement peer. Without the
    binding, the old capability would ride generation B's forward.
    """

    async def test_http_refuses_a_generation_moved_after_resolve(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            # The lease is live at resolve (generation A). The rebuild lands inside
            # the forward attempt, exactly in the window between the relay's lease
            # resolution and the manager's url+credential selection.
            h.peer.bump_on_next_forward = True
            resp = await h.client.get(f"{base}echo")
            # Refused before any byte crossed: the manager's not-connected maps to
            # 503, and the replacement peer was never consulted.
            assert resp.status == 503
            assert h.hits == []

    async def test_websocket_refuses_a_generation_moved_after_resolve(self) -> None:
        async with _harness() as h:
            base = h.issue_path()
            h.peer.bump_on_next_forward = True
            ws = await h.client.ws_connect(f"{base}ws")
            msg = await ws.receive()
            # The relay closes the downstream socket rather than pumping to the
            # replacement peer; the peer WebSocket route was never reached.
            assert msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            )
            assert not any(hit["path_qs"].endswith("/ws") for hit in h.hits)
            with contextlib.suppress(Exception):
                await ws.close()


# ── entry document is bounded before buffering ───────────────────────────────


class TestEntryDocumentBound:
    """The one buffered response — the text/html entry document — is size-capped
    before it is held whole, and answers a fixed error past the cap."""

    async def test_entry_html_at_the_cap_is_served(self, monkeypatch) -> None:
        from kiro_crew.dashboard import instance_pane_relay as mod

        monkeypatch.setattr(mod, "_ENTRY_HTML_MAX_BYTES", 1024)
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}big-html?bytes=1024")
            assert resp.status == 200
            assert resp.headers.get("Content-Type", "").startswith("text/html")

    async def test_entry_html_over_the_cap_is_a_fixed_error(self, monkeypatch) -> None:
        from kiro_crew.dashboard import instance_pane_relay as mod

        monkeypatch.setattr(mod, "_ENTRY_HTML_MAX_BYTES", 1024)
        async with _harness() as h:
            base = h.issue_path()
            resp = await h.client.get(f"{base}big-html?bytes=1025")
            assert resp.status == 502
            assert (await resp.json())["code"] == "pane_document_too_large"
            # The over-cap body was refused, not buffered and forwarded.


# ── lease bounds ──────────────────────────────────────────────────────────────


class TestLeaseBounds:
    async def test_per_instance_cap_evicts_the_oldest(self) -> None:
        async with _harness(max_leases_per_instance=2) as h:
            first = h.relay.issue(INSTANCE_ID)
            assert first is not None
            h.relay.issue(INSTANCE_ID)
            h.relay.issue(INSTANCE_ID)  # third issue evicts the first
            resp = await h.client.get(f"{first.document_path}echo")
            assert resp.status == 404

    async def test_issue_returns_none_when_not_connected(self) -> None:
        async with _harness() as h:
            h.peer.go_down()
            assert h.relay.issue(INSTANCE_ID) is None

    async def test_issue_is_retry_safe_distinct_capabilities(self) -> None:
        async with _harness() as h:
            a = h.relay.issue(INSTANCE_ID)
            b = h.relay.issue(INSTANCE_ID)
            assert a is not None and b is not None
            assert a.document_path != b.document_path
            assert a.channel != b.channel


# ── the owner-authenticated issuer ───────────────────────────────────────────


def _enable_instances(tmp_path, monkeypatch, *, enabled=True) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"instances": {"enabled": enabled}}))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()


class _IssuerReq:
    """Owner-authenticated request double for the issuer handler.

    Same three reads ``_guard``'s owner predicate performs as the connect-route
    tests' double: ``.get('user')``, ``'app' in``, ``['app']``.
    """

    def __init__(self, state, *, body=None, query=None, user="owner", app_token=""):
        self.app = {"state": state}
        self.headers: dict[str, str] = {}
        self.match_info = {"id": INSTANCE_ID}
        self.query = query or {}
        self._body = body
        self._attrs = {"app": app_token}
        if user is not None:
            self._attrs["user"] = user

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _IssuerState:
    owner_id = "owner"

    def __init__(self, manager, relay=None):
        self.instances_registry = None
        self.instances_manager = manager
        self.instance_pane_relay = relay


class _IssuerMgr:
    """Manager double for the issuer: a connected tunnel, a confirmable token,
    and a controllable advertised pane protocol."""

    def __init__(self, *, protocol=PANE_RELAY_PROTOCOL, connected=True, turn_url=""):
        self._protocol = protocol
        self._connected = connected
        self._turn_url = turn_url

    async def connect(self, iid, *, rebuild=False, only_if_connected=False):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        state = TunnelState.CONNECTED if self._connected else TunnelState.DISCONNECTED
        return TunnelStatus(iid, state, local_port=7778, remote_port=7777, turn_url=self._turn_url)

    def get_token(self, iid):
        return "SECRET_TOK"

    async def token_validates(self, local_port, token):
        return True

    async def refresh_token(self, iid):
        return "FRESH_TOK"

    async def peer_capability(self, iid, path):
        assert path == "/api/status"
        if self._protocol is None:
            return False, {"code": "capability_peer_too_old"}
        return True, {"pane_relay_protocol": self._protocol}


class _IssuerRelay:
    """Relay double for the issuer: records issue() and returns a fixed grant."""

    def __init__(self):
        self.issued_for = []

    def issue(self, instance_id):
        from kiro_crew.dashboard.instance_pane_relay import PaneGrant

        self.issued_for.append(instance_id)
        return PaneGrant(
            kind="same-origin-relay",
            document_path="/instance-pane/CAP/",
            channel="chan",
            protocol=PANE_RELAY_PROTOCOL,
            lease_expires_at_epoch_ms=123,
        )


def _body(resp):
    return json.loads(resp.body.decode())


class TestIssuerOwnerBoundary:
    async def test_feature_disabled_is_denied(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch, enabled=False)
        state = _IssuerState(_IssuerMgr())
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "same-origin-relay"})
        )
        assert resp.status == 403

    async def test_non_owner_is_denied(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        state = _IssuerState(_IssuerMgr(), _IssuerRelay())
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "same-origin-relay"}, user="not-the-owner")
        )
        assert resp.status in (401, 403)

    async def test_bad_access_mode_is_400(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        state = _IssuerState(_IssuerMgr(), _IssuerRelay())
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "nonsense"})
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "bad_request"

    async def test_unsupported_protocol_fails_closed_before_a_lease(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        relay = _IssuerRelay()
        state = _IssuerState(_IssuerMgr(protocol=2), relay)  # remote too new/old
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "same-origin-relay"})
        )
        assert resp.status == 409
        assert _body(resp)["code"] == "remote_upgrade_required"
        assert relay.issued_for == []  # no lease was ever created

    async def test_unknown_protocol_also_fails_closed(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        relay = _IssuerRelay()
        state = _IssuerState(_IssuerMgr(protocol=None), relay)  # peer too old to advertise
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "same-origin-relay"})
        )
        assert resp.status == 409
        assert relay.issued_for == []

    async def test_relay_success_returns_the_grant(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        relay = _IssuerRelay()
        state = _IssuerState(_IssuerMgr(), relay)
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "same-origin-relay"})
        )
        assert resp.status == 200
        body = _body(resp)
        assert body["kind"] == "same-origin-relay"
        assert body["documentPath"] == "/instance-pane/CAP/"
        assert body["protocol"] == PANE_RELAY_PROTOCOL
        assert relay.issued_for == [INSTANCE_ID]
        # A relay endpoint carries no port and no remote token.
        assert "token" not in body and "local_port" not in body

    async def test_direct_loopback_returns_port_and_token(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        state = _IssuerState(_IssuerMgr(), _IssuerRelay())
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "direct-loopback"})
        )
        assert resp.status == 200
        body = _body(resp)
        assert body["kind"] == "direct-loopback"
        assert body["local_port"] == 7778
        assert body["token"] == "SECRET_TOK"
        # A direct endpoint carries no relay capability, and never a turn_url —
        # the producer only ever emits the single token-bearing shape the
        # consumer (parsePaneEndpoint) accepts.
        assert "documentPath" not in body
        assert "turn_url" not in body

    async def test_fargate_crew_is_refused_a_direct_pane_not_handed_a_turn_url(
        self, tmp_path, monkeypatch
    ) -> None:
        # A fargate crew serves a turn API and NO dashboard, so its connected
        # status carries a turn_url and no token. The producer must REFUSE a pane
        # rather than emit a direct-loopback endpoint carrying turn_url (which the
        # consumer parsePaneEndpoint has no variant for) — the producer/consumer
        # mismatch this test pins closed.
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        state = _IssuerState(_IssuerMgr(turn_url="http://127.0.0.1:7778/turn"), _IssuerRelay())
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "direct-loopback"})
        )
        assert resp.status == 409
        body = _body(resp)
        assert body["code"] == "pane_not_supported_for_transport"
        # No token or turn_url leaked into the refusal body.
        assert "token" not in body and "turn_url" not in body

    async def test_fargate_crew_is_refused_a_relay_pane_before_any_lease(
        self, tmp_path, monkeypatch
    ) -> None:
        # Same refusal for the relay access mode, and BEFORE a lease is minted:
        # a turn-only crew has no dashboard to relay in either mode.
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        relay = _IssuerRelay()
        state = _IssuerState(_IssuerMgr(turn_url="http://127.0.0.1:7778/turn"), relay)
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(state, body={"access": "same-origin-relay"})
        )
        assert resp.status == 409
        assert _body(resp)["code"] == "pane_not_supported_for_transport"
        assert relay.issued_for == []  # refused before the lease

    async def test_connected_only_issue_declines_without_a_lease_when_down(
        self, tmp_path, monkeypatch
    ) -> None:
        # The background issue mode (auto-warm / renewal): a forward that is not up
        # is DECLINED (200, instance_not_connected) with no lease, no endpoint, and
        # no token — never brought up. The manager double reports disconnected.
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        relay = _IssuerRelay()
        state = _IssuerState(_IssuerMgr(connected=False), relay)
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(
                state, body={"access": "same-origin-relay"}, query={"only_if_connected": "1"}
            )
        )
        assert resp.status == 200
        body = _body(resp)
        assert body["code"] == "instance_not_connected"
        assert relay.issued_for == []  # no lease minted
        assert "token" not in body and "documentPath" not in body

    async def test_connected_only_issue_returns_a_grant_without_a_token_when_up(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        relay = _IssuerRelay()
        state = _IssuerState(_IssuerMgr(connected=True), relay)
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(
                state, body={"access": "same-origin-relay"}, query={"only_if_connected": "1"}
            )
        )
        assert resp.status == 200
        body = _body(resp)
        assert body["kind"] == "same-origin-relay"
        assert relay.issued_for == [INSTANCE_ID]
        assert "token" not in body and "local_port" not in body

    async def test_rebuild_and_only_if_connected_are_mutually_exclusive(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable_instances(tmp_path, monkeypatch)
        relay = _IssuerRelay()
        state = _IssuerState(_IssuerMgr(connected=True), relay)
        resp = await handlers.api_instances_open_pane(
            _IssuerReq(
                state,
                body={"access": "same-origin-relay"},
                query={"rebuild": "1", "only_if_connected": "1"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "bad_request"
        assert relay.issued_for == []


# ── access-log capability redaction ──────────────────────────────────────────


class TestAccessLogRedaction:
    """The capability is a bearer secret in the request PATH. aiohttp's access
    log (default ``%r`` atom) is a separate sink from the relay's SEL audit, so a
    route-aware redactor at the server boundary masks it there too — otherwise a
    log reader could replay a live capability."""

    async def test_redacts_a_capability_in_a_request_line(self) -> None:
        from kiro_crew.dashboard.instance_pane_relay import redact_capability_in_log

        line = '127.0.0.1 "GET /instance-pane/AbC-9f3a_XyZ/api/status?x=1 HTTP/1.1" 200 12'
        out = redact_capability_in_log(line)
        assert "AbC-9f3a_XyZ" not in out
        assert "/instance-pane/<redacted>/api/status?x=1" in out
        # The rest of the line (method, status, timing, query) is intact.
        assert out.endswith('HTTP/1.1" 200 12')

    async def test_redacts_at_prefix_root_and_leaves_non_relay_paths_untouched(self) -> None:
        from kiro_crew.dashboard.instance_pane_relay import redact_capability_in_log

        assert redact_capability_in_log("GET /instance-pane/CAP HTTP/1.1").count("<redacted>") == 1
        # A path that is not the relay prefix is never touched.
        untouched = "GET /api/instances/cd-1/status HTTP/1.1"
        assert redact_capability_in_log(untouched) == untouched

    async def test_filter_mutates_the_record_and_always_passes(self) -> None:
        import logging

        from kiro_crew.dashboard.instance_pane_relay import CapabilityRedactingLogFilter

        f = CapabilityRedactingLogFilter()
        rec = logging.LogRecord(
            "aiohttp.access",
            logging.INFO,
            __file__,
            1,
            '"GET /instance-pane/SECRETCAP/x HTTP/1.1" 200',
            None,
            None,
        )
        assert f.filter(rec) is True
        assert "SECRETCAP" not in rec.getMessage()
        assert "<redacted>" in rec.getMessage()

    async def test_install_is_idempotent(self) -> None:
        import logging

        from kiro_crew.dashboard.instance_pane_relay import (
            CapabilityRedactingLogFilter,
            install_access_log_redaction,
        )

        probe = logging.getLogger("test.pane.access.redaction")
        probe.filters = []
        install_access_log_redaction(probe)
        install_access_log_redaction(probe)
        installed = [f for f in probe.filters if isinstance(f, CapabilityRedactingLogFilter)]
        assert len(installed) == 1
        # And it actually redacts a record emitted through this logger.
        red = installed[0]
        rec = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "/instance-pane/LIVECAP/ hit", None, None
        )
        red.filter(rec)
        assert "LIVECAP" not in rec.getMessage()
