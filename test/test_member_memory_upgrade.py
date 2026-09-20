"""A member store written before member identities existed is repaired at start.

The old layout: ``memory_stores.<name>`` with ``memory_version: 2`` and an
``owner_member`` label but no ``owner_member_id``; the bound Crew Member with no
``member_id``; ``member-memory.json`` beside a ``memory.db`` holding the crew
tables and learned rows but no ``member_database`` row. Today's resolvers refuse
that shape outright, so the upgrade is the only way such a member works again.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from kiro_crew import cli, cli_doctor
from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_schema, memory_stores
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.execution_context import resolve_member_execution
from kiro_crew.memory import PREFERENCES_FILE, PROJECTS_FILE
from kiro_crew.memory_stores import (
    LEGACY_MEMBER_MANIFEST,
    LEGACY_MEMBER_STORE_REMEDY,
    legacy_member_store_states,
    migrate_legacy_member_stores,
    repair_legacy_member_stores,
    require_member_memory_store,
)
from kiro_crew.slack import gateway as slack_gateway
from kiro_crew.vector_memory import open_member_database, read_member_database_identity

STORE = "member-reviewer-0123456789abcdef0123456789abcdef"
LESSON_KEY = "lesson.deadbeef"


def type_name(value: object) -> str:
    return type(value).__name__


def _write_legacy_database(path: Path, *, store: str, owner: str) -> None:
    """The database an earlier build's ``VectorMemoryStore.init`` left for a V2 member."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(memory_schema.CREW_SCHEMA_SQL)
        db.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO schema_version VALUES (?, ?)",
            (memory_schema.CREW_SCHEMA_VERSION, "2000-01-01T00:00:00+00:00"),
        )
        record_meta.ensure_schema(db)
        db.executemany(
            "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, ?)",
            (
                (memory_schema.LINEAGE_META_KEY, memory_schema.LINEAGE_CREW, "2000-01-01"),
                (memory_schema.STORE_NAME_META_KEY, store, "2000-01-01"),
                ("owner_member", owner, "2000-01-01"),
                ("private_memory_version", "2", "2000-01-01"),
            ),
        )
        db.execute(
            "INSERT INTO memory_items (id, kind, key, text, value_json, source, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_schema.semantic_item_id(LESSON_KEY),
                memory_schema.KIND_DIRECTIVE,
                LESSON_KEY,
                "Always run the gate before pushing",
                json.dumps({"rule": "Always run the gate before pushing", "category": "process"}),
                "user_explicit",
                "2000-01-01T00:00:00+00:00",
                "2000-01-01T00:00:00+00:00",
            ),
        )
    finally:
        db.close()


def _write_legacy_home(*bound: str, owner: str = "reviewer", manifest: bool = True) -> Path:
    """Write a pre-identity member home; return the store directory.

    *bound* names the Crew Members whose ``memory_store`` is the store (the
    single-owner case is the normal one). Every member also gets a peer on the
    default store so the roster is not degenerate.
    """
    from kiro_crew.config import loader

    home = config_dir()
    agents: dict[str, dict] = {"default": {}, "peer": {"kiro_agent": "kirocrew"}}
    for alias in bound:
        agents[alias] = {"kiro_agent": "kirocrew", "memory_store": STORE}
    config = {
        "agents": agents,
        "memory_stores": {
            "default": {},
            STORE: {"owner_member": owner, "memory_version": 2},
        },
    }
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    directory = memory_stores.memory_stores_root() / STORE
    directory.mkdir(parents=True)
    if manifest:
        (directory / LEGACY_MEMBER_MANIFEST).write_text(
            json.dumps({"owner_member": owner, "memory_version": 2}), encoding="utf-8"
        )
    _write_legacy_database(directory / memory_stores.MEMORY_DB_FILE, store=STORE, owner=owner)
    loader._invalidate_config_cache()
    return directory


def _config_bytes() -> bytes:
    return (config_dir() / "config.json").read_bytes()


def _has_member_database(database: Path) -> bool:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='member_database'"
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


