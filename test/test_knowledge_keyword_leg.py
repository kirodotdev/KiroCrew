"""The keyword leg's top hit survives RRF truncation in hybrid search.

The vector leg carries ``VECTOR_RRF_WEIGHT`` in the fusion, so a document found
only by the FTS5 keyword leg can be pushed past ``limit`` even when it is the
single best answer -- the case where a query carries an exact error string, a
ticket id or a rare technical term. ``HybridRetriever.search`` therefore appends
that one document as an extra trailing row instead of dropping it.

The legs are stubbed here so the ranking under test is exact: fusion, truncation
and row construction run against a real ``KnowledgeStore``.
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

# The confidence floor `local_knowledge_search` applies to every row it renders
# (mcp_tools/knowledge.py). A row stamped with a sentinel score instead of its
# real fused score would be filtered out there, making the rescue a no-op on the
# primary LLM surface.
_MIN_TOOL_SCORE = 0.012

# RRF constant k, mirrored from `_rrf_fuse`, so expected scores are exact.
_RRF_K = 60


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kw-leg.db"))
    yield s
    s.close()


def _retriever(store, kw, gr=None, vec=None) -> HybridRetriever:
    """A retriever whose three legs return fixed ``[(item_id, rank)]`` lists."""
    retriever = HybridRetriever(store)
    retriever._keyword_search = lambda *a, **kw_: list(kw)  # type: ignore[method-assign]
    retriever._graph_search = lambda *a, **kw_: list(gr or [])  # type: ignore[method-assign]
    retriever._vector_search = lambda *a, **kw_: (  # type: ignore[method-assign]
        None if vec is None else list(vec)
    )
    return retriever


@pytest.fixture()
def crowded(store):
    """A keyword-only target crowded out by four weighted vector-only hits.

    Fused scores: each vector hit scores ``2.0 / (60 + rank)`` and the target
    ``1.0 / 61``, so the target sorts last of the five.
    """
    target = store.add_item("Exact Error String", "ORA-01555 snapshot too old", "doc")
    vec_ids = [
        store.add_item(f"Semantic Neighbour {n}", f"related prose {n}", "doc") for n in range(4)
    ]
    return target, vec_ids


def _search(store, target, vec_ids, limit, gr=None):
    kw = [(target, 1)]
    vec = [(item_id, rank) for rank, item_id in enumerate(vec_ids, start=1)]
    return _retriever(store, kw, gr=gr, vec=vec).search("ORA-01555", limit=limit)


def test_keyword_top_hit_is_appended_when_fusion_drops_it(store, crowded):
    """The dropped keyword winner comes back as one extra trailing row."""
    target, vec_ids = crowded
    results = _search(store, target, vec_ids, limit=3)
    assert len(results) == 4
    assert results[-1]["id"] == target


def test_appended_row_carries_its_fused_score_and_leg_label(store, crowded):
    """The extra row keeps its real fused score, so downstream floors pass it."""
    target, vec_ids = crowded
    results = _search(store, target, vec_ids, limit=3)
    rescued = results[-1]
    assert rescued["score"] == pytest.approx(1.0 / (_RRF_K + 1))
    assert rescued["score"] >= _MIN_TOOL_SCORE
    assert rescued["match_type"] == "keyword"
    assert rescued["title"] == "Exact Error String"
    assert rescued["content"] == "ORA-01555 snapshot too old"


def test_appended_row_records_every_leg_it_matched(store, crowded):
    """A rescued row that the graph leg also found is labelled for both legs."""
    target, vec_ids = crowded
    results = _search(store, target, vec_ids, limit=3, gr=[(target, 20)])
    assert results[-1]["id"] == target
    assert results[-1]["match_type"] == "keyword+graph"


def test_appended_row_carries_citation_metadata(store):
    """The extra row is enriched like every other, so it stays citable."""
    sid = store.add_source("runbook.md", "local_file", "/docs/runbook.md")
    target = store.add_item(
        "Exact Error String", "ORA-01555 snapshot too old", "doc", source_id=sid
    )
    store.add_source_location(target, sid, chunk_range="10-25", section_title="Snapshot Errors")
    vec_ids = [store.add_item(f"Prose {n}", f"neighbour {n}", "doc") for n in range(4)]
    results = _search(store, target, vec_ids, limit=3)
    rescued = results[-1]
    assert rescued["id"] == target
    assert rescued["section_title"] == "Snapshot Errors"
    assert rescued["chunk_range"] == "10-25"
    assert rescued["source_uri"] == "/docs/runbook.md"


def test_ranked_rows_are_untouched_by_the_rescue(store, crowded):
    """Nothing is removed, reordered or demoted to make room for the extra row.

    The weighted vector leg therefore still outranks a literal keyword leg --
    the behaviour VECTOR_RRF_WEIGHT exists for: the rescue only appends, it
    never promotes.
    """
    target, vec_ids = crowded
    results = _search(store, target, vec_ids, limit=3)
    assert [r["id"] for r in results[:3]] == vec_ids[:3]
    assert results[0]["score"] > results[-1]["score"]


def test_no_extra_row_when_keyword_top_hit_already_ranked(store, crowded):
    """A window wide enough for the keyword winner returns it exactly once."""
    target, vec_ids = crowded
    results = _search(store, target, vec_ids, limit=5)
    assert len(results) == 5
    assert [r["id"] for r in results].count(target) == 1


def test_no_extra_row_when_keyword_leg_found_nothing(store, crowded):
    """An empty keyword leg leaves the window exactly as it was."""
    _target, vec_ids = crowded
    vec = [(item_id, rank) for rank, item_id in enumerate(vec_ids, start=1)]
    results = _retriever(store, [], vec=vec).search("prose", limit=3)
    assert len(results) == 3


def test_no_extra_row_when_the_vector_leg_is_unavailable(store, crowded):
    """With no embedder the keyword leg is unweighted, so nothing crowds it out."""
    target, vec_ids = crowded
    kw = [(target, 1)] + [(item_id, rank) for rank, item_id in enumerate(vec_ids, start=2)]
    results = _retriever(store, kw, vec=None).search("ORA-01555", limit=3)
    assert len(results) == 3
    assert results[0]["id"] == target


def test_limit_one_still_protects_the_keyword_top_hit(store, crowded):
    """The narrowest window is where the loss hurts most, so it is covered too."""
    target, vec_ids = crowded
    results = _search(store, target, vec_ids, limit=1)
    assert [r["id"] for r in results] == [vec_ids[0], target]


def test_unresolvable_keyword_top_hit_adds_no_row(store, crowded):
    """A stale FTS row pointing at a deleted item is skipped, not surfaced."""
    target, vec_ids = crowded
    store.db.execute("DELETE FROM items WHERE id = ?", (target,))
    store.db.commit()
    results = _search(store, target, vec_ids, limit=3)
    assert [r["id"] for r in results] == vec_ids[:3]
