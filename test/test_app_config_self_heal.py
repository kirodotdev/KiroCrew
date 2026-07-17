"""Tests for handle_app_config — graceful handling of a missing config.json.

When an app's data/ directory is wiped (e.g. by an app update), GET on the
config endpoint must not hang or error: it seeds an empty config and returns
``{}`` so the app UI gets a valid response instead of a perpetual load state.
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, app_data_dir, install_app
from kiro_crew.apps.routes import register_app_routes


def _setup_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home


def _make_app():
    app = web.Application()
    register_app_routes(app)
    return app


def _install(tmp_path, name="cfg-app"):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Config App",
        "description": "App for config endpoint testing",
        "author": "tester",
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    install_app(str(src))


@pytest.mark.asyncio
async def test_config_missing_seeds_empty_and_returns(tmp_path, monkeypatch):
    """GET with no config.json seeds an empty file and returns {}."""
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/cfg-app/config")
        assert resp.status == 200
        assert await resp.json() == {}

    # The empty config file should now exist on disk.
    config_path = app_data_dir("cfg-app") / "config.json"
    assert config_path.is_file()
    assert json.loads(config_path.read_text()) == {}


@pytest.mark.asyncio
async def test_config_not_found_app(tmp_path, monkeypatch):
    """GET for an uninstalled app returns 404."""
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/does-not-exist/config")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_config_put_then_get(tmp_path, monkeypatch):
    """PUT writes config.json and GET reads it back."""
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.put("/api/apps/cfg-app/config", json={"key": "value"})
        assert resp.status == 200
        assert (await resp.json())["ok"] is True

        resp = await client.get("/api/apps/cfg-app/config")
        assert resp.status == 200
        assert (await resp.json())["key"] == "value"
