"""Tests for ``kiro_crew.dashboard.server._register_dist_static_routes``.

The dashboard serves the React ``dist/`` build by mounting each present
subdirectory at a fixed URL prefix. The font route in particular is load-
bearing: the self-hosted AWS Diatype woff2 files are referenced by absolute
``url('/fonts/...')`` in ``@font-face``, so without a ``/fonts`` static route
the request falls through to the SPA fallback (``index.html``) and the browser
fails to parse the HTML as a font ("invalid sfntVersion").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.server import _register_dist_static_routes


def _registered_prefixes(app: web.Application) -> set[str]:
    """The set of static-route prefixes wired onto ``app``."""
    prefixes: set[str] = set()
    for resource in app.router.resources():
        info = resource.get_info()
        # aiohttp StaticResource exposes its mount point under "prefix".
        prefix = info.get("prefix")
        if prefix:
            prefixes.add(prefix)
    return prefixes


def _make_dist(root: Path, *subdirs: str) -> Path:
    """Create a fake dist/ dir with the given subdirectories populated."""
    dist = root / "dist"
    dist.mkdir()
    for sub in subdirs:
        (dist / sub).mkdir()
    return dist


def test_fonts_route_registered_when_fonts_dir_present(tmp_path) -> None:
    """A dist/ with a fonts/ subdir gets a /fonts static route."""
    dist = _make_dist(tmp_path, "assets", "fonts")
    app = web.Application()

    _register_dist_static_routes(app, dist)

    prefixes = _registered_prefixes(app)
    assert "/fonts" in prefixes
    assert "/assets" in prefixes


def test_fonts_route_skipped_when_fonts_dir_absent(tmp_path) -> None:
    """No fonts/ subdir -> no /fonts route (only the always-on /assets)."""
    dist = _make_dist(tmp_path, "assets")
    app = web.Application()

    _register_dist_static_routes(app, dist)

    prefixes = _registered_prefixes(app)
    assert "/fonts" not in prefixes
    assert "/assets" in prefixes


def test_optional_subdirs_registered_only_when_present(tmp_path) -> None:
    """sprites/, vendor/ and app-assets/ mount only when they exist; /assets is always on."""
    dist = _make_dist(tmp_path, "assets", "sprites", "fonts", "vendor", "app-assets")
    app = web.Application()

    _register_dist_static_routes(app, dist)

    prefixes = _registered_prefixes(app)
    assert {"/assets", "/sprites", "/fonts", "/vendor", "/app-assets"} <= prefixes


def test_app_assets_route_registered_when_dir_present(tmp_path) -> None:
    """A dist/ with an app-assets/ subdir gets an /app-assets static route.

    Regression: builtin app.json files reference brand art via absolute
    ``url('/app-assets/...')`` (iconUrl / heroImage / heroImageDark). Without
    this mount the request falls through to the SPA fallback (index.html) and
    the App Store <img> tags load HTML as an image, tripping their onError
    placeholder (generic lucide icon / "KIROCREW" hero).
    """
    dist = _make_dist(tmp_path, "assets", "app-assets")
    app = web.Application()

    _register_dist_static_routes(app, dist)

    assert "/app-assets" in _registered_prefixes(app)


def test_app_assets_route_skipped_when_dir_absent(tmp_path) -> None:
    """No app-assets/ subdir -> no /app-assets route (only the always-on /assets)."""
    dist = _make_dist(tmp_path, "assets")
    app = web.Application()

    _register_dist_static_routes(app, dist)

    prefixes = _registered_prefixes(app)
    assert "/app-assets" not in prefixes
    assert "/assets" in prefixes


@pytest.mark.asyncio
async def test_app_assets_svg_served_not_spa_shell(tmp_path: Path) -> None:
    """An SVG under /app-assets is served verbatim (the file, not index.html).

    Proves the builtin icon/hero art is reachable through the gateway once the
    build's dist/app-assets/ is mounted — the exact failure mode that made the
    "recently added" colorful icons and hero images not render.
    """
    dist = _make_dist(tmp_path, "assets", "app-assets")
    (dist / "app-assets" / "auto-research").mkdir()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>'
    (dist / "app-assets" / "auto-research" / "icon.svg").write_bytes(svg)

    app = web.Application()
    _register_dist_static_routes(app, dist)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/app-assets/auto-research/icon.svg")
        assert resp.status == 200
        assert (await resp.read()) == svg
        assert resp.content_type == "image/svg+xml"


# ---------------------------------------------------------------------------
# Content-Type verification for font files served via /fonts static route
# ---------------------------------------------------------------------------

_FONT_CONTENT_TYPE_CASES = [
    ("test.woff", "font/woff"),
    ("test.woff2", "font/woff2"),
    ("test.ttf", "font/ttf"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,expected_ct", _FONT_CONTENT_TYPE_CASES)
async def test_font_files_served_with_correct_content_type(
    tmp_path: Path,
    filename: str,
    expected_ct: str,
) -> None:
    """Font files under /fonts must return their proper MIME Content-Type.

    aiohttp's bare MimeTypes instance lacks font extensions and would fall
    back to ``application/octet-stream`` without explicit registration.
    The import-time registration in ``server.py`` fixes this for all static
    routes — verify it works end-to-end through the aiohttp test client.
    """
    dist = _make_dist(tmp_path, "assets", "fonts")
    # Create a dummy font file with some arbitrary bytes.
    (dist / "fonts" / filename).write_bytes(b"\x00wOFF" * 4)

    app = web.Application()
    _register_dist_static_routes(app, dist)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/fonts/{filename}")
        assert resp.status == 200
        assert resp.content_type == expected_ct
