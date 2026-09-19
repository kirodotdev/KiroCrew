"""The member database is the sole learned authority and opens never provision it."""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime

# pysqlite3 omits Connection.iterdump; use the same SQL dumper on either driver.
from sqlite3.dump import _iterdump as iter_sql_dump

import pytest

from kiro_crew import memory_stores
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.hooks import FileTooLargeError
from kiro_crew.memory import MemoryStore
from kiro_crew.vector_memory import (
    VectorMemoryStore,
    create_member_database,
    open_member_database,
    read_member_database_identity,
)


def test_empty_pysqlite_package_admission_uses_reader_fallback(tmp_path):
    # A fresh interpreter exercises both selectors without reloading shared
    # application modules or leaking the fabricated package into other tests.
    script = textwrap.dedent("""
        import sqlite3
        import sys
        from pathlib import Path
        from types import ModuleType, SimpleNamespace

        sys.modules['pysqlite3'] = ModuleType('pysqlite3')
        from kiro_crew import execution_context, memory_stores, vector_memory
        from kiro_crew.config.sections import KiroCrewAgentConfig, MemoryStoreConfig

        assert not hasattr(sys.modules['pysqlite3'], 'connect')
        assert vector_memory.sqlite3 is sqlite3
        path = Path.cwd() / 'memory_stores' / 'alice-store' / 'memory.db'
        vector_memory.create_member_database(path, member_id='alice-id', store_id='alice-store')
        config = SimpleNamespace(
            agents={'alice': KiroCrewAgentConfig(member_id='alice-id', memory_store='alice-store')},
            memory_stores={'alice-store': MemoryStoreConfig(
                owner_member='alice', owner_member_id='alice-id', memory_version=2)},
        )
        admitted = execution_context.resolve_member_execution(config, 'alice')
        checks = (
            lambda: execution_context.validate_execution(admitted),
            lambda: memory_stores.require_memory_store('alice-store', config=config),
        )
        for check in checks:
            check()
        path.unlink()
        for damaged in (False, True):
            if damaged:
                path.write_bytes(b'existing damaged data')
            for check in checks:
                try:
                    check()
                except memory_stores.UnknownMemoryStore as error:
                    assert 'Global was not used' in str(error)
                    assert isinstance(error.__cause__, sqlite3.Error)
                else:
                    raise AssertionError('Unreadable member database was admitted')
                assert admitted.store.store_id == 'alice-store'
                assert not (Path.cwd() / 'memory.db').exists()
                if damaged:
                    assert path.read_bytes() == b'existing damaged data'
                else:
                    assert not path.exists()
        """)
    env = dict(os.environ, KIROCREW_HOME=str(tmp_path), KIRO_HOME=str(tmp_path / "kiro"))
    env.update(TMPDIR=str(tmp_path), TMP=str(tmp_path), TEMP=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def member_db(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
    path = tmp_path / "memory_stores" / "alice-store" / "memory.db"
    create_member_database(path, member_id="alice-id", store_id="alice-store")
    store = open_member_database(path, member_id="alice-id", store_id="alice-store")
    try:
        yield store
    finally:
        store.close()


def test_open_refuses_missing_corrupt_and_mismatched_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    directory = tmp_path / "memory_stores" / "alice-store"
    directory.mkdir(parents=True)
    path = directory / "memory.db"
    with pytest.raises(sqlite3.OperationalError):
        open_member_database(path, member_id="alice", store_id="alice-store")
    assert not path.exists()
    path.write_bytes(b"existing damaged data")
    before = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        open_member_database(path, member_id="alice", store_id="alice-store")
    assert path.read_bytes() == before
    with pytest.raises(FileExistsError):
        create_member_database(path, member_id="alice", store_id="alice-store")
    assert path.read_bytes() == before
    path = directory / "valid.db"
    create_member_database(path, member_id="alice", store_id="alice-store")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="identity"):
        open_member_database(path, member_id="bob", store_id="alice-store")
    assert path.read_bytes() == before
    assert read_member_database_identity(path) == ("alice", "alice-store")


def test_history_search_and_edits_share_database_without_learned_files(member_db):
    facade = MemoryStore(
        workspace=member_db._db_path.parent, memory_version=2, vector_store=member_db
    )
    facade.init()
    facade.append_history("The release codename is aurora.")
    assert "aurora" in facade.read_recent_history()
    assert facade.search("aurora")[0]["path"].startswith("history:")
    old = facade.read_editable_history()
    facade.append_history("A concurrent session added a note.")
    assert not facade.write_today_history(
        "stale", expected_baseline=old, validate_current=lambda value: None
    )
    current = facade.read_editable_history()
    assert facade.write_today_history(
        "Current release: borealis", expected_baseline=current, validate_current=lambda value: None
    )
    assert not facade.search("aurora")
    assert facade.search("borealis")
    assert member_db.db.execute("SELECT revision FROM memory_history").fetchone()[0] == 3
    assert not (member_db._db_path.parent / "memory").exists()
    assert not (member_db._db_path.parent / "memory_index.db").exists()
    assert not (member_db._db_path.parent / "lessons.jsonl").exists()


def test_obsolete_full_transcript_receipt_schema_refuses_without_repair(member_db):
    from kiro_crew.vector_memory import sqlite3 as reader_sqlite

    path = member_db._db_path
    member_db.db.execute("DROP TABLE memory_consolidations")
    member_db.db.execute(
        "CREATE TABLE memory_consolidations (source_id TEXT PRIMARY KEY, session_key TEXT, "
        "source_snapshot TEXT, receipt_json TEXT, created_at TEXT)"
    )
    member_db.db.commit()
    member_db.close()
    before = path.read_bytes()
    with pytest.raises(reader_sqlite.Error, match="source_total"):
        read_member_database_identity(path)
    with pytest.raises(reader_sqlite.Error, match="source_total"):
        open_member_database(path, member_id="alice-id", store_id="alice-store")
    assert path.read_bytes() == before


@pytest.mark.parametrize("reader", ["read_history_entries", "read_editable_history"])
def test_member_history_refuses_oversized_body_before_materializing(member_db, reader):
    limit = MemoryStore._HISTORY_SNAPSHOT_MAX_BYTES
    day = datetime.now().astimezone().date().isoformat()
    # Seed only the history row: this is a read-bound test, not an FTS write test.
    with member_db.db:
        member_db.db.execute(
            "INSERT INTO memory_history VALUES (?,?,1,?)", (day, "x" * (limit + 1), "now")
        )
    changes = member_db.db.total_changes

    def refuse_oversized_decode(raw):
        assert len(raw) <= limit, "SQLite returned the oversized body to Python"
        return raw.decode("utf-8")

    original = member_db.db.text_factory
    member_db.db.text_factory = refuse_oversized_decode
    try:
        with pytest.raises(FileTooLargeError):
            getattr(member_db, reader)()
    finally:
        member_db.db.text_factory = original
    assert member_db.db.total_changes == changes
    assert (
        member_db.db.execute(
            "SELECT length(CAST(content AS BLOB)) FROM memory_history WHERE day=?", (day,)
        ).fetchone()[0]
        == limit + 1
    )


@pytest.mark.parametrize("content", ["ééé", "a\x00bbbb", "🌙🌙"])
def test_member_history_first_day_budget_counts_utf8_bytes(member_db, content):
    with member_db.db:
        member_db.db.execute(
            "INSERT INTO memory_history VALUES ('2026-09-18',?,1,'now')", (content,)
        )
    with pytest.raises(FileTooLargeError):
        member_db.read_history_entries(max_bytes=5)


@pytest.mark.parametrize("reader", ["read_history_entries", "read_editable_history"])
def test_member_history_exact_byte_limit_returns_complete_unicode(member_db, reader):
    limit = MemoryStore._HISTORY_SNAPSHOT_MAX_BYTES
    day = datetime.now().astimezone().date().isoformat()
    content = "🌙" * (limit // 4)
    with member_db.db:
        member_db.db.execute("INSERT INTO memory_history VALUES (?,?,1,'now')", (day, content))
    result = getattr(member_db, reader)()
    assert (result[0]["content"] if isinstance(result, list) else result) == content


def test_member_history_total_budget_preserves_whole_newest_days(member_db):
    rows = [("2026-09-16", "🌙"), ("2026-09-17", "a\x00b"), ("2026-09-18", "éé")]
    with member_db.db:
        member_db.db.executemany("INSERT INTO memory_history VALUES (?,?,1,'now')", rows)
    changes = member_db.db.total_changes
    entries = member_db.read_history_entries(max_bytes=7)
    assert [(entry["date"], entry["content"]) for entry in entries] == rows[1:]
    assert sum(len(entry["content"].encode("utf-8")) for entry in entries) == 7
    assert member_db.db.total_changes == changes
    assert [
        tuple(row)
        for row in member_db.db.execute("SELECT day,content FROM memory_history ORDER BY day")
    ] == rows


def consolidate(store, **overrides):
    arguments = dict(
        source_id="span-1",
        session_key="chat:alice",
        source_total=len(overrides.get("messages", [])),
        snapshot={},
        messages=[],
        result={
            "semantic": [{"key": "project.codename", "value": "aurora", "confidence": 0.9}],
            "episodic": [{"text": "The team approved the aurora release."}],
            "lessons": [{"rule": "Check release notes before shipping"}],
            "history_entry": "Approved the aurora release.",
        },
    )
    arguments.update(overrides)
    return store.apply_consolidation(**arguments)


def test_consolidation_receipt_deduplicates_lost_ack_across_reopen(member_db):
    first = consolidate(member_db)
    assert first == {"source_id": "span-1", "semantic": 1, "episodic": 1, "lessons": 1}
    baseline = list(iter_sql_dump(member_db.db))
    assert consolidate(member_db, result={"history_entry": "different retry extraction"}) == first
    assert list(iter_sql_dump(member_db.db)) == baseline
    assert member_db.search_memory("aurora")
    path = member_db._db_path
    member_db.close()
    reopened = open_member_database(path, member_id="alice-id", store_id="alice-store")
    try:
        assert consolidate(reopened) == first
        assert list(iter_sql_dump(reopened.db)) == baseline
    finally:
        reopened.close()


def test_history_does_not_duplicate_full_bodies_in_a_revision_table(member_db):
    member_db.append_history("A retained authoritative history entry.")
    before = member_db.read_editable_history()
    assert member_db.replace_today_history(
        "The owner edited today's history.",
        expected_baseline=before,
        validate_current=lambda _: None,
    )
    assert member_db.read_editable_history() == "The owner edited today's history."
    tables = {row[0] for row in member_db.db.execute("SELECT name FROM sqlite_schema")}
    assert "memory_history_revisions" not in tables
    assert "memory_revisions" in tables


def test_consolidation_retains_only_compact_source_evidence(member_db):
    messages = [{"role": "user", "content": "UNRETAINED_TRANSCRIPT_秘密" * 1000}]
    consolidate(member_db, messages=messages)
    row = dict(member_db.db.execute("SELECT * FROM memory_consolidations").fetchone())
    assert "source_snapshot" not in row
    assert "session_key" not in row
    assert row["source_count"] == 1
    assert row["source_total"] == 1
    assert len(row["source_digest"]) == 64
    assert "UNRETAINED_TRANSCRIPT" not in json.dumps(row)


@pytest.mark.parametrize("change", ["content", "count", "total"])
def test_committed_receipt_refuses_changed_source_without_rewriting(member_db, change):
    messages = [{"content": "Original source 中文", "role": "user"}]
    first = consolidate(member_db, messages=messages, source_total=3)
    before = list(iter_sql_dump(member_db.db))
    # Dictionary insertion order is not a source edit.
    assert (
        consolidate(
            member_db,
            messages=[{"role": "user", "content": "Original source 中文"}],
            source_total=3,
        )
        == first
    )
    if change == "content":
        messages[0]["content"] = "Edited source"
    elif change == "count":
        messages.append({"role": "user", "content": "Additional source"})
    with pytest.raises(ValueError, match="source identity changed"):
        consolidate(member_db, messages=messages, source_total=4 if change == "total" else 3)
    assert list(iter_sql_dump(member_db.db)) == before


def test_consolidation_failure_rolls_back_history_facts_index_and_receipt(member_db, monkeypatch):
    before = list(iter_sql_dump(member_db.db))
    original = member_db._write_history

    def fail_after_history(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated disk failure before receipt")

    monkeypatch.setattr(member_db, "_write_history", fail_after_history)
    with pytest.raises(RuntimeError, match="disk failure"):
        consolidate(member_db)
    assert list(iter_sql_dump(member_db.db)) == before
    assert not member_db.search_memory("aurora")


def test_recall_has_no_database_writes_and_fts_tracks_revision_mutations(member_db, caplog):
    member_db.set_semantic("project.codename", "aurora", 1, "user_explicit")
    member_db.write_episodic("Aurora release notes are ready for review.", defer_embedding=True)
    before = list(iter_sql_dump(member_db.db))
    changes = member_db.db.total_changes
    with caplog.at_level("DEBUG", logger="kiro_crew.vector_memory"):
        member_db.recall("aurora release")
    assert "aurora release" not in caplog.text
    member_db.search_memory("aurora")
    assert member_db.db.total_changes == changes
    assert list(iter_sql_dump(member_db.db)) == before
    member_db.delete_semantic("project.codename", "user_explicit")
    assert all(row["path"] != "key:project.codename" for row in member_db.search_memory("aurora"))


@pytest.mark.parametrize("query_embedding", [None, [1.0, 0.0, 0.0]])
def test_legacy_recall_diagnostics_do_not_record_query(tmp_path, caplog, query_embedding):
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=3)
    store.init()
    try:
        store.write_episodic("Release notes are ready", defer_embedding=True)
        with caplog.at_level("DEBUG", logger="kiro_crew.vector_memory"):
            store.search_episodic(query_embedding, query_text="private aurora release")
        assert "Episodic" in caplog.text
        assert "private aurora release" not in caplog.text
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["appended", "edited", "shortened", "key-order"])
@pytest.mark.parametrize("restart", [False, True])
async def test_lost_transcript_ack_recovers_committed_prefix_without_another_model_call(
    member_db, monkeypatch, change, restart
):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.context import ContextBuilder
    from kiro_crew.history_consolidation import HistoryConsolidator

    log = MagicMock()
    first = {"role": "user", "content": "The release codename is aurora 中文."}
    second = {"role": "assistant", "content": "Recorded the original decision."}
    log.snapshot_for_consolidation.return_value = ([first, second], 2, 0)
    log.consolidation_retry_state.return_value = (0, 0.0)
    log.get_metadata.return_value = {}
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    writer = HistoryConsolidator(log, memory, vector_store=member_db, migrated=True)
    writer._call_llm = AsyncMock(return_value={"history_entry": "Approved aurora."})
    writer._note_failed_attempt = AsyncMock()
    monkeypatch.setattr("kiro_crew.context.store_of_session", lambda *_: "alice-store")
    monkeypatch.setattr(memory_stores, "memory_store_version", lambda *_: 2)
    monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=member_db))
    monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_: memory)

    def reject_legacy_lesson_store(**_):
        raise AssertionError("V2 consolidation must not open JSONL learning")

    monkeypatch.setattr(ContextBuilder, "get_lessons_for", reject_legacy_lesson_store)
    log.mark_consolidated.side_effect = OSError("lost acknowledgement")
    with pytest.raises(OSError, match="lost acknowledgement"):
        await writer._consolidate("chat:alice")
    before = list(iter_sql_dump(member_db.db))
    assert "Approved aurora" in member_db.read_editable_history()
    messages = [first, second, {"role": "user", "content": "Another decision arrived later."}]
    if change == "edited":
        messages[0] = dict(first, content="The old prefix was edited.")
    elif change == "shortened":
        messages = [first]
    elif change == "key-order":
        messages[0] = dict(reversed(list(first.items())))
    log.snapshot_for_consolidation.return_value = (messages, len(messages), 0)
    log.mark_consolidated.side_effect = None
    log.mark_consolidated.reset_mock()
    model = writer._call_llm
    reopened = None
    if restart:
        path = member_db._db_path
        member_db.close()
        reopened = open_member_database(path, member_id="alice-id", store_id="alice-store")
        monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=reopened))
        writer = HistoryConsolidator(log, memory, vector_store=reopened, migrated=True)
        writer._call_llm = model
    try:
        if change in {"edited", "shortened"}:
            with pytest.raises(ValueError, match="source changed"):
                await writer._consolidate("chat:alice")
            log.mark_consolidated.assert_not_called()
        else:
            await writer._consolidate("chat:alice")
            log.mark_consolidated.assert_called_once_with("chat:alice", 2, 0)
        assert model.await_count == 1
        assert list(iter_sql_dump((reopened or member_db).db)) == before
    finally:
        if reopened is not None:
            reopened.close()


