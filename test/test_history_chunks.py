"""Tests for ``history_chunks`` and ``history_vectors``: chunk bounds, and the store.

The tokenizer is a stand-in that counts words and CJK characters, so the bounds
are exact and no model is needed. The store tests cover dedup across sessions, the
freshness rule, and the deletion contract ``delete_session`` relies on.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import pytest

from kiro_crew import history_vectors
from kiro_crew.history_chunks import _MAX_UNIT_CHARS, chunk_messages
from kiro_crew.history_vectors import SessionVectorStore

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text: str) -> int:
    return len(_CJK.findall(text)) + len(_CJK.sub(" ", text).split())


def _store(tmp_path: Path) -> SessionVectorStore:
    return SessionVectorStore(tmp_path / ".index" / "session_vectors.db")


def _stat(tmp_path: Path, key: str) -> os.stat_result:
    path = tmp_path / f"{key}.jsonl"
    if not path.exists():
        path.write_text("{}\n")
    return path.stat()


def _sync(store: SessionVectorStore, tmp_path: Path, key: str, *texts: str) -> None:
    chunks = chunk_messages(list(texts), _tokens, max_tokens=5)
    store.sync_session(key, _stat(tmp_path, key), "chunker-1", chunks)


def _rows(store: SessionVectorStore, sql: str) -> list[tuple]:
    db = sqlite3.connect(store._db_path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


class TestChunker:
    def test_no_chunk_exceeds_the_bound_or_spans_a_message(self) -> None:
        texts = ["one two three four five six seven eight nine ten eleven", "a b c"]

        chunks = chunk_messages(texts, _tokens, max_tokens=4)

        assert all(_tokens(c.text) <= 4 for c in chunks)
        for index, text in enumerate(texts):
            mine = [c for c in chunks if c.message_index == index]
            assert "".join(c.text for c in mine) == text
            assert all(text[c.char_start : c.char_end] == c.text for c in mine)

    def test_a_cjk_run_is_cut_between_characters(self) -> None:
        chunks = chunk_messages(["我们要给订单表加一个非空列"], _tokens, max_tokens=5)

        assert [c.text for c in chunks] == ["我们要给订", "单表加一个", "非空列"]
        assert [c.token_count for c in chunks] == [5, 5, 3]

    def test_a_word_longer_than_a_unit_is_split(self) -> None:
        word = "x" * (_MAX_UNIT_CHARS * 2 + 3)

        chunks = chunk_messages([word], lambda text: len(text) // _MAX_UNIT_CHARS + 1)

        assert "".join(c.text for c in chunks) == word

    def test_whitespace_only_text_yields_nothing(self) -> None:
        assert chunk_messages(["   \n\t", ""], _tokens) == []

    def test_a_lone_surrogate_becomes_a_replacement_character(self, tmp_path: Path) -> None:
        (chunk,) = chunk_messages(["hello \ud83d world"], _tokens)

        assert chunk.text == "hello \ufffd world"
        store = _store(tmp_path)
        store.sync_session("a", _stat(tmp_path, "a"), "c", [chunk])
        assert _rows(store, "SELECT text FROM pending_text") == [("hello \ufffd world",)]

    def test_identical_text_hashes_identically(self) -> None:
        first, second = chunk_messages(["same words", "same words"], _tokens)

        assert first.text_hash == second.text_hash
        assert (first.message_index, second.message_index) == (0, 1)


class TestStore:
    def test_identical_text_is_stored_once(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "a", "shared words here")
        _sync(store, tmp_path, "b", "shared words here")

        assert _rows(store, "SELECT count(*) FROM chunk") == [(1,)]
        assert _rows(store, "SELECT count(*) FROM chunk_ref") == [(2,)]
        assert _rows(store, "SELECT count(*) FROM pending_text") == [(1,)]

    def test_a_drop_keeps_a_chunk_another_session_holds(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "a", "shared words here", "only in a zqxjvbnm")
        _sync(store, tmp_path, "b", "shared words here")

        assert store.drop(["a"]) is True

        assert _rows(store, "SELECT session_key FROM chunk_ref") == [("b",)]
        assert _rows(store, "SELECT count(*) FROM chunk") == [(1,)]
        assert _rows(store, "SELECT text FROM pending_text") == [("shared words here",)]
        assert store.indexed_keys() == {"b"}

    def test_a_dropped_chunk_leaves_no_bytes_in_the_file_or_the_wal(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "keep", "ordinary words")
        _sync(store, tmp_path, "doomed", "zqxjvbnm marker")

        assert store.drop(["doomed"]) is True

        files = [store._db_path, store._db_path.with_name(store._db_path.name + "-wal")]
        raw = b"".join(p.read_bytes() for p in files if p.exists())
        assert b"zqxjvbnm" not in raw
        assert b"ordinary" in raw

    def test_a_resync_replaces_the_session_rows(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "a", "first version text")
        _sync(store, tmp_path, "a", "second version")

        assert _rows(store, "SELECT text FROM pending_text") == [("second version",)]

    def test_drop_with_no_store_file_succeeds_and_creates_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        assert store.drop(["a"]) is True
        assert not store._db_path.exists()

    def test_drop_fails_when_the_store_exists_but_cannot_open(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store._db_path.mkdir(parents=True)

        assert store.drop(["a"]) is False

    def test_drop_fails_when_the_wal_cannot_be_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "a", "some words")
        monkeypatch.setattr(store, "_truncate_wal", lambda conn: False)

        assert store.drop(["a"]) is False

    def test_an_untruncated_drop_is_owed_until_settled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "a", "some words")
        monkeypatch.setattr(store, "_truncate_wal", lambda conn: False)
        assert store.drop(["a"]) is False
        assert store.erasure_owed is True
        assert store.settle() is False

        monkeypatch.setattr(store, "_truncate_wal", lambda conn: True)

        assert store.settle() is True
        assert store.erasure_owed is False

    def test_a_truncate_that_raises_after_the_rows_go_is_owed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "a", "some words")

        def broken(conn: object) -> bool:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(store, "_truncate_wal", broken)

        assert store.drop(["a"]) is False
        assert store.indexed_keys() == set()
        assert store.erasure_owed is True

    def test_a_busy_checkpoint_is_not_reported_as_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(history_vectors, "_WAL_TRUNCATE_RETRY_SECS", 0)
        calls: list[str] = []

        class _BusyConn:
            def execute(self, sql: str) -> "_BusyConn":
                calls.append(sql)
                return self

            def fetchone(self) -> tuple[int, int, int]:
                return (1, 5, 3)

        assert _store(tmp_path)._truncate_wal(_BusyConn()) is False
        assert len(calls) == history_vectors._WAL_TRUNCATE_ATTEMPTS

    def test_fresh_keys_follows_the_file_and_the_chunker(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "a", "words")
        stats = {"a": _stat(tmp_path, "a")}

        assert store.fresh_keys(stats, "chunker-1") == {"a"}
        assert store.fresh_keys(stats, "chunker-2") == set()
        (tmp_path / "a.jsonl").write_text('{"changed": true}\n')
        assert store.fresh_keys({"a": _stat(tmp_path, "a")}, "chunker-1") == set()

    def test_a_store_from_another_version_starts_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        _sync(store, tmp_path, "a", "words")
        store.close()
        monkeypatch.setattr(history_vectors, "_STORE_VERSION", 2)

        assert _store(tmp_path).indexed_keys() == set()
