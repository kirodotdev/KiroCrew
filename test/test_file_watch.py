"""Tests for the /api/file-watch SSE endpoint."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_file_watch


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-watch", api_file_watch)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture()
def tmp_file(tmp_path):
    f = tmp_path / "test.md"
    f.write_text("# Hello\n\nInitial content")
    return f


@pytest.fixture()
def home_patch(tmp_path):
    real_realpath = os.path.realpath

    def fake_expanduser(p):
        return p.replace("~", str(tmp_path))

    with patch("os.path.expanduser", side_effect=fake_expanduser), patch(
        "os.path.realpath", side_effect=real_realpath
    ), patch("pathlib.Path.home", return_value=tmp_path):
        yield tmp_path


class TestFileWatch:
    @pytest.mark.asyncio
    async def test_rejects_empty_path(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-watch?path=")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_sensitive_path(self, mock_sel, home_patch):
        sensitive = str(home_patch / ".aws" / "credentials")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-watch?path={sensitive}")
            assert resp.status == 400
            mock_sel.log_tool_invocation.assert_called()

    @pytest.mark.asyncio
    async def test_returns_sse_content_type(self, tmp_file, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-watch?path={tmp_file}")
            assert resp.status == 200
            assert "text/event-stream" in resp.content_type
            resp.close()

    @pytest.mark.asyncio
    async def test_streams_initial_content(self, tmp_file, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-watch?path={tmp_file}")
            assert resp.status == 200

            # Read the first SSE event
            buf = b""
            async for chunk in resp.content.iter_any():
                buf += chunk
                if b"\n\n" in buf:
                    break

            data_line = [line for line in buf.decode().split("\n") if line.startswith("data: ")][0]
            payload = json.loads(data_line[6:])
            assert "# Hello" in payload["content"]
            assert "Initial content" in payload["content"]
            assert "mtime" in payload

    @pytest.mark.asyncio
    async def test_sel_audit_logged(self, tmp_file, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-watch?path={tmp_file}")
            assert resp.status == 200
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="dashboard",
                tool_name="file_watch",
                outcome="success",
                resources=str(tmp_file),
            )
            resp.close()
