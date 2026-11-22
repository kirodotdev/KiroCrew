"""A ``via_app`` token reaches the dashboard, minus credential and control-plane routes.

Real ``token_auth_middleware`` and the real route table (``register_all``). The token is
an owner token carrying ``via_app=<app>`` (never the ``app`` claim). Every request
re-reads the owner-enable marker and the grant for that app, so a disable ends the token
at once; a route with no delegated tag is refused.
"""

from __future__ import annotations

import contextlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.routes import register_all

_APP = "tunnel-like"
_REFUSED_CODE = "delegated_credential_refused"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"delegated-scope-test-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    st = _make_state(tmp_path)
    st.owner_id = ""
    return st


@pytest.fixture
def granted(monkeypatch) -> dict:
    from kiro_crew.apps import dashboard_proxy

    flag = {"on": True}
    monkeypatch.setattr(dashboard_proxy, "granted", lambda name: flag["on"] and name == _APP)
    return flag


@contextlib.asynccontextmanager
async def _serve(state, *extra_routes):
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    for method, path in extra_routes:

        async def _ok(_request):
            return web.json_response({"ok": True})

        app.router.add_route(method, path, _ok)
    async with TestClient(TestServer(app)) as client:
        yield client


def _delegated() -> dict[str, str]:
    from kiro_crew.dashboard.token_auth_tokens import owner_mint_extra

    token = token_auth.generate_token("local-app", extra=owner_mint_extra({"via_app": _APP}))
    return {"token": token}


@pytest.mark.asyncio
async def test_a_plain_dashboard_read_is_allowed(state, granted):
    async with _serve(state) as client:
        resp = await client.get("/api/status", params=_delegated())
        assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/auth/mobile-link"),
        ("POST", "/api/tailnet/mobile/qr"),
        ("POST", "/api/webhooks/tokens"),
        ("POST", "/api/apps/register"),
        ("POST", "/api/apps/install"),
        ("POST", f"/api/apps/{_APP}/enable"),
        ("POST", f"/api/apps/{_APP}/disable"),
        ("PUT", f"/api/apps/{_APP}/bot-grants"),
        ("PUT", "/api/config/kirocrew"),
        ("POST", "/api/security/tool-grants"),
        ("POST", "/api/chat/mode"),
        ("POST", "/api/restart"),
        ("POST", "/api/update/approve"),
        ("GET", "/v1/crew/owner/approvals"),
        ("POST", "/v1/crew/owner/grant-requests/abc/decision"),
    ],
)
async def test_each_refused_route_answers_delegated_credential_refused(
    state, granted, method, path
):
    async with _serve(state) as client:
        resp = await client.request(method, path, params=_delegated(), json={})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == _REFUSED_CODE


@pytest.mark.asyncio
async def test_a_route_with_no_tag_is_refused_to_a_delegated_token_only(state, granted):
    async with _serve(state, ("GET", "/api/zz-untagged")) as client:
        resp = await client.get("/api/zz-untagged", params=_delegated())
        assert resp.status == 403
        assert (await resp.json())["code"] == _REFUSED_CODE
        plain = {"token": token_auth.generate_token("local-app")}
        assert (await client.get("/api/zz-untagged", params=plain)).status == 200


@pytest.mark.asyncio
async def test_the_token_ends_the_moment_the_owner_disables_the_app(state, granted):
    async with _serve(state) as client:
        # The first request exchanges the link for the session cookie the client keeps.
        assert (await client.get("/api/status", params=_delegated())).status == 200
        assert (await client.get("/api/status")).status == 200
        granted["on"] = False
        resp = await client.get("/api/status")
        assert resp.status == 403
        assert (await resp.json())["code"] == _REFUSED_CODE
