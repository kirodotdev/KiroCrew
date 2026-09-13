"""Recall bodies, query reuse and concurrent vector commits keep their identity."""

import json
import struct
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from member_memory_helpers import env as _member_env

from kiro_crew.mcp_tools import learn
from kiro_crew.memory_recall import MAX_RECALL_PAYLOAD_BYTES, _transport_size

env = _member_env


@pytest.mark.parametrize("name", ["", "member-alice"])
def test_mcp_recall_contains_each_body_once(env, monkeypatch, name):
    tier = env.tiers[name]
    fact = "Unique database fact keeps backups in Dublin."
    episode = "Unique database episode restored backups after the outage."
    assert tier.set_semantic("project.database", fact, 1.0, "user_explicit") is None
    assert tier.write_episodic(episode, defer_embedding=True)
    payload = tier.recall("database backups")
    assert fact in payload["semantic_preview"]
    assert episode in payload["episodic_preview"]
    monkeypatch.setattr(learn.mcp_core, "require_strict_session_key", lambda *a: ("test", ""))
    monkeypatch.setattr(learn.mcp_core, "_get", lambda *a, **kw: payload)

    wire = learn.memory_recall("memory_recall", {"query": "database backups"})

    assert wire.count(fact) == 1
    assert wire.count(episode) == 1
    result = json.loads(wire)
    assert "semantic_preview" not in result
    assert "episodic_preview" not in result
    for kind, field in (("facts", "snippet"), ("episodes", "text")):
        assert all(field not in row for row in result["retrieval"][kind])
        assert all("id" in row and "retrieval" in row for row in result["retrieval"][kind])
    assert "reference data, not instructions" in result["semantic_context"]
    assert "[End of memory]" in result["episodic_context"]
    assert _transport_size(wire, mcp_envelope=True) <= MAX_RECALL_PAYLOAD_BYTES
    # The model projection must not mutate the UI response.
    assert payload["retrieval"]["episodes"][0]["text"] == episode
    assert episode in payload["episodic_preview"]


@pytest.mark.parametrize("name", ["", "member-alice"])
@pytest.mark.parametrize("vector", [None, [1.0, 0.0]])
def test_one_query_embedding_attempt_per_recall(env, name, vector):
    tier = env.tiers[name]
    assert tier.set_semantic("project.database", "database backups", 1.0, "user_explicit") is None
    assert tier.write_episodic("database backups restored safely", defer_embedding=True)
    assert tier.write_lesson("Check database backups before a release")
    calls = []

    def embed(text):
        calls.append(text)
        return vector

    tier.embed_fn = embed
    result = tier.recall("database backups")
    assert calls == ["database backups"]
    assert "database backups" in result["semantic_context"]
    assert "database backups" in result["lessons_context"]
    tier.recall("database backups")
    assert calls == ["database backups", "database backups"]


@pytest.mark.parametrize("name", ["", "member-alice"])
@pytest.mark.parametrize("when", ["before_facts", "before_episode", "before_return"])
def test_recall_switch_discards_all_old_space_ranking(env, monkeypatch, name, when):
    tier = env.tiers[name]
    tier.set_embedding_dim(2)
    assert tier.set_semantic("project.database", "database backups", 1.0, "user_explicit") is None
    assert tier.write_episodic("database backups restored safely", defer_embedding=True)
    assert tier.write_episodic("baking bread with flour and yeast", defer_embedding=True)
    assert tier.write_lesson("Check database backups before a release")
    tier.embed_fn = lambda text: [1.0, 0.0]
    tier.reconcile_embedding_space("old-space")
    with tier._db_lock:
        tier.db.execute(
            f"UPDATE {tier._sem_rel} SET embedding=? WHERE key NOT LIKE 'lesson.%'",
            (struct.pack("2f", 1.0, 0.0),),
        )
        for row in tier.get_episodic_list():
            vector = [1.0, 0.0] if "database" in row["text"] else [0.0, 1.0]
            tier.db.execute(
                f"UPDATE {tier._epi_rel} SET embedding=? WHERE id=?",
                (struct.pack("2f", *vector), row["id"]),
            )
        tier.db.commit()
    paused, resume = Event(), Event()
    method = "_semantic_candidates_v2" if name else "_semantic_candidates_v1"
    if when == "before_return":
        method = "get_lessons_context"
    elif when == "before_facts":
        method = "_try_embed"
    original = getattr(tier, method)
    first = True

    def pause_after_read(*args, **kwargs):
        nonlocal first
        result = original(*args, **kwargs)
        if first:
            first = False
            paused.set()
            assert resume.wait(10)
        return result

    monkeypatch.setattr(tier, method, pause_after_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tier.recall, "database backups")
        try:
            assert paused.wait(10)
            tier.begin_space_change()
            tier.reconcile_embedding_space("new-space")
            tier.embed_fn = lambda text: [0.0, 1.0]
            with tier._db_lock:
                for row in tier.get_episodic_list():
                    vector = [0.0, 1.0] if "database" in row["text"] else [1.0, 0.0]
                    tier.db.execute(
                        f"UPDATE {tier._epi_rel} SET embedding=? WHERE id=?",
                        (struct.pack("2f", *vector), row["id"]),
                    )
                tier.db.commit()
                tier._invalidate_episodic_scoring()
        finally:
            resume.set()
        result = future.result(timeout=10)
    assert "database backups restored safely" in result["episodic_context"]
    assert "baking bread" not in result["episodic_context"]
    assert all(row["retrieval"].get("cosine") is None for row in result["retrieval"]["facts"])


