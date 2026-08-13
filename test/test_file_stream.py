"""Tests for /api/file-stream — Range-capable audio/video serving.

Covers the media sniffing allowlist (content decides, not extension), the
HTTP Range semantics that <video>/<audio> seeking depends on (200 full body,
206 partial, 416 unsatisfiable, suffix form), and the security envelope
shared with the sibling file endpoints (path validation, sensitive paths,
symlinks, size cap).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_file_stream
from kiro_crew.dashboard.handlers.files import (
    _parse_range_header,
    _resolve_project_relative,
    _sniff_media_type,
)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-stream", api_file_stream)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m, \
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False):
        instance = MagicMock()
        m.return_value = instance
        yield instance


# A minimal-but-valid mp4 prefix: 4-byte box size, then "ftyp".
_MP4_BYTES = b"\x00\x00\x00\x20ftypisom" + bytes(range(256)) * 4
_WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00" * 128
_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64
_MP3_ID3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" + b"\x00" * 64


# --- unit: sniffing ---


def test_sniff_recognizes_media_containers():
    assert _sniff_media_type(_MP4_BYTES[:16]) == "video/mp4"
    assert _sniff_media_type(_WEBM_BYTES[:16]) == "video/webm"
    assert _sniff_media_type(_WAV_BYTES[:16]) == "audio/wav"
    assert _sniff_media_type(_MP3_ID3_BYTES[:16]) == "audio/mpeg"
    assert _sniff_media_type(b"OggS" + b"\x00" * 12) == "audio/ogg"
    assert _sniff_media_type(b"fLaC" + b"\x00" * 12) == "audio/flac"


def test_sniff_rejects_non_media():
    assert _sniff_media_type(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) is None  # png
    assert _sniff_media_type(b"%PDF-1.7" + b"\x00" * 8) is None
    assert _sniff_media_type(b"PK\x03\x04" + b"\x00" * 12) is None  # zip/xlsx
    assert _sniff_media_type(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4) is None  # webp is an image
    assert _sniff_media_type(b"") is None


# --- unit: range parsing ---


def test_parse_range_forms():
    assert _parse_range_header("bytes=0-99", 1000) == (0, 99)
    assert _parse_range_header("bytes=500-", 1000) == (500, 999)
    assert _parse_range_header("bytes=-100", 1000) == (900, 999)
    assert _parse_range_header("bytes=0-9999", 1000) == (0, 999)  # end clamped


def test_parse_range_rejects_malformed_and_unsatisfiable():
    assert _parse_range_header("bytes=1000-1001", 1000) is None  # start past EOF
    assert _parse_range_header("bytes=5-2", 1000) is None
    assert _parse_range_header("bytes=0-10,20-30", 1000) is None  # multi-range
    assert _parse_range_header("items=0-10", 1000) is None
    assert _parse_range_header("bytes=abc-def", 1000) is None
    assert _parse_range_header("bytes=-0", 1000) is None


# --- endpoint: happy paths ---


@pytest.mark.asyncio
async def test_full_body_200_with_accept_ranges(tmp_path, mock_sel):
    f = tmp_path / "demo.mp4"
    f.write_bytes(_MP4_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 200
            assert resp.headers["Accept-Ranges"] == "bytes"
            assert resp.headers["Content-Type"] == "video/mp4"
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert await resp.read() == _MP4_BYTES


@pytest.mark.asyncio
async def test_range_request_returns_206_partial(tmp_path, mock_sel):
    f = tmp_path / "demo.mp4"
    f.write_bytes(_MP4_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-stream?path={f}", headers={"Range": "bytes=10-49"}
            )
            assert resp.status == 206
            assert resp.headers["Content-Range"] == f"bytes 10-49/{len(_MP4_BYTES)}"
            body = await resp.read()
            assert body == _MP4_BYTES[10:50]


@pytest.mark.asyncio
async def test_suffix_range_serves_file_tail(tmp_path, mock_sel):
    f = tmp_path / "note.wav"
    f.write_bytes(_WAV_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-stream?path={f}", headers={"Range": "bytes=-16"}
            )
            assert resp.status == 206
            assert await resp.read() == _WAV_BYTES[-16:]


@pytest.mark.asyncio
async def test_unsatisfiable_range_416_with_star_size(tmp_path, mock_sel):
    f = tmp_path / "demo.mp4"
    f.write_bytes(_MP4_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-stream?path={f}",
                headers={"Range": f"bytes={len(_MP4_BYTES) + 10}-"},
            )
            assert resp.status == 416
            assert resp.headers["Content-Range"] == f"bytes */{len(_MP4_BYTES)}"
            assert (await resp.json())["code"] == "bad_range"


# --- endpoint: refusals ---


@pytest.mark.asyncio
async def test_non_media_content_refused_415(tmp_path, mock_sel):
    f = tmp_path / "fake.mp4"  # extension claims video, bytes are a zip
    f.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 415
            assert (await resp.json())["code"] == "not_media"


@pytest.mark.asyncio
async def test_invalid_path_400(mock_sel):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-stream?path=../../etc/passwd")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_path"


@pytest.mark.asyncio
async def test_nul_byte_path_is_invalid_not_500(mock_sel):
    """An embedded NUL must be refused, never crash. On POSIX realpath raises
    ValueError inside the validator (400 invalid_path); on Windows realpath
    tolerates the NUL and the existence probe refuses instead (404 not_found).
    Both are refusals; the guarded failure is an unhandled 500."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/file-stream?path=%00evil.mp4")
        assert resp.status in (400, 404)
        assert (await resp.json())["code"] in ("invalid_path", "not_found")


