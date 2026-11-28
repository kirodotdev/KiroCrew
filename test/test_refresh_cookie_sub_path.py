"""The refresh cookie's path follows the sub-path in ``dashboard.url``.

Served at ``https://host/kirocrew`` with the proxy stripping ``/kirocrew``, the
browser calls ``/kirocrew/api/auth/refresh``; a cookie scoped to ``/api/auth``
is never sent there. ``dashboard.url`` is that public URL, so its path is the
prefix every refresh-cookie writer and clearer uses.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

from kiro_crew.dashboard import refresh_tokens as rt
from kiro_crew.dashboard import urls
from kiro_crew.dashboard.handlers import auth_refresh as h
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware

PORT = 7777
SUB_PATH_URL = "https://host.example/kirocrew"


def _mk(dashboard_url: str | None, *, cookies: str = "") -> web.Request:
    app = web.Application()
    app["port"] = PORT
    app["allowed_origins"] = {f"http://localhost:{PORT}"}
    if dashboard_url is not None:
        app["dashboard_url"] = dashboard_url
    headers = {"Host": f"localhost:{PORT}"}
    if cookies:
        headers["Cookie"] = cookies
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        ("127.0.0.1", 44444) if key == "peername" else default
    )
    return make_mocked_request(
        "POST", "/api/auth/refresh", headers=headers, app=app, transport=transport
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", ""),
        ("http://localhost:5476", ""),
        ("https://host.example/", ""),
        ("https://host.example/kirocrew", "/kirocrew"),
        ("https://host.example/kirocrew/", "/kirocrew"),
        ("https://host.example/kirocrew?x=1#y", "/kirocrew"),
        ("host.example:8443/team/kiro-crew_1.x", "/team/kiro-crew_1.x"),
        ("https://host.example/kiro crew", ""),
        ("https://host.example/kirocrew;Domain=evil", ""),
        ("https://host.example/a,b", ""),
        ("https://host.example/a/../b", ""),
        ("https://host.example/.", ""),
        ("https://host.example//kirocrew", ""),
        ("https://host.example/a//b", ""),
        ("https://host.example/%7Ekiro", ""),
        ("ftp://host.example/kirocrew", ""),
    ],
)
def test_dashboard_path_prefix_accepts_only_a_well_formed_path(url: str, expected: str) -> None:
    assert urls.dashboard_path_prefix(url) == expected


def test_refresh_cookie_is_set_and_cleared_under_the_sub_path() -> None:
    request = _mk(SUB_PATH_URL)
    name = rt.refresh_cookie_name(str(PORT))
    response = web.json_response({})
    h._set_refresh_cookie(response, request, "rtok", time.time() + 600)
    assert response.cookies[name]["path"] == "/kirocrew/api/auth"
    cleared = web.json_response({})
    h._clear_refresh_cookie(cleared, request)
    assert cleared.cookies[name]["path"] == "/kirocrew/api/auth"


@pytest.mark.parametrize("dashboard_url", [None, "", "http://localhost:5476/"])
def test_refresh_cookie_keeps_the_root_path_without_a_sub_path(dashboard_url: str | None) -> None:
    response = web.json_response({})
    h._set_refresh_cookie(response, _mk(dashboard_url), "rtok", time.time() + 600)
    assert response.cookies[rt.refresh_cookie_name(str(PORT))]["path"] == "/api/auth"


@pytest.mark.asyncio
async def test_refresh_and_logout_endpoints_scope_the_cookie_to_the_sub_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotation sets, and logout clears, the refresh cookie at ``/kirocrew/api/auth``."""
    monkeypatch.setattr(rt, "_state_singleton", rt.RefreshStateManager(tmp_path / "chains.json"))
    monkeypatch.setattr(h, "_refresh_rate_buckets", {})
    monkeypatch.setattr(h, "_audit", lambda *args, **kwargs: None)
    name = rt.refresh_cookie_name(str(PORT))
    token, chain_id, _jti, _exp = rt.generate_refresh_token("alice", family="fam")
    refreshed = await h.api_auth_refresh(_mk(SUB_PATH_URL, cookies=f"{name}={token}"))
    assert refreshed.status == 200
    assert refreshed.cookies[name]["path"] == "/kirocrew/api/auth"
    rotated = refreshed.cookies[name].value
    logged_out = await h.api_auth_logout(_mk(SUB_PATH_URL, cookies=f"{name}={rotated}"))
    assert logged_out.status == 200
    assert logged_out.cookies[name]["path"] == "/kirocrew/api/auth"
    assert rt._get_state().is_chain_revoked(chain_id)


def test_foreign_port_cleanup_expires_refresh_cookies_at_the_sub_path() -> None:
    padding = "p" * 3200
    cookies = f"mc_token_{PORT}={padding}; mc_token_9999={padding}; mc_refresh_9999=r"
    response = web.json_response({})
    h._expire_foreign_port_cookies(response, _mk(SUB_PATH_URL, cookies=cookies))
    expired = {morsel.key: morsel["path"] for morsel in response.cookies.values()}
    assert expired == {"mc_token_9999": "/", "mc_refresh_9999": "/kirocrew/api/auth"}


@pytest.mark.asyncio
async def test_token_exchange_mints_and_trims_refresh_cookies_at_the_sub_path() -> None:
    """The ``?token=`` sign-in mints this port's refresh cookie, and trims others', under it."""
    req = _make_request(
        path="/api/auth/me",
        query={"token": generate_token("alice")},
        cookies={"pad": "p" * 7000, "mc_token_9999": "t", "mc_refresh_9999": "r"},
        headers={"Host": f"localhost:{PORT}"},
    )
    app_get = req.app.get
    req.app.get = lambda key, *default: (
        SUB_PATH_URL if key == "dashboard_url" else app_get(key, *default)
    )
    resp = await token_auth_middleware()(req, _ok_handler)
    assert resp.status == 200
    assert resp.cookies[rt.refresh_cookie_name(str(PORT))]["path"] == "/kirocrew/api/auth"
    assert resp.cookies["mc_refresh_9999"]["path"] == "/kirocrew/api/auth"
    assert resp.cookies["mc_refresh_9999"]["max-age"] == "0"
    assert resp.cookies["mc_token_9999"]["path"] == "/"


@pytest.mark.asyncio
async def test_no_refresh_link_expires_the_refresh_cookie_at_the_sub_path() -> None:
    """A ``no_refresh`` link clears the browser's refresh cookie where it lives."""
    name = rt.refresh_cookie_name(str(PORT))
    req = _make_request(
        query={"token": generate_token("qruser", extra={"no_refresh": "1"})},
        cookies={name: "a-refresh-token-from-an-earlier-session"},
        headers={"Host": f"localhost:{PORT}"},
    )
    app_get = req.app.get
    req.app.get = lambda key, *default: (
        SUB_PATH_URL if key == "dashboard_url" else app_get(key, *default)
    )
    resp = await token_auth_middleware()(req, _ok_handler)
    assert resp.status == 200
    assert resp.cookies[name]["path"] == "/kirocrew/api/auth"
    assert resp.cookies[name]["max-age"] == "0"
