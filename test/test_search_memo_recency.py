"""Transcript-search memo admission, sharing and cold-burst coordination.

Every assertion counts calls, documents read or memo bookkeeping, and waits only
on events the code under test sets, so the result does not depend on host load.
A wait carries a long ceiling that exists only to fail a hang.
"""

from __future__ import annotations

import threading
import types

import pytest

from kiro_crew import history_search
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.history import ConversationLog
from kiro_crew.history_cache import _GroupRecencyMemo
from kiro_crew.history_index import SessionSearchIndex
from kiro_crew.history_search import SessionCatalogProjection

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")

_HANG = 60.0

_WORDS = ["deployment", "pipelines", "session", "message", "about", "gateway", "memory"]


def _corpus(n: int = 6) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in range(n):
        out[f"s{s}"] = [
            f"session {s} turn {m} {_WORDS[(s + m) % len(_WORDS)]} {_WORDS[(s * m) % len(_WORDS)]}"
            for m in range(6)
        ]
    return out


def _indexed_log(tmp_path, sessions: dict[str, list[str]]) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    for key, messages in sessions.items():
        for i, text in enumerate(messages):
            log.append(key, "user" if i % 2 == 0 else "assistant", text)
    for key in sessions:
        assert log._catalog_projection.index_session(key)
    return log


def _count_documents(monkeypatch) -> list[object]:
    seen: list[object] = []
    original = SessionSearchIndex.document

    def counted(self, *args, **kwargs):
        seen.append(args[0] if args else None)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SessionSearchIndex, "document", counted)
    return seen


def _queries() -> list[str]:
    return [f"{a} {b}" for a in _WORDS for b in _WORDS if a != b]


# --------------------------------------------------------------- memo policy


def _memo(max_bytes: int) -> _GroupRecencyMemo:
    return _GroupRecencyMemo(max_bytes, lambda v: v[1], lambda k: k.split(":")[1])


def test_full_memo_evicts_least_recent_unpinned_group():
    memo = _memo(30)
    for s in range(3):
        memo[f"d{s}:old"] = (s, 5)
    for s in range(3):
        memo[f"d{s}:hot"] = (s, 5)
    memo.get("d0:old")  # touching a group makes it recent again
    memo["d0:new"] = (0, 5)
    assert "d0:hot" not in memo and "d0:old" in memo and "d0:new" in memo
    assert memo.stats()["bytes"] <= 30


def test_pinned_group_is_never_evicted_and_the_entry_is_refused():
    memo = _memo(10)
    memo["d0:a"] = (0, 5)
    memo["d1:a"] = (1, 5)
    with memo.pinned(["a", "b"]):
        memo["d0:b"] = (0, 5)
    assert "d0:a" in memo and "d1:a" in memo and "d0:b" not in memo
    assert memo.stats()["refused"] == 1
    memo["d0:b"] = (0, 5)  # unpinned now, so "a" gives way
    assert "d0:b" in memo and "d0:a" not in memo and "d1:a" not in memo


def test_eviction_is_pressure_for_the_prune():
    memo = _memo(10)
    memo["d0:a"] = (0, 10)
    assert memo.refused_since_prune() == 0
    memo["d0:b"] = (0, 10)
    assert memo.refused_since_prune() == 1
    assert memo.retain({"d0:b"}) == 0
    assert memo.refused_since_prune() == 0


def test_query_first_seen_after_the_cap_is_memoized(tmp_path, monkeypatch):
    monkeypatch.setattr(history_search, "_SEARCH_COUNT_MEMO_BYTES", 6000)
    log = _indexed_log(tmp_path, _corpus())
    memo = log._catalog_projection._count_memo
    for q in _queries()[:20]:
        log.search_sessions(q, 50)
    assert memo.stats()["evicted"] > 0, "the earlier queries filled the memo"
    late = "gateway memory"
    first = log.search_sessions(late, 50)
    reads = _count_documents(monkeypatch)
    assert log.search_sessions(late, 50) == first
    assert reads == [], "a query first seen after the cap was admitted"


def test_query_larger_than_the_budget_keeps_a_stable_partial_hit_rate(tmp_path, monkeypatch):
    """Pinning stops a query evicting its own entries, so repeating a query that
    does not fit still hits on the part that did."""
    monkeypatch.setattr(history_search, "_SEARCH_COUNT_MEMO_BYTES", 1200)
    log = _indexed_log(tmp_path, _corpus())
    reads = _count_documents(monkeypatch)
    query = "session turn"
    log.search_sessions(query, 50)
    cold = len(reads)
    reads.clear()
    log.search_sessions(query, 50)
    warm = len(reads)
    reads.clear()
    log.search_sessions(query, 50)
    assert 0 < warm < cold
    assert len(reads) == warm


# ----------------------------------------------------------- equivalence


