"""Tests for /api/file-raw image serving endpoint — magic bytes, MIME, symlinks."""

from __future__ import annotations

import errno
import os
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
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False):
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
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 403
            assert "sensitive" in (await resp.json())["error"]


# --- _open_rb_nofollow: Windows-safe open (no O_NOFOLLOW there) ---


class TestOpenRbNofollow:
    """The endpoint's open must work on platforms WITHOUT ``os.O_NOFOLLOW``.

    Windows has no ``O_NOFOLLOW``; a bare reference raises AttributeError and
    turns every /api/file-raw request into an HTTP 500 — on exactly the
    platform whose image previews route through this endpoint.
    """

    def test_regular_file_opens_without_o_nofollow(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers.files import _open_rb_nofollow

        f = tmp_path / "img.png"
        f.write_bytes(b"\x89PNG payload")
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        fd = _open_rb_nofollow(str(f))
        with os.fdopen(fd, "rb") as fh:
            assert fh.read() == b"\x89PNG payload"

    def test_symlink_rejected_with_eloop_without_o_nofollow(self, tmp_path, monkeypatch):
        """The lstat fallback must keep the POSIX ELOOP contract so callers'
        error handling (403 symlinks not allowed) is platform-invariant."""
        from kiro_crew.dashboard.handlers.files import _open_rb_nofollow

        target = tmp_path / "secret.png"
        target.write_bytes(b"x")
        link = tmp_path / "link.png"
        os.symlink(target, link)
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        with pytest.raises(OSError) as exc_info:
            _open_rb_nofollow(str(link))
        assert exc_info.value.errno == errno.ELOOP

    def test_symlink_rejected_with_o_nofollow_present(self, tmp_path):
        from kiro_crew.dashboard.handlers.files import _open_rb_nofollow

        target = tmp_path / "secret.png"
        target.write_bytes(b"x")
        link = tmp_path / "link.png"
        os.symlink(target, link)
        with pytest.raises(OSError) as exc_info:
            _open_rb_nofollow(str(link))
        assert exc_info.value.errno == errno.ELOOP


# ── both file endpoints share ONE security envelope (#4031) ──────────────────


def test_both_endpoints_route_through_the_shared_envelope():
    """api_file_raw and api_file_download must not re-grow private copies.

    The envelope (validate -> sensitive-path -> O_NOFOLLOW open -> bounded-read size
    cap -> read) is a security boundary. It used to be spelled out per endpoint,
    and the copies had already drifted -- they called different bindings of
    is_sensitive_path, so an override applied to one was invisible to the other.
    Three divergent copies mean a future hardening fix lands in some and leaves
    the rest on the old posture.

    Asserted on the source because the property is structural: what matters is
    that neither endpoint opens files itself, not what a given call returns.
    """
    import inspect

    from kiro_crew.dashboard.handlers import files as mod

    for fn in (mod.api_file_raw, mod.api_file_download):
        src = inspect.getsource(fn)
        body = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        assert "_open_checked" in body, f"{fn.__name__} must use the shared envelope"
        assert "asyncio.to_thread" in body, (
            f"{fn.__name__} must offload the envelope to a worker thread -- the "
            "envelope is synchronous file I/O and must not run on the event loop"
        )
        assert "_open_rb_nofollow(" not in body, (
            f"{fn.__name__} re-opened a file itself instead of going through "
            "_open_checked -- that is how the copies drifted apart"
        )
        assert "is_sensitive_path" not in body, (
            f"{fn.__name__} re-checks the sensitive path itself; the envelope owns it"
        )


@pytest.mark.asyncio
async def test_the_envelope_still_rejects_a_symlink_for_both(tmp_path):
    """The boundary's most load-bearing guard, exercised through both endpoints
    now that only one implementation of it exists."""
    from kiro_crew.dashboard.handlers import api_file_download

    real = tmp_path / "real.png"
    real.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    link = tmp_path / "link.png"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/user")

    app = web.Application()
    app.router.add_get("/api/file-raw", api_file_raw)
    app.router.add_get("/api/file-download", api_file_download)

    for route in ("file-raw", "file-download"):
        with patch(
            "kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(link)
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False
        ), patch("kiro_crew.sel.sel") as m:
            m.return_value = MagicMock()
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(f"/api/{route}?path={link}")
                assert resp.status == 403, route
