"""Private V2 relevance and recovery, with the global V1 contract held apart."""

from __future__ import annotations

import json
import math
import struct
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import memory_edit, memory_stores, memory_v2
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.history_consolidation import HistoryConsolidator
from kiro_crew.vector_memory import VectorMemoryStore


@pytest.fixture
def stores(tmp_path, monkeypatch):
    root = tmp_path / "memory_stores"
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: root)
    directory = declare_v2_store(tmp_path, "member-alice")
    v2 = VectorMemoryStore(db_path=directory / "memory.db", embedding_dim=2)
    v1 = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    for store in (v1, v2):
        store.init()
    try:
        yield v1, v2
    finally:
        for store in (v1, v2):
            store.close()


def episode(store, text, cosine=None, **kwargs):
    assert store.write_episodic(text, defer_embedding=True, **kwargs)
    row = next(r for r in store.get_episodic_list() if r["text"] == text)
    if cosine is not None:
        blob = struct.pack("2f", cosine, math.sqrt(1 - cosine**2))
        store.db.execute(
            f"UPDATE {store._epi_rel} SET embedding = ? WHERE id = ?", (blob, row["id"])
        )
        store.db.commit()
        store._invalidate_episodic_scoring()
    return row["id"]


def test_explicit_private_marker_enables_v2_without_changing_global(stores):
    v1, v2 = stores
    assert v1.algorithm_version == "v1"
    assert v2.algorithm_version == "v2"
    assert v2.policy_revision == "member-v2"
    assert v1._episodic_relevance_threshold("x" * 400) == 0.42
    assert v1._episodic_relevance_threshold("short") == 0.55
    assert v2._episodic_relevance_threshold("x" * 400) == 0.57
    assert v2._episodic_relevance_threshold("short") == 0.62
    assert v1.recall("deployment")["algorithm_version"] == "v1"


@pytest.mark.parametrize(
    "old_conf,new_conf,allowed",
    [(0.8, 0.9, True), (0.9, 0.85, True), (0.85, 0.85, True), (1, 0.8, False)],
)
def test_v1_confidence_conflicts_remain_separate_from_v2_proposals(
    stores, old_conf, new_conf, allowed
):
    v1, v2 = stores
    for store in stores:
        assert store.set_semantic("project.status", "old", old_conf, "consolidation:old") is None
    outcome = v1.set_semantic("project.status", "new", new_conf, "consolidation:new")
    assert (outcome is None) is allowed
    assert json.loads(v1.get_semantic("project.status")["value_json"]) == (
        "new" if allowed else "old"
    )
    assert v2.set_semantic("project.status", "new", new_conf, "consolidation:new") is not None
    assert json.loads(v2.get_semantic("project.status")["value_json"]) == "old"
    assert (
        v2.db.execute("SELECT COUNT(*) FROM memory_revisions WHERE status='conflict'").fetchone()[0]
        == 1
    )


def test_v1_reaffirmation_refreshes_source_while_v2_preserves_origin(stores):
    for store in stores:
        assert store.set_semantic("project.status", "active", 0.9, "consolidation:old") is None
        assert store.set_semantic("project.status", "active", 0.95, "consolidation:new") is None
    v1, v2 = stores
    assert v1.get_semantic("project.status")["source"] == "consolidation:new"
    assert v1.get_semantic("project.status")["confidence"] == 0.95
    assert v2.get_semantic("project.status")["source"] == "consolidation:old"


def test_v2_automatic_reextraction_keeps_forgotten_fact_hidden(stores):
    for store in stores:
        assert store.set_semantic("project.status", "old", 1, "user_explicit") is None
        assert store.delete_semantic("project.status", "user_explicit")
    v1, v2 = stores
    assert v1.set_semantic("project.status", "new", 0.9, "consolidation:new") is None
    assert json.loads(v1.get_semantic("project.status")["value_json"]) == "new"
    assert v2.set_semantic("project.status", "new", 0.9, "consolidation:new") is not None
    assert v2.get_semantic("project.status") is None
    assert (
        v2.db.execute("SELECT COUNT(*) FROM memory_revisions WHERE status='conflict'").fetchone()[0]
        == 1
    )


