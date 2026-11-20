"""The refresh cookie's path follows a prefix-stripping proxy's sub-path.

Served at ``https://host/kirocrew`` with the proxy stripping ``/kirocrew``, the
browser calls ``/kirocrew/api/auth/refresh``; a cookie scoped to ``/api/auth``
is never sent there. The gateway reads the stripped prefix from
``X-Forwarded-Prefix``, from a loopback peer only.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import refresh_tokens as rt
from kiro_crew.dashboard.handlers import auth_refresh as h
from kiro_crew.dashboard.origin import public_path_prefix

PORT = 7777


def _mk(prefix: str | None, *, remote: str = "127.0.0.1", cookies: str = "") -> web.Request:
    app = web.Application()
    app["port"] = PORT
    app["allowed_origins"] = {f"http://localhost:{PORT}"}
    headers = {"Host": f"localhost:{PORT}"}
    if prefix is not None:
        headers["X-Forwarded-Prefix"] = prefix
    if cookies:
        headers["Cookie"] = cookies
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        (remote, 44444) if key == "peername" else default
    )
    return make_mocked_request(
        "POST", "/api/auth/refresh", headers=headers, app=app, transport=transport
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, ""),
        ("", ""),
        ("/", ""),
        ("/kirocrew", "/kirocrew"),
        ("/kirocrew/", "/kirocrew"),
        ("/team/kiro-crew_1.x", "/team/kiro-crew_1.x"),
        ("kirocrew", ""),
        ("/kiro crew", ""),
        ("/kirocrew;Domain=evil", ""),
        ("/a,/b", ""),
        ("/a/../b", ""),
        ("/.", ""),
        ("//kirocrew", ""),
        ("/" + "a" * 300, ""),
    ],
)
def test_public_path_prefix_accepts_only_a_well_formed_path(
    header: str | None, expected: str
) -> None:
    assert public_path_prefix(_mk(header)) == expected


def test_public_path_prefix_ignores_the_header_from_a_remote_peer() -> None:
    assert public_path_prefix(_mk("/kirocrew", remote="203.0.113.9")) == ""


def test_refresh_cookie_is_set_and_cleared_under_the_proxy_prefix() -> None:
    request = _mk("/kirocrew")
    name = rt.refresh_cookie_name(str(PORT))
    response = web.json_response({})
    h._set_refresh_cookie(response, request, "rtok", time.time() + 600)
    assert response.cookies[name]["path"] == "/kirocrew/api/auth"
    cleared = web.json_response({})
    h._clear_refresh_cookie(cleared, request)
    assert cleared.cookies[name]["path"] == "/kirocrew/api/auth"


def test_refresh_cookie_keeps_the_root_path_without_a_prefix() -> None:
    response = web.json_response({})
    h._set_refresh_cookie(response, _mk(None), "rtok", time.time() + 600)
    assert response.cookies[rt.refresh_cookie_name(str(PORT))]["path"] == "/api/auth"


@pytest.mark.asyncio
async def test_refresh_and_logout_endpoints_scope_the_cookie_to_the_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotation sets, and logout clears, the refresh cookie at ``/kirocrew/api/auth``."""
    monkeypatch.setattr(rt, "_state_singleton", rt.RefreshStateManager(tmp_path / "chains.json"))
    monkeypatch.setattr(h, "_refresh_rate_buckets", {})
    monkeypatch.setattr(h, "_audit", lambda *args, **kwargs: None)
    name = rt.refresh_cookie_name(str(PORT))
    token, chain_id, _jti, _exp = rt.generate_refresh_token("alice")
    refreshed = await h.api_auth_refresh(_mk("/kirocrew", cookies=f"{name}={token}"))
    assert refreshed.status == 200
    assert refreshed.cookies[name]["path"] == "/kirocrew/api/auth"
    rotated = refreshed.cookies[name].value
    logged_out = await h.api_auth_logout(_mk("/kirocrew", cookies=f"{name}={rotated}"))
    assert logged_out.status == 200
    assert logged_out.cookies[name]["path"] == "/kirocrew/api/auth"
    assert rt._get_state().is_chain_revoked(chain_id)


def test_foreign_port_cleanup_expires_refresh_cookies_at_the_prefixed_path() -> None:
    padding = "p" * 3200
    cookies = f"mc_token_{PORT}={padding}; mc_token_9999={padding}; mc_refresh_9999=r"
    response = web.json_response({})
    h._expire_foreign_port_cookies(response, _mk("/kirocrew", cookies=cookies))
    expired = {morsel.key: morsel["path"] for morsel in response.cookies.values()}
    assert expired == {"mc_token_9999": "/", "mc_refresh_9999": "/kirocrew/api/auth"}
