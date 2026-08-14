"""Tests for the auto_open_git_panel dashboard config field.

The flag decides whether a chat's right side panel EXPANDS ITSELF to the Git tab
the first time the session's project directory is seen to be a git repository.
It is opt-in (default False) because a new session inherits
``dashboard.default_project``, so with it on every new chat in a git project
opens the panel. The Git tab is created either way, so the default costs the user
one click rather than a feature.

`/api/dashboard/config` validates each field explicitly and echoes a fixed dict,
so a new field has to be wired in THREE places (PUT allowlist, validation block,
GET response) — these tests cover all three, because missing any one of them
leaves the toggle silently unable to persist.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


def test_auto_open_git_panel_default_false():
    # The whole point of the change: an install that never opts in never has the
    # panel open itself.
    cfg = KiroCrewConfig()
    assert cfg.dashboard.auto_open_git_panel is False


def test_auto_open_git_panel_save_load(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.auto_open_git_panel = True
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["auto_open_git_panel"] is True

    cfg2 = KiroCrewConfig.load()
    assert cfg2.dashboard.auto_open_git_panel is True


def test_auto_open_git_panel_non_bool_falls_back(cfg_file):
    # Hand-edited config: a truthy string must not read as "on". _safe_bool is
    # what keeps a typo from silently enabling the behaviour being retired here.
    cfg_file.write_text(json.dumps({"dashboard": {"auto_open_git_panel": "yes"}}), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.auto_open_git_panel is False


def test_auto_open_git_panel_in_generated_schema():
    # The settings UI is driven by the generated schema, so the field must carry
    # its label/help through to /api/config/schema without a manual edit.
    from kiro_crew.config.schema import JSON_SCHEMA

    node = JSON_SCHEMA["properties"]["dashboard"]["properties"]["auto_open_git_panel"]
    assert node["type"] == "boolean"
    assert node["default"] is False
    assert node["x-meta"]["label"]
    assert node["x-meta"]["help"]


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
    return app


@pytest.mark.asyncio
async def test_handler_put_auto_open_git_panel_true(handler_app, cfg_file):
    # Proves the field is in the PUT allowlist AND has a validation branch.
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"auto_open_git_panel": True})
        assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.auto_open_git_panel is True


@pytest.mark.asyncio
async def test_handler_put_auto_open_git_panel_invalid(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"auto_open_git_panel": "yes"})
        assert resp.status == 400
        body = await resp.json()
        assert "boolean" in body["error"]
        assert body["code"] == "invalid_auto_open_git_panel"


@pytest.mark.asyncio
async def test_handler_get_returns_auto_open_git_panel(handler_app, cfg_file):
    # Proves the field is echoed by GET — without this the toggle would render
    # as permanently off even after a successful write.
    cfg_file.write_text(json.dumps({"dashboard": {"auto_open_git_panel": True}}), encoding="utf-8")
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["auto_open_git_panel"] is True
