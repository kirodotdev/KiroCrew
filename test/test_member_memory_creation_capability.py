"""Member creation refuses unusable isolation before changing ownership."""

from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import member_memory_auth as auth
from kiro_crew import sandbox
from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_dir
from kiro_crew.dashboard.handlers import agents as handlers
from kiro_crew.dashboard.handlers.core import api_kirocrew_config_patch
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    memory_index_path_for,
    memory_store_dir_for,
    memory_stores_root,
    provision_member_memory,
    require_member_memory_store,
)

_UNSUPPORTED = [
    ("win32", "kas", "auto", "namespace", False, "WSL/Linux gateway"),
    ("linux", "codex", "auto", "namespace", False, "Use Kiro, Claude Code or KAS"),
    ("linux", "kas", "off", "namespace", False, "Enable agent.sandbox"),
    ("linux", "kas", "auto", "none", False, "Restore OS sandbox support"),
    ("darwin", "", "auto", "sandbox-exec", True, "Disable that delegation"),
]


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
@pytest.mark.parametrize("entrypoint", ["create", "opt_in", "sync"])
@pytest.mark.parametrize("platform,backend,mode,mechanism,delegates,remedy", _UNSUPPORTED)
async def test_dashboard_refuses_unsupported_allocation_without_side_effects(
    monkeypatch, entrypoint, platform, backend, mode, mechanism, delegates, remedy
):
    await asyncio.to_thread(
        _environment, monkeypatch, platform, backend, mode, mechanism, delegates
    )
    app = _app(monkeypatch)
    retire = AsyncMock()
    monkeypatch.setattr(handlers, "_retire_legacy_member_contexts", retire)
    checked = []
    supported = auth.private_memory_execution_supported

    def check(*, session_key):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        checked.append(session_key)
        return supported(session_key=session_key)

    monkeypatch.setattr(auth, "private_memory_execution_supported", check)
    if entrypoint == "sync":
        discovered = AgentInfo(
            name="new-member",
            filename="new-member.json",
            description="",
            model="auto",
            source="package",
        )
        monkeypatch.setattr(handlers, "list_agents", lambda: [discovered])
    before = await asyncio.to_thread(_snapshot)
    async with TestClient(TestServer(app)) as client:
        if entrypoint == "create":
            response = await client.post(
                "/api/agents", json={"name": "new-member", "kiro_agent": "kirocrew"}
            )
        elif entrypoint == "sync":
            response = await client.post("/api/agents/sync")
        else:
            response = await client.put(
                "/api/agents/reviewer", json={"provision_memory": True, "description": "Changed"}
            )
        assert response.status == 409, await response.text()
        result = await response.json()
        assert result["code"] == "member_memory_unavailable"
        assert remedy in result["error"]
    assert checked == [
        "dashboard:member-reviewer" if entrypoint == "opt_in" else "dashboard:member-new-member"
    ]
    retire.assert_not_awaited()
    assert await asyncio.to_thread(_snapshot) == before
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert await asyncio.to_thread(require_member_memory_store, loaded, "reviewer") == "default"


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("platform,backend,mode,mechanism,delegates,remedy", _UNSUPPORTED)
def test_cli_refuses_unsupported_allocation_before_persisting(
    monkeypatch, capsys, action, platform, backend, mode, mechanism, delegates, remedy
):
    from kiro_crew.cli_commands import _handle_agent

    _environment(monkeypatch, platform, backend, mode, mechanism, delegates)
    before = _snapshot()
    with pytest.raises(SystemExit) as exc:
        _handle_agent(
            argparse.Namespace(
                agent_action=action,
                name="new-member" if action == "create" else "reviewer",
                kiro_agent="kirocrew" if action == "create" else None,
                workspace="default" if action == "create" else None,
                memory_store="default" if action == "create" else None,
                provision_memory=True,
            )
        )
    assert exc.value.code == 1
    assert remedy in capsys.readouterr().err
    assert _snapshot() == before
    assert require_member_memory_store(KiroCrewConfig.load(), "reviewer") == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["create", "opt_in"])
async def test_supported_member_backend_can_allocate_with_a_different_default_backend(
    monkeypatch, entrypoint
):
    cfg = await asyncio.to_thread(
        _environment, monkeypatch, "linux", "kas", "auto", "namespace", False
    )
    cfg.agent.acp_backend = "codex"
    await asyncio.to_thread(cfg.save)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        if entrypoint == "create":
            response = await client.post(
                "/api/agents", json={"name": "new-member", "kiro_agent": "kirocrew"}
            )
            name = "new-member"
        else:
            response = await client.put("/api/agents/reviewer", json={"provision_memory": True})
            name = "reviewer"
        assert response.status == 200, await response.text()
        store = (await response.json())["memory_store"]
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert store != "default"
    assert loaded.memory_stores[store].owner_member == name
    assert await asyncio.to_thread(require_member_memory_store, loaded, name) == store


