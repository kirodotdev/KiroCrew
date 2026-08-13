"""Tests for /api/file-download — raw byte streaming endpoint that fixes
the binary-file corruption seen when the dashboard download path went
through /api/file-read (UTF-8 decode with errors='replace').

Covers the regression case (docx round-trip preserves original bytes),
the security envelope (path validation, sensitive paths, symlinks, size),
and the text-redaction defense in depth.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_file_download


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-download", api_file_download)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m, \
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False):
        instance = MagicMock()
        m.return_value = instance
        yield instance


# --- Regression: binary content survives the download round-trip ---


# Minimal docx fingerprint: ZIP header + non-UTF-8 bytes. These are exactly
# the kinds of bytes that errors='replace' would mangle into U+FFFD.
_DOCX_LIKE_BYTES = (
    b"PK\x03\x04"  # ZIP local file header magic
    + bytes(range(256))  # full 0x00-0xFF range; lots of non-UTF-8 sequences
    + b"\xef\xbf\xbd"  # an actual U+FFFD that must NOT be confused with corruption
)


@pytest.mark.asyncio
async def test_binary_bytes_survive_round_trip(tmp_path, mock_sel):
    """The regression: docx-like bytes must come back identical."""
    f = tmp_path / "doc.docx"
    f.write_bytes(_DOCX_LIKE_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 200
            body = await resp.read()
            assert body == _DOCX_LIKE_BYTES, "binary bytes must round-trip without UTF-8 mangling"


@pytest.mark.asyncio
async def test_sets_attachment_disposition_and_nosniff(tmp_path, mock_sel):
    f = tmp_path / "Stores Discovery.docx"
    f.write_bytes(_DOCX_LIKE_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 200
            disp = resp.headers["Content-Disposition"]
            assert disp.startswith("attachment;")
            # RFC 5987 percent-encoded filename for the space and the dot
            assert "filename*=UTF-8''" in disp
            assert resp.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_content_type_for_known_extensions(tmp_path, mock_sel):
    cases = {
        "doc.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "report.pdf": "application/pdf",
        "image.png": "image/png",
        "data.csv": "text/csv",
    }
    for name, expected in cases.items():
        f = tmp_path / name
        f.write_bytes(_DOCX_LIKE_BYTES if not name.endswith(".csv") else b"a,b,c\n1,2,3\n")
        with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/file-download?path={f}")
                assert resp.status == 200, f"failed for {name}"
                assert resp.headers["Content-Type"] == expected, f"wrong type for {name}"


@pytest.mark.asyncio
async def test_unknown_extension_falls_back_to_octet_stream(tmp_path, mock_sel):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/octet-stream"


# --- Text files: still scanned for sensitive content (defense in depth) ---


@pytest.mark.asyncio
async def test_text_file_served_when_clean(tmp_path, mock_sel):
    f = tmp_path / "notes.txt"
    f.write_text("hello world\nno secrets here")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 200
            assert (await resp.read()) == b"hello world\nno secrets here"


@pytest.mark.asyncio
async def test_text_file_redacted_blocks_download(tmp_path, mock_sel):
    """If text content trips the redaction pass, the download is aborted —
    matches the api_outbox_download policy. The handler routes content through
    the context-aware redact() shim (which runs both the exfil-URL and
    credential passes, plus a loaded companion's extra regexes); this test
    forces redact() to mutate the text so the abort path is exercised.
    """
    f = tmp_path / "leaky.txt"
    f.write_text("ok body")
    redact_path = "kiro_crew.dashboard.handlers.files.redact"
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
            patch(redact_path, return_value="ok body REDACTED"):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 400
            payload = await resp.json()
            assert "redacted" in payload["error"]


# --- Security envelope ---


@pytest.mark.asyncio
async def test_invalid_path_rejected(mock_sel):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-download?path=/etc/passwd")
            assert resp.status == 400


@pytest.mark.asyncio
async def test_sensitive_path_rejected(tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")
    # Patch is_sensitive_path on the importing module so the alias bound at
    # files.py import-time resolves to the True-returning mock.
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True), \
         patch("kiro_crew.sel.sel") as m:
        m.return_value = MagicMock()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 403


@pytest.mark.asyncio
async def test_missing_file_404(tmp_path, mock_sel):
    missing = tmp_path / "nope.docx"
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(missing)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={missing}")
            assert resp.status == 404


@pytest.mark.asyncio
async def test_symlink_rejected(tmp_path, mock_sel):
    """O_NOFOLLOW must reject symlinked paths atomically, like api_file_raw."""
    target = tmp_path / "real.docx"
    target.write_bytes(_DOCX_LIKE_BYTES)
    link = tmp_path / "linked.docx"
    os.symlink(target, link)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(link)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={link}")
            assert resp.status == 403
            payload = await resp.json()
            assert "symlinks" in payload["error"]


@pytest.mark.asyncio
async def test_oversize_file_rejected(tmp_path, mock_sel):
    """Files larger than _MAX_UPLOAD_BYTES (50 MB) must be rejected with 413
    before the body is buffered. We simulate via stat patching to avoid
    actually writing 50 MB to disk in a unit test."""
    f = tmp_path / "huge.docx"
    f.write_bytes(b"\x00" * 1024)  # tiny on disk; we lie about the size
    real_fstat = os.fstat

    def _fake_fstat(fd):
        st = real_fstat(fd)
        # Replace st_size with one byte over the cap
        from kiro_crew.dashboard.handlers.files import _MAX_UPLOAD_BYTES
        return os.stat_result((
            st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
            _MAX_UPLOAD_BYTES + 1, st.st_atime, st.st_mtime, st.st_ctime,
        ))

    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("os.fstat", side_effect=_fake_fstat):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 413


# --- resolve=1 mirrors api_file_read semantics ---


@pytest.mark.asyncio
async def test_resolve_relative_path_within_project(tmp_path, mock_sel, monkeypatch):
    proj = tmp_path / "project"
    proj.mkdir()
    f = proj / "sub" / "doc.docx"
    f.parent.mkdir()
    f.write_bytes(_DOCX_LIKE_BYTES)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    # _validate_dashboard_path receives the resolved absolute path
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f.resolve())):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-download?path=sub/doc.docx&resolve=1")
            assert resp.status == 200
            assert (await resp.read()) == _DOCX_LIKE_BYTES


@pytest.mark.asyncio
async def test_resolve_relative_path_outside_project_rejected(tmp_path, mock_sel, monkeypatch):
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    async with TestClient(TestServer(_make_app())) as client:
        # ../../etc/passwd would resolve outside proj
        resp = await client.get("/api/file-download?path=../../etc/passwd&resolve=1")
        assert resp.status == 400