class TestTheUpgradeRepairsAnAttributableStore:
    def test_the_member_resolves_and_its_lessons_survive(self) -> None:
        directory = _write_legacy_home("reviewer")
        database = directory / memory_stores.MEMORY_DB_FILE
        cfg = KiroCrewConfig.load()
        with pytest.raises(memory_stores.UnknownMemoryStore):
            require_member_memory_store(cfg, "reviewer")

        assert migrate_legacy_member_stores(cfg) == [STORE]

        # The in-memory config and the document on disk carry the same identity.
        member_id = cfg.agents["reviewer"].member_id
        assert member_id == "reviewer"
        assert cfg.memory_stores[STORE].owner_member_id == member_id
        assert cfg.memory_stores[STORE].owner_member == "reviewer"
        reloaded = KiroCrewConfig.load()
        assert reloaded.agents["reviewer"].member_id == member_id
        assert reloaded.memory_stores[STORE].owner_member_id == member_id
        assert reloaded.memory_stores[STORE].memory_version == 2
        # Every resolver that refused the old shape admits the repaired one.
        assert require_member_memory_store(reloaded, "reviewer") == STORE
        execution = resolve_member_execution(reloaded, "reviewer", validate_memory_files=True)
        assert (execution.member_id, execution.store.store_id) == (member_id, STORE)
        assert read_member_database_identity(database) == (member_id, STORE)
        # The learned rows are read through the ordinary member-store open path.
        store = open_member_database(database, member_id=member_id, store_id=STORE)
        try:
            lessons = store.get_lessons()
        finally:
            store.close()
        assert [lesson["key"] for lesson in lessons] == [LESSON_KEY]
        assert (directory / "memory" / PREFERENCES_FILE).read_text(encoding="utf-8")
        assert (directory / "memory" / PROJECTS_FILE).read_text(encoding="utf-8")
        # Nothing the old layout wrote is removed.
        assert (directory / LEGACY_MEMBER_MANIFEST).exists()
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            meta = dict(connection.execute("SELECT key, value FROM memory_meta").fetchall())
        finally:
            connection.close()
        assert meta["owner_member"] == "reviewer"
        assert meta[memory_schema.LINEAGE_META_KEY] == memory_schema.LINEAGE_CREW

    def test_a_second_run_changes_nothing(self) -> None:
        directory = _write_legacy_home("reviewer")
        database = directory / memory_stores.MEMORY_DB_FILE
        cfg = KiroCrewConfig.load()
        assert migrate_legacy_member_stores(cfg) == [STORE]
        config_after, database_after = _config_bytes(), database.read_bytes()

        assert migrate_legacy_member_stores(cfg) == []
        assert migrate_legacy_member_stores(KiroCrewConfig.load()) == []
        assert repair_legacy_member_stores() == []

        assert _config_bytes() == config_after
        assert database.read_bytes() == database_after
        assert legacy_member_store_states(KiroCrewConfig.load()) == {}

    def test_the_start_of_process_entry_runs_it_once(self) -> None:
        _write_legacy_home("reviewer")
        assert repair_legacy_member_stores() == [STORE]
        assert repair_legacy_member_stores() == []
        assert KiroCrewConfig.load().agents["reviewer"].member_id == "reviewer"

    def test_a_slug_already_held_gets_a_suffix_like_member_creation(self) -> None:
        _write_legacy_home("reviewer")
        cfg = KiroCrewConfig.load()
        cfg.agents["peer"].member_id = "reviewer"
        cfg.save()
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == [STORE]

        member_id = cfg.agents["reviewer"].member_id
        assert member_id != "reviewer" and member_id.startswith("reviewer-")
        assert KiroCrewConfig.load().memory_stores[STORE].owner_member_id == member_id

    def test_an_interrupted_upgrade_resumes_with_the_identity_already_in_the_database(
        self,
    ) -> None:
        directory = _write_legacy_home("reviewer")
        database = directory / memory_stores.MEMORY_DB_FILE
        # The database half landed, the config half did not: the same id is kept
        # rather than a second one being allocated.
        memory_stores._complete_legacy_member_database(
            database, member_id="reviewer-resumed", store=STORE
        )
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == [STORE]

        assert cfg.agents["reviewer"].member_id == "reviewer-resumed"
        assert read_member_database_identity(database) == ("reviewer-resumed", STORE)

    def test_a_missing_manifest_is_not_required(self) -> None:
        _write_legacy_home("reviewer", manifest=False)
        assert migrate_legacy_member_stores(KiroCrewConfig.load()) == [STORE]

    def test_a_published_member_identity_resumes_instead_of_stranding(self) -> None:
        """The member's identity is published before the store record's.

        An interruption between the two leaves the member holding an id the
        store lacks. That is this store's own half-finished upgrade, so it must
        complete rather than refuse: refusing strands the store for good, and
        the remedy of blanking the member_id would put the two into
        disagreement.
        """
        directory = _write_legacy_home("reviewer")
        database = directory / memory_stores.MEMORY_DB_FILE
        memory_stores._complete_legacy_member_database(database, member_id="reviewer", store=STORE)
        from kiro_crew.config import loader

        data = json.loads(_config_bytes())
        data["agents"]["reviewer"]["member_id"] = "reviewer"
        (config_dir() / "config.json").write_text(json.dumps(data), encoding="utf-8")
        loader._invalidate_config_cache()
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == [STORE]
        assert cfg.memory_stores[STORE].owner_member_id == "reviewer"

    def test_another_members_identity_is_still_a_conflict(self) -> None:
        """Only the bound member's own id resumes; any other holder refuses."""
        directory = _write_legacy_home("reviewer")
        memory_stores._complete_legacy_member_database(
            directory / memory_stores.MEMORY_DB_FILE, member_id="someone-else", store=STORE
        )
        from kiro_crew.config import loader

        data = json.loads(_config_bytes())
        data["agents"]["intruder"] = {"kiro_agent": "kirocrew", "member_id": "someone-else"}
        (config_dir() / "config.json").write_text(json.dumps(data), encoding="utf-8")
        loader._invalidate_config_cache()

        assert migrate_legacy_member_stores(KiroCrewConfig.load()) == []


