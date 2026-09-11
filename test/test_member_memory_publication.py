"""Failed member publication preserves data without blocking a safe retry."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from member_memory_helpers import patch_private_memory_supported

from kiro_crew import cli_commands
from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    update_config_locked,
)
from kiro_crew.dashboard.handlers import agents as handlers
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    _named_store_dir,
    provision_member_memory,
    require_member_memory_not_archived,
    require_member_memory_store,
)


@pytest.fixture
def owner_gateway(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    patch_private_memory_supported(monkeypatch)
    monkeypatch.setattr(handlers, "list_agents", lambda: [])
    cfg = KiroCrewConfig.load()
    cfg.agents["legacy"] = KiroCrewAgentConfig()
    cfg.save()
    return cfg


def _request(monkeypatch, action):
    create = action == "create"
    request = make_mocked_request(
        "POST" if create else "PUT",
        "/api/agents" if create else "/api/agents/legacy",
        app=web.Application(),
        match_info={} if create else {"name": "legacy"},
    )
    body = (
        {"name": "new-member", "kiro_agent": "kirocrew"} if create else {"provision_memory": True}
    )
    monkeypatch.setattr(request, "json", AsyncMock(return_value=body))
    return request


async def _dispatch(request, action):
    handler = (
        handlers.api_kirocrew_agents_create
        if action == "create"
        else handlers.api_kirocrew_agent_update
    )
    return await handler(request)


def _retained_and_archived(store, owner):
    assert (_named_store_dir(store) / "evidence.txt").read_bytes() == b"retained allocation"
    with pytest.raises(UnknownMemoryStore, match="archived"):
        require_member_memory_not_archived(store, expected_owner=owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "opt_in"])
async def test_dashboard_publication_failure_retires_only_new_allocation_and_allows_retry(
    owner_gateway, monkeypatch, action
):
    request = _request(monkeypatch, action)
    failed_stores = []
    original = handlers.persist_member_config

    def fail(cfg, name, **kwargs):
        store = cfg.agents[name].memory_store
        failed_stores.append(store)
        (_named_store_dir(store) / "evidence.txt").write_bytes(b"retained allocation")
        raise OSError("publication refused")

    monkeypatch.setattr(handlers, "persist_member_config", fail)
    if action == "create":
        response = await _dispatch(request, action)
        assert response.status == 409
        assert "publication refused" in json.loads(response.text)["error"]
    else:
        with pytest.raises(OSError, match="publication refused"):
            await _dispatch(request, action)
    owner = "new-member" if action == "create" else "legacy"
    assert len(failed_stores) == 1
    await asyncio.to_thread(_retained_and_archived, failed_stores[0], owner)
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == "default"
    monkeypatch.setattr(handlers, "persist_member_config", original)
    response = await _dispatch(request, action)
    assert response.status == 200, response.text
    assert json.loads(response.text)["memory_store"] != failed_stores[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "opt_in"])
@pytest.mark.parametrize("phase", ["provision", "published"])
async def test_cancelled_dashboard_request_drains_worker_before_deciding_retirement(
    owner_gateway, monkeypatch, action, phase
):
    entered = threading.Event()
    release = threading.Event()
    allocated = []
    original_provision = handlers.provision_member_memory
    original_publish = handlers.persist_member_config

    def provision(cfg, name):
        store = original_provision(cfg, name)
        allocated.append((store, name))
        (_named_store_dir(store) / "evidence.txt").write_bytes(b"retained allocation")
        if phase == "provision":
            entered.set()
            assert release.wait(5), "test did not release allocation worker"
        return store

    def publish(*args, **kwargs):
        original_publish(*args, **kwargs)
        if phase == "published":
            entered.set()
            assert release.wait(5), "test did not release publication worker"

    monkeypatch.setattr(handlers, "provision_member_memory", provision)
    monkeypatch.setattr(handlers, "persist_member_config", publish)
    task = asyncio.create_task(_dispatch(_request(monkeypatch, action), action))
    try:
        assert await asyncio.to_thread(entered.wait, 5), "request did not reach the worker"
        task.cancel()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert len(allocated) == 1
    store, owner = allocated[0]
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    if phase == "provision":
        await asyncio.to_thread(_retained_and_archived, store, owner)
        assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == "default"
    else:
        assert await asyncio.to_thread(require_member_memory_store, loaded, owner) == store
        await asyncio.to_thread(require_member_memory_not_archived, store, expected_owner=owner)


@pytest.mark.asyncio
async def test_failed_idempotent_opt_in_does_not_offer_existing_v2_to_cleanup(
    owner_gateway, monkeypatch
):
    cfg = owner_gateway
    store = await asyncio.to_thread(provision_member_memory, cfg, "legacy")
    await asyncio.to_thread(cfg.save)
    cleanup = Mock()
    monkeypatch.setattr(handlers, "retire_unpublished_member_memory_store", cleanup)
    monkeypatch.setattr(
        handlers, "persist_member_config", Mock(side_effect=OSError("publication refused"))
    )
    with pytest.raises(OSError, match="publication refused"):
        await _dispatch(_request(monkeypatch, "opt_in"), "opt_in")
    cleanup.assert_not_called()
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == store


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_replace_the_publication_failure(owner_gateway, monkeypatch):
    monkeypatch.setattr(
        handlers, "persist_member_config", Mock(side_effect=OSError("primary write failure"))
    )
    cleanup = Mock(side_effect=OSError("secondary cleanup failure"))
    monkeypatch.setattr(handlers, "retire_unpublished_member_memory_store", cleanup)
    with pytest.raises(OSError, match="primary write failure"):
        await _dispatch(_request(monkeypatch, "opt_in"), "opt_in")
    cleanup.assert_called_once()


@pytest.mark.parametrize("action", ["create", "update"])
def test_cli_publication_failure_keeps_legacy_binding_and_retires_new_store(
    owner_gateway, monkeypatch, capsys, action
):
    failed_stores = []

    def fail(cfg, name, **kwargs):
        store = cfg.agents[name].memory_store
        failed_stores.append(store)
        (_named_store_dir(store) / "evidence.txt").write_bytes(b"retained allocation")
        raise OSError("publication refused")

    monkeypatch.setattr(cli_commands, "persist_member_config", fail)
    with pytest.raises(SystemExit) as exc:
        cli_commands._handle_agent(
            argparse.Namespace(
                agent_action=action,
                name="new-member" if action == "create" else "legacy",
                kiro_agent="kirocrew" if action == "create" else None,
                workspace="default" if action == "create" else None,
                memory_store="default" if action == "create" else None,
                provision_memory=True,
            )
        )
    assert exc.value.code == 1
    assert "publication refused" in capsys.readouterr().err
    assert len(failed_stores) == 1
    _retained_and_archived(failed_stores[0], "new-member" if action == "create" else "legacy")
    assert require_member_memory_store(KiroCrewConfig.load(), "legacy") == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed_write", "concurrent_member", "cleanup_refused"])
async def test_sync_retires_unpublished_allocations_after_failed_or_skipped_publication(
    owner_gateway, monkeypatch, outcome
):
    info = AgentInfo(
        name="new-member",
        filename="new-member.json",
        description="",
        model="auto",
        source="package",
    )
    monkeypatch.setattr(handlers, "list_agents", lambda: [info])
    allocated = []
    original_provision = handlers.provision_member_memory

    def provision(cfg, name):
        store = original_provision(cfg, name)
        allocated.append(store)
        (_named_store_dir(store) / "evidence.txt").write_bytes(b"retained allocation")
        return store

    def write(*args, **kwargs):
        if outcome == "failed_write":
            raise OSError("sync publication refused")

        def concurrent(doc):
            doc["agents"]["new-member"] = {"kiro_agent": "kirocrew", "memory_store": "default"}
            return doc

        update_config_locked(mutate=concurrent)
        return update_config_locked(*args, **kwargs)

    monkeypatch.setattr(handlers, "provision_member_memory", provision)
    monkeypatch.setattr(handlers, "update_config_locked", write)
    if outcome == "cleanup_refused":
        cleanup = Mock(side_effect=OSError("retirement unavailable"))
        monkeypatch.setattr(handlers, "retire_unpublished_member_memory_store", cleanup)
    request = make_mocked_request("POST", "/api/agents/sync", app=web.Application())
    response = await handlers.api_kirocrew_agents_sync(request)
    assert response.status == (200 if outcome == "concurrent_member" else 500)
    assert len(allocated) == 1
    if outcome == "cleanup_refused":
        assert json.loads(response.text)["ok"] is False
        assert cleanup.call_count == 2
        await asyncio.to_thread(
            require_member_memory_not_archived, allocated[0], expected_owner="new-member"
        )
        retained = await asyncio.to_thread(
            (_named_store_dir(allocated[0]) / "evidence.txt").read_bytes
        )
        assert retained == b"retained allocation"
        return
    await asyncio.to_thread(_retained_and_archived, allocated[0], "new-member")
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == "default"
    if outcome == "concurrent_member":
        assert (
            await asyncio.to_thread(require_member_memory_store, loaded, "new-member") == "default"
        )
