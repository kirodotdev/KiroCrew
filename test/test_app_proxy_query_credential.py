"""The app proxy never forwards the gateway's ``?token=`` credential to an app backend.

``dashboard/token_auth.py`` accepts the dashboard or app token as the ``token`` query
parameter, so a page that loads with ``?token=`` and calls its own backend with it
would hand that credential to the backend, which may log it. The proxy already drops
the ``cookie`` and ``authorization`` headers for this reason; these tests pin that it
drops the query credential too, keeps every other parameter's exact wire bytes, and
signs the request-target it actually sends.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yarl
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
import kiro_crew.apps.routes_proxy as routes_proxy
from kiro_crew.apps.manager import enable_app
from kiro_crew.apps.proxy_auth import (
    PROXY_NONCE_HEADER,
    PROXY_V2_HEADER,
    ProxyPrincipalReplayCache,
    raw_request_target,
    verify_proxy_request,
    verify_proxy_request_v2,
)
from kiro_crew.apps.routes import invalidate_app_secret_cache


@pytest.mark.parametrize(
    ("raw_qs", "forwarded"),
    [
        ("", ""),
        ("a=1", "a=1"),
        ("token=abc", ""),
        ("path=%2Ftmp&token=eyJhbGciOi", "path=%2Ftmp"),
        ("token=abc&path=/tmp", "path=/tmp"),
        ("a=%E2%9C%93&token=x&b=c+d%20e", "a=%E2%9C%93&b=c+d%20e"),
        # aiohttp percent-decodes the name before token_auth reads it.
        ("%74oken=x&a=1", "a=1"),
        ("tok%65n=x&a=1", "a=1"),
        ("token=1&a=1&token=2", "a=1"),
        ("token&a=1", "a=1"),
        ("token=", ""),
    ],
)
def test_the_credential_parameter_is_dropped_and_the_rest_keeps_its_bytes(
    raw_qs: str, forwarded: str
) -> None:
    assert routes_proxy._forwarded_query(raw_qs) == forwarded


@pytest.mark.parametrize(
    "raw_qs",
    [
        # Only the exact name token_auth reads is a credential.
        "tokens=1&my_token=2&TOKEN=3&token_type=4",
        # A value, not a name.
        "a=token&b=token%3D1",
        # An empty field and a trailing separator are left exactly as sent.
        "a=1&&b=2&",
        "+token=1&token+=2",
    ],
)
def test_a_query_without_the_credential_parameter_is_forwarded_unchanged(raw_qs: str) -> None:
    assert routes_proxy._forwarded_query(raw_qs) == raw_qs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_target"),
    [
        ("path=%2Ftmp&token=owner-credential&q=a+b%20c", "/api/resolve?path=%2Ftmp&q=a+b%20c"),
        ("token=owner-credential", "/api/resolve"),
        ("%74oken=owner-credential&path=/tmp", "/api/resolve?path=/tmp"),
    ],
)
async def test_the_backend_receives_no_token_and_the_signatures_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str, expected_target: str
) -> None:
    """A real proxy hop: the backend's request-target carries no ``token`` and both
    the v1 and the v2 signature verify over the target it received."""
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    enable_app(APP)
    (home / "apps" / APP / ".app_secret").write_text("proxy-key", encoding="utf-8")
    invalidate_app_secret_cache(APP)

    seen: dict[str, Any] = {}

    async def _backend_handler(request: web.Request) -> web.Response:
        seen["target"] = raw_request_target(request)
        seen["query"] = dict(request.query)
        seen["headers"] = dict(request.headers)
        seen["body"] = await request.read()
        return web.json_response({"ok": True})

    backend = web.Application()
    backend.router.add_route("*", "/api/{tail:.*}", _backend_handler)
    async with TestServer(backend) as backend_server:
        monkeypatch.setattr(
            routes_mod,
            "_resolve_app_backend_url",
            lambda n: f"http://127.0.0.1:{backend_server.port}",
        )
        async with TestClient(TestServer(_make_app())) as client:
            # ``encoded=True`` sends the query bytes exactly as written.
            resp = await client.get(yarl.URL(f"/apps/{APP}/api/resolve?{query}", encoded=True))
            assert resp.status == 200

    assert seen["target"] == expected_target
    assert "token" not in seen["query"]
    assert "owner-credential" not in seen["target"]
    assert verify_proxy_request(
        seen["headers"]["X-KiroCrew-Proxy"],
        method="GET",
        target=seen["target"],
        body=seen["body"],
        secret="proxy-key",
    )
    assert verify_proxy_request_v2(
        seen["headers"][PROXY_V2_HEADER],
        seen["headers"][PROXY_NONCE_HEADER],
        method="GET",
        target=seen["target"],
        body=seen["body"],
        replay_cache=ProxyPrincipalReplayCache(),
        secret="proxy-key",
    )
