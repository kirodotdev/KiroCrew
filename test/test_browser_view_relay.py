"""The same-origin browser-view relay (`/browser-view/{token}/{tail}`).

Each test spins a real loopback stub upstream (standing in for
``playwright-cli show``) and drives the relay handler through an aiohttp
``TestClient``, so what is asserted is the wire behaviour the Browser panel
sees: the redirect/HTML/CSS rewrites that make a root-absolute SPA work under
a path prefix (carrying the capability token with them), byte-exact
passthrough for everything else, WebSocket pumping, the CSP ``sandbox`` stamp
on every relayed response except script types, and the fail-closed answers
(uniform 404 without the token, 502 only for a token-bearing caller whose
upstream died).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, AsyncIterator, Callable

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.browser_cli import view as browser_cli_view
from kiro_crew.dashboard.handlers import browser_view_relay

pytestmark = pytest.mark.asyncio


# ── Stub upstream: the shapes the real `playwright-cli show` serves ──────────

#: A stand-in capability token in the ``token_urlsafe`` alphabet.
TOKEN = "capAbc123xyz_-0"

INDEX_HTML = (
    "<!DOCTYPE html><html><head>"
    '<link rel="icon" href="/playwright-logo.svg" type="image/svg+xml">'
    '<script type="module" crossorigin src="/assets/index-abc.js"></script>'
    '<link rel="stylesheet" crossorigin href="/assets/index-def.css">'
    "</head><body></body></html>"
)
CSS_BODY = "@font-face{src:url(/assets/codicon-xyz.ttf)}"
BINARY_BODY = bytes(range(256)) * 4


def _stub_upstream(hits: list[str]) -> web.Application:
    app = web.Application()

    @web.middleware
    async def record(request: web.Request, handler: Any) -> web.StreamResponse:
        hits.append(request.path)
        return await handler(request)

    app.middlewares.append(record)

    async def root(_request: web.Request) -> web.Response:
        raise web.HTTPFound("/index.html?ws=tok123")

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def css(_request: web.Request) -> web.Response:
        return web.Response(text=CSS_BODY, content_type="text/css")

    async def binary(_request: web.Request) -> web.Response:
        return web.Response(body=BINARY_BODY, content_type="application/octet-stream")

    async def svg(_request: web.Request) -> web.Response:
        # A squatted upstream's payoff shape: a scriptable non-HTML document.
        return web.Response(
            text='<svg xmlns="http://www.w3.org/2000/svg"><script>fetch("/api")</script></svg>',
            content_type="image/svg+xml",
        )

    async def xhtml(_request: web.Request) -> web.Response:
        return web.Response(
            text='<html xmlns="http://www.w3.org/1999/xhtml"><script>1</script></html>',
            content_type="application/xhtml+xml",
        )

    async def script(_request: web.Request) -> web.Response:
        return web.Response(text="export {};", content_type="text/javascript")

    async def ranged(_request: web.Request) -> web.Response:
        # A media-shaped partial answer: the response headers a browser needs
        # to make sense of a 206 (plus the validators the forwarded
        # conditionals would revalidate against).
        return web.Response(
            status=206,
            body=BINARY_BODY[:4],
            content_type="application/octet-stream",
            headers={
                "Content-Range": f"bytes 0-3/{len(BINARY_BODY)}",
                "Accept-Ranges": "bytes",
                "ETag": '"v1"',
                "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            },
        )

    async def tagged_html(_request: web.Request) -> web.Response:
        # An HTML document that CARRIES validators: the rewrite branch must
        # drop them, because the relayed body is not upstream's body.
        return web.Response(
            text=INDEX_HTML,
            content_type="text/html",
            headers={"ETag": '"h1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
        )

    async def sock(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(f"echo:{msg.data}")
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(bytes(reversed(msg.data)))
        return ws

    app.router.add_get("/", root)
    app.router.add_get("/index.html", index)
    app.router.add_get("/assets/index-def.css", css)
    app.router.add_get("/bin", binary)
    app.router.add_get("/img.svg", svg)
    app.router.add_get("/page.xhtml", xhtml)
    app.router.add_get("/assets/index-abc.js", script)
    app.router.add_get("/part", ranged)
    app.router.add_get("/tagged.html", tagged_html)
    app.router.add_get("/tok123", sock)
    return app


def _relay_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/browser-view", browser_view_relay.api_browser_view_relay)
    app.router.add_get("/browser-view/{tail:.*}", browser_view_relay.api_browser_view_relay)
    app.on_cleanup.append(browser_view_relay.close_relay_client)
    return app


@contextlib.asynccontextmanager
async def _running_stack(
    monkeypatch: pytest.MonkeyPatch, hits: list[str] | None = None
) -> AsyncIterator[TestClient]:
    """Stub upstream + relay TestClient, capability ``TOKEN`` installed.

    An ``async with`` helper rather than an ``@pytest_asyncio.fixture``, by this
    repo's convention: the pinned pytest-asyncio does not collect async
    generator fixtures, and both servers' teardown has to await.
    """
    recorded = hits if hits is not None else []
    server = TestServer(_stub_upstream(recorded), host="127.0.0.1")
    await server.start_server()
    port = server.port
    assert port is not None

    def _authorize(
        candidate: str, *, proof_not_before: float | None = None
    ) -> tuple[str, int | None]:
        # Mirrors view.relay_authorize's contract: compare first, port only on
        # a match. The real ordering (no probes for a bad token) is pinned by
        # the view-supervisor unit tests; the relay tests exercise the wire.
        if candidate == TOKEN:
            return "ok", port
        return "token_mismatch", None

    monkeypatch.setattr(browser_cli_view, "relay_authorize", _authorize)
    client = TestClient(TestServer(_relay_app(), host="127.0.0.1"))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        await server.close()


# ── Rewrites ─────────────────────────────────────────────────────────────────


async def test_redirect_is_rerooted_and_ws_param_prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}/", allow_redirects=False)
        assert resp.status == 302
        location = resp.headers["Location"]
        assert location.startswith(f"/browser-view/{TOKEN}/index.html?")
        # '/' + <ws param> is how the SPA builds its WebSocket URL, so the value
        # itself carries prefix AND token — that is what authenticates the
        # socket's follow-up request.
        assert (
            f"ws=browser-view%2F{TOKEN}%2Ftok123" in location
            or f"ws=browser-view/{TOKEN}/tok123" in location
        )
        # The redirect is stamped like every other relayed non-script response:
        # CORS-mode asset fetches re-check each hop (a bare 302 would strand
        # Vite's ``crossorigin`` tags), and the module's posture claim covers
        # this branch too.
        assert "sandbox" in resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Access-Control-Allow-Origin"] == "*"


async def test_tokened_bare_prefix_proxies_upstream_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"].startswith(f"/browser-view/{TOKEN}/index.html?")


async def test_html_root_absolute_refs_are_rerooted(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}/index.html")
        assert resp.status == 200
        body = await resp.text()
        assert f'src="/browser-view/{TOKEN}/assets/index-abc.js"' in body
        assert f'href="/browser-view/{TOKEN}/assets/index-def.css"' in body
        assert f'href="/browser-view/{TOKEN}/playwright-logo.svg"' in body
        assert 'src="/assets/' not in body


async def test_html_documents_are_stamped_into_an_opaque_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The CSP `sandbox` stamp is what keeps the relayed SPA isolated from
    # dashboard state even when opened as a full tab (no iframe sandbox attr).
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}/index.html")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert csp.startswith("sandbox")
        assert "allow-scripts" in csp
        assert "allow-same-origin" not in csp
        # Popup parity with the direct-path iframe: allowed, but never the
        # escape variant — a popup must inherit the sandbox (opaque origin
        # included), or the stamp's isolation would leak through new windows.
        assert "allow-popups" in csp
        assert "allow-popups-to-escape-sandbox" not in csp
        # The opaque origin fetches its `crossorigin` assets in CORS mode
        # (`Origin: null`), so every relayed response is CORS-open — the
        # capability token gates access, CORS only gates readability.
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        # And it cannot touch real window storage (bare reads throw), so the
        # document carries the in-memory shim, armed before the SPA's scripts.
        body = await resp.text()
        assert body.count("void window.localStorage") == 1
        assert body.index("void window.localStorage") < body.index("index-abc.js")

        asset = await relay_client.get(f"/browser-view/{TOKEN}/bin")
        # Non-documents carry the stamp too (inert on a subresource, decisive
        # if navigated), and stay CORS-open for the same reason.
        assert asset.headers.get("Content-Security-Policy", "").startswith("sandbox")
        assert asset.headers.get("Access-Control-Allow-Origin") == "*"


async def test_every_relayed_type_is_stamped_except_scripts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The port-squat payoff: a squatted upstream answers with a scriptable
    # NON-HTML document (XHTML, SVG, the XML+XSLT class) and the owner opens
    # the relay URL as a full tab. The stamp must cover every such type — an
    # unstamped one would execute on the dashboard's real origin. Streamed
    # types also carry nosniff so an unknown type cannot be sniffed into a
    # document either.
    async with _running_stack(monkeypatch) as relay_client:
        for path in ("img.svg", "page.xhtml", "bin"):
            resp = await relay_client.get(f"/browser-view/{TOKEN}/{path}")
            csp = resp.headers.get("Content-Security-Policy", "")
            assert csp.startswith("sandbox"), path
            assert "allow-same-origin" not in csp, path
            assert resp.headers.get("X-Content-Type-Options") == "nosniff", path

        # Script types are the one exemption: a worker created from a relayed
        # script URL is governed by the CSP on the SCRIPT response, so
        # stamping it would sandbox the view SPA's own workers — and a script
        # type never renders as a document on navigation.
        js = await relay_client.get(f"/browser-view/{TOKEN}/assets/index-abc.js")
        assert not js.headers.get("Content-Security-Policy", "").startswith("sandbox")
        assert js.headers.get("X-Content-Type-Options") == "nosniff"
        assert js.headers.get("Access-Control-Allow-Origin") == "*"


async def test_css_url_refs_are_rerooted(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}/assets/index-def.css")
        assert resp.status == 200
        body = await resp.text()
        assert f"url(/browser-view/{TOKEN}/assets/codicon-xyz.ttf)" in body


async def test_binary_passthrough_is_byte_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}/bin")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/octet-stream"
        assert await resp.read() == BINARY_BODY


async def test_stream_branch_carries_range_and_validator_headers_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The header-allowlist symmetry finding: Range / If-None-Match /
    # If-Modified-Since are forwarded upstream, so their response halves must
    # come back — a 206 without Content-Range is a protocol violation the
    # browser discards, and validators that never reach the browser make the
    # forwarded conditionals dead weight. Streamed bodies pass through
    # byte-identical, so upstream's headers describe exactly what we send.
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}/part")
        assert resp.status == 206
        assert resp.headers["Content-Range"] == f"bytes 0-3/{len(BINARY_BODY)}"
        assert resp.headers["Accept-Ranges"] == "bytes"
        assert resp.headers["ETag"] == '"v1"'
        assert resp.headers["Last-Modified"] == "Wed, 01 Jan 2025 00:00:00 GMT"
        assert await resp.read() == BINARY_BODY[:4]


async def test_rewritten_documents_never_carry_upstream_validators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half of the symmetry decision: the rewrite branch TRANSFORMS
    # the body, so upstream's validators describe bytes the relay did not
    # send — emitting them would let a browser revalidate the wrong content.
    # With no validator ever emitted, the browser never sends a conditional
    # for these documents, keeping the forwarded conditionals inert there.
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}/tagged.html")
        assert resp.status == 200
        body = await resp.text()
        assert f'src="/browser-view/{TOKEN}/assets' in body  # rewrite really ran
        for name in ("ETag", "Last-Modified", "Content-Range", "Accept-Ranges"):
            assert name not in resp.headers


async def test_off_origin_redirect_fails_closed() -> None:
    # The rewrite refuses a Location carrying a scheme/host: the upstream only
    # ever redirects within itself, so anything else degrades to the relay root.
    prefix = f"/browser-view/{TOKEN}"
    assert browser_view_relay._rewrite_location("https://evil.example/x", prefix) == f"{prefix}/"
    assert browser_view_relay._rewrite_location("//evil.example/x", prefix) == f"{prefix}/"


# ── WebSocket pumping ────────────────────────────────────────────────────────


async def test_websocket_text_and_binary_pump_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        _running_stack(monkeypatch) as relay_client,
        relay_client.ws_connect(f"/browser-view/{TOKEN}/tok123") as ws,
    ):
        await ws.send_str("hello")
        msg = await ws.receive(timeout=5)
        assert msg.type == aiohttp.WSMsgType.TEXT
        assert msg.data == "echo:hello"

        await ws.send_bytes(b"\x01\x02\x03")
        msg = await ws.receive(timeout=5)
        assert msg.type == aiohttp.WSMsgType.BINARY
        assert msg.data == b"\x03\x02\x01"


# ── Fail-closed answers ──────────────────────────────────────────────────────


async def test_wrong_token_answers_404_without_touching_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits: list[str] = []
    async with _running_stack(monkeypatch, hits) as relay_client:
        for path in (
            "/browser-view/WRONGTOKEN/index.html",  # wrong capability
            "/browser-view/",  # empty token segment
            "/browser-view",  # no token segment at all
        ):
            resp = await relay_client.get(path, allow_redirects=False)
            assert resp.status == 404, path
        assert hits == []  # the gate answered before any upstream contact


async def test_view_not_running_answers_the_same_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # Indistinguishable from a wrong token: an unauthenticated probe must not
    # learn whether a logged-in browser is up.
    monkeypatch.setattr(browser_cli_view, "relay_authorize", lambda candidate: ("view_down", None))
    client = TestClient(TestServer(_relay_app(), host="127.0.0.1"))
    await client.start_server()
    try:
        resp = await client.get(f"/browser-view/{TOKEN}/")
        assert resp.status == 404
        payload: dict[str, Any] = await resp.json()
        assert payload["code"] == "not_found"
    finally:
        await client.close()


async def test_supervisor_busy_answers_retryable_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # A start window holds the supervisor lock and the gate refuses as "busy"
    # rather than parking the thread pool. Busy is reachable ONLY by a caller
    # whose candidate matched the live token (the lock-free pre-check refuses
    # everyone else without touching the lock), so answering it as a
    # retryable 503 leaks nothing to a prober — and it must NOT be a 404: a
    # 404 on a validly-tokened stylesheet is permanent to the browser,
    # turning a transient lock hold into a broken document.
    recorder = _AuditRecorder()
    monkeypatch.setattr(browser_view_relay, "sel", lambda: recorder)
    monkeypatch.setattr(browser_cli_view, "relay_authorize", lambda candidate: ("busy", None))
    client = TestClient(TestServer(_relay_app(), host="127.0.0.1"))
    await client.start_server()
    try:
        resp = await client.get(f"/browser-view/{TOKEN}/")
        assert resp.status == 503
        assert resp.headers.get("Retry-After") == "2"
        payload: dict[str, Any] = await resp.json()
        assert payload["code"] == "busy"
    finally:
        await client.close()
    assert [(r["outcome"], r["resources"]) for r in recorder.records] == [("denied", "busy")]


async def test_dead_upstream_answers_502(monkeypatch: pytest.MonkeyPatch) -> None:
    # A port nothing listens on: bind-and-release to find one that is free.
    # The caller HOLDS the valid token, so the run-state disclosure is fine.
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    monkeypatch.setattr(browser_cli_view, "relay_authorize", lambda candidate: ("ok", dead_port))
    client = TestClient(TestServer(_relay_app(), host="127.0.0.1"))
    await client.start_server()
    try:
        resp = await client.get(f"/browser-view/{TOKEN}/")
        assert resp.status == 502
        payload: dict[str, Any] = await resp.json()
        assert payload["code"] == "browser_view_unreachable"
    finally:
        await client.close()


def test_relay_prefix_bypasses_cookie_auth_by_design() -> None:
    # The panel frames the relay in an opaque-origin sandbox that sends no
    # cookies, so the capability token is the gate — the prefix must therefore
    # sit on token_auth's bypass list (like /artifact-app/ and /sandbox-doc/).
    from kiro_crew.dashboard import token_auth

    assert "/browser-view/" in token_auth._BYPASS_PREFIXES
    # The bare path (no trailing slash) is a registered relay route too, and
    # the prefix entry misses it: without its own exact bypass the middleware
    # answers 403 there, breaking the relay's uniform-404 contract.
    assert "/browser-view" in token_auth._BYPASS_EXACT


# ── Status payload carries the relay path ────────────────────────────────────


async def test_status_payload_gains_tokened_relay_path_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.handlers import messaging

    monkeypatch.setattr(
        messaging.browser_cli_view,
        "status",
        lambda: {"status": "running", "url": "http://127.0.0.1:1", "port": 1, "reason": None},
    )
    monkeypatch.setattr(messaging.browser_cli_view, "relay_target", lambda: (1, TOKEN))
    assert messaging._browser_view_payload()["path"] == f"/browser-view/{TOKEN}/"

    monkeypatch.setattr(
        messaging.browser_cli_view,
        "status",
        lambda: {"status": "stopped", "url": None, "port": None, "reason": None},
    )
    assert messaging._browser_view_payload()["path"] is None


async def test_status_payload_never_publishes_target_the_proof_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # status() said "running", but by the time relay_target() re-proves
    # ownership the child is gone (or the proof is inconclusive) and it
    # returns None. Publishing the recorded direct url anyway would hand the
    # frontend's fallback a frameable target the proof just refused to vouch
    # for — whatever wins the freed loopback port inherits the frame. The
    # whole payload must degrade to stopped instead; the panel's next poll
    # re-reports a live view.
    from kiro_crew.dashboard.handlers import messaging

    monkeypatch.setattr(
        messaging.browser_cli_view,
        "status",
        lambda: {"status": "running", "url": "http://127.0.0.1:1", "port": 1, "reason": None},
    )
    monkeypatch.setattr(messaging.browser_cli_view, "relay_target", lambda: None)
    payload = messaging._browser_view_payload()
    assert payload["status"] == "stopped"
    assert payload["url"] is None
    assert payload["port"] is None
    assert payload["path"] is None


# ── Post-connect ownership re-proof (proof→connect race) ────────────────────


def _sequenced_authorize(
    monkeypatch: pytest.MonkeyPatch,
    second: Callable[[tuple[str, int | None]], tuple[str, int | None]],
) -> list[str]:
    """Wrap the stack's ``relay_authorize`` stub: first call passes through,
    the second (the post-connect re-proof) is rewritten by ``second``.

    Returns the call log. Must run AFTER ``_running_stack`` has installed its
    stub, so the pre-connect call still resolves the real upstream port.
    """
    inner = browser_cli_view.relay_authorize
    calls: list[str] = []

    def _authorize(
        candidate: str, *, proof_not_before: float | None = None
    ) -> tuple[str, int | None]:
        calls.append(candidate)
        result = inner(candidate, proof_not_before=proof_not_before)
        if len(calls) >= 2:
            return second(result)
        return result

    monkeypatch.setattr(browser_cli_view, "relay_authorize", _authorize)
    return calls


async def test_happy_path_runs_exactly_two_proofs(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cost contract: one pre-connect authorization plus ONE post-connect
    # re-proof per established connection — never per chunk.
    async with _running_stack(monkeypatch) as relay_client:
        calls = _sequenced_authorize(monkeypatch, lambda result: result)
        resp = await relay_client.get(f"/browser-view/{TOKEN}/bin", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
        assert len(calls) == 2


async def test_post_connect_reproof_carries_a_monotonic_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prover caches verdicts; the fence is what keeps the re-proof a
    # RE-proof. The pre-connect authorization passes no fence (a TTL-fresh
    # shared verdict is its exact semantics); the post-connect call must
    # demand a verdict recorded after the connect, so a pre-connect verdict
    # can never be replayed to vouch for the established connection.
    async with _running_stack(monkeypatch) as relay_client:
        fences: list[float | None] = []
        inner = browser_cli_view.relay_authorize

        def _spy(
            candidate: str, *, proof_not_before: float | None = None
        ) -> tuple[str, int | None]:
            fences.append(proof_not_before)
            return inner(candidate, proof_not_before=proof_not_before)

        monkeypatch.setattr(browser_cli_view, "relay_authorize", _spy)
        before = time.monotonic()
        resp = await relay_client.get(f"/browser-view/{TOKEN}/bin", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
        assert len(fences) == 2
        assert fences[0] is None
        assert fences[1] is not None and fences[1] >= before


async def test_reproof_failure_after_connect_answers_uniform_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The child died between the pre-connect proof and the connect: the
    # established connection may belong to a squatter on the freed port.
    # Nothing may be streamed downstream, and the wire answer is the same
    # uniform 404 as every pre-connect denial.
    async with _running_stack(monkeypatch) as relay_client:
        calls = _sequenced_authorize(monkeypatch, lambda result: ("ownership_unproven", None))
        resp = await relay_client.get(f"/browser-view/{TOKEN}/index.html", allow_redirects=False)
        assert resp.status == 404
        payload: dict[str, Any] = await resp.json()
        assert payload["code"] == "not_found"
        assert len(calls) == 2


async def test_reproof_port_move_is_refused_as_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    # The view RESTARTED after we connected: relay_authorize says "ok" for the
    # new instance, but our held connection went to the freed OLD port. Same
    # ok-different-port must be refused, not served.
    async with _running_stack(monkeypatch) as relay_client:
        _sequenced_authorize(monkeypatch, lambda result: ("ok", (result[1] or 0) + 1))
        resp = await relay_client.get(f"/browser-view/{TOKEN}/index.html", allow_redirects=False)
        assert resp.status == 404


async def test_reproof_busy_answers_retryable_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # A start grabs the supervisor lock between our pre-connect authorization
    # and the post-connect re-proof. The caller demonstrably holds the
    # current token (it passed the pre-connect gate), so the refusal is the
    # retryable 503 — matching the pre-connect gate — not the permanent 404
    # that would strand a validly-tokened asset.
    async with _running_stack(monkeypatch) as relay_client:
        _sequenced_authorize(monkeypatch, lambda result: ("busy", None))
        resp = await relay_client.get(f"/browser-view/{TOKEN}/index.html", allow_redirects=False)
        assert resp.status == 503
        assert resp.headers.get("Retry-After") == "2"
        payload: dict[str, Any] = await resp.json()
        assert payload["code"] == "busy"


async def test_redirect_branch_is_also_gated_by_the_reproof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The re-proof gates every branch, including redirects — a squatter's 302
    # steers the panel's next fetch and must not escape the gate.
    async with _running_stack(monkeypatch) as relay_client:
        _sequenced_authorize(monkeypatch, lambda result: ("view_down", None))
        resp = await relay_client.get(f"/browser-view/{TOKEN}/", allow_redirects=False)
        assert resp.status == 404
        assert "Location" not in resp.headers


async def test_websocket_closes_before_any_frame_when_reproof_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The WS carries live operator input: a failed re-proof must close the
    # bridge before the first frame crosses in either direction.
    async with _running_stack(monkeypatch) as relay_client:
        _sequenced_authorize(monkeypatch, lambda result: ("ownership_unproven", None))
        async with relay_client.ws_connect(f"/browser-view/{TOKEN}/tok123") as ws:
            await ws.send_str("hello")
            msg = await ws.receive(timeout=5)
            assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
            assert ws.close_code == aiohttp.WSCloseCode.TRY_AGAIN_LATER


async def test_stale_upstream_denial_is_audited_with_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Post-connect denials are audited like every other decision: fixed
    # reason, never the token or the path.
    recorder = _AuditRecorder()
    monkeypatch.setattr(browser_view_relay, "sel", lambda: recorder)
    async with _running_stack(monkeypatch) as relay_client:
        _sequenced_authorize(monkeypatch, lambda result: ("ownership_unproven", None))
        await relay_client.get(f"/browser-view/{TOKEN}/index.html", allow_redirects=False)
    outcomes = [(r["outcome"], r["resources"]) for r in recorder.records]
    assert ("denied", "stale_upstream:ownership_unproven") in outcomes
    for record in recorder.records:
        for value in record.values():
            assert TOKEN not in str(value)


# ── SEL auditing of the token decision ───────────────────────────────────────


class _AuditRecorder:
    """Stands in for ``sel()``, capturing ``log_api_access`` kwargs."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


