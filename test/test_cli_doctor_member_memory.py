"""Doctor diagnoses existing member bindings without changing their stored memory."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from kiro_crew import cli_doctor, memory_stores
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_dir
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.vector_memory import VectorMemoryStore


def _snapshot(home: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()
    }


def _assert_only_new_sqlite_coordination(home: Path, before: dict[str, bytes], database: Path):
    after = _snapshot(home)
    added = after.keys() - before.keys()
    wal = str(database.with_name("memory.db-wal").relative_to(home))
    shm = str(database.with_name("memory.db-shm").relative_to(home))
    # Read-only WAL readers may update SHM read marks; database, committed WAL,
    # configuration and every other original file remain byte-identical.
    assert {name: after[name] for name in before if name != shm} == {
        name: value for name, value in before.items() if name != shm
    }
    assert added <= {wal, shm}
    if wal in added:
        assert after[wal] == b""
    if shm in added:
        # A mode=ro reader of a WAL database may create its shared WAL index.
        # This fixture needs one SQLite index region, with no new data frames.
        assert len(after[shm]) == 32768
        assert database.read_bytes()[18:20] == b"\x02\x02"


@pytest.fixture
def members(monkeypatch):
    # The root fixture pins the data home. Finish normal configuration creation
    # before taking a byte baseline; the diagnostic itself never loads or saves it.
    cfg = KiroCrewConfig.load()
    home = config_dir()
    cfg.agents = {
        "default": KiroCrewAgentConfig(),
        "legacy": KiroCrewAgentConfig(memory_store="legacy-store"),
        "private": KiroCrewAgentConfig(),
        "healthy-peer": KiroCrewAgentConfig(),
    }
    cfg.default_agent = "default"
    cfg.memory_stores["legacy-store"] = MemoryStoreConfig()
    legacy = memory_stores.memory_stores_root() / "legacy-store"
    legacy.mkdir(parents=True)
    for directory in (home, legacy):
        db = sqlite3.connect(directory / "memory.db")
        try:
            with db:
                db.execute("CREATE TABLE original_notes (value TEXT)")
                db.execute("INSERT INTO original_notes VALUES ('retained V1 content')")
        finally:
            db.close()
    private_store = memory_stores.provision_member_memory(cfg, "private")
    cfg.save()
    cfg = KiroCrewConfig.load()
    writes: list[str] = []

    def forbidden_write(*args, **kwargs):
        writes.append("memory initialization, allocation or publication")
        raise AssertionError("doctor must only inspect existing bindings")

    monkeypatch.setattr(VectorMemoryStore, "init", forbidden_write)
    monkeypatch.setattr(memory_stores, "provision_member_memory", forbidden_write)
    monkeypatch.setattr(memory_stores, "persist_member_config", forbidden_write)
    return cfg, home, private_store, writes


def test_global_named_v1_and_owned_v2_are_checked_without_writes(members, capsys):
    cfg, home, private_store, writes = members
    before = _snapshot(home)

    for _ in range(2):
        issues: list[str] = []
        cli_doctor._doctor_member_memory_bindings(cfg, issues)
        assert issues == []
        output = capsys.readouterr().out
        for name, store in (
            ("default", "default"),
            ("legacy", "legacy-store"),
            ("private", private_store),
            ("healthy-peer", "default"),
        ):
            assert f"{name!r} -> {store!r}: valid binding" in output
        _assert_only_new_sqlite_coordination(
            home, before, memory_stores.memory_stores_root() / private_store / "memory.db"
        )
    assert writes == []


@pytest.mark.parametrize(
    ("damage", "member", "reason"),
    [
        (
            "missing-database",
            "private",
            "Member database is missing or unreadable; Global was not used",
        ),
        ("wrong-database-owner", "private", "identity does not match"),
        ("wrong-config-owner", "private", "member identity is missing or ambiguous"),
        ("missing-declaration", "legacy", "is unavailable"),
        ("corrupt-v1-database", "legacy", "is unreadable"),
    ],
)
def test_broken_nondefault_binding_reports_reason_and_continues_healthy_peers(
    members, capsys, damage, member, reason
):
    cfg, home, private_store, writes = members
    database = memory_stores.memory_stores_root() / private_store / "memory.db"
    if damage == "missing-database":
        database.unlink()
    elif damage == "wrong-database-owner":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE member_database SET member_id='different-member'")
    elif damage == "wrong-config-owner":
        cfg.memory_stores[private_store].owner_member_id = "different-member"
        cfg.save()
    elif damage == "missing-declaration":
        del cfg.memory_stores["legacy-store"]
        cfg.save()
    else:
        (memory_stores.memory_stores_root() / "legacy-store" / "memory.db").write_bytes(
            b"unreadable legacy database"
        )
    cfg = KiroCrewConfig.load()
    assert cfg.default_agent == "default"
    before = _snapshot(home)
    issues: list[str] = []

    cli_doctor._doctor_member_memory_bindings(cfg, issues)

    binding = f"{member!r} -> {cfg.agents[member].memory_store!r}"
    output = capsys.readouterr().out
    assert f"{binding}: unavailable" in output
    assert reason in output
    assert "'default' -> 'default': valid binding" in output
    assert "'healthy-peer' -> 'default': valid binding" in output
    assert issues == [f"member memory binding unavailable: {binding}"]
    _assert_only_new_sqlite_coordination(home, before, database)
    assert writes == []


def test_private_identity_in_committed_wal_is_read_without_changing_memory(members, capsys):
    cfg, home, private_store, writes = members
    directory = memory_stores.memory_stores_root() / private_store
    database = directory / "memory.db"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        original_database = database.read_bytes()
        with writer:
            changed = writer.execute(
                "UPDATE member_database SET member_id=? WHERE singleton=1",
                ("wrong-owner",),
            )
            assert changed.rowcount == 1
        assert database.read_bytes() == original_database
        wal = database.with_name("memory.db-wal")
        assert wal.stat().st_size > 32
        before = _snapshot(home)
        issues: list[str] = []

        cli_doctor._doctor_member_memory_bindings(cfg, issues)

        output = capsys.readouterr().out
        assert "'default' -> 'default': valid binding" in output
        assert f"'private' -> {private_store!r}: unavailable" in output
        assert "'legacy' -> 'legacy-store': valid binding" in output
        assert "'healthy-peer' -> 'default': valid binding" in output
        assert issues == [f"member memory binding unavailable: 'private' -> {private_store!r}"]
        after = _snapshot(home)
        assert after.keys() == before.keys()
        # The held writer prevents checkpoint/cleanup. Readers may update SHM
        # read marks; database, committed WAL and all ownership/config bytes stay.
        shm = str(database.with_name("memory.db-shm").relative_to(home))
        assert shm in before
        assert {key: value for key, value in after.items() if key != shm} == {
            key: value for key, value in before.items() if key != shm
        }
        assert writes == []
    finally:
        writer.close()


def test_member_store_and_error_text_cannot_inject_terminal_controls(members, capsys):
    cfg, home, private_store, writes = members
    name = "untrusted\x1b[2J\nmember"
    store = "missing\x1b[2J\nstore"
    cfg.agents[name] = KiroCrewAgentConfig(memory_store=store)
    cfg.save()
    cfg = KiroCrewConfig.load()
    before = _snapshot(home)
    issues: list[str] = []

    cli_doctor._doctor_member_memory_bindings(cfg, issues)

    output = capsys.readouterr().out
    assert f"{name!r} -> {store!r}: unavailable" in output
    assert "\\x1b" in output
    assert "\x1b" not in output
    assert len(issues) == 1
    assert "\x1b" not in issues[0]
    assert "\n" not in issues[0]
    _assert_only_new_sqlite_coordination(
        home, before, memory_stores.memory_stores_root() / private_store / "memory.db"
    )
    assert writes == []


def test_member_diagnostics_are_wired_into_the_doctor_command():
    # Other doctor sections launch external probes; exercise this section with
    # real stores above and retain the command's explicit call-site contract.
    source = inspect.getsource(cli_doctor._doctor)
    assert "_doctor_member_memory_bindings(cfg, issues)" in source