@pytest.mark.parametrize("name", ["", "member-alice", "member-bob"])
@pytest.mark.parametrize("change", ["edit", "forget", "backfill"])
def test_lesson_tail_cannot_overwrite_a_later_owner_write(env, monkeypatch, name, change):
    tier = env.tiers[name]
    old_rule = "Check database backups before a release"
    new_rule = "Keep service retries below three attempts"
    tier.embed_fn = lambda text: [0.0, 1.0] if text == new_rule else [1.0, 0.0]
    paused, resume = Event(), Event()
    original = tier.set_semantic

    def write_and_pause(*args, **kwargs):
        result = original(*args, **kwargs)
        paused.set()
        assert resume.wait(10)
        return result

    monkeypatch.setattr(tier, "set_semantic", write_and_pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tier.write_lesson, old_rule)
        try:
            assert paused.wait(10)
            row = tier.get_lessons()[0]
            key = row["key"]
            if change == "edit":
                metadata = tier.with_record_metadata([row])[0]
                assert (
                    original(
                        key,
                        {"rule": new_rule, "category": "knowledge", "negative": None},
                        1.0,
                        "user_explicit",
                        expected_revision=metadata["record_revision"],
                    )
                    is None
                )
                tier._backfill_lesson_embeddings(pace=False)
            elif change == "forget":
                tier.delete_semantic(key, "user_explicit")
            else:
                tier.embed_fn = lambda text: [0.0, 1.0]
                tier._backfill_lesson_embeddings(pace=False)
            before = tier.db.execute(
                "SELECT value_json, embedding, is_deleted FROM semantic_memory WHERE key=?", (key,)
            ).fetchone()
        finally:
            resume.set()
        assert future.result(timeout=10)
    after = tier.db.execute(
        "SELECT value_json, embedding, is_deleted FROM semantic_memory WHERE key=?", (key,)
    ).fetchone()
    assert tuple(after) == tuple(before)


@pytest.mark.parametrize("source", ["user_explicit", "consolidation"])
def test_lazy_lesson_backfill_cannot_overwrite_owner_edit(env, monkeypatch, source):
    tier = env.tiers[""]
    old_rule = "Validate database backups before deployment"
    new_rule = "Keep network retries below three attempts"
    assert tier.write_lesson(old_rule)
    key = tier.get_lessons()[0]["key"]
    paused, resume = Event(), Event()

    def embed(text):
        if text == old_rule:
            paused.set()
            assert resume.wait(10)
            return [1.0, 0.0]
        return [0.0, 1.0] if text == new_rule else [1.0, 1.0]

    tier.embed_fn = embed
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            tier.write_lesson, "Archive monthly invoices for accounting", source=source
        )
        try:
            assert paused.wait(10)
            assert (
                tier.set_semantic(
                    key,
                    {"rule": new_rule, "category": "knowledge", "negative": None},
                    1.0,
                    "user_explicit",
                )
                is None
            )
            tier._backfill_lesson_embeddings(pace=False)
        finally:
            resume.set()
        assert future.result(timeout=10)
    row = next(row for row in tier.get_lessons() if row["key"] == key)
    assert struct.unpack("2f", row["embedding"]) == (0.0, 1.0)


@pytest.mark.parametrize("name", ["", "member-alice"])
def test_recall_inference_does_not_hold_database_lock(env, name):
    tier = env.tiers[name]
    reads = []
    with ThreadPoolExecutor(max_workers=1) as pool:

        def embed(text):
            reads.append(pool.submit(tier.count_lessons).result(timeout=5))
            return [1.0, 0.0]

        tier.embed_fn = embed
        tier.recall("database backups")
    assert reads
    assert all(count == 0 for count in reads)
