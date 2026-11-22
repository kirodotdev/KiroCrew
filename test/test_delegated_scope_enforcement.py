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
        resp = await client.get("/api/version", params=_delegated())
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
        assert (await client.get("/api/version", params=_delegated())).status == 200
        assert (await client.get("/api/version")).status == 200
        granted["on"] = False
        resp = await client.get("/api/version")
        assert resp.status == 403
        assert (await resp.json())["code"] == _REFUSED_CODE


class _SelRecorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log_api_access(self, **row) -> None:
        self.rows.append(row)


@pytest.fixture
def sel_rows(monkeypatch) -> list[dict]:
    rec = _SelRecorder()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: rec)
    return rec.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/apps/other-app/api"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a_delegated_token_cannot_reach_another_apps_backend(
    state, granted, sel_rows, prefix, method
):
    async with _serve(state) as client:
        resp = await client.request(method, f"{prefix}/x", params=_delegated(), json={})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == _REFUSED_CODE
    assert [r["caller"] for r in sel_rows] == [_APP]
    assert sel_rows[0]["outcome"] == "denied"
    assert "other-app" in sel_rows[0]["resources"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [f"/apps/{_APP}/api"])
async def test_a_delegated_token_still_reaches_its_own_apps_route(state, granted, sel_rows, prefix):
    async with _serve(state) as client:
        resp = await client.get(f"{prefix}/x", params=_delegated())
        body = await resp.text()
        assert "delegated_credential_refused" not in body, body
    assert sel_rows == []


def test_a_delegated_request_never_carries_the_owner_session_principal():
    from kiro_crew.apps.proxy_auth import PRINCIPAL_NONE, PRINCIPAL_OWNER_SESSION
    from kiro_crew.apps.routes_proxy import _proxy_principal

    class _Req(dict):
        pass

    owner = _Req(is_dashboard_user=True, auth_from_query_token=False, user="owner")
    delegated = _Req(owner, via_app=_APP)
    from kiro_crew.dashboard.handlers import source_providers

    orig = source_providers.is_owner_dashboard_request
    source_providers.is_owner_dashboard_request = lambda _r: True
    try:
        assert _proxy_principal(owner)[0] == PRINCIPAL_OWNER_SESSION
        assert _proxy_principal(delegated) == (PRINCIPAL_NONE, "")
    finally:
        source_providers.is_owner_dashboard_request = orig


def test_a_deny_star_row_stays_deny_for_every_concrete_method():
    from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS
    from kiro_crew.dashboard.delegated_scope import route_tag

    stars = [c for c, t in ROUTE_TAGS.items() if t.get("*") == "deny"]
    assert stars
    for canonical in stars:
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            assert route_tag(method, canonical) != "allow", f"{method} {canonical}"


class _Match(dict):
    def __init__(self, canonical: str, name: str) -> None:
        super().__init__(name=name)
        self.route = type("R", (), {"resource": type("Res", (), {"canonical": canonical})()})()


class _FakeReq(dict):
    def __init__(self, canonical: str, name: str) -> None:
        super().__init__(auth_token="t")
        self.method = "GET" if canonical.startswith("/app-frame/") else "POST"
        self.match_info = _Match(canonical, name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical",
    [
        "/apps/{name}/api/{path}",
        "/app-frame-fetch/{name}/{path}",
        "/app-frame/{name}/{point}/{point_id}/{frame_id}/{token}",
    ],
)
async def test_enforce_delegated_allows_only_the_tokens_own_app_on_the_app_routes(
    granted, sel_rows, monkeypatch, canonical
):
    # /app-frame/ and /app-frame-fetch/ skip token auth today (the path token or the
    # capability is the credential), so the scope check is driven directly: it must hold
    # if either route ever sees a delegated token.
    from kiro_crew.dashboard import token_auth_tokens
    from kiro_crew.dashboard.delegated_scope import enforce_delegated

    monkeypatch.setattr(token_auth_tokens, "token_via_app_unverified", lambda _t: _APP)
    other = await enforce_delegated(_FakeReq(canonical, "other-app"), "/x")
    assert other is not None and other.status == 403
    assert len(sel_rows) == 1 and "other-app" in sel_rows[0]["resources"]
    own = _FakeReq(canonical, _APP)
    assert await enforce_delegated(own, "/x") is None
    assert own["via_app"] == _APP
