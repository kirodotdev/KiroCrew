"""Private member memory creation, ownership and fail-closed execution."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_dir_link
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    config_dir,
    resolve_agent_bindings,
    update_config_locked,
)
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.memory_stores import (
    MemberAlreadyExists,
    UnknownMemoryStore,
    memory_store_dir_for,
    memory_store_version,
    memory_stores_root,
    persist_member_config,
    provision_member_memory,
    require_member_memory_store,
    require_memory_store,
    resolve_store_path,
)
from kiro_crew.vector_memory import read_member_database_identity


def _new_member(name: str = "reviewer") -> tuple[KiroCrewConfig, str]:
    cfg = KiroCrewConfig.load()
    cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    return cfg, provision_member_memory(cfg, name)


class TestPrivateOwnership:
    def test_partial_update_preserves_concurrent_fields_and_binding(self):
        cfg, store = _new_member()
        persist_member_config(cfg, "reviewer", create=True)
        stale = copy.deepcopy(cfg)
        stale.agents["reviewer"].kiro_agent = "new-template"

        def concurrent_edit(data):
            data["agents"]["reviewer"]["workspace"] = "concurrent-workspace"
            data["agents"]["reviewer"]["avatar"] = {"kind": "image", "v": 42}
            return data

        update_config_locked(mutate=concurrent_edit)
        persist_member_config(
            stale, "reviewer", expected_store=store, changed_fields={"kiro_agent"}
        )
        loaded = KiroCrewConfig.load()
        assert loaded.agents["reviewer"].kiro_agent == "new-template"
        assert loaded.agents["reviewer"].workspace == "concurrent-workspace"
        assert loaded.agents["reviewer"].avatar == {"kind": "image", "v": 42}
        assert require_member_memory_store(loaded, "reviewer") == store

    @pytest.mark.parametrize("occupied", [None, "invalid-entry", {}])
    def test_creation_refuses_every_occupied_member_key(self, occupied):
        cfg, _ = _new_member()

        def concurrent_creation(data):
            data.setdefault("agents", {})["reviewer"] = occupied
            return data

        update_config_locked(mutate=concurrent_creation)
        with pytest.raises(MemberAlreadyExists):
            persist_member_config(cfg, "reviewer", create=True)
        saved = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
        assert saved["agents"]["reviewer"] == occupied

    def test_partial_update_cannot_omit_a_new_binding_or_add_unknown_fields(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        cfg.save()
        provision_member_memory(cfg, "reviewer")
        with pytest.raises(UnknownMemoryStore, match="omitted its changed memory binding"):
            persist_member_config(
                cfg, "reviewer", expected_store="default", changed_fields={"workspace"}
            )
        with pytest.raises(UnknownMemoryStore, match="unknown fields"):
            persist_member_config(
                cfg, "reviewer", expected_store="default", changed_fields={"typo"}
            )
        assert KiroCrewConfig.load().agents["reviewer"].memory_store == "default"

    def test_member_starts_empty_and_global_files_are_unchanged(self):
        home = config_dir()
        (home / "memory.db").write_bytes(b"existing-v1-database")
        (home / "lessons.jsonl").write_text("global lessons", encoding="utf-8")
        cfg, store = _new_member()
        persist_member_config(cfg, "reviewer", create=True)
        root = memory_store_dir_for(store)
        assert {p.name for p in root.iterdir()} <= {
            "memory",
            "memory.db",
            "memory.db-wal",
            "memory.db-shm",
        }
        assert (root / "memory.db").is_file()
        assert (home / "memory.db").read_bytes() == b"existing-v1-database"
        assert (home / "lessons.jsonl").read_text(encoding="utf-8") == "global lessons"
        loaded = KiroCrewConfig.load()
        assert loaded.default_agent == "default"
        assert resolve_agent_bindings(loaded, "default").memory_store_name == "default"
        assert resolve_agent_bindings(loaded, "reviewer").memory_store_name == store
        assert memory_store_version(store) == 2
        assert memory_store_version("default") == 1

    def test_private_database_carries_durable_store_and_owner_identity(self):
        _cfg, store = _new_member()
        database = memory_stores_root() / store / "memory.db"
        assert read_member_database_identity(database) == (_cfg.agents["reviewer"].member_id, store)

    def test_members_have_distinct_stores_even_when_names_share_a_slug(self):
        cfg, first = _new_member("Code Review")
        cfg.agents["Code-Review"] = KiroCrewAgentConfig()
        second = provision_member_memory(cfg, "Code-Review")
        assert first != second
        assert require_member_memory_store(cfg, "Code Review") == first
        assert require_member_memory_store(cfg, "Code-Review") == second
        assert cfg.agents["Code Review"].member_id != cfg.agents["Code-Review"].member_id

    @pytest.mark.parametrize("replacement", ["Code Review", "Code-Review"])
    def test_deleted_member_identity_stays_reserved_by_retained_store(self, replacement):
        from kiro_crew.execution_context import (
            member_config_for_id,
            resolve_member_execution,
            validate_execution,
        )
        from kiro_crew.memory import MemoryStore
        from kiro_crew.vector_memory import open_member_database

        global_db = config_dir() / "memory.db"
        global_db.write_bytes(b"existing-global-v1")
        cfg, old_store = _new_member("Code Review")
        persist_member_config(cfg, "Code Review", create=True)
        captured = resolve_member_execution(cfg, "Code Review")
        old_path = memory_stores_root() / old_store / "memory.db"
        old_db = open_member_database(old_path, member_id=captured.member_id, store_id=old_store)
        try:
            MemoryStore(
                workspace=old_path.parent, memory_version=2, vector_store=old_db
            ).append_history("The retired member remembers aurora.")
        finally:
            old_db.close()
        old_bytes = old_path.read_bytes()

        # Member deletion retains the store declaration and its learned data.
        def delete_member(data):
            del data["agents"]["Code Review"]
            return data

        update_config_locked(mutate=delete_member)
        cfg = KiroCrewConfig.load()
        cfg.agents[replacement] = KiroCrewAgentConfig()
        new_store = provision_member_memory(cfg, replacement)
        persist_member_config(cfg, replacement, create=True)
        loaded = KiroCrewConfig.load()
        new_id = loaded.agents[replacement].member_id

        with pytest.raises(UnknownMemoryStore, match="identity is missing"):
            member_config_for_id(loaded, captured.member_id)
        assert new_id != captured.member_id
        assert new_store != old_store
        assert loaded.memory_stores[old_store].owner_member_id == captured.member_id
        assert validate_execution(captured) == captured
        assert old_path.read_bytes() == old_bytes
        new_path = memory_stores_root() / new_store / "memory.db"
        assert read_member_database_identity(new_path) == (new_id, new_store)
        new_db = open_member_database(new_path, member_id=new_id, store_id=new_store)
        try:
            assert not MemoryStore(
                workspace=new_path.parent, memory_version=2, vector_store=new_db
            ).read_recent_history()
        finally:
            new_db.close()
        assert global_db.read_bytes() == b"existing-global-v1"
        assert memory_store_version("default") == 1

    def test_selecting_member_as_default_does_not_grant_global_memory(self):
        cfg, store = _new_member()
        cfg.default_agent = "reviewer"
        assert resolve_agent_bindings(cfg).memory_store_name == store
        assert resolve_agent_bindings(cfg, "default").memory_store_name == "default"

    def test_store_listing_follows_exact_member_avatar_and_updates_without_rebinding(self):
        from kiro_crew.dashboard.handlers.memory_admin import _list_stores_blocking

        cfg, first = _new_member("Code Review")
        cfg.agents["Code Review"].avatar = {"kind": "image", "v": 11}
        persist_member_config(cfg, "Code Review", create=True)
        cfg = KiroCrewConfig.load()
        cfg.agents["Code-Review"] = KiroCrewAgentConfig(avatar={"kind": "image", "v": 22})
        second = provision_member_memory(cfg, "Code-Review")
        persist_member_config(cfg, "Code-Review", create=True)

        rows = {row["name"]: row for row in _list_stores_blocking()}
        assert rows[first]["owner_member"] == "Code Review"
        assert rows[first]["owner_avatar"] == {"kind": "image", "v": 11}
        assert rows[second]["owner_member"] == "Code-Review"
        assert rows[second]["owner_avatar"] == {"kind": "image", "v": 22}
        assert rows["default"]["owner_avatar"] == {}

        cfg = KiroCrewConfig.load()
        cfg.agents["Code Review"].avatar = {"kind": "image", "v": 33}
        persist_member_config(cfg, "Code Review", create=False, expected_store=first)
        refreshed = {row["name"]: row for row in _list_stores_blocking()}
        assert refreshed[first]["owner_avatar"] == {"kind": "image", "v": 33}
        assert refreshed[second]["owner_avatar"] == rows[second]["owner_avatar"]
        assert require_member_memory_store(KiroCrewConfig.load(), "Code Review") == first

    def test_deleted_member_store_is_not_listed_for_the_picker(self):
        """The store record stays (its member id is reserved), but no route can
        read it, so listing it hands the picker a row whose every click is a 503."""
        from kiro_crew.dashboard.handlers.memory_admin import _list_stores_blocking

        cfg, store = _new_member("reviewer")
        persist_member_config(cfg, "reviewer", create=True)
        assert store in {row["name"] for row in _list_stores_blocking()}

        def delete_member(data):
            del data["agents"]["reviewer"]
            return data

        update_config_locked(mutate=delete_member)
        names = {row["name"] for row in _list_stores_blocking()}
        assert store not in names and "default" in names
        assert store in KiroCrewConfig.load().memory_stores  # reservation intact

    @pytest.mark.parametrize("junk", [[], {"id": 1}, 7, None])
    def test_non_string_identities_in_config_never_crash_the_picker(self, junk):
        """config.json is hand-editable: a ``member_id: []`` (or a junk
        ``owner_member_id``) is kept as written so the resolvers refuse it and
        doctor can show it, but the picker must not hash it into an unhashable
        TypeError that turns ``/api/memory/stores`` into a 500."""
        from kiro_crew.dashboard.handlers.memory_admin import _list_stores_blocking

        cfg, store = _new_member("reviewer")
        persist_member_config(cfg, "reviewer", create=True)

        def corrupt(data):
            data["agents"]["reviewer"]["member_id"] = junk
            data["agents"]["bystander"] = {"member_id": junk}
            data["memory_stores"][store]["owner_member_id"] = junk
            return data

        update_config_locked(mutate=corrupt)
        loaded = KiroCrewConfig.load()
        assert loaded.agents["reviewer"].member_id == junk
        assert loaded.agents["bystander"].member_id == junk
        assert loaded.memory_stores[store].owner_member_id == junk
        names = {row["name"] for row in _list_stores_blocking()}
        # A damaged identity is not a living owner, so the record is omitted
        # like a deleted member's store; the default store still lists.
        assert store not in names and "default" in names

    def test_mcp_advisory_binding_does_not_open_hidden_files_but_runtime_does(self):
        cfg, store = _new_member()
        (memory_stores_root() / store / "memory.db").unlink()
        assert (
            resolve_agent_bindings(cfg, "reviewer", validate_memory_files=False).memory_store_name
            == store
        )
        with pytest.raises(UnknownMemoryStore, match="database is missing or unreadable"):
            resolve_agent_bindings(cfg, "reviewer")

    @pytest.mark.parametrize("binding", ["", "missing", "../escape", None])
    def test_broken_member_binding_never_selects_global_or_initialization(self, binding):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store=binding)
        with pytest.raises(UnknownMemoryStore):
            resolve_agent_bindings(cfg, "reviewer")

    @pytest.mark.parametrize("binding", ["default", "legacy"])
    def test_existing_legacy_member_keeps_its_exact_binding_and_database(self, binding):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store=binding)
        cfg.default_agent = "reviewer"
        cfg.memory_stores["legacy"] = MemoryStoreConfig()
        cfg.save()
        root = memory_stores_root() / "legacy"
        root.mkdir(parents=True)
        database = root / "memory.db"
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE retained (text TEXT)")
            connection.execute("INSERT INTO retained VALUES ('existing legacy knowledge')")
            connection.commit()
        finally:
            connection.close()
        before = database.read_bytes()
        assert resolve_agent_bindings(cfg, "reviewer").memory_store_name == binding
        assert resolve_agent_bindings(cfg).memory_store_name == binding
        assert require_member_memory_store(cfg, "reviewer") == binding
        assert database.read_bytes() == before
        assert not (root / "member-memory.json").exists()

    @pytest.mark.parametrize("binding", ["default", "legacy"])
    def test_legacy_advisory_resolution_does_not_probe_private_or_database_files(
        self, monkeypatch, binding
    ):
        import kiro_crew.memory_stores as stores

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store=binding)
        cfg.memory_stores["legacy"] = MemoryStoreConfig()

        def unexpected(*args):
            raise AssertionError("configuration resolution opened memory files")

        monkeypatch.setattr(stores, "_require_legacy_store_files", unexpected)
        result = resolve_agent_bindings(cfg, "reviewer", validate_memory_files=False)
        assert result.memory_store_name == binding

    @pytest.mark.parametrize("damage", ["database", "record", "owner", "version"])
    def test_private_damage_is_never_classified_as_legacy(self, damage):
        cfg, store = _new_member()
        root = memory_stores_root() / store
        if damage == "database":
            (root / "memory.db").write_bytes(b"invalid")
        elif damage == "record":
            del cfg.memory_stores[store]
        elif damage == "owner":
            cfg.memory_stores[store].owner_member_id = ""
        else:
            cfg.memory_stores[store].memory_version = 1
        with pytest.raises(UnknownMemoryStore):
            resolve_agent_bindings(cfg, "reviewer")

    def test_unrelated_nonregular_database_never_opens_sqlite(self, monkeypatch):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        database = memory_stores_root() / "malformed-peer" / "memory.db"
        database.mkdir(parents=True)

        def forbidden(*args, **kwargs):
            raise AssertionError("nonregular database reached SQLite")

        monkeypatch.setattr(sqlite3, "connect", forbidden)
        assert resolve_agent_bindings(cfg, "reviewer").memory_store_name == "default"

    def test_selected_legacy_nonregular_database_refuses_before_sqlite(self, monkeypatch):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store="legacy")
        cfg.memory_stores["legacy"] = MemoryStoreConfig()
        database = memory_stores_root() / "legacy" / "memory.db"
        database.mkdir(parents=True)

        def forbidden(*args, **kwargs):
            raise AssertionError("nonregular database reached SQLite")

        monkeypatch.setattr(sqlite3, "connect", forbidden)
        with pytest.raises(UnknownMemoryStore, match="unreadable"):
            resolve_agent_bindings(cfg, "reviewer")

    def test_unowned_declaration_cannot_downgrade_private_database(self):
        cfg, store = _new_member()
        root = memory_stores_root() / store
        cfg.memory_stores[store] = MemoryStoreConfig()
        before = (root / "memory.db").read_bytes()
        with pytest.raises(UnknownMemoryStore, match="member"):
            require_memory_store(store, config=cfg)
        assert (root / "memory.db").read_bytes() == before

    def test_corrupt_peer_does_not_disable_legacy_members_or_global(self):
        cfg, peer = _new_member("other")
        (memory_stores_root() / peer / "memory.db").write_text("{invalid", encoding="utf-8")
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        assert require_member_memory_store(cfg, "reviewer") == "default"
        assert require_member_memory_store(cfg, "default") == "default"
        with pytest.raises(UnknownMemoryStore):
            require_member_memory_store(cfg, "other")

    @pytest.mark.parametrize("owner,version", [("other", 2), ("reviewer", 1)])
    def test_owned_invalid_binding_never_advertises_initialization(self, owner, version):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store="owned")
        cfg.memory_stores["owned"] = MemoryStoreConfig(owner_member=owner, memory_version=version)
        before = copy.deepcopy(cfg)
        for operation in (require_member_memory_store, provision_member_memory):
            with pytest.raises(UnknownMemoryStore) as refused:
                operation(cfg, "reviewer")
            assert "Create private memory" not in str(refused.value)
        assert cfg == before

    def test_shared_binding_is_refused_for_both_members(self):
        cfg, store = _new_member()
        cfg.agents["intruder"] = KiroCrewAgentConfig(memory_store=store)
        for name in ("reviewer", "intruder"):
            with pytest.raises(UnknownMemoryStore):
                require_member_memory_store(cfg, name)

    def test_missing_directory_is_not_recreated_on_resolution(self):
        cfg, store = _new_member()
        root = memory_stores_root() / store
        import shutil

        shutil.rmtree(root)
        with pytest.raises(UnknownMemoryStore, match="missing"):
            require_member_memory_store(cfg, "reviewer")
        assert not root.exists()

    @pytest.mark.parametrize("missing", [True, False])
    def test_missing_or_invalid_database_is_not_recreated(self, missing):
        cfg, store = _new_member()
        database = memory_stores_root() / store / "memory.db"
        if missing:
            database.unlink()
        else:
            database.write_bytes(b"invalid database")
        with pytest.raises(UnknownMemoryStore, match="database is missing or unreadable"):
            require_member_memory_store(cfg, "reviewer")
        assert database.exists() is not missing

    def test_store_link_to_another_member_is_refused(self):
        cfg, store = _new_member()
        cfg.agents["other"] = KiroCrewAgentConfig()
        other = provision_member_memory(cfg, "other")
        root = memory_stores_root() / store
        import shutil

        shutil.rmtree(root)
        make_dir_link(root, memory_stores_root() / other)
        with pytest.raises(UnknownMemoryStore, match="refusing a link"):
            require_member_memory_store(cfg, "reviewer")

    def test_undeclared_store_never_resolves_to_configured_or_global_default(self):
        cfg = KiroCrewConfig.load()
        cfg.memory_stores["legacy"] = MemoryStoreConfig()
        cfg.default_memory_store = "legacy"
        cfg.save()
        with pytest.raises(UnknownMemoryStore, match="not declared"):
            resolve_store_path("missing")

    def test_existing_private_store_cannot_be_reset_by_provision(self):
        cfg, store = _new_member()
        assert provision_member_memory(cfg, "reviewer") == store
        assert len([v for v in cfg.memory_stores.values() if v.owner_member == "reviewer"]) == 1

    def test_legacy_named_store_is_not_adopted_or_copied(self):
        cfg = KiroCrewConfig.load()
        cfg.memory_stores["legacy"] = MemoryStoreConfig()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store="legacy")
        cfg.save()
        legacy = memory_store_dir_for("legacy")
        legacy.mkdir(parents=True)
        (legacy / "lessons.jsonl").write_text("legacy content", encoding="utf-8")
        store = provision_member_memory(cfg, "reviewer")
        assert store != "legacy"
        assert (legacy / "lessons.jsonl").read_text(encoding="utf-8") == "legacy content"
        assert not (memory_stores_root() / store / "lessons.jsonl").exists()
        assert memory_store_version("legacy") == 1

    def test_concurrent_creation_has_one_winner_and_preserves_other_settings(self):
        cfg = KiroCrewConfig.load()
        cfg.save()
        first, _ = _new_member()
        second, _ = _new_member()

        def publish(snapshot):
            try:
                persist_member_config(snapshot, "reviewer", create=True)
                return True
            except UnknownMemoryStore:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, [first, second]))
        assert sorted(results) == [False, True]
        loaded = KiroCrewConfig.load()
        assert len([v for v in loaded.memory_stores.values() if v.owner_member == "reviewer"]) == 1
        require_member_memory_store(loaded, "reviewer")

    def test_metadata_edit_does_not_resurrect_a_concurrently_removed_member(self):
        cfg, store = _new_member()
        persist_member_config(cfg, "reviewer", create=True)
        stale_editor = KiroCrewConfig.load()
        stale_editor.agents["reviewer"].description = "Unsaved edit"
        latest = KiroCrewConfig.load()
        del latest.agents["reviewer"]
        latest.save()
        with pytest.raises(UnknownMemoryStore, match="removed concurrently"):
            persist_member_config(stale_editor, "reviewer", expected_store=store)
        assert "reviewer" not in KiroCrewConfig.load().agents


@pytest.fixture
def owner_crud_app(monkeypatch):
    import kiro_crew.dashboard.handlers.agents as handlers

    pass  # Member routing does not depend on OS isolation.
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    monkeypatch.setattr(handlers, "list_agents", lambda: [])
    app = web.Application()
    app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", handlers.api_kirocrew_agent_update)
    return app


class TestMemberMemoryUserFlows:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid",
        [
            {"session_color": "invalid-color"},
            {"avatar": {"kind": "invalid"}},
            {"avatar": {"kind": "image", "promote": True}},
        ],
    )
    async def test_rejected_opt_in_does_not_leave_private_evidence(self, owner_crud_app, invalid):
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        await asyncio.to_thread(cfg.save)
        root = memory_stores_root()
        before = set(root.iterdir()) if root.exists() else set()
        async with TestClient(TestServer(owner_crud_app)) as client:
            response = await client.put(
                "/api/agents/reviewer", json={"provision_memory": True, **invalid}
            )
            assert response.status == 400, await response.text()
            loaded = await asyncio.to_thread(KiroCrewConfig.load)
            assert loaded.agents["reviewer"].memory_store == "default"
            assert (
                await asyncio.to_thread(require_member_memory_store, loaded, "reviewer")
                == "default"
            )
            assert (set(root.iterdir()) if root.exists() else set()) == before
            response = await client.put("/api/agents/reviewer", json={"provision_memory": True})
            assert response.status == 400, await response.text()
            assert (await response.json())["code"] == "member_memory_creation_only"

    @pytest.mark.asyncio
    async def test_create_edit_and_refuse_rebinding(self, owner_crud_app):
        async with TestClient(TestServer(owner_crud_app)) as client:
            response = await client.post(
                "/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"}
            )
            assert response.status == 200, await response.text()
            store = (await response.json())["memory_store"]
            response = await client.put(
                "/api/agents/reviewer",
                json={"description": "Checks patches", "memory_store": store},
            )
            assert response.status == 200, await response.text()
            response = await client.put("/api/agents/reviewer", json={"memory_store": "default"})
            assert response.status == 409
        loaded = await asyncio.to_thread(KiroCrewConfig.load)
        assert loaded.agents["reviewer"].description == "Checks patches"
        assert loaded.agents["reviewer"].memory_store == store

    @pytest.mark.asyncio
    async def test_legacy_member_metadata_edits_do_not_initialize_memory(self, owner_crud_app):
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        await asyncio.to_thread(cfg.save)
        async with TestClient(TestServer(owner_crud_app)) as client:
            response = await client.put(
                "/api/agents/reviewer", json={"description": "Checks patches"}
            )
            assert response.status == 200
            unchanged = await asyncio.to_thread(KiroCrewConfig.load)
            assert unchanged.agents["reviewer"].memory_store == "default"
            assert (
                await asyncio.to_thread(require_member_memory_store, unchanged, "reviewer")
                == "default"
            )
            response = await client.put("/api/agents/reviewer", json={"provision_memory": True})
            assert response.status == 400, await response.text()
        loaded = await asyncio.to_thread(KiroCrewConfig.load)
        await asyncio.to_thread(require_member_memory_store, loaded, "reviewer")

    def test_cli_creates_private_memory_and_refuses_rebinding(self, capsys, monkeypatch):
        from kiro_crew.cli_commands import _handle_agent

        pass  # Member routing does not depend on OS isolation.
        _handle_agent(
            argparse.Namespace(
                agent_action="create",
                name="reviewer",
                kiro_agent="kirocrew",
                workspace="default",
                memory_store="default",
            )
        )
        loaded = KiroCrewConfig.load()
        require_member_memory_store(loaded, "reviewer")
        with pytest.raises(SystemExit) as exc:
            _handle_agent(
                argparse.Namespace(
                    agent_action="update",
                    name="reviewer",
                    kiro_agent=None,
                    workspace=None,
                    memory_store="default",
                )
            )
        assert exc.value.code == 1
        assert "cannot be rebound" in capsys.readouterr().err

    @pytest.mark.asyncio
    @pytest.mark.parametrize("awaiting_approval", [False, True])
    async def test_open_active_private_thread_reuses_assignment_without_interrupting(
        self, owner_crud_app, tmp_path, monkeypatch, awaiting_approval
    ):
        from unittest.mock import AsyncMock

        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.handlers import members as handlers
        from kiro_crew.member_memory_auth import read_private_session_store

        cfg, store = await asyncio.to_thread(_new_member)
        await asyncio.to_thread(persist_member_config, cfg, "reviewer", create=True)
        state = _make_state(tmp_path)
        owner_crud_app["state"] = state
        owner_crud_app.router.add_post("/api/members/{slug}/thread", handlers.api_member_thread)
        pin = AsyncMock(wraps=handlers.pin_private_agent_store)
        monkeypatch.setattr(handlers, "pin_private_agent_store", pin)
        async with TestClient(TestServer(owner_crud_app)) as client:
            response = await client.post("/api/members/reviewer/thread")
            assert response.status == 200, await response.text()
            first = await response.json()
            slot = state._slots[first["slot_key"]]
            key = f"dashboard:{slot.key}"
            pin.reset_mock()
            task = asyncio.current_task()
            approval = asyncio.get_running_loop().create_future()
            slot.task = task
            if awaiting_approval:
                slot._approval_futures["pending-test"] = approval
            original_history_flag = slot._memory_assignment_from_history
            try:
                response = await client.post("/api/members/reviewer/thread")
                assert response.status == 200, await response.text()
                assert await response.json() == first
                assert slot.task is task
                assert not approval.done()
                assert slot.memory_store == store
                assert slot._memory_assignment_from_history == original_history_flag
                assert await asyncio.to_thread(read_private_session_store, key) == store
                pin.assert_not_awaited()
            finally:
                slot.task = None
                slot._approval_futures.pop("pending-test", None)
                approval.cancel()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid", ["missing", "different", "unreadable", "slot_store", "linked"]
    )
    async def test_open_active_private_thread_refuses_unverified_identity(
        self, owner_crud_app, tmp_path, monkeypatch, invalid
    ):
        from unittest.mock import AsyncMock

        from chat_test_helpers import _make_state

        from kiro_crew import member_memory_auth
        from kiro_crew.dashboard.handlers import members as handlers

        cfg, store = await asyncio.to_thread(_new_member)
        await asyncio.to_thread(persist_member_config, cfg, "reviewer", create=True)
        state = _make_state(tmp_path)
        owner_crud_app["state"] = state
        owner_crud_app.router.add_post("/api/members/{slug}/thread", handlers.api_member_thread)
        async with TestClient(TestServer(owner_crud_app)) as client:
            response = await client.post("/api/members/reviewer/thread")
            assert response.status == 200, await response.text()
            slot = state._slots[(await response.json())["slot_key"]]
            key = f"dashboard:{slot.key}"
            real_read = member_memory_auth.read_private_session_store

            def read(session_key):
                if session_key == key:
                    if invalid == "missing":
                        return None
                    if invalid == "different":
                        return "another-private-store"
                    if invalid == "unreadable":
                        raise OSError("protected identity is unreadable")
                return real_read(session_key)

            monkeypatch.setattr(member_memory_auth, "read_private_session_store", read)
            pin = AsyncMock(wraps=handlers.pin_private_agent_store)
            monkeypatch.setattr(handlers, "pin_private_agent_store", pin)
            if invalid == "slot_store":
                slot.memory_store = "another-private-store"
            if invalid == "linked":
                slot.linked_session_key = "dashboard:unrelated"
            original_store = slot.memory_store
            original_link = slot.linked_session_key
            slot.task = asyncio.current_task()
            try:
                response = await client.post("/api/members/reviewer/thread")
                assert response.status == (503 if invalid == "unreadable" else 409)
                assert slot.running
                assert slot.memory_store == original_store
                assert slot.linked_session_key == original_link
                assert await asyncio.to_thread(real_read, key) == store
                pin.assert_not_awaited()
            finally:
                slot.task = None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("busy", ["turn", "children"])
    async def test_opt_in_refuses_active_member_work_before_configuration_change(
        self, owner_crud_app, tmp_path, busy
    ):
        from unittest.mock import AsyncMock, MagicMock

        from chat_test_helpers import _make_state

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        cfg.save()
        state = _make_state(tmp_path)
        owner_crud_app["state"] = state
        slot = state.get_or_create_slot("old-chat", agent="reviewer")
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock()
        if busy == "turn":
            slot.task = asyncio.current_task()
        else:
            state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["child"]))
        try:
            async with TestClient(TestServer(owner_crud_app)) as client:
                response = await client.put("/api/agents/reviewer", json={"provision_memory": True})
                assert response.status == 400, await response.text()
                assert (await response.json())["code"] == "member_memory_creation_only"
            assert KiroCrewConfig.load().agents["reviewer"].memory_store == "default"
            state.sessions.reset.assert_not_awaited()
        finally:
            slot.task = None
