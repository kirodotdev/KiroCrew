"""A codex runtime's private SQLite home starts from a completed index.

The private home lives in the runtime's scratch directory, so without a seed
codex rebuilds its whole index from the rollouts at every start, and a large
history outlasts the initialize timeout. ``seed_private_state`` copies a
completed ``state_5.sqlite`` from ``CODEX_HOME`` before the child starts and
falls back to a cold start whenever the copy cannot be trusted.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kiro_crew.acp import codex_sqlite
from kiro_crew.acp.codex_sqlite import SQLITE_HOME_ENV, seed_private_state

STATE = "state_5.sqlite"


def _make_state(
    path: Path, *, status: str = "complete", threads: int = 3, wal: bool = False
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        if wal:
            db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE backfill_state (id INTEGER PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO backfill_state VALUES (1, ?)", (status,))
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO threads VALUES (?)", [(f"t{i}",) for i in range(threads)])
        db.commit()


def _read(path: Path) -> tuple[str, int]:
    with closing(sqlite3.connect(path)) as db:
        status = db.execute("SELECT status FROM backfill_state WHERE id = 1").fetchone()[0]
        count = db.execute("SELECT count(*) FROM threads").fetchone()[0]
    return status, count


@pytest.fixture
def homes(tmp_path):
    codex_home = tmp_path / "codex"
    scratch = tmp_path / "scratch"
    codex_home.mkdir()
    scratch.mkdir()
    return codex_home, scratch, {"CODEX_HOME": str(codex_home)}


def _leftovers(scratch: Path) -> list[str]:
    return sorted(p.name for p in scratch.iterdir() if p.name != STATE)


def test_a_complete_index_is_copied_into_scratch(homes):
    codex_home, scratch, env = homes
    _make_state(codex_home / STATE, threads=5)

    seed_private_state(env, scratch)

    assert _read(scratch / STATE) == ("complete", 5)
    assert _leftovers(scratch) == []
    assert _read(codex_home / STATE) == ("complete", 5)


def test_rows_still_in_the_source_wal_are_copied(homes):
    codex_home, scratch, env = homes
    source = codex_home / STATE
    _make_state(source, threads=2, wal=True)
    # A writer that stays open keeps its commits in the WAL, not the main file.
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO threads VALUES ('late')")
        writer.commit()
        seed_private_state(env, scratch)

    assert _read(scratch / STATE) == ("complete", 3)


def test_an_operator_sqlite_home_skips_the_seed(homes):
    codex_home, scratch, env = homes
    _make_state(codex_home / STATE)

    seed_private_state({**env, SQLITE_HOME_ENV: "/operator/choice"}, scratch)

    assert list(scratch.iterdir()) == []


def test_an_existing_destination_is_left_alone(homes):
    codex_home, scratch, env = homes
    _make_state(codex_home / STATE, threads=5)
    _make_state(scratch / STATE, threads=1)

    seed_private_state(env, scratch)

    assert _read(scratch / STATE) == ("complete", 1)
    assert _leftovers(scratch) == []


def test_a_missing_source_means_a_cold_start(homes):
    _codex_home, scratch, env = homes

    seed_private_state(env, scratch)

    assert list(scratch.iterdir()) == []


def test_codex_home_defaults_to_dot_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_sqlite.Path, "home", classmethod(lambda cls: tmp_path))
    _make_state(tmp_path / ".codex" / STATE, threads=4)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    seed_private_state({}, scratch)

    assert _read(scratch / STATE) == ("complete", 4)


def test_an_incomplete_backfill_means_a_cold_start(homes, caplog):
    codex_home, scratch, env = homes
    _make_state(codex_home / STATE, status="running")

    with caplog.at_level(logging.WARNING, logger=codex_sqlite.__name__):
        seed_private_state(env, scratch)

    assert list(scratch.iterdir()) == []
    assert "incomplete" in caplog.text


def test_a_source_that_is_not_a_database_means_a_cold_start(homes, caplog):
    codex_home, scratch, env = homes
    (codex_home / STATE).write_bytes(b"not a sqlite file at all" * 64)

    with caplog.at_level(logging.WARNING, logger=codex_sqlite.__name__):
        seed_private_state(env, scratch)

    assert list(scratch.iterdir()) == []
    assert "cold initialization" in caplog.text


def test_a_copy_that_fails_after_staging_leaves_nothing_behind(homes):
    codex_home, scratch, env = homes
    source = codex_home / STATE
    # Complete backfill but no threads table: the check after the backup fails.
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE backfill_state (id INTEGER PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO backfill_state VALUES (1, 'complete')")
        db.commit()

    seed_private_state(env, scratch)

    assert list(scratch.iterdir()) == []


def test_a_copy_over_its_time_budget_leaves_nothing_behind(homes, monkeypatch):
    codex_home, scratch, env = homes
    _make_state(codex_home / STATE)
    monkeypatch.setattr(codex_sqlite, "_BACKUP_TIMEOUT_SECS", -1.0)

    seed_private_state(env, scratch)

    assert list(scratch.iterdir()) == []
