"""The enterprise gate's login half: gate state, login watch and middleware.

Design: docs/request-for-change/rfc-crew-enterprise-edition.md, "Enterprise gate".
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import enterprise_gate_middleware as gate_mw
from kiro_crew.dashboard import routes as routes_pkg
from kiro_crew.enterprise_gate import REASON_LOGIN_REQUIRED, EnterpriseGate, LoginWatch
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
from kiro_crew.testing.wait import async_wait_until


class FakeLogin:
    """A login source whose verdict the test sets."""

    def __init__(self, signed_in: bool = False) -> None:
        self.signed_in = signed_in
        self.verified_calls: list[float] = []
        self.raise_on_verify = False

    @property
    def authenticated(self) -> bool:
        return self.signed_in

    async def login_verified(self, *, max_age_secs: float) -> bool:
        self.verified_calls.append(max_age_secs)
        if self.raise_on_verify:
            raise RuntimeError("probe broke")
        return self.signed_in


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


# --------------------------------------------------------------------------- #
# Gate state


def test_gate_starts_closed_for_login() -> None:
    gate = EnterpriseGate()
    status = gate.status()
    assert not gate.is_open
    assert status.open is False
    assert status.reason == REASON_LOGIN_REQUIRED


def test_gate_opens_only_when_every_reason_is_cleared() -> None:
    gate = EnterpriseGate()
    seen: list[bool] = []
    gate.add_listener(lambda status: seen.append(status.open))
    assert gate.close("governance_unavailable") is False  # already closed
    assert gate.clear(REASON_LOGIN_REQUIRED) is False  # governance still holds it
    assert gate.status().reasons == ("governance_unavailable",)
    assert gate.clear("governance_unavailable") is True
    assert gate.is_open and gate.status().reason == ""
    assert gate.close("governance_unavailable") is True
    assert seen == [True, False]


def test_gate_clearing_an_absent_reason_changes_nothing() -> None:
    gate = EnterpriseGate(closed_for=())
    assert gate.is_open
    assert gate.clear("governance_unavailable") is False
    assert gate.is_open


@pytest.mark.parametrize("reason", ["", "Login", "has space", "x" * 65, None])
def test_gate_refuses_a_malformed_reason(reason: Any) -> None:
    gate = EnterpriseGate()
    with pytest.raises(ValueError):
        gate.close(reason)


def test_a_failing_listener_does_not_stop_the_others() -> None:
    gate = EnterpriseGate()
    seen: list[bool] = []

    def boom(_status: Any) -> None:
        raise RuntimeError("listener broke")

    gate.add_listener(boom)
    gate.add_listener(lambda status: seen.append(status.open))
    gate.clear(REASON_LOGIN_REQUIRED)
    assert seen == [True]


# --------------------------------------------------------------------------- #
# Login watch


@pytest.mark.asyncio
async def test_watch_check_opens_on_login_and_closes_on_expiry() -> None:
    gate = EnterpriseGate()
    login = FakeLogin(signed_in=True)
    watch = LoginWatch(gate, login)
    assert await watch.check() is True
    assert gate.is_open
    login.signed_in = False
    assert await watch.check() is False
    assert gate.status().reason == REASON_LOGIN_REQUIRED
    login.signed_in = True
    await watch.check()
    assert gate.is_open


@pytest.mark.asyncio
async def test_a_close_wakes_a_watch_sleeping_its_open_interval() -> None:
    gate = EnterpriseGate()
    sleeps: list[float] = []
    second_check = asyncio.Event()

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)
        await asyncio.Event().wait()  # never ends on its own

    class Counting(FakeLogin):
        async def login_verified(self, *, max_age_secs: float) -> bool:
            result = await super().login_verified(max_age_secs=max_age_secs)
            if len(self.verified_calls) == 2:
                second_check.set()
            return result

    login = Counting(signed_in=True)
    watch = LoginWatch(gate, login, sleep=fake_sleep)
    watch.start()
    try:
        await async_wait_until(lambda: gate.is_open)
        # An ACP auth failure narrows the latch; the next request's sync closes.
        login.signed_in = False
        watch.sync()
        await asyncio.wait_for(second_check.wait(), timeout=30)
        assert sleeps[0] == 60.0  # it was in the long open sleep
        assert login.verified_calls[1] == 60.0  # and checked at the closed age
    finally:
        await watch.stop()


@pytest.mark.asyncio
async def test_watch_check_fails_closed_when_the_probe_raises() -> None:
    gate = EnterpriseGate(closed_for=())
    login = FakeLogin(signed_in=True)
    login.raise_on_verify = True
    assert await LoginWatch(gate, login).check() is False
    assert not gate.is_open


def test_watch_sync_closes_on_a_signed_out_latch_and_never_opens() -> None:
    gate = EnterpriseGate(closed_for=())
    login = FakeLogin(signed_in=False)
    watch = LoginWatch(gate, login)
    watch.sync()
    assert not gate.is_open
    login.signed_in = True
    watch.sync()
    assert not gate.is_open  # only check() opens


@pytest.mark.asyncio
async def test_watch_run_checks_at_once_then_on_each_interval() -> None:
    gate = EnterpriseGate()
    login = FakeLogin(signed_in=True)
    sleeps: list[float] = []
    stop = asyncio.Event()

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)
        if len(sleeps) >= 2:
            stop.set()
            await asyncio.Event().wait()

    watch = LoginWatch(gate, login, sleep=fake_sleep)
    watch.start()
    await asyncio.wait_for(stop.wait(), timeout=30)
    await watch.stop()
    assert gate.is_open
    assert len(login.verified_calls) == 2
    # Open: the long interval and the long probe age.
    assert sleeps == [60.0, 60.0]
    assert login.verified_calls == [60.0, 900.0]


# --------------------------------------------------------------------------- #
# The service's login check (the reused whoami probe)


def _service(tmp_path: Any) -> KiroPrerequisiteService:
    return KiroPrerequisiteService(
        platform_name="linux",
        environ={},
        home=tmp_path,
        data_home=tmp_path / "crew",
        clock=lambda: 1000.0,
        spec_lister=lambda: [],
        warm_up_delay=0,
    )


@pytest.mark.asyncio
async def test_login_verified_probes_when_never_probed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    calls: list[bool] = []

    async def fake_probe(*, force: bool = False) -> None:
        calls.append(force)
        service._has_probed = True
        service._last_probe_at = 1000.0
        service._status.authenticated = True

    monkeypatch.setattr(service, "_probe", fake_probe)
    assert service.authenticated is False
    assert await service.login_verified(max_age_secs=60) is True
    assert calls == [True]
    assert service.authenticated is True


def test_test_mode_does_not_assume_a_login_in_an_enterprise_build(
    tmp_path: Any, enterprise: None
) -> None:
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={},
        home=tmp_path,
        data_home=tmp_path / "crew",
        spec_lister=lambda: [],
        assume_ready=True,
    )
    assert service.authenticated is False
    assert service._assume_ready is False


def test_test_mode_still_assumes_a_login_in_oss(tmp_path: Any) -> None:
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={},
        home=tmp_path,
        data_home=tmp_path / "crew",
        spec_lister=lambda: [],
        assume_ready=True,
    )
    assert service.authenticated is True


@pytest.mark.asyncio
async def test_enterprise_test_mode_login_check_still_probes(
    tmp_path: Any, enterprise: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={},
        home=tmp_path,
        data_home=tmp_path / "crew",
        clock=lambda: 1000.0,
        spec_lister=lambda: [],
        assume_ready=True,
    )
    calls: list[bool] = []

    async def fake_probe(*, force: bool = False) -> None:
        calls.append(force)

    monkeypatch.setattr(service, "_probe", fake_probe)
    gate = EnterpriseGate()
    assert await LoginWatch(gate, service).check() is False
    assert calls == [True]
    assert not gate.is_open


@pytest.mark.asyncio
async def test_login_verified_reprobes_on_an_identity_change(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service._has_probed = True
    service._last_probe_at = 1000.0
    service._status.authenticated = True
    calls: list[bool] = []

    async def fake_probe(*, force: bool = False) -> None:
        calls.append(force)
        service._status.authenticated = False

    async def changed() -> bool:
        return True

    monkeypatch.setattr(service, "_probe", fake_probe)
    monkeypatch.setattr(service, "identity_changed_since_probe", changed)
    assert await service.login_verified(max_age_secs=900) is False
    assert calls == [True]


@pytest.mark.asyncio
async def test_login_verified_fails_closed_when_the_probe_raises(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service._status.authenticated = True

    async def broken_probe(*, force: bool = False) -> None:
        raise OSError("spawn failed")

    monkeypatch.setattr(service, "_probe", broken_probe)
    assert await service.login_verified(max_age_secs=0) is False


# --------------------------------------------------------------------------- #
# Middleware over a real aiohttp server


async def _ok(_request: web.Request) -> web.Response:
    return web.json_response({"data": "secret"})


async def _shell(_request: web.Request) -> web.Response:
    return web.Response(text="<html>shell</html>", content_type="text/html")


async def _ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_str("hello")
    async for _msg in ws:
        pass
    return ws


def _install(app: web.Application, login: FakeLogin) -> None:
    """Install the gate as boot does (after a seen login), then apply *login*.

    A boot with no login exits before this (``test_enterprise_boot.py``), so a
    closed gate here is always a login lost while the gateway runs.
    """
    wanted = login.signed_in
    login.signed_in = True
    gate_mw.install_enterprise_gate(app, login)
    login.signed_in = wanted
    if not wanted:
        app[gate_mw.GATE_APP_KEY].close(REASON_LOGIN_REQUIRED)


def _gated_app(login: FakeLogin) -> web.Application:
    app = web.Application()
    _install(app, login)
    middleware = gate_mw.enterprise_gate_middleware_for(app, spa_shell_handler=_shell)
    assert middleware is not None
    app.middlewares.append(middleware)
    app.router.add_get("/api/kiro-prerequisite", _ok)
    app.router.add_get("/api/status", _ok)
    app.router.add_post("/api/shutdown", _ok)
    app.router.add_get("/api/shutdown", _ok)
    app.router.add_get("/api/ws", _ws)
    app.router.add_get("/assets/app.js", _ok)
    return app


async def _client(app: web.Application, *, keep_alive: bool = False) -> TestClient:
    # One connection per request by default: closing the gate closes every
    # connection that carried gated data, and aiohttp's client does not retry
    # a request on a pooled connection the server closed (a browser does).
    if keep_alive:
        client = TestClient(TestServer(app))
    else:
        client = TestClient(TestServer(app), connector=aiohttp.TCPConnector(force_close=True))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_closed_gate_denies_an_api_route_and_allows_login(enterprise: None) -> None:
    login = FakeLogin(signed_in=False)
    client = await _client(_gated_app(login))
    try:
        denied = await client.get("/api/status")
        assert denied.status == 403
        body = await denied.json()
        assert body["code"] == "enterprise_gate_closed"
        assert body["reason"] == REASON_LOGIN_REQUIRED
        assert "secret" not in await denied.text()

        allowed = await client.get("/api/kiro-prerequisite")
        assert allowed.status == 200
        assert (await client.get("/assets/app.js")).status == 200
        # Method-scoped: shutdown is allowed as POST only.
        assert (await client.post("/api/shutdown")).status == 200
        assert (await client.get("/api/shutdown")).status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closed_gate_denies_an_unknown_route(enterprise: None) -> None:
    client = await _client(_gated_app(FakeLogin()))
    try:
        for path in ("/api/not-a-route", "/v1/models", "/feature-videos/x"):
            resp = await client.get(path)
            assert resp.status == 403, path
            assert (await resp.json())["code"] == "enterprise_gate_closed"
        resp = await client.post("/api/not-a-route")
        assert resp.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closed_gate_serves_only_the_static_shell_for_a_page(enterprise: None) -> None:
    client = await _client(_gated_app(FakeLogin()))
    try:
        resp = await client.get("/chat/some-session", headers={"Accept": "text/html"})
        assert resp.status == 200
        assert await resp.text() == "<html>shell</html>"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closed_gate_denies_a_websocket_upgrade(enterprise: None) -> None:
    client = await _client(_gated_app(FakeLogin()))
    try:
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect("/api/ws")
        assert exc.value.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_opens_expiry_closes_and_relogin_reopens(enterprise: None) -> None:
    login = FakeLogin(signed_in=False)
    app = _gated_app(login)
    client = await _client(app)
    watch: LoginWatch = app[gate_mw.LOGIN_WATCH_APP_KEY]
    gate: EnterpriseGate = app[gate_mw.GATE_APP_KEY]
    try:
        await watch.stop()  # drive checks by hand
        assert (await client.get("/api/status")).status == 403

        login.signed_in = True
        await watch.check()
        assert (await client.get("/api/status")).status == 200

        # Expiry seen by the latch (an ACP auth failure): the next request closes.
        login.signed_in = False
        assert (await client.get("/api/status")).status == 403
        assert not gate.is_open

        # Re-login: the watch's check reopens.
        login.signed_in = True
        assert (await client.get("/api/status")).status == 403  # sync never opens
        await watch.check()
        assert (await client.get("/api/status")).status == 200

        # Expiry seen by the background check.
        login.signed_in = False
        await watch.check()
        assert (await client.get("/api/status")).status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closing_the_gate_drops_an_open_websocket(enterprise: None) -> None:
    login = FakeLogin(signed_in=True)
    app = _gated_app(login)
    client = await _client(app)
    watch: LoginWatch = app[gate_mw.LOGIN_WATCH_APP_KEY]
    gate: EnterpriseGate = app[gate_mw.GATE_APP_KEY]
    try:
        await watch.stop()
        await watch.check()
        ws = await client.ws_connect("/api/ws")
        first = await asyncio.wait_for(ws.receive(), timeout=30)
        assert first.data == "hello"
        gate.close(REASON_LOGIN_REQUIRED)
        msg = await asyncio.wait_for(ws.receive(), timeout=30)
        assert msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        )
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closing_the_gate_drops_a_connection_after_its_handler_returned(
    enterprise: None,
) -> None:
    # aiohttp sends a returned response (a FileResponse download) after the
    # middleware has returned, so the connection must stay tracked past that.
    login = FakeLogin(signed_in=True)
    app = _gated_app(login)
    seen: list[Any] = []

    async def capture(request: web.Request) -> web.Response:
        seen.append(request.transport)
        return web.json_response({"data": "secret"})

    app.router.add_get("/api/download", capture)
    client = await _client(app, keep_alive=True)
    watch: LoginWatch = app[gate_mw.LOGIN_WATCH_APP_KEY]
    gate: EnterpriseGate = app[gate_mw.GATE_APP_KEY]
    closed = asyncio.Event()
    try:
        await watch.stop()
        await watch.check()
        assert (await client.get("/api/download")).status == 200
        (transport,) = seen
        assert not transport.is_closing()
        gate.add_listener(lambda _status: asyncio.get_running_loop().call_soon(closed.set))
        gate.close(REASON_LOGIN_REQUIRED)
        await asyncio.wait_for(closed.wait(), timeout=30)
        assert transport.is_closing()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closed_gate_allows_session_revocation(enterprise: None) -> None:
    app = _gated_app(FakeLogin())
    app.router.add_post("/api/auth/logout", _ok)
    app.router.add_post("/api/logout", _ok)
    client = await _client(app)
    try:
        assert (await client.post("/api/auth/logout")).status == 200
        assert (await client.post("/api/logout")).status == 200
    finally:
        await client.close()


def _status_app(
    monkeypatch: pytest.MonkeyPatch, login: FakeLogin, *, owner: bool
) -> web.Application:
    from kiro_crew.dashboard.handlers import kiro_prerequisite as prereq

    class FakeService:
        initial_setup_complete = True

        async def snapshot(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ready": False, "authenticated": False, "initial_setup_complete": True}

        async def update_cli(self, _caller: str = "") -> dict[str, Any]:
            return await self.snapshot()

        async def repair_agent_specs(self, _caller: str = "") -> dict[str, Any]:
            return await self.snapshot()

    monkeypatch.setattr(prereq, "_service", lambda _request: FakeService())
    monkeypatch.setattr(prereq, "_is_dashboard_owner", lambda _request: owner)

    @web.middleware
    async def as_dashboard_user(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[as_dashboard_user])
    _install(app, login)
    middleware = gate_mw.enterprise_gate_middleware_for(app, spa_shell_handler=None)
    assert middleware is not None
    app.middlewares.append(middleware)
    app.router.add_get("/api/kiro-prerequisite", prereq.api_kiro_prerequisite_status)
    app.router.add_post(
        "/api/kiro-prerequisite/update-cli", prereq.api_kiro_prerequisite_update_cli
    )
    app.router.add_post(
        "/api/kiro-prerequisite/repair-specs", prereq.api_kiro_prerequisite_repair_specs
    )
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/kiro-prerequisite/update-cli", "/api/kiro-prerequisite/repair-specs"]
)
async def test_closed_gate_marks_the_remedy_responses_locked_too(
    enterprise: None, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    # The dashboard replaces its cached status with each remedy's response, so
    # an undecorated one would reopen the app while the gateway still refuses it.
    login = FakeLogin(signed_in=False)
    app = _status_app(monkeypatch, login, owner=True)
    client = await _client(app)
    try:
        await app[gate_mw.LOGIN_WATCH_APP_KEY].stop()
        body = await (await client.post(path)).json()
        assert body["initial_setup_complete"] is False
        assert body["enterprise_login_required"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_closed_gate_status_asks_an_established_install_to_sign_in(
    enterprise: None, monkeypatch: pytest.MonkeyPatch, owner: bool
) -> None:
    login = FakeLogin(signed_in=False)
    app = _status_app(monkeypatch, login, owner=owner)
    client = await _client(app)
    watch: LoginWatch = app[gate_mw.LOGIN_WATCH_APP_KEY]
    try:
        await watch.stop()
        body = await (await client.get("/api/kiro-prerequisite")).json()
        assert body["initial_setup_complete"] is False
        assert body["enterprise_login_required"] is True

        login.signed_in = True
        await watch.check()
        body = await (await client.get("/api/kiro-prerequisite")).json()
        assert body["initial_setup_complete"] is True
        assert "enterprise_login_required" not in body
    finally:
        await client.close()


def test_login_screen_is_never_required_without_a_gate() -> None:
    assert gate_mw.login_screen_required(web.Application()) is False


# --------------------------------------------------------------------------- #
# oss: nothing changes


def test_oss_installs_no_gate_and_no_middleware() -> None:
    app = web.Application()
    startup, cleanup = len(app.on_startup), len(app.on_cleanup)
    gate_mw.install_enterprise_gate(app, FakeLogin())
    assert gate_mw.GATE_APP_KEY not in app
    assert (len(app.on_startup), len(app.on_cleanup)) == (startup, cleanup)
    assert gate_mw.enterprise_gate_middleware_for(app, spa_shell_handler=_shell) is None


def test_oss_chain_insert_is_a_no_op() -> None:
    from kiro_crew.dashboard import server

    app = web.Application()
    marker = object()
    app.middlewares.append(marker)  # type: ignore[arg-type]
    server._insert_enterprise_gate(app, spa_shell_handler=None)
    assert list(app.middlewares) == [marker]


def test_enterprise_without_an_installed_gate_refuses_to_build(enterprise: None) -> None:
    with pytest.raises(RuntimeError):
        gate_mw.enterprise_gate_middleware_for(web.Application(), spa_shell_handler=None)


def test_enterprise_chain_puts_the_gate_right_after_token_auth(enterprise: None) -> None:
    from kiro_crew.dashboard import server

    async def token_auth(request: web.Request, handler: Any) -> Any:
        return await handler(request)

    token_auth._is_token_auth = True  # type: ignore[attr-defined]
    before, after = object(), object()
    app = web.Application()
    gate_mw.install_enterprise_gate(app, FakeLogin(signed_in=True))
    app.middlewares.extend([before, token_auth, after])  # type: ignore[list-item]
    server._insert_enterprise_gate(app, spa_shell_handler=None)
    chain = list(app.middlewares)
    assert chain[0] is before and chain[1] is token_auth and chain[3] is after
    assert getattr(chain[2], "__name__", "") == "enterprise_gate_middleware"


# --------------------------------------------------------------------------- #
# Route enumeration: walk the real route tables and pin what stays open.

#: Every registered (method, route) the closed gate serves. A route added to the
#: allowlist, or a route registered on an allowlisted path, changes this set.
_EXPECTED_OPEN_ROUTES = {
    ("GET", "/"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/health"),
    ("GET", "/api/kiro-prerequisite"),
    ("GET", "/api/live"),
    ("GET", "/api/ready"),
    ("GET", "/api/theme/boot"),
    ("GET", "/api/token/local"),
    ("GET", "/favicon.ico"),
    ("GET", "/logo.png"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/kiro-prerequisite/repair-specs"),
    ("POST", "/api/kiro-prerequisite/update-cli"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/logout"),
    ("POST", "/api/shutdown"),
}


def _sample_path(canonical: str) -> str:
    return re.sub(r"\{[^}]*\}", "x", canonical)


def _route_table() -> list[tuple[str, str]]:
    app = web.Application()
    routes_pkg.register_all(app)
    from kiro_crew.dashboard import server

    server._register_mcp_routes(app)
    table: list[tuple[str, str]] = []
    for route in app.router.routes():
        if route.method in ("HEAD", "*"):
            continue
        table.append((route.method, route.resource.canonical))
    return table


def test_closed_gate_serves_exactly_the_pinned_routes() -> None:
    table = _route_table()
    assert len(table) > 300  # the walk saw the real tables, not an empty app
    open_routes = {
        (method, canonical)
        for method, canonical in table
        if gate_mw.allowed_while_closed(method, _sample_path(canonical))
    }
    assert open_routes == _EXPECTED_OPEN_ROUTES


def test_every_allowlisted_api_path_is_a_real_route() -> None:
    registered = {(method, canonical) for method, canonical in _route_table()}
    for path, methods in gate_mw.ALLOWED_WHILE_CLOSED.items():
        if not path.startswith("/api/"):
            continue
        for method in methods - {"HEAD"}:
            assert (method, path) in registered, (method, path)


def test_no_websocket_route_is_allowlisted() -> None:
    for method, canonical in _route_table():
        if canonical.endswith("/ws") or "/ws/" in canonical:
            assert not gate_mw.allowed_while_closed(method, _sample_path(canonical))