def test_member_database_cannot_open_a_separate_index_or_migrate_global_files(member_db):
    facade = MemoryStore(
        workspace=member_db._db_path.parent, memory_version=2, vector_store=member_db
    )
    before = list(iter_sql_dump(member_db.db))
    with pytest.raises(ValueError, match="full-text search"):
        facade._get_db()
    with pytest.raises(ValueError, match="legacy learned files"):
        member_db.migrate_from_markdown()
    assert list(iter_sql_dump(member_db.db)) == before
    assert not (member_db._db_path.parent / "memory_index.db").exists()


@pytest.mark.parametrize("kind", ["fact", "directive", "episode"])
@pytest.mark.parametrize("change", ["content", "space"])
def test_member_backfill_rejects_late_content_or_model_space(member_db, monkeypatch, kind, change):
    from kiro_crew import memory_edit

    if kind == "fact":
        member_db.set_semantic("project.name", "aurora", 1, "user_explicit")
        identity = "key:project.name"
    elif kind == "directive":
        member_db.write_lesson("Review aurora before shipping", category="knowledge")
        identity = "key:" + member_db.get_lessons()[0]["key"]
    else:
        assert member_db.write_episodic("The aurora release was approved.", defer_embedding=True)
        identity = member_db.get_episodic_list(limit=1)[0]["id"]
    member_db._embedding_dim = 3
    member_db.reconcile_embedding_space("model-a", clear_when_unknown=True)

    def embed(text):
        if change == "space":
            # A second connection changes durable model space while inference runs.
            other = open_member_database(
                member_db._db_path, member_id="alice-id", store_id="alice-store", embedding_dim=3
            )
            try:
                other.reconcile_embedding_space("model-b", clear_when_unknown=True)
            finally:
                other.close()
        else:
            preview = memory_edit.preview_edit(
                member_db,
                "alice-store",
                b"synthetic-secret",
                {
                    "selection": {"query": {"q": "aurora", "kind": kind}},
                    "operation": {
                        "type": "replace_text",
                        "find": "aurora",
                        "replacement": "borealis",
                    },
                },
            )
            memory_edit.apply_edit(
                member_db, "alice-store", b"synthetic-secret", preview["preview_id"]
            )
        return [1.0, 0.0, 0.0]

    member_db.embed_fn = embed
    member_db.backfill_missing_embeddings(pace=False)
    row = member_db.db.execute("SELECT * FROM memory_items WHERE id=?", (identity,)).fetchone()
    assert row["embedding"] is None
    if change == "content":
        assert "borealis" in str(dict(row))
    else:
        assert member_db.recorded_embedding_space() == "model-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_consolidation_never_reads_transcript_or_opens_memory(
    member_db, monkeypatch, mode
):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED, HistoryConsolidator

    execution = ExecutionContext(
        "alice-id", MemoryStoreRef("alice-store", "alice-id"), "member", "kirocrew", mode
    )
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", lambda _: execution)
    log = MagicMock()
    writer = HistoryConsolidator(log, MagicMock(), vector_store=member_db)
    writer._call_llm = AsyncMock()
    before = list(iter_sql_dump(member_db.db))
    assert await writer._consolidate("chat:alice") is _CONSOLIDATION_REFUSED
    log.get_metadata.assert_not_called()
    log.snapshot_for_consolidation.assert_not_called()
    writer._call_llm.assert_not_awaited()
    assert list(iter_sql_dump(member_db.db)) == before
