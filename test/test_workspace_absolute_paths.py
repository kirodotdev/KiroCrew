"""Tests for workspace create/update with absolute path support."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_workspaces_create, api_workspaces_update


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/workspaces", api_workspaces_create)
    app.router.add_put("/api/workspaces/{name}", api_workspaces_update)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    """Set up a clean config dir with a default workspace."""
    cfg_dir = tmp_path / ".kirocrew"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(json.dumps({
        "workspaces": {"default": {"dir": "workspace"}}
    }))
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_path", lambda: cfg_file
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: cfg_dir
    )
    # Also patch the local import in handlers
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.config_dir", lambda: cfg_dir
    )
    return cfg_dir


class TestWorkspaceCreateAbsolutePath:
    @pytest.mark.asyncio
    async def test_create_with_absolute_path(self, cfg_env, mock_sel, tmp_path):
        abs_dir = tmp_path / "my-project"
        abs_dir.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "myproj", "dir": str(abs_dir)},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_create_with_relative_path(self, cfg_env, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "relws", "dir": "workspace-relws"},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_create_relative_traversal_blocked(self, cfg_env, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "evil", "dir": "../../etc"},
            )
            assert resp.status == 400
            assert "Invalid" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_absolute_path_skips_traversal_check(self, cfg_env, mock_sel, tmp_path):
        """Absolute paths outside config_dir are allowed (not traversal)."""
        outside = tmp_path / "outside"
        outside.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "outside", "dir": str(outside)},
            )
            assert resp.status == 200


class TestWorkspaceUpdateAbsolutePath:
    @pytest.mark.asyncio
    async def test_update_with_absolute_path(self, cfg_env, mock_sel, tmp_path):
        abs_dir = tmp_path / "new-location"
        abs_dir.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/workspaces/default",
                json={"dir": str(abs_dir)},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_update_relative_traversal_blocked(self, cfg_env, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/workspaces/default",
                json={"dir": "../../../tmp"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_sensitive_path_blocked(self, cfg_env, mock_sel):
        """Absolute paths to sensitive dirs like ~/.aws are rejected."""
        import os
        sensitive = os.path.expanduser("~/.aws")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "evil", "dir": sensitive},
            )
            assert resp.status == 400
            assert "Invalid" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_update_sensitive_path_blocked(self, cfg_env, mock_sel):
        """Updating workspace to sensitive dir is rejected."""
        import os
        sensitive = os.path.expanduser("~/.ssh")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/workspaces/default",
                json={"dir": sensitive},
            )
            assert resp.status == 400
            assert "Invalid" in (await resp.json())["error"]