def test_consolidation_protects_owner_facts_and_keeps_v1_deletes_and_lesson_origin(stores):
    writer = object.__new__(HistoryConsolidator)
    for store in stores:
        assert store.set_semantic("project.status", "old", 1, "user_explicit") is None
        writer._write_structured_memory(
            {"semantic": [{"key": "project.status", "value": "new", "confidence": 1}]},
            "session",
            store,
        )
        assert json.loads(store.get_semantic("project.status")["value_json"]) == "old"
        assert store.get_semantic("project.status")["source"] == "user_explicit"
        writer._write_structured_memory(
            {"semantic": [{"key": "project.status", "delete": True}]},
            "session",
            store,
        )
        writer._save_lessons(
            [{"rule": "Check release notes before shipping", "category": "tool"}],
            store,
            lesson_store=None,
        )
    v1, v2 = stores
    assert v1.get_semantic("project.status") is None
    assert json.loads(v2.get_semantic("project.status")["value_json"]) == "old"
    v1_lesson = next(row for row in v1.get_all_semantic() if row["key"].startswith("lesson."))
    v2_lesson = next(row for row in v2.get_all_semantic() if row["key"].startswith("lesson."))
    assert (v1_lesson["source"], v1_lesson["confidence"]) == ("consolidation", 0.9)
    assert (v2_lesson["source"], v2_lesson["confidence"]) == ("consolidation", 0.9)


@pytest.mark.asyncio
async def test_v2_consolidation_instructions_do_not_change_v1_prompt(stores):
    prompts = []
    for store in stores:
        assert store.set_semantic("project.status", "active", 0.9, "consolidation:old") is None
        log = MagicMock()
        log.get_metadata_status.return_value = ({}, True)
        log.get_metadata.return_value = {}
        log.snapshot_for_consolidation.return_value = (
            [{"role": "user", "content": "Status is active"}],
            1,
            0,
        )
        log.consolidation_retry_state.return_value = (0, 0.0)
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        writer = HistoryConsolidator(log, memory, vector_store=store, migrated=True)
        writer._call_llm = AsyncMock(return_value=None)
        await writer._consolidate("session", include_history=False)
        prompts.append(writer._call_llm.call_args.args[0])
    assert "To DELETE a stale/invalidated key" in prompts[0]
    assert '"delete": false}. Rules:' in prompts[0]
    assert "record_revision" not in prompts[0]
    assert "correction_quote" not in prompts[0]
    assert "require owner review" not in prompts[0]
    assert "To propose removal" in prompts[1]
    assert "record_revision" in prompts[1]
    assert "correction_quote" in prompts[1]


@pytest.mark.parametrize("value", ["灯塔部署在法兰克福", {"location": "灯塔部署在法兰克福"}])
def test_semantic_keyword_recall_decodes_escaped_unicode_values(stores, value):
    _, private = stores
    assert private.set_semantic("project.deployment", value, 1.0, "user_explicit") is None
    assert private.set_semantic("project.travel", "周末去旅行", 1.0, "user_explicit") is None
    result = private.recall("灯塔部署")
    assert "灯塔部署" in result["semantic_context"]
    assert "project.travel" not in result["semantic_context"]
    evidence = result["retrieval"]["facts"][0]["retrieval"]
    assert evidence["reason"] == "keyword_match"
    assert "灯塔" in evidence["matched_terms"]


def test_unowned_named_schema_does_not_enable_new_algorithm(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path)
    store = VectorMemoryStore(db_path=tmp_path / "legacy" / "memory.db")
    store.init()
    try:
        assert store.algorithm_version == "v1"
        assert store._lineage == "crew"
        assert store.set_semantic("project.status", "old", 0.8, "consolidation:old") is None
        assert store.set_semantic("project.status", "new", 0.95, "consolidation:new") is None
        assert json.loads(store.get_semantic("project.status")["value_json"]) == "new"
    finally:
        store.close()


@pytest.mark.parametrize("length,cosine", [(100, 0.60), (400, 0.50)])
def test_v2_rejects_loose_v1_semantic_admissions(stores, length, cosine):
    v1, v2 = stores
    text = "The service account rotates credentials according to its policy. " + "x" * length
    for store in (v1, v2):
        episode(store, text, cosine)
    assert v1.search_episodic([1.0, 0.0], "password reset window", relevance_filter=True)
    assert v2.search_episodic([1.0, 0.0], "password reset window", relevance_filter=True) == []