async def test_both_token_outcomes_are_audited_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The finding: relay request -> token allow/deny decision -> SEL record.
    # Both outcomes must be audited, and no record field may carry the token —
    # or the request path/query, which embed the relay and socket tokens.
    recorder = _AuditRecorder()
    monkeypatch.setattr(browser_view_relay, "sel", lambda: recorder)
    async with _running_stack(monkeypatch) as relay_client:
        await relay_client.get("/browser-view/WRONGTOKEN/index.html", allow_redirects=False)
        await relay_client.get("/browser-view", allow_redirects=False)
        await relay_client.get(f"/browser-view/{TOKEN}/index.html", allow_redirects=False)

    outcomes = [(r["outcome"], r["resources"]) for r in recorder.records]
    assert ("denied", "token_mismatch") in outcomes
    assert ("denied", "no_token") in outcomes
    allowed = [r for r in recorder.records if r["outcome"] == "allowed"]
    assert allowed and allowed[0]["resources"].startswith("port:")
    for record in recorder.records:
        assert record["caller"] == "dashboard:browser-view-relay"
        assert record["operation"] == "browser_view_relay.serve"
        for value in record.values():
            assert TOKEN not in str(value)
            assert "WRONGTOKEN" not in str(value)
            assert "index.html" not in str(value)


