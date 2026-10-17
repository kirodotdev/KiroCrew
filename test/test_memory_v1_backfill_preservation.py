"""Opening a populated V1 file preserves its original rows during metadata backfill."""

import json
import struct
from contextlib import closing

from member_memory_helpers import forget_declared_stores, write_member_home

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config import loader
from kiro_crew.vector_memory import VectorMemoryStore

# The existing V1 schema after migrations 1, 2 and 3, before record metadata.
# Keep this fixture independent of the current initializer and its DDL.
_LEGACY_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE semantic_memory (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, confidence REAL DEFAULT 0.5,
    source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    is_deleted INTEGER DEFAULT 0, embedding BLOB
);
CREATE INDEX idx_semantic_deleted ON semantic_memory(is_deleted);
CREATE TABLE episodic_memories (
    id TEXT PRIMARY KEY, conversation_id TEXT, text TEXT NOT NULL, embedding BLOB,
    tags TEXT DEFAULT '[]', importance REAL DEFAULT 0.5, created_at TEXT NOT NULL,
    last_accessed_at TEXT, is_deleted INTEGER DEFAULT 0
);
CREATE INDEX idx_episodic_deleted ON episodic_memories(is_deleted);
CREATE INDEX idx_episodic_created ON episodic_memories(created_at);
CREATE INDEX idx_episodic_conversation ON episodic_memories(conversation_id);
CREATE TABLE memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    memory_type TEXT NOT NULL, memory_key TEXT NOT NULL, old_value TEXT,
    new_value TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX idx_events_type ON memory_events(memory_type, created_at);
CREATE INDEX idx_events_key ON memory_events(memory_key);
CREATE TABLE memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def _original_rows(db):
    return {
        "semantic": [
            tuple(row) for row in db.execute("SELECT * FROM semantic_memory ORDER BY key")
        ],
        "episodic": [
            tuple(row) for row in db.execute("SELECT * FROM episodic_memories ORDER BY id")
        ],
        "events": [tuple(row) for row in db.execute("SELECT * FROM memory_events ORDER BY id")],
        "meta": [tuple(row) for row in db.execute("SELECT * FROM memory_meta ORDER BY key")],
        "versions": [
            tuple(row) for row in db.execute("SELECT * FROM schema_version ORDER BY version")
        ],
        "event_counter": [
            tuple(row)
            for row in db.execute("SELECT * FROM sqlite_sequence WHERE name='memory_events'")
        ],
    }


def _revision_rows(db):
    return {
        "meta": [
            tuple(row) for row in db.execute("SELECT * FROM memory_record_meta ORDER BY record_id")
        ],
        "history": [tuple(row) for row in db.execute("SELECT * FROM memory_revisions ORDER BY id")],
    }


def test_original_v1_rows_survive_first_backfill_and_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    write_member_home(tmp_path)
    forget_declared_stores(monkeypatch)
    path = tmp_path / "memory.db"
    created = "2024-01-02T03:04:05.123456+00:00"
    updated = "2024-02-03T04:05:06.654321+00:00"
    vector = struct.pack("<ff", -0.0, 0.625)
    semantic = [
        (
            "user.email",
            '  "owner@example.net"  ',
            1.0,
            "user_explicit",
            created,
            updated,
            0,
            vector,
        ),
        (
            "lesson.keep_detail",
            '{ "rule": "保留细节", "negative": "不要丢失原文" }',
            0.875,
            "user_explicit",
            created,
            created,
            0,
            None,
        ),
        (
            "user.retired",
            '{ "old": [1, null, "é"] }',
            0.25,
            "consolidation:old",
            created,
            updated,
            1,
            b"",
        ),
    ]
    episodic = [
        (
            "episode-live",
            "dashboard:old",
            "First line\r\n原文\n",
            vector,
            '[ "work", "细节" ]',
            0.75,
            created,
            updated,
            0,
        ),
        ("episode-deleted", None, "Retained tombstone", None, "[]", 0.125, updated, None, 1),
    ]
    with closing(sqlite3.connect(str(path))) as db:
        db.executescript(_LEGACY_SCHEMA)
        db.executemany(
            "INSERT INTO schema_version VALUES (?, ?)", [(1, created), (2, created), (3, updated)]
        )
        db.executemany("INSERT INTO semantic_memory VALUES (?, ?, ?, ?, ?, ?, ?, ?)", semantic)
        db.executemany("INSERT INTO episodic_memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", episodic)
        db.execute(
            "INSERT INTO memory_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                42,
                "delete",
                "semantic",
                "user.retired",
                ' { "old": true } ',
                None,
                "user_explicit",
                updated,
            ),
        )
        db.executemany(
            "INSERT INTO memory_meta VALUES (?, ?, ?)",
            [
                ("embedding_space_sig", "legacy-model:2", created),
                ("legacy-extra", " retained \n", updated),
            ],
        )
        db.commit()
        before = _original_rows(db)
        assert before["semantic"] == sorted(semantic)
        assert before["episodic"] == sorted(episodic)
        assert (
            db.execute(
                "SELECT name FROM sqlite_schema WHERE name IN ('memory_record_meta', 'memory_revisions')"
            ).fetchall()
            == []
        )

    expected = {
        "key:user.email": ("fact", "active"),
        "key:lesson.keep_detail": ("directive", "active"),
        "key:user.retired": ("fact", "forgotten"),
        "episode-live": ("episode", "active"),
        "episode-deleted": ("episode", "forgotten"),
    }
    tier = VectorMemoryStore(db_path=path, embedding_dim=2)
    try:
        tier.init()
        assert tier.algorithm_version == "v1"
        assert tier._lineage == "v1"
        assert _original_rows(tier.db) == before
        actual = {
            row["record_id"]: (row["kind"], row["status"], row["revision"])
            for row in tier.db.execute("SELECT * FROM memory_record_meta")
        }
        assert actual == {key: (*value, 1) for key, value in expected.items()}
        revisions = list(tier.db.execute("SELECT * FROM memory_revisions ORDER BY record_id"))
        assert len(revisions) == len(expected)
        assert {row["record_id"] for row in revisions} == set(expected)
        for row in revisions:
            assert (row["revision"], row["base_revision"], row["status"]) == (1, 0, "accepted")
            assert (row["operation"], row["source"], row["before_json"]) == (
                "backfill",
                "legacy_import",
                None,
            )
            content = json.loads(row["after_json"])
            if row["record_id"] == "key:user.email":
                assert content["value_json"] == semantic[0][1]
                assert content["source"] == "user_explicit"
            if row["record_id"] == "episode-live":
                assert content["text"] == episodic[0][2]
                assert content["tags"] == episodic[0][4]
        backfilled = _revision_rows(tier.db)
        tier.close()
        tier.init()
        assert tier.algorithm_version == "v1"
        assert tier._lineage == "v1"
        assert _original_rows(tier.db) == before
        assert _revision_rows(tier.db) == backfilled
    finally:
        tier.close()
        loader._invalidate_config_cache()