def test_deferred_embedding_row_recalled_when_query_has_vector(stores):
    _, v2 = stores
    target = episode(v2, "Project lantern deploys PostgreSQL using the eu-west region.")
    episode(v2, "A holiday itinerary includes the mountain railway.", 0.1)
    result = v2.search_episodic([1.0, 0.0], "lantern PostgreSQL region", relevance_filter=True)
    assert [row["id"] for row in result] == [target]
    assert result[0]["retrieval"]["reason"] == "keyword_match"
    assert result[0]["retrieval"]["cosine"] is None


def test_vectorless_coverage_does_not_outrank_stronger_cosine():
    vectorless = memory_v2.rank_score({"cosine": None, "query_coverage": 0.925}, importance=1.0)
    embedded = memory_v2.rank_score({"cosine": 0.95, "query_coverage": 0.76}, importance=1.0)
    assert embedded > vectorless


@pytest.mark.parametrize(
    "query,text",
    [
        ("数据库备份", "项目数据库备份每天凌晨运行，保存七天。"),
        ("データベース", "プロジェクトのデータベースは毎日バックアップします。"),
        ("데이터베이스", "프로젝트 데이터베이스 백업을 매일 실행합니다."),
        (
            "réinitialisation mot passe",
            "La réinitialisation du mot de passe expire sous une heure.",
        ),
    ],
)
def test_multilingual_keyword_recovery_without_model(stores, query, text):
    _, v2 = stores
    target = episode(v2, text)
    assert [r["id"] for r in v2.search_episodic(query_text=query, relevance_filter=True)] == [
        target
    ]


def test_function_words_do_not_admit_unrelated_rows(stores):
    _, v2 = stores
    episode(v2, "The archive has the previous invoices in it.")
    assert (
        v2.search_episodic(query_text="What is the database region?", relevance_filter=True) == []
    )


def test_tag_filter_runs_before_top_k(stores):
    _, v2 = stores
    for index in range(10):
        episode(v2, f"Unrelated high similarity fragment number {index}.", 0.99, tags=["other"])
    wanted = episode(v2, "An older project fragment with the required tag.", 0.80, tags=["target"])
    results = v2.search_episodic([1.0, 0.0], limit=1, tag_filter=["target"], relevance_filter=True)
    assert [r["id"] for r in results] == [wanted]


def test_relevant_old_episode_is_not_erased_by_recency(stores):
    _, v2 = stores
    old = episode(v2, "Canonical project architecture records the failover design.", 0.82)
    recent = episode(v2, "Another project mentioned its delivery status yesterday.", 0.80)
    stamp = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    v2.db.execute("UPDATE memory_items SET created_at = ? WHERE id = ?", (stamp, old))
    v2.db.commit()
    assert [r["id"] for r in v2.search_episodic([1.0, 0.0], limit=2, mmr=False)] == [old, recent]


@pytest.mark.parametrize("decay_rate", [0.03, 10.0])
def test_v2_equal_relevance_has_equal_score_at_any_age(stores, decay_rate):
    v1, v2 = stores
    stamp = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    for store in (v1, v2):
        store._decay_by_tag = {"project": decay_rate}
        old = episode(
            store,
            "A project architecture decision from an earlier session.",
            0.8,
            importance=0.4,
            tags=["project"],
        )
        new = episode(
            store,
            "A project architecture decision from the current session.",
            0.8,
            importance=0.4,
            tags=["project"],
        )
        store.db.execute(f"UPDATE {store._epi_rel} SET created_at=? WHERE id=?", (stamp, old))
        store.db.commit()
        store._faiss_index = None
        store._invalidate_episodic_scoring()
        results = store.search_episodic([1.0, 0.0], "project architecture", limit=2, mmr=False)
        by_id = {row["id"]: row for row in results}
        if store.algorithm_version == "v2":
            assert by_id[old]["score"] == by_id[new]["score"]
            assert by_id[old]["retrieval"]["age_days"] >= 365
            assert by_id[new]["retrieval"]["age_days"] == 0
        else:
            assert by_id[old]["score"] < by_id[new]["score"]


