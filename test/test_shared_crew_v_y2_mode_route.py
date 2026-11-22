"""``GET /api/shared-crew/mode`` is served by the gateway's own route table.

The app is built the way ``server.start_dashboard`` builds it: the real token
auth middleware and ``routes.register_all``. The browser treats an unreadable
mode route as an error, so a route the lane module defines but the gateway does
not mount would leave People and the session list on an error state.

With the Shared Crew preview off, the People and mode reads answer without
walking the session catalogue or asking the Tailscale daemon.
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
from kiro_crew.shared_crew import people, preview, routes_identity, tailscale_sharees
from kiro_crew.shared_crew.contracts import Principal, PrincipalKind, Provenance


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


async def _read_with_a_counted_walk(state, monkeypatch, path: str, *, preview_on: bool):
    """Read *path*; return the response body and how often the catalogue and the daemon were read."""
    if preview_on:
        monkeypatch.setenv(preview.PREVIEW_ENV, "1")
    else:
        monkeypatch.delenv(preview.PREVIEW_ENV, raising=False)
    preview.reset_for_tests()
    reads: list[str] = []

    async def walk(_request):
        reads.append("catalog")
        return []

    async def sharees():
        reads.append("daemon")
        return ()

    monkeypatch.setattr(routes_identity, "_catalog_rows", walk)
    monkeypatch.setattr(tailscale_sharees, "read_sharees", sharees)
    token = token_auth.generate_token("local-app")
    async with _serve(state) as client:
        resp = await client.get(path, params={"token": token})
        assert resp.status == 200, await resp.text()
        return await resp.json(), reads


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [routes_identity.PEOPLE_PATH, routes_identity.MODE_PATH])
async def test_with_the_preview_off_the_read_does_not_walk_the_catalog(state, monkeypatch, path):
    body, reads = await _read_with_a_counted_walk(state, monkeypatch, path, preview_on=False)
    assert reads == []
    if path == routes_identity.MODE_PATH:
        # No catalogue means no other creator, so the Crew reads as personal (not "in use").
        assert body["mode"] == "personal"
    else:
        assert body["people"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [routes_identity.PEOPLE_PATH, routes_identity.MODE_PATH])
async def test_with_the_preview_on_the_read_still_walks_the_catalog(state, monkeypatch, path):
    _body, reads = await _read_with_a_counted_walk(state, monkeypatch, path, preview_on=True)
    assert "catalog" in reads


def test_a_teammate_seen_by_one_test_is_seen_by_this_process():
    """Leave a teammate in the process-wide people registry, as a teammate request does.

    The next test in this file proves the per-test reset forgets them.
    """
    people.observe(
        Principal(
            kind=PrincipalKind.HUMAN,
            id="p_teammate_39456",
            provenance=Provenance.KIRO_RELAY,
        )
    )
    assert any(obs.principal.id == "p_teammate_39456" for obs in people.registry().snapshot())


@pytest.mark.asyncio
async def test_a_teammate_an_earlier_test_saw_does_not_make_this_crew_network(state, monkeypatch):
    """Runs after the test above on the same worker; the Crew still reads as personal."""
    assert people.registry().snapshot() == []
    body, _reads = await _read_with_a_counted_walk(
        state, monkeypatch, routes_identity.MODE_PATH, preview_on=False
    )
    assert body["mode"] == "personal"