@pytest.mark.asyncio
async def test_unsupported_gateway_keeps_v1_edits_and_owned_v2_management(monkeypatch):
    cfg = await asyncio.to_thread(
        _environment, monkeypatch, "win32", "kas", "auto", "namespace", False
    )
    cfg.agents["private-member"] = KiroCrewAgentConfig()
    store = await asyncio.to_thread(provision_member_memory, cfg, "private-member")
    await asyncio.to_thread(cfg.save)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        response = await client.put("/api/agents/reviewer", json={"description": "Still V1"})
        assert response.status == 200, await response.text()
        response = await client.put("/api/agents/private-member", json={"provision_memory": True})
        assert response.status == 200, await response.text()
        assert (await response.json())["memory_store"] == store
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert loaded.agents["reviewer"].description == "Still V1"
    assert await asyncio.to_thread(require_member_memory_store, loaded, "reviewer") == "default"
    assert await asyncio.to_thread(require_member_memory_store, loaded, "private-member") == store


async def _set_provisioning(client, enabled):
    response = await client.patch(
        "/api/config/kirocrew",
        json={"path": "memory.private_provisioning_enabled", "value": enabled},
    )
    assert response.status == 200, await response.text()
    assert (await response.json())["memory"]["private_provisioning_enabled"] is enabled
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert loaded.memory.private_provisioning_enabled is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["create", "opt_in", "sync"])
async def test_owner_provisioning_pause_refuses_new_stores_and_can_be_resumed(
    monkeypatch, entrypoint
):
    await asyncio.to_thread(_environment, monkeypatch, "linux", "kas", "auto", "namespace", False)
    app = _app(monkeypatch)
    retire = AsyncMock(return_value=None)
    monkeypatch.setattr(handlers, "_retire_legacy_member_contexts", retire)
    if entrypoint == "sync":
        discovered = AgentInfo(
            name="new-member",
            filename="new-member.json",
            description="",
            model="auto",
            source="package",
        )
        monkeypatch.setattr(handlers, "list_agents", lambda: [discovered])

    async def allocate(client):
        if entrypoint == "create":
            return await client.post(
                "/api/agents", json={"name": "new-member", "kiro_agent": "kirocrew"}
            )
        if entrypoint == "sync":
            return await client.post("/api/agents/sync")
        return await client.put("/api/agents/reviewer", json={"provision_memory": True})

    async with TestClient(TestServer(app)) as client:
        await _set_provisioning(client, False)
        before = await asyncio.to_thread(_snapshot)
        response = await allocate(client)
        assert response.status == 409, await response.text()
        body = await response.json()
        assert body["code"] == "member_memory_unavailable"
        assert "paused by memory.private_provisioning_enabled" in body["error"]
        retire.assert_not_awaited()
        assert await asyncio.to_thread(_snapshot) == before

        await _set_provisioning(client, True)
        response = await allocate(client)
        assert response.status == 200, await response.text()
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    member = "reviewer" if entrypoint == "opt_in" else "new-member"
    store = await asyncio.to_thread(require_member_memory_store, loaded, member)
    assert loaded.memory_stores[store].memory_version == 2
    assert loaded.memory_stores[store].owner_member == member


@pytest.mark.parametrize("action", ["create", "update"])
def test_cli_provisioning_pause_preserves_existing_v1(monkeypatch, capsys, action):
    from kiro_crew.cli_commands import _handle_agent

    cfg = _environment(monkeypatch, "linux", "kas", "auto", "namespace", False)
    cfg.memory.private_provisioning_enabled = False
    cfg.save()
    before = _snapshot()
    with pytest.raises(SystemExit) as exc:
        _handle_agent(
            argparse.Namespace(
                agent_action=action,
                name="new-member" if action == "create" else "reviewer",
                kiro_agent="kirocrew" if action == "create" else None,
                workspace="default" if action == "create" else None,
                memory_store="default" if action == "create" else None,
                provision_memory=True,
            )
        )
    assert exc.value.code == 1
    assert "paused by memory.private_provisioning_enabled" in capsys.readouterr().err
    assert _snapshot() == before
    assert require_member_memory_store(KiroCrewConfig.load(), "reviewer") == "default"


