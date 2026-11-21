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
