"""A relay Crew in a browser tab: the gateway keeps the grant and relays the Crew.

Each open gets an origin of its own (``<sid>.relay.localhost``), redeemed once by
a cookie-less GET into a host-only Strict cookie. Only that cookie gets requests
relayed, always to the listed Crew URL, with the relay header the gateway adds.
A sign-out, a sign-in, or an account change seen on a document or WebSocket drops
every open and cuts every relayed WebSocket.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import re
from collections.abc import AsyncIterator
from typing import Any, Literal

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from test_relay_viewer_routes import ACCESS, EP, viewer  # noqa: F401

from kiro_crew.dashboard import origin
from kiro_crew.dashboard.handlers import kas_login
from kiro_crew.dashboard.handlers import relay_viewer as rv
from kiro_crew.relay_viewer import browser_origin as bo
from kiro_crew.relay_viewer import cps
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

_PORT = "5476"
_ALLOWED = {f"http://localhost:{_PORT}", f"http://127.0.0.1:{_PORT}"}
TOKEN = "relay-access-token-SENSITIVE"


@pytest.fixture(autouse=True)
def _no_opens() -> Any:
    bo.close_all()
    yield
    bo.close_all()


class _Crew:
    """A stand-in for the Crew behind the relay, recording what reaches it."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.ws_open = asyncio.Event()
        self.ws_closed = asyncio.Event()
        self.first_part_sent = asyncio.Event()
        self.send_second_part = asyncio.Event()
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.seen.append({"path": request.path_qs, "headers": dict(request.headers)})
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            self.ws_open.set()
            async for _msg in ws:
                pass
            self.ws_closed.set()
            return ws
        if request.path == "/split":
            split = web.StreamResponse(headers={"Content-Type": "text/html"})
            await split.prepare(request)
            await split.write(b"<html><head><title>t</title>")
            await split.drain()
            self.first_part_sent.set()
            await self.send_second_part.wait()
            await split.write(b"<script src=/boot.js></script></head></html>")
            await split.write_eof()
            return split
        resp = web.Response(text="crew page", content_type="text/html")
        resp.headers["Clear-Site-Data"] = '"*"'
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Location"] = f"http://127.0.0.1:{self.port}/next"
        return resp

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest_asyncio.fixture
async def crew() -> AsyncIterator[_Crew]:
    c = _Crew()
    await c.start()
    try:
        yield c
    finally:
        await c.stop()


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[TestClient]:
    async def dashboard(_request: web.Request) -> web.Response:
        return web.Response(text="dashboard")

    app = web.Application(middlewares=[bo.build_relay_origin_middleware(None)])
    app["allowed_origins"] = set(_ALLOWED)
    app.router.add_route("*", "/{path:.*}", dashboard)
    # No jar: each test sends exactly the cookies it names, as a fresh tab would.
    client = TestClient(TestServer(app, host="127.0.0.1"), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


class _Account:
    def __init__(self) -> None:
        self.marker: str | None = "marker-a"
        self.renewals: list[str | None] = []

    async def renew(self, expected: str | None) -> tuple[str, float, float, str | None]:
        self.renewals.append(expected)
        return f"{TOKEN}-renewed", 240.0, 300.0, self.marker

    async def now(self) -> str | None:
        return self.marker


def _open(crew: _Crew, account: _Account, renew_in: float = 240.0) -> str:
    sid = bo.register(
        bo.epoch(),
        EP,
        f"http://127.0.0.1:{crew.port}",
        TOKEN,
        renew_in,
        300.0,
        account.marker,
        account.renew,
        account.now,
    )
    assert sid is not None
    return sid


def _host(sid: str) -> str:
    return f"{bo.relay_host(sid)}:{_PORT}"


async def _redeem(gateway: TestClient, sid: str) -> str:
    resp = await gateway.get("/", headers={"Host": _host(sid)})
    assert resp.status == 200
    morsel = resp.cookies[bo.COOKIE]
    assert morsel["httponly"] and morsel["samesite"] == "Strict" and not morsel["domain"]
    assert 'http-equiv="refresh"' in await resp.text()
    return morsel.value


def test_parse_relay_host() -> None:
    sid = "a" * 32
    assert bo.parse_relay_host(f"kcr-{sid}.localhost:5476") == (sid, "5476")
    assert bo.parse_relay_host(f"KCR-{sid.upper()}.Localhost.") == (sid, "80")
    for other in (
        "localhost:5476",
        "kirocrew.localhost:5476",
        "demo.apps.localhost:5476",
        "crew.localhost:9000",
    ):
        assert bo.parse_relay_host(other) is None
    for bad in ("kcr-short.localhost", f"kcr-{'1' * 32}.localhost"):
        assert bo.parse_relay_host(bad) is False, bad
    # Deeper names are not this router's; the dashboard's Host barrier refuses them.
    assert bo.parse_relay_host(f"x.kcr-{sid}.localhost") is None


def test_each_open_is_a_site_of_its_own() -> None:
    # One label under localhost, a public suffix by the list's default rule: no
    # page can set a cookie on a name that another open's host also falls under.
    host = bo.relay_host("a" * 32)
    assert host.count(".") == 1 and host.endswith(".localhost")


def test_the_dashboard_refuses_a_request_from_a_relayed_crew_origin() -> None:
    own = f"http://{bo.relay_host('a' * 32)}:{_PORT}"
    app = web.Application()
    app["allowed_origins"] = set(_ALLOWED)
    host = make_mocked_request("GET", "/api/status", headers={"Host": own[7:]}, app=app)
    assert origin.check_host(host) is False
    post = make_mocked_request(
        "POST", "/api/chat", headers={"Host": f"localhost:{_PORT}", "Origin": own}, app=app
    )
    assert origin.check_origin(post) is False


@pytest.mark.asyncio
async def test_redeemed_once_then_relayed_with_the_gateways_header_only(
    gateway: TestClient, crew: _Crew
) -> None:
    account = _Account()
    sid = _open(crew, account)
    cookie = await _redeem(gateway, sid)
    again = await gateway.get("/", headers={"Host": _host(sid)})
    assert again.status == 401, "a second cookie-less load is refused"
    assert again.headers["Clear-Site-Data"] == '"cache", "storage"'
    resp = await gateway.get(
        "/page?q=1",
        headers={
            "Host": _host(sid),
            "Cookie": f"{bo.COOKIE}={cookie}; mc_token_5476=dash; crew_own=1",
            "Kiro-Relay-Authorization": "Bearer page-made",
            "Authorization": "Bearer page",
            "Referer": "http://localhost:5476/chat",
            "X-KiroCrew-Proxy": "forged",
            "X-Session-Key": "dashboard:chat-2",
        },
    )
    assert resp.status == 200
    assert await resp.text() == "crew page"
    seen = crew.seen[-1]
    assert seen["path"] == "/page?q=1"
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["kiro-relay-authorization"] == f"Bearer {TOKEN}"
    assert headers["host"] == f"127.0.0.1:{crew.port}"
    assert headers.get("cookie") == "crew_own=1"
    assert headers["x-session-key"] == "dashboard:chat-2", "the page's own chat reaches its gateway"
    for gone in ("authorization", "referer", "x-kirocrew-proxy"):
        assert gone not in headers
    assert "Clear-Site-Data" not in resp.headers
    assert "Access-Control-Allow-Origin" not in resp.headers
    assert resp.headers["Location"] == f"http://{_host(sid)}/next"
    # The Crew may frame its own documents (artifacts, widgets); no other site may.
    assert "frame-ancestors 'self'" in resp.headers.getall("Content-Security-Policy")
    assert TOKEN not in await resp.text()


@pytest.mark.asyncio
async def test_a_wrong_cookie_a_foreign_origin_and_a_service_worker_are_refused(
    gateway: TestClient, crew: _Crew
) -> None:
    sid = _open(crew, _Account())
    cookie = await _redeem(gateway, sid)
    wrong = await gateway.get("/", headers={"Host": _host(sid), "Cookie": f"{bo.COOKIE}=nope"})
    assert wrong.status == 401
    jar = {"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
    foreign = await gateway.post("/api/x", headers={**jar, "Origin": "http://localhost:5476"})
    assert foreign.status == 403
    same = await gateway.post("/api/x", headers={**jar, "Origin": f"http://{_host(sid)}"})
    assert same.status == 200
    rewritten = {k.lower(): v for k, v in crew.seen[-1]["headers"].items()}
    assert rewritten["origin"] == f"http://127.0.0.1:{crew.port}"
    worker = await gateway.get("/sw.js", headers={**jar, "Service-Worker": "script"})
    assert worker.status == 403
    assert len(crew.seen) == 1, "only the same-origin POST reached the Crew"


@pytest.mark.asyncio
async def test_a_malformed_name_and_a_forwarded_request_reach_nothing(
    gateway: TestClient, crew: _Crew
) -> None:
    resp = await gateway.get("/", headers={"Host": f"kcr-x.localhost:{_PORT}"})
    assert resp.status == 404
    sid = _open(crew, _Account())
    proxied = await gateway.get("/", headers={"Host": _host(sid), "X-Forwarded-For": "10.0.0.9"})
    assert proxied.status == 421
    other_port = await gateway.get("/", headers={"Host": f"{bo.relay_host(sid)}:9"})
    assert other_port.status == 421
    assert crew.seen == []


@pytest.mark.asyncio
async def test_a_redeem_after_its_window_is_refused(gateway: TestClient, crew: _Crew) -> None:
    sid = _open(crew, _Account())
    bo._opens[sid].deadline = 0.0  # the window has passed
    resp = await gateway.get("/", headers={"Host": _host(sid)})
    assert resp.status == 401


@pytest.mark.asyncio
async def test_a_second_open_of_the_crew_kills_the_first_and_close_all_kills_both(
    gateway: TestClient, crew: _Crew
) -> None:
    first = _open(crew, _Account())
    cookie = await _redeem(gateway, first)
    second = _open(crew, _Account())
    assert second != first
    dead = await gateway.get("/", headers={"Host": _host(first), "Cookie": f"{bo.COOKIE}={cookie}"})
    assert dead.status == 401
    at = bo.epoch()
    bo.close_all()
    assert bo.epoch() == at + 1
    gone = await gateway.get("/", headers={"Host": _host(second)})
    assert gone.status == 401
    late = bo.register(
        at,
        EP,
        f"http://127.0.0.1:{crew.port}",
        TOKEN,
        240.0,
        300.0,
        "m",
        _Account().renew,
        _Account().now,
    )
    assert late is None, "a mint that straddled close_all stores nothing"


@pytest.mark.asyncio
async def test_a_document_load_after_an_account_change_closes_every_open(
    gateway: TestClient, crew: _Crew
) -> None:
    account = _Account()
    sid = _open(crew, account)
    cookie = await _redeem(gateway, sid)
    jar = {"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
    sub = await gateway.get("/app.js", headers={**jar, "Sec-Fetch-Dest": "script"})
    assert sub.status == 200
    account.marker = "marker-b"
    still = await gateway.get("/more.js", headers={**jar, "Sec-Fetch-Dest": "script"})
    assert still.status == 200, "a sub-resource is not where the account is read"
    doc = await gateway.get("/", headers={**jar, "Sec-Fetch-Dest": "document"})
    assert doc.status == 401
    assert bo.parse_relay_host(_host(sid)) == (sid, _PORT)
    after = await gateway.get("/more.js", headers=jar)
    assert after.status == 401, "every open is gone"


@pytest.mark.asyncio
async def test_a_due_token_is_renewed_with_the_opens_own_marker(
    gateway: TestClient, crew: _Crew
) -> None:
    account = _Account()
    sid = _open(crew, account, renew_in=0.0)
    cookie = await _redeem(gateway, sid)
    resp = await gateway.get("/", headers={"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"})
    assert resp.status == 200
    assert account.renewals == ["marker-a"]
    headers = {k.lower(): v for k, v in crew.seen[-1]["headers"].items()}
    assert headers["kiro-relay-authorization"] == f"Bearer {TOKEN}-renewed"


@pytest.mark.asyncio
async def test_a_signed_out_renewal_closes_every_open(gateway: TestClient, crew: _Crew) -> None:
    account = _Account()

    async def refused(_expected: str | None) -> tuple[str, float, float, str | None]:
        raise cps.CpsRefused(cps.SIGNED_OUT, "signed out")

    sid = bo.register(
        bo.epoch(),
        EP,
        f"http://127.0.0.1:{crew.port}",
        TOKEN,
        0.0,
        300.0,
        "m",
        refused,
        account.now,
    )
    assert sid is not None
    other = _open(crew, account)
    assert other
    cookie = await _redeem(gateway, other)
    bo._opens[other].renew = refused
    bo._opens[other].renew_at = 0.0
    resp = await gateway.get("/", headers={"Host": _host(other), "Cookie": f"{bo.COOKIE}={cookie}"})
    assert resp.status == 401
    assert bo._opens == {}


@pytest.mark.asyncio
async def test_close_all_cuts_a_relayed_websocket(gateway: TestClient, crew: _Crew) -> None:
    sid = _open(crew, _Account())
    cookie = await _redeem(gateway, sid)
    ws = await gateway.ws_connect(
        "/api/ws", headers={"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
    )
    await asyncio.wait_for(crew.ws_open.wait(), FALLBACK_TIMEOUT_SECS)
    headers = {k.lower(): v for k, v in crew.seen[-1]["headers"].items()}
    assert headers["kiro-relay-authorization"] == f"Bearer {TOKEN}"
    bo.close_all()
    msg = await asyncio.wait_for(ws.receive(), FALLBACK_TIMEOUT_SECS)
    assert msg.type in (
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.CLOSING,
    )
    await ws.close()
    await async_wait_until(lambda: crew.ws_closed.is_set(), timeout=FALLBACK_TIMEOUT_SECS)


def test_sign_out_and_sign_in_close_every_browser_open(crew: _Crew) -> None:
    sid = bo.register(
        bo.epoch(),
        EP,
        "https://x.example.test",
        TOKEN,
        240.0,
        300.0,
        "m",
        _Account().renew,
        _Account().now,
    )
    assert sid in bo._opens
    kas_login._close_browser_relay_crews()
    assert bo._opens == {}


# ── the open route ───────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _route_client(served: bool = True) -> AsyncIterator[TestClient]:
    """The open route on a dashboard whose own port is served (or, with
    *served* False, a forward on a port the dashboard does not serve)."""
    app = web.Application()
    origins: set[str] = set()
    app["allowed_origins"] = origins
    app.router.add_post("/api/relay-viewer/browser-open", rv.api_relay_viewer_browser_open)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        origins.add(f"http://127.0.0.1:{client.port if served else client.port + 1}")
        yield client


@pytest.mark.asyncio
async def test_browser_open_answers_only_the_tabs_url(viewer) -> None:  # noqa: F811
    async with _route_client() as client:
        resp = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        body = await resp.json()
        assert resp.status == 200, body
        assert set(body) == {"url"}
        assert ACCESS not in await resp.text()
        sid, port = bo.parse_relay_host(body["url"].split("//", 1)[1].rstrip("/"))  # type: ignore[misc]
        assert port == str(client.port)
        assert bo._opens[sid].token == ACCESS
        again = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        second = (await again.json())["url"]
        assert second != body["url"] and sid not in bo._opens, "one token stands per Crew"
        assert [c for c in viewer.calls if c[0] == "mint"] == [("mint", EP), ("mint", EP)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "opened"),
    [
        (f"{EP}.us-east-1.relay.kiro.dev", True),
        (f"{EP}.kiro-relay.eu-central-1.on.aws", True),
        # The address CPS lists is trusted, so any host under the Crew's own name opens.
        (f"{EP}.us-east-1.relay.kiro.dev.evil.example.test", True),
        (f"x.{EP}.evil.example.test", False),
        (f"{EP}.kiro-relay.eu-central-1.on.aws:8443", False),
    ],
)
async def test_with_no_setting_the_browser_opens_only_a_crew_at_its_own_name(
    viewer, monkeypatch, host, opened  # noqa: F811
) -> None:
    """The list's host rule holds for the browser open too: no mint off the Crew's own name."""
    viewer.enabled = False
    raw = {"endpointId": EP, "crewUrl": f"https://{host}", "name": "Box", "crewType": "TEAM"}

    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        crew = cps._crew_endpoint(raw, crew_domain)
        return () if crew is None else (crew,)

    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    async with _route_client() as client:
        resp = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        body = await resp.json()
    minted = [c for c in viewer.calls if c[0] == "mint"]
    if opened:
        assert resp.status == 200 and set(body) == {"url"}
        assert minted == [("mint", EP)] and len(bo._opens) == 1
    else:
        assert (resp.status, body["code"]) == (403, "CREW_ACCESS_NOT_GRANTED")
        assert minted == [] and bo._opens == {}


@pytest.mark.asyncio
async def test_browser_open_on_a_port_the_dashboard_does_not_serve_mints_nothing(
    viewer,  # noqa: F811
) -> None:
    async with _route_client(served=False) as client:
        resp = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        assert resp.status == 403
        assert (await resp.json())["code"] == "BROWSER_PORT_NOT_SERVED"
    assert [c for c in viewer.calls if c[0] == "mint"] == []
    assert bo._opens == {}


@pytest.mark.asyncio
async def test_browser_open_is_owner_only_local_only_and_off_in_a_locked_build(
    viewer,  # noqa: F811
) -> None:
    async with _route_client() as client:
        viewer.owner = False
        assert (
            await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        ).status == 403
        viewer.owner = True
        forwarded = await client.post(
            "/api/relay-viewer/browser-open",
            json={"endpointId": EP},
            headers={"X-Forwarded-For": "10.0.0.9"},
        )
        assert forwarded.status == 403
        assert (await forwarded.json())["code"] == "BROWSER_NOT_LOCAL"
        viewer.locked = True
        off = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        assert off.status == 404
    assert viewer.calls == [] and bo._opens == {}


@pytest.mark.asyncio
async def test_browser_open_during_a_sign_out_stores_nothing(
    viewer, monkeypatch  # noqa: F811
) -> None:
    real = cps.create_crew_access_token

    async def mint_then_sign_out(*args: Any, **kwargs: Any) -> Any:
        grant = await real(*args, **kwargs)
        bo.close_all()
        return grant

    monkeypatch.setattr(cps, "create_crew_access_token", mint_then_sign_out)
    async with _route_client() as client:
        resp = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        assert resp.status == 409
        assert (await resp.json())["code"] == "SIGNED_OUT"
    assert bo._opens == {}


@pytest.mark.asyncio
async def test_close_all_cuts_a_response_still_streaming(gateway: TestClient) -> None:
    started = asyncio.Event()
    stop = asyncio.Event()

    async def stream(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b"data: one\n\n")
        started.set()
        await stop.wait()
        return resp

    app = web.Application()
    app.router.add_get("/{path:.*}", stream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = runner.addresses[0][1]
        account = _Account()
        sid = bo.register(
            bo.epoch(),
            EP,
            f"http://127.0.0.1:{port}",
            TOKEN,
            240.0,
            300.0,
            "marker-a",
            account.renew,
            account.now,
        )
        assert sid is not None
        cookie = await _redeem(gateway, sid)
        resp = await gateway.get(
            "/api/events", headers={"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
        )
        assert await resp.content.readline() == b"data: one\n"
        await asyncio.wait_for(started.wait(), FALLBACK_TIMEOUT_SECS)
        bo.close_all()
        # The relay was cut mid-body, so the tab's response ends short.
        with pytest.raises(aiohttp.ClientPayloadError):
            await asyncio.wait_for(resp.content.read(), FALLBACK_TIMEOUT_SECS)
        resp.release()
    finally:
        stop.set()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_renewal_once_the_viewer_is_locked_off_ends_the_open(
    viewer,  # noqa: F811
) -> None:
    async with _route_client() as client:
        resp = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        sid = bo.parse_relay_host((await resp.json())["url"].split("//", 1)[1].rstrip("/"))[0]  # type: ignore[index]
    opened = bo._opens[sid]
    viewer.locked = True
    with pytest.raises(cps.CpsRefused) as refused:
        await opened.renew(opened.marker)
    assert refused.value.code == cps.NOT_SET_UP
    opened.renew_at = 0.0
    assert await bo._fresh(sid, opened, recheck=False) is False
    assert sid not in bo._opens


@pytest.mark.asyncio
async def test_a_passing_renewal_failure_keeps_the_open_on_its_token(
    gateway: TestClient, crew: _Crew
) -> None:
    calls: list[str | None] = []

    async def flaky(expected: str | None) -> tuple[str, float, float, str | None]:
        calls.append(expected)
        raise cps.CpsRefused(cps.UNREACHABLE, "control plane unreachable")

    account = _Account()
    sid = _open(crew, account, renew_in=0.0)
    bo._opens[sid].renew = flaky
    cookie = await _redeem(gateway, sid)
    jar = {"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
    resp = await gateway.get("/", headers=jar)
    assert resp.status == 200, "still relayed on the token it holds"
    headers = {k.lower(): v for k, v in crew.seen[-1]["headers"].items()}
    assert headers["kiro-relay-authorization"] == f"Bearer {TOKEN}"
    again = await gateway.get("/", headers=jar)
    assert again.status == 200
    assert len(calls) == 1, "the next try waits"
    bo._opens[sid].renew_at = 0.0
    bo._opens[sid].expires_at = 0.0
    gone = await gateway.get("/", headers=jar)
    assert gone.status == 401, "once the token it holds has ended, the open ends"


@pytest.mark.asyncio
async def test_the_two_403_refusals_are_recorded(
    gateway: TestClient, crew: _Crew, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import app_web_origin

    rows: list[tuple[str, str]] = []

    async def record(_request: web.Request, *, outcome: str, resources: str, error: str) -> None:
        rows.append((outcome, error))

    monkeypatch.setattr(app_web_origin, "_audit", record)
    sid = _open(crew, _Account())
    cookie = await _redeem(gateway, sid)
    jar = {"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
    rows.clear()
    worker = await gateway.get("/sw.js", headers={**jar, "Service-Worker": "script"})
    foreign = await gateway.post("/api/x", headers={**jar, "Origin": "http://localhost:5476"})
    assert (worker.status, foreign.status) == (403, 403)
    assert rows == [("denied", "service worker script"), ("denied", "cross-origin write")]


@pytest.mark.asyncio
async def test_a_relayed_page_gets_the_watch_script_and_a_sub_resource_does_not(
    gateway: TestClient, crew: _Crew
) -> None:
    sid = _open(crew, _Account())
    cookie = await _redeem(gateway, sid)
    jar = {"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
    doc = await gateway.get(
        "/", headers={**jar, "Sec-Fetch-Dest": "document", "If-None-Match": '"v1"'}
    )
    assert doc.status == 200
    assert await doc.text() == f'<script src="{bo.WATCH_PATH}"></script>crew page'
    assert doc.headers["Cache-Control"] == "no-store"
    sent = {k.lower(): v for k, v in crew.seen[-1]["headers"].items()}
    assert sent["accept-encoding"] == "identity" and "if-none-match" not in sent
    sub = await gateway.get("/app.js", headers={**jar, "Sec-Fetch-Dest": "script"})
    assert await sub.text() == "crew page"
    script = await gateway.get(bo.WATCH_PATH, headers=jar)
    assert script.status == 200 and bo.ALIVE_PATH in await script.text()
    assert all(seen["path"] != bo.WATCH_PATH for seen in crew.seen), "never relayed"
    assert bo._with_watch(b"<html><HEAD lang=x><title>t") == (
        b'<html><HEAD lang=x><script src="/.kcr/watch.js"></script><title>t'
    )


@pytest.mark.asyncio
async def test_a_held_watch_is_answered_closed_when_the_open_is_dropped(
    gateway: TestClient, crew: _Crew, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _Account()
    sid = _open(crew, account)
    cookie = await _redeem(gateway, sid)
    jar = {"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
    monkeypatch.setattr(bo, "ALIVE_SECONDS", 0.0)
    quiet = await gateway.get(bo.ALIVE_PATH, headers=jar)
    assert quiet.status == 204, "still open: the script asks again"
    monkeypatch.setattr(bo, "ALIVE_SECONDS", FALLBACK_TIMEOUT_SECS)
    parked = asyncio.Event()

    class _Gone(asyncio.Event):
        async def wait(self) -> Literal[True]:
            parked.set()
            return await super().wait()

    bo._opens[sid].gone = _Gone()
    held = asyncio.ensure_future(gateway.get(bo.ALIVE_PATH, headers=jar))
    await asyncio.wait_for(parked.wait(), FALLBACK_TIMEOUT_SECS)
    bo.close_all()
    resp = await asyncio.wait_for(held, FALLBACK_TIMEOUT_SECS)
    assert resp.status == 401
    assert resp.headers["Clear-Site-Data"] == '"cache", "storage"'
    assert all(seen["path"] != bo.ALIVE_PATH for seen in crew.seen), "never relayed"


@pytest.mark.asyncio
async def test_a_watch_sees_a_sign_in_changed_elsewhere(gateway: TestClient, crew: _Crew) -> None:
    account = _Account()
    sid = _open(crew, account)
    cookie = await _redeem(gateway, sid)
    account.marker = "marker-b"
    resp = await gateway.get(
        bo.ALIVE_PATH, headers={"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"}
    )
    assert resp.status == 401
    assert bo._opens == {}


@pytest.mark.asyncio
async def test_the_shared_relay_session_keeps_no_cookies() -> None:
    from kiro_crew.apps import app_web_origin
    from kiro_crew.apps.routes import register_app_routes

    app = web.Application()
    register_app_routes(app)
    for start in app.on_startup:
        await start(app)
    try:
        assert isinstance(app["_proxy_session"].cookie_jar, aiohttp.DummyCookieJar)
    finally:
        for cleanup in app.on_cleanup:
            await cleanup(app)
    request = make_mocked_request("GET", "/", app=web.Application())
    fallback, owns = app_web_origin._client_session(request)
    try:
        assert owns and isinstance(fallback.cookie_jar, aiohttp.DummyCookieJar)
    finally:
        await fallback.close()


@pytest.mark.asyncio
async def test_a_page_sent_in_parts_is_edited_whole(gateway: TestClient, crew: _Crew) -> None:
    sid = _open(crew, _Account())
    cookie = await _redeem(gateway, sid)
    headers = {"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}", "Sec-Fetch-Dest": "document"}
    load = asyncio.ensure_future(gateway.get("/split", headers=headers))
    await asyncio.wait_for(crew.first_part_sent.wait(), FALLBACK_TIMEOUT_SECS)
    crew.send_second_part.set()
    resp = await asyncio.wait_for(load, FALLBACK_TIMEOUT_SECS)
    assert await resp.text() == (
        f'<html><head><script src="{bo.WATCH_PATH}"></script><title>t</title>'
        "<script src=/boot.js></script></head></html>"
    )


@pytest.mark.asyncio
async def test_a_sign_out_during_the_account_read_relays_nothing(
    gateway: TestClient, crew: _Crew
) -> None:
    account = _Account()
    sid = _open(crew, account)
    cookie = await _redeem(gateway, sid)

    async def signed_out_meanwhile() -> str | None:
        bo.close_all()
        return account.marker  # the old marker: the read began before the sign-out

    bo._opens[sid].marker_now = signed_out_meanwhile
    resp = await gateway.get(
        "/",
        headers={
            "Host": _host(sid),
            "Cookie": f"{bo.COOKIE}={cookie}",
            "Sec-Fetch-Dest": "document",
        },
    )
    assert resp.status == 401
    assert crew.seen == []


@pytest.mark.asyncio
async def test_a_token_past_its_end_is_never_used_during_a_retry_wait(
    gateway: TestClient, crew: _Crew
) -> None:
    async def flaky(_expected: str | None) -> tuple[str, float, float, str | None]:
        raise cps.CpsRefused(cps.UNREACHABLE, "control plane unreachable")

    sid = _open(crew, _Account())
    cookie = await _redeem(gateway, sid)
    opened = bo._opens[sid]
    opened.renew = flaky
    opened.renew_at = float("inf")  # mid retry wait
    opened.expires_at = 0.0  # but the token has ended
    resp = await gateway.get("/", headers={"Host": _host(sid), "Cookie": f"{bo.COOKIE}={cookie}"})
    assert resp.status == 401
    assert crew.seen == []
    assert sid not in bo._opens


@pytest.mark.asyncio
async def test_a_same_origin_write_on_the_default_port_is_relayed(
    gateway: TestClient, crew: _Crew
) -> None:
    gateway.server.app["allowed_origins"].add("http://localhost:80")
    sid = _open(crew, _Account())
    host = bo.relay_host(sid)  # no port: the browser is on port 80
    redeem = await gateway.get("/", headers={"Host": host})
    cookie = redeem.cookies[bo.COOKIE].value
    resp = await gateway.post(
        "/api/x",
        headers={"Host": host, "Cookie": f"{bo.COOKIE}={cookie}", "Origin": f"http://{host}"},
    )
    assert resp.status == 200
    sent = {k.lower(): v for k, v in crew.seen[-1]["headers"].items()}
    assert sent["origin"] == f"http://127.0.0.1:{crew.port}"


@pytest.mark.asyncio
async def test_the_startup_chain_routes_a_relayed_host_and_passes_every_other(
    crew: _Crew,
) -> None:
    from kiro_crew.dashboard.server_runtime.middleware_chain import _relay_origin_middleware

    async def dashboard(_request: web.Request) -> web.Response:
        return web.Response(text="dashboard")

    app = web.Application(middlewares=[_relay_origin_middleware(None)])
    app["allowed_origins"] = set(_ALLOWED)
    app.router.add_route("*", "/{path:.*}", dashboard)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        sid = _open(crew, _Account())
        relayed = await client.get("/", headers={"Host": _host(sid)})
        assert bo.COOKIE in relayed.cookies, "the relay router redeemed it"
        plain = await client.get("/", headers={"Host": f"localhost:{_PORT}"})
        assert await plain.text() == "dashboard"


@pytest.mark.asyncio
async def test_every_browser_open_decision_is_recorded(
    viewer, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    rows: list[tuple[str, str, str]] = []

    def record(
        _request: web.Request, outcome: str, resources: str, operation: str = "", source: str = ""
    ) -> None:
        rows.append((operation, outcome, resources))

    monkeypatch.setattr(rv, "_audit", record)
    async with _route_client() as client:
        ok = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
        bad = await client.post("/api/relay-viewer/browser-open", json={"endpointId": "x"})
    async with _route_client(served=False) as client:
        port = await client.post("/api/relay-viewer/browser-open", json={"endpointId": EP})
    assert (ok.status, bad.status, port.status) == (200, 400, 403)
    assert rows == [
        ("relay_viewer.browser_open", "success", EP),
        ("relay_viewer.browser_open", "denied", cps.INVALID_REQUEST),
        ("relay_viewer.browser_open", "denied", "BROWSER_PORT_NOT_SERVED"),
    ]
    assert all(bo.relay_host("")[:4] not in r[2] for r in rows), "no sid is recorded"


def test_the_closed_tab_page_is_styled_by_its_hash_alone() -> None:
    """The page a closed Crew tab shows carries its own style, and the CSP allows only that."""
    resp = bo._closed()
    style = re.search(r"<style>(.*?)</style>", resp.text, re.S)
    assert resp.status == 401 and style is not None and "<h1>" in resp.text
    digest = base64.b64encode(hashlib.sha256(style.group(1).encode()).digest()).decode()
    csp = resp.headers["Content-Security-Policy"]
    assert f"style-src 'sha256-{digest}'" in csp
    assert "default-src 'none'" in csp and "unsafe-inline" not in csp
