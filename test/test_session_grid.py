"""Tests for session_grid dashboard config field."""

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


def test_session_grid_default_false():
    cfg = KiroCrewConfig()
    assert cfg.dashboard.session_grid is False


def test_session_grid_save_load(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.session_grid = True
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["session_grid"] is True

    cfg2 = KiroCrewConfig.load()
    assert cfg2.dashboard.session_grid is True


def test_session_grid_load_from_existing(cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {"session_grid": True}}), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.session_grid is True


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
async def test_handler_put_session_grid_true(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"session_grid": True})
        assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.session_grid is True


@pytest.mark.asyncio
async def test_handler_put_session_grid_invalid(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"session_grid": "yes"})
        assert resp.status == 400
        body = await resp.json()
        assert "boolean" in body["error"]


@pytest.mark.asyncio
async def test_handler_get_returns_session_grid(handler_app, cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {"session_grid": True}}), encoding="utf-8")
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["session_grid"] is True
