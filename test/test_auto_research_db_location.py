"""Research Lab keeps its campaigns database in the app's ``data/`` directory.

An update, an adoption onto the app's own repository and a default uninstall keep
an app's ``data/`` and replace the rest of ``<data home>/apps/<app>/``, so a
database kept beside ``app.json`` would be deleted with the old tree. The
campaigns DB lives in ``data/``, and one kept beside ``app.json`` is copied in
once, losslessly, before any connection opens, and the old file is then removed.
These tests pin where the database is, what the copy carries, the owner-only mode
it is published with, and everything the copy must never do: replace a database
already in ``data/`` (before or during the copy), follow a link, leave a partial
file, hide the old rows when it fails, hold up the start on a locked old file, or
let a concurrent first caller write to the old file mid-copy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from kiro_crew.apps import builtin_adoption_pins
from kiro_crew.apps.builtins.auto_research import handlers as h
from kiro_crew.apps.builtins.auto_research.campaign import storage
from kiro_crew.apps.manager import app_data_dir, app_dir


@pytest.fixture(autouse=True)
def _live_paths(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve against the test's isolated data home, with a fresh attempt record."""
    _floor_monkeypatch.setattr(storage, "DB_PATH", None)
    _floor_monkeypatch.setattr(storage, "_MIGRATION_ATTEMPTED", set())


def _count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    finally:
        conn.close()


def _seed_legacy(rows: int) -> sqlite3.Connection:
    """A WAL campaigns DB beside ``app.json`` whose rows are still in its ``-wal``.

    The returned connection stays open with automatic checkpoints off, so the
    rows are in the WAL file and not yet in the main file: a copy of the main
    file alone would lose them.
    """
    legacy = storage.legacy_db_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(legacy), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "CREATE TABLE campaigns (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "question TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    for i in range(rows):
        conn.execute(
            "INSERT INTO campaigns (id, name, question, created_at) VALUES (?, ?, ?, ?)",
            (f"c{i:04d}", f"n{i}", f"q{i}", float(i)),
        )
    assert Path(f"{legacy}-wal").stat().st_size > 0
    return conn


def test_the_database_is_in_the_app_data_directory():
    assert storage.data_db_path() == app_data_dir("auto-research") / "campaigns.db"
    assert storage.legacy_db_path() == app_dir("auto-research") / "campaigns.db"


def test_a_fresh_home_creates_the_database_in_data():
    conn = storage._get_db()
    conn.close()
    assert storage.db_path() == storage.data_db_path()
    assert storage.data_db_path().is_file()
    assert not os.path.lexists(storage.legacy_db_path())


def test_the_copy_carries_every_row_still_in_the_wal():
    holder = _seed_legacy(5)
    try:
        assert storage.db_path() == storage.legacy_db_path()
        assert storage.migrate_legacy_db() is True
    finally:
        holder.close()
    assert storage.db_path() == storage.data_db_path()
    assert _count(storage.data_db_path()) == 5
    assert [p.name for p in storage.data_db_path().parent.iterdir() if p.name.startswith(".")] == []
    # The schema migrations run on the copy like on any database.
    conn = storage._get_db()
    try:
        assert {r["id"] for r in conn.execute("SELECT id FROM campaigns")} == {
            f"c{i:04d}" for i in range(5)
        }
    finally:
        conn.close()