def test_results_match_memos_off_through_evictions(tmp_path, monkeypatch):
    """Byte-identical rows, snippets included, with a memo small enough to evict
    on almost every query, against the same queries with both memos off."""
    _indexed_log(tmp_path, _corpus(8))
    monkeypatch.setattr(history_search, "_time", types.SimpleNamespace(time=lambda: 1.9e9))
    monkeypatch.setattr(history_search, "_SEARCH_COUNT_MEMO_BYTES", 3000)
    monkeypatch.setattr(history_search, "_SEARCH_SNIPPET_MEMO_BYTES", 1500)
    with_memo = ConversationLog(base_dir=tmp_path)
    monkeypatch.setattr(history_search, "_SEARCH_COUNT_MEMO_BYTES", 0)
    monkeypatch.setattr(history_search, "_SEARCH_SNIPPET_MEMO_BYTES", 0)
    without = ConversationLog(base_dir=tmp_path)
    assert without._catalog_projection._count_memo is None
    queries = _queries()
    for q in queries + queries[::3] + ["turn", "session 3", "about about"]:
        assert with_memo.search_sessions(q, 50) == without.search_sessions(q, 50), q
    assert with_memo._catalog_projection._count_memo.stats()["evicted"] > 0


def test_logs_over_one_directory_share_the_memos(tmp_path, monkeypatch):
    log = _indexed_log(tmp_path, _corpus())
    first = log.search_sessions("deployment pipelines", 50)
    other = ConversationLog(base_dir=tmp_path)
    assert other._catalog_projection._count_memo is log._catalog_projection._count_memo
    reads = _count_documents(monkeypatch)
    assert other.search_sessions("deployment pipelines", 50) == first
    assert reads == []


# -------------------------------------------------------------- cold burst


class _CountingEvent(threading.Event):
    waiters = 0
    joined = threading.Condition()

    def wait(self, timeout=None):
        with _CountingEvent.joined:
            _CountingEvent.waiters += 1
            _CountingEvent.joined.notify_all()
        return super().wait(timeout)


class _CountedFlight(history_search._Flight):
    def __init__(self, epoch: int) -> None:
        super().__init__(epoch)
        self.done = _CountingEvent()


def _hold_leader(monkeypatch, release) -> tuple[list[str], threading.Event]:
    """Make the first scoring pass wait until ``release()`` holds.

    Returns the queries of every pass that ran, and an event set once the first
    pass is being held.
    """
    monkeypatch.setattr(_CountingEvent, "waiters", 0)
    monkeypatch.setattr(history_search, "_Flight", _CountedFlight)
    passes: list[str] = []
    holding = threading.Event()
    real = SessionCatalogProjection._search_parsed

    def held(self, query, *args, **kwargs):
        passes.append(query)
        if len(passes) == 1:
            holding.set()
            with _CountingEvent.joined:
                assert _CountingEvent.joined.wait_for(release, _HANG)
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(SessionCatalogProjection, "_search_parsed", held)
    return passes, holding


def _run_all(fns) -> list:
    results: list = [None] * len(fns)
    errors: list[BaseException] = []

    def run(i, fn):
        try:
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_HANG)
        assert not t.is_alive()
    assert not errors, errors
    return results


def test_concurrent_cold_queries_share_one_pass(tmp_path, monkeypatch):
    log = _indexed_log(tmp_path, _corpus())
    passes, _ = _hold_leader(monkeypatch, lambda: _CountingEvent.waiters >= 5)
    results = _run_all([lambda: log.search_sessions("deployment pipelines", 50)] * 6)
    assert passes == ["deployment pipelines"], "six callers, one scoring pass"
    assert results[0] and all(r == results[0] for r in results)
    assert len({id(r) for r in results}) == 6, "each caller gets its own rows"
    assert history_search._flights == {}


def test_search_after_a_write_does_not_join_an_earlier_pass(tmp_path, monkeypatch):
    log = _indexed_log(tmp_path, _corpus())
    after_write = threading.Event()
    passes, holding = _hold_leader(monkeypatch, after_write.is_set)
    query = "zebra"

    def writer_then_search():
        assert holding.wait(_HANG)
        log.append("s0", "user", "a zebra appears")
        try:
            return log.search_sessions(query, 50)
        finally:
            with _CountingEvent.joined:
                after_write.set()
                _CountingEvent.joined.notify_all()

    before, after = _run_all([lambda: log.search_sessions(query, 50), writer_then_search])
    assert passes == [query, query], "the later search ran its own pass"
    assert _CountingEvent.waiters == 0, "and never waited on the earlier one"
    assert [r["key"] for r in after] == ["s0"], "it sees the write"
    assert before is not after


def test_restricted_search_never_joins(tmp_path, monkeypatch):
    log = _indexed_log(tmp_path, _corpus())
    passes: list[str] = []
    real = SessionCatalogProjection._search_parsed

    def counted(self, query, *args, **kwargs):
        passes.append(query)
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(SessionCatalogProjection, "_search_parsed", counted)
    log.search_sessions("session", 50, keys={"s1"})
    log.search_sessions("session", 50, keys={"s1"})
    assert passes == ["session", "session"]
    assert history_search._flights == {}


def test_warm_search_reads_the_window_metadata(tmp_path, monkeypatch):
    _indexed_log(tmp_path, _corpus())
    log = ConversationLog(base_dir=tmp_path)
    log.warm_search()
    heads: list[object] = []
    real = SessionCatalogProjection._scan_head_title

    def counted(path):
        heads.append(path)
        return real(path)

    monkeypatch.setattr(SessionCatalogProjection, "_scan_head_title", staticmethod(counted))
    assert log.search_sessions("deployment", 50)
    assert heads == [], "the first search found every session's metadata cached"


def test_warm_search_on_a_missing_directory_is_a_no_op(tmp_path):
    ConversationLog(base_dir=tmp_path / "absent").warm_search()
