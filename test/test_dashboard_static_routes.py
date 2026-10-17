"""Tests for ``kiro_crew.dashboard.server._register_dist_static_routes``.

The dashboard serves the React ``dist/`` build by mounting each build
subdirectory at a fixed URL prefix. The font route in particular is load-
bearing: the self-hosted AWS Diatype woff2 files are referenced by absolute
``url('/fonts/...')`` in ``@font-face``, so without a ``/fonts`` route the
request falls through to the SPA fallback (``index.html``) and the browser
fails to parse the HTML as a font ("invalid sfntVersion").

Every route resolves ``static/dist`` per request. On a source checkout that is
a link staging re-points, and a route resolved once at startup would keep
serving the old target while ``index.html`` (read per request) references the
new one's chunks: a blank dashboard until restart.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_dir_link, requires_symlinks
from kiro_crew.dashboard import server as server_mod
from kiro_crew.dashboard.server import _register_dist_static_routes
from kiro_crew.platform_compat import unlink_link_or_junction

_PREFIXES = {"/assets", "/sprites", "/fonts", "/vendor", "/app-assets"}


def _registered_prefixes(app: web.Application) -> set[str]:
    """The build prefixes wired onto ``app`` (``/<prefix>/{tail}`` routes)."""
    return {
        resource.canonical.split("/{", 1)[0]
        for resource in app.router.resources()
        if resource.canonical.endswith("/{tail}")
    }


def _make_dist(root: Path, *subdirs: str) -> Path:
    """Create a fake dist/ dir with the given subdirectories populated."""
    dist = root / "dist"
    dist.mkdir()
    for sub in subdirs:
        (dist / sub).mkdir()
    return dist


def test_every_build_prefix_is_registered_whatever_the_build_holds(tmp_path) -> None:
    """All five prefixes are routed even before the build has the subdirectory, or exists."""
    for dist in (_make_dist(tmp_path, "assets"), tmp_path / "not-built"):
        app = web.Application()
        _register_dist_static_routes(app, dist)
        assert _registered_prefixes(app) == _PREFIXES


@pytest.mark.asyncio
async def test_a_build_that_lands_after_start_is_served(tmp_path: Path) -> None:
    """A gateway started before the first build serves it once it lands, without a restart."""
    dist = tmp_path / "dist"
    app = web.Application()
    _register_dist_static_routes(app, dist)

    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/assets/app-1.js")).status == 404
        (dist / "assets").mkdir(parents=True)
        (dist / "assets" / "app-1.js").write_text("export {}", encoding="utf-8")
        resp = await client.get("/assets/app-1.js")
        assert resp.status == 200
        assert await resp.text() == "export {}"


@pytest.mark.asyncio
async def test_a_re_pointed_dist_link_is_followed_on_the_next_request(tmp_path: Path) -> None:
    """Re-pointing ``static/dist`` moves every route with it, as it moves index.html.

    A route resolved once at registration would keep serving the old target
    after a re-point, so ``/assets/app-B.js`` (what the new index.html
    references) would answer 404 until a restart, and ``/assets/app-A.js``
    would still answer 200. The old target is left in place: deleting it would
    prove nothing more, and on Windows the file the first response served can
    still be open in the server when the client has its status.
    """
    for name in ("a", "b"):
        (tmp_path / name / "assets").mkdir(parents=True)
        (tmp_path / name / "assets" / f"app-{name.upper()}.js").write_text(name, encoding="utf-8")
    served = tmp_path / "served"
    make_dir_link(served, tmp_path / "a")
    app = web.Application()
    _register_dist_static_routes(app, served)

    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/assets/app-A.js")).status == 200
        unlink_link_or_junction(served)
        make_dir_link(served, tmp_path / "b")
        resp = await client.get("/assets/app-B.js")
        assert resp.status == 200
        assert await resp.text() == "b"
        assert (await client.get("/assets/app-A.js")).status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/assets/../secret.txt",
        "/assets/%2e%2e/secret.txt",
        "/assets/..%2fsecret.txt",
        "/assets/%2Fetc%2Fpasswd",
        "/fonts/../../secret.txt",
    ],
)
async def test_a_path_outside_the_build_is_not_served(tmp_path: Path, path: str) -> None:
    """``..``, an encoded ``..`` and an absolute tail never reach a file outside the subdirectory."""
    dist = _make_dist(tmp_path, "assets", "fonts")
    (dist / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    app = web.Application()
    _register_dist_static_routes(app, dist)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        assert resp.status == 404
        assert await resp.text() != "secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/assets/%5C%5Cshare.example%5Cshare%5Cx",
        "/assets///share.example/share/x",
        "/fonts/C:/Windows/win.ini",
        "/vendor/C:x",
    ],
)
async def test_an_anchored_tail_is_refused_before_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A UNC, rooted or drive-qualified tail never reaches ``resolve``, as add_static refused it."""
    dist = _make_dist(tmp_path, "assets", "fonts", "vendor")
    resolved: list[Path] = []
    real_resolve = Path.resolve

    def _recording_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        resolved.append(self)
        return real_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    app = web.Application()
    _register_dist_static_routes(app, dist)

    async with TestClient(TestServer(app)) as client:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "resolve", _recording_resolve)
            resp = await client.get(path)
        assert resp.status == 404
    assert not [p for p in resolved if str(dist) in str(p)], resolved


