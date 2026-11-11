"""A relocated data home does not widen the dashboard's trusted browser origins.

``KIROCREW_HOME`` is set by every pod gateway, the dev scripts and anyone who
moves their data. It says where the data lives, nothing about who owns another
loopback port, and the ``SameSite=Lax`` session cookie reaches every port of the
host. So an origin it added could open the terminal WebSocket (a GET the CSRF
middleware never sees) and drive a shell under the operator's session. The Vite
dev server's ``:3000`` is trusted only through the explicit
``KIROCREW_ALLOWED_LOOPBACK_PORTS`` opt-in.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_request,
)

from kiro_crew.dashboard.handlers import terminal
from kiro_crew.dashboard.origin import build_allowed_origins, check_origin

_PORT = 7777
_DEV_PORT_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://[::1]:3000",
    "http://kirocrew.localhost:3000",
)


@pytest.fixture
def data_home_set(monkeypatch, tmp_path):
    """A gateway environment with a data home and no origin opt-in of any kind."""
    monkeypatch.setenv("KIROCREW_HOME", os.environ.get("KIROCREW_HOME") or str(tmp_path))
    monkeypatch.delenv("KIROCREW_ALLOWED_LOOPBACK_PORTS", raising=False)
    monkeypatch.delenv("KIROCREW_CORS_ORIGINS", raising=False)


def _request(origin: str, allowed: set[str], *, user="testuser"):
    req = _make_request(user=user, origin=origin)
    req.app["allowed_origins"] = allowed
    req.headers["Host"] = f"localhost:{_PORT}"
    return req


@pytest.mark.usefixtures("data_home_set")
class TestDataHomeAddsNoOrigin:
    def test_no_dev_port_origin_is_trusted(self) -> None:
        origins = build_allowed_origins(_PORT, local_only=True)
        assert not [o for o in origins if o.endswith(":3000")], origins
        assert f"http://localhost:{_PORT}" in origins

    @pytest.mark.parametrize("origin", _DEV_PORT_ORIGINS)
    def test_a_dev_port_page_fails_the_csrf_check(self, origin: str) -> None:
        allowed = build_allowed_origins(_PORT, local_only=True)
        assert check_origin(_request(origin, allowed), require=True) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", _DEV_PORT_ORIGINS)
    async def test_a_dev_port_page_cannot_open_the_terminal(self, origin: str) -> None:
        allowed = build_allowed_origins(_PORT, local_only=True)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            with pytest.raises(web.HTTPForbidden):
                await terminal.api_terminal_ws(_request(origin, allowed))

    @pytest.mark.asyncio
    async def test_the_dashboard_origin_still_reaches_the_terminal_gates(self) -> None:
        """The own origin passes the origin gate and stops at authentication."""
        allowed = build_allowed_origins(_PORT, local_only=True)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(
                _request(f"http://localhost:{_PORT}", allowed, user=None)
            )
        assert resp.status == 401


@pytest.mark.usefixtures("data_home_set")
def test_the_dev_server_port_is_trusted_when_opted_in(monkeypatch) -> None:
    """The documented frontend dev flow: the operator names :3000 explicitly."""
    monkeypatch.setenv("KIROCREW_ALLOWED_LOOPBACK_PORTS", "3000")
    origins = build_allowed_origins(_PORT, local_only=True)
    for origin in _DEV_PORT_ORIGINS:
        assert origin in origins
