"""Cost tests for indexed transcript search.

They pin what a repeated or early-matching search must NOT do on sessions the
index vouches for: fetch the folded document again, decompress text it does not
need, or build a ``Path`` per listed file. Every assertion counts calls or
bytes, never wall-clock time, so the result does not depend on host load.
"""

from __future__ import annotations

import os
import pathlib
import zlib

import pytest

from kiro_crew import history_index, history_search
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import SessionSearchIndex

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")


def _indexed_log(tmp_path, sessions: dict[str, list[str]]) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    for key, messages in sessions.items():
        for i, text in enumerate(messages):
            log.append(key, "user" if i % 2 == 0 else "assistant", text)
    for key in sessions:
        assert log._catalog_projection.index_session(key)
    return log


def _corpus() -> dict[str, list[str]]:
    return {
        f"s{n}": [f"session {n} message {m} about deployment pipelines" for m in range(6)]
        for n in range(5)
    }


def _count_calls(monkeypatch, cls, name) -> list[object]:
    seen: list[object] = []
    original = getattr(cls, name)

    def counted(self, *args, **kwargs):
        seen.append(args[0] if args else None)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, counted)
    return seen


def test_repeat_query_reads_no_document(tmp_path, monkeypatch):
    log = _indexed_log(tmp_path, _corpus())
    reads = _count_calls(monkeypatch, SessionSearchIndex, "document")
    first = log.search_sessions("deployment pipelines", 50)
    assert len(first) == 5
    assert reads, "the first query folds every candidate from the index"
    reads.clear()
    assert log.search_sessions("deployment pipelines", 50) == first
    assert reads == [], "a repeated query is answered from the count memo"


def test_repeat_query_builds_no_snippet_text(tmp_path, monkeypatch):
    log = _indexed_log(tmp_path, _corpus())
    decompressed: list[int] = []
    real = zlib.decompress

    def counted(data, *args, **kwargs):
        out = real(data, *args, **kwargs)
        decompressed.append(len(out))
        return out

    monkeypatch.setattr(history_index.zlib, "decompress", counted)
    first = log.search_sessions("deployment", 50)
    assert all(row.get("snippet") for row in first)
    assert decompressed, "the first query builds its snippets from stored text"
    decompressed.clear()
    assert log.search_sessions("deployment", 50) == first
    assert decompressed == [], "a repeated query reuses its snippets"


def test_memo_off_keeps_reading_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(history_search, "_SEARCH_COUNT_MEMO_BYTES", 0)
    monkeypatch.setattr(history_search, "_SEARCH_SNIPPET_MEMO_BYTES", 0)
    log = _indexed_log(tmp_path, _corpus())
    reads = _count_calls(monkeypatch, SessionSearchIndex, "document")
    first = log.search_sessions("deployment", 50)
    n = len(reads)
    assert n
    assert log.search_sessions("deployment", 50) == first
    assert len(reads) == 2 * n
    assert log._catalog_projection._count_memo is None


def test_early_snippet_match_decompresses_only_its_chunk(tmp_path, monkeypatch):
    filler = "x" * 4000
    messages = ["needle sits in the first message"] + [f"{filler} {i}" for i in range(80)]
    log = _indexed_log(tmp_path, {"big": messages})
    total = sum(len(m) for m in messages)
    decompressed: list[int] = []
    real = zlib.decompress

    def counted(data, *args, **kwargs):
        out = real(data, *args, **kwargs)
        decompressed.append(len(out))
        return out

    monkeypatch.setattr(history_index.zlib, "decompress", counted)
    rows = log.search_sessions("needle", 5)
    assert rows and "needle" in rows[0]["snippet"]
    assert 0 < sum(decompressed) < total / 4


def test_chunked_text_round_trips(tmp_path):
    index = SessionSearchIndex(tmp_path / "session_index.db")
    path = tmp_path / "a.jsonl"
    path.write_text("x", encoding="utf-8")
    st = path.stat()
    texts = [f"message {i} " + "y" * (i * 997 % 20000) for i in range(60)]
    index.sync(
        "a", mtime_ns=st.st_mtime_ns, size=st.st_size, dev=st.st_dev, ino=st.st_ino, texts=texts
    )
    assert list(index.iter_raw_texts("a", st)) == texts
    chunks = index._ensure_open().execute("SELECT COUNT(*) FROM session_text").fetchone()[0]
    assert chunks > 1
    index.close()


