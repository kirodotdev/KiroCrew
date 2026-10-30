"""A client-sent ``x-kiro-relay-*`` identity header never reaches a handler or an app.

The relay identity header is written only by an in-process relay client, so a copy
that arrives over a socket is dropped at the front of both middleware chains and,
independently, by the app-backend proxy. The boot tests drive the real
``start_dashboard`` and ``start_api_server`` apps through their own runners, which
every listener of that gateway serves (TCP, Unix socket, second loopback family and
the loopback port ``tailscale serve`` forwards to), and read what the
``/api/health`` handler was handed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
from kiro_crew.apps.routes import register_app_routes
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard import server as srv
from kiro_crew.dashboard.relay_headers import (
    RELAY_HEADER_PREFIX,
    is_relay_header,
    relay_header_strip_middleware,
    strip_client_relay_headers,
)

#: Every spelling a client might send. Header names are case-insensitive on the
#: wire, so each must be dropped, and the prefix covers names beyond ``-user``.
_FORGED = (
    ("X-Kiro-Relay-User", "forged-a"),
    ("x-kiro-relay-user", "forged-b"),
    ("X-KIRO-RELAY-USER", "forged-c"),
    ("x-kiro-relay-foo", "forged-d"),
)

#: The forwarded shape ``tailscale serve`` presents on the loopback listener.
_SERVE_FORWARDED = {
    "X-Forwarded-For": "100.64.0.5",
    "Tailscale-User-Login": "alice@example.com",
    "Tailscale-User-Name": "Alice A",
}


def _relay_names(headers: Any) -> list[str]:
    return [name for name in headers if name.lower().startswith(RELAY_HEADER_PREFIX)]


def _raw_relay_names(raw_headers: Any) -> list[str]:
    return [
        name.decode("latin-1")
        for name, _value in raw_headers
        if name.decode("latin-1").lower().startswith(RELAY_HEADER_PREFIX)
    ]


def _forged_headers() -> list[tuple[str, str]]:
    return [*_FORGED, ("X-Kept", "kept")]


# -- the strip itself ------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("x-kiro-relay-user", True),
        ("X-Kiro-Relay-User", True),
        ("X-KIRO-RELAY-FOO", True),
        ("x-kiro-relay", False),
        ("x-kirocrew-principal", False),
        ("x-relay-user", False),
    ],
)
def test_a_relay_header_is_recognised_by_its_prefix_in_any_case(name: str, expected: bool) -> None:
    assert is_relay_header(name) is expected


def test_every_relay_header_is_dropped_and_every_other_header_kept_in_order() -> None:
    request = make_mocked_request(
        "GET",
        "/x",
        headers=[("X-First", "1"), *_FORGED, ("X-First", "2"), ("X-Last", "3")],
    )
    stripped = strip_client_relay_headers(request)
    assert stripped is not request
    assert _relay_names(stripped.headers) == []
    assert _raw_relay_names(stripped.raw_headers) == []
    assert list(stripped.headers.items()) == [("X-First", "1"), ("X-First", "2"), ("X-Last", "3")]


def test_a_request_without_a_relay_header_is_handed_on_unchanged() -> None:
    request = make_mocked_request("GET", "/x", headers={"X-Kiro-Crew": "1"})
    assert strip_client_relay_headers(request) is request


@pytest.mark.asyncio
async def test_the_middleware_hands_the_handler_a_request_with_no_relay_header() -> None:
    seen: list[web.Request] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request)
        return web.Response(text="ok")

    request = make_mocked_request("GET", "/x", headers=_forged_headers())
    resp = await relay_header_strip_middleware(request, handler)
    assert resp.status == 200
    assert len(seen) == 1
    assert _relay_names(seen[0].headers) == []
    assert seen[0].headers["X-Kept"] == "kept"


@pytest.mark.asyncio
async def test_a_relay_header_beside_an_unencodable_header_is_refused_not_served() -> None:
    """A clone re-encodes every header as UTF-8, which a raw ``0xff`` byte cannot be."""

    reached: list[web.Request] = []

    async def handler(request: web.Request) -> web.Response:
        reached.append(request)
        return web.Response(text="ok")

    app = web.Application(middlewares=[relay_header_strip_middleware])
    app.router.add_get("/x", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        try:
            writer.write(
                b"GET /x HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Kiro-Relay-User: forged\r\n"
                b"X-Other: \xff\xfe\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=30)
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await server.close()
    assert response.startswith(b"HTTP/1.1 400 ")
    assert reached == []


# -- every listener of both gateways --------------------------------------- #


def _probe_health(monkeypatch: pytest.MonkeyPatch) -> list[web.Request]:
    """Route ``/api/health`` on the next boot to a probe that keeps each request."""

    seen: list[web.Request] = []

    async def probe(request: web.Request) -> web.Response:
        seen.append(request)
        return web.json_response({"status": "ok"})

    monkeypatch.setattr(handlers, "api_health", probe)
    return seen


async def _assert_stripped(client: TestClient, seen: list[web.Request]) -> None:
    for extra in ({}, _SERVE_FORWARDED):
        del seen[:]
        resp = await client.get("/api/health", headers=[*_forged_headers(), *extra.items()])
        assert resp.status == 200, await resp.text()
        assert len(seen) == 1
        assert _relay_names(seen[0].headers) == []
        assert _raw_relay_names(seen[0].raw_headers) == []
        assert seen[0].headers["X-Kept"] == "kept"


@pytest.mark.asyncio
async def test_the_dashboard_gateway_never_hands_a_handler_a_relay_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    seen = _probe_health(monkeypatch)
    async with harness._dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        async with TestClient(harness._RunningAppServer(runner)) as client:
            await _assert_stripped(client, seen)


@pytest.mark.asyncio
async def test_the_headless_gateway_never_hands_a_handler_a_relay_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_coverage as api_harness
    import test_dashboard_server_startup_coverage as harness

    monkeypatch.setattr(srv, "_start_unix_site", AsyncMock(return_value=None))
    monkeypatch.setattr(srv, "_start_secondary_loopback_site", AsyncMock(return_value=None))
    seen = _probe_health(monkeypatch)
    runner, _state = await api_harness._start_api(tmp_path, monkeypatch)
    try:
        async with TestClient(harness._RunningAppServer(runner)) as client:
            await _assert_stripped(client, seen)
    finally:
        await runner.cleanup()
        await harness._cancel_stray_tasks()


# -- the app-backend proxy -------------------------------------------------- #


class TestAppProxyNeverForwardsARelayHeader:
    """The proxy drops relay headers itself, with no strip middleware in front of it."""

    _SECRET = "test-secret-relay"

    @pytest.fixture(autouse=True)
    def _proxy_env(self, tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "kirocrew-home"
        app_dir = home / "apps" / "proxy-app"
        app_dir.mkdir(parents=True)
        _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
        (app_dir / ".app_secret").write_text(self._SECRET)
        (app_dir / "installed.json").write_text(
            json.dumps(
                {
                    "name": "proxy-app",
                    "version": "1.0.0",
                    "displayName": "Proxy App",
                    "enabled": True,
                    "origin": "local",
                    "resources": "gateway",
                    "lifecycle": "gateway",
                    "schemaVersion": 2,
                }
            )
        )
        (app_dir / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": "proxy-app",
                    "version": "1.0.0",
                    "displayName": "Proxy App",
                    "description": "For proxy testing",
                    "author": "tester",
                }
            )
        )
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as bridges_mod
        from kiro_crew.apps.routes import _app_secret_cache

        kiro_agents = tmp_path / "kiro-agents"
        kiro_agents.mkdir()
        _floor_monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
        bmod._processes.clear()
        bmod._allocated_ports.clear()
        _app_secret_cache.clear()

    @pytest.mark.asyncio
    async def test_a_relay_header_in_any_case_never_reaches_the_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.apps.routes as routes

        received: list[tuple[str, str]] = []

        async def capture(request: web.Request) -> web.Response:
            received.extend((k.decode(), v.decode()) for k, v in request.raw_headers)
            await request.read()
            return web.json_response({"ok": True})

        backend = web.Application()
        backend.router.add_route("*", "/{path:.*}", capture)
        backend_runner = web.AppRunner(backend)
        await backend_runner.setup()
        site = web.TCPSite(backend_runner, "127.0.0.1", 0)
        await site.start()
        port = backend_runner.addresses[0][1]
        monkeypatch.setattr(
            routes, "_resolve_app_backend_url", lambda _name: f"http://127.0.0.1:{port}"
        )

        @web.middleware
        async def stamp(request: web.Request, handler: Any) -> web.StreamResponse:
            request["user"] = "owner"
            request["app"] = ""
            request["is_dashboard_user"] = True
            request["auth_from_query_token"] = False
            return await handler(request)

        gateway = web.Application(middlewares=[stamp])
        gateway["state"] = SimpleNamespace(owner_id="owner")
        register_app_routes(gateway)
        try:
            async with TestClient(TestServer(gateway)) as client:
                resp = await client.get("/apps/proxy-app/api/probe", headers=_forged_headers())
                assert resp.status == 200, await resp.text()
        finally:
            await backend_runner.cleanup()
        assert [name for name, _ in received if is_relay_header(name)] == []
        assert ("X-Kept", "kept") in received
