"""A damaged knowledge.db is moved aside so the gateway still starts.

The gateway builds the Knowledge Library store before its listener binds, so a
file SQLite reports as damaged must not raise out of that build.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.sqlite_quarantine import quarantine_sqlite_file

_GARBAGE = b"this is not a sqlite file " * 400


@pytest.fixture
def stores() -> Iterator[list[KnowledgeStore]]:
    opened: list[KnowledgeStore] = []
    yield opened
    for s in opened:
        s._close_all_for_tests()


def _damaged_library(path: Path) -> None:
    """A real library whose pages past the header are overwritten."""
    s = KnowledgeStore(str(path))
    s.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    s._close_all_for_tests()
    data = bytearray(path.read_bytes())
    data[4096:] = b"\xab" * (len(data) - 4096)
    path.write_bytes(bytes(data))
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _quarantined(path: Path) -> list[Path]:
    return sorted(p for p in path.parent.iterdir() if p.name.startswith(path.name + ".corrupt-"))


def test_a_file_that_is_not_a_database_is_moved_aside(tmp_path: Path, stores) -> None:
    db = tmp_path / "knowledge.db"
    db.write_bytes(_GARBAGE)
    with pytest.raises(sqlite3.DatabaseError):
        KnowledgeStore(str(db))

    store = KnowledgeStore.open_recovering(str(db))
    stores.append(store)

    assert store.db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    kept = [p for p in _quarantined(db) if not p.name.endswith(("-wal", "-shm", "-journal"))]
    assert len(kept) == 1
    assert kept[0].read_bytes() == _GARBAGE


def test_a_malformed_library_is_moved_aside(tmp_path: Path, stores) -> None:
    db = tmp_path / "knowledge.db"
    _damaged_library(db)
    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        KnowledgeStore(str(db))

    store = KnowledgeStore.open_recovering(str(db))
    stores.append(store)

    assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
    assert _quarantined(db)


def test_a_healthy_library_is_left_in_place(tmp_path: Path, stores) -> None:
    db = tmp_path / "knowledge.db"
    first = KnowledgeStore(str(db))
    first.db.execute(
        "INSERT INTO sources (id, name, source_type, uri, created_at, updated_at) "
        "VALUES ('s1', 'n', 'local_file', 'file:///x', 't', 't')"
    )
    first._close_all_for_tests()

    store = KnowledgeStore.open_recovering(str(db))
    stores.append(store)

    assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
    assert _quarantined(db) == []


def test_a_sidecar_that_will_not_move_is_reported_and_the_file_still_moves(
    tmp_path: Path, monkeypatch
) -> None:
    import kiro_crew.sqlite_quarantine as q

    db = tmp_path / "knowledge.db"
    db.write_bytes(_GARBAGE)
    (tmp_path / "knowledge.db-wal").write_bytes(b"wal")

    def _refuse(src: Path, dst: Path) -> None:
        raise PermissionError("held open")

    monkeypatch.setattr(q, "move_without_overwrite", _refuse)
    moved = quarantine_sqlite_file(db)

    assert not db.exists()
    assert moved.target.read_bytes() == _GARBAGE
    assert moved.left and "knowledge.db-wal" in moved.left[0]


def test_a_database_that_will_not_move_leaves_no_placeholder(tmp_path: Path, monkeypatch) -> None:
    import kiro_crew.sqlite_quarantine as q

    db = tmp_path / "knowledge.db"
    db.write_bytes(_GARBAGE)

    def _refuse(src, dst) -> None:
        raise PermissionError("held open")

    monkeypatch.setattr(q.os, "replace", _refuse)
    with pytest.raises(PermissionError):
        quarantine_sqlite_file(db)

    assert db.read_bytes() == _GARBAGE
    assert _quarantined(db) == []


def test_a_locked_database_is_not_moved(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "knowledge.db"
    db.write_bytes(b"placeholder")

    def _locked(self, *_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(KnowledgeStore, "__init__", _locked)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        KnowledgeStore.open_recovering(str(db))
    assert db.read_bytes() == b"placeholder"
    assert _quarantined(db) == []


def test_the_gateway_store_opens_over_a_damaged_file(tmp_path: Path, monkeypatch, stores) -> None:
    import kiro_crew.dashboard.state as state_mod

    monkeypatch.setattr(state_mod, "config_dir", lambda: tmp_path)
    db_dir = tmp_path / "workspace" / "knowledge"
    db_dir.mkdir(parents=True)
    (db_dir / "knowledge.db").write_bytes(_GARBAGE)

    holder = types.SimpleNamespace(_knowledge_store=None)
    store = state_mod.DashboardState.knowledge_store.fget(holder)
    stores.append(store)

    assert store.db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert _quarantined(db_dir / "knowledge.db")
