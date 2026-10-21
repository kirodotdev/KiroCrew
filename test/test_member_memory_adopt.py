"""A new crewmate can start from the memory store a deleted crewmate left behind.

The store is adopted by binding the new crewmate to the store's own recorded
``member_id``: nothing is copied and no identity is rewritten. It is offered only
for a store no crewmate owns, and only when named.
"""

from __future__ import annotations

import unittest.mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cli import main
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.member_memory_adopt import (
    ownerless_member_stores,
    provision_or_adopt_member_memory,
)
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    UnknownMemoryStore,
    active_store_names,
    memory_store_dir_for,
    require_member_memory_store,
)


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the handlers past their independent owner-auth boundary."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )

    async def _owner(request, operation):
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.memory_admin.require_owner_dashboard_request", _owner
    )


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_delete,
        api_kirocrew_agents_create,
        api_memory_ownerless_stores,
    )

    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_delete("/api/agents/{name}", api_kirocrew_agent_delete)
    app.router.add_get("/api/memory/ownerless-stores", api_memory_ownerless_stores)
    return app


async def _create(client: TestClient, name: str, **extra) -> web.Response:
    return await client.post("/api/agents", json={"name": name, "kiro_agent": "kirocrew", **extra})


async def _created_then_deleted(client: TestClient, name: str) -> tuple[str, str]:
    """Create *name*, delete it, and return its kept ``(store, member_id)``."""
    created = await _create(client, name)
    assert created.status == 200
    body = await created.json()
    deleted = await client.delete(f"/api/agents/{name}")
    assert deleted.status == 200
    return body["memory_store"], body["member_id"]


class TestAdoptingAnOwnerlessStore:
    @pytest.mark.asyncio
    async def test_a_new_crewmate_takes_the_kept_store_and_its_identity(self) -> None:
        async with TestClient(TestServer(_app())) as client:
            store, member_id = await _created_then_deleted(client, "reviewer")

            listed = await client.get("/api/memory/ownerless-stores")
            assert listed.status == 200
            assert (await listed.json())["stores"] == [{"name": store, "former_owner": "reviewer"}]

            adopted = await _create(client, "scout", adopt_memory_store=store)
            assert adopted.status == 200, await adopted.text()
            body = await adopted.json()
            assert body["memory_store"] == store
            assert body["member_id"] == member_id

            # Once owned, it is no longer on offer.
            relisted = await client.get("/api/memory/ownerless-stores")
            assert (await relisted.json())["stores"] == []

        cfg = KiroCrewConfig.load()
        assert cfg.agents["scout"].memory_store == store
        assert cfg.agents["scout"].member_id == member_id
        record = cfg.memory_stores[store]
        assert record.owner_member_id == member_id
        assert record.owner_member == "scout"
        assert record.memory_version == 2
        # Every route that resolves a store through its live owner works again,
        # and routine maintenance covers it again.
        assert require_member_memory_store(cfg, "scout") == store
        assert store in active_store_names()
        # The same database, not a copy.
        assert memory_store_dir_for(store).joinpath("memory.db").is_file()

    @pytest.mark.asyncio
    async def test_the_kept_chat_thread_follows_the_new_crewmate(self) -> None:
        from kiro_crew.members import member_slot_key, read_dm_binding, write_dm_binding

        async with TestClient(TestServer(_app())) as client:
            created = await _create(client, "reviewer")
            body = await created.json()
            store, slug = body["memory_store"], body["member_id"]
            write_dm_binding(
                slug, member="reviewer", slot_key=member_slot_key(slug, store), memory_store=store
            )
            assert (await client.delete("/api/agents/reviewer")).status == 200
            # Kept, but it names no live crewmate.
            assert read_dm_binding(slug) is None
            assert (await _create(client, "scout", adopt_memory_store=store)).status == 200
        binding = read_dm_binding(slug)
        assert binding is not None and binding["member"] == "scout"

    @pytest.mark.asyncio
    async def test_a_same_named_create_without_asking_still_starts_fresh(self) -> None:
        async with TestClient(TestServer(_app())) as client:
            store, member_id = await _created_then_deleted(client, "reviewer")
            recreated = await _create(client, "reviewer")
            assert recreated.status == 200
            body = await recreated.json()
        assert body["memory_store"] != store
        assert body["member_id"] != member_id
        assert [row["name"] for row in ownerless_member_stores(KiroCrewConfig.load())] == [store]


