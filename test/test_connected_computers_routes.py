"""Connected computers: registering the routes loads none of the feature's code.

Every gateway registers the connected-computers routes, with the preview off as
well, so registration must not put the connector socket, the broker or the
credential code on the boot path. The handlers import them on first request.
The import check runs in a fresh interpreter: in this warm test process those
modules are already loaded by the other tests.

With the preview off the connector path must meet the dashboard gate and the
router exactly as a path no route serves (``TestTokenGate`` here; the router half
is in ``test_connected_computers_remote_routes.py``).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.config import live
from kiro_crew.connected_computers import flag, remote_routes, ws_route
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.dashboard.token_auth_paths import (
    _BYPASS_EXACT,
    _BYPASS_EXACT_METHODS,
    CONNECTOR_CONNECT_PATH,
    _exact_method_bypass,
)
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: What a request loads, and registration must not.
_LOADED_ON_FIRST_REQUEST = (
    "kiro_crew.connected_computers.ws_route",
    "kiro_crew.connected_computers.broker",
    "kiro_crew.connected_computers.connector_token",
    "kiro_crew.connected_computers.devices",
    "kiro_crew.connected_computers.identity",
    "kiro_crew.connected_computers.vault_doc",
    "kiro_crew.connected_computers.authenticated",
    "kiro_crew.connected_computers.protocol",
    "kiro_crew.remote_session.link",
)

_PROBE = textwrap.dedent("""
    import sys
    from aiohttp import web
    # The dashboard gate is loaded before any route on a real boot; whatever it
    # loads is not this feature's doing.
    import kiro_crew.dashboard.token_auth_paths  # noqa: F401
    before = set(sys.modules)
    from kiro_crew.connected_computers.remote_routes import register_all

    app = web.Application()
    register_all(app)
    paths = sorted(r.canonical for r in app.router.resources())
    assert paths == sorted(sys.argv[1].split(",")), paths
    assert "kiro_crew.connected_computers.ws_route" not in sys.modules
    loaded = [m for m in sys.argv[2].split(",") if m in sys.modules and m not in before]
    assert not loaded, f"registration imported {loaded}"
    """)


def _expected_paths() -> list[str]:
    devices = remote_routes.DEVICES_PATH
    return [
        CONNECTOR_CONNECT_PATH,
        devices,
        devices + "/{device_id}/revoke",
        devices + "/{device_id}/remove",
    ]


def test_registering_the_routes_imports_no_connector_code() -> None:
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            ",".join(_expected_paths()),
            ",".join(_LOADED_ON_FIRST_REQUEST),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert res.returncode == 0, res.stderr


def test_the_socket_path_is_the_one_the_dashboard_gate_lets_through() -> None:
    assert ws_route.CONNECT_PATH == CONNECTOR_CONNECT_PATH


def _previews(monkeypatch, value: object) -> None:
    previews = {} if value is None else {flag.PREVIEW_NAME: value}
    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))
    monkeypatch.setattr(live, "snapshot", lambda: cfg)


@pytest.mark.asyncio
async def test_the_connect_route_hands_the_request_to_the_socket_handler(monkeypatch) -> None:
    _previews(monkeypatch, True)
    seen: list[object] = []
    answer = web.Response(status=503)

    async def fake_connect(request: object) -> web.Response:
        seen.append(request)
        return answer

    monkeypatch.setattr(ws_route, "api_connector_connect", fake_connect)
    request = SimpleNamespace(method="GET")
    assert await remote_routes.api_connector_connect(request) is answer  # type: ignore[arg-type]
    assert seen == [request]


@pytest.mark.asyncio
async def test_with_the_preview_off_the_socket_handler_is_never_reached(monkeypatch) -> None:
    _previews(monkeypatch, None)

    async def fake_connect(request: object) -> web.Response:
        raise AssertionError("reached the socket handler with the preview off")

    monkeypatch.setattr(ws_route, "api_connector_connect", fake_connect)
    with pytest.raises(web.HTTPNotFound):
        await remote_routes.api_connector_connect(SimpleNamespace(method="GET"))  # type: ignore[arg-type]


class TestTokenGate:
    """The socket skips the dashboard gate only while its preview is on.

    Driven through the real middleware with no credential: with the preview off
    the connector path is refused exactly as a path no route serves.
    """

    _UNSERVED = "/api/connected-computers/not-a-route"

    @staticmethod
    async def _handler(request: web.Request) -> web.Response:
        return web.Response(text="reached")

    @staticmethod
    def _request(method: str, path: str) -> Any:
        req = MagicMock(spec=web.Request)
        req.path = path
        req.query = {}
        req.cookies = {}
        req.remote = "203.0.113.9"
        req.headers = {}
        req.method = method
        return req

    async def _answer(self, method: str, path: str) -> tuple[int, str, str]:
        resp = await token_auth_middleware()(self._request(method, path), self._handler)
        return resp.status, resp.text, resp.headers.get("X-Auth-Required", "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [None, False, "true", 1])
    @pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
    async def test_off_is_refused_like_an_unserved_path(self, monkeypatch, value, method) -> None:
        _previews(monkeypatch, value)
        unserved = await self._answer(method, self._UNSERVED)
        assert unserved[0] == 403
        assert await self._answer(method, CONNECTOR_CONNECT_PATH) == unserved

    @pytest.mark.asyncio
    async def test_no_live_configuration_is_off(self, monkeypatch) -> None:
        monkeypatch.setattr(live, "snapshot", lambda: None)
        assert not _exact_method_bypass(CONNECTOR_CONNECT_PATH, "GET")
        assert (await self._answer("GET", CONNECTOR_CONNECT_PATH))[0] == 403

    @pytest.mark.asyncio
    async def test_on_lets_only_get_through(self, monkeypatch) -> None:
        _previews(monkeypatch, True)
        assert await self._answer("GET", CONNECTOR_CONNECT_PATH) == (200, "reached", "")
        for method in ("HEAD", "POST", "PUT", "DELETE"):
            status, text, _ = await self._answer(method, CONNECTOR_CONNECT_PATH)
            assert (status, text) != (200, "reached"), method

    def test_the_static_table_never_names_the_socket(self) -> None:
        assert CONNECTOR_CONNECT_PATH not in _BYPASS_EXACT_METHODS
        assert CONNECTOR_CONNECT_PATH not in _BYPASS_EXACT
