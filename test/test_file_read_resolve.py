"""Tests for /api/file-read resolve=1 path resolution."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_file_read


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-read", api_file_read)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture()
def home_patch(tmp_path):
    real_realpath = os.path.realpath

    def fake_expanduser(p):
        return p.replace("~", str(tmp_path))

    with patch("os.path.expanduser", side_effect=fake_expanduser), patch(
        "os.path.realpath", side_effect=real_realpath
    ), patch("pathlib.Path.home", return_value=tmp_path):
        yield tmp_path


class TestFileReadResolve:
    @pytest.mark.asyncio
    async def test_resolve_joins_project_dir(self, tmp_path, mock_sel, home_patch):
        proj = tmp_path / "project"
        proj.mkdir()
        f = proj / "src" / "app.py"
        f.parent.mkdir(parents=True)
        f.write_text("print('hello')")

        with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(proj)}):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/file-read?path=src/app.py&resolve=1")
                assert resp.status == 200
                text = await resp.text()
                assert "hello" in text

    @pytest.mark.asyncio
    async def test_resolve_ignored_for_absolute_paths(self, tmp_path, mock_sel, home_patch):
        f = tmp_path / "abs.py"
        f.write_text("absolute")

        with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": "/wrong/dir"}):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/file-read?path={f}&resolve=1")
                assert resp.status == 200
                text = await resp.text()
                assert "absolute" in text

    @pytest.mark.asyncio
    async def test_resolve_without_project_dir_denies(self, mock_sel, home_patch):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KIROCREW_PROJECT_DIR", None)
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/file-read?path=src/app.py&resolve=1")
                assert resp.status == 400
                body = await resp.json()
                assert "cannot resolve" in body["error"]

    @pytest.mark.asyncio
    async def test_audit_logs_resolved_path(self, tmp_path, mock_sel, home_patch):
        proj = tmp_path / "project"
        proj.mkdir()
        f = proj / "test.txt"
        f.write_text("content")

        with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(proj)}):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/file-read?path=test.txt&resolve=1")
                assert resp.status == 200
                # Verify the resolved path (not "test.txt") was logged
                calls = [str(c) for c in mock_sel.log_tool_invocation.call_args_list]
                assert any(str(proj) in c for c in calls)

    @pytest.mark.asyncio
    async def test_resolve_rejects_traversal(self, tmp_path, mock_sel, home_patch):
        proj = tmp_path / "project"
        proj.mkdir()
        with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(proj)}):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/file-read?path=../../etc/passwd&resolve=1")
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_head_returns_200_no_body(self, tmp_path, mock_sel, home_patch):
        f = tmp_path / "exists.txt"
        f.write_text("content")

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.head(f"/api/file-read?path={f}")
            assert resp.status == 200
            body = await resp.read()
            assert body == b""

    @pytest.mark.asyncio
    async def test_head_returns_404_for_missing_file(self, tmp_path, mock_sel, home_patch):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.head(f"/api/file-read?path={tmp_path}/nope.txt")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_head_logs_success(self, tmp_path, mock_sel, home_patch):
        f = tmp_path / "logged.txt"
        f.write_text("data")

        async with TestClient(TestServer(_make_app())) as client:
            await client.head(f"/api/file-read?path={f}")
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="dashboard", tool_name="file_read", outcome="success", resources=str(f)
            )