@pytest.mark.parametrize(
    "tail", ["\\\\share.example\\share\\x", "//share.example/share/x", "C:x", "/x"]
)
def test_an_anchored_tail_names_no_file(tmp_path: Path, tail: str) -> None:
    """The guard itself, independent of how aiohttp decodes the request path."""
    assert server_mod._is_anchored(tail)
    assert server_mod._resolve_dist_file(_make_dist(tmp_path, "assets"), "assets", tail) is None


def test_an_ordinary_tail_is_not_anchored() -> None:
    """Hashed chunks and nested window entries pass the guard."""
    assert not server_mod._is_anchored("app-abc123.js")
    assert not server_mod._is_anchored("dev-fleet/window.html")


@pytest.mark.asyncio
async def test_assets_fall_back_to_the_build_root_without_an_assets_dir(tmp_path: Path) -> None:
    """A build with no ``assets/`` is served at ``/assets`` from its root, as the mount always was."""
    dist = _make_dist(tmp_path)
    (dist / "app.js").write_text("root", encoding="utf-8")
    app = web.Application()
    _register_dist_static_routes(app, dist)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/assets/app.js")
        assert resp.status == 200
        assert await resp.text() == "root"


@requires_symlinks
@pytest.mark.asyncio
async def test_a_link_inside_the_build_that_leaves_it_is_not_followed(tmp_path: Path) -> None:
    """Confined like ``add_static(follow_symlinks=False)``: the resolved file must stay inside."""
    dist = _make_dist(tmp_path, "assets")
    (tmp_path / "outside.js").write_text("outside", encoding="utf-8")
    os.symlink(tmp_path / "outside.js", dist / "assets" / "escape.js")
    app = web.Application()
    _register_dist_static_routes(app, dist)

    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/assets/escape.js")).status == 404


@pytest.mark.asyncio
async def test_a_precompressed_sibling_is_still_negotiated(tmp_path: Path) -> None:
    """``FileResponse`` keeps answering with the ``.gz`` sibling the build precompressed."""
    import gzip

    dist = _make_dist(tmp_path, "assets")
    (dist / "assets" / "app.js").write_text("export const x = 1", encoding="utf-8")
    (dist / "assets" / "app.js.gz").write_bytes(gzip.compress(b"export const x = 1"))
    app = web.Application()
    _register_dist_static_routes(app, dist)

    async with TestClient(TestServer(app), auto_decompress=False) as client:
        resp = await client.get("/assets/app.js", headers={"Accept-Encoding": "gzip"})
        assert resp.status == 200
        assert resp.headers.get("Content-Encoding") == "gzip"


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