@pytest.mark.asyncio
async def test_pause_after_admission_keeps_admitted_creation_and_preserves_pause(monkeypatch):
    await asyncio.to_thread(_environment, monkeypatch, "linux", "kas", "auto", "namespace", False)
    admitted = []

    def admit_then_pause(member):
        auth.require_member_memory_creation(member)
        admitted.append(member)
        cfg = KiroCrewConfig.load()
        cfg.memory.private_provisioning_enabled = False
        cfg.save()

    monkeypatch.setattr(handlers, "require_member_memory_creation", admit_then_pause)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        response = await client.post(
            "/api/agents", json={"name": "admitted-member", "kiro_agent": "kirocrew"}
        )
        assert response.status == 200, await response.text()
        store = (await response.json())["memory_store"]
        assert admitted == ["admitted-member"]
        loaded = await asyncio.to_thread(KiroCrewConfig.load)
        assert loaded.memory.private_provisioning_enabled is False
        assert (
            await asyncio.to_thread(require_member_memory_store, loaded, "admitted-member") == store
        )

        before = await asyncio.to_thread(_snapshot)
        response = await client.post(
            "/api/agents", json={"name": "later-member", "kiro_agent": "kirocrew"}
        )
        assert response.status == 409, await response.text()
        assert (await response.json())["code"] == "member_memory_unavailable"
        assert admitted == ["admitted-member"]
        assert await asyncio.to_thread(_snapshot) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", 0, None])
async def test_owner_provisioning_control_rejects_non_boolean_writes(monkeypatch, value):
    await asyncio.to_thread(_environment, monkeypatch, "linux", "kas", "auto", "namespace", False)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        await _set_provisioning(client, False)
        before = await asyncio.to_thread(_snapshot)
        response = await client.patch(
            "/api/config/kirocrew",
            json={"path": "memory.private_provisioning_enabled", "value": value},
        )
        assert response.status == 400, await response.text()
        assert (await response.json())["error"] == "must be a boolean"
        assert await asyncio.to_thread(_snapshot) == before


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
            assert loaded.memory.private_provisioning_enabled is True
            store = await asyncio.to_thread(require_member_memory_store, loaded, "new-member")
            assert loaded.memory_stores[store].memory_version == 2
        else:
            assert response.status == 409, body
            assert body["code"] == "member_memory_unavailable"
            assert "requires readable memory configuration" in body["error"]
            assert await asyncio.to_thread(_snapshot) == before


@pytest.mark.asyncio
async def test_provisioning_pause_preserves_v2_admission_binding_and_memory_management(monkeypatch):
    cfg = await asyncio.to_thread(
        _environment, monkeypatch, "linux", "kas", "auto", "namespace", False
    )
    cfg.agents["private-member"] = KiroCrewAgentConfig()
    store = await asyncio.to_thread(provision_member_memory, cfg, "private-member")
    await asyncio.to_thread(cfg.save)
    session_key = "dashboard:member-private-member"
    await asyncio.to_thread(auth.bind_private_session_store, session_key, store)

    async with TestClient(TestServer(_app(monkeypatch))) as client:
        await _set_provisioning(client, False)
        _, before_inventory = await asyncio.to_thread(_snapshot)
        response = await client.put("/api/agents/private-member", json={"provision_memory": True})
        assert response.status == 200, await response.text()
        assert (await response.json())["memory_store"] == store
        assert (await asyncio.to_thread(_snapshot))[1] == before_inventory
        response = await client.put("/api/agents/reviewer", json={"description": "Still V1"})
        assert response.status == 200, await response.text()

    def check_existing_memory():
        loaded = KiroCrewConfig.load()
        assert loaded.memory.private_provisioning_enabled is False
        assert require_member_memory_store(loaded, "private-member") == store
        assert require_member_memory_store(loaded, "reviewer") == "default"
        assert loaded.agents["reviewer"].description == "Still V1"
        auth.require_private_memory_execution(session_key=session_key)
        assert auth.read_private_session_store(session_key) == store
        memory = MemoryStore(
            workspace=memory_store_dir_for(store),
            index_db=memory_index_path_for(store),
            memory_version=2,
        )
        assert memory.write_preferences("Keep this member's existing guidance.")
        assert memory.read_preferences() == "Keep this member's existing guidance."
        assert auth.read_private_session_store(session_key) == store

    await asyncio.to_thread(check_existing_memory)