# --- resolve=1: the relative-path contract shared with file-read/file-download ---


@pytest.mark.asyncio
async def test_resolve_serves_relative_path_from_project_dir(tmp_path, mock_sel, monkeypatch):
    (tmp_path / "media").mkdir()
    f = tmp_path / "media" / "demo.mp4"
    f.write_bytes(_MP4_BYTES)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", side_effect=lambda p: p):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-stream?path=media/demo.mp4&resolve=1")
            assert resp.status == 200
            assert await resp.read() == _MP4_BYTES


@pytest.mark.asyncio
async def test_resolve_refuses_escape_outside_project(tmp_path, mock_sel, monkeypatch):
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/file-stream?path=../etc/passwd&resolve=1")
        assert resp.status == 400
        assert (await resp.json())["code"] == "outside_project"


@pytest.mark.asyncio
async def test_resolve_without_project_dir_refused(mock_sel, monkeypatch):
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/file-stream?path=media/demo.mp4&resolve=1")
        assert resp.status == 400
        assert (await resp.json())["code"] == "cannot_resolve"


# --- redaction parity: a text file wearing a media magic must not stream ---


@pytest.mark.asyncio
async def test_text_credentials_behind_forged_magic_refused(tmp_path, mock_sel):
    """file-download refuses redact()-flagged text; the same file with a
    forged media prefix must not become retrievable through file-stream."""
    f = tmp_path / "secrets.mp3"
    f.write_bytes(b"ID3" + b"AKIA_FAKE_CREDENTIAL_TEXT\n" * 10)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.dashboard.handlers.files.redact", side_effect=lambda t: "[REDACTED]"):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 400
            assert (await resp.json())["code"] == "content_redacted"


@pytest.mark.asyncio
async def test_invalid_utf8_magic_cannot_skip_the_scan(tmp_path, mock_sel):
    """The forged magic itself may be invalid UTF-8 (bare mp3 frame sync).
    The probe decodes with errors=replace, so the credential scan still runs
    and the file is refused -- a prepended byte is not a bypass."""
    f = tmp_path / "secrets2.mp3"
    f.write_bytes(b"\xff\xfb" + b"AKIA_FAKE_CREDENTIAL_TEXT\n" * 10)

    def _flag_credentials(text: str) -> str:
        return text.replace("AKIA_FAKE_CREDENTIAL_TEXT", "[REDACTED]")

    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.dashboard.handlers.files.redact", side_effect=_flag_credentials):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 400
            assert (await resp.json())["code"] == "content_redacted"


@pytest.mark.asyncio
async def test_clean_text_behind_media_magic_still_serves(tmp_path, mock_sel):
    """The probe only refuses on a redact() hit; benign text with a media
    prefix (however odd) still streams, so false positives cannot brick
    playback of unusual-but-clean files."""
    f = tmp_path / "odd.mp3"
    f.write_bytes(b"ID3" + b"just plain notes\n" * 5)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 200