def test_v2_capacity_never_discards_existing_memory_and_survives_restart(stores):
    _, v2 = stores
    v2._episodic_max = 2
    first = episode(
        v2, "Archive fact number zero preserves the original project decision.", importance=0
    )
    for index in range(1, 4):
        episode(
            v2, f"Archive fact number {index} preserves another project decision.", importance=1
        )
    assert v2.write_episodic(
        "Archive fact selected explicitly from another memory store.",
        defer_embedding=True,
        preserve_existing=True,
    )
    ids = {row["id"] for row in v2.get_episodic_list()}
    assert len(ids) == 5 and first in ids
    v2._enforce_episodic_cap()
    v2.close()
    v2.init()
    assert {row["id"] for row in v2.get_episodic_list()} == ids
    assert (
        v2.db.execute("SELECT COUNT(*) FROM memory_revisions WHERE source='capacity'").fetchone()[0]
        == 0
    )
    assert v2.delete_episodic(first)
    assert first not in {row["id"] for row in v2.get_episodic_list()}


def test_v1_capacity_keeps_its_eviction_and_preserve_existing_refusal(stores):
    v1, _ = stores
    v1._episodic_max = 2
    first = episode(
        v1, "Archive fact number zero is the lowest importance project decision.", importance=0
    )
    second = episode(
        v1, "Archive fact number one is a high importance project decision.", importance=1
    )
    third = episode(
        v1, "Archive fact number two is another high importance project decision.", importance=1
    )
    assert {row["id"] for row in v1.get_episodic_list()} == {second, third}
    assert first not in {row["id"] for row in v1.get_episodic_list()}
    assert not v1.write_episodic(
        "Archive fact selected explicitly cannot evict existing V1 content.",
        defer_embedding=True,
        preserve_existing=True,
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("pref.color: red.", True),
        ("pref.color is red.", True),
        ("An update was recorded. pref.color = red.", True),
        ("The deployment color: red.", False),
        ("The deployment color is red.", False),
        ("Bob's favorite color is red and should stay in his profile.", False),
        ("Bob's pref.color: red.", False),
        ("project.color: red.", False),
        ("The deployment color: redwood.", False),
        ("The color is blue and the alert was red.", False),
        ("The color is not red.", False),
        ("Previously the color was red.", False),
        ("The color is blue. The report is red.", False),
    ],
)
def test_supersession_requires_a_current_literal_assertion(text, expected):
    assert memory_v2.superseded_value_is_asserted(text, "pref.color", "red") is expected


def test_v2_retirement_is_bounded_and_recoverable(stores):
    _, v2 = stores
    for index in range(5):
        episode(v2, f"pref.color: red. Deployment record {index}.")
    preserved = episode(v2, "The deployment color is blue and the report is red.")
    v2._retire_stale_episodic("pref.color", "red")
    retired = v2.get_retired_episodic()
    assert len(retired) == 3
    assert all(row["superseded_by"] == "pref.color" for row in retired)
    assert preserved in {r["id"] for r in v2.get_episodic_list()}
    assert v2.restore_episodic(retired[0]["id"])
    assert len(v2.get_retired_episodic()) == 2


def test_owner_correction_preserves_other_subjects_and_ambiguous_prose(stores):
    _, v2 = stores
    bob = episode(v2, "Bob's favorite color is red and should stay in his profile.")
    project = episode(v2, "project.color: red is the official project branding.")
    ambiguous = episode(v2, "The color is red for the theme.")
    linked = episode(v2, "pref.color: red is the owner's selected theme.")
    v2.set_semantic("pref.color", "red", 1.0, "user_explicit")
    v2.set_semantic("pref.color", "blue", 1.0, "user_explicit")
    assert {row["id"] for row in v2.get_retired_episodic()} == {linked}
    assert {row["id"] for row in v2.get_episodic_list()} == {bob, project, ambiguous}


@pytest.mark.parametrize("accelerator", ["numpy", "faiss"])
@pytest.mark.parametrize("version", [0, 1], ids=["v1", "v2"])
def test_restored_episode_returns_to_warm_recall(stores, monkeypatch, accelerator, version):
    from kiro_crew import vector_memory

    if accelerator == "faiss" and not vector_memory._HAS_FAISS:
        pytest.skip("FAISS is not installed")
    if accelerator == "numpy" and not vector_memory._HAS_NUMPY:
        pytest.skip("NumPy is not installed")
    if accelerator == "numpy":
        monkeypatch.setattr(vector_memory, "_HAS_FAISS", False)
    store = stores[version]
    restored = episode(store, "The color preference was recorded for the owner's theme.", 1.0)
    survivor = episode(store, "Sailing boat dock marker records an unrelated excursion.", 0.8)
    assert store.delete_episodic(restored)
    if accelerator == "faiss":
        assert store.build_faiss_index() == 1
    before = store.search_episodic([1.0, 0.0], limit=8, mmr=False)
    assert [row["id"] for row in before] == [survivor]
    assert store.restore_episodic(restored)
    after = store.search_episodic([1.0, 0.0], limit=8, mmr=False)
    assert {row["id"] for row in after} == {restored, survivor}


