"""A write patches the resident semantic scoring set; recall ranks what a rebuild ranks.

Every check compares the store that was patched in place with a second store on the
same database file that has to rebuild its set from the table, so a patch that
drifts from the rebuild in rows, term index, row vectors or ranking fails here.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import numpy as np
import pytest

from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime import semantic, term_index

_DIM = 16
_QUERIES = ["tokyo deploy", "rust recall memory", "brand new gateway", "python note", "zzz nothing"]


def _embed(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [(b - 128) / 64.0 for b in digest[:_DIM]]


def _store(path: Path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=path, embedding_dim=_DIM)
    store.init()
    store.embed_fn = _embed
    return store


@pytest.fixture
def pair(tmp_path: Path):
    """(patched store, reference store) on one database, with 60 seeded facts."""
    path = tmp_path / "mem.db"
    store = _store(path)
    words = ["tokyo", "paris", "deploy", "pipeline", "memory", "recall", "rust", "python"]
    for i in range(60):
        value = f"{words[i % len(words)]} {words[(i * 3) % len(words)]} note {i}"
        assert store.set_semantic(f"project.item_{i}.fact", value, 1.0, "user_explicit") is None
    reference = _store(path)
    yield store, reference
    store.close()
    reference.close()


def _count_scans(store: VectorMemoryStore) -> dict[str, int]:
    counts = {"n": 0}
    original = store._fetch_all_locked

    def counting(sql, params=(), *, scan=None):
        if scan == "semantic":
            counts["n"] += 1
        return original(sql, params, scan=scan)

    store._fetch_all_locked = counting  # type: ignore[method-assign]
    return counts


def _warm(store: VectorMemoryStore) -> None:
    """Build the resident set and every piece derived from it."""
    for query in _QUERIES:
        store._semantic_candidates_v1(query)
    derived = store._semantic_scoring.derived
    assert derived["term_index"] is not None
    assert ("vectors", _DIM) in derived


def _assert_matches_rebuild(store: VectorMemoryStore, reference: VectorMemoryStore) -> None:
    """The patched set and its derived work equal what a rebuild produces."""
    assert store._semantic_scoring is not None, "the write must patch the resident set, not drop it"
    for query in _QUERIES:
        assert store._semantic_candidates_v1(query) == reference._semantic_candidates_v1(query)
    patched = store._semantic_scoring
    assert patched is not None
    rebuilt = reference._semantic_scoring
    assert rebuilt is not None
    by_key = lambda rows: sorted(rows, key=lambda r: r["key"])  # noqa: E731
    assert by_key(patched.rows) == by_key(rebuilt.rows)

    # Derived work, against the same function of the same rows computed from scratch.
    fresh = semantic._SemanticScoringSet(patched.rows, patched.generation, patched.data_version)
    query_vec = _embed("tokyo deploy")
    assert semantic._scoring_cosines(patched, query_vec) == semantic._scoring_cosines(
        fresh, query_vec
    )
    held = patched.derived["term_index"]
    assert held is not None
    index, key_texts = held
    scratch = term_index.RowTermIndex.build(
        [
            text
            for row, key_text in zip(patched.rows, key_texts)
            for text in (key_text, str(row["value_json"]).lower())
        ],
        np,
    )
    assert scratch is not None
    for terms in ({"tokyo", "deploy"}, {"note", "brand"}, {"python"}, {"absent"}):
        got, want = index.query(terms), scratch.query(terms)
        for text_index in range(2 * len(patched.rows)):
            assert got.matched(text_index) == want.matched(text_index)
    assert len(key_texts) == len(patched.rows)
    for row, key_text in zip(patched.rows, key_texts):
        assert key_text == str(row["key"]).replace("_", " ").replace(".", " ")


def test_a_write_patches_the_resident_set_instead_of_rebuilding(pair) -> None:
    store, reference = pair
    _warm(store)
    scans = _count_scans(store)

    steps = [
        lambda: store.set_semantic(
            "project.brand_new.fact", "brand new gateway tokyo", 1.0, "user_explicit"
        ),
        lambda: store.set_semantic(
            "project.item_3.fact", "rust python rewritten note", 1.0, "user_explicit"
        ),
        lambda: store.set_semantic(
            "project.item_3.fact", "rust python rewritten note", 1.0, "user_explicit"
        ),
        lambda: store.set_semantic_if_absent(
            "project.if_absent.fact", "deploy memory pipeline", 0.9, "user_explicit"
        ),
        lambda: store.set_semantic_if_absent(
            "project.if_absent.fact", "ignored", 0.9, "user_explicit"
        ),
        lambda: store.delete_semantic("project.item_10.fact", "user_explicit"),
        lambda: store.delete_semantic(
            "project.item_11.fact",
            "user_explicit",
            superseded_by="project.brand_new.fact",
            supersede_reason="test",
        ),
        lambda: store.delete_semantic("project.never_existed.fact", "user_explicit"),
        lambda: store.set_semantic(
            "project.item_10.fact", "tokyo returned after delete", 1.0, "user_explicit"
        ),
        lambda: store.set_semantic(
            "project.item_0.fact", "first row rewritten gateway", 1.0, "user_explicit"
        ),
        lambda: store.set_semantic(
            "project.item_59.fact", "last row rewritten gateway", 1.0, "user_explicit"
        ),
    ]
    for step in steps:
        step()
        _assert_matches_rebuild(store, reference)
    assert scans["n"] == 0, "no write or recall above may rescan the table"


def test_a_row_without_a_vector_or_of_another_width_patches_like_the_rebuild(pair) -> None:
    store, reference = pair
    _warm(store)
    store.embed_fn = None  # the next write stores no vector
    assert (
        store.set_semantic(
            "project.no_vector.fact", "gateway tokyo without vector", 1.0, "user_explicit"
        )
        is None
    )
    store.embed_fn = lambda text: _embed(text)[: _DIM // 2]  # and this one a narrower one
    assert (
        store.set_semantic(
            "project.narrow.fact", "gateway deploy narrow vector", 1.0, "user_explicit"
        )
        is None
    )
    store.embed_fn = _embed
    scans = _count_scans(store)
    _assert_matches_rebuild(store, reference)
    assert scans["n"] == 0


def test_a_lesson_write_keeps_the_set_and_moves_the_generation(pair) -> None:
    store, reference = pair
    _warm(store)
    resident = store._semantic_scoring
    generation = store._semantic_scoring_generation
    store._invalidate_semantic_scoring("lesson.something")
    assert store._semantic_scoring_generation == generation + 1
    assert store._semantic_scoring.rows is resident.rows
    assert store._semantic_scoring.derived is resident.derived
    _assert_matches_rebuild(store, reference)


def test_another_process_commit_falls_back_to_a_rebuild(pair) -> None:
    store, reference = pair
    _warm(store)
    # A commit from a second connection: the only writer a generation cannot see.
    reference.set_semantic(
        "project.foreign.fact", "foreign tokyo gateway write", 1.0, "user_explicit"
    )
    store.set_semantic("project.local.fact", "local tokyo gateway write", 1.0, "user_explicit")
    assert store._semantic_scoring is None, "a set that missed a foreign commit must not be patched"
    scans = _count_scans(store)
    for query in _QUERIES:
        got = store._semantic_candidates_v1(query)
        assert got == reference._semantic_candidates_v1(query)
    assert scans["n"] == 1
    keys = {r["key"] for r in store._semantic_candidates_v1("foreign tokyo gateway")}
    assert {"project.foreign.fact", "project.local.fact"} <= keys


def test_a_rebuild_does_not_hold_the_db_lock(pair) -> None:
    store, _ = pair
    store._semantic_scoring = None
    store._semantic_scoring_generation += 1
    original = store._fetch_all_locked
    acquired: list[bool] = []

    def watching(sql, params=(), *, scan=None):
        rows = original(sql, params, scan=scan)
        if scan == "semantic":

            def probe() -> None:
                got = store._db_lock.acquire(timeout=5)
                acquired.append(got)
                if got:
                    store._db_lock.release()

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(10)
        return rows

    store._fetch_all_locked = watching  # type: ignore[method-assign]
    assert store._semantic_scoring_set() is not None
    assert acquired == [True], "a writer must be able to take the lock while the table is read"
    assert store._semantic_scoring is not None


def test_a_write_during_a_rebuild_keeps_the_stale_read_from_being_installed(pair) -> None:
    store, reference = pair
    store._semantic_scoring = None
    store._semantic_scoring_generation += 1
    original = store._fetch_all_locked
    fired: list[bool] = []

    def writing(sql, params=(), *, scan=None):
        rows = original(sql, params, scan=scan)
        if scan == "semantic" and not fired:
            fired.append(True)
            store.set_semantic(
                "project.during.fact", "during rebuild tokyo gateway", 1.0, "user_explicit"
            )
        return rows

    store._fetch_all_locked = writing  # type: ignore[method-assign]
    first = store._semantic_scoring_set()
    assert first is not None
    assert "project.during.fact" not in {r["key"] for r in first.rows}
    assert store._semantic_scoring is None, "a read that predates a write must not become resident"
    store._fetch_all_locked = original  # type: ignore[method-assign]
    for query in _QUERIES + ["during rebuild"]:
        assert store._semantic_candidates_v1(query) == reference._semantic_candidates_v1(query)
    assert "project.during.fact" in {r["key"] for r in store._semantic_scoring.rows}


def _deferred_write(store: VectorMemoryStore, key: str, value: str) -> None:
    """A consolidation-style write: applied on the connection, committed by the caller."""
    import json

    with store._db_lock:
        assert (
            store._write_semantic(key, json.dumps(value), 1.0, "user_explicit", _consolidation=True)
            is None
        )


def _keys(store: VectorMemoryStore) -> set[str]:
    return {row["key"] for row in store._semantic_scoring.rows}


def test_a_rolled_back_write_leaves_nothing_in_the_set(pair) -> None:
    store, reference = pair
    _warm(store)
    scans = _count_scans(store)

    _deferred_write(store, "project.rolled_back.fact", "rolled back tokyo gateway")
    _deferred_write(store, "project.item_3.fact", "rewritten then rolled back python note")
    assert "project.rolled_back.fact" in _keys(store), "the open transaction's own rows are visible"
    store.db.rollback()

    _assert_matches_rebuild(store, reference)
    assert "project.rolled_back.fact" not in _keys(store)
    assert scans["n"] == 0, "the rollback is repaired by patching, not by a rescan"
    assert not store._semantic_scoring_pending


def test_a_later_transaction_does_not_hide_an_earlier_rollback(pair) -> None:
    store, reference = pair
    _warm(store)
    _deferred_write(store, "project.rolled_back.fact", "rolled back tokyo gateway")
    store.db.rollback()
    _deferred_write(store, "project.second.fact", "second batch deploy memory")
    assert "project.rolled_back.fact" not in _keys(store)
    assert "project.second.fact" in _keys(store)
    store.db.commit()
    _assert_matches_rebuild(store, reference)
    assert "project.second.fact" in _keys(store)
    assert not store._semantic_scoring_pending


def test_a_committed_batch_keeps_its_patched_rows(pair) -> None:
    store, reference = pair
    _warm(store)
    scans = _count_scans(store)
    for i in range(5):
        _deferred_write(store, f"project.batch_{i}.fact", f"batch tokyo deploy {i}")
    store.db.commit()
    _assert_matches_rebuild(store, reference)
    assert {f"project.batch_{i}.fact" for i in range(5)} <= _keys(store)
    assert scans["n"] == 0
    assert not store._semantic_scoring_pending


def test_the_term_index_vocabulary_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(term_index, "VOCAB_GROWTH_SLACK", 2)
    index = term_index.RowTermIndex.build(["alpha beta", "gamma delta", "alpha", "beta"], np)
    assert index is not None
    built = len(index._terms)
    bound = built + max(2, built)
    grown = index
    for i in range(1, 20):
        # Replace a row with one of fresh words: the live texts stay
        # the same size while the vocabulary only gains.
        grown = grown.patched(0, [f"fresh{i}a fresh{i}b", "alpha"])
        if grown is None:
            break
        assert len(grown._terms) <= bound
    assert grown is None, "the index must be dropped once the vocabulary outgrows its bound"
    assert len(index._terms) > bound, "the drop happens past the bound, not before it"
