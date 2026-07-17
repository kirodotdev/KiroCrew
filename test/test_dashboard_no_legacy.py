"""Regression guard for P472043682 — legacy fallback dashboard removed.

The legacy in-tree dashboard shell (``static/dashboard.html`` + its JS/CSS)
and the vendored DOMPurify were dead code and an XSS surface. This test pins
that they stay gone and that ``core.index()`` now serves ONLY the React build
or a static, secret-free build-required guidance page — never the old shell.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.core as core

_STATIC_DIR = Path(core.__file__).resolve().parent.parent.parent / "static"

_LEGACY_FILES = (
    _STATIC_DIR / "dashboard.html",
    _STATIC_DIR / "js" / "dashboard.js",
    _STATIC_DIR / "js" / "purify.min.js",
    _STATIC_DIR / "css" / "dashboard.css",
    _STATIC_DIR / "css" / "cli-mode.css",
)


@pytest.mark.parametrize("legacy", _LEGACY_FILES, ids=lambda p: p.name)
def test_legacy_static_files_absent(legacy: Path) -> None:
    assert not legacy.exists(), f"legacy dashboard asset should be removed: {legacy}"


def test_no_html_path_constant() -> None:
    """The removed ``_HTML_PATH`` constant must not be reintroduced."""
    assert not hasattr(core, "_HTML_PATH")


def _make_request(remote: str = "127.0.0.1", cookies: dict | None = None) -> MagicMock:
    req = MagicMock(spec=web.Request)
    req.path = "/"
    req.query = {}
    req.cookies = cookies or {}
    req.remote = remote
    req.headers = {}
    req.method = "GET"
    return req


@pytest.mark.asyncio
async def test_index_serves_guidance_when_bundle_missing(monkeypatch, tmp_path: Path) -> None:
    """With the React build absent, index() serves the static guidance page —
    never the legacy shell — and the body is request-independent."""
    # Point _DIST_INDEX at a non-existent path so index() hits the fallback.
    monkeypatch.setattr(core, "_DIST_INDEX", tmp_path / "dist" / "index.html")

    resp_anon = await core.index(_make_request(remote="10.0.0.1"))
    resp_authed = await core.index(
        _make_request(remote="10.0.0.1", cookies={"mc_token_7777": "x.y.z"})
    )

    assert core.DASHBOARD_HTML_NOT_FOUND_MARKER in resp_anon.text
    assert '<div class="shell"' not in resp_anon.text
    # Request-independent: anon body == authed body (auth-boundary invariant).
    assert resp_anon.text == resp_authed.text


@pytest.mark.asyncio
async def test_index_serves_react_build_when_present(monkeypatch, tmp_path: Path) -> None:
    """When dist/index.html exists, index() serves it verbatim."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>REACT_SPA</body></html>", encoding="utf-8")
    monkeypatch.setattr(core, "_DIST_INDEX", dist / "index.html")

    resp = await core.index(_make_request())
    assert "REACT_SPA" in resp.text
    assert core.DASHBOARD_HTML_NOT_FOUND_MARKER not in resp.text
