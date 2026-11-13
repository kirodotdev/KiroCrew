"""A member store whose declaration an older build's save stripped is repaired at start.

An older CLI still on ``PATH`` rebuilds ``config.json`` from a schema that
predates member fields: the member's ``member_id`` and the store record's
``memory_version`` / ``owner_member_id`` / ``owner_member`` are dropped, so the
record reads as a plain V1 declaration while ``memory.db`` still carries its
``member_database`` identity. Every resolver refuses that store. The
start-of-process repair restores the config half from the database identity.
"""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew import cli_doctor, memory_stores
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.execution_context import resolve_member_execution
from kiro_crew.memory_stores import (
    legacy_member_store_states,
    provision_member_memory,
    repair_legacy_member_stores,
    require_member_memory_store,
)
from kiro_crew.vector_memory import read_member_database_identity


def _config_path():
    return config_dir() / "config.json"


def _write_config(data: dict) -> None:
    _config_path().write_text(json.dumps(data), encoding="utf-8")
    loader._invalidate_config_cache()


def _provisioned_member(alias: str = "reviewer") -> tuple[str, str]:
    """Create a real member store the way member creation does; return (member_id, store)."""
    _write_config(
        {
            "agents": {"default": {}, "peer": {"kiro_agent": "kirocrew"}, alias: {}},
            "memory_stores": {"default": {}},
        }
    )
    cfg = KiroCrewConfig.load()
    store = provision_member_memory(cfg, alias)
    cfg.save()  # creation publishes the allocation; provisioning is in-memory
    loader._invalidate_config_cache()
    reloaded = KiroCrewConfig.load()
    member_id = reloaded.agents[alias].member_id
    assert member_id and reloaded.memory_stores[store].memory_version == 2
    return member_id, store


def _strip_member_fields(alias: str, store: str) -> None:
    """Rewrite config.json the way a build without the member schema saves it."""
    data = json.loads(_config_path().read_text(encoding="utf-8"))
    data["agents"][alias].pop("member_id", None)
    data["memory_stores"][store] = {"description": "", "embedding_provider": ""}
    _write_config(data)


def _database(store: str):
    return memory_stores.memory_stores_root() / store / memory_stores.MEMORY_DB_FILE


class TestAStrippedDeclarationIsRestored:
    def test_the_member_resolves_again_from_its_database_identity(self) -> None:
        member_id, store = _provisioned_member()
        database_before = _database(store).read_bytes()
        _strip_member_fields("reviewer", store)
        stripped = KiroCrewConfig.load()
        assert stripped.memory_stores[store].memory_version == 1
        with pytest.raises(memory_stores.UnknownMemoryStore):
            require_member_memory_store(stripped, "reviewer", require_directory=True)

        assert repair_legacy_member_stores() == [store]

        repaired = KiroCrewConfig.load()
        assert repaired.agents["reviewer"].member_id == member_id
        record = repaired.memory_stores[store]
        assert (record.memory_version, record.owner_member_id, record.owner_member) == (
            2,
            member_id,
            "reviewer",
        )
        execution = resolve_member_execution(repaired, "reviewer", validate_memory_files=True)
        assert (execution.member_id, execution.store.store_id) == (member_id, store)
        # Only the config half was missing: the database is not rewritten.
        assert _database(store).read_bytes() == database_before
        assert read_member_database_identity(_database(store)) == (member_id, store)

    def test_a_second_run_changes_nothing(self) -> None:
        _member_id, store = _provisioned_member()
        _strip_member_fields("reviewer", store)
        assert repair_legacy_member_stores() == [store]
        after = _config_path().read_bytes()

        assert repair_legacy_member_stores() == []
        assert _config_path().read_bytes() == after
        assert legacy_member_store_states(KiroCrewConfig.load()) == {}

    def test_doctor_reports_it_as_pending_not_broken(self, capsys) -> None:
        _member_id, store = _provisioned_member()
        _strip_member_fields("reviewer", store)
        issues: list[str] = []

        cli_doctor._doctor_member_memory_bindings(KiroCrewConfig.load(), issues)

        output = capsys.readouterr().out
        assert f"'reviewer' -> {store!r}: no member identity yet" in output
        assert issues == []


class TestWhatIsNotAStrippedDeclaration:
    def test_an_ordinary_v1_named_store_is_left_alone_silently(self, caplog) -> None:
        _write_config(
            {
                "agents": {"default": {}, "crew": {"memory_store": "team-notes"}},
                "memory_stores": {"default": {}, "team-notes": {"description": "shared"}},
            }
        )
        directory = memory_stores.memory_stores_root() / "team-notes"
        directory.mkdir(parents=True)
        from kiro_crew.vector_memory import VectorMemoryStore

        v1 = VectorMemoryStore(directory / memory_stores.MEMORY_DB_FILE)
        v1.init()
        v1.close()
        before = _config_path().read_bytes()

        with caplog.at_level(logging.WARNING, logger="kiro_crew.memory_stores"):
            assert repair_legacy_member_stores() == []

        assert _config_path().read_bytes() == before
        assert legacy_member_store_states(KiroCrewConfig.load()) == {}
        assert not [r for r in caplog.records if "team-notes" in r.getMessage()]

    def test_two_bound_members_are_not_guessed_between(self) -> None:
        _member_id, store = _provisioned_member()
        _strip_member_fields("reviewer", store)
        data = json.loads(_config_path().read_text(encoding="utf-8"))
        data["agents"]["peer"]["memory_store"] = store
        _write_config(data)
        before = _config_path().read_bytes()

        assert repair_legacy_member_stores() == []
        assert _config_path().read_bytes() == before

    def test_a_database_recording_another_store_is_not_adopted(self) -> None:
        _member_id, store = _provisioned_member()
        _strip_member_fields("reviewer", store)
        # The same bytes under another store's name: its identity names the
        # original store, so the declaration it sits under is not its own.
        data = json.loads(_config_path().read_text(encoding="utf-8"))
        data["agents"]["reviewer"]["memory_store"] = "member-copy"
        data["memory_stores"]["member-copy"] = data["memory_stores"].pop(store)
        _write_config(data)
        moved = memory_stores.memory_stores_root() / "member-copy"
        (memory_stores.memory_stores_root() / store).rename(moved)
        before = _config_path().read_bytes()

        assert repair_legacy_member_stores() == []
        assert _config_path().read_bytes() == before
