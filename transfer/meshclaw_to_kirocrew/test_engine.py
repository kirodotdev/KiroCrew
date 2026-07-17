"""Tests for kiro_crew.meshclaw_migrate — MeshClaw → KiroCrew data migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import engine as mm

_MEM_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE semantic_memory (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, confidence REAL DEFAULT 0.5,
    source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    is_deleted INTEGER DEFAULT 0, embedding BLOB);
CREATE TABLE episodic_memories (
    id TEXT PRIMARY KEY, conversation_id TEXT, text TEXT NOT NULL, embedding BLOB,
    tags TEXT DEFAULT '[]', importance REAL DEFAULT 0.5, created_at TEXT NOT NULL,
    last_accessed_at TEXT, is_deleted INTEGER DEFAULT 0);
CREATE TABLE memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    memory_type TEXT NOT NULL, memory_key TEXT NOT NULL, old_value TEXT,
    new_value TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def _make_memory_db(path: Path, *, semantic: list[str], episodic: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_MEM_SCHEMA)
        for k in semantic:
            conn.execute(
                "INSERT INTO semantic_memory(key,value_json,source,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (k, '{"v":1}', "test", "2026-01-01", "2026-01-01"),
            )
        for i in episodic:
            conn.execute(
                "INSERT INTO episodic_memories(id,text,created_at) VALUES (?,?,?)",
                (i, "hello", "2026-01-01"),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Point Path.home() at a tmp dir so both ~/.meshclaw and ~/.kirocrew are sandboxed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    src = home / ".meshclaw"
    dst = home / ".kirocrew"
    return home, src, dst


def _seed_meshclaw(src: Path) -> None:
    """Create a representative MeshClaw home."""
    (src / "sessions").mkdir(parents=True)
    (src / "sessions" / "chat-1.jsonl").write_text('{"role":"user"}', encoding="utf-8")
    (src / "uploads").mkdir()
    (src / "uploads" / "a.png").write_text("img", encoding="utf-8")
    _make_memory_db(src / "memory.db", semantic=["pref:a", "pref:b"], episodic=["e1"])
    # config + a path-bearing file
    (src / "config.json").write_text(
        json.dumps({"agents": {"x": 1}, "slack": {"token": "t"}, "tunnel": {"on": True}}),
        encoding="utf-8",
    )
    (src / "crons.json").write_text(
        json.dumps({"script": str(src / "crons" / "f.py")}), encoding="utf-8"
    )
    # secrets / per-install — must NOT be copied
    (src / ".env").write_text("SECRET=1", encoding="utf-8")
    (src / "token_signing.key").write_text("key", encoding="utf-8")
    (src / "gateway.lock").write_text("", encoding="utf-8")
    (src / "kiro_pids.txt").write_text("123", encoding="utf-8")


def test_detect_absent(homes):
    _, _src, _dst = homes
    info = mm.detect()
    assert info["available"] is False


def test_detect_present(homes):
    _, src, _dst = homes
    _seed_meshclaw(src)
    info = mm.detect()
    assert info["available"] is True
    assert info["summary"]["sessions"] == 1
    assert info["summary"]["memory"]["semantic_memory"] == 2
    assert info["summary"]["memory"]["episodic_memories"] == 1


def test_dry_run_writes_nothing(homes):
    _, src, dst = homes
    _seed_meshclaw(src)
    res = mm.migrate(dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    assert res["copied_dirs"]["sessions"] == 1
    assert res["backup_path"] is None
    # target untouched
    assert not (dst / "sessions" / "chat-1.jsonl").exists()
    assert not (dst / "memory.db").exists()


def test_migrate_copies_user_data(homes):
    _, src, dst = homes
    _seed_meshclaw(src)
    res = mm.migrate()
    assert res["ok"] is True
    assert (dst / "sessions" / "chat-1.jsonl").exists()
    assert (dst / "uploads" / "a.png").exists()
    assert (dst / "memory.db").exists()
    cnt = mm._memory_counts(dst / "memory.db")
    assert cnt["semantic_memory"] == 2 and cnt["episodic_memories"] == 1


def test_secrets_and_runtime_state_skipped(homes):
    _, src, dst = homes
    _seed_meshclaw(src)
    mm.migrate()
    assert not (dst / ".env").exists()
    assert not (dst / "token_signing.key").exists()
    assert not (dst / "gateway.lock").exists()
    assert not (dst / "kiro_pids.txt").exists()


def test_config_merge_fills_and_drops(homes):
    _, src, dst = homes
    _seed_meshclaw(src)
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "config.json").write_text(json.dumps({"agents": {}}), encoding="utf-8")
    res = mm.migrate()
    merged = json.loads((dst / "config.json").read_text(encoding="utf-8"))
    # slack filled (absent in target); tunnel dropped (legacy)
    assert merged["slack"] == {"token": "t"}
    assert "tunnel" not in merged
    assert "tunnel" in res["config"]["dropped"]


def test_path_rewrite_in_crons(homes):
    _, src, dst = homes
    _seed_meshclaw(src)
    mm.migrate()
    crons = (dst / "crons.json").read_text(encoding="utf-8")
    assert ".meshclaw" not in crons
    assert ".kirocrew" in crons


def test_backup_created_when_target_nonempty(homes):
    home, src, dst = homes
    _seed_meshclaw(src)
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "marker.txt").write_text("existing", encoding="utf-8")
    res = mm.migrate()
    assert res["backup_path"] is not None
    assert (Path(res["backup_path"]) / "marker.txt").exists()


def test_memory_row_merge_when_target_has_data(homes):
    _, src, dst = homes
    _seed_meshclaw(src)
    # target already has overlapping + distinct rows
    _make_memory_db(dst / "memory.db", semantic=["pref:a", "pref:c"], episodic=[])
    mm.migrate()
    cnt = mm._memory_counts(dst / "memory.db")
    # pref:a (dup, ignored) + pref:c (existing) + pref:b (new) = 3 unique
    assert cnt["semantic_memory"] == 3
    assert cnt["episodic_memories"] == 1


def test_idempotent_second_run_is_noop(homes):
    _, src, dst = homes
    _seed_meshclaw(src)
    mm.migrate()
    res2 = mm.migrate()
    assert res2["ok"] is True
    assert res2["copied_dirs"]["sessions"] == 0
    assert res2["copied_files"] == []
