"""App web origins: ``<app>.apps.localhost`` on the dashboard's port.

The router serves an enabled, opted-in app's backend on its own site and refuses
every other name under the suffix before any dashboard layer sees it. These tests
drive the real middleware in front of a real loopback backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import app_web_origin as awo
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard.ssm_gate import GateRef, SsmGate
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

_PORT = "5476"
_ALLOWED = {
    f"http://localhost:{_PORT}",
    f"http://127.0.0.1:{_PORT}",
    f"http://[::1]:{_PORT}",
    f"http://kirocrew.localhost:{_PORT}",
    "https://crew.example.com",
}
_HOST = f"demo.apps.localhost:{_PORT}"


# ── pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_HOST, ("demo", _PORT)),
        (f"DEMO.Apps.LocalHost:{_PORT}", ("demo", _PORT)),
        (f"demo.apps.localhost.:{_PORT}", ("demo", _PORT)),
        (f"localhost:{_PORT}", None),
        (f"kirocrew.localhost:{_PORT}", None),
        (f"demo.localhost:{_PORT}", None),
        # A rebinding name that merely contains the suffix is not ours; the
        # dashboard's own Host barrier refuses it.
        (f"demo.apps.localhost.evil.example:{_PORT}", None),
        ("", None),
        ("demo.apps.localhost", ("demo", "80")),
        (f"a.b.apps.localhost:{_PORT}", False),
        (f"-x.apps.localhost:{_PORT}", False),
        (f"{'a' * 64}.apps.localhost:{_PORT}", False),
        (f".apps.localhost:{_PORT}", False),
        ("demo.apps.localhost:x", False),
    ],
)
def test_parse_app_host(raw: str, expected: object) -> None:
    assert awo.parse_app_host(raw) == expected


def test_dashboard_loopback_origins_are_the_dashboard_names_only() -> None:
    assert awo.dashboard_loopback_origins(_ALLOWED) == sorted(
        f"http://{host}:{_PORT}" for host in ("localhost", "127.0.0.1", "kirocrew.localhost")
    )


def test_dashboard_cookies_are_not_forwarded() -> None:
    raw = "mc_token_5476=dash; app=1; mc_refresh_5476=r; mc_token=legacy; other=a=b"
    assert awo.forwarded_cookie(raw) == "app=1; other=a=b"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a=1; Domain=apps.localhost; Path=/", "a=1; Path=/"),
        ("a=1; domain=.localhost; HttpOnly", "a=1; HttpOnly"),
        ("a=1; Path=/; Secure", "a=1; Path=/; Secure"),
    ],
)
def test_set_cookie_is_host_only(value: str, expected: str) -> None:
    assert awo.host_only_set_cookie(value) == expected


def test_response_hardening() -> None:
    headers = awo.hardened_response_headers(
        [
            ("Content-Security-Policy", "default-src 'self'; frame-ancestors *"),
            (
                "Content-Security-Policy",
                "frame-ancestors 'none', script-src 'none'; frame-ancestors *",
            ),
            ("Clear-Site-Data", '"*"'),
            ("Set-Cookie", "a=1; Domain=apps.localhost"),
            ("Set-Cookie", "b=2"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Type", "text/html"),
        ],
        "'self' http://localhost:5476",
    )
    assert headers.getall("Content-Security-Policy") == [
        "default-src 'self'",
        "script-src 'none'",
        "frame-ancestors 'self' http://localhost:5476",
    ]
    assert headers.getall("Set-Cookie") == ["a=1", "b=2"]
    assert "Clear-Site-Data" not in headers
    assert "Transfer-Encoding" not in headers
    assert headers["Content-Type"] == "text/html"


@pytest.mark.parametrize(
    ("backend", "name", "error"),
    [
        ({"entryPoint": "s.py", "webOrigin": True}, "demo", False),
        ({"entryPoint": "s.py", "webOrigin": True}, "a" * 64, True),
        ({"entryPoint": "s.py"}, "a" * 64, False),
    ],
)
def test_manifest_opt_in(backend: dict[str, Any], name: str, error: bool) -> None:
    manifest = AppManifest.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "displayName": "D",
            "description": "d",
            "backend": backend,
        }
    )
    assert manifest.backend.webOrigin is bool(backend.get("webOrigin"))
    assert ("backend.webOrigin" in " ".join(manifest.validate())) is error


@pytest.mark.parametrize("raw", ["true", 1, "yes", None])
def test_manifest_opt_in_is_the_json_boolean_only(raw: object) -> None:
    manifest = AppManifest.from_dict(
        {
            "name": "demo",
            "version": "1.0.0",
            "displayName": "D",
            "description": "d",
            "backend": {"entryPoint": "s.py", "webOrigin": raw},
        }
    )
    assert manifest.backend.webOrigin is False
    assert "webOrigin" not in manifest.to_dict().get("backend", {})


# ── the router in front of a real backend ────────────────────────────────────


class _Backend:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.ws_texts: list[str] = []
        self.ws_close_codes: list[int | None] = []
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def _capture(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse(protocols=("chat",))
            await ws.prepare(request)
            self.seen.append({"ws": True, "headers": dict(request.headers)})
            while True:
                msg = await ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self.ws_texts.append(msg.data)
                    if msg.data == "bye":
                        await ws.close(code=4001)
                        self.ws_close_codes.append(None)
                        break
                    await ws.send_str(f"echo:{msg.data}")
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(msg.data[::-1])
                else:
                    # The code in the close frame the relay sent, before this
                    # side's own closing handshake overwrites ws.close_code.
                    self.ws_close_codes.append(
                        msg.data if msg.type == aiohttp.WSMsgType.CLOSE else None
                    )
                    break
            return ws
        body = await request.read()
        self.seen.append(
            {
                "headers": dict(request.headers),
                "cookies": request.headers.get("Cookie", ""),
                "target": request.raw_path,
                "body": body,
                "method": request.method,
            }
        )
        resp = web.Response(text="framed page", content_type="text/html")
        resp.headers.add("Set-Cookie", "app=1; Domain=apps.localhost; Path=/")
        resp.headers["Content-Security-Policy"] = (
            "frame-ancestors 'none', script-src 'none'; frame-ancestors *"
        )
        resp.headers["Clear-Site-Data"] = '"*"'
        return resp

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._capture)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[_Backend]:
    b = _Backend()
    await b.start()
    try:
        yield b
    finally:
        await b.stop()


def _router_app(
    backend: _Backend, monkeypatch: pytest.MonkeyPatch, ssm_gate: GateRef | None
) -> web.Application:
    """The router outermost, then a stand-in for every dashboard layer."""

    async def _port(name: str) -> int | None:
        return backend.port if name == "demo" else None

    monkeypatch.setattr(awo, "_backend_port", _port)

    async def dashboard(_request: web.Request) -> web.Response:
        return web.Response(text="dashboard")

    app = web.Application(middlewares=[awo.build_app_web_origin_middleware(ssm_gate)])
    app["allowed_origins"] = set(_ALLOWED)
    app.router.add_route("*", "/{path:.*}", dashboard)
    return app


@pytest_asyncio.fixture
async def gateway(backend: _Backend, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    """The router on a gateway with no Session Manager listener."""
    client = TestClient(TestServer(_router_app(backend, monkeypatch, None), host="127.0.0.1"))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def ssm_gateway(
    backend: _Backend, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[TestClient, int]]:
    """The router with a Session Manager listener bound beside the dashboard's.

    As ``ssm_gate.start_after_bind`` binds it: a second site on the same runner, so
    a request on either port runs the same chain, and the gate holder the chain was
    built with names the second site's port. Yields the client and that port.
    """
    ref = GateRef()
    client = TestClient(TestServer(_router_app(backend, monkeypatch, ref), host="127.0.0.1"))
    await client.start_server()
    try:
        runner = client.server.runner
        assert runner is not None
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        ssm_port = next(addr[1] for addr in runner.addresses if addr[1] != client.port)
        ref.gate = SsmGate(port=ssm_port)
        yield client, ssm_port
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_serves_the_app_and_never_the_dashboard(
    gateway: TestClient, backend: _Backend
) -> None:
    for path in ("/", "/api/session", "/__kc_auth?t=x&next=/doc/1"):
        resp = await gateway.get(path, headers={"Host": _HOST})
        assert resp.status == 200
        assert await resp.text() == "framed page"
    assert [s["target"] for s in backend.seen] == [
        "/",
        "/api/session",
        "/__kc_auth?t=x&next=/doc/1",
    ]


@pytest.mark.asyncio
async def test_other_hosts_reach_the_dashboard(gateway: TestClient, backend: _Backend) -> None:
    resp = await gateway.get("/", headers={"Host": f"localhost:{_PORT}"})
    assert await resp.text() == "dashboard"
    assert backend.seen == []


@pytest.mark.parametrize(
    ("host", "extra", "status"),
    [
        (f"other.apps.localhost:{_PORT}", {}, 404),
        ("demo.apps.localhost:6000", {}, 421),
        ("demo.apps.localhost", {}, 421),
        (f"a.b.apps.localhost:{_PORT}", {}, 404),
        (_HOST, {"X-Forwarded-For": "203.0.113.9"}, 421),
        (_HOST, {"Forwarded": "for=203.0.113.9"}, 421),
    ],
)
@pytest.mark.asyncio
async def test_refusals(
    gateway: TestClient, backend: _Backend, host: str, extra: dict[str, str], status: int
) -> None:
    resp = await gateway.get("/api/session", headers={"Host": host, **extra})
    assert resp.status == status
    assert await resp.text() in ("not found", "misdirected request")
    assert backend.seen == []


@pytest.mark.asyncio
async def test_default_port_host_is_served_only_by_port_80_dashboard(
    gateway: TestClient, backend: _Backend
) -> None:
    allowed_origins = gateway.app["allowed_origins"]
    allowed_origins.clear()
    allowed_origins.update({"http://localhost:80", "http://localhost"})
    origins = awo.dashboard_loopback_origins(allowed_origins)
    assert awo._served_ports(origins) == {"80"}

    resp = await gateway.get("/", headers={"Host": "demo.apps.localhost"})
    assert resp.status == 200
    headers = {key.lower(): value for key, value in backend.seen[0]["headers"].items()}
    assert headers["host"] == "demo.apps.localhost:80"

    allowed_origins.clear()
    allowed_origins.update(_ALLOWED)
    resp = await gateway.get("/", headers={"Host": "demo.apps.localhost"})
    assert resp.status == 421
    assert len(backend.seen) == 1


@pytest.mark.asyncio
async def test_gateway_credentials_are_never_forwarded(
    gateway: TestClient, backend: _Backend
) -> None:
    resp = await gateway.post(
        "/api/thing?x=%20y",
        data=b"payload",
        headers={
            "Host": _HOST,
            "Cookie": "mc_token_5476=dash; kc_app=yes; mc_refresh_5476=r",
            "Authorization": "Bearer dash",
            "X-KiroCrew-Proxy": "1:forged",
            "X-KiroCrew-Proxy-V2": "forged",
            "X-KiroCrew-Principal": "forged",
            "X-Internal-Secret": "s",
            "X-Kiro-Relay-User": "someone",
            "Origin": f"http://{_HOST}",
        },
    )
    assert resp.status == 200
    seen = backend.seen[0]
    lowered = {k.lower(): v for k, v in seen["headers"].items()}
    assert seen["cookies"] == "kc_app=yes"
    assert not [k for k in lowered if k.startswith(("x-kirocrew-", "x-kiro-relay-", "x-internal"))]
    assert "authorization" not in lowered
    assert lowered["host"] == _HOST
    assert lowered["origin"] == f"http://{_HOST}"
    assert seen["body"] == b"payload"
    assert seen["target"] == "/api/thing?x=%20y"


@pytest.mark.asyncio
async def test_response_is_hardened(gateway: TestClient) -> None:
    resp = await gateway.get("/", headers={"Host": _HOST})
    assert resp.headers.getall("Set-Cookie") == ["app=1; Path=/"]
    assert "Clear-Site-Data" not in resp.headers
    policies = resp.headers.getall("Content-Security-Policy")
    assert policies[0] == "script-src 'none'"
    assert policies[1] == (
        f"frame-ancestors 'self' http://127.0.0.1:{_PORT} "
        f"http://kirocrew.localhost:{_PORT} http://localhost:{_PORT}"
    )


@pytest.mark.asyncio
async def test_websocket_round_trips(gateway: TestClient, backend: _Backend) -> None:
    async with asyncio.timeout(FALLBACK_TIMEOUT_SECS):
        ws = await gateway.ws_connect(
            "/live",
            headers={
                "Host": _HOST,
                "Cookie": "mc_token_5476=dash; kc_app=yes",
                "Origin": f"http://{_HOST}",
                "X-KiroCrew-Proxy": "1:forged",
            },
            protocols=("chat",),
        )
        assert ws.protocol == "chat"
        await ws.send_str("hi")
        assert (await ws.receive()).data == "echo:hi"
        await ws.send_bytes(b"abc")
        assert (await ws.receive()).data == b"cba"
        await ws.close()
    headers = {k.lower(): v for k, v in backend.seen[0]["headers"].items()}
    assert headers["cookie"] == "kc_app=yes"
    assert headers["host"] == _HOST
    assert headers["origin"] == f"http://{_HOST}"
    assert "x-kirocrew-proxy" not in headers


@pytest.mark.asyncio
async def test_the_session_manager_listener_never_reaches_a_backend(
    ssm_gateway: tuple[TestClient, int], backend: _Backend
) -> None:
    """A peer with only a Session Manager forward is refused before any backend lookup.

    The router runs before that listener's gate has judged the request, and the
    peer is loopback with no forwarding header, so only the socket's local port
    tells it apart from this machine's own browser.
    """
    client, ssm_port = ssm_gateway
    on_ssm = f"http://127.0.0.1:{ssm_port}"
    for host in (_HOST, f"DEMO.apps.localhost.:{_PORT}"):
        async with client.session.get(f"{on_ssm}/", headers={"Host": host}) as resp:
            assert resp.status == 421, host
            assert await resp.text() == "misdirected request"
    with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
        await client.session.ws_connect(f"{on_ssm}/live", headers={"Host": _HOST})
    assert refused.value.status == 421
    assert backend.seen == []
    # The same request on the dashboard's own listener is still served.
    resp = await client.get("/", headers={"Host": _HOST})
    assert resp.status == 200
    assert await resp.text() == "framed page"
    assert [s["target"] for s in backend.seen] == ["/"]


class _Audit:
    """Stands in for the security event log, keeping each row it is handed."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **row: Any) -> None:
        self.rows.append(row)


