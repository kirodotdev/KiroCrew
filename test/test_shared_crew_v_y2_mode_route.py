"""``GET /api/shared-crew/mode`` is served by the gateway's own route table.

The app is built the way ``server.start_dashboard`` builds it: the real token
auth middleware and ``routes.register_all``. The browser treats an unreadable
mode route as an error, so a route the lane module defines but the gateway does
not mount would leave People and the session list on an error state.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.routes import register_all
from kiro_crew.shared_crew import routes_identity


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"shared-crew-mode-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    st.crons = None
    return st


@contextlib.asynccontextmanager
async def _serve(state):
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def test_gateway_route_table_mounts_the_mode_handler() -> None:
    app = web.Application()
    register_all(app)
    served = {
        (route.method, route.resource.canonical): route.handler
        for route in app.router.routes()
        if route.resource is not None
    }
    assert served.get(("GET", routes_identity.MODE_PATH)) is routes_identity.api_shared_crew_mode


@pytest.mark.asyncio
async def test_dashboard_user_reads_the_mode_on_the_gateway_app(state):
    token = token_auth.generate_token("local-app")
    async with _serve(state) as client:
        resp = await client.get(routes_identity.MODE_PATH, params={"token": token})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["mode"] in {"personal", "network", "console", "invalid"}
    assert body["crew_id"]
    assert body["team_config"]["state"] in {"not_configured", "valid", "invalid"}