class TestAdoptionIsRefused:
    @pytest.mark.asyncio
    async def test_a_store_that_has_an_owner(self) -> None:
        async with TestClient(TestServer(_app())) as client:
            owned = await _create(client, "reviewer")
            store = (await owned.json())["memory_store"]
            refused = await _create(client, "scout", adopt_memory_store=store)
            assert refused.status == 409
            assert (await refused.json())["code"] == "member_memory_unavailable"
        cfg = KiroCrewConfig.load()
        assert "scout" not in cfg.agents
        assert cfg.agents["reviewer"].memory_store == store
        assert cfg.memory_stores[store].owner_member == "reviewer"

    @pytest.mark.asyncio
    async def test_a_second_adopt_of_the_same_store(self) -> None:
        async with TestClient(TestServer(_app())) as client:
            store, _ = await _created_then_deleted(client, "reviewer")
            assert (await _create(client, "scout", adopt_memory_store=store)).status == 200
            again = await _create(client, "ranger", adopt_memory_store=store)
            assert again.status == 409
        cfg = KiroCrewConfig.load()
        assert "ranger" not in cfg.agents
        assert cfg.memory_stores[store].owner_member == "scout"

    @pytest.mark.asyncio
    async def test_a_store_that_does_not_exist(self) -> None:
        async with TestClient(TestServer(_app())) as client:
            refused = await _create(client, "scout", adopt_memory_store="member-nobody-0000")
            assert refused.status == 409
            assert (await refused.json())["code"] == "member_memory_unavailable"
        assert "scout" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_the_default_store(self) -> None:
        async with TestClient(TestServer(_app())) as client:
            refused = await _create(client, "scout", adopt_memory_store=DEFAULT_MEMORY_STORE)
            assert refused.status == 409
        assert "scout" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["../escape", "Work", 7, ["x"]])
    async def test_a_malformed_name(self, bad: object) -> None:
        async with TestClient(TestServer(_app())) as client:
            refused = await _create(client, "scout", adopt_memory_store=bad)
            assert refused.status == 400
            assert (await refused.json())["code"] == "invalid_adopt_memory_store"
        assert "scout" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_a_store_whose_database_names_another_identity(self) -> None:
        async with TestClient(TestServer(_app())) as client:
            store, _ = await _created_then_deleted(client, "reviewer")
        cfg = KiroCrewConfig.load()
        cfg.memory_stores[store].owner_member_id = "someone-else"
        cfg.save()
        cfg = KiroCrewConfig.load()
        cfg.agents["scout"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        with pytest.raises(UnknownMemoryStore, match="does not match its declaration"):
            provision_or_adopt_member_memory(cfg, "scout", store)
        assert cfg.agents["scout"].member_id == ""

    @pytest.mark.asyncio
    async def test_a_store_a_legacy_crewmate_would_share_a_slug_with(self) -> None:
        async with TestClient(TestServer(_app())) as client:
            store, member_id = await _created_then_deleted(client, "reviewer")
        cfg = KiroCrewConfig.load()
        # A crewmate with no member id derives its slug from its name.
        cfg.agents[member_id] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        assert [row["name"] for row in ownerless_member_stores(cfg)] == []
        cfg.agents["scout"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        with pytest.raises(UnknownMemoryStore, match="shares its identity"):
            provision_or_adopt_member_memory(cfg, "scout", store)


class TestTheListing:
    @pytest.mark.asyncio
    async def test_it_is_owner_gated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = []

        async def _deny(request, operation):
            seen.append(operation)
            return web.json_response({"code": "owner_only"}, status=403)

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.memory_admin.require_owner_dashboard_request", _deny
        )
        async with TestClient(TestServer(_app())) as client:
            store, _ = await _created_then_deleted(client, "reviewer")
            resp = await client.get("/api/memory/ownerless-stores")
            assert resp.status == 403
            assert store not in await resp.text()
        assert seen == ["memory.stores.ownerless"]

    def test_a_store_whose_directory_is_gone_is_not_offered(self) -> None:
        import shutil

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        store = provision_or_adopt_member_memory(cfg, "reviewer")
        del cfg.agents["reviewer"]
        cfg.save()
        assert [row["name"] for row in ownerless_member_stores(KiroCrewConfig.load())] == [store]
        shutil.rmtree(memory_store_dir_for(store))
        assert ownerless_member_stores(KiroCrewConfig.load()) == []


class TestTheCli:
    def test_agent_create_can_adopt_a_kept_store(self, capsys) -> None:
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        store = provision_or_adopt_member_memory(cfg, "reviewer")
        member_id = cfg.agents["reviewer"].member_id
        cfg.save()
        with unittest.mock.patch("sys.argv", ["kirocrew", "agent", "delete", "reviewer"]):
            main()
        argv = ["kirocrew", "agent", "create", "--name", "scout", "--adopt-memory-store", store]
        with unittest.mock.patch("sys.argv", argv):
            main()
        assert f"Memory: started from kept store {store}" in capsys.readouterr().out
        saved = KiroCrewConfig.load()
        assert saved.agents["scout"].memory_store == store
        assert saved.agents["scout"].member_id == member_id
        assert require_member_memory_store(saved, "scout") == store

    def test_agent_create_refuses_a_store_that_has_an_owner(self) -> None:
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        store = provision_or_adopt_member_memory(cfg, "reviewer")
        cfg.save()
        argv = ["kirocrew", "agent", "create", "--name", "scout", "--adopt-memory-store", store]
        with unittest.mock.patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        saved = KiroCrewConfig.load()
        assert "scout" not in saved.agents
        assert saved.memory_stores[store].owner_member == "reviewer"