@pytest.mark.asyncio
async def test_each_serve_and_refusal_is_audited(
    gateway: TestClient, backend: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = _Audit()
    monkeypatch.setattr(awo, "sel", lambda: audit)
    served = await gateway.post("/doc/1?x=2", data=b"edit", headers={"Host": _HOST})
    assert served.status == 200
    async with asyncio.timeout(FALLBACK_TIMEOUT_SECS):
        ws = await gateway.ws_connect("/live?k=v", headers={"Host": _HOST}, protocols=("chat",))
        await ws.close()
    refused = await gateway.get("/", headers={"Host": f"nosuch.apps.localhost:{_PORT}"})
    assert refused.status == 404
    assert [(r["outcome"], r["operation"], r["resources"]) for r in audit.rows] == [
        ("granted", "POST app web origin", f"{_HOST}/doc/1"),
        ("granted", "GET app web origin", f"{_HOST}/live"),
        ("denied", "GET app web origin", f"nosuch.apps.localhost:{_PORT}"),
    ]
    assert {r["source"] for r in audit.rows} == {"app_web_origin"}


def test_the_websocket_message_cap_is_above_the_library_default() -> None:
    """A live document sync can send one frame larger than aiohttp's 4 MiB default."""
    assert awo._MAX_WS_MESSAGE_BYTES > 4 * 1024 * 1024


@pytest.mark.asyncio
async def test_an_oversized_message_from_the_page_never_reaches_the_backend(
    gateway: TestClient, backend: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(awo, "_MAX_WS_MESSAGE_BYTES", 1024)
    async with asyncio.timeout(FALLBACK_TIMEOUT_SECS):
        ws = await gateway.ws_connect("/live", headers={"Host": _HOST}, protocols=("chat",))
        await ws.send_str("small")
        assert (await ws.receive()).data == "echo:small"
        await ws.send_str("x" * 2048)
        closing = await ws.receive()
        assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        assert ws.close_code == aiohttp.WSCloseCode.MESSAGE_TOO_BIG
        await ws.close()
    await async_wait_until(lambda: backend.ws_close_codes, describe=lambda: backend.ws_close_codes)
    assert backend.ws_texts == ["small"]
    # The backend is told why, with a code an endpoint may send.
    assert backend.ws_close_codes == [aiohttp.WSCloseCode.MESSAGE_TOO_BIG]


@pytest.mark.asyncio
async def test_an_oversized_message_from_the_backend_closes_the_relay(
    gateway: TestClient, backend: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(awo, "_MAX_WS_MESSAGE_BYTES", 1024)
    # Under the cap on the way in; its echo ("echo:" + 1020 bytes) is over it.
    async with asyncio.timeout(FALLBACK_TIMEOUT_SECS):
        ws = await gateway.ws_connect("/live", headers={"Host": _HOST}, protocols=("chat",))
        await ws.send_str("z" * 1020)
        closing = await ws.receive()
        assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        assert ws.close_code == aiohttp.WSCloseCode.MESSAGE_TOO_BIG
        await ws.close()
    assert backend.ws_texts == ["z" * 1020]


@pytest.mark.asyncio
async def test_the_page_close_code_reaches_the_backend(
    gateway: TestClient, backend: _Backend
) -> None:
    async with asyncio.timeout(FALLBACK_TIMEOUT_SECS):
        ws = await gateway.ws_connect("/live", headers={"Host": _HOST}, protocols=("chat",))
        await ws.send_str("hi")
        assert (await ws.receive()).data == "echo:hi"
        await ws.close(code=4000)
    await async_wait_until(lambda: backend.ws_close_codes, describe=lambda: backend.ws_close_codes)
    assert backend.ws_close_codes == [4000]


@pytest.mark.asyncio
async def test_the_backend_close_code_reaches_the_page(
    gateway: TestClient, backend: _Backend
) -> None:
    async with asyncio.timeout(FALLBACK_TIMEOUT_SECS):
        ws = await gateway.ws_connect("/live", headers={"Host": _HOST}, protocols=("chat",))
        await ws.send_str("bye")
        closing = await ws.receive()
        assert closing.type == aiohttp.WSMsgType.CLOSE
        assert closing.data == 4001
        await ws.close()


@pytest.mark.asyncio
async def test_a_close_with_no_status_closes_both_legs_normally(
    gateway: TestClient, backend: _Backend
) -> None:
    """A browser's plain ``ws.close()`` sends an empty close frame; aiohttp reads it as 0."""
    async with asyncio.timeout(FALLBACK_TIMEOUT_SECS):
        ws = await gateway.ws_connect("/live", headers={"Host": _HOST}, protocols=("chat",))
        await ws.send_str("hi")
        assert (await ws.receive()).data == "echo:hi"
        writer = getattr(ws, "_writer", None)
        assert writer is not None and hasattr(writer, "send_frame"), "aiohttp writer API moved"
        await writer.send_frame(b"", aiohttp.WSMsgType.CLOSE)
        closing = await ws.receive()
        assert closing.type == aiohttp.WSMsgType.CLOSE
        assert closing.data == aiohttp.WSCloseCode.OK
        await ws.close()
    await async_wait_until(lambda: backend.ws_close_codes, describe=lambda: backend.ws_close_codes)
    assert backend.ws_close_codes == [aiohttp.WSCloseCode.OK]


@pytest.mark.parametrize(
    ("ended_with", "relayed"),
    [
        (None, 1000),
        (0, 1000),
        (1005, 1000),
        (1006, 1001),
        (1015, 1001),
        (999, 1002),
        (5000, 1002),
        (1000, 1000),
        (1009, 1009),
        (4000, 4000),
    ],
)
def test_only_a_sendable_close_code_is_relayed(ended_with: int | None, relayed: int) -> None:
    assert awo._close_code_to_relay(ended_with) == relayed


@pytest.mark.asyncio
async def test_unreachable_backend_is_502(gateway: TestClient, backend: _Backend) -> None:
    await backend.stop()
    resp = await gateway.get("/", headers={"Host": _HOST})
    assert resp.status == 502


# ── who may be served ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("backend_cfg", "enabled", "port", "served"),
    [
        ({"entryPoint": "s.py", "webOrigin": True}, True, 9107, True),
        ({"entryPoint": "s.py", "webOrigin": True}, False, 9107, False),
        ({"entryPoint": "s.py"}, True, 9107, False),
        ({"entryPoint": "s.py", "webOrigin": True}, True, None, False),
    ],
)
@pytest.mark.asyncio
async def test_backend_port_requires_enabled_opted_in_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend_cfg: dict[str, Any],
    enabled: bool,
    port: int | None,
    served: bool,
) -> None:
    import kiro_crew.apps.backend as backend_mod
    import kiro_crew.apps.manager as manager

    manifest = AppManifest.from_dict(
        {
            "name": "demo",
            "version": "1.0.0",
            "displayName": "D",
            "description": "d",
            "backend": backend_cfg,
        }
    )
    monkeypatch.setattr(
        manager, "get_app_manifest", lambda name: manifest if name == "demo" else None
    )
    monkeypatch.setattr(manager, "is_app_enabled", lambda _name: enabled)
    monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _name: port)
    assert (await awo._backend_port("demo")) == (port if served else None)
    assert await awo._backend_port("missing") is None


