"""One mixed-internal cookie poll writes one api_access row.

A browser cookie request on a mixed-internal path (``/api/workflows``) must not
emit a redundant ``internal_auth``/``granted`` ``api_access`` row on top of the
dashboard-layer handler's own audit. The cookie caller is a dashboard user; the
``_log_auth`` auth-log stream still records the grant, and every genuinely
distinct decision (denial, internal-secret grant, scope check, peer bind) still
logs an ``api_access`` row.
"""

from __future__ import annotations

import pytest
from aiohttp import web

import kiro_crew.dashboard.token_auth as ta
from kiro_crew.dashboard.token_auth import (
    bind_token_ip,
    generate_token,
    mark_consumed,
    token_auth_middleware,
)


@pytest.fixture(autouse=True)
def _clear(tmp_path, monkeypatch):
    import kiro_crew.dashboard.revocation_gen as _rg

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(_rg, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    ta._state.clear_all()
    ta._app_perms_cache.clear()
    yield
    ta._state.clear_all()
    ta._app_perms_cache.clear()


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _make_request(path, cookies, remote="127.0.0.1", headers=None, method="GET"):
    from unittest.mock import MagicMock

    req = MagicMock(spec=web.Request)
    req.path = path
    req.query = {}
    req.cookies = cookies or {}
    req.remote = remote
    req.headers = headers or {}
    req.method = method
    return req


def _capture_sel(monkeypatch):
    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(ta, "_sel_fn", lambda: _FakeSel())
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", ["127.0.0.1", "10.0.0.1"])
async def test_mixed_internal_cookie_poll_emits_one_api_access_row(monkeypatch, remote):
    """A cookie poll of a mixed-internal path writes exactly ONE api_access row.

    Both the loopback (``127.0.0.1``) and the DCV/SSH-forwarded
    (``10.0.0.1``) cookie-auth success branches must collapse to the single
    dashboard-layer ``_log_auth`` row — no redundant ``internal_auth``/
    ``granted``/``source=token_auth`` row stacked on top.
    """
    calls = _capture_sel(monkeypatch)
    token = generate_token("owner", ttl_seconds=300)
    bind_token_ip(token, remote)
    mark_consumed(token)

    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/workflows"}),
        internal_secret="s",
        local_only=False,
    )
    req = _make_request("/api/workflows/runs", cookies={"mc_token_5476": token}, remote=remote)
    resp = await mw(req, _ok_handler)
    assert resp.status == 200

    rows = [c for c in calls if c.get("resources") == "/api/workflows/runs"]
    assert len(rows) == 1, f"one request must write one api_access row; got {rows}"
    # The surviving row is the dashboard-layer auth audit, not the redundant
    # token-auth-layer row.
    assert rows[0]["operation"] == "dashboard.token_auth"
    assert rows[0].get("source") != "token_auth"
    # The surviving row carries the resolved caller identity, so it is a true
    # superset of the suppressed row — the daemon-verified login (here the
    # token subject ``owner`` with no peer) reaches the audit trail rather than
    # the ``"internal"`` literal.
    assert rows[0]["caller"] == "owner", rows[0]
    assert not [
        c
        for c in calls
        if c.get("source") == "token_auth" and c.get("operation") == "internal_auth"
    ], "the redundant internal_auth/granted token_auth row must be gone"


@pytest.mark.asyncio
async def test_mixed_internal_cookie_denial_still_logs(monkeypatch):
    """A failed cookie on a mixed-internal path still writes a denial api_access row."""
    calls = _capture_sel(monkeypatch)
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/workflows"}),
        internal_secret="s",
        local_only=False,
    )
    req = _make_request("/api/workflows/runs", cookies={"mc_token_5476": "garbage"})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    denials = [c for c in calls if c.get("outcome") == "denied"]
    assert denials, "a cookie-auth denial on a mixed-internal path must still log"


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", ["127.0.0.1", "10.0.0.1"])
async def test_mixed_internal_app_token_keeps_layer_row(monkeypatch, remote):
    """An app token's scoped grant on a mixed path keeps its token_auth row.

    The dedup narrows to the dashboard user: an app token reaching a mixed
    route is a distinct security decision, so its per-layer
    ``source=token_auth`` row survives where the two decisions genuinely
    differ.
    """
    monkeypatch.setattr(ta, "app_token_path_allowed", lambda app_name, path: True)
    calls = _capture_sel(monkeypatch)
    token = generate_token("someapp", ttl_seconds=300, app="someapp")
    bind_token_ip(token, remote)
    mark_consumed(token)

    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/workflows"}),
        internal_secret="s",
        local_only=False,
    )
    req = _make_request("/api/workflows/runs", cookies={"mc_token_5476": token}, remote=remote)
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    layer_rows = [
        c
        for c in calls
        if c.get("source") == "token_auth" and c.get("operation") == "internal_auth"
    ]
    assert layer_rows, "an app token's mixed-path grant must keep its token_auth row"
