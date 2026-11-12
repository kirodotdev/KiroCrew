"""One row score for both places that rank semantic rows against a query.

The chat path's V1 candidates (``_semantic_candidates_v1``) and a consolidation pass's
relevance (``score_consolidation_rows``) score each row through
``text_scoring._semantic_row_score``: a query word the key side holds counts 3, one the
value holds counts 1, the sum is normalized over ten, and the result blends 0.6 cosine
to 0.4 keyword when the query has a vector. The first tests pin that arithmetic on the
helper, the next pins it on the chat path (the consolidation suite pins it on its own
path), and the last checks both callers give one row the same score.

Every store here is a real V1 file on ``tmp_path``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime import text_scoring

_DIM = 16


def _embed(text: str) -> list[float]:
    """Deterministic and text-dependent, so cosines fall anywhere in [-1, 1]."""
    digest = hashlib.sha256(text.encode()).digest()
    return [(b - 128) / 64.0 for b in digest[:_DIM]]


def _store(path: Path, opened, *, embed: bool) -> VectorMemoryStore:
    store = opened(VectorMemoryStore(db_path=path, embedding_dim=_DIM))
    store.init()
    if embed:
        store.embed_fn = _embed
    return store


def _score(
    query: set[str], key: str, value: str, cosine: float = 0.0, *, query_has_vector: bool = False
) -> float:
    tokens = text_scoring._row_stem_tokens_uncached
    return text_scoring._semantic_row_score(
        query,
        text_scoring._semantic_key_words(key, tokens),
        tokens(value),
        cosine,
        query_has_vector=query_has_vector,
    )


# ── the helper ─────────────────────────────────────────────────────────────────────


def test_a_key_word_counts_three_a_value_word_one_and_a_word_both_hold_four() -> None:
    assert _score({"boat"}, "project.zz_boat", "paddle at dawn") == 0.3
    assert _score({"paddle"}, "project.zz_boat", "paddle at dawn") == 0.1
    assert _score({"kayak"}, "project.kayak_trip", "paddle a kayak") == 0.4
    assert _score({"boat", "paddle"}, "project.zz_boat", "paddle at dawn") == 0.4
    assert _score({"piano"}, "project.zz_boat", "paddle at dawn") == 0.0


def test_underscores_and_dots_in_a_label_break_words() -> None:
    words = text_scoring._semantic_key_words(
        "user.contact_primary (alice, work_email)", text_scoring._row_stem_tokens_uncached
    )
    assert {"user", "contact", "primary", "alice", "work", "email"} <= words


def test_the_keyword_term_stops_at_ten() -> None:
    # Four key words are 12 of 10.
    assert _score({"a1", "b2", "c3", "d4"}, "project.a1_b2_c3_d4", "") == 1.0


def test_with_a_query_vector_the_score_blends_the_cosine_with_the_keyword_term() -> None:
    blended = _score({"boat"}, "project.zz_boat", "", 0.5, query_has_vector=True)
    assert blended == pytest.approx(0.6 * 0.5 + 0.4 * 0.3)
    # A row with no stored vector keeps the 0.4 keyword weight of its embedded siblings.
    assert _score({"boat"}, "project.zz_boat", "", 0.0, query_has_vector=True) == (
        pytest.approx(0.4 * 0.3)
    )
    # A cosine is the caller's to clamp: the helper takes it as given.
    assert _score({"boat"}, "project.zz_boat", "", -0.5, query_has_vector=True) == (
        pytest.approx(0.6 * -0.5 + 0.4 * 0.3)
    )


def test_without_a_query_vector_the_score_is_the_keyword_term() -> None:
    assert _score({"boat"}, "project.zz_boat", "", 0.0, query_has_vector=False) == 0.3


# ── the callers ────────────────────────────────────────────────────────────────────


def test_the_chat_candidates_weigh_a_key_word_three_and_a_value_word_one(tmp_path, opened) -> None:
    store = _store(tmp_path / "memory.db", opened, embed=False)
    for key, value in (
        ("project.zz_boat", "paddle a kayak at dawn"),
        ("project.yard", "garden needs water"),
        ("project.music", "weekly piano lessons"),
    ):
        assert store.set_semantic(key, value, 0.9, "user_explicit") is None
    assert (
        store.set_semantic(
            "user.contact.primary",
            "alice at example dot com",
            0.9,
            "user_explicit",
            metadata={"subject": "alice", "predicate": "work_email"},
        )
        is None
    )

    ranked = store._semantic_candidates_v1("boat garden")
    assert [(r["key"], r["retrieval"]["score"]) for r in ranked] == [
        ("project.zz_boat", 0.3),
        ("project.yard", 0.1),
    ]
    # The identity's predicate words count as key words.
    ranked = store._semantic_candidates_v1("her work email")
    assert [(r["key"], r["retrieval"]["score"]) for r in ranked] == [
        ("user.contact.primary", pytest.approx(0.6))
    ]


@pytest.mark.parametrize("embed", [True, False], ids=["vectors", "keywords"])
def test_both_callers_give_a_row_the_same_score(tmp_path, opened, embed) -> None:
    store = _store(tmp_path / "memory.db", opened, embed=embed)
    for key, value in (
        ("project.zz_boat", "paddle a kayak at dawn"),
        ("project.kayak_club", "meets at the lake"),
        ("project.yard", "garden needs water"),
        ("project.music", "weekly piano lessons"),
    ):
        assert store.set_semantic(key, value, 0.9, "user_explicit") is None
    assert (
        store.set_semantic(
            "user.contact.primary",
            "alice at example dot com",
            0.9,
            "user_explicit",
            metadata={"subject": "alice", "predicate": "work_email"},
        )
        is None
    )
    for index in range(20):
        value = f"filler row {index}"
        assert (
            store.set_semantic(f"project.filler_{index:02d}", value, 0.9, "user_explicit") is None
        )
    # No query word is held by more than two of the 25 rows, so the consolidation
    # scorer's common-word filter leaves the query whole and both see the same words.
    query = "kayak garden work email"

    chat = {r["key"]: r["retrieval"]["score"] for r in store._semantic_candidates_v1(query)}
    rows = store.get_all_semantic()
    scores = store.score_consolidation_rows(rows, [r["value_json"] for r in rows], query)
    assert scores is not None
    consolidation = {row["key"]: score for row, score in zip(rows, scores) if score > 0}

    assert chat == consolidation
    assert {"project.zz_boat", "project.kayak_club", "user.contact.primary"} <= set(chat)
    if embed:
        # Non-vacuous: some row scored on its vector, not only on its words.
        assert any(r["retrieval"]["cosine"] for r in store._semantic_candidates_v1(query))
