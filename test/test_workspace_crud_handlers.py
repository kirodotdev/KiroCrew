"""Unit tests for workspace CRUD HTTP handlers.

Focused on covering the handler code paths that Coverlay flagged as uncovered.
Tests call the actual async handler functions with mock requests.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload

from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
)
from kiro_crew.dashboard.handlers import (
    api_workspaces_create,
    api_workspaces_delete,
    api_workspaces_update,
)

# ── Helpers ──


def _req(body: dict | None = None, match_info: dict | None = None) -> web.Request:
    """Build a mocked aiohttp.web.Request carrying real body bytes."""
    # ``body=None`` means "malformed JSON": the capped read parses the bytes
    # itself, so the malformed case is expressed as malformed bytes.
    raw = json.dumps(body).encode() if body is not None else b"{bad json"
    return make_mocked_request(
        "POST",
        "/api/workspaces",
        match_info=match_info or {},
        headers={"Content-Length": str(len(raw))},
        payload=BodyStreamPayload(raw),
    )


def _cfg(**kw: object) -> KiroCrewConfig:
    defaults: dict = {
        "workspaces": {"default": WorkspaceConfig(dir="workspace")},
        "default_workspace": "default",
        "agents": {},
    }
    defaults.update(kw)
    return KiroCrewConfig(**defaults)


_SEL = "kiro_crew.sel.sel"
_LOAD = "kiro_crew.config.loader.KiroCrewConfig.load"
_CFGDIR = "kiro_crew.config.loader.config_dir"
_DATAHOME = "kiro_crew.dashboard.handlers.files.data_home"


# ── Create handler ──


class TestCreateHandler:
    """Cover api_workspaces_create code paths."""

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        resp = await api_workspaces_create(_req(body=None))
        assert resp.status == 400
        assert b"invalid JSON" in resp.body

    @pytest.mark.asyncio
    async def test_empty_name_returns_400(self) -> None:
        resp = await api_workspaces_create(_req({"name": "  "}))
        assert resp.status == 400
        assert b"required" in resp.body

    @pytest.mark.asyncio
    async def test_invalid_name_returns_400(self) -> None:
        resp = await api_workspaces_create(_req({"name": "../bad"}))
        assert resp.status == 400
        assert b"Invalid workspace name" in resp.body

    @pytest.mark.asyncio
    async def test_duplicate_name_returns_409(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_create(_req({"name": "default"}))
        assert resp.status == 409
        assert b"already exists" in resp.body

    @pytest.mark.asyncio
    async def test_copy_from_missing_returns_404(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_create(_req({"name": "new1", "copy_from": "nonexistent"}))
        assert resp.status == 404
        assert b"not found" in resp.body

    @pytest.mark.asyncio
    async def test_create_success(self, tmp_path: Path) -> None:
        cfg = _cfg()
        with (
            patch(_LOAD, return_value=cfg),
            patch.object(cfg, "save"),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL) as mock_sel,
        ):
            resp = await api_workspaces_create(_req({"name": "staging"}))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["name"] == "staging"
        assert "staging" in cfg.workspaces
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_with_copy_from(self, tmp_path: Path) -> None:
        cfg = _cfg()
        src_dir = tmp_path / "workspace"
        src_dir.mkdir()
        (src_dir / "data.txt").write_text("hello")
        with (
            patch(_LOAD, return_value=cfg),
            patch.object(cfg, "save"),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(_req({"name": "copied", "copy_from": "default"}))
        assert resp.status == 200
        assert "copied" in cfg.workspaces
        # Verify the copy happened (symlinks=True means it's a real copy)
        dst = tmp_path / "workspace-copied" / "data.txt"
        assert dst.exists()

    @pytest.mark.asyncio
    async def test_create_path_traversal_rejected(self, tmp_path: Path) -> None:
        cfg = _cfg()
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
        ):
            resp = await api_workspaces_create(_req({"name": "evil", "dir": "../../etc"}))
        assert resp.status == 400
        assert b"Invalid directory" in resp.body


# ── Update handler ──


class TestUpdateHandler:
    """Cover api_workspaces_update code paths."""

    @pytest.mark.asyncio
    async def test_not_found_returns_404(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_update(
                _req({"dir": "x"}, match_info={"name": "nonexistent"})
            )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_update(_req(body=None, match_info={"name": "default"}))
        assert resp.status == 400
        assert b"invalid JSON" in resp.body

    @pytest.mark.asyncio
    async def test_update_dir_success(self, tmp_path: Path) -> None:
        cfg = _cfg()
        with (
            patch(_LOAD, return_value=cfg),
            patch.object(cfg, "save"),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL) as mock_sel,
        ):
            resp = await api_workspaces_update(
                _req({"dir": "new-dir"}, match_info={"name": "default"})
            )
        assert resp.status == 200
        assert cfg.workspaces["default"].dir == "new-dir"
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_path_traversal_rejected(self, tmp_path: Path) -> None:
        cfg = _cfg()
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
        ):
            resp = await api_workspaces_update(
                _req({"dir": "../../etc"}, match_info={"name": "default"})
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_no_dir_is_noop(self, tmp_path: Path) -> None:
        cfg = _cfg()
        with (
            patch(_LOAD, return_value=cfg),
            patch.object(cfg, "save"),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_update(
                _req({"other": "field"}, match_info={"name": "default"})
            )
        assert resp.status == 200
        assert cfg.workspaces["default"].dir == "workspace"


# ── Delete handler ──


class TestDeleteHandler:
    """Cover api_workspaces_delete code paths."""

    @pytest.mark.asyncio
    async def test_not_found_returns_404(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_delete(_req(match_info={"name": "nonexistent"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_default_returns_409(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_delete(_req(match_info={"name": "default"}))
        assert resp.status == 409
        assert b"default workspace" in resp.body

    @pytest.mark.asyncio
    async def test_delete_referenced_returns_409(self) -> None:
        cfg = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),
            },
            agents={"bot": KiroCrewAgentConfig(workspace="staging")},
        )
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_delete(_req(match_info={"name": "staging"}))
        assert resp.status == 409
        assert b"referenced by agents" in resp.body

    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        cfg = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),
            },
        )
        with (
            patch(_LOAD, return_value=cfg),
            patch.object(cfg, "save"),
            patch(_SEL) as mock_sel,
        ):
            resp = await api_workspaces_delete(_req(match_info={"name": "staging"}))
        assert resp.status == 200
        assert "staging" not in cfg.workspaces
        mock_sel().log_api_access.assert_called_once()