class TestTheUpgradeRefusesWhatItCannotAttribute:
    @pytest.mark.parametrize(
        ("bound", "reason"),
        [
            ((), "no Crew Member is bound to it"),
            (("reviewer", "auditor"), "2 Crew Members are bound to it: auditor, reviewer"),
        ],
    )
    def test_zero_or_two_bound_members_skip_with_one_warning(self, bound, reason, caplog) -> None:
        directory = _write_legacy_home(*bound)
        database = directory / memory_stores.MEMORY_DB_FILE
        # The first load writes the loader's own migrations back; the baseline
        # is the document AFTER that, so only the upgrade's writes are measured.
        cfg = KiroCrewConfig.load()
        before = _config_bytes()

        with caplog.at_level(logging.WARNING, logger="kiro_crew.memory_stores"):
            assert migrate_legacy_member_stores(cfg) == []

        warnings = [record for record in caplog.records if STORE in record.getMessage()]
        assert len(warnings) == 1
        assert reason in warnings[0].getMessage()
        assert LEGACY_MEMBER_STORE_REMEDY in warnings[0].getMessage()
        assert _config_bytes() == before
        assert not _has_member_database(database)
        for alias in bound:
            assert cfg.agents[alias].member_id == ""
        assert legacy_member_store_states(cfg) == {STORE: reason}

    def test_a_member_that_already_has_an_identity_is_refused(self) -> None:
        _write_legacy_home("reviewer")
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"].member_id = "someone-else"
        cfg.save()
        cfg = KiroCrewConfig.load()
        before = _config_bytes()

        assert migrate_legacy_member_stores(cfg) == []

        assert _config_bytes() == before
        assert "already carries member_id 'someone-else'" in legacy_member_store_states(cfg)[STORE]

    @pytest.mark.parametrize("invalid", [[], {}, None, False, 0], ids=type_name)
    def test_a_non_string_owner_id_is_damaged_not_missing(self, invalid) -> None:
        """A hand-edited ``owner_member_id: []`` is falsy like the pre-identity
        ``""``, but the loader keeps it as written so the resolvers refuse it.
        Reading it as "no identity" would overwrite the damage with a fresh id."""
        directory = _write_legacy_home("reviewer")
        path = config_dir() / "config.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["memory_stores"][STORE]["owner_member_id"] = invalid
        path.write_text(json.dumps(document), encoding="utf-8")
        cfg = KiroCrewConfig.load()
        assert cfg.memory_stores[STORE].owner_member_id == invalid
        before = _config_bytes()

        assert migrate_legacy_member_stores(cfg) == []

        assert _config_bytes() == before
        assert not _has_member_database(directory / memory_stores.MEMORY_DB_FILE)
        assert cfg.agents["reviewer"].member_id == ""
        assert "owner_member_id is not a string" in legacy_member_store_states(cfg)[STORE]

    def test_a_manifest_naming_another_owner_is_refused(self) -> None:
        directory = _write_legacy_home("reviewer")
        (directory / LEGACY_MEMBER_MANIFEST).write_text(
            json.dumps({"owner_member": "stranger", "memory_version": 2}), encoding="utf-8"
        )
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == []

        assert "names owner 'stranger', not 'reviewer'" in legacy_member_store_states(cfg)[STORE]
        assert cfg.agents["reviewer"].member_id == ""

    def test_a_record_and_manifest_that_agree_on_another_owner_are_refused(self) -> None:
        """The store is bound to ``reviewer`` today, but both the config record
        and the manifest say it was ``alice``'s. Two labels agreeing with each
        other is not the bound member agreeing with either: adopting it would
        hand alice's lessons to reviewer."""
        _write_legacy_home("reviewer", owner="alice")
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == []

        reason = legacy_member_store_states(cfg)[STORE]
        assert "names owner_member 'alice', not 'reviewer'" in reason
        assert cfg.agents["reviewer"].member_id == ""
        assert cfg.memory_stores[STORE].owner_member_id == ""

    def test_a_record_naming_another_owner_is_refused_without_a_manifest(self) -> None:
        _write_legacy_home("reviewer", owner="alice", manifest=False)
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == []

        assert (
            "names owner_member 'alice', not 'reviewer'" in legacy_member_store_states(cfg)[STORE]
        )

    @pytest.mark.parametrize(
        ("stamp", "reason"),
        [
            (
                {"store": "member-somebody-else", "owner": "reviewer"},
                f"memory.db records store_name 'member-somebody-else', not {STORE!r}",
            ),
            (
                {"store": STORE, "owner": "somebody-else"},
                "memory.db records owner_member 'somebody-else', not 'reviewer'",
            ),
        ],
        ids=["stamped-for-another-store", "stamped-for-another-owner"],
    )
    def test_a_database_stamped_for_another_store_or_owner_is_refused(self, stamp, reason) -> None:
        # A database copied or restored into this store's directory still
        # carries the stamps the old build wrote for the store it came from;
        # the record and manifest agree with the bound member, the file does not.
        directory = _write_legacy_home("reviewer")
        database = directory / memory_stores.MEMORY_DB_FILE
        database.unlink()
        _write_legacy_database(database, **stamp)
        cfg = KiroCrewConfig.load()
        before = _config_bytes()

        assert migrate_legacy_member_stores(cfg) == []

        assert _config_bytes() == before
        assert cfg.agents["reviewer"].member_id == ""
        assert legacy_member_store_states(cfg)[STORE] == reason
        assert not _has_member_database(database)

    def test_a_database_recording_another_stores_identity_is_refused(self) -> None:
        directory = _write_legacy_home("reviewer")
        database = directory / memory_stores.MEMORY_DB_FILE
        memory_stores._complete_legacy_member_database(
            database, member_id="reviewer", store="member-somebody-else"
        )
        cfg = KiroCrewConfig.load()
        before = _config_bytes()

        assert migrate_legacy_member_stores(cfg) == []

        assert _config_bytes() == before
        assert cfg.agents["reviewer"].member_id == ""
        reason = legacy_member_store_states(cfg)[STORE]
        assert "already records the identity of store 'member-somebody-else'" in reason
        assert read_member_database_identity(database) == ("reviewer", "member-somebody-else")

    def test_a_database_identity_another_member_holds_is_refused(self) -> None:
        directory = _write_legacy_home("reviewer")
        memory_stores._complete_legacy_member_database(
            directory / memory_stores.MEMORY_DB_FILE, member_id="peer-id", store=STORE
        )
        cfg = KiroCrewConfig.load()
        cfg.agents["peer"].member_id = "peer-id"
        cfg.save()
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == []

        assert (
            "which another member or store already holds" in legacy_member_store_states(cfg)[STORE]
        )

    def test_a_missing_database_is_refused_rather_than_created(self) -> None:
        directory = _write_legacy_home("reviewer")
        (directory / memory_stores.MEMORY_DB_FILE).unlink()
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == []

        assert not (directory / memory_stores.MEMORY_DB_FILE).exists()
        assert legacy_member_store_states(cfg)[STORE] == "memory.db is missing"

    def test_a_hard_linked_database_is_refused_and_its_other_name_untouched(
        self, tmp_path: Path
    ) -> None:
        """One inode under two names is two stores sharing a file: writing this
        store's identity through its name would relabel the other silently. The
        upgrade refuses the read and the write alike, so the sibling keeps the
        schema it had."""
        directory = _write_legacy_home("reviewer")
        database = directory / memory_stores.MEMORY_DB_FILE
        sibling = tmp_path / "restored-copy.db"
        os.link(database, sibling)
        before = sibling.read_bytes()
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == []

        assert legacy_member_store_states(cfg)[STORE] == "memory.db is hard-linked to another file"
        assert cfg.agents["reviewer"].member_id == ""
        assert not _has_member_database(sibling)
        assert sibling.read_bytes() == before
        with pytest.raises(memory_stores.UnknownMemoryStore, match="hard-linked"):
            memory_stores._complete_legacy_member_database(
                database, member_id="reviewer", store=STORE
            )

    def test_a_symlinked_database_is_refused(self, tmp_path: Path) -> None:
        directory = _write_legacy_home("reviewer")
        database = directory / memory_stores.MEMORY_DB_FILE
        elsewhere = tmp_path / "elsewhere.db"
        database.rename(elsewhere)
        database.symlink_to(elsewhere)
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == []

        assert legacy_member_store_states(cfg)[STORE] == "memory.db is not a regular file"
        assert not _has_member_database(elsewhere)

    def test_one_refused_store_does_not_block_another(self) -> None:
        directory = _write_legacy_home("reviewer")
        from kiro_crew.config import loader

        other = "member-auditor-fedcba9876543210fedcba9876543210"
        data = json.loads(_config_bytes())
        data["agents"]["auditor"] = {"kiro_agent": "kirocrew", "memory_store": other}
        data["agents"]["shadow"] = {"kiro_agent": "kirocrew", "memory_store": other}
        data["memory_stores"][other] = {"owner_member": "auditor", "memory_version": 2}
        (config_dir() / "config.json").write_text(json.dumps(data), encoding="utf-8")
        other_dir = directory.parent / other
        other_dir.mkdir()
        _write_legacy_database(
            other_dir / memory_stores.MEMORY_DB_FILE, store=other, owner="auditor"
        )
        loader._invalidate_config_cache()
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == [STORE]

        assert cfg.agents["reviewer"].member_id == "reviewer"
        assert set(legacy_member_store_states(KiroCrewConfig.load())) == {other}

    def test_a_damaged_identity_elsewhere_does_not_block_a_repair(self) -> None:
        """One unreadable record must not cost every other store its repair.

        Allocation reserves the slugs already in use. A non-string identity
        cannot collide with a string slug, so it is excluded from that set
        rather than refusing the whole allocation; the damaged record is still
        refused on its own behalf by the candidate scan.
        """
        _write_legacy_home("reviewer")
        from kiro_crew.config import loader

        damaged = "member-wrecked-0123456789abcdef0123456789abcdef"
        data = json.loads(_config_bytes())
        data["memory_stores"][damaged] = {
            "owner_member": "wrecked",
            "owner_member_id": 7,
            "memory_version": 2,
        }
        (config_dir() / "config.json").write_text(json.dumps(data), encoding="utf-8")
        loader._invalidate_config_cache()
        cfg = KiroCrewConfig.load()

        assert migrate_legacy_member_stores(cfg) == [STORE]
        assert cfg.agents["reviewer"].member_id == "reviewer"

    def test_creating_a_member_still_refuses_a_damaged_identity(self) -> None:
        """The creation path keeps refusing: a config it cannot read is no
        basis for writing a new identity into it."""
        _write_legacy_home("reviewer")
        from kiro_crew.config import loader

        data = json.loads(_config_bytes())
        data["memory_stores"][STORE]["owner_member_id"] = 7
        (config_dir() / "config.json").write_text(json.dumps(data), encoding="utf-8")
        loader._invalidate_config_cache()
        cfg = KiroCrewConfig.load()

        with pytest.raises(memory_stores.UnknownMemoryStore):
            memory_stores._allocate_member_id(cfg, "newcomer")