@pytest.mark.asyncio
async def test_binary_media_is_not_affected_by_the_probe(tmp_path, mock_sel):
    """redact() sees the replacement-decoded probe of real media and leaves
    it unchanged, so the full body still round-trips."""
    f = tmp_path / "demo.mp4"
    f.write_bytes(_MP4_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.dashboard.handlers.files.redact", side_effect=lambda t: t):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 200
            assert await resp.read() == _MP4_BYTES


@pytest.mark.asyncio
async def test_sensitive_path_403(tmp_path, mock_sel):
    f = tmp_path / "demo.mp4"
    f.write_bytes(_MP4_BYTES)
    # The endpoint imports is_sensitive_path from kiro_crew.security at call
    # time, so the patch must target the source module, not the files module.
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.security.is_sensitive_path", return_value=True):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 403
            assert (await resp.json())["code"] == "sensitive_path"


@pytest.mark.asyncio
async def test_missing_file_404(tmp_path, mock_sel):
    missing = tmp_path / "gone.mp4"
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(missing)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={missing}")
            assert resp.status == 404
            assert (await resp.json())["code"] == "not_found"


@pytest.mark.asyncio
async def test_symlink_refused_403(tmp_path, mock_sel):
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    real = tmp_path / "real.mp4"
    real.write_bytes(_MP4_BYTES)
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(link)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={link}")
            assert resp.status == 403
            assert (await resp.json())["code"] == "symlink_refused"


@pytest.mark.asyncio
async def test_oversize_file_413_via_fstat_not_read(tmp_path, mock_sel):
    """The cap must come from fstat, never from materializing the file."""
    f = tmp_path / "big.mp4"
    f.write_bytes(_MP4_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.dashboard.handlers.files._STREAM_MAX_BYTES", 10):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 413
            assert (await resp.json())["code"] == "file_too_large"


@pytest.mark.asyncio
async def test_range_streaming_is_chunked(tmp_path, mock_sel):
    """A body larger than the chunk size arrives complete and intact."""
    payload = b"\x00\x00\x00\x20ftypisom" + os.urandom(300 * 1024)
    f = tmp_path / "long.mp4"
    f.write_bytes(payload)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-stream?path={f}")
            assert resp.status == 200
            assert await resp.read() == payload


# --- resolve=1 must never realpath a Windows-absolute shape ---


def test_resolve_passes_windows_absolute_shapes_unaltered(monkeypatch):
    """UNC (\\\\host\\share) and drive-letter paths are not project-relative.
    os.path.join would substitute them wholesale, and realpath on the joined
    result contacts the named host (SMB round-trip) BEFORE any validation.
    They must reach the validator unchanged -- its network-path gate sits
    ahead of its own realpath."""
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", "/proj")
    unc = "\\\\evil-host\\share\\x.mp4"
    with patch(
        "kiro_crew.dashboard.handlers.files.os.path.realpath",
        side_effect=AssertionError("realpath must not run on this shape"),
    ):
        assert _resolve_project_relative(unc) == (unc, None)
        assert _resolve_project_relative("C:\\Users\\x\\v.mp4") == ("C:\\Users\\x\\v.mp4", None)
        assert _resolve_project_relative("C:v.mp4") == ("C:v.mp4", None)  # drive-relative


# --- SEL: the allow decision is recorded even if the client disconnects ---


@pytest.mark.asyncio
async def test_allow_decision_logged_before_prepare(tmp_path, mock_sel):
    """A permitted read must leave a SEL event even when the client hangs up
    during response setup -- the audit record cannot depend on the stream
    finishing."""
    f = tmp_path / "demo.mp4"
    f.write_bytes(_MP4_BYTES)
    import contextlib

    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch.object(web.StreamResponse, "prepare", side_effect=ConnectionResetError):
        async with TestClient(TestServer(_make_app())) as client:
            with contextlib.suppress(Exception):
                await client.get(f"/api/file-stream?path={f}")
    outcomes = [c.kwargs.get("outcome") for c in mock_sel.log_tool_invocation.call_args_list]
    assert "success" in outcomes
