"""The failure and legacy paths of three ``store_parts`` owners, driven on a real store.

``maintenance_sweeps``: a sweep runs as short ``BEGIN IMMEDIATE`` transactions, and one
that fails part-way must roll its transaction back and raise, leaving every row it had
not committed in place and the connection outside any transaction -- the next write on
it would otherwise run inside the dead one.

``fulltext_index``: a term-representation rebuild that fails part-way rolls the index
back to its legacy terms, and leaves the representation unknown and unlatched so the
next reader tries again.

``schema_migrations``: a library written before a column existed gains the column on its
next migrating open, with the column's default on every row already there, and a
read-only open of a file no migrating open has touched reports no vector generation
rather than failing the read.
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge import store as store_module
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def kstore(tmp_path, opened):
    return opened(KnowledgeStore(str(tmp_path / "knowledge.db")))


def _orphan_source(store: KnowledgeStore) -> str:
    """A source the orphan predicate selects: an ended ingest, and nothing under it."""
    source_id = store.add_source("gone", "url", "https://example.invalid/gone")
    store.update_source(source_id, sync_status="synced")
    return source_id


def _source_ids(store: KnowledgeStore) -> set[str]:
    return {row["id"] for row in store.db.execute("SELECT id FROM sources").fetchall()}


class TestASweepThatFailsRollsBack:
    def test_a_refused_orphan_delete_rolls_back_and_keeps_the_source(self, kstore):
        source_id = _orphan_source(kstore)
        # A finished job does not protect the source, so the sweep deletes it first --
        # inside the same transaction as the source row the trigger then refuses.
        kstore.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('job-done', ?, 'completed', '2000-01-01T00:00:00', '2000-01-01T00:00:00')",
            (source_id,),
        )
        # Refuse the delete on this connection only: the sweep runs on the calling
        # thread, so it takes this same connection through ``db``.
        kstore.db.execute(
            "CREATE TEMP TRIGGER refuse_source_delete BEFORE DELETE ON sources "
            "BEGIN SELECT RAISE(ABORT, 'source delete refused'); END"
        )
        with pytest.raises(store_module.sqlite3.IntegrityError, match="source delete refused"):
            kstore.reclaim_orphans()
        assert source_id in _source_ids(kstore)
        jobs = kstore.db.execute("SELECT id FROM ingestion_jobs").fetchall()
        assert [row["id"] for row in jobs] == ["job-done"]
        assert not kstore.db.in_transaction

    def test_a_failed_entity_prune_rolls_back_and_raises(self, kstore, monkeypatch):
        kept = kstore.add_entity("kept", "concept")
        gone = kstore.add_entity("gone", "concept")
        kstore.add_entity_relation(kept, gone, "relates")
        # A relation left pointing at a deleted entity: the sweep's own transaction deletes
        # it before the prune that is about to fail.
        kstore.db.execute("PRAGMA foreign_keys=OFF")
        kstore.db.execute("DELETE FROM entities WHERE id = ?", (gone,))
        kstore.db.execute("PRAGMA foreign_keys=ON")

        def refuse() -> None:
            raise RuntimeError("prune refused")

        monkeypatch.setattr(kstore, "_prune_orphan_entities", refuse)
        with pytest.raises(RuntimeError, match="prune refused"):
            kstore.reclaim_orphans()
        assert kstore.db.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0] == 1
        assert not kstore.db.in_transaction

    def test_a_failed_residue_delete_rolls_back_and_keeps_the_item(self, kstore, monkeypatch):
        source_id = kstore.add_source("Auto-added", "agent", "agent://")
        item_id = kstore.add_item(
            "doc", "body", "document", source_id=source_id, content_hash="h-residue"
        )
        # The marker an interrupted ingest leaves: same hash, started before the item.
        kstore.db.execute(
            "INSERT INTO agent_ingest_intent (source_id, slug, content_hash, started_at) "
            "VALUES (?, 'doc', 'h-residue', '0000-01-01T00:00:00')",
            (source_id,),
        )

        delete = kstore.delete_items_batch_in_txn

        def delete_then_refuse(item_ids, owner_source_id=None) -> None:
            delete(item_ids, owner_source_id=owner_source_id)
            raise RuntimeError("residue delete refused")

        monkeypatch.setattr(kstore, "delete_items_batch_in_txn", delete_then_refuse)
        with pytest.raises(RuntimeError, match="residue delete refused"):
            kstore.reclaim_agent_source_residue()
        assert kstore.get_item(item_id) is not None
        assert not kstore.db.in_transaction


class TestAFailedIndexRebuildKeepsTheLegacyIndex:
    def test_a_rebuild_that_fails_part_way_rolls_back_and_is_retried(self, kstore, monkeypatch):
        index = kstore._fts_index
        kstore.add_item("run", "body text", "note")
        # A fresh library still declares the legacy term representation.
        assert kstore.db.execute("PRAGMA user_version").fetchone()[0] == 0

        def refuse(rowid, title, content, tags) -> None:
            raise RuntimeError("index write refused")

        monkeypatch.setattr(kstore, "_fts_index", refuse)
        with pytest.raises(RuntimeError, match="index write refused"):
            kstore.ensure_fts_index_current()
        assert not kstore.db.in_transaction
        assert kstore.db.execute("PRAGMA user_version").fetchone()[0] == 0
        # Unknown rather than False, and not latched, so the next reader retries.
        assert kstore._fts_segmented is None
        assert kstore._fts_index_current is False
        # The rollback undid the reset: the legacy index still matches the item.
        hits = kstore.db.execute(
            "SELECT rowid FROM items_fts WHERE items_fts MATCH 'body'"
        ).fetchall()
        assert len(hits) == 1
        # The next reader migrates once the writes are accepted again.
        monkeypatch.setattr(kstore, "_fts_index", index)
        kstore.ensure_fts_index_current()
        assert kstore._fts_index_current is True
        version = kstore.db.execute("PRAGMA user_version").fetchone()[0]
        assert version == store_module.FTS_INDEX_VERSION
        assert [row["title"] for row in kstore.search_items_fts("body")] == ["run"]


# Each table as a library written before the column migrations existed held it: the
# columns ``_init_schema`` indexes are present, every column ``_migrate`` adds is not.
_LEGACY_SCHEMA = """
    CREATE TABLE sources (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, source_type TEXT NOT NULL,
        uri TEXT UNIQUE NOT NULL, properties TEXT DEFAULT '{}', last_synced TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE items (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
        item_type TEXT NOT NULL, source_id TEXT REFERENCES sources(id),
        chunk_index INTEGER DEFAULT 0, summary TEXT, tags TEXT DEFAULT '[]',
        embedding BLOB, status TEXT DEFAULT 'active',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE ingestion_jobs (
        id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id),
        status TEXT DEFAULT 'pending', items_total INTEGER DEFAULT 0,
        items_processed INTEGER DEFAULT 0, error TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE folder_file_state (
        source_id TEXT NOT NULL REFERENCES sources(id), file_path TEXT NOT NULL,
        content_hash TEXT, mtime REAL, item_ids TEXT DEFAULT '[]',
        last_seen TEXT NOT NULL, PRIMARY KEY (source_id, file_path)
    );
    CREATE TABLE artifact_item_state (
        source_id TEXT NOT NULL REFERENCES sources(id), slug TEXT NOT NULL,
        content_hash TEXT, item_ids TEXT DEFAULT '[]', updated_at TEXT NOT NULL,
        PRIMARY KEY (source_id, slug)
    );
    CREATE TABLE agent_item_state (
        source_id TEXT NOT NULL REFERENCES sources(id), slug TEXT NOT NULL,
        content_hash TEXT, item_ids TEXT DEFAULT '[]', updated_at TEXT NOT NULL,
        name TEXT, PRIMARY KEY (source_id, slug)
    );
"""

#: Column -> the value a row already present must read once the column is added.
_ADDED_COLUMNS = {
    "items": {
        "namespace": "default",
        "embedding_sig": None,
        "embedded_at": None,
        "content_hash": None,
    },
    "ingestion_jobs": {"items_failed": 0},
    "sources": {"sync_status": "pending", "summary_topic": None, "summary_themes": None},
    "folder_file_state": {
        "status": "pending",
        "error_message": None,
        "merged_into_source_id": None,
        "text_hash": None,
        "attempts": 0,
    },
    "artifact_item_state": {
        "name": None,
        "status": "active",
        "merged_into_source_id": None,
        "kind": None,
    },
    "agent_item_state": {"status": "active", "merged_into_source_id": None, "source_uri": None},
}


def _legacy_library(path) -> None:
    # The store's own driver: one SQLite library touches the file.
    conn = store_module.sqlite3.connect(path)
    try:
        conn.executescript(_LEGACY_SCHEMA)
        stamp = "2000-01-01T00:00:00"
        conn.execute(
            "INSERT INTO sources (id, name, source_type, uri, created_at, updated_at) "
            "VALUES ('s1', 'notes', 'local_folder', '/srv/notes', ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, created_at, "
            "updated_at) VALUES ('i1', 't', 'c', 'note', 's1', ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO ingestion_jobs (id, source_id, created_at, updated_at) "
            "VALUES ('j1', 's1', ?, ?)",
            (stamp, stamp),
        )
        for table, key in (
            ("folder_file_state", "file_path"),
            ("artifact_item_state", "slug"),
            ("agent_item_state", "slug"),
        ):
            stamp_col = "last_seen" if table == "folder_file_state" else "updated_at"
            conn.execute(
                f"INSERT INTO {table} (source_id, {key}, {stamp_col}) "  # noqa: S608
                "VALUES ('s1', 'k1', ?)",
                (stamp,),
            )
        conn.commit()
    finally:
        conn.close()


class TestALegacyLibraryMigratesOnOpen:
    def test_every_missing_column_is_added_with_its_default(self, tmp_path, opened):
        path = str(tmp_path / "legacy.db")
        _legacy_library(path)
        store = opened(KnowledgeStore(path))
        for table, added in _ADDED_COLUMNS.items():
            columns = {row[1] for row in store.db.execute(f"PRAGMA table_info({table})")}
            assert set(added) <= columns, table
            row = store.db.execute(
                f"SELECT {', '.join(added)} FROM {table}"
            ).fetchone()  # noqa: S608
            assert dict(row) == added, table
        # The rows the library held are still there, now readable through the store.
        assert store.get_item("i1")["namespace"] == "default"
        assert store.get_source_by_uri("/srv/notes")["sync_status"] == "pending"

    def test_a_second_open_changes_nothing_the_first_added(self, tmp_path, opened):
        path = str(tmp_path / "legacy.db")
        _legacy_library(path)

        def schema(store: KnowledgeStore) -> dict[str, str]:
            rows = store.db.execute("SELECT name, sql FROM sqlite_schema WHERE sql IS NOT NULL")
            return {row["name"]: row["sql"] for row in rows}

        first = opened(KnowledgeStore(path))
        migrated = schema(first)
        first._close_all_for_tests()
        assert schema(opened(KnowledgeStore(path))) == migrated

    def test_a_read_only_open_of_an_unmigrated_file_has_no_vector_generation(
        self, tmp_path, opened
    ):
        path = str(tmp_path / "legacy.db")
        _legacy_library(path)
        store = opened(KnowledgeStore.open_read_only(path))
        assert store.vector_generation() is None
