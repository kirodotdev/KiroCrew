"""The memory page's search by meaning ranks every kind by its stored vector."""

from __future__ import annotations

import pytest
from test_memory_edit import store  # noqa: F401  (the v1 / v2 store fixture)

from kiro_crew import memory_edit, memory_record_meaning


def _embed(text: str) -> list[float]:
    """Three topics on three axes, so the cosine is known exactly."""
    lowered = text.lower()
    return [
        1.0 if "worktree" in lowered or "checkout" in lowered else 0.0,
        1.0 if "chinese" in lowered or "language" in lowered else 0.0,
        1.0 if "shard" in lowered else 0.05,
    ]


def _seed(store):  # noqa: F811
    store.embed_fn = _embed
    assert store.set_semantic("user.language", "Chinese for chat", 1.0, "user_explicit") is None
    assert store.set_semantic("project.flaky", "shard 8 is flaky", 0.8, "taskrunner") is None
    value = {"rule": "Make every change in a worktree", "category": "tool", "applies": "always"}
    assert store.set_semantic("lesson.a", value, 1.0, "user_explicit") is None
    assert store.write_episodic("Kept the live checkout clean.", conversation_id="c1", tags=["ops"])
    # Lessons get their vector from the normal backfill, as on a real store.
    store.backfill_missing_embeddings(pace=False)


def test_ranks_facts_lessons_and_experiences_together_and_hides_weak_matches(store):  # noqa: F811
    _seed(store)
    body = memory_edit.list_records(store, {"q": "worktree habits"}, meaning=True, facets=True)
    kinds = [entry["kind"] for entry in body["entries"]]
    # A lesson and an experience match by meaning, though only one shares the word.
    assert sorted(kinds) == ["directive", "episode"]
    assert all(entry["match"] >= memory_record_meaning.MEANING_CUTOFF for entry in body["entries"])
    assert body["entries"][0]["match"] >= body["entries"][-1]["match"]
    assert body["hidden"] == 2
    assert body["total"] == 2
    assert body["facets"]["kind"] == {"directive": 1, "episode": 1}
    weak = memory_edit.list_records(store, {"q": "worktree habits"}, meaning=True, weak=True)
    assert weak["total"] == 4
    assert "hidden" in weak and weak["hidden"] == 0
    assert weak["entries"][-1]["match"] < memory_record_meaning.MEANING_CUTOFF


def test_kind_and_filters_apply_to_the_ranked_set(store):  # noqa: F811
    _seed(store)
    body = memory_edit.list_records(store, {"q": "worktree", "kind": "episode"}, meaning=True)
    assert [entry["kind"] for entry in body["entries"]] == ["episode"]
    # The hidden count is for the same kind: both facts are facts, not experiences.
    assert body["hidden"] == 0


def test_refuses_without_an_embedding_model(store):  # noqa: F811
    store.embed_fn = None
    store.embed_fn_factory = None
    with pytest.raises(memory_edit.MemoryEditError) as refused:
        memory_edit.list_records(store, {"q": "worktree"}, meaning=True)
    assert refused.value.code == "meaning_unavailable"
    assert refused.value.status == 409
    with pytest.raises(memory_edit.MemoryEditError):
        memory_edit.list_records(store, {"q": ""}, meaning=True)


def test_embeds_the_search_words_as_a_recall_query(store, monkeypatch):  # noqa: F811
    # Recall embeds its words with the model's query instruction; this search must match it.
    seen = []

    def embed(text, priority, *, query=False):
        seen.append(query)
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(store, "_try_embed", embed)
    memory_record_meaning.embed_query(store, "worktree habits")
    assert seen == [True]


def test_graph_search_matches_the_list_without_adding_meaning_only_hits(store):  # noqa: F811
    _seed(store)
    # The lesson holds the word; the episode matches by meaning only. A second
    # word match has no related vector, so semantic admission cannot replace words.
    assert store.set_semantic("user.word", "worktree", 0.8, "user_explicit") is None
    table = "memory_items" if store.algorithm_version == "v2" else "semantic_memory"
    with store._db_lock:
        store.db.execute(f"UPDATE {table} SET embedding = NULL WHERE key = ?", ("user.word",))
        store.db.commit()
    query = {"q": "worktree"}
    words = memory_edit.list_records(store, query, facets=True)
    semantic = memory_edit.list_records(store, query, meaning=True)
    lit, meaning = memory_edit.matching_scores(store, query)
    assert {row["id"] for row in words["entries"]} == {"lesson.a", "user.word"}
    assert any(row["kind"] == "episode" for row in semantic["entries"])
    assert "user.word" not in {row["id"] for row in semantic["entries"]}
    assert set(lit) == {row["id"] for row in words["entries"]}
    assert len(lit) == words["total"]
    assert set(lit.values()) == {1.0}
    assert meaning is False


def test_graph_search_uses_list_kind_filter_without_embedding(store, monkeypatch):  # noqa: F811
    _seed(store)
    query = {"q": "worktree", "kind": "directive"}
    monkeypatch.setattr(
        memory_record_meaning, "embed_query", lambda *_: pytest.fail("word search must not embed")
    )
    words = memory_edit.list_records(store, query, limit=1)
    lit, meaning = memory_edit.matching_scores(store, query)
    assert set(lit) == {row["id"] for row in words["entries"]} == {"lesson.a"}
    assert len(lit) == words["total"]
    assert meaning is False


def test_graph_search_finds_by_words_alone_without_an_embedding_model(store):  # noqa: F811
    _seed(store)
    store.embed_fn = None
    store.embed_fn_factory = None
    lit, meaning = memory_edit.matching_scores(store, {"q": "shard"})
    assert meaning is False
    assert lit == {"project.flaky": 1.0}


def test_graph_search_reads_light_summaries_and_keeps_list_kind_and_box(
    store, monkeypatch  # noqa: F811
):
    # A broad word on a large store must not build a full record per match under the lock.
    from kiro_crew import memory_record_facets, memory_record_metadata

    _seed(store)
    assert store.set_semantic("user.word", "worktree", 0.8, "user_explicit") is None
    assert store.write_episodic("Cleaned a worktree up.", conversation_id="c2", tags=["ops"])
    boxed = frozenset({"lesson.a", "user.word"})
    monkeypatch.setattr(
        memory_edit,
        "_with_box",
        lambda _store, filters: (
            {**filters, memory_record_facets.BOX_IDS: boxed} if "box" in filters else filters
        ),
    )
    box = {"box": "-1e9,-1e9,1e9,1e9", "box_layout": "L1"}
    queries = [
        {"q": "worktree"},
        {"q": "worktree", "kind": "episode"},
        {"q": "worktree", "kind": "directive"},
        {"q": "worktree", "filters": box},
        {"q": "worktree", "kind": "fact", "filters": box},
    ]
    listed = [
        {row["id"] for row in memory_edit.list_records(store, query, limit=100)["entries"]}
        for query in queries
    ]
    assembled = []
    real_record = memory_edit._record
    monkeypatch.setattr(
        memory_edit, "_record", lambda row: assembled.append(row) or real_record(row)
    )
    real_meta = memory_record_metadata.get_record_metadata
    monkeypatch.setattr(
        memory_record_metadata,
        "get_record_metadata",
        lambda *a, **k: assembled.append(a) or real_meta(*a, **k),
    )
    for query, ids in zip(queries, listed):
        lit, meaning = memory_edit.matching_scores(store, query)
        assert set(lit) == ids, query
        assert meaning is False
    assert assembled == []
    assert listed[3] == boxed and listed[4] == {"user.word"}
    assert len(listed[1]) == 1 and listed[2] == {"lesson.a"}
