"""The resident fast paths of memory recall return exactly what the scans return.

Covers the derived work held on ``_SemanticScoringSet`` (row tokens, row
vectors), the status-first eligibility pass, and the
lesson render memo. Each is checked against the unchanged slow path on the
same store, so a speed change cannot quietly change a result.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from kiro_crew import memory_record_metadata as record_meta
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime import lessons, semantic, term_index

_DIM = 16


def _embed(text: str) -> list[float]:
    """Deterministic, text-dependent, not normalized (rows carry their own norm)."""
    digest = hashlib.sha256(text.encode()).digest()
    return [(b - 128) / 64.0 for b in digest[:_DIM]]


def _store(tmp_path: Path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=_DIM)
    store.init()
    store.embed_fn = _embed
    return store


def _seed_facts(store: VectorMemoryStore, n: int) -> None:
    words = ["tokyo", "paris", "deploy", "pipeline", "memory", "recall", "rust", "python"]
    for i in range(n):
        value = f"{words[i % len(words)]} {words[(i * 3) % len(words)]} note {i}"
        assert store.set_semantic(f"project.item_{i}.fact", value, 1.0, "user_explicit") is None


def _scan(store: VectorMemoryStore, query: str) -> list[dict]:
    """The per-call scan path: the resident set disabled."""
    original = store._semantic_scoring_set
    store._semantic_scoring_set = lambda: None  # type: ignore[method-assign]
    try:
        return store._semantic_candidates_v1(query)
    finally:
        store._semantic_scoring_set = original  # type: ignore[method-assign]


@pytest.mark.parametrize("query", ["tokyo deploy", "rust recall memory", "nothing matches zzz"])
def test_resident_fact_ranking_matches_the_scan(tmp_path: Path, query: str) -> None:
    store = _store(tmp_path)
    _seed_facts(store, 60)
    # One row with no vector and one of another width: both score 0.0 on the
    # vector term in either path.
    store.db.execute(
        "UPDATE semantic_memory SET embedding = NULL WHERE key = 'project.item_3.fact'"
    )
    store.db.execute(
        "UPDATE semantic_memory SET embedding = ? WHERE key = 'project.item_4.fact'",
        (np.ones(_DIM // 2, dtype=np.float32).tobytes(),),
    )
    store.db.commit()
    store._semantic_scoring = None

    fast = store._semantic_candidates_v1(query)
    assert store._semantic_scoring is not None, "the fast path must have run"
    assert store._semantic_scoring.derived, "derived work must be held on the set"
    slow = _scan(store, query)

    assert [r["key"] for r in fast] == [r["key"] for r in slow]
    # Whole payloads, not chosen fields: identity, retrieval, row columns, order.
    assert fast == slow
    # A second query reuses the same derived state rather than rebuilding it.
    held = store._semantic_scoring.derived
    store._semantic_candidates_v1(query + " again")
    assert store._semantic_scoring.derived is held


def test_resident_fact_ranking_honours_ineligible_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_facts(store, 10)
    store.db.execute(
        "UPDATE memory_record_meta SET status = 'forgotten' WHERE record_id = ?",
        (record_meta.record_id_for("fact", "project.item_0.fact"),),
    )
    store.db.commit()
    store._semantic_scoring = None
    fast = [r["key"] for r in store._semantic_candidates_v1("tokyo")]
    assert "project.item_0.fact" not in fast
    assert fast == [r["key"] for r in _scan(store, "tokyo")]


def test_scoring_cosines_match_the_per_row_scorer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_facts(store, 300)  # spans several 128-row chunks
    scoring = store._semantic_scoring_set()
    query = _embed("query text")
    per_row = store._stored_similarity_scorer(query)
    cosines = semantic._scoring_cosines(scoring, query)
    for i, row in enumerate(scoring.rows):
        assert cosines[i] == per_row(row), i  # bit-identical, not merely close
    zero = semantic._scoring_cosines(scoring, [0.0] * _DIM)
    assert not any(zero)


def test_scoring_vectors_reference_the_rows_own_blobs(tmp_path: Path) -> None:
    """The set keeps no second copy of the embeddings."""
    store = _store(tmp_path)
    _seed_facts(store, 20)
    scoring = store._semantic_scoring_set()
    semantic._scoring_cosines(scoring, _embed("q"))
    index, blobs, _norms = scoring.derived[("vectors", _DIM)]
    for i, blob in zip(index, blobs):
        assert blob is scoring.rows[i]["embedding"]


def test_token_memo_stops_growing_at_its_byte_cap(tmp_path: Path, monkeypatch) -> None:
    # The per-text memo serves a set the term index declines.
    monkeypatch.setattr(term_index, "MAX_TERM_IDS", 0)
    store = _store(tmp_path)
    _seed_facts(store, 60)
    store._semantic_scoring = None
    query = "tokyo deploy"
    uncapped = store._semantic_candidates_v1(query)
    full = len(store._semantic_scoring.derived["tokens"])

    monkeypatch.setattr(semantic, "_SCORING_TOKEN_MEMO_MAX_BYTES", 2_000)
    store._semantic_scoring = None
    capped = store._semantic_candidates_v1(query)
    derived = store._semantic_scoring.derived
    assert 0 < len(derived["tokens"]) < full
    held = len(derived["tokens"])
    store._semantic_candidates_v1(query)
    assert len(derived["tokens"]) == held
    # The cap trades speed only: the ranking is unchanged.
    assert capped == uncapped


def test_ineligible_record_ids_settles_status_without_a_clock() -> None:
    rows = [
        {"record_id": "a", "status": "forgotten", "valid_from": "", "valid_until": ""},
        {"record_id": "b", "status": "superseded", "valid_from": "", "valid_until": ""},
        {"record_id": "c", "status": None, "valid_from": "", "valid_until": ""},
        {
            "record_id": "d",
            "status": "active",
            "valid_from": "2999-01-01T00:00:00+00:00",
            "valid_until": "",
        },
        {
            "record_id": "e",
            "status": "active",
            "valid_from": "",
            "valid_until": "2000-01-01T00:00:00+00:00",
        },
        {
            "record_id": "f",
            "status": "active",
            "valid_from": "2000-01-01T00:00:00+00:00",
            "valid_until": "2999-01-01T00:00:00+00:00",
        },
    ]
    expected = {r["record_id"] for r in rows if not record_meta.eligible(dict(r))}
    assert record_meta.ineligible_record_ids(rows) == expected == {"a", "b", "c", "d", "e"}


def test_lesson_render_memo_matches_the_uncached_render(tmp_path: Path, caplog) -> None:
    store = _store(tmp_path)
    for i in range(5):
        store.write_lesson(f"Always run the build before release step {i}", "tool")
    rows = store.get_lessons()
    rows.append({"key": "lesson.broken", "value_json": "{not json"})
    lessons._lesson_render_fields.cache_clear()
    with caplog.at_level("WARNING", logger="kiro_crew.vector_memory"):
        cached = lessons._renderable_entries(rows, None)
    assert "lesson.broken" in caplog.text
    uncached = []
    for row in rows:
        try:
            decoded = json.loads(row["value_json"])
        except ValueError:
            continue
        text = lessons._renderable_lesson_text(decoded, row["key"])
        if text:
            uncached.append((row, text))
    assert [(r["key"], t) for r, t in cached] == [(r["key"], t) for r, t in uncached]
    assert lessons._lesson_render_fields.cache_info().currsize == len(rows)


def _memo_path(store: VectorMemoryStore, query: str, monkeypatch) -> list[dict]:
    """The resident set scored through the per-text memo, the index declined."""
    with monkeypatch.context() as m:
        m.setattr(term_index, "MAX_TERM_IDS", 0)
        store._semantic_scoring = None
        return store._semantic_candidates_v1(query)


@pytest.mark.parametrize(
    "query", ["tokyo deploy", "Rust recall memories", "pipelines deploying notes 7", "zzz"]
)
def test_term_index_ranks_like_the_memo_and_the_scan(
    tmp_path: Path, monkeypatch, query: str
) -> None:
    store = _store(tmp_path)
    _seed_facts(store, 60)
    store._semantic_scoring = None
    fast = store._semantic_candidates_v1(query)
    derived = store._semantic_scoring.derived
    assert isinstance(derived["term_index"][0], term_index.RowTermIndex)
    assert not derived.get("tokens"), "the index path stemmed a row through the memo"
    assert fast == _memo_path(store, query, monkeypatch) == _scan(store, query)


def test_a_label_changed_since_the_build_is_scored_from_its_new_text(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    _seed_facts(store, 20)
    store._semantic_scoring = None
    store._semantic_candidates_v1("tokyo")  # builds the index with no identities
    real = store._fact_identities
    renamed = {"project.item_2.fact": {"subject": "Kyoto", "predicate": "trip", "scope": ""}}
    monkeypatch.setattr(store, "_fact_identities", lambda: {**real(), **renamed})
    fast = store._semantic_candidates_v1("kyoto trip")
    renamed_row = next(r for r in fast if r["key"] == "project.item_2.fact")
    assert renamed_row["retrieval"]["matched_terms"] == ["trip"]
    assert fast == _scan(store, "kyoto trip")


def test_the_index_is_built_once_per_set_and_patched_by_a_write(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    _seed_facts(store, 10)
    store._semantic_scoring = None
    builds = []
    real_build = term_index.RowTermIndex.build
    monkeypatch.setattr(
        term_index.RowTermIndex,
        "build",
        classmethod(lambda cls, texts, np: builds.append(1) or real_build(texts, np)),
    )
    for query in ("tokyo", "paris", "rust"):
        store._semantic_candidates_v1(query)
    assert builds == [1]
    assert store.set_semantic("project.new.fact", "brand new", 1.0, "user_explicit") is None
    found = store._semantic_candidates_v1("brand")
    assert "project.new.fact" in [r["key"] for r in found]
    assert builds == [1], "a write patches the set's index in place; it is not built again"
    patched = store._semantic_scoring.derived["term_index"]
    assert isinstance(patched[0], term_index.RowTermIndex)
    assert len(patched[1]) == len(store._semantic_scoring.rows)


def test_term_index_holds_each_text_s_exact_terms() -> None:
    from kiro_crew.vector_memory_runtime import text_scoring

    texts = ["deploy pipelines", "Rust memory recalls", "", "deploy deploy x"]
    index = term_index.RowTermIndex.build(texts, np)
    for query in ({"deploy", "rust", "memori"}, {"recal", "recall", "x"}, set()):
        hits = index.query(query)
        for i, text in enumerate(texts):
            assert hits.matched(i) == query & text_scoring._row_stem_tokens_uncached(text)


def test_past_its_bound_the_index_declines(monkeypatch) -> None:
    monkeypatch.setattr(term_index, "MAX_TERM_IDS", 3)
    assert term_index.RowTermIndex.build(["a b", "c d e"], np) is None
    assert term_index.RowTermIndex.build(["a b", "c"], np) is not None