def test_v1_retirement_keeps_pre_feature_unbounded_contract(stores):
    v1, _ = stores
    for index in range(5):
        episode(v1, f"Deployment {index}: color: red.")
    v1._retire_stale_episodic("pref.color", "red")
    assert v1.get_episodic_list() == []
    events = [e for e in v1.get_events() if e["event_type"] == "conflict_retire"]
    assert len(events) == 5
    assert all(e["new_value"] is None for e in events)


def test_reaffirming_a_v2_fact_does_not_retire_its_evidence(stores):
    _, v2 = stores
    assert v2.set_semantic("pref.color", "red", 1.0, "user_explicit") is None
    target = episode(v2, "The selected color: red for deployment.")
    assert v2.set_semantic("pref.color", "red", 1.0, "user_explicit") is None
    assert target in {r["id"] for r in v2.get_episodic_list()}
    assert v2.get_retired_episodic() == []


@pytest.mark.parametrize(
    "first,second",
    [
        (
            "Use database migrations for schema changes in production.",
            "Review database migrations for schema changes before deployment.",
        ),
        (
            "Zebra crossings need beacons",
            "Submarine hatches demand orange lanterns for visibility",
        ),
    ],
    ids=["topic-overlap", "semantic-only"],
)
def test_v2_lessons_keep_distinct_rules_despite_similarity_and_source(stores, first, second):
    _, v2 = stores
    v2.embed_fn = lambda _text: [1.0, 0.0]
    assert v2.write_lesson(first, source="user_explicit")
    assert v2.write_lesson(second, source="consolidation")
    lessons = [json.loads(r["value_json"])["rule"] for r in v2.get_lessons()]
    assert set(lessons) == {first, second}
    assert not any(e["event_type"] == "delete" for e in v2.get_events())


def test_v2_shared_episode_prefix_does_not_discard_new_information(stores):
    v1, v2 = stores
    prefix = "The rollout requires checking every deployment in each region before proceeding. "
    first = prefix + "Production uses PostgreSQL."
    second = prefix + "Staging uses SQLite."
    for store in (v1, v2):
        assert store.write_episodic(first, defer_embedding=True)
    assert not v1.write_episodic(second, defer_embedding=True)
    assert v2.write_episodic(second, defer_embedding=True)
    assert not v2.write_episodic(second, defer_embedding=True)


def test_explicit_seed_is_independent_traceable_and_never_overwrites(stores):
    v1, v2 = stores
    assert v1.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit") is None
    source = dict(v1.get_semantic("project.database"))
    result = v2.seed_item_if_absent(
        source, source_store="default", source_id="project.database", kind="fact"
    )
    assert result["outcome"] == "imported"
    row = v2.list_by_facets(kind="fact")[0]
    assert json.loads(row["derived_from"])["store"] == "default"
    assert v2.set_semantic("project.database", "SQLite", 1.0, "user_explicit") is None
    assert (
        v2.seed_item_if_absent(
            source, source_store="default", source_id="project.database", kind="fact"
        )["outcome"]
        == "existing"
    )
    assert json.loads(v2.get_semantic("project.database")["value_json"]) == "SQLite"
    assert json.loads(v1.get_semantic("project.database")["value_json"]) == "PostgreSQL"


def test_episode_seed_copies_provenance_without_source_vector(stores):
    _, v2 = stores
    result = v2.seed_item_if_absent(
        {
            "text": "The initial database deployment uses Postgres.",
            "tags": "[]",
            "source": "consolidation",
        },
        source_store="default",
        source_id="episode-source",
        kind="episode",
    )
    assert result["outcome"] == "imported"
    row = v2.list_by_facets(kind="episode")[0]
    assert json.loads(row["derived_from"])["item_id"] == "episode-source"
    assert row["source"] == "user_seed"


