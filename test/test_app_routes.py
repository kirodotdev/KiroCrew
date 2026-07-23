"""Tests for kiro_crew.apps.routes — REST API endpoints."""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.routes import register_app_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name="api-test-app"):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "API Test App",
        "description": "App for API testing",
        "author": "tester",
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_empty(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps")
        assert resp.status == 200
        data = await resp.json()
        assert data == []


@pytest.mark.asyncio
async def test_install_and_list(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/install", json={"source": str(src)})
        assert resp.status == 201
        data = await resp.json()
        assert data["ok"] is True

        resp = await client.get("/api/apps")
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "api-test-app"


@pytest.mark.asyncio
async def test_install_missing_source(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/install", json={"source": ""})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_get_app(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "api-test-app"


@pytest.mark.asyncio
async def test_get_app_not_found(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/nonexistent")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_get_manifest(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/api-test-app/manifest")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "api-test-app"


@pytest.mark.asyncio
async def test_enable_disable(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/enable")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

        resp = await client.post("/api/apps/api-test-app/disable")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True


@pytest.mark.asyncio
async def test_uninstall(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/uninstall")
        assert resp.status == 200

        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 404


# ---------------------------------------------------------------------------
# UI file serving — cache policy
# ---------------------------------------------------------------------------

def _make_app_source_with_ui(tmp_path, name="ui-cache-app"):
    src = _make_app_source(tmp_path, name)
    ui = src / "ui"
    ui.mkdir()
    (ui / "index.mjs").write_text("export default function App() { return null }\n")
    manifest = json.loads((src / APP_MANIFEST_FILENAME).read_text())
    manifest["ui"] = {"entry": "index.mjs", "pages": [{"route": f"/{name}", "label": name}]}
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


@pytest.mark.asyncio
async def test_ui_file_no_cache_revalidation(tmp_path, monkeypatch):
    """App UI files are served with Cache-Control: no-cache so browsers
    revalidate every load (app updates / dev edits show on plain refresh),
    and conditional requests get a body-less 304 so unchanged files stay cheap.

    Regression: the previous ``public, max-age=3600`` served every app's UI
    stale for up to an hour after an update.
    """
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source_with_ui(tmp_path)))
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/apps/ui-cache-app/ui/index.mjs")
        assert resp.status == 200
        assert resp.headers.get("Cache-Control") == "no-cache"
        assert "max-age" not in resp.headers.get("Cache-Control", "")
        last_modified = resp.headers.get("Last-Modified")
        assert last_modified  # FileResponse provides the validator

        # A revalidation request with the validator must yield 304 (no body).
        resp304 = await client.get(
            "/apps/ui-cache-app/ui/index.mjs",
            headers={"If-Modified-Since": last_modified},
        )
        assert resp304.status == 304
