"""Failed member creation removes only the allocation it never published."""

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
    persist_member_config,
    provision_member_memory,
    require_member_memory_store,
    retire_unpublished_allocation,
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


def _removed_allocation(store, owner):
    # The unpublished allocation is gone; nothing in the namespace names it.
    assert not _named_store_dir(store).exists()
    loaded = KiroCrewConfig.load()
    assert store not in loaded.memory_stores
    assert all(agent.memory_store != store for agent in loaded.agents.values())


def _retained_allocation(store, owner):
    assert (_named_store_dir(store) / "evidence.txt").read_bytes() == b"retained allocation"
    from kiro_crew.vector_memory import read_member_database_identity

    member_id, stored = read_member_database_identity(_named_store_dir(store) / "memory.db")
    assert stored == store
    assert member_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create"])
async def test_dashboard_publication_failure_removes_new_allocation_and_allows_retry(
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
    await asyncio.to_thread(_removed_allocation, failed_stores[0], owner)
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == "default"
    monkeypatch.setattr(handlers, "persist_member_config", original)
    response = await _dispatch(request, action)
    assert response.status == 200, response.text
    assert json.loads(response.text)["memory_store"] != failed_stores[0]


@pytest.mark.asyncio
async def test_dashboard_failure_after_landed_publication_keeps_the_store(
    owner_gateway, monkeypatch
):
    # The failure surfaces AFTER update_config_locked wrote the member: the
    # retire step must find the store referenced on disk and leave it alone.
    request = _request(monkeypatch, "create")
    original = handlers.persist_member_config
    published = []

    def publish_then_fail(cfg, name, **kwargs):
        original(cfg, name, **kwargs)
        published.append(cfg.agents[name].memory_store)
        raise OSError("post-publication failure")

    monkeypatch.setattr(handlers, "persist_member_config", publish_then_fail)
    response = await _dispatch(request, "create")
    assert response.status == 409
    assert len(published) == 1
    store = published[0]
    assert (_named_store_dir(store) / "memory.db").is_file()
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert await asyncio.to_thread(require_member_memory_store, loaded, "new-member") == store


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
        # Cancelled before publication: the drained worker still finished the
        # allocation, and the retire step removed it because nothing on disk
        # names it.
        await asyncio.to_thread(_removed_allocation, store, owner)
        assert await asyncio.to_thread(require_member_memory_store, loaded, "legacy") == "default"
    else:
        # Cancelled after publication landed: the retire step reads the store
        # from config.json and keeps it.
        assert await asyncio.to_thread(require_member_memory_store, loaded, owner) == store
        assert (_named_store_dir(store) / "memory.db").is_file()
        await asyncio.to_thread(_retained_allocation, store, owner)


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
    _removed_allocation(failed_stores[0], "new-member" if action == "create" else "legacy")
    assert require_member_memory_store(KiroCrewConfig.load(), "legacy") == "default"


def test_every_create_purges_the_name_inside_the_locked_mutation(owner_gateway, monkeypatch):
    """The crew-teams purge is part of publication itself, not a caller's
    option: it runs for the created name after the concurrency checks and
    BEFORE the record lands, and a store that cannot purge aborts the write
    with the config untouched."""
    from kiro_crew import crew_teams
    from kiro_crew.config import loader

    cfg = owner_gateway
    cfg.agents["new-member"] = KiroCrewAgentConfig()
    provision_member_memory(cfg, "new-member")
    seen: list[tuple[str, bool]] = []

    def purge(name: str) -> None:
        # Observed from inside the mutation: the record is not on disk yet.
        on_disk = json.loads(loader.config_path().read_text(encoding="utf-8"))
        seen.append((name, name in on_disk.get("agents", {})))

    monkeypatch.setattr(crew_teams, "release_for_create", purge)
    persist_member_config(cfg, "new-member", create=True)
    assert seen == [("new-member", False)]
    assert require_member_memory_store(KiroCrewConfig.load(), "new-member") != "default"
    # An update publishes without purging: only a create can inherit.
    persist_member_config(cfg, "legacy", create=False, expected_store="default")
    assert seen == [("new-member", False)]

    cfg.agents["second"] = KiroCrewAgentConfig()
    provision_member_memory(cfg, "second")

    def refuse(name: str) -> None:
        raise crew_teams.TeamsUnavailable("teams store unavailable")

    monkeypatch.setattr(crew_teams, "release_for_create", refuse)
    with pytest.raises(crew_teams.TeamsUnavailable):
        persist_member_config(cfg, "second", create=True)
    assert "second" not in KiroCrewConfig.load().agents


def test_after_write_runs_only_once_the_registry_write_committed(owner_gateway, monkeypatch):
    """The hook the crew-teams drop rides: never before the rename, never when
    the mutation changed nothing, and a failed write never reaches it."""
    from kiro_crew.config import loader

    calls: list[bool] = []

    def observe() -> None:
        on_disk = json.loads(loader.config_path().read_text(encoding="utf-8"))
        calls.append("legacy" not in on_disk.get("agents", {}))

    def remove_legacy(doc: dict) -> dict:
        del doc["agents"]["legacy"]
        return doc

    update_config_locked(loader.config_path(), mutate=remove_legacy, after_write=observe)
    assert calls == [True]  # observed AFTER the delete landed on disk
    update_config_locked(loader.config_path(), mutate=lambda doc: None, after_write=observe)
    assert calls == [True]  # nothing committed, hook not run

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(loader, "write_config_atomically", boom)
    with pytest.raises(OSError):
        update_config_locked(
            loader.config_path(), mutate=lambda doc: {**doc, "x": 1}, after_write=observe
        )
    assert calls == [True]  # failed write, hook not run


def test_retire_keeps_a_store_the_disk_config_still_references(owner_gateway):
    # Direct contract of the retire helper: a store that config.json names is
    # never removed, and the in-memory binding is still restored for a retry.
    cfg = owner_gateway
    cfg.agents["new-member"] = KiroCrewAgentConfig()
    store = provision_member_memory(cfg, "new-member")
    persist_member_config(cfg, "new-member", create=True)
    removed = retire_unpublished_allocation(
        cfg, "new-member", store, previous_store="default", previous_member_id=""
    )
    assert removed is False
    assert (_named_store_dir(store) / "memory.db").is_file()
    assert require_member_memory_store(KiroCrewConfig.load(), "new-member") == store


def test_retire_never_touches_a_pre_existing_binding(owner_gateway):
    cfg = owner_gateway
    store = provision_member_memory(cfg, "legacy")
    persist_member_config(cfg, "legacy", create=False, expected_store="default")
    # The idempotent provisioning result equals the previous binding: no-op.
    assert (
        retire_unpublished_allocation(
            cfg, "legacy", store, previous_store=store, previous_member_id="legacy"
        )
        is False
    )
    assert (_named_store_dir(store) / "memory.db").is_file()


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
