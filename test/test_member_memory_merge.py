"""Merging the rows a member store lacks back from an older copy of the same store."""

from __future__ import annotations

import hashlib
import threading
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from member_memory_helpers import MEMBERS, forget_declared_stores, write_member_home

from kiro_crew import member_memory_backup, member_memory_merge, memory_schema
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config import loader
from kiro_crew.vector_memory import open_member_database

pytestmark = pytest.mark.xdist_group("member_memory_backup")

FACETS = memory_schema.MemoryFacets(
    surface="dashboard", crew="worker", session_key="dashboard:chat-7", scope="lantern"
)
ITEM_FIELDS = (
    "id, kind, key, text, value_json, source, created_at, updated_at, scope, surface, crew, "
    "session_key, derived_from, importance, confidence"
)


def copy_db(src: Path, dst: Path) -> Path:
    """A consistent copy of *src* at *dst* through SQLite's backup API."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(src.absolute().as_uri() + "?mode=ro", uri=True)) as s:
        with closing(sqlite3.connect(str(dst))) as d:
            s.backup(d)
    return dst


def rows(path: Path, where: str = "1=1") -> dict[str, tuple]:
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        return {
            r[0]: tuple(r)
            for r in db.execute(f"SELECT {ITEM_FIELDS} FROM memory_items WHERE {where}")
        }


def item_id_with_text(store, text: str) -> str:
    return next(r["id"] for r in store.get_episodic_list() if r["text"] == text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Alice's store as a restore left it, and the superseded copy holding later rows."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    write_member_home(tmp_path, *MEMBERS)
    forget_declared_stores(monkeypatch)
    live = tmp_path / "memory_stores" / "member-alice" / "memory.db"
    store = open_member_database(live, member_id="alice", store_id="member-alice")
    store.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    older = copy_db(live, tmp_path / "backup" / "memory.db")
    store.write_episodic(
        "Deployed the lantern build to staging.", defer_embedding=True, facets=FACETS
    )
    store.set_semantic(
        "project.region", "us-west-2", 0.9, "consolidation:dashboard:chat-7", facets=FACETS
    )
    store.write_lesson("Check backups before restore")
    superseded = copy_db(live, tmp_path / "superseded" / "memory.db")
    store.close()
    # The restore: the older copy takes the live path.
    for suffix in ("-wal", "-shm"):
        Path(str(live) + suffix).unlink(missing_ok=True)
    live.write_bytes(older.read_bytes())
    store = open_member_database(live, member_id="alice", store_id="member-alice")
    store.write_episodic("Written after the restore.", defer_embedding=True)
    try:
        yield SimpleNamespace(live=live, store=store, superseded=superseded, root=tmp_path)
    finally:
        store.close()
        loader._invalidate_config_cache()


def test_apply_adds_missing_rows_with_their_provenance(env):
    lost = {rid: row for rid, row in rows(env.superseded).items() if rid not in rows(env.live)}
    assert len(lost) == 3

    report = member_memory_merge.merge_missing_rows(env.store, env.superseded, apply=True)

    assert report.applied and report.total_added == 3 and not report.conflicts
    assert report.added == {"directive": 1, "episode": 1, "fact": 1}
    after = rows(env.live)
    for rid, row in lost.items():
        assert after[rid] == row
    region = after["key:project.region"]
    assert region[9:12] == ("dashboard", "worker", "dashboard:chat-7")
    episode = item_id_with_text(env.store, "Deployed the lantern build to staging.")
    assert after[episode][6] == lost[episode][6]  # created_at, not the merge time
    with closing(sqlite3.connect(str(env.live))) as db:
        assert db.execute(
            "SELECT count(*) FROM memory_record_meta WHERE record_id='key:project.region'"
        ).fetchone() == (1,)
        assert (
            db.execute(
                "SELECT count(*) FROM memory_revisions WHERE record_id='key:project.region'"
            ).fetchone()[0]
            >= 1
        )
        assert db.execute(
            "SELECT count(*) FROM memory_fts WHERE path='key:project.region'"
        ).fetchone() == (1,)
    # The row written after the restore is still there, and recall reads a merged row.
    assert item_id_with_text(env.store, "Written after the restore.")
    assert env.store.get_semantic("project.region") is not None


def test_rows_already_present_are_skipped_and_a_second_merge_adds_nothing(env):
    first = member_memory_merge.merge_missing_rows(env.store, env.superseded, apply=True)
    # project.database is in both copies.
    assert first.duplicates == 1

    second = member_memory_merge.merge_missing_rows(env.store, env.superseded, apply=True)

    assert second.total_added == 0 and second.duplicates == 4


def test_a_fact_with_a_new_key_and_a_live_facts_value_is_added(env):
    # A fact's text is its value: project.engine shares project.database's text.
    source = open_member_database(env.superseded, member_id="alice", store_id="member-alice")
    try:
        source.set_semantic("project.engine", "PostgreSQL", 1.0, "user_explicit")
    finally:
        source.close()

    report = member_memory_merge.merge_missing_rows(env.store, env.superseded, apply=True)

    assert report.added == {"directive": 1, "episode": 1, "fact": 2}
    assert env.store.get_semantic("project.engine") is not None


def test_a_row_the_live_store_deleted_stays_deleted(env):
    env.store.set_semantic("project.region", "eu-west-1", 0.9, "user_explicit")
    env.store.delete_semantic("project.region", "user_explicit")

    report = member_memory_merge.merge_missing_rows(env.store, env.superseded, apply=True)

    assert report.added == {"directive": 1, "episode": 1}
    assert env.store.get_semantic("project.region") is None


def test_dry_run_writes_nothing(env):
    with closing(sqlite3.connect(str(env.live))) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(env.live.read_bytes()).hexdigest()
    wal = Path(str(env.live) + "-wal")
    wal_before = wal.read_bytes() if wal.exists() else b""

    report = member_memory_merge.merge_missing_rows(env.store, env.superseded)

    assert not report.applied and report.total_added == 3
    assert hashlib.sha256(env.live.read_bytes()).hexdigest() == before
    assert (wal.read_bytes() if wal.exists() else b"") == wal_before
    assert set(rows(env.superseded)) - set(rows(env.live))
    lines = member_memory_merge.report_lines(report, env.superseded)
    assert lines[0].startswith("Would add 3 row(s)") and "DRY RUN" in lines[-1]


def test_a_write_made_while_the_merge_runs_is_kept(env, monkeypatch):
    other = open_member_database(env.live, member_id="alice", store_id="member-alice")
    outcome: dict[str, object] = {}
    writer = threading.Thread(
        target=lambda: outcome.setdefault(
            "written",
            other.write_episodic("Written while the merge ran.", defer_embedding=True),
        )
    )
    original = member_memory_merge._copy_row

    def copy_then_race(*args, **kwargs):
        # A second connection writes while the merge holds its transaction open.
        if not writer.is_alive() and "written" not in outcome:
            writer.start()
        return original(*args, **kwargs)

    monkeypatch.setattr(member_memory_merge, "_copy_row", copy_then_race)
    try:
        report = member_memory_merge.merge_missing_rows(env.store, env.superseded, apply=True)
        writer.join(timeout=30)
        assert not writer.is_alive()
        assert outcome["written"] is True
        assert report.total_added == 3
        texts = {r["text"] for r in other.get_episodic_list()}
        assert {"Written while the merge ran.", "Deployed the lantern build to staging."} <= texts
    finally:
        other.close()


def test_a_source_from_another_member_is_refused(env):
    bob = env.root / "memory_stores" / "member-bob" / "memory.db"
    open_member_database(bob, member_id="bob", store_id="member-bob").close()
    foreign = copy_db(bob, env.root / "foreign" / "memory.db")

    with pytest.raises(ValueError):
        with member_memory_merge.source_database(foreign, env.live):
            pass


def test_a_backup_bundle_is_refused_and_nothing_is_written(env):
    bundle = env.root / "memory.20261127T000000000000Z-0.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("memory.db", env.superseded.read_bytes())
    before = rows(env.live)

    with pytest.raises(ValueError, match="memory.db"):
        with member_memory_merge.source_database(bundle, env.live):
            pass

    assert rows(env.live) == before


def test_cli_merge_is_a_dry_run_until_apply(env, capsys):
    import argparse

    from kiro_crew.cli_commands_memory import _memory_cmd
    from kiro_crew.cli_parser_agent import register_memory_parser

    parser = argparse.ArgumentParser()
    register_memory_parser(parser.add_subparsers(dest="command"))
    base = ["memory", "restore", "--store", "member-alice", "--merge-from", str(env.superseded)]

    _memory_cmd(parser.parse_args(base))
    assert "DRY RUN" in capsys.readouterr().out
    assert set(rows(env.superseded)) - set(rows(env.live))

    _memory_cmd(parser.parse_args([*base, "--apply"]))
    assert capsys.readouterr().out.startswith("Added 3 row(s)")
    assert not set(rows(env.superseded)) - set(rows(env.live))

    with pytest.raises(SystemExit):
        _memory_cmd(parser.parse_args(["memory", "restore", "--store", "member-alice", "--apply"]))
    assert "--apply only goes with --merge-from" in capsys.readouterr().err


def test_cli_empty_merge_from_is_refused_and_stages_no_restore(env, capsys):
    import argparse

    from kiro_crew.cli_commands_memory import _memory_cmd
    from kiro_crew.cli_parser_agent import register_memory_parser

    # A backup exists, so a fall-through to a whole-store restore would stage one.
    member_memory_backup.backup_store(env.live)
    parser = argparse.ArgumentParser()
    register_memory_parser(parser.add_subparsers(dest="command"))
    pending = member_memory_backup.backup_directory(env.live) / member_memory_backup.PENDING

    for extra in ([], ["--apply"]):
        args = ["memory", "restore", "--store", "member-alice", "--merge-from", "", *extra]
        with pytest.raises(SystemExit):
            _memory_cmd(parser.parse_args(args))
        assert "--merge-from names no source" in capsys.readouterr().err
        assert not pending.exists()
