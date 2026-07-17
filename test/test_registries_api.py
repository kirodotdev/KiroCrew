"""Tests for /api/apps/registries — federated registry management endpoint."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.routes import register_app_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Create empty config
    cfg = home / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "kiro_crew.apps.routes.config_path",
        lambda: str(cfg),
    )
    # Mock SEL
    mock_sel = MagicMock()
    monkeypatch.setattr("kiro_crew.apps.routes.sel", lambda: mock_sel)
    # Mock bridges/backend to avoid side effects
    import kiro_crew.apps.bridges as bridges_mod
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home, cfg


def _make_app():
    app = web.Application()
    register_app_routes(app)
    return app


# ---------------------------------------------------------------------------
# GET /api/apps/registries
# ---------------------------------------------------------------------------


class TestGetRegistries:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_registries(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/apps/registries")
            assert resp.status == 200
            data = await resp.json()
            assert data["registries"] == []

    @pytest.mark.asyncio
    async def test_returns_configured_registries(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({
            "registries": [
                {"name": "myorg", "repo": "MyOrgApps", "branch": "mainline"},
            ]
        }), encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/apps/registries")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["registries"]) == 1
            assert data["registries"][0]["repo"] == "MyOrgApps"


# ---------------------------------------------------------------------------
# PUT /api/apps/registries — happy path
# ---------------------------------------------------------------------------


class TestPutRegistries:
    @pytest.mark.asyncio
    async def test_add_registry(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"name": "identity", "repo": "IdentityApps", "branch": "mainline"},
                ]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert len(data["registries"]) == 1
            assert data["registries"][0]["repo"] == "IdentityApps"

            # Verify persisted to config
            saved = json.loads(cfg.read_text(encoding="utf-8"))
            assert len(saved["registries"]) == 1

    @pytest.mark.asyncio
    async def test_name_defaults_to_repo(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "SomeRepo"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["registries"][0]["name"] == "SomeRepo"
            assert data["registries"][0]["branch"] == "mainline"

    @pytest.mark.asyncio
    async def test_replace_registries(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({
            "registries": [{"name": "old", "repo": "OldRepo", "branch": "mainline"}]
        }), encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [
                    {"name": "new", "repo": "NewRepo", "branch": "dev"},
                ]},
            )
            assert resp.status == 200
            saved = json.loads(cfg.read_text(encoding="utf-8"))
            assert len(saved["registries"]) == 1
            assert saved["registries"][0]["repo"] == "NewRepo"

    @pytest.mark.asyncio
    async def test_empty_list_clears_registries(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text(json.dumps({
            "registries": [{"name": "x", "repo": "X", "branch": "mainline"}]
        }), encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": []},
            )
            assert resp.status == 200
            saved = json.loads(cfg.read_text(encoding="utf-8"))
            assert saved["registries"] == []


# ---------------------------------------------------------------------------
# PUT /api/apps/registries — validation errors
# ---------------------------------------------------------------------------


class TestPutRegistriesValidation:
    @pytest.mark.asyncio
    async def test_rejects_non_array(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": "not-an-array"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "must be an array" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_missing_repo(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"name": "foo"}]},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "repo is required" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_repo_name(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "../evil"}]},
            )
            assert resp.status == 400
            assert "invalid repo name" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_repo_with_spaces(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "my repo"}]},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_blocked_repo(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "KiroCrew"}]},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "core registry" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_branch(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "ValidRepo", "branch": "main/../evil"}]},
            )
            assert resp.status == 400
            assert "invalid branch" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_non_object_entry(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": ["not-an-object"]},
            )
            assert resp.status == 400
            assert "must be an object" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_json_body(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_invalid_name(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "ValidRepo", "name": "evil<script>"}]},
            )
            assert resp.status == 400
            assert "invalid registry name" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_returns_500_on_malformed_config(self, tmp_path, monkeypatch):
        home, cfg = _setup_env(tmp_path, monkeypatch)
        cfg.write_text("not valid json {{{", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "SomeRepo"}]},
            )
            assert resp.status == 500
            assert "malformed" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_accepts_valid_branch_with_slashes(self, tmp_path, monkeypatch):
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "MyRepo", "branch": "feature/new-apps"}]},
            )
            assert resp.status == 200
