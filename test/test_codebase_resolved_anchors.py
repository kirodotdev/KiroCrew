"""Tests for the resolved codebase anchors in ``GET /api/dashboard/config``.

The sidebar "group by codebase" lane treats a session whose ``slot.project`` is a
gateway-assigned default (the workspace dir, or a per-session dir under
``dashboard.session_folder_root``) as "no codebase". The gateway realpaths both
settings when it assigns ``slot.project``, so the GET exposes the RESOLVED anchors
(``default_project_resolved``, ``session_folder_root_resolved``) for the browser to
compare against — the raw config strings miss a ``~/...`` or symlinked value, and
miss the common case of an UNSET ``default_project`` (resolved to the workspace
dir, a path the empty raw string cannot name).
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


@pytest.fixture()
def handler_app(cfg_file, mock_sel):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    return as_owner(app)


def test_resolver_uses_workspace_dir_when_default_project_unset(tmp_path):
    # The reviewer's case: default_project unset -> the resolved anchor is the
    # realpath'd workspace dir the gateway assigns, not the empty raw string.
    # Through the facade: the helper reads is_sensitive_path and
    # validate_session_folder_root from the facade's composed globals, so it must
    # be called as the facade binds it, not imported from the owner module.
    from kiro_crew.dashboard.handlers.files import _resolved_codebase_anchors

    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg = KiroCrewConfig()
    cfg.dashboard.default_project = ""
    cfg.dashboard.session_folder_root = ""
    with patch(
        "kiro_crew.config.loader_pipeline.default_project_dir",
        return_value=os.path.realpath(str(ws)),
    ):
        default_resolved, root_resolved = _resolved_codebase_anchors(cfg)
    assert default_resolved == os.path.realpath(str(ws))
    assert root_resolved == ""


def test_resolver_realpaths_a_configured_default_project(tmp_path):
    # A configured default that exists and is non-sensitive resolves to its
    # realpath (expanduser applied), matching how slot_create stores slot.project.
    # Through the facade: the helper reads is_sensitive_path and
    # validate_session_folder_root from the facade's composed globals, so it must
    # be called as the facade binds it, not imported from the owner module.
    from kiro_crew.dashboard.handlers.files import _resolved_codebase_anchors

    proj = tmp_path / "repo"
    proj.mkdir()
    cfg = KiroCrewConfig()
    cfg.dashboard.default_project = str(proj)
    cfg.dashboard.session_folder_root = ""
    default_resolved, _ = _resolved_codebase_anchors(cfg)
    assert default_resolved == os.path.realpath(str(proj))


@pytest.mark.asyncio
async def test_get_exposes_resolved_anchors(handler_app, cfg_file, tmp_path):
    # Proves both keys are echoed by GET — without them the frontend has only the
    # raw strings and mis-groups unscoped sessions as real codebases.
    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg_file.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
    with patch(
        "kiro_crew.config.loader_pipeline.default_project_dir",
        return_value=os.path.realpath(str(ws)),
    ):
        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.get("/api/dashboard/config")
            assert resp.status == 200
            body = await resp.json()
    assert "default_project_resolved" in body
    assert "session_folder_root_resolved" in body
    assert body["default_project_resolved"] == os.path.realpath(str(ws))


@pytest.mark.asyncio
async def test_put_tolerates_round_tripped_resolved_keys(handler_app, cfg_file):
    # The settings surfaces PUT the whole GET document back, so the read-only
    # resolved keys ride along on an unrelated toggle save and must not 400.
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put(
            "/api/dashboard/config",
            json={
                "mcp_app_panel": True,
                "default_project_resolved": "/real/workspace",
                "session_folder_root_resolved": "/real/sessions",
            },
        )
        assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.mcp_app_panel is True
        # The resolved keys are read-only: they are not written to config.
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "default_project_resolved" not in raw.get("dashboard", {})
