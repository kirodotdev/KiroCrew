"""Regression: a 404 under ``/assets/`` must never leave with ``immutable``.

The unit tests in ``test_dashboard_security_headers.py`` feed
``_apply_security_headers`` a ``web.Response`` whose status is already final,
so they cannot see this bug: aiohttp's ``FileResponse`` (what ``add_static``
returns) is constructed with status 200 and only discovers the file is missing
inside ``prepare()`` -- AFTER every middleware has run. The middleware therefore
stamps ``public, max-age=31536000, immutable`` on what becomes a 404, and
Chromium stores that 404 for a year under the request URL. Because lucide icon
chunks keep the same content hash across releases, a 404 cached while a gateway
was mid-swap of its ``dist/`` poisons the module graph of every later bundle
that imports the same chunk: the ``<script type=module>`` fails silently and the
pane never boots. Observed on the desktop app's remote-crew panes (one origin
stuck for days while its siblings on other ports loaded fine).

These tests drive a real ``add_static`` mount through the real middleware and a
real ``TestClient`` so the headers asserted are the ones that went on the wire.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import server as srv


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "main-krfrL6rl.js").write_text("export const x = 1\n", encoding="utf-8")
    return dist


def _make_app(dist: Path) -> web.Application:
    """The production wiring in miniature: static mount + header middleware
    + the prepare-time finalizer, nothing else."""
    app = web.Application()
    srv._register_dist_static_routes(app, dist)

    @web.middleware  # type: ignore[misc]
    async def no_cache_middleware(request: web.Request, handler: object) -> web.StreamResponse:
        resp = await handler(request)  # type: ignore[operator]
        if hasattr(resp, "headers"):
            srv._apply_security_headers(resp, request.app, request.path, request)
        return resp  # type: ignore[return-value]

    app.middlewares.append(no_cache_middleware)
    srv._install_asset_cache_control_finalizer(app)
    return app


@pytest.mark.asyncio
async def test_missing_hashed_asset_is_not_cached(tmp_path: Path) -> None:
    async with TestClient(TestServer(_make_app(_dist(tmp_path)))) as client:
        resp = await client.get("/assets/check-BiXj6uGO.js")
        assert resp.status == 404
        cc = resp.headers["Cache-Control"]
        assert "immutable" not in cc, cc
        assert "no-store" in cc, cc
        assert resp.headers.get("Pragma") == "no-cache"
        assert resp.headers.get("Expires") == "0"


@pytest.mark.asyncio
async def test_present_hashed_asset_stays_immutable(tmp_path: Path) -> None:
    async with TestClient(TestServer(_make_app(_dist(tmp_path)))) as client:
        resp = await client.get("/assets/main-krfrL6rl.js")
        assert resp.status == 200
        cc = resp.headers["Cache-Control"]
        assert "immutable" in cc, cc
        assert "no-store" not in cc, cc


@pytest.mark.asyncio
async def test_conditional_hit_on_hashed_asset_stays_immutable(tmp_path: Path) -> None:
    """A 304 is a FileResponse too, and its headers merge into the browser's
    stored entry -- downgrading it would un-cache a good bundle."""
    async with TestClient(TestServer(_make_app(_dist(tmp_path)))) as client:
        first = await client.get("/assets/main-krfrL6rl.js")
        etag = first.headers["ETag"]
        resp = await client.get("/assets/main-krfrL6rl.js", headers={"If-None-Match": etag})
        assert resp.status == 304
        assert "immutable" in resp.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_finalizer_leaves_non_asset_paths_alone(tmp_path: Path) -> None:
    """A handler-produced 404 outside /assets/ is already no-store from the
    middleware; the finalizer must not touch it (and must not raise)."""
    app = _make_app(_dist(tmp_path))

    async def missing(_request: web.Request) -> web.Response:
        return web.Response(status=404, headers={"X-Probe": "1"})

    app.router.add_get("/api/missing", missing)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/missing")
        assert resp.status == 404
        assert resp.headers["X-Probe"] == "1"
        assert "no-store" in resp.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_without_finalizer_the_404_is_immutable(tmp_path: Path) -> None:
    """Pins the mechanism this file exists for: with only the middleware, the
    missing-file 404 really does leave with ``immutable``. If aiohttp ever
    moves the existence check back before the handler returns, this test
    fails and the finalizer can be retired."""
    app = web.Application()
    srv._register_dist_static_routes(app, _dist(tmp_path))

    @web.middleware  # type: ignore[misc]
    async def mw(request: web.Request, handler: object) -> web.StreamResponse:
        resp = await handler(request)  # type: ignore[operator]
        srv._apply_security_headers(resp, request.app, request.path, request)
        return resp  # type: ignore[return-value]

    app.middlewares.append(mw)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/assets/check-BiXj6uGO.js")
        assert resp.status == 404
        assert "immutable" in resp.headers["Cache-Control"]