async def test_view_down_denial_is_audited_with_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The wire answer is the same uniform 404 as a wrong token; the AUDIT may
    # distinguish (SEL is not readable by the unauthenticated caller).
    recorder = _AuditRecorder()
    monkeypatch.setattr(browser_view_relay, "sel", lambda: recorder)
    monkeypatch.setattr(browser_cli_view, "relay_authorize", lambda candidate: ("view_down", None))
    client = TestClient(TestServer(_relay_app(), host="127.0.0.1"))
    await client.start_server()
    try:
        resp = await client.get(f"/browser-view/{TOKEN}/")
        assert resp.status == 404
    finally:
        await client.close()
    assert [(r["outcome"], r["resources"]) for r in recorder.records] == [("denied", "view_down")]


async def test_no_token_request_never_reaches_the_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pre-auth cost finding, wire side: a request with no token segment is
    # refused before the view supervisor is consulted at all — its lock and
    # ownership probes are never a cost an unauthenticated caller can impose.
    def _must_not_be_called(candidate: str) -> tuple[str, int | None]:
        raise AssertionError("supervisor consulted for a tokenless request")

    recorder = _AuditRecorder()
    monkeypatch.setattr(browser_view_relay, "sel", lambda: recorder)
    monkeypatch.setattr(browser_cli_view, "relay_authorize", _must_not_be_called)
    client = TestClient(TestServer(_relay_app(), host="127.0.0.1"))
    await client.start_server()
    try:
        for path in ("/browser-view", "/browser-view/"):
            resp = await client.get(path, allow_redirects=False)
            assert resp.status == 404, path
    finally:
        await client.close()
    assert [(r["outcome"], r["resources"]) for r in recorder.records] == [
        ("denied", "no_token"),
        ("denied", "no_token"),
    ]


