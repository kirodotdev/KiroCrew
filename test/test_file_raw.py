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
    """Every file-serving site must route through the shared security prefix.

    The prefix (validate -> sensitive-path -> O_NOFOLLOW open -> size cap) is
    a security boundary, and a hand-rolled copy of one drifts: separate
    bindings of is_sensitive_path mean an override applied to one is
    invisible to the other, and a future hardening fix lands in some copies
    and leaves the rest on the old posture.

    Pins all four adopters -- the two whole-read endpoints (via _open_checked),
    the streaming endpoint, and the sheet endpoint (via _open_checked_file
    directly) -- plus the whole-read envelope itself and the sheet parser, so
    a fifth hand-rolled copy of the prefix fails here before it can drift.

    Asserted on the source because the property is structural: what matters is
    that no endpoint opens files or re-checks the sensitive path itself, not
    what a given call returns.
    """
    import ast
    import inspect
    import textwrap

    from kiro_crew.dashboard.handlers import files as mod

    def _body(fn):
        """The function's source minus every docstring and comment.

        The docstrings must go -- including NESTED ones (api_file_stream's
        _open_media carries its own): they legitimately NAME the shared
        helpers, so a positive assertion against raw source would pass on
        prose alone and the mutation check (remove an adopter, the test must
        fail) would go vacuous.
        """
        src = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(src)
        spans = []
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (
                isinstance(body, list)
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                spans.append((body[0].lineno, body[0].end_lineno))
        lines = src.splitlines()
        for start, end in sorted(spans, reverse=True):
            del lines[start - 1:end]
        return "\n".join(
            line for line in lines if not line.lstrip().startswith("#")
        )

    # Whole-read endpoints: the shared _open_checked envelope, offloaded.
    for fn in (mod.api_file_raw, mod.api_file_download):
        body = _body(fn)
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

    # The whole-read envelope itself is a bounded read over the shared prefix:
    # it must not re-grow a private copy of the open-and-check half.
    body = _body(mod._open_checked)
    assert "_open_checked_file(" in body, (
        "_open_checked must route through the shared open-and-check prefix"
    )
    assert "_open_rb_nofollow(" not in body, (
        "_open_checked re-opened a file itself; _open_checked_file owns the open"
    )
    assert "is_sensitive_path" not in body, (
        "_open_checked re-checks the sensitive path itself; the prefix owns the gate"
    )

    # Streaming + sheet endpoints: the same prefix, their caps passed in as
    # policy (fstat cap for the stream, bounded read for the sheet parser).
    # No paren in the positive assertion: the sheet endpoint hands the helper
    # to asyncio.to_thread as an argument rather than calling it inline.
    for fn in (mod.api_file_stream, mod.api_file_sheet):
        body = _body(fn)
        assert "_open_checked_file" in body, (
            f"{fn.__name__} must use the shared open-and-check prefix"
        )
        assert "asyncio.to_thread" in body, (
            f"{fn.__name__} must offload the prefix to a worker thread -- it is "
            "synchronous file I/O and must not run on the event loop"
        )
        assert "_open_rb_nofollow(" not in body, (
            f"{fn.__name__} re-opened a file itself instead of going through "
            "_open_checked_file -- that is how the copies drifted apart"
        )
        assert "is_sensitive_path" not in body, (
            f"{fn.__name__} re-checks the sensitive path itself; the prefix owns it"
        )

    # The sheet parser receives the checked-open file object; it must not
    # re-grow an open or a sensitive-path check of its own.
    body = _body(mod._load_sheet_payload)
    assert "_open_rb_nofollow(" not in body, (
        "_load_sheet_payload re-opened a file itself; it must receive the "
        "checked-open file from _open_checked_file"
    )
    assert "is_sensitive_path" not in body, (
        "_load_sheet_payload re-checks the sensitive path itself; the prefix owns it"
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


@pytest.mark.asyncio
async def test_a_nul_bearing_path_is_a_400_with_a_sel_denied(mock_sel):
    """A malformed path (embedded NUL makes realpath raise ValueError) is an
    invalid path, not a crash: the shared prefix answers 400 and the refusal
    is SEL-audited as denied. Pinned because /api/file-raw has no
    validate_tool_args guard ahead of the envelope, so the prefix's
    ValueError handling is the only thing between this input and an
    unaudited 500."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/file-raw?path=%00x")
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid or forbidden path"
    outcomes = [
        call.kwargs.get("outcome")
        for call in mock_sel.log_tool_invocation.call_args_list
    ]
    assert "denied" in outcomes
