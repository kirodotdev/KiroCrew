"""Memory binding shape validation and immutable private member identity."""

from __future__ import annotations

import json
import os
import unittest.mock
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cli import main
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    UnknownMemoryStore,
    memory_store_binding_defect,
    memory_store_dir_for,
    memory_store_name_defect,
    persist_member_config,
    provision_member_memory,
    require_member_memory_store,
    require_memory_store,
    retire_unpublished_member_memory_store,
)

# One malformed value per rule in the shape table, so a rule dropped from
# ``memory_store_name_defect`` stops being enforced at the write boundary too and
# this file is what reports it.
MALFORMED = [
    "Work",  # not lowercase
    "../escape",  # traversal, and not a single segment
    "work/notes",  # not a single segment
    "work\\notes",  # a single segment to posixpath, two on Windows
    "con",  # a Windows reserved device basename
    "trailing.",  # ends with a dot
    "-leading",  # the slug dialect admits no leading hyphen
    "under_score",  # nor an underscore
    "x" * 200,  # over the length cap
]

# A non-string is refused even though ``""`` is not: it lands in config.json
# verbatim and every reader downstream is annotated ``str``.
NON_STRINGS = [None, 7, ["work"], {"work": 1}]


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the agent handlers past their independent owner-auth boundary."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_execution_supported", lambda **kwargs: True
    )


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_delete,
        api_kirocrew_agent_update,
        api_kirocrew_agents_create,
    )

    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    app.router.add_delete("/api/agents/{name}", api_kirocrew_agent_delete)
    return app


@pytest.fixture()
def seeded_agent() -> str:
    """One stored crew on the default store, written through the real config API."""
    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store=DEFAULT_MEMORY_STORE
    )
    cfg.save()
    return "existing"


def _declare_store(name: str) -> None:
    """Add *name* to the operator's ``memory_stores`` table."""
    cfg = KiroCrewConfig.load()
    cfg.memory_stores[name] = MemoryStoreConfig()
    cfg.save()