class TestDoctorReportsWhatTheUpgradeLeftAlone:
    def test_a_refused_store_gets_its_reason_and_remedy(self, capsys) -> None:
        _write_legacy_home("reviewer", "auditor")
        cfg = KiroCrewConfig.load()
        before = _config_bytes()
        issues: list[str] = []

        cli_doctor._doctor_member_memory_bindings(cfg, issues)

        output = capsys.readouterr().out
        assert f"store {STORE!r}: no member identity and not upgradable" in output
        assert "2 Crew Members are bound to it" in output
        assert LEGACY_MEMBER_STORE_REMEDY in output
        assert f"member memory store without identity: {STORE!r}" in issues
        # Doctor only reports; the document and the member's binding are untouched.
        assert _config_bytes() == before
        assert cfg.agents["reviewer"].member_id == ""

    def test_a_repairable_store_is_reported_as_pending_not_broken(self, capsys) -> None:
        _write_legacy_home("reviewer")
        cfg = KiroCrewConfig.load()
        issues: list[str] = []

        cli_doctor._doctor_member_memory_bindings(cfg, issues)

        output = capsys.readouterr().out
        assert f"'reviewer' -> {STORE!r}: no member identity yet" in output
        assert "upgrades it automatically" in output
        assert f"{STORE!r}: unavailable" not in output
        # Pending is not broken: the binding is not an issue either, so doctor
        # exits clean on a store the next start repairs by itself.
        assert issues == []
        assert cfg.agents["reviewer"].member_id == ""


class TestTheHookIsWiredOnBothSurfaces:
    def test_the_cli_prologue_runs_it_before_dispatch(self) -> None:
        source = inspect.getsource(cli.main)
        assert "repair_legacy_member_stores()" in source
        # Before the first subcommand dispatch, so no command resolves a member first.
        assert source.index("repair_legacy_member_stores()") < source.index(
            'if args.command == "chat"'
        )

    def test_the_cli_prologue_leaves_the_gateway_boot_path_alone(self) -> None:
        """The gateway's boot path admits no new work before the dashboard socket
        accepts requests; its repair runs in the post-readiness memory worker."""
        source = inspect.getsource(cli.main)
        guard = source[: source.index("repair_legacy_member_stores()")]
        guard = guard[guard.rindex("if args.command") :]
        assert '"gateway"' in guard and '"doctor"' in guard and '"mcp-"' in guard

    def test_the_gateway_memory_worker_runs_it_before_restores(self) -> None:
        source = inspect.getsource(slack_gateway.GatewayOrchestrator._initialize_memory_worker)
        assert source.index("repair_legacy_member_stores()") < source.index(
            "apply_pending_member_restores("
        )
