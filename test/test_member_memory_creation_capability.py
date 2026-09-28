"""Explicit member creation is independent of OS memory-confidentiality mechanisms."""

from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import member_memory_auth as auth
from kiro_crew import sandbox
from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_dir
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.dashboard.handlers import agents as handlers
from kiro_crew.dashboard.handlers.core import api_kirocrew_config_patch
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    memory_stores_root,
    provision_member_memory,
    require_member_memory_store,
)


def _environment(monkeypatch, platform, backend, mode, mechanism, delegates):
    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = "kas"
    cfg.agent.member_acp_backend = backend
    cfg.agent.sandbox = mode
    cfg.agents["reviewer"] = KiroCrewAgentConfig()
    cfg.save()
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(sandbox, "_clamp_sandbox_mode", lambda value: value)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kwargs: mechanism)
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: delegates)
    return cfg


def _snapshot():
    root = memory_stores_root()
    return (
        (config_dir() / "config.json").read_bytes(),
        sorted(str(path.relative_to(root)) for path in root.rglob("*")) if root.exists() else [],
    )


def _app(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    monkeypatch.setattr(handlers, "list_agents", lambda: [])
    app = web.Application()
    app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
    app.router.add_post("/api/agents/sync", handlers.api_kirocrew_agents_sync)
    app.router.add_put("/api/agents/{name}", handlers.api_kirocrew_agent_update)
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("without_jsonschema", [False, True])
@pytest.mark.parametrize("shape", ["absent-memory", "memory-list", "document-list", "invalid-json"])
async def test_private_creation_refuses_degraded_config_but_allows_absent_memory(
    monkeypatch, shape, without_jsonschema
):
    from kiro_crew.config import validation

    if without_jsonschema:
        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)
    await asyncio.to_thread(_environment, monkeypatch, "linux", "kas", "auto", "namespace", False)

    def write_config_shape():
        path = config_dir() / "config.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        if shape == "absent-memory":
            document.pop("memory")
        elif shape == "memory-list":
            document["memory"] = []
        elif shape == "document-list":
            document = []
        payload = "{" if shape == "invalid-json" else json.dumps(document)
        path.write_text(payload, encoding="utf-8")

    await asyncio.to_thread(write_config_shape)
    before = await asyncio.to_thread(_snapshot)
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    if shape == "absent-memory":
        assert loaded.degraded_sections == frozenset()
    else:
        assert ("memory" if shape == "memory-list" else "*") in loaded.degraded_sections
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        response = await client.post(
            "/api/agents", json={"name": "new-member", "kiro_agent": "kirocrew"}
        )
        body = await response.json()
        if shape == "absent-memory":
            assert response.status == 200, body
            loaded = await asyncio.to_thread(KiroCrewConfig.load)
            store = await asyncio.to_thread(require_member_memory_store, loaded, "new-member")
            assert loaded.memory_stores[store].memory_version == 2
        else:
            assert response.status == 409, body
            assert body["code"] == "member_memory_unavailable"
            assert "configuration is unreadable" in body["error"]
            assert await asyncio.to_thread(_snapshot) == before


