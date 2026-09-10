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
from member_memory_helpers import patch_private_memory_supported

from conftest import make_dir_link
from kiro_crew import memory_schema
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    config_dir,
    resolve_agent_bindings,
    update_config_locked,
)
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.memory_stores import (
    MEMBER_MEMORY_MANIFEST,
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
from kiro_crew.vector_memory import VectorMemoryStore


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
        assert {p.name for p in root.iterdir()} == {MEMBER_MEMORY_MANIFEST, "memory.db"}
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
        with sqlite3.connect(database) as db:
            meta = dict(db.execute("SELECT key, value FROM memory_meta").fetchall())
        assert meta[memory_schema.PRIVATE_MEMORY_VERSION_META_KEY] == "2"
        assert meta[memory_schema.STORE_NAME_META_KEY] == store
        assert meta[memory_schema.OWNER_MEMBER_META_KEY] == "reviewer"

    @pytest.mark.parametrize("replacement", [None, "someone-else"])
    def test_raw_private_database_open_refuses_lost_or_changed_manifest(self, replacement):
        _cfg, store = _new_member()
        manifest = memory_stores_root() / store / MEMBER_MEMORY_MANIFEST
        if replacement is None:
            manifest.unlink()
        else:
            manifest.write_text(
                json.dumps({"owner_member": replacement, "memory_version": 2}),
                encoding="utf-8",
            )

        raw = VectorMemoryStore(db_path=memory_stores_root() / store / "memory.db")
        with pytest.raises(ValueError, match="ownership is missing or does not match"):
            raw.init()
        raw.close()

    def test_members_have_distinct_stores_even_when_names_share_a_slug(self):
        cfg, first = _new_member("Code Review")
        cfg.agents["Code-Review"] = KiroCrewAgentConfig()
        second = provision_member_memory(cfg, "Code-Review")
        assert first != second
        assert require_member_memory_store(cfg, "Code Review") == first
        assert require_member_memory_store(cfg, "Code-Review") == second

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
        assert not (root / MEMBER_MEMORY_MANIFEST).exists()

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

        monkeypatch.setattr(stores.os, "scandir", unexpected)
        monkeypatch.setattr(stores, "_require_legacy_store_files", unexpected)
        result = resolve_agent_bindings(cfg, "reviewer", validate_memory_files=False)
        assert result.memory_store_name == binding

    def test_conflicting_private_declaration_refuses_advisory_without_archive_io(self, monkeypatch):
        import kiro_crew.memory_stores as stores

        cfg, _store = _new_member()
        cfg.agents["reviewer"].memory_store = "default"

        def unexpected(*args):
            raise AssertionError("configuration resolution read a retirement marker")

        monkeypatch.setattr(stores, "_member_archive_record", unexpected)
        with pytest.raises(UnknownMemoryStore, match="private memory declaration"):
            resolve_agent_bindings(cfg, "reviewer", validate_memory_files=False)

    @pytest.mark.parametrize("damage", ["manifest", "database", "record", "owner", "version"])
    def test_private_damage_is_never_classified_as_legacy(self, damage):
        cfg, store = _new_member()
        root = memory_stores_root() / store
        if damage == "manifest":
            (root / MEMBER_MEMORY_MANIFEST).unlink()
        elif damage == "database":
            (root / "memory.db").write_bytes(b"invalid")
        elif damage == "record":
            del cfg.memory_stores[store]
        elif damage == "owner":
            cfg.memory_stores[store].owner_member = ""
        else:
            cfg.memory_stores[store].memory_version = 1
        with pytest.raises(UnknownMemoryStore):
            resolve_agent_bindings(cfg, "reviewer")

    @pytest.mark.parametrize("member", ["reviewer", "r" * 1100])
    @pytest.mark.parametrize("matching_owner", [True, False])
    def test_custom_store_database_owner_survives_lost_manifest_and_declaration(
        self, member, matching_owner
    ):
        owner = member if matching_owner else member + "-other"
        cfg = KiroCrewConfig.load()
        cfg.agents[member] = KiroCrewAgentConfig()
        cfg.save()
        root = memory_stores_root() / "custom-private"
        root.mkdir(parents=True)
        database = root / "memory.db"
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE memory_meta (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute(
                "INSERT INTO memory_meta VALUES (?, ?)",
                (memory_schema.OWNER_MEMBER_META_KEY, owner),
            )
            connection.commit()
        finally:
            connection.close()
        before = database.read_bytes()
        if matching_owner:
            with pytest.raises(UnknownMemoryStore, match="retains private memory evidence"):
                resolve_agent_bindings(cfg, member)
            with pytest.raises(UnknownMemoryStore, match="retains private memory evidence"):
                provision_member_memory(cfg, member)
        else:
            assert resolve_agent_bindings(cfg, member).memory_store_name == "default"
        assert database.read_bytes() == before

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
        with pytest.raises(UnknownMemoryStore, match="exclusive regular file"):
            resolve_agent_bindings(cfg, "reviewer")

    @pytest.mark.parametrize("keep_manifest", [True, False])
    def test_unowned_declaration_cannot_downgrade_private_database(self, keep_manifest):
        cfg, store = _new_member()
        root = memory_stores_root() / store
        if not keep_manifest:
            (root / MEMBER_MEMORY_MANIFEST).unlink()
        cfg.memory_stores[store] = MemoryStoreConfig()
        before = (root / "memory.db").read_bytes()
        with pytest.raises(UnknownMemoryStore, match="private"):
            require_memory_store(store, config=cfg)
        assert (root / "memory.db").read_bytes() == before

    @pytest.mark.parametrize(
        "evidence", ["declaration", "manifest", "damaged_manifest", "empty_manifest"]
    )
    def test_lost_private_member_binding_cannot_use_or_initialize_v1(self, evidence):
        cfg, store = _new_member()
        persist_member_config(cfg, "reviewer", create=True)
        cfg.agents["reviewer"].memory_store = "default"
        if evidence != "declaration":
            del cfg.memory_stores[store]
        if evidence == "damaged_manifest":
            (memory_stores_root() / store / MEMBER_MEMORY_MANIFEST).unlink()
        elif evidence == "empty_manifest":
            (memory_stores_root() / store / MEMBER_MEMORY_MANIFEST).write_text(
                "{}", encoding="utf-8"
            )
        cfg.save()

        def delete_binding(data):
            data["agents"]["reviewer"].pop("memory_store")
            return data

        update_config_locked(mutate=delete_binding)
        cfg = KiroCrewConfig.load()
        for operation in (require_member_memory_store, provision_member_memory):
            with pytest.raises(UnknownMemoryStore, match="private"):
                operation(cfg, "reviewer")
        assert cfg.agents["reviewer"].memory_store == "default"

    def test_corrupt_peer_does_not_disable_legacy_members_or_global(self):
        cfg, peer = _new_member("other")
        (memory_stores_root() / peer / MEMBER_MEMORY_MANIFEST).write_text(
            "{invalid", encoding="utf-8"
        )
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        assert require_member_memory_store(cfg, "reviewer") == "default"
        assert require_member_memory_store(cfg, "default") == "default"
        with pytest.raises(UnknownMemoryStore):
            require_member_memory_store(cfg, "other")

    def test_archived_generation_does_not_rebind_recreated_member(self):
        from kiro_crew.memory_stores import archive_member_memory_store

        cfg, old = _new_member()
        persist_member_config(cfg, "reviewer", create=True)
        assert archive_member_memory_store(old, "reviewer")
        cfg.agents["reviewer"].memory_store = "default"
        cfg.save()
        assert require_member_memory_store(cfg, "reviewer") == "default"
        fresh = provision_member_memory(cfg, "reviewer")
        assert fresh != old
        assert require_member_memory_store(cfg, "reviewer") == fresh
        assert (memory_stores_root() / old / "memory.db").exists()

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
        (root / MEMBER_MEMORY_MANIFEST).unlink()
        (root / "memory.db").unlink()
        root.rmdir()
        with pytest.raises(UnknownMemoryStore, match="missing or unreadable"):
            require_member_memory_store(cfg, "reviewer")
        assert not root.exists()

    def test_unreadable_directory_does_not_fall_back(self, monkeypatch):
        cfg, store = _new_member()
        import kiro_crew.memory_stores as stores

        def denied(_):
            raise PermissionError("access denied")

        monkeypatch.setattr(stores.os, "scandir", denied)
        with pytest.raises(UnknownMemoryStore, match="access denied"):
            require_memory_store(store, config=cfg)

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

    def test_corrupt_or_wrong_ownership_manifest_refuses_execution(self):
        cfg, store = _new_member()
        manifest = memory_stores_root() / store / MEMBER_MEMORY_MANIFEST
        for payload in ("{bad", json.dumps({"owner_member": "someone-else", "memory_version": 2})):
            manifest.write_text(payload, encoding="utf-8")
            with pytest.raises(UnknownMemoryStore):
                require_member_memory_store(cfg, "reviewer")

    def test_store_link_to_another_member_is_refused(self):
        cfg, store = _new_member()
        cfg.agents["other"] = KiroCrewAgentConfig()
        other = provision_member_memory(cfg, "other")
        root = memory_stores_root() / store
        (root / MEMBER_MEMORY_MANIFEST).unlink()
        (root / "memory.db").unlink()
        root.rmdir()
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

    patch_private_memory_supported(monkeypatch)
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
            assert response.status == 200, await response.text()
            assert (await response.json())["new_conversation_required"] is True

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
            assert response.status == 200, await response.text()
        loaded = await asyncio.to_thread(KiroCrewConfig.load)
        await asyncio.to_thread(require_member_memory_store, loaded, "reviewer")

    def test_cli_creates_private_memory_and_refuses_rebinding(self, capsys, monkeypatch):
        from kiro_crew.cli_commands import _handle_agent

        patch_private_memory_supported(monkeypatch)
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
    async def test_opt_in_retires_v1_provider_and_opens_fresh_verified_member_thread(
        self, owner_crud_app, tmp_path
    ):
        from unittest.mock import AsyncMock, MagicMock

        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.handlers.members import api_member_thread
        from kiro_crew.member_memory_auth import read_private_session_store
        from kiro_crew.members import (
            DM_SLOT_MODE,
            member_slot_key,
            read_dm_binding_for_slot,
            write_dm_binding,
        )

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        cfg.save()
        state = _make_state(tmp_path)
        owner_crud_app["state"] = state
        owner_crud_app.router.add_post("/api/members/{slug}/thread", api_member_thread)
        old_slot = state.get_or_create_slot(
            member_slot_key("reviewer"), agent="reviewer", mode=DM_SLOT_MODE
        )
        write_dm_binding("reviewer", member="reviewer", slot_key=old_slot.key)
        old_key = f"dashboard:{old_slot.key}"
        log = state.conversation_log
        await asyncio.to_thread(log.append, old_key, "user", "V1 roadmap")
        await asyncio.to_thread(log.append, old_key, "assistant", "V1 answer")
        old_bytes = log._path(old_key).read_bytes()
        provider = MagicMock(has_active_turn=MagicMock(return_value=False))
        state.sessions.get_provider = MagicMock(return_value=provider)

        async def retire(key, *, skip_if_busy):
            assert key == old_key and skip_if_busy
            assert old_slot.memory_store == "default"
            assert old_slot._memory_assignment_from_history
            state.sessions.get_provider.return_value = None
            return True

        state.sessions.reset = AsyncMock(side_effect=retire)
        async with TestClient(TestServer(owner_crud_app)) as client:
            response = await client.put("/api/agents/reviewer", json={"provision_memory": True})
            assert response.status == 200, await response.text()
            result = await response.json()
            assert result["new_conversation_required"] is True
            response = await client.post("/api/members/reviewer/thread")
            assert response.status == 200, await response.text()
            new_slot_key = (await response.json())["slot_key"]
            assert new_slot_key != old_slot.key
            new_key = f"dashboard:{new_slot_key}"
            assert read_private_session_store(new_key) == result["memory_store"]
            assert read_private_session_store(old_key) is None
            assert state._slots[new_slot_key].messages == []
            assert read_dm_binding_for_slot(old_slot.key) is None
            assert read_dm_binding_for_slot(new_slot_key)["member"] == "reviewer"
            # Reopening the committed private generation restores only its own history.
            await asyncio.to_thread(log.append, new_key, "assistant", "Private answer")
            await asyncio.to_thread(
                log.update_metadata,
                new_key,
                {"agent": "reviewer", "mode": DM_SLOT_MODE, "memory_store": result["memory_store"]},
            )
            state._slots.pop(new_slot_key)
            response = await client.post("/api/members/reviewer/thread")
            assert response.status == 200, await response.text()
            assert (await response.json())["slot_key"] == new_slot_key
            messages = state._slots[new_slot_key].messages
            assert any("Private answer" in row.get("content", "") for row in messages)
            assert all("V1" not in row.get("content", "") for row in messages)
        state.sessions.reset.assert_awaited_once()
        assert log._path(old_key).read_bytes() == old_bytes

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
                assert response.status == 409, await response.text()
                assert (await response.json())["code"] == "member_memory_busy"
            assert KiroCrewConfig.load().agents["reviewer"].memory_store == "default"
            state.sessions.reset.assert_not_awaited()
        finally:
            slot.task = None