def test_recall_fits_budget_and_reports_only_selected_evidence(stores):
    _, v2 = stores
    assert v2.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit") is None
    assert v2.set_semantic("project.holiday", "Alpine railway", 1.0, "user_explicit") is None
    target = episode(v2, "The PostgreSQL database runs in the western region.")
    result = v2.recall("PostgreSQL database", cap=500)
    assert result["total_chars"] <= 500
    assert [r["key"] for r in result["retrieval"]["facts"]] == ["project.database"]
    assert [r["id"] for r in result["retrieval"]["episodes"]] == [target]
    assert target in result["episodic_context"]
    assert "Alpine" not in result["semantic_context"]
    assert v2.recall("", cap=500)["total_chars"] == 0


def test_v2_recall_truncates_one_admitted_long_fact_instead_of_dropping_it(stores):
    _, v2 = stores
    value = "database migration evidence " + "x" * 1600
    assert v2.set_semantic("project.database_runbook", value, 1.0, "user_explicit") is None

    result = v2.recall("database migration", cap=3000)

    assert result["semantic_context"]
    assert "project.database_runbook" in result["semantic_context"]
    assert result["total_chars"] <= 3000
    assert result["retrieval"]["facts"][0]["id"] == "key:project.database_runbook"
    assert result["retrieval"]["facts"][0]["snippet_truncated"] is True


@pytest.mark.parametrize("cap", [0, 1, 31])
def test_v2_recall_tiny_caps_do_not_emit_partial_unlocatable_evidence(stores, cap):
    _, v2 = stores
    assert (
        v2.set_semantic("project.database_runbook", "database " * 200, 1.0, "user_explicit") is None
    )

    result = v2.recall("database", cap=cap)

    assert result["total_chars"] <= cap
    assert result["semantic_context"] == ""
    assert result["retrieval"]["facts"] == []


def test_oversized_fact_does_not_hide_short_relevant_fact(stores):
    _, v2 = stores
    v2.set_semantic("project.database_details", "database " * 100, 1.0, "user_explicit")
    v2.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    assert "project.database: PostgreSQL" in v2.get_semantic_context("database", cap=80)


@pytest.mark.parametrize("private", [False, True])
def test_episode_bulk_correction_clears_stale_vector_and_rolls_back_failed_audit(stores, private):
    store = stores[1 if private else 0]
    name = "member-alice" if private else ""
    secret = b"test-preview-key"
    original_text = "Postgres listens on port 5432 locally"
    mem_id = episode(store, original_text, cosine=0.95)
    row = memory_edit.list_records(store, {"kind": "episode"})["entries"][0]
    body = {
        "selection": {"items": [{key: row[key] for key in ("kind", "id", "revision")}]},
        "operation": {"type": "set", "text": original_text},
    }
    preview = memory_edit.preview_edit(store, name, secret, body)
    assert memory_edit.apply_edit(store, name, secret, preview["preview_id"])["changed_count"] == 0
    original_vector = store.db.execute(
        "SELECT embedding FROM episodic_memories WHERE id=?", (mem_id,)
    ).fetchone()[0]
    assert original_vector is not None
    body["operation"]["text"] = "Postgres listens on port 6432 locally"
    preview = memory_edit.preview_edit(store, name, secret, body)
    store.db.execute(
        "CREATE TRIGGER reject_correction BEFORE INSERT ON memory_events WHEN NEW.event_type='correct' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
    )
    with pytest.raises(sqlite3.Error, match="audit unavailable"):
        memory_edit.apply_edit(store, name, secret, preview["preview_id"])
    assert memory_edit.list_records(store, {"kind": "episode"})["entries"][0] == row
    assert (
        store.db.execute(
            "SELECT embedding FROM episodic_memories WHERE id=?", (mem_id,)
        ).fetchone()[0]
        == original_vector
    )
    store.db.execute("DROP TRIGGER reject_correction")
    assert memory_edit.apply_edit(store, name, secret, preview["preview_id"])["changed_count"] == 1
    assert (
        store.db.execute(
            "SELECT embedding FROM episodic_memories WHERE id=?", (mem_id,)
        ).fetchone()[0]
        is None
    )
    assert (
        store.search_episodic(query_text="Postgres port 6432", relevance_filter=True)[0]["id"]
        == mem_id
    )
