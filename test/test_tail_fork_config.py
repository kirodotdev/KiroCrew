"""Tests for tail_fork dashboard config fields (GET/PUT round-trip)."""

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


def test_tail_fork_enabled_default_false():
    cfg = KiroCrewConfig()
    assert cfg.dashboard.tail_fork_enabled is False


def test_tail_fork_save_load(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.tail_fork_enabled = True
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["tail_fork_enabled"] is True

    cfg2 = KiroCrewConfig.load()
    assert cfg2.dashboard.tail_fork_enabled is True


def test_tail_fork_load_from_existing(cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"tail_fork_enabled": True}}),
        encoding="utf-8",
    )
    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.tail_fork_enabled is True


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
async def test_handler_put_tail_fork_persists(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put(
            "/api/dashboard/config",
            json={"tail_fork_enabled": True},
        )
        assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.tail_fork_enabled is True


@pytest.mark.asyncio
async def test_handler_put_tail_fork_enabled_invalid(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"tail_fork_enabled": "yes"})
        assert resp.status == 400
        body = await resp.json()
        assert "boolean" in body["error"]


@pytest.mark.asyncio
async def test_handler_get_returns_tail_fork_fields(handler_app, cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"tail_fork_enabled": True}}),
        encoding="utf-8",
    )
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["tail_fork_enabled"] is True


@pytest.mark.asyncio
async def test_handler_get_returns_tail_fork_defaults(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["tail_fork_enabled"] is False


@pytest.mark.asyncio
async def test_handler_put_ignores_deprecated_head_handling_key(handler_app, cfg_file):
    """Backward-compat shim: a stale frontend bundle that still sends the removed
    ``tail_fork_head_handling`` key must not have its entire config save rejected.
    The key is silently ignored (no persistence, no AttributeError) rather than
    tripping the unknown-fields 400."""
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put(
            "/api/dashboard/config",
            json={"tail_fork_enabled": True, "tail_fork_head_handling": "summarize"},
        )
        assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.tail_fork_enabled is True
        assert not hasattr(cfg.dashboard, "tail_fork_head_handling")