def test_the_old_file_and_its_sidecars_are_removed_once_copied():
    """A campaign deleted after the copy must not stay readable in a stale second copy."""
    _seed_legacy(3).close()
    legacy = storage.legacy_db_path()
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{legacy}{suffix}").write_bytes(b"")
    assert storage.migrate_legacy_db() is True
    assert _count(storage.data_db_path()) == 3
    assert [p.name for p in legacy.parent.iterdir()] == ["data"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_published_copy_is_owner_only():
    _seed_legacy(1).close()
    previous = os.umask(0o022)
    try:
        assert storage.migrate_legacy_db() is True
    finally:
        os.umask(previous)
    assert storage.data_db_path().stat().st_mode & 0o777 == 0o600


def test_a_database_appearing_during_the_copy_is_never_replaced(
    monkeypatch: pytest.MonkeyPatch,
):
    """The publish itself refuses a taken name, not only the check before it."""
    _seed_legacy(4).close()
    target = storage.data_db_path()
    real_prepare = storage.prepare_owner_only_sqlite

    def _prepare_then_plant(path: Path) -> None:
        real_prepare(path)
        if path.name.endswith(".migrating"):
            target.write_bytes(b"appeared meanwhile")

    monkeypatch.setattr(storage, "prepare_owner_only_sqlite", _prepare_then_plant)
    assert storage.migrate_legacy_db() is False
    assert target.read_bytes() == b"appeared meanwhile"
    assert _count(storage.legacy_db_path()) == 4
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_a_caller_arriving_mid_copy_waits_for_it(monkeypatch: pytest.MonkeyPatch):
    """A first ``_get_db`` racing the copy must not open, and write to, the old file."""
    _seed_legacy(2).close()
    in_copy, release, late_arrived = threading.Event(), threading.Event(), threading.Event()
    real_copy = storage._copy_database

    def _held_copy(source: Path, target: Path) -> None:
        in_copy.set()
        assert release.wait(10)
        real_copy(source, target)

    class _ObservedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __enter__(self) -> _ObservedLock:
            if threading.current_thread().name == "late-caller":
                late_arrived.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_exc: object) -> None:
            self._lock.release()

    monkeypatch.setattr(storage, "_copy_database", _held_copy)
    monkeypatch.setattr(storage, "_MIGRATION_LOCK", _ObservedLock())
    seen: dict[str, Path] = {}

    def _late() -> None:
        storage.migrate_legacy_db()
        seen["path"] = storage.db_path()
        late_arrived.set()

    first = threading.Thread(target=storage.migrate_legacy_db, name="first-caller")
    late = threading.Thread(target=_late, name="late-caller")
    try:
        first.start()
        assert in_copy.wait(10)
        late.start()
        assert late_arrived.wait(10)
        assert "path" not in seen, "the late caller went on while the copy was running"
    finally:
        release.set()
        first.join(10)
        late.join(10)
    assert seen["path"] == storage.data_db_path()
    assert _count(seen["path"]) == 2


def test_a_locked_old_database_does_not_hold_up_the_start(monkeypatch: pytest.MonkeyPatch):
    """The gateway's start awaits the copy, so a lock someone holds on the old file
    (here an exclusive-mode writer) makes the copy give up, not wait for it."""
    holder = _seed_legacy(2)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute(
        "INSERT INTO campaigns (id, name, question, created_at) VALUES ('w', 'w', 'w', 9)"
    )
    monkeypatch.setattr(storage, "_COPY_LOCK_WAIT_SECS", 0.0)
    outcome: list[bool] = []
    copier = threading.Thread(target=lambda: outcome.append(storage.migrate_legacy_db()))
    try:
        copier.start()
        copier.join(10)
        assert not copier.is_alive(), "the copy waited on the lock instead of giving up"
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        copier.join(10)
    assert outcome == [False]
    assert [p.name for p in storage.data_db_path().parent.iterdir()] == []
    assert storage.db_path() == storage.legacy_db_path()
    assert _count(storage.legacy_db_path()) == 2
    # The next start, with the lock gone, copies it.
    monkeypatch.setattr(storage, "_MIGRATION_ATTEMPTED", set())
    assert storage.migrate_legacy_db() is True
    assert _count(storage.db_path()) == 2


def test_auto_research_is_not_pinned_while_the_swap_can_retire_its_old_database():
    """The adoption swap keeps only ``data/``; nothing at the swap refuses an old
    ``campaigns.db`` beside ``app.json``. A user who never started a release that
    carries the copy would lose every campaign the first time the swap ran, so a
    pin row for this app must land with a swap-side refusal (or a carry) for that
    file, and this test is replaced by that refusal's own test."""
    table = json.loads(builtin_adoption_pins._PINS_PATH.read_text(encoding="utf-8"))
    assert "auto-research" not in {row["name"] for row in table["pins"]}


def test_the_rows_survive_removing_everything_but_data():
    """What an update or an adoption does to the app directory, by hand."""
    _seed_legacy(3).close()
    conn = storage._get_db()
    conn.close()
    for entry in app_dir("auto-research").iterdir():
        if entry.name != "data":
            entry.unlink()
    assert _count(storage.db_path()) == 3


