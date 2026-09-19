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

from kiro_crew import cli_commands
from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    update_config_locked,
)
from kiro_crew.dashboard.handlers import agents as handlers
from kiro_crew.memory_stores import (
    _named_store_dir,
    provision_member_memory,
    require_member_memory_store,
)

#: Ceiling on one handshake with the publication worker, in either direction.
#: A lost-run guard, never the barrier: every wait below returns the moment its
#: event is set, so only a worker that never arrives pays this. Generous because
#: the worker's step before the handshake is a REAL allocation -- a SQLite
#: database created, initialised and fsynced under ``tmp_path`` -- and on a
#: loaded Windows CI worker that alone takes several seconds. Kept under
#: pytest's per-test timeout so a genuinely lost handshake still fails as this
#: assertion, not as a killed worker.
_HANDSHAKE_CEILING_SECS = 60.0


@pytest.fixture
def owner_gateway(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    pass  # Member routing does not depend on OS isolation.
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


def _retained_allocation(store, owner):
    assert (_named_store_dir(store) / "evidence.txt").read_bytes() == b"retained allocation"
    from kiro_crew.vector_memory import read_member_database_identity

    member_id, stored = read_member_database_identity(_named_store_dir(store) / "memory.db")
    assert stored == store
    assert member_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create"])
async def test_dashboard_publication_failure_preserves_new_allocation_and_allows_retry(
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
    await asyncio.to_thread(_retained_allocation, failed_stores[0], owner)
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == "default"
    monkeypatch.setattr(handlers, "persist_member_config", original)
    response = await _dispatch(request, action)
    assert response.status == 200, response.text
    assert json.loads(response.text)["memory_store"] != failed_stores[0]


async def _wait_for_publication_worker(task, entered):
    """Wait for the tested worker phase, surfacing an earlier response or error."""
    ready = asyncio.create_task(entered.wait())
    try:
        settled, _ = await asyncio.wait(
            {task, ready}, timeout=_HANDSHAKE_CEILING_SECS, return_when=asyncio.FIRST_COMPLETED
        )
        if task in settled:
            response = task.result()
            raise AssertionError(
                f"request ended before the worker handshake: {response.status} {response.text}"
            )
        assert ready in settled, "request did not reach the worker"
    finally:
        ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create"])
@pytest.mark.parametrize("phase", ["provision", "published"])
async def test_cancelled_dashboard_request_drains_worker_before_observing_publication(
    owner_gateway, monkeypatch, action, phase
):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    allocated = []
    original_provision = handlers.provision_member_memory
    original_publish = handlers.persist_member_config

    def provision(cfg, name):
        store = original_provision(cfg, name)
        allocated.append((store, name))
        (_named_store_dir(store) / "evidence.txt").write_bytes(b"retained allocation")
        if phase == "provision":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(_HANDSHAKE_CEILING_SECS), "test did not release allocation worker"
        return store

    def publish(*args, **kwargs):
        original_publish(*args, **kwargs)
        if phase == "published":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(_HANDSHAKE_CEILING_SECS), "test did not release publication worker"

    monkeypatch.setattr(handlers, "provision_member_memory", provision)
    monkeypatch.setattr(handlers, "persist_member_config", publish)
    task = asyncio.create_task(_dispatch(_request(monkeypatch, action), action))
    try:
        await _wait_for_publication_worker(task, entered)
        task.cancel()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert len(allocated) == 1
    store, owner = allocated[0]
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    if phase == "provision":
        await asyncio.to_thread(_retained_allocation, store, owner)
        assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == "default"
    else:
        assert await asyncio.to_thread(require_member_memory_store, loaded, owner) == store
        assert (_named_store_dir(store) / "memory.db").is_file()


@pytest.mark.asyncio
async def test_rejected_provisioning_update_preserves_existing_v2(owner_gateway, monkeypatch):
    cfg = owner_gateway
    store = await asyncio.to_thread(provision_member_memory, cfg, "legacy")
    await asyncio.to_thread(cfg.save)
    monkeypatch.setattr(
        handlers, "persist_member_config", Mock(side_effect=OSError("publication refused"))
    )
    response = await _dispatch(_request(monkeypatch, "opt_in"), "opt_in")
    assert response.status == 400
    handlers.persist_member_config.assert_not_called()
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == store


@pytest.mark.parametrize("action", ["create"])
def test_cli_publication_failure_keeps_legacy_binding_and_preserves_new_store(
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
    _retained_allocation(failed_stores[0], "new-member" if action == "create" else "legacy")
    assert require_member_memory_store(KiroCrewConfig.load(), "legacy") == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed_write", "concurrent_member"])
async def test_sync_never_allocates_private_memory_on_failed_or_skipped_publication(
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
    provision = Mock(side_effect=AssertionError("discovery must not allocate private memory"))
    monkeypatch.setattr(handlers, "provision_member_memory", provision)
    before = await asyncio.to_thread(KiroCrewConfig.load)

    def write(*args, **kwargs):
        if outcome == "failed_write":
            raise OSError("sync publication refused")

        def concurrent(doc):
            doc["agents"]["new-member"] = {
                "kiro_agent": "kirocrew",
                "memory_store": "default",
                "description": "concurrent owner edit",
            }
            return doc

        update_config_locked(mutate=concurrent)
        return update_config_locked(*args, **kwargs)

    monkeypatch.setattr(handlers, "update_config_locked", write)
    request = make_mocked_request("POST", "/api/agents/sync", app=web.Application())
    response = await handlers.api_kirocrew_agents_sync(request)
    assert response.status == (200 if outcome == "concurrent_member" else 500)
    provision.assert_not_called()
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert loaded.memory_stores == before.memory_stores
    assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == "default"
    if outcome == "concurrent_member":
        assert loaded.agents["new-member"].description == "concurrent owner edit"
        assert loaded.agents["new-member"].kiro_agent == "kirocrew"
        assert (
            await asyncio.to_thread(require_member_memory_store, loaded, "new-member") == "default"
        )
    else:
        assert "new-member" not in loaded.agents


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["response", "exception", "handshake"])
async def test_publication_worker_wait_observes_request_completion(outcome):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def request():
        if outcome == "response":
            return web.json_response({"error": "setup refused"}, status=409)
        if outcome == "exception":
            raise OSError("publication setup failed")
        entered.set()
        await release.wait()
        return web.json_response({"ok": True})

    task = asyncio.create_task(request())
    try:
        if outcome == "response":
            with pytest.raises(AssertionError, match="409.*setup refused"):
                await _wait_for_publication_worker(task, entered)
        elif outcome == "exception":
            with pytest.raises(OSError, match="publication setup failed"):
                await _wait_for_publication_worker(task, entered)
        else:
            await _wait_for_publication_worker(task, entered)
            assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
