"""Tests for /api/browse-dirs endpoint."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_browse_dirs
from kiro_crew.dashboard.handlers.files import _browse_dirs_sync


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/browse-dirs", api_browse_dirs)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


class TestBrowseDirs:
    @pytest.mark.asyncio
    async def test_default_path_is_home(self, tmp_path, mock_sel):
        (tmp_path / "projects").mkdir()
        with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", str(tmp_path))):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/browse-dirs")
                data = await resp.json()
                assert data["path"] == str(tmp_path)
                names = {d["name"] for d in data["dirs"]}
                assert "projects" in names

    @pytest.mark.asyncio
    async def test_lists_subdirectories(self, tmp_path, mock_sel):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        (tmp_path / "file.txt").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-dirs?path={tmp_path}")
            data = await resp.json()
            names = [d["name"] for d in data["dirs"]]
            assert "alpha" in names
            assert "beta" in names
            assert "file.txt" not in names  # files excluded

    @pytest.mark.asyncio
    async def test_sorted_alphabetically(self, tmp_path, mock_sel):
        (tmp_path / "zebra").mkdir()
        (tmp_path / "apple").mkdir()
        (tmp_path / "mango").mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-dirs?path={tmp_path}")
            names = [d["name"] for d in (await resp.json())["dirs"]]
            assert names == ["apple", "mango", "zebra"]

    @pytest.mark.asyncio
    async def test_skips_hidden_and_excluded(self, tmp_path, mock_sel):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "src").mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-dirs?path={tmp_path}")
            names = {d["name"] for d in (await resp.json())["dirs"]}
            assert names == {"src"}

    @pytest.mark.asyncio
    async def test_returns_parent(self, tmp_path, mock_sel):
        child = tmp_path / "child"
        child.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-dirs?path={child}")
            data = await resp.json()
            assert data["parent"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_invalid_path_returns_400(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/browse-dirs?path=/nonexistent_xyz_123")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_permission_error_returns_empty_dirs(self, tmp_path, mock_sel):
        restricted = tmp_path / "restricted"
        restricted.mkdir()
        restricted.chmod(0o000)
        try:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-dirs?path={restricted}")
                data = await resp.json()
                assert data["dirs"] == []
        finally:
            restricted.chmod(0o755)

    @pytest.mark.asyncio
    async def test_scan_does_not_run_on_the_event_loop(self, tmp_path, mock_sel):
        """The directory walk must execute off the loop thread.

        Moving a scan into a thread preserves behaviour exactly, so no assertion on
        the response body can tell the offload from an inline walk — a plain revert
        keeps every other test in this file green. Record the thread the scan really
        runs on and compare it against the loop's own thread instead.
        """
        (tmp_path / "alpha").mkdir()
        loop_thread = threading.get_ident()
        ran_on: list[int] = []

        def spy(base: str, skip: set[str]) -> list[dict]:
            ran_on.append(threading.get_ident())
            return _browse_dirs_sync(base, skip)

        with patch("kiro_crew.dashboard.handlers.files._browse_dirs_sync", spy):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-dirs?path={tmp_path}")
                assert resp.status == 200
                names = {d["name"] for d in (await resp.json())["dirs"]}
                assert names == {"alpha"}
        assert ran_on, "the scan helper was never called"
        assert ran_on[0] != loop_thread