def test_a_database_already_in_data_is_never_replaced():
    _seed_legacy(4).close()
    target = storage.data_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.execute("CREATE TABLE campaigns (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO campaigns (id) VALUES ('kept')")
    conn.commit()
    conn.close()
    before = target.read_bytes()
    assert storage.migrate_legacy_db() is False
    assert target.read_bytes() == before
    assert storage.db_path() == target


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_linked_old_database_is_not_copied_and_stays_in_use(tmp_path: Path):
    elsewhere = tmp_path / "operator-disk" / "campaigns.db"
    elsewhere.parent.mkdir()
    sqlite3.connect(str(elsewhere)).close()
    legacy = storage.legacy_db_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    try:
        legacy.symlink_to(elsewhere)
    except OSError:
        pytest.skip("symlinks are not permitted here")
    assert storage.migrate_legacy_db() is False
    assert not os.path.lexists(storage.data_db_path())
    assert storage.db_path() == legacy


def test_a_directory_at_the_old_name_is_not_copied():
    storage.legacy_db_path().mkdir(parents=True)
    assert storage.migrate_legacy_db() is False
    assert not os.path.lexists(storage.data_db_path())


def test_a_failed_copy_publishes_nothing_and_keeps_the_old_rows(monkeypatch: pytest.MonkeyPatch):
    _seed_legacy(2).close()

    class _Boom(sqlite3.Connection):
        def backup(self, *_a: Any, **_k: Any) -> None:  # type: ignore[override]
            raise sqlite3.OperationalError("disk I/O error")

    real_connect = sqlite3.connect
    monkeypatch.setattr(
        storage.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_Boom, **k)
    )
    assert storage.migrate_legacy_db() is False
    monkeypatch.setattr(storage.sqlite3, "connect", real_connect)
    assert storage.legacy_db_path().is_file()
    data = storage.data_db_path().parent
    assert not os.path.lexists(storage.data_db_path())
    assert [p.name for p in data.iterdir()] == []
    assert storage.db_path() == storage.legacy_db_path()
    assert _count(storage.db_path()) == 2
    # Not again in this process: a connection opened since may still write there.
    assert storage.migrate_legacy_db() is False
    # The next start retries, and the copy lands.
    monkeypatch.setattr(storage, "_MIGRATION_ATTEMPTED", set())
    assert storage.migrate_legacy_db() is True
    assert _count(storage.db_path()) == 2


def test_a_copy_failing_its_check_is_not_published_and_the_old_file_stays(
    monkeypatch: pytest.MonkeyPatch,
):
    _seed_legacy(2).close()

    class _Corrupt(sqlite3.Connection):
        def execute(self, sql: str, *a: Any) -> sqlite3.Cursor:  # type: ignore[override]
            if sql == "PRAGMA quick_check":
                return super().execute("SELECT 'row 1 missing from index'")
            return super().execute(sql, *a)

    real_connect = sqlite3.connect
    monkeypatch.setattr(
        storage.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_Corrupt, **k)
    )
    assert storage.migrate_legacy_db() is False
    monkeypatch.setattr(storage.sqlite3, "connect", real_connect)
    assert [p.name for p in storage.data_db_path().parent.iterdir()] == []
    assert storage.db_path() == storage.legacy_db_path()
    assert _count(storage.legacy_db_path()) == 2


def test_an_override_path_is_never_migrated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _seed_legacy(1).close()
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "pinned.db")
    assert storage.migrate_legacy_db() is False
    assert storage.db_path() == tmp_path / "pinned.db"
    assert not os.path.lexists(storage.data_db_path())


@pytest.mark.asyncio
async def test_startup_copies_the_database_while_the_app_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """The adoption swap runs at a later start before any route registers, so the
    copy cannot wait for the app to be enabled or a route to be hit."""
    _seed_legacy(3).close()

    class _Pool:
        def __init__(self, pool_size: int) -> None:
            self.shutdown = AsyncMock()

    monkeypatch.setattr(h, "LLMPool", _Pool)
    monkeypatch.setattr(h, "is_app_enabled", lambda _name: False)
    app = web.Application()
    h.register_routes(app)
    await app.on_startup[-1](app)
    try:
        assert storage.db_path() == storage.data_db_path()
        assert _count(storage.data_db_path()) == 3
    finally:
        await app.on_shutdown[-1](app)
