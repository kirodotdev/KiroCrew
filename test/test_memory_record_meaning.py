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
