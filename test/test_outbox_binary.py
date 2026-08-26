"""Integration tests for binary file support in outbox notify + download handlers."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from tmpdir_helpers import short_tmp_base

from kiro_crew.dashboard.handlers import api_outbox_download, api_outbox_notify


def _make_app(state=None) -> web.Application:
    app = web.Application()
    app["state"] = state or MagicMock(_slots={})
    app.router.add_post("/api/outbox/notify", api_outbox_notify)
    app.router.add_get("/api/outbox/{filename}", api_outbox_download)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture
def outbox(tmp_path):
    # Use /tmp as a stable base — macOS tmp_path contains high-entropy directory
    # IDs that trigger the bare-secret heuristic in redact_credentials(), causing
    # api_outbox_notify to reject the path with 400 before any test logic runs.
    #
    # Removed on teardown: `mkdtemp` registers no finalizer, so each test otherwise
    # left a directory in /tmp forever. Same fixture as
    # test_outbox_notify_broadcast.py.
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp(dir=short_tmp_base()))
    odir = base / "outbox"
    odir.mkdir()
    try:
        with patch("kiro_crew.config.loader.outbox_dir", return_value=odir):
            yield odir
    finally:
        shutil.rmtree(base, ignore_errors=True)


class TestOutboxNotifyBinary:
    @pytest.mark.asyncio
    async def test_binary_mp3_accepted(self, outbox, mock_sel):
        """Binary MP3 file passes notify validation (in allowlist)."""
        mp3 = outbox / "test.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 50)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(mp3),
                "filename": "test.mp3",
                "description": "test audio",
                "size": mp3.stat().st_size,
            })
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_binary_exe_rejected(self, outbox, mock_sel):
        """Binary EXE file rejected (not in allowlist)."""
        exe = outbox / "payload.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00\x03\x00\xff\xfe\x80\x81" * 10)  # non-UTF-8 PE header
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(exe),
                "filename": "payload.exe",
                "description": "bad file",
                "size": exe.stat().st_size,
            })
            assert resp.status == 400
            data = await resp.json()
            assert "not allowed" in data["error"]

    @pytest.mark.asyncio
    async def test_text_with_secrets_rejected(self, outbox, mock_sel):
        """Text file with AWS key is rejected."""
        txt = outbox / "secrets.txt"
        txt.write_text("key=AKIAIOSFODNN7EXAMPLE")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(txt),
                "filename": "secrets.txt",
                "description": "oops",
                "size": txt.stat().st_size,
            })
            assert resp.status == 400
            data = await resp.json()
            assert "sensitive" in data["error"]


class TestOutboxDownloadBinary:
    @pytest.mark.asyncio
    async def test_download_mp3_inline(self, outbox, mock_sel):
        """MP3 served with audio/mpeg content-type and inline disposition."""
        mp3 = outbox / "standup.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/standup.mp3")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "audio/mpeg"
            assert "inline" in resp.headers["Content-Disposition"]
            assert resp.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.asyncio
    async def test_download_exe_rejected(self, outbox, mock_sel):
        """EXE file rejected by download handler (not in allowlist).

        macOS ships /etc/apache2/mime.types which classifies .exe as
        application/x-msdownload; Linux CI lacks that file and falls
        through to application/octet-stream. Compute the expected mime
        from the same call the handler makes so the assertion is
        platform-portable.
        """
        exe = outbox / "bad.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00\x03\x00\xff\xfe\x80\x81" * 10)  # non-UTF-8
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/bad.exe")
            assert resp.status == 403
            data = await resp.json()
            assert "not allowed" in data["error"]
        expected_mime = mimetypes.guess_type("bad.exe")[0] or "application/octet-stream"
        mock_sel.log_tool_invocation.assert_called_with(
            session_key="api", source="api", tool_name="file_send",
            tool_kind="download", outcome="denied",
            error=f"binary_mime_not_allowed: {expected_mime}",
        )

    @pytest.mark.asyncio
    async def test_download_text_served(self, outbox, mock_sel):
        """Clean text file served normally."""
        txt = outbox / "readme.txt"
        txt.write_text("hello world")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/readme.txt")
            assert resp.status == 200
            body = await resp.read()
            assert b"hello world" in body

    @pytest.mark.asyncio
    async def test_download_video_inline(self, outbox, mock_sel):
        """MP4 served with video/mp4 and inline disposition."""
        mp4 = outbox / "clip.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x1cftyp\xff\xfe\x80\x81" + b"\x90" * 100)  # non-UTF-8
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/clip.mp4")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "video/mp4"
            assert "inline" in resp.headers["Content-Disposition"]