def test_count_memo_stays_within_budget_and_admits_later_queries(tmp_path, monkeypatch):
    """A full memo evicts older queries' terms, so a query first seen after the
    budget filled is still memoized, and the budget is never exceeded."""
    monkeypatch.setattr(history_search, "_SEARCH_COUNT_MEMO_BYTES", 8192)
    log = _indexed_log(tmp_path, _corpus())
    memo = log._catalog_projection._count_memo
    words = ["deployment", "pipelines", "session", "message", "about"] * 2
    queries = [f"{a} {b}" for a, b in zip(words, words[3:] + words[:3])]
    for q in queries:
        log.search_sessions(q, 50)
        assert memo.stats()["bytes"] <= 8192
    assert memo.stats()["evicted"] > 0, "the walk is larger than the budget"
    reads = _count_calls(monkeypatch, SessionSearchIndex, "document")
    log.search_sessions(queries[-1], 50)
    assert reads == [], "the newest query's counts were admitted and kept"


def test_full_memo_drops_entries_for_changed_text(tmp_path, monkeypatch):
    monkeypatch.setattr(history_search, "_SEARCH_COUNT_MEMO_BYTES", 8192)
    log = _indexed_log(tmp_path, _corpus())
    projection = log._catalog_projection
    memo = projection._count_memo
    log.search_sessions("deployment", 50)
    old = {key[:32] for key in memo._data}
    assert old
    for n in range(5):
        log.append(f"s{n}", "user", "a new message")
        assert projection.index_session(f"s{n}")
    for q in ("session message", "about pipelines", "deployment about", "pipelines session"):
        log.search_sessions(q, 50)
    stats = memo.stats()
    assert stats["refused"] + stats["evicted"] > 0
    log.search_sessions("message about", 50)
    held = {key[:32] for key in memo._data}
    assert held, "entries for the current text are kept"
    assert not (held & old), "entries for superseded text are pruned once the memo is full"


def test_reindex_during_a_search_does_not_poison_a_twin(tmp_path, monkeypatch):
    """Counts read from a session's replacement text must not be filed under
    the digest the shortlist saw, which an identical session still carries."""
    log = ConversationLog(base_dir=tmp_path)
    # "a" is written last, so it is newest and scored before its twin.
    for key in ("b", "a"):
        log.update_metadata(key, {"title": "notes"})
        log.append(key, "user", "alpha text")
        later = 1_700_000_000 + (10 if key == "a" else 0)
        os.utime(log._path(key), (later, later))
    projection = log._catalog_projection
    for key in ("a", "b"):
        assert projection.index_session(key)
    real = projection._index_shortlist
    rewritten: list[str] = []

    def rewrite_after_shortlist(window, needles):
        out = real(window, needles)
        if not rewritten:
            path = log._path("a")
            path.write_text(path.read_text(encoding="utf-8").replace("alpha", "beta"))
            assert projection.index_session("a")
            rewritten.append("a")
        return out

    monkeypatch.setattr(projection, "_index_shortlist", rewrite_after_shortlist)
    log.search_sessions("alpha", 50)
    assert rewritten == ["a"]
    monkeypatch.setattr(projection, "_index_shortlist", real)
    assert [row["key"] for row in log.search_sessions("alpha", 50)] == ["b"]


def test_listing_builds_no_path_per_file(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    for n in range(20):
        log.append(f"s{n}", "user", "hello")
    log.list_sessions()
    joins: list[object] = []
    real = pathlib.PurePath.__truediv__

    def counted(self, other):
        joins.append(other)
        return real(self, other)

    monkeypatch.setattr(pathlib.PurePath, "__truediv__", counted)
    assert len(log.list_sessions()) == 20
    assert joins == []
