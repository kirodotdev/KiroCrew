"""Tests for the dashboard.link_previews config field (GET/PUT round-trip).

The field gates outbound fetches of every link the model emits, so the tests
below pin both halves of the contract: the default is OFF, and a non-boolean
value in config.json degrades to OFF rather than to something truthy.
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


def test_link_previews_default_false():
    cfg = KiroCrewConfig()
    assert cfg.dashboard.link_previews is False


def test_link_previews_save_load(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.link_previews = True
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["link_previews"] is True

    cfg2 = KiroCrewConfig.load()
    assert cfg2.dashboard.link_previews is True


def test_link_previews_load_from_existing(cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"link_previews": True}}),
        encoding="utf-8",
    )
    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.link_previews is True


def test_link_previews_absent_key_stays_false(cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.link_previews is False


def test_link_previews_non_bool_degrades_to_false(cfg_file):
    """A truthy non-bool ("true", 1) must NOT enable outbound fetching: the
    field is loaded through _safe_bool so a malformed config fails closed."""
    for bad in ("true", 1, ["yes"]):
        cfg_file.write_text(
            json.dumps({"dashboard": {"link_previews": bad}}),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.link_previews is False, bad


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
async def test_handler_put_link_previews_persists(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"link_previews": True})
        assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.link_previews is True


@pytest.mark.asyncio
async def test_handler_put_link_previews_invalid(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"link_previews": "yes"})
        assert resp.status == 400
        body = await resp.json()
        assert "boolean" in body["error"]
        assert KiroCrewConfig.load().dashboard.link_previews is False


@pytest.mark.asyncio
async def test_handler_get_returns_link_previews(handler_app, cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"link_previews": True}}),
        encoding="utf-8",
    )
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["link_previews"] is True


@pytest.mark.asyncio
async def test_handler_get_returns_link_previews_default(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["link_previews"] is False


@pytest.mark.asyncio
async def test_handler_put_round_trips_get_body(handler_app, cfg_file):
    """Both settings surfaces save with `mutate({ ...dashCfg, ...patch })`, so the
    whole GET body comes back on the PUT. Saving an unrelated toggle must not 400
    just because link_previews rode along."""
    async with TestClient(TestServer(handler_app)) as client:
        get_resp = await client.get("/api/dashboard/config")
        body = await get_resp.json()
        body["quick_send"] = True
        resp = await client.put("/api/dashboard/config", json=body)
        assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.quick_send is True
        assert cfg.dashboard.link_previews is False
