"""Session search reads a chat's size-rotated archive head, and forgets what retention deletes.

Size rotation moves a transcript's oldest rows into ``archive/`` as ``rotate``
segments. The search corpus includes them (``history_archive_text``), through the
scan path and the index alike, and retention removing a segment makes its text
stop matching on the next query.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from kiro_crew import history_files, history_search, history_vectors_index
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import _INDEX_VERSION, INDEX_FILENAME
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: Far older than any retention window, without reading the clock.
_LONG_AGO = 1_000_000


@pytest.fixture(autouse=True)
def _small_rotation(_floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
    _floor_monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
    _floor_monkeypatch.setattr(history_search, "_INDEX_QUIET_WINDOW_SECS", 0.0)


def _rotated_log(base):
    log = ConversationLog(base_dir=base)
    log.append("alpha", "user", "tamarind is the word only the first message holds")
    for i in range(20):
        log.append("alpha", "user", f"filler message number {i:02d} with enough text to rotate")
    log.append("alpha", "user", "jackfruit sits in the newest message")
    log.append("beta", "user", "an unrelated chat about kumquat")
    assert list((base / "archive").glob("alpha__*.jsonl")), "fixture must rotate"
    assert all("tamarind" not in m.get("content", "") for m in log.read_messages("alpha"))
    return log


def _index_all(log):
    report = log._catalog_projection.backfill_index(budget_secs=FALLBACK_TIMEOUT_SECS)
    assert report["remaining"] == 0


def _keys(log, query):
    return [row["key"] for row in log.search_sessions(query, 10)]


def test_a_word_only_the_archive_holds_ranks_its_chat_on_the_scan_path(tmp_path):
    # Nothing is indexed yet, so every chat is scanned from its files.
    log = _rotated_log(tmp_path)

    assert _keys(log, "tamarind") == ["alpha"]
    assert "tamarind" in log.search_sessions("tamarind", 10)[0].get("snippet", "")
    assert _keys(log, "jackfruit") == ["alpha"]


def test_a_discarded_archive_is_not_searched(tmp_path):
    # A rewrite's `compact` segment holds text the product dropped on purpose.
    log = ConversationLog(base_dir=tmp_path)
    log.append("t1", "user", "persimmon before the rewrite")
    log.rewrite_session("t1", [{"role": "user", "content": "after", "ts": "x"}])

    assert _keys(log, "persimmon") == []


@pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")
def test_the_index_holds_the_archive_text_and_rows_from_before_are_rebuilt(tmp_path):
    log = _rotated_log(tmp_path)
    _index_all(log)
    assert _keys(log, "tamarind") == ["alpha"]

    # A store written by the previous version holds live-file text only, and the
    # live file's stat still vouches for it: reopening must drop it, not read it.
    db = tmp_path / ".index" / INDEX_FILENAME
    log._catalog_projection.search_index.close()
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version={_INDEX_VERSION - 1}")
    conn.close()

    reopened = ConversationLog(base_dir=tmp_path)
    assert reopened._catalog_projection.search_index.indexed_keys() == set()
    _index_all(reopened)
    assert _keys(reopened, "tamarind") == ["alpha"]


@pytest.mark.parametrize("indexed", [False, True])
def test_a_segment_retention_deletes_stops_matching_on_the_next_query(
    tmp_path, monkeypatch, indexed
):
    if indexed and not fts5_available():
        pytest.skip("SQLite built without FTS5")
    log = _rotated_log(tmp_path)
    if indexed:
        _index_all(log)
    # Warm every memo first, so a stale one would answer the second query.
    assert _keys(log, "tamarind") == ["alpha"]

    for seg in (tmp_path / "archive").glob("alpha__*.jsonl"):
        os.utime(seg, (_LONG_AGO, _LONG_AGO))
    # The rotation's own archive write already ran the hourly sweep.
    monkeypatch.setattr(history_files, "_last_cleanup", 0.0)
    assert history_files._cleanup_old_archives(retention_days=7, base=tmp_path) > 0

    assert _keys(log, "tamarind") == []
    assert _keys(log, "jackfruit") == ["alpha"]
    assert _keys(log, "kumquat") == ["beta"]


def test_retention_drops_the_semantic_chunks_of_the_chats_it_expired(tmp_path, monkeypatch):
    dropped: list[list[str]] = []

    class _Store:
        def drop(self, keys):
            dropped.append(list(keys))
            return True

        def close(self):
            pass

    monkeypatch.setattr(history_vectors_index, "vector_store_for", lambda _dir: _Store())
    _rotated_log(tmp_path)
    for seg in (tmp_path / "archive").glob("alpha__*.jsonl"):
        os.utime(seg, (_LONG_AGO, _LONG_AGO))
    monkeypatch.setattr(history_files, "_last_cleanup", 0.0)
    assert history_files._cleanup_old_archives(retention_days=7, base=tmp_path) > 0

    assert dropped == [["alpha"]]


def test_an_archive_record_past_the_cap_is_skipped_and_the_rest_still_ranks(tmp_path, monkeypatch):
    from kiro_crew import history_archive_text

    _rotated_log(tmp_path)
    [seg, *_] = sorted((tmp_path / "archive").glob("alpha__*.jsonl"))
    lines = seg.read_text(encoding="utf-8").splitlines(keepends=True)
    huge = '{"role": "user", "content": "durian ' + "x" * 4096 + '"}\n'
    seg.write_text(lines[0] + huge + "".join(lines[1:]), encoding="utf-8")
    monkeypatch.setattr(history_archive_text, "_SEGMENT_RECORD_CAP", 1024)

    texts = list(history_archive_text.iter_rotated_texts(tmp_path, "alpha"))

    assert not any("durian" in t for t in texts)
    assert any("tamarind" in t for t in texts)


def test_semantic_chunks_stored_before_the_archive_was_read_are_rebuilt(tmp_path):
    # A store filled by an earlier build holds the live file's chunks only, under
    # the bare tokenizer signature, and the live file's stat still matches them.
    import sqlite3

    from kiro_crew import history_vectors_index as chunking

    log = _rotated_log(tmp_path)
    tokenizer = chunking.Tokenizer(lambda text: len(text.split()), "words-1")
    store = chunking.vector_store_for(tmp_path)
    try:
        st = log._path("alpha").stat()
        live = [m["content"] for m in log.read_messages("alpha")]
        store.sync_session(
            "alpha", st, tokenizer.signature, chunking.chunk_messages(live, tokenizer.count_tokens)
        )
        chunking.backfill_chunks(log, store, tokenizer)
    finally:
        store.close()

    db = sqlite3.connect(tmp_path / ".index" / "session_vectors.db")
    try:
        texts = [
            row[0]
            for row in db.execute(
                "SELECT p.text FROM chunk_ref r JOIN pending_text p ON p.chunk_id = r.chunk_id"
                " WHERE r.session_key = 'alpha'"
            )
        ]
        [stored] = db.execute("SELECT chunker FROM session_state WHERE session_key='alpha'")
    finally:
        db.close()
    assert any("tamarind" in t for t in texts)
    assert stored[0] == chunking.chunker_value(tokenizer)


def test_a_chat_whose_stem_extends_another_keeps_its_archive_to_itself(tmp_path):
    # `_safe_key` keeps underscores, so `notes__private`'s segments also start
    # with `notes__`. Neither search nor paging may hand them to `notes`.
    log = ConversationLog(base_dir=tmp_path)
    log.append("notes", "user", "an ordinary note about lunch")
    log.append("notes__private", "user", "cardamom is the private word")
    for i in range(20):
        log.append(
            "notes__private", "user", f"filler message number {i:02d} with enough text to rotate"
        )
    assert list((tmp_path / "archive").glob("notes__private__*.jsonl")), "fixture must rotate"

    assert _keys(log, "cardamom") == ["notes__private"]
    assert log.read_rotated_messages("notes") == []
    assert [m["content"] for m in log.read_messages_chained_full("notes")] == [
        "an ordinary note about lunch"
    ]


def _expire_archive(base, stem):
    for seg in (base / "archive").glob(f"{stem}__*.jsonl"):
        os.utime(seg, (_LONG_AGO, _LONG_AGO))


@pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")
def test_an_index_pass_that_read_an_expired_segment_cannot_restore_it_after_the_purge(
    tmp_path, monkeypatch
):
    # The index pass reads under the chat's lock, then pauses before its sync;
    # retention runs meanwhile. The purge must wait for that sync and then drop
    # the row, or the row keeps the deleted text and the live stat vouches for it.
    import threading

    from kiro_crew.history import ConversationLog as Log

    log = _rotated_log(tmp_path)
    _index_all(log)
    _expire_archive(tmp_path, "alpha")
    read_done, go, progress = threading.Event(), threading.Event(), threading.Event()
    real_iter = history_search.iter_rotated_texts

    def paused_iter(base_dir, key):
        texts = list(real_iter(base_dir, key))
        if key == "alpha" and threading.current_thread() is worker:
            read_done.set()
            assert go.wait(FALLBACK_TIMEOUT_SECS)
        return iter(texts)

    real_locked = Log._locked

    def spied_locked(self, key):
        if threading.current_thread() is sweeper:
            progress.set()
        return real_locked(self, key)

    def sweep():
        try:
            history_files._cleanup_old_archives(retention_days=7, base=tmp_path)
        finally:
            progress.set()

    monkeypatch.setattr(history_search, "iter_rotated_texts", paused_iter)
    monkeypatch.setattr(Log, "_locked", spied_locked)
    monkeypatch.setattr(history_files, "_last_cleanup", 0.0)
    worker = threading.Thread(target=log._catalog_projection.index_session, args=("alpha",))
    sweeper = threading.Thread(target=sweep)
    worker.start()
    assert read_done.wait(FALLBACK_TIMEOUT_SECS)
    sweeper.start()
    # The sweep either asked for the chat's lock or finished without it.
    assert progress.wait(FALLBACK_TIMEOUT_SECS)
    go.set()
    worker.join(FALLBACK_TIMEOUT_SECS)
    sweeper.join(FALLBACK_TIMEOUT_SECS)
    assert not worker.is_alive() and not sweeper.is_alive()
    monkeypatch.setattr(history_search, "iter_rotated_texts", real_iter)

    assert not list((tmp_path / "archive").glob("alpha__*.jsonl"))
    assert _keys(log, "tamarind") == []


def test_two_chats_locked_in_opposite_order_finish_while_one_rotates(tmp_path, monkeypatch):
    # A writer holds alpha and then wants beta while beta's archive write runs.
    # The sweep that write starts must not run under beta's lock, or it waits for
    # alpha while the writer waits for beta.
    import threading

    log = _rotated_log(tmp_path)
    _expire_archive(tmp_path, "alpha")
    monkeypatch.setattr(history_files, "_last_cleanup", 0.0)
    holds_alpha, holds_beta = threading.Event(), threading.Event()

    def writer():
        with log._locked("alpha"):
            holds_alpha.set()
            assert holds_beta.wait(FALLBACK_TIMEOUT_SECS)
            with log._locked("beta"):
                pass

    def rotator():
        assert holds_alpha.wait(FALLBACK_TIMEOUT_SECS)
        with log._locked("beta"):
            holds_beta.set()
            history_files._archive_lines(
                "beta", ['{"role": "user", "content": "x"}\n'], "rotate", base=tmp_path
            )

    threads = [
        threading.Thread(target=writer, daemon=True),
        threading.Thread(target=rotator, daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(FALLBACK_TIMEOUT_SECS)
    assert not any(t.is_alive() for t in threads)
    sweep = history_files._cleanup_thread
    assert sweep is not None
    sweep.join(FALLBACK_TIMEOUT_SECS)
    assert not sweep.is_alive()

    assert not list((tmp_path / "archive").glob("alpha__*.jsonl"))
    assert list((tmp_path / "archive").glob("beta__*.jsonl"))


def _rotate(log, key, word):
    log.append(key, "user", f"{word} is the word only the first message holds")
    for i in range(20):
        log.append(key, "user", f"filler message number {i:02d} with enough text to rotate")


@pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")
def test_retention_forgets_a_chat_whose_key_has_a_colon(tmp_path, monkeypatch):
    # A dashboard key's segments carry the sanitized stem `dashboard_chat-1-100`;
    # the purge must drop the rows the index holds for that chat.
    log = ConversationLog(base_dir=tmp_path)
    _rotate(log, "dashboard:chat-1-100", "tamarind")
    stem = "dashboard_chat-1-100"
    assert list((tmp_path / "archive").glob(f"{stem}__*.jsonl")), "fixture must rotate"
    _index_all(log)
    assert _keys(log, "tamarind") == [stem]

    _expire_archive(tmp_path, stem)
    monkeypatch.setattr(history_files, "_last_cleanup", 0.0)
    assert history_files._cleanup_old_archives(retention_days=7, base=tmp_path) > 0

    assert _keys(log, "tamarind") == []
    assert log._catalog_projection.search_index.indexed_keys() == set()


@pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")
@pytest.mark.parametrize("failing", ["index", "vectors"])
def test_a_failed_drop_keeps_the_segment_so_the_next_sweep_retries(tmp_path, monkeypatch, failing):
    from kiro_crew import history_index, history_vectors

    log = _rotated_log(tmp_path)
    _index_all(log)
    _expire_archive(tmp_path, "alpha")
    store = (
        history_index.SessionSearchIndex
        if failing == "index"
        else history_vectors.SessionVectorStore
    )
    real_drop = store.drop
    monkeypatch.setattr(store, "drop", lambda self, keys: False)
    monkeypatch.setattr(history_files, "_last_cleanup", 0.0)

    assert history_files._cleanup_old_archives(retention_days=7, base=tmp_path) == 0
    assert list((tmp_path / "archive").glob("alpha__*.jsonl"))

    monkeypatch.setattr(store, "drop", real_drop)
    monkeypatch.setattr(history_files, "_last_cleanup", 0.0)
    assert history_files._cleanup_old_archives(retention_days=7, base=tmp_path) > 0
    assert not list((tmp_path / "archive").glob("alpha__*.jsonl"))
    assert _keys(log, "tamarind") == []
