"""Tests for quick_send dashboard config field."""

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


def test_quick_send_default_false():
    cfg = KiroCrewConfig()
    assert cfg.dashboard.quick_send is False


def test_quick_send_save_load(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.quick_send = True
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["quick_send"] is True

    cfg2 = KiroCrewConfig.load()
    assert cfg2.dashboard.quick_send is True


def test_quick_send_load_from_existing(cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {"quick_send": True}}), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.quick_send is True


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
async def test_handler_put_quick_send_true(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"quick_send": True})
        assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.quick_send is True


@pytest.mark.asyncio
async def test_handler_put_quick_send_invalid(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"quick_send": "yes"})
        assert resp.status == 400
        body = await resp.json()
        assert "boolean" in body["error"]


@pytest.mark.asyncio
async def test_handler_get_returns_quick_send(handler_app, cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {"quick_send": True}}), encoding="utf-8")
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["quick_send"] is True
