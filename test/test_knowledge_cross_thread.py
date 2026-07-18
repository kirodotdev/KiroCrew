"""Regression tests for KnowledgeStore sqlite access from worker threads.

KnowledgeStore is created on the event-loop thread, but HybridRetriever.search()
runs on mc-embed executor threads (dashboard/handlers/knowledge.py
search_for_context via run_in_embed_pool). A shared sqlite3 connection carries
creating-thread affinity (check_same_thread=True default), so every
worker-thread query would raise sqlite3.ProgrammingError -> HTTP 500 on
/api/knowledge/search-for-context. KnowledgeStore hands each thread its own
WAL connection via a thread-local ``db`` property; these tests pin that
contract.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "cross_thread.db"))
    yield s
    s.close()


def _add_item(store: KnowledgeStore, title: str, content: str) -> None:
    now = datetime.utcnow().isoformat()
    store.db.execute(
        "INSERT INTO items (id, title, content, item_type, created_at, updated_at)"
        " VALUES (?, ?, ?, 'note', ?, ?)",
        (title.lower().replace(" ", "-"), title, content, now, now),
    )
    row = store.db.execute("SELECT rowid FROM items WHERE title = ?", (title,)).fetchone()
    store.db.execute(
        "INSERT INTO items_fts (rowid, title, content, tags) VALUES (?, ?, ?, '[]')",
        (row["rowid"], title, content),
    )


def test_db_property_returns_per_thread_connections(store):
    """Each thread must observe a distinct sqlite connection object."""
    main_conn = store.db
    assert store.db is main_conn  # stable within a thread

    seen = {}

    def grab(idx):
        seen[idx] = store.db
        assert store.db is seen[idx]

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conns = list(seen.values()) + [main_conn]
    assert len({id(c) for c in conns}) == len(conns)


def test_worker_thread_query_does_not_raise(store):
    """A worker thread must be able to execute() on a store created elsewhere."""
    _add_item(store, "Deployment Runbook", "How to deploy the gateway safely.")

    def query():
        return store.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        # Before the fix this raised sqlite3.ProgrammingError
        # ("SQLite objects created in a thread can only be used in that same thread").
        assert pool.submit(query).result(timeout=10) == 1


def test_hybrid_retriever_search_from_executor_thread(store):
    """Mirror search_for_context: retriever.search dispatched to a worker pool."""
    _add_item(store, "Gateway Restart Guide", "Restart the kirocrew gateway with systemctl.")
    _add_item(store, "Unrelated Doc", "Nothing about the query terms here.")

    retriever = HybridRetriever(store)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="mc-embed-test") as pool:
        results = pool.submit(retriever.search, "gateway restart", 5).result(timeout=10)

    assert any(r["id"] == "gateway-restart-guide" for r in results)


def test_writes_from_two_threads_are_serialized(store):
    """WAL + busy_timeout must let concurrent writers succeed, not error."""

    def write(n):
        _add_item(store, f"Doc {n}", f"content {n}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in [pool.submit(write, i) for i in range(8)]:
            f.result(timeout=15)

    assert store.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 8


def test_close_only_releases_calling_threads_connection(store):
    """close() must not break other threads' live connections."""
    _add_item(store, "Persistent Doc", "still readable")

    worker_conn_ok = {}

    def use_then_signal(barrier):
        store.db.execute("SELECT 1").fetchone()
        barrier.wait(timeout=10)  # main thread closes its conn here
        worker_conn_ok["ok"] = (
            store.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1
        )

    barrier = threading.Barrier(2)
    t = threading.Thread(target=use_then_signal, args=(barrier,))
    t.start()
    barrier.wait(timeout=10)
    store.close()
    t.join(timeout=10)
    assert worker_conn_ok.get("ok") is True
    # And the main thread lazily reconnects after close().
    assert store.db.execute("SELECT 1").fetchone() is not None
