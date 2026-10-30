"""Tests for handle_app_config — graceful handling of a missing config.json.

When an app's data/ directory is wiped (e.g. by an app update), GET on the
config endpoint must not hang or error: it seeds an empty config and returns
``{}`` so the app UI gets a valid response instead of a perpetual load state.
"""
from __future__ import annotations

import json
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

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
    # PUT is owner-gated for a dashboard subject; these rows act as the owner.
    return as_owner(app)


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
    assert json.loads(config_path.read_text(encoding="utf-8")) == {}


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


def _record_threads(monkeypatch, attr):
    """Wrap ``kiro_crew.apps.routes.<attr>`` to record the thread each call runs on."""
    import kiro_crew.apps.routes as routes_mod

    real = getattr(routes_mod, attr)
    seen: list[int] = []

    def _spy(name):
        seen.append(threading.get_ident())
        return real(name)

    monkeypatch.setattr(routes_mod, attr, _spy)
    return seen


@pytest.mark.asyncio
async def test_config_reads_the_installed_record_off_the_event_loop(tmp_path, monkeypatch):
    """GET /config resolves the installed app on a worker thread, not the loop.

    Every app iframe fetches /config on load, and get_app reads installed.json,
    app.json and the grants row from disk.
    """
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    seen = _record_threads(monkeypatch, "get_app")
    loop_thread = threading.get_ident()

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/cfg-app/config")
        assert resp.status == 200
        assert await resp.json() == {}

    assert seen, "/config never called get_app"
    assert all(t != loop_thread for t in seen)


@pytest.mark.asyncio
async def test_detail_reads_the_installed_record_off_the_event_loop(tmp_path, monkeypatch):
    """GET /api/apps/{name} resolves the installed app on a worker thread."""
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    seen = _record_threads(monkeypatch, "get_app")
    loop_thread = threading.get_ident()

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/cfg-app")
        assert resp.status == 200
        assert (await resp.json())["name"] == "cfg-app"

    assert seen, "the detail route never called get_app"
    assert all(t != loop_thread for t in seen)


@pytest.mark.asyncio
async def test_manifest_reads_off_the_event_loop(tmp_path, monkeypatch):
    """GET /manifest parses app.json on a worker thread and keeps its 200/404 answers."""
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    seen = _record_threads(monkeypatch, "get_app_manifest")
    loop_thread = threading.get_ident()

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/cfg-app/manifest")
        assert resp.status == 200
        assert (await resp.json())["name"] == "cfg-app"
        resp = await client.get("/api/apps/does-not-exist/manifest")
        assert resp.status == 404

    assert len(seen) == 2
    assert all(t != loop_thread for t in seen)