def _store_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Warnings from ``memory_stores`` alone.

    ``caplog`` captures at the ROOT, so the create handler's own "template is not
    in the installed agent listing" warning — which fires in a test environment
    with no installed kiro agents and names the same crew — lands in the same list.
    A substring match on the crew name would find that one instead.
    """
    return [r.getMessage() for r in caplog.records if r.name == "kiro_crew.memory_stores"]


class TestTheBindingPredicate:
    """``memory_store_binding_defect`` is the shape rule plus one exception."""

    def test_it_delegates_every_rule_to_the_name_predicate(self) -> None:
        """No second copy of the shape rule.

        A restated rule is how a write boundary comes to accept a name the
        resolvers refuse to compose a path for, so the binding predicate must
        answer identically to the name predicate everywhere the one exception
        does not apply — including the reason text, which is what the surfaces
        show the author.
        """
        for value in [*MALFORMED, *NON_STRINGS, "work", DEFAULT_MEMORY_STORE, "a-b-9"]:
            assert memory_store_binding_defect(value) == memory_store_name_defect(value), value

    def test_the_empty_string_is_the_one_divergence(self) -> None:
        """``""`` is the absence of a choice, not a broken name.

        ``resolve_agent_bindings`` maps it onto the default-store floor and the
        crew editor emits it for "nothing selected" while sending the field on
        every save, so a write that refused it would make a crew whose stored
        binding is already empty unsavable.
        """
        assert memory_store_name_defect("") == "empty"
        assert memory_store_binding_defect("") is None

    def test_an_undeclared_name_is_not_a_defect(self) -> None:
        """Shape only. Declaredness is resolution's question, not the write's."""
        assert memory_store_binding_defect("never-declared-anywhere") is None


class TestTheDashboardRefusesAMalformedBinding:
    @pytest.mark.parametrize("bad", MALFORMED)
    @pytest.mark.asyncio
    async def test_create_refuses_and_stores_nothing(self, bad: str) -> None:
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "reviewer", "kiro_agent": "kirocrew", "memory_store": bad},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "invalid_memory_store"
            # The message must name the ACTUAL defect; "invalid" alone leaves the
            # author guessing which of nine rules they tripped.
            assert memory_store_name_defect(bad) in body["error"]

        assert "reviewer" not in KiroCrewConfig.load().agents

    @pytest.mark.parametrize("bad", NON_STRINGS)
    @pytest.mark.asyncio
    async def test_create_refuses_a_non_string(self, bad: object) -> None:
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "reviewer", "kiro_agent": "kirocrew", "memory_store": bad},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_memory_store"

        assert "reviewer" not in KiroCrewConfig.load().agents

    @pytest.mark.parametrize("bad", MALFORMED)
    @pytest.mark.asyncio
    async def test_update_refuses_and_writes_nothing_at_all(
        self, seeded_agent: str, bad: str
    ) -> None:
        """The whole request is a no-op on disk, not just the offending field.

        A rejected store cannot smuggle a workspace rebind through with it. What
        this pins is the PERSISTED outcome, deliberately: the refusal returns
        before the config save, so moving the check after the in-memory field
        assignments would leave this assertion true. Pinning the assignment order
        instead would need to observe the un-saved config object, and the property
        that matters to a user is that a 400 changes nothing they can later read.
        """
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"workspace": "other", "memory_store": bad},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_memory_store"

        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.memory_store == DEFAULT_MEMORY_STORE
        assert stored.workspace == "default"

    @pytest.mark.asyncio
    async def test_update_refuses_a_non_string(self, seeded_agent: str) -> None:
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"memory_store": None})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_memory_store"

        assert KiroCrewConfig.load().agents[seeded_agent].memory_store == DEFAULT_MEMORY_STORE


class TestDashboardPrivateBinding:
    @staticmethod
    def _unpublished_private_allocation(member: str) -> str:
        attempted = KiroCrewConfig.load()
        return provision_member_memory(attempted, member)

    @staticmethod
    def _private_store_bytes(store: str) -> dict[str, bytes]:
        import kiro_crew.memory_stores as stores

        # This helper intentionally examines the allocation before config
        # publication, so the public declared-store resolver must still refuse
        # it. Compose through the product's identity-checking private helper.
        directory = stores._named_store_dir(store)
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    def test_failed_publication_retires_only_the_unpublished_generation(
        self, seeded_agent: str
    ) -> None:
        import kiro_crew.memory_stores as stores
        from kiro_crew.config.loader import config_path

        store = self._unpublished_private_allocation(seeded_agent)
        config_before = config_path().read_bytes()
        files_before = self._private_store_bytes(store)

        assert retire_unpublished_member_memory_store(store, seeded_agent)
        assert config_path().read_bytes() == config_before
        assert self._private_store_bytes(store) == files_before
        assert stores._member_archive_record(store) == {
            "version": 1,
            "archived": True,
            "memory_store": store,
            "owner_member": seeded_agent,
        }
        persisted = KiroCrewConfig.load()
        assert require_member_memory_store(persisted, seeded_agent) == DEFAULT_MEMORY_STORE

        retry = KiroCrewConfig.load()
        next_store = provision_member_memory(retry, seeded_agent)
        assert next_store != store

    @pytest.mark.parametrize("publication", ["store-declaration", "agent-reference"])
    def test_cleanup_preserves_a_generation_visible_in_the_locked_config(
        self, seeded_agent: str, publication: str
    ) -> None:
        import kiro_crew.memory_stores as stores
        from kiro_crew.config.loader import config_path, update_config_locked

        store = self._unpublished_private_allocation(seeded_agent)

        def publish(doc: dict) -> dict:
            if publication == "store-declaration":
                doc.setdefault("memory_stores", {})[store] = {
                    "owner_member": seeded_agent,
                    "memory_version": 2,
                }
            else:
                doc["agents"][seeded_agent]["memory_store"] = store
            return doc

        update_config_locked(mutate=publish)
        config_before = config_path().read_bytes()
        files_before = self._private_store_bytes(store)

        assert not retire_unpublished_member_memory_store(store, seeded_agent)
        assert config_path().read_bytes() == config_before
        assert self._private_store_bytes(store) == files_before
        assert stores._member_archive_record(store) is None

    @pytest.mark.parametrize("malformed", ["agents", "memory-stores", "agent-entry"])
    def test_cleanup_refuses_unreadable_publication_state(
        self, seeded_agent: str, malformed: str
    ) -> None:
        import kiro_crew.memory_stores as stores
        from kiro_crew.config.loader import config_path, update_config_locked

        store = self._unpublished_private_allocation(seeded_agent)

        def corrupt(doc: dict) -> dict:
            if malformed == "agents":
                doc["agents"] = []
            elif malformed == "memory-stores":
                doc["memory_stores"] = []
            else:
                doc["agents"]["broken"] = "not-an-agent-record"
            return doc

        update_config_locked(mutate=corrupt)
        config_before = config_path().read_bytes()
        files_before = self._private_store_bytes(store)

        with pytest.raises(UnknownMemoryStore, match="preserved"):
            retire_unpublished_member_memory_store(store, seeded_agent)
        assert config_path().read_bytes() == config_before
        assert self._private_store_bytes(store) == files_before
        assert stores._member_archive_record(store) is None

    def test_cleanup_refuses_a_different_manifest_owner(self, seeded_agent: str) -> None:
        import kiro_crew.memory_stores as stores
        from kiro_crew.config.loader import config_path

        store = self._unpublished_private_allocation(seeded_agent)
        config_before = config_path().read_bytes()
        files_before = self._private_store_bytes(store)

        with pytest.raises(UnknownMemoryStore, match="does not belong"):
            retire_unpublished_member_memory_store(store, "another-member")
        assert config_path().read_bytes() == config_before
        assert self._private_store_bytes(store) == files_before
        assert stores._member_archive_record(store) is None

    def test_cleanup_preserves_a_store_when_config_is_missing(self, seeded_agent: str) -> None:
        import kiro_crew.memory_stores as stores
        from kiro_crew.config.loader import config_path

        store = self._unpublished_private_allocation(seeded_agent)
        files_before = self._private_store_bytes(store)
        config_path().unlink()

        with pytest.raises(UnknownMemoryStore, match="preserved"):
            retire_unpublished_member_memory_store(store, seeded_agent)
        assert self._private_store_bytes(store) == files_before
        assert stores._member_archive_record(store) is None

    def test_cleanup_preserves_a_store_when_config_json_is_malformed(
        self, seeded_agent: str
    ) -> None:
        import kiro_crew.memory_stores as stores
        from kiro_crew.config.loader import ConfigReadError, config_path

        store = self._unpublished_private_allocation(seeded_agent)
        files_before = self._private_store_bytes(store)
        malformed = b'{"agents":'
        config_path().write_bytes(malformed)

        with pytest.raises(ConfigReadError):
            retire_unpublished_member_memory_store(store, seeded_agent)
        assert config_path().read_bytes() == malformed
        assert self._private_store_bytes(store) == files_before
        assert stores._member_archive_record(store) is None

    def test_cleanup_winning_the_config_lock_prevents_a_stale_publication(
        self, seeded_agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.config.loader as loader
        import kiro_crew.memory_stores as stores

        attempted = KiroCrewConfig.load()
        store = provision_member_memory(attempted, seeded_agent)
        real_update = loader.update_config_locked

        def cleanup_before_mutation(*args, mutate, **kwargs):
            def interleave(doc: dict):
                assert stores.archive_member_memory_store(store, seeded_agent)
                return mutate(doc)

            return real_update(*args, mutate=interleave, **kwargs)

        monkeypatch.setattr(loader, "update_config_locked", cleanup_before_mutation)
        with pytest.raises(UnknownMemoryStore, match="archived"):
            persist_member_config(
                attempted,
                seeded_agent,
                expected_store=DEFAULT_MEMORY_STORE,
                changed_fields={"memory_store"},
            )

        persisted = KiroCrewConfig.load()
        assert persisted.agents[seeded_agent].memory_store == DEFAULT_MEMORY_STORE
        assert store not in persisted.memory_stores
        archive = stores._member_archive_record(store)
        assert archive is not None
        assert archive["owner_member"] == seeded_agent

    @pytest.mark.asyncio
    async def test_create_without_a_store_allocates_private_memory(self):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200
        cfg = KiroCrewConfig.load()
        store = cfg.agents["reviewer"].memory_store
        assert store != DEFAULT_MEMORY_STORE
        assert cfg.memory_stores[store].owner_member == "reviewer"
        assert cfg.memory_stores[store].memory_version == 2

    @pytest.mark.asyncio
    async def test_delete_then_same_name_create_keeps_old_store_and_starts_fresh(self):
        async with TestClient(TestServer(_crud_app())) as client:
            created = await client.post(
                "/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"}
            )
            assert created.status == 200
            old_store = (await created.json())["memory_store"]

            deleted = await client.delete("/api/agents/reviewer")
            assert deleted.status == 200
            recreated = await client.post(
                "/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"}
            )
            assert recreated.status == 200
            new_store = (await recreated.json())["memory_store"]

        cfg = KiroCrewConfig.load()
        assert new_store != old_store
        assert old_store in cfg.memory_stores
        assert cfg.memory_stores[old_store].owner_member == "reviewer"
        assert cfg.agents["reviewer"].memory_store == new_store

    @pytest.mark.asyncio
    async def test_deleted_private_generation_cannot_be_rebound_or_restored(self):
        from kiro_crew import memory_backup

        async with TestClient(TestServer(_crud_app())) as client:
            created = await client.post(
                "/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"}
            )
            assert created.status == 200
            old_store = (await created.json())["memory_store"]
            database = memory_store_dir_for(old_store) / "memory.db"
            backup = memory_backup.backup_store(database)
            assert backup is not None
            deleted = await client.delete("/api/agents/reviewer")
            assert deleted.status == 200

        # A hand edit that restores the old config references is not authority
        # to reactivate the retained generation.
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(memory_store=old_store)
        cfg.save()
        rebound = KiroCrewConfig.load()
        # Configuration-only callers deliberately skip directory readiness,
        # but they still pass the shared store-admission seam and must not
        # reinterpret retained files as a live member generation.
        with pytest.raises(UnknownMemoryStore, match="archived"):
            require_memory_store(old_store, config=rebound, require_directory=False)
        with pytest.raises(UnknownMemoryStore, match="archived"):
            require_member_memory_store(rebound, "reviewer")
        with pytest.raises(UnknownMemoryStore, match="archived"):
            memory_backup.restore_from_backup(backup, old_store)

    @pytest.mark.asyncio
    async def test_failed_delete_save_rolls_back_its_retirement_marker(self, monkeypatch):
        from kiro_crew.config.loader import config_path

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "reviewer")
        cfg.save()

        def fail_after_mutation(*args, **kwargs):
            doc = json.loads(config_path().read_text(encoding="utf-8"))
            kwargs["mutate"](doc)
            raise OSError("read only")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.update_config_locked",
            fail_after_mutation,
        )

        async with TestClient(TestServer(_crud_app())) as client:
            response = await client.delete("/api/agents/reviewer")
            assert response.status == 500

        persisted = KiroCrewConfig.load()
        assert persisted.agents["reviewer"].memory_store == store
        assert require_member_memory_store(persisted, "reviewer") == store

    def test_interrupted_archive_publication_never_commits_a_partial_marker(self, monkeypatch):
        import kiro_crew.memory_stores as stores

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "reviewer")
        cfg.save()

        def interrupt(_staging, _destination):
            raise OSError("interrupted before publication")

        monkeypatch.setattr(stores, "_publish_member_archive_dir", interrupt)
        with pytest.raises(OSError, match="interrupted before publication"):
            stores.archive_member_memory_store(store, "reviewer")
        assert stores._member_archive_record(store) is None
        assert require_member_memory_store(KiroCrewConfig.load(), "reviewer") == store

    def test_missing_committed_archive_record_does_not_reactivate_a_store(self):
        import kiro_crew.memory_stores as stores

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "reviewer")
        cfg.save()
        assert stores.archive_member_memory_store(store, "reviewer")
        stores._member_archive_path(store).unlink()
        with pytest.raises(UnknownMemoryStore, match="retirement marker is invalid"):
            require_member_memory_store(KiroCrewConfig.load(), "reviewer")

    @pytest.mark.parametrize("redirect", ["store", "archive-root"])
    def test_dangling_archive_directory_does_not_reactivate_a_store(self, redirect):
        import kiro_crew.memory_stores as stores

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "reviewer")
        cfg.save()
        assert stores.archive_member_memory_store(store, "reviewer")
        marker = stores._member_archive_path(store)
        directory = marker.parent
        marker.unlink()
        directory.rmdir()
        if redirect == "archive-root":
            directory = directory.parent
            directory.rmdir()
        try:
            directory.symlink_to(
                directory.with_name("missing-archive-target"), target_is_directory=True
            )
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        with pytest.raises(UnknownMemoryStore, match="retirement marker is invalid"):
            require_member_memory_store(KiroCrewConfig.load(), "reviewer")

    def test_unreadable_archive_directory_probe_fails_closed(self, monkeypatch):
        import kiro_crew.memory_stores as stores

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "reviewer")
        cfg.save()
        directory = stores._member_archive_path(store).parent
        real_lstat = Path.lstat

        def unreadable(candidate, *args, **kwargs):
            if candidate == directory:
                raise PermissionError("archive directory unreadable")
            return real_lstat(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", unreadable)
        with pytest.raises(UnknownMemoryStore, match="retirement marker is invalid"):
            require_member_memory_store(KiroCrewConfig.load(), "reviewer")

    @pytest.mark.asyncio
    async def test_failed_deleter_does_not_undo_a_concurrently_committed_retirement(
        self, monkeypatch
    ):
        from kiro_crew.config.loader import config_path, write_config_atomically

        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "reviewer")
        cfg.save()

        def competitor_commits_then_writer_reports_failure(*args, **kwargs):
            doc = json.loads(config_path().read_text(encoding="utf-8"))
            committed = kwargs["mutate"](doc)
            assert committed is not None
            write_config_atomically(config_path(), committed)
            raise OSError("original writer lost its completion signal")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.update_config_locked",
            competitor_commits_then_writer_reports_failure,
        )
        async with TestClient(TestServer(_crud_app())) as client:
            response = await client.delete("/api/agents/reviewer")
            assert response.status == 500

        rebind = KiroCrewConfig.load()
        assert "reviewer" not in rebind.agents
        rebind.agents["reviewer"] = KiroCrewAgentConfig(memory_store=store)
        rebind.save()
        with pytest.raises(UnknownMemoryStore, match="archived"):
            require_member_memory_store(KiroCrewConfig.load(), "reviewer")

    def test_private_database_hard_link_is_refused(self, tmp_path: Path) -> None:
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        store = provision_member_memory(cfg, "reviewer")
        cfg.save()
        database = memory_store_dir_for(store) / "memory.db"
        alias = tmp_path / "memory-alias.db"
        try:
            os.link(database, alias)
        except OSError as exc:
            pytest.skip(f"hard links unavailable: {exc}")

        with pytest.raises(UnknownMemoryStore, match="hard-link alias"):
            require_memory_store(store, config=cfg)

    @pytest.mark.parametrize("declared", [True, False])
    @pytest.mark.asyncio
    async def test_create_cannot_select_existing_or_undeclared_memory(self, declared):
        if declared:
            _declare_store("work")
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "reviewer", "kiro_agent": "kirocrew", "memory_store": "work"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "private_memory_required"
        assert "reviewer" not in KiroCrewConfig.load().agents

    @pytest.mark.parametrize("target", ["", "ghost", "work"])
    @pytest.mark.asyncio
    async def test_update_cannot_rebind_memory(self, seeded_agent, target):
        _declare_store("work")
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"memory_store": target})
            assert resp.status == 409
            assert (await resp.json())["code"] == "private_memory_immutable"
        assert KiroCrewConfig.load().agents[seeded_agent].memory_store == DEFAULT_MEMORY_STORE


def _cli_config(tmp_path: Path) -> Path:
    """A minimal config.json with a default crew, declaring only the default store."""
    payload = {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": DEFAULT_MEMORY_STORE,
            },
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}},
        "memory_stores": {DEFAULT_MEMORY_STORE: {}},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class TestTheCliRefusesAMalformedBinding:
    """``kirocrew agent create``/``update`` apply the same predicate.

    Two surfaces writing one ``config.json`` must not accept different sets of
    values, or the stricter one is decoration: the operator reaches the same bad
    state through the other verb.
    """

    @pytest.mark.parametrize("bad", MALFORMED)
    def test_create_exits_nonzero_and_writes_nothing(
        self, tmp_path: Path, bad: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _cli_config(tmp_path)
        # ``--memory-store=<value>``, never the two-token form: a value that opens
        # with a hyphen is an option to argparse, so the two-token form would test
        # the parser rather than the guard.
        argv = ["kirocrew", "agent", "create", "--name", "reviewer", f"--memory-store={bad}"]
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", argv),
            pytest.raises(SystemExit) as exc,
        ):
            main()

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert memory_store_name_defect(bad) in err
        assert "reviewer" not in json.loads(cfg_path.read_text(encoding="utf-8"))["agents"]

    @pytest.mark.parametrize("bad", MALFORMED)
    def test_update_exits_nonzero_and_leaves_the_binding_alone(
        self, tmp_path: Path, bad: str
    ) -> None:
        cfg_path = _cli_config(tmp_path)
        # ``update`` takes the crew name positionally.
        argv = ["kirocrew", "agent", "update", "default", f"--memory-store={bad}"]
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", argv),
            pytest.raises(SystemExit) as exc,
        ):
            main()

        assert exc.value.code != 0
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["default"]["memory_store"] == DEFAULT_MEMORY_STORE

    def test_update_refuses_an_undeclared_name(self, tmp_path):
        cfg_path = _cli_config(tmp_path)
        argv = ["kirocrew", "agent", "update", "default", "--memory-store=ghost"]
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", argv),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 1
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["default"]["memory_store"] == DEFAULT_MEMORY_STORE

    def test_delete_archives_the_exact_private_generation(self, tmp_path, monkeypatch):
        cfg_path = _cli_config(tmp_path)
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_path)
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "reviewer")
        cfg.save()

        with unittest.mock.patch("sys.argv", ["kirocrew", "agent", "delete", "reviewer"]):
            main()

        rebind = KiroCrewConfig.load()
        rebind.agents["reviewer"] = KiroCrewAgentConfig(memory_store=store)
        rebind.save()
        with pytest.raises(UnknownMemoryStore, match="archived"):
            require_member_memory_store(KiroCrewConfig.load(), "reviewer")


class TestWhyAMalformedBindingHadToBeRefused:
    def test_a_non_string_binding_breaks_the_command_that_would_show_it(
        self, tmp_path: Path
    ) -> None:
        """``kirocrew agent list`` formats the field through a width spec.

        Characterizes the state the write guard now prevents rather than any
        behaviour of the guard: a hand-edited ``config.json`` can still reach it,
        and this is what it costs — the listing raises, so the operator cannot see
        the binding that is wrong. Nothing sanitizes it on the read side, and
        nothing should: repairing a name is what would merge two crews' memory
        into one directory.
        """
        payload = json.loads(_cli_config(tmp_path).read_text(encoding="utf-8"))
        payload["agents"]["default"]["memory_store"] = None
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "agent", "list"]),
            pytest.raises(TypeError),
        ):
            main()