async def test_ownership_unproven_is_audited_with_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A matched token whose instance fails (or cannot complete) the ownership
    # proof: same uniform 404 on the wire, its own reason in the audit trail.
    recorder = _AuditRecorder()
    monkeypatch.setattr(browser_view_relay, "sel", lambda: recorder)
    monkeypatch.setattr(
        browser_cli_view, "relay_authorize", lambda candidate: ("ownership_unproven", None)
    )
    client = TestClient(TestServer(_relay_app(), host="127.0.0.1"))
    await client.start_server()
    try:
        resp = await client.get(f"/browser-view/{TOKEN}/")
        assert resp.status == 404
        payload: dict[str, Any] = await resp.json()
        assert payload["code"] == "not_found"
    finally:
        await client.close()
    assert [(r["outcome"], r["resources"]) for r in recorder.records] == [
        ("denied", "ownership_unproven")
    ]


async def test_audit_failure_never_breaks_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    # Best-effort audit, per the sibling capability handlers: a SEL failure is
    # logged and the relay keeps serving.
    def _boom() -> Any:
        raise RuntimeError("sel unavailable")

    monkeypatch.setattr(browser_view_relay, "sel", _boom)
    async with _running_stack(monkeypatch) as relay_client:
        resp = await relay_client.get(f"/browser-view/{TOKEN}/index.html", allow_redirects=False)
        assert resp.status == 200
