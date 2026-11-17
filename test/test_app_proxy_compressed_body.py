"""The app API proxy relays a compressed backend body as the bytes its headers describe.

The relay copies the backend's ``Content-Encoding`` and ``Content-Length``. If the
proxy's own client decompressed the body on the way through, the browser would get
plain bytes labelled gzip and fail to decode them.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import enable_app
from kiro_crew.apps.routes import invalidate_app_secret_cache

_BODY = b"compressible " * 400


@pytest.mark.asyncio
async def test_a_gzip_backend_body_reaches_the_client_decodable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    enable_app(APP)
    (home / "apps" / APP / ".app_secret").write_text("proxy-key", encoding="utf-8")
    invalidate_app_secret_cache(APP)

    async def _backend_handler(request: web.Request) -> web.Response:
        return web.Response(
            body=gzip.compress(_BODY),
            headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"},
        )

    backend = web.Application()
    backend.router.add_route("*", "/api/{tail:.*}", _backend_handler)
    async with TestServer(backend) as backend_server:
        monkeypatch.setattr(
            routes_mod,
            "_resolve_app_backend_url",
            lambda n: f"http://127.0.0.1:{backend_server.port}",
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/apps/{APP}/api/data", headers={"Accept-Encoding": "gzip"})
            assert resp.status == 200
            assert resp.headers.get("Content-Encoding") == "gzip"
            try:
                body = await resp.read()
            except aiohttp.ClientPayloadError as exc:
                pytest.fail(f"the relayed body did not match its Content-Encoding: {exc}")
    assert body == _BODY
