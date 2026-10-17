"""Time the knowledge vector leg right after a write, against the uncached path.

Builds a synthetic library (default 22,000 items, 1024-dim float32 vectors) in
a temporary directory, then times ``HybridRetriever._vector_search`` in four
states, interleaved round by round so host load drifts hit every state alike:

* ``uncached``     -- the matrix cache disabled: every search reads and scores
                      all rows (the path a search took before the cache existed)
* ``warm``         -- a repeat search with nothing written in between
* ``after_job``    -- the first search after a write to ``ingestion_jobs`` only
* ``after_item``   -- the first search after one new item is added

It also checks that the cached ranking equals the uncached ranking, element by
element, on every round. Run from a worktree with its venv::

    .venv/bin/python test/perf/knowledge_vector_after_write.py --rounds 15
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import struct
import tempfile
import time
import uuid
from pathlib import Path

from kiro_crew.knowledge import retrieval
from kiro_crew.knowledge.retrieval import ANY_EMBEDDING_SPACE, HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

# Row timestamps play no part in what is timed.
_STAMP = "2026-01-01T00:00:00"


def _vec(rng: random.Random, dim: int) -> list[float]:
    return [rng.gauss(0.0, 1.0) for _ in range(dim)]


def _populate(store: KnowledgeStore, n: int, dim: int, rng: random.Random) -> None:
    now = _STAMP
    pack = struct.Struct(f"<{dim}f").pack
    db = store.db
    db.execute("BEGIN IMMEDIATE")
    for i in range(n):
        db.execute(
            "INSERT INTO items (id, title, content, item_type, embedding, created_at, updated_at) "
            "VALUES (?, ?, ?, 'doc', ?, ?, ?)",
            (str(uuid.uuid4()), f"doc {i}", "x" * 600, pack(*_vec(rng, dim)), now, now),
        )
    db.execute("COMMIT")


def _write_job(store: KnowledgeStore) -> None:
    now = _STAMP
    store.db.execute(
        "INSERT INTO ingestion_jobs (id, status, created_at, updated_at) VALUES (?, 'pending', ?, ?)",
        (str(uuid.uuid4()), now, now),
    )


def _timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000.0, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=22_000)
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    rng = random.Random(1)
    with tempfile.TemporaryDirectory() as tmp:
        store = KnowledgeStore(str(Path(tmp) / "kb.db"))
        _populate(store, args.items, args.dim, rng)
        pack = struct.Struct(f"<{args.dim}f").pack
        times: dict[str, list[float]] = {
            k: [] for k in ("uncached", "warm", "after_job", "after_item")
        }
        mismatches = 0
        real_cached = retrieval._cached_matrix
        for _ in range(args.rounds):
            q = _vec(rng, args.dim)
            r = HybridRetriever(store, embedder=lambda _q, q=q: q, embed_sig=ANY_EMBEDDING_SPACE)
            run = lambda: r._vector_search("q", limit=args.limit)  # noqa: E731

            retrieval._cached_matrix = lambda *a, **k: None
            try:
                t, ref = _timed(run)
            finally:
                retrieval._cached_matrix = real_cached
            times["uncached"].append(t)

            run()  # make sure the cache holds the current generation
            t, warm = _timed(run)
            times["warm"].append(t)

            _write_job(store)
            t, after_job = _timed(run)
            times["after_job"].append(t)

            now = _STAMP
            store.db.execute(
                "INSERT INTO items (id, title, content, item_type, embedding, created_at, updated_at) "
                "VALUES (?, 'new', 'x', 'doc', ?, ?, ?)",
                (str(uuid.uuid4()), pack(*_vec(rng, args.dim)), now, now),
            )
            retrieval._cached_matrix = lambda *a, **k: None
            try:
                ref_after = run()
            finally:
                retrieval._cached_matrix = real_cached
            t, after_item = _timed(run)
            times["after_item"].append(t)

            mismatches += (warm != ref) + (after_job != ref) + (after_item != ref_after)
        store.close()

    summary = {
        k: {
            "p50_ms": round(statistics.median(v), 1),
            "min_ms": round(min(v), 1),
            "max_ms": round(max(v), 1),
        }
        for k, v in times.items()
    }
    summary["ranking_mismatches"] = mismatches
    summary["rounds"] = args.rounds
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