@pytest.mark.asyncio
async def test_discovery_and_update_never_provision_member_memory(monkeypatch):
    await asyncio.to_thread(_environment, monkeypatch, "linux", "kas", "auto", "namespace", False)
    app = _app(monkeypatch)
    monkeypatch.setattr(
        handlers,
        "list_agents",
        lambda: [
            AgentInfo(
                name="discovered",
                filename="discovered.json",
                description="",
                model="auto",
                source="package",
            )
        ],
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/agents/sync")
        assert response.status == 200, await response.text()
        loaded = await asyncio.to_thread(KiroCrewConfig.load)
        assert loaded.agents["discovered"].memory_store == "default"
        before = await asyncio.to_thread(_snapshot)
        response = await client.put("/api/agents/discovered", json={"provision_memory": True})
        assert response.status == 400, await response.text()
        assert await asyncio.to_thread(_snapshot) == before
        assert KiroCrewConfig.load().agents["discovered"].memory_store == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("backend", ["kas", "codex"])
async def test_explicit_creation_uses_database_not_platform_admission(
    monkeypatch, platform, backend
):
    await asyncio.to_thread(_environment, monkeypatch, platform, backend, "off", "none", False)

    def forbidden(*args, **kwargs):
        raise AssertionError("member creation queried OS sandbox capability")

    monkeypatch.setattr(sandbox, "detect_backend", forbidden)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        response = await client.post(
            "/api/agents", json={"name": "new-member", "kiro_agent": "kirocrew"}
        )
        member = "new-member"
        assert response.status == 200, await response.text()
        store = (await response.json())["memory_store"]
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    assert cfg.agents[member].member_id == cfg.memory_stores[store].owner_member_id
    assert await asyncio.to_thread(require_member_memory_store, cfg, member) == store


@pytest.mark.parametrize("backend", ["kiro", "kas", "claude", "codex"])
def test_cli_explicit_creation_with_sandbox_off(monkeypatch, backend):
    from kiro_crew.cli_commands import _handle_agent

    _environment(monkeypatch, "win32", backend, "off", "none", False)
    _handle_agent(
        argparse.Namespace(
            agent_action="create",
            name="new-member",
            kiro_agent="kirocrew",
            workspace="default",
            memory_store="default",
        )
    )
    cfg = KiroCrewConfig.load()
    assert require_member_memory_store(cfg, "new-member") != "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("interface", ["dashboard", "cli"])
@pytest.mark.parametrize("field", ["member_id", "owner_member_id"])
@pytest.mark.parametrize(
    "invalid", [[], {}, None, False, 123], ids=["list", "dict", "null", "bool", "number"]
)
async def test_creation_refuses_malformed_reserved_identity(
    monkeypatch, capsys, interface, field, invalid
):
    from kiro_crew.cli_commands import _handle_agent

    def prepare():
        cfg = _environment(monkeypatch, "win32", "kas", "off", "none", False)
        store = provision_member_memory(cfg, "reviewer")
        cfg.agents["legacy-global"] = KiroCrewAgentConfig()
        cfg.agents["legacy-named"] = KiroCrewAgentConfig(memory_store="legacy")
        cfg.memory_stores["legacy"] = MemoryStoreConfig()
        (memory_stores_root() / "legacy").mkdir()
        cfg.save()
        path = config_dir() / "config.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        record = (
            document["agents"]["reviewer"]
            if field == "member_id"
            else document["memory_stores"][store]
        )
        record[field] = invalid
        document["unrelated-setting"] = {"keep": ["exactly"]}
        path.write_text(json.dumps(document), encoding="utf-8")
        return store

    def snapshot_and_check(store):
        cfg = KiroCrewConfig.load()
        record = cfg.agents["reviewer"] if field == "member_id" else cfg.memory_stores[store]
        assert getattr(record, field) == invalid
        assert type(getattr(record, field)) is type(invalid)
        with pytest.raises(UnknownMemoryStore):
            require_member_memory_store(cfg, "reviewer")
        assert require_member_memory_store(cfg, "legacy-global") == "default"
        assert require_member_memory_store(cfg, "legacy-named") == "legacy"
        assert "new-member" not in cfg.agents
        root = memory_stores_root()
        return (
            (config_dir() / "config.json").read_bytes(),
            {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            },
        )

    store = await asyncio.to_thread(prepare)
    before = await asyncio.to_thread(snapshot_and_check, store)
    if interface == "dashboard":
        async with TestClient(TestServer(_app(monkeypatch))) as client:
            response = await client.post(
                "/api/agents", json={"name": "new-member", "kiro_agent": "kirocrew"}
            )
            assert response.status == 409, await response.text()
            body = await response.json()
            assert body["code"] == "member_memory_unavailable"
            assert "identity must be a string" in body["error"]
    else:
        with pytest.raises(SystemExit) as exc:
            await asyncio.to_thread(
                _handle_agent,
                argparse.Namespace(
                    agent_action="create",
                    name="new-member",
                    kiro_agent="kirocrew",
                    workspace="default",
                    memory_store="default",
                ),
            )
        assert exc.value.code == 1
        assert "identity must be a string" in capsys.readouterr().err
    assert await asyncio.to_thread(snapshot_and_check, store) == before
