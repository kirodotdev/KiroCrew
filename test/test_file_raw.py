"""Tests for /api/file-raw image serving endpoint — magic bytes, MIME, symlinks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_file_raw


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-raw", api_file_raw)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m, \
         patch("kiro_crew.security.is_sensitive_path", return_value=False):
        instance = MagicMock()
        m.return_value = instance
        yield instance


# --- Magic bytes: accepted formats ---

_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
_JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 100
_GIF89_HEADER = b"GIF89a" + b"\x00" * 100
_WEBP_HEADER = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100
_BMP_HEADER = b"BM" + b"\x00" * 100
_TIFF_LE_HEADER = b"II\x2a\x00" + b"\x00" * 100
_ICO_HEADER = b"\x00\x00\x01\x00" + b"\x00" * 100
_SVG_CONTENT = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
_SVG_WITH_XML = b"<?xml version='1.0'?><svg></svg>"
_SVG_WITH_BOM = b"\xef\xbb\xbf<svg></svg>"


@pytest.mark.asyncio
@pytest.mark.parametrize("ext,data", [
    ("png", _PNG_HEADER),
    ("jpg", _JPEG_HEADER),
    ("gif", _GIF89_HEADER),
    ("webp", _WEBP_HEADER),
    ("bmp", _BMP_HEADER),
    ("tiff", _TIFF_LE_HEADER),
    ("ico", _ICO_HEADER),
])
async def test_serves_valid_image_formats(tmp_path, mock_sel, ext, data):
    f = tmp_path / f"test.{ext}"
    f.write_bytes(data)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 200
            assert resp.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [_SVG_CONTENT, _SVG_WITH_XML, _SVG_WITH_BOM])
async def test_serves_svg(tmp_path, mock_sel, data):
    f = tmp_path / "test.svg"
    f.write_bytes(data)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 200


# --- Rejected cases ---

@pytest.mark.asyncio
async def test_rejects_non_image_mime(tmp_path, mock_sel):
    f = tmp_path / "test.txt"
    f.write_text("not an image")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 403
            assert "not a recognized format" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_rejects_wrong_magic_bytes(tmp_path, mock_sel):
    """File with .png extension but non-image content."""
    f = tmp_path / "fake.png"
    f.write_bytes(b"this is not a png file at all")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 403
            assert "not a recognized format" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_rejects_invalid_path(mock_sel):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-raw?path=../../etc/passwd")
            assert resp.status == 400


@pytest.mark.asyncio
async def test_rejects_missing_file(tmp_path, mock_sel):
    missing = str(tmp_path / "nope.png")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=missing):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-raw?path={missing}")
            assert resp.status == 404


@pytest.mark.asyncio
async def test_rejects_symlink(tmp_path, mock_sel):
    real = tmp_path / "real.png"
    real.write_bytes(_PNG_HEADER)
    link = tmp_path / "link.png"
    link.symlink_to(real)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(link)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-raw?path={link}")
            assert resp.status == 403
            assert "symlink" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_rejects_sensitive_path(tmp_path, mock_sel):
    f = tmp_path / "creds.png"
    f.write_bytes(_PNG_HEADER)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.security.is_sensitive_path", return_value=True):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 403
            assert "sensitive" in (await resp.json())["error"]