def test_dashboard_csrf_refuses_an_app_web_origin() -> None:
    """A page on an app web origin cannot post to a dashboard API."""
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.origin import check_origin

    request = MagicMock()
    request.app = {"allowed_origins": set(_ALLOWED)}
    request.remote = "127.0.0.1"
    request.headers = {"Origin": f"http://{_HOST}", "Host": f"localhost:{_PORT}"}
    assert check_origin(request) is False
    # Same request from the dashboard itself still passes.
    request.headers = {"Origin": f"http://localhost:{_PORT}", "Host": f"localhost:{_PORT}"}
    assert check_origin(request) is True


def test_dashboard_csp_admits_app_web_origins() -> None:
    """The dashboard may frame and probe an app web origin, and no wider ``*.`` name."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.server import _apply_security_headers

    app = web.Application()
    app["state"] = SimpleNamespace(instances_manager=None)
    app["allowed_origins"] = set(_ALLOWED)
    resp = web.Response(text="ok")
    _apply_security_headers(resp, app)
    csp = resp.headers["Content-Security-Policy"]
    directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}
    for name in ("frame-src", "connect-src"):
        wildcards = [s for s in directives[name] if s.startswith("http://*.")]
        assert wildcards == ["http://*.apps.localhost:*"], (name, wildcards)
