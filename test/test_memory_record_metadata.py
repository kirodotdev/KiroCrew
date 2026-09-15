"""Behavioral compatibility, correction evidence and temporal quality fixtures."""

import json
import struct
from datetime import datetime, timezone

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import memory_record_metadata as meta
from kiro_crew import memory_stores, vector_memory
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.history_consolidation import HistoryConsolidator
from kiro_crew.vector_memory import VectorMemoryStore


@pytest.fixture(params=["v1", "v2"])
def store(request, tmp_path, monkeypatch):
    root = tmp_path / "memory_stores"
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: root)
    directory = tmp_path if request.param == "v1" else declare_v2_store(tmp_path, "member-alice")
    tier = VectorMemoryStore(db_path=directory / "memory.db", embedding_dim=2)
    tier.init()
    yield tier
    tier.close()


def fact(store, value="old@example.com", **kwargs):
    assert store.set_semantic("user.work_email", value, 1, "user_explicit", **kwargs) is None
    return store.with_record_metadata([store.get_semantic("user.work_email")])[0]


def test_transaction_rollback_preserves_record_and_full_history(store):
    before = fact(store)
    prior = meta.get_record_metadata(store.db, "key:user.work_email")
    assert prior["email_addresses"] == ["old@example.com"]
    store.db.execute("BEGIN IMMEDIATE")
    store.db.execute(
        f"UPDATE {store._sem_rel} SET value_json=? WHERE key=?",
        (json.dumps("new@example.com"), "user.work_email"),
    )
    after = dict(store.db.execute("SELECT * FROM semantic_memory").fetchone())
    result = meta.sync_record(
        store.db, kind="fact", record_id="user.work_email", before=before, after=after
    )
    assert result["revision"] == prior["revision"] + 1
    assert store.db.in_transaction
    store.db.rollback()
    assert meta.get_record_metadata(store.db, "key:user.work_email") == prior
    assert json.loads(store.get_semantic("user.work_email")["value_json"]) == "old@example.com"


def test_identical_semantic_rewrite_keeps_one_content_revision(store, monkeypatch):
    now = ["2026-09-09T10:00:00+00:00"]
    monkeypatch.setattr(vector_memory, "_now_iso", lambda: now[0])
    fact(store)
    physical_before = dict(store.db.execute("SELECT * FROM semantic_memory").fetchone())
    metadata_before = meta.get_record_metadata(store.db, "key:user.work_email")
    history_before = store.db.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]
    recorded = json.loads(store.db.execute("SELECT after_json FROM memory_revisions").fetchone()[0])
    assert recorded["created_at"] == physical_before["created_at"]
    assert recorded["updated_at"] == physical_before["updated_at"]

    now[0] = "2026-09-09T10:01:00+00:00"
    fact(store)

    physical_after = dict(store.db.execute("SELECT * FROM semantic_memory").fetchone())
    metadata_after = meta.get_record_metadata(store.db, "key:user.work_email")
    assert physical_after["updated_at"] != physical_before["updated_at"]
    assert physical_after["created_at"] == physical_before["created_at"]
    assert metadata_after["revision"] == metadata_before["revision"] == 1
    assert metadata_after["content_hash"] == metadata_before["content_hash"]
    assert store.db.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == history_before


def test_metadata_backfill_and_older_writer_reconciliation(store):
    fact(store)
    versions = list(store.db.execute("SELECT version FROM schema_version"))
    # An older binary only knows the original relation. Physical shape stays valid.
    store.db.execute(
        f"UPDATE {store._sem_rel} SET value_json=? WHERE key=?",
        (json.dumps("legacy@example.net"), "user.work_email"),
    )
    store.db.commit()
    store.close()
    store.init()
    current = meta.get_record_metadata(store.db, "key:user.work_email")
    assert current["revision"] == 2
    assert current["email_addresses"] == ["legacy@example.net"]
    revision = dict(
        store.db.execute("SELECT * FROM memory_revisions ORDER BY id DESC LIMIT 1").fetchone()
    )
    assert revision["source"] == "legacy_untracked"
    assert "old@example.com" in revision["before_json"]
    assert list(store.db.execute("SELECT version FROM schema_version")) == versions
    store.close()
    store.init()
    assert meta.get_record_metadata(store.db, "key:user.work_email")["revision"] == 2


def test_exact_identity_reuses_key_without_cross_entity_merge(store):
    identity = {"subject": " Alice ", "predicate": "Work_Email", "scope": "ACME"}
    fact(store, metadata=identity)
    assert (
        store.set_semantic(
            "user.work_contact", "new@example.com", 1, "user_explicit", metadata=identity
        )
        is None
    )
    assert len(store.get_all_semantic()) == 1
    assert json.loads(store.get_semantic("user.work_email")["value_json"]) == "new@example.com"
    assert (
        store.set_semantic(
            "user.bob_email",
            "bob@example.com",
            1,
            "user_explicit",
            metadata={**identity, "subject": "Bob"},
        )
        is None
    )
    assert len(store.get_all_semantic()) == 2


def test_explicit_identity_terms_improve_sparse_key_lookup_without_embeddings(store):
    assert store.set_semantic("user.route_42", "accounts@example.com", 1, "user_explicit") is None
    assert not store.recall("Alice billing")["retrieval"]["facts"]
    assert (
        store.set_semantic(
            "user.route_42",
            "accounts@example.com",
            1,
            "user_explicit",
            metadata={"subject": "Alice", "predicate": "billing_email"},
        )
        is None
    )
    recall = store.recall("Alice billing")
    assert [row["key"] for row in recall["retrieval"]["facts"]] == ["user.route_42"]
    assert "alice, billing_email" in recall["semantic_context"]
    assert store.embed_fn is None


def test_scope_preserves_case_and_unicode_identity_does_not_guess_equivalence(store):
    for index, scope in enumerate(("/Project/Foo", "/Project/foo", "/Project/项目")):
        assert (
            store.set_semantic(
                f"project.address_{index}",
                f"team{index}@example.com",
                1,
                "user_explicit",
                metadata={"subject": "Ａlice", "predicate": "Email", "scope": scope},
            )
            is None
        )
    assert len(store.get_all_semantic()) == 3


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_automated_value_and_metadata_conflicts_preserve_owner_and_deduplicate(store):
    fact(store)
    for _ in range(2):
        assert (
            store.set_semantic("user.work_email", "other@example.com", 1, "consolidation:dm")
            is not None
        )
    assert (
        store.set_semantic(
            "user.work_email",
            "old@example.com",
            1,
            "consolidation:dm",
            metadata={"status": "expired"},
        )
        is not None
    )
    assert meta.get_record_metadata(store.db, "key:user.work_email")["status"] == "active"
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM memory_revisions WHERE status='conflict'"
        ).fetchone()[0]
        == 2
    )
    assert store.propose_semantic_delete("user.work_email", "consolidation:dm")
    assert store.get_semantic("user.work_email") is not None


def correction(before, *, role="user", quote=None):
    quote = quote or "Please replace old@example.com with new@example.com."
    return meta.verified_correction(
        key="user.work_email",
        before=before,
        value="new@example.com",
        quote=quote,
        messages=[{"role": role, "content": quote, "ts": datetime.now(timezone.utc).isoformat()}],
        session_key="dashboard:alice",
    )


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_verified_current_transcript_correction_updates_outdated_fact(store):
    before = fact(store)
    evidence = correction(before)
    assert evidence is not None
    assert (
        store.set_semantic(
            "user.work_email",
            "new@example.com",
            1,
            "consolidation:dashboard:alice",
            correction=evidence,
            expected_revision=evidence.revision,
        )
        is None
    )
    current = store.get_semantic("user.work_email")
    assert json.loads(current["value_json"]) == "new@example.com"
    assert current["source"] == "consolidation:dashboard:alice"
    metadata = meta.get_record_metadata(store.db, "key:user.work_email")
    assert "#message:0:" in metadata["source_ref"]
    assert metadata["revision"] == 2
    history = store.db.execute(
        "SELECT before_json FROM memory_revisions ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert "user_explicit" in history and "old@example.com" in history


def test_fabricated_assistant_ambiguous_or_stale_corrections_cannot_replace(store):
    before = fact(store)
    assert correction(before, role="assistant") is None
    assert (
        correction(before, quote="old@example.com and new@example.com may be alternatives") is None
    )
    evidence = correction(before)
    fact(store, "third@example.com")
    assert (
        store.set_semantic(
            "user.work_email",
            "new@example.com",
            1,
            "consolidation:dm",
            correction=evidence,
            expected_revision=before["record_revision"],
        )
        is not None
    )
    assert json.loads(store.get_semantic("user.work_email")["value_json"]) == "third@example.com"


def test_temporal_rows_do_not_crowd_current_fact_or_episode_context(store):
    # This fixture tests validity, independently of optional V1 cosine dedup.
    store._faiss_index = None
    for index, metadata in enumerate(
        ({}, {"valid_until": "2000-01-01"}, {"valid_from": "2100-01-01"})
    ):
        assert (
            store.set_semantic(
                f"project.release_{index}",
                f"release schedule {index}",
                1,
                "user_explicit",
                metadata=metadata,
            )
            is None
        )
        assert store.write_episodic(
            f"Release schedule plan is number {index} for this project.",
            embedding=[1, 0],
            metadata=metadata,
        )
    store.embed_fn = lambda _query: [1, 0]
    recall = store.recall("release schedule", cap=2000)
    assert [row["key"] for row in recall["retrieval"]["facts"]] == ["project.release_0"]
    assert len(recall["retrieval"]["episodes"]) == 1
    assert recall["total_chars"] <= 2000
    # Owner management retains future/past records; eligibility is retrieval-only.
    assert len(store.get_all_semantic()) == 3


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_consolidator_confidence_is_not_authority_and_bad_item_is_local(store):
    fact(store)
    writer = object.__new__(HistoryConsolidator)
    writer._write_structured_memory(
        {
            "semantic": [
                {"key": "user.work_email", "value": "attacker@example.com", "confidence": 1},
                {"key": "user.work_email", "delete": True},
                {"key": "project.bad", "value": "invalid", "confidence": "NaN"},
                {"key": "project.valid", "value": "retained", "confidence": 0.9},
            ]
        },
        "dashboard:alice",
        store,
    )
    assert json.loads(store.get_semantic("user.work_email")["value_json"]) == "old@example.com"
    assert store.get_semantic("project.valid")["source"] == "consolidation:dashboard:alice"
    assert store.get_semantic("project.bad") is None


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_consolidator_creates_new_fact_with_metadata_without_orphan_proposal(store):
    writer = object.__new__(HistoryConsolidator)
    writer._write_structured_memory(
        {
            "semantic": [
                {
                    "key": "user.new_contact",
                    "value": "new@example.com",
                    "confidence": 0.9,
                    "metadata": {
                        "category": "contact",
                        "subject": "alice",
                        "predicate": "email",
                        "scope": "project:/Foo",
                    },
                }
            ]
        },
        "dashboard:alice",
        store,
    )
    row = store.get_semantic("user.new_contact")
    assert json.loads(row["value_json"]) == "new@example.com"
    assert row["source"] == "consolidation:dashboard:alice"
    metadata = meta.get_record_metadata(store.db, "key:user.new_contact")
    assert metadata["revision"] == 1
    assert metadata["category"] == "contact"
    assert metadata["scope"] == "project:/Foo"
    revisions = list(store.db.execute("SELECT status, operation FROM memory_revisions"))
    assert [tuple(row) for row in revisions] == [("accepted", "create")]


def test_failed_import_audit_rolls_back_record_and_history_and_allows_retry(store, monkeypatch):
    key = "user.work_email"
    original = store._record_mutation
    history_before = store.db.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]

    def fail_after_audit(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("audit write failed")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_record_mutation", fail_after_audit)
        with pytest.raises(sqlite3.OperationalError, match="audit write failed"):
            store.set_semantic_if_absent(key, "first@example.com", 1.0, "import")

    assert not store.db.in_transaction
    assert store.get_semantic(key) is None
    assert meta.get_record_metadata(store.db, f"key:{key}") == {}
    assert (
        store.db.execute(
            "SELECT 1 FROM memory_record_meta WHERE record_id=?", (f"key:{key}",)
        ).fetchone()
        is None
    )
    assert store.db.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == history_before
    # An unrelated writer must be able to start its own transaction immediately.
    store.db.execute("BEGIN IMMEDIATE")
    store.db.commit()
    assert store.set_semantic_if_absent(key, "retry@example.com", 1.0, "import") == "imported"
    assert json.loads(store.get_semantic(key)["value_json"]) == "retry@example.com"
    assert meta.get_record_metadata(store.db, f"key:{key}")["revision"] == 1
    assert (
        store.db.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]
        == history_before + 1
    )


def test_metadata_invalid_interval_rolls_back_physical_write(store):
    assert (
        store.set_semantic(
            "project.invalid",
            "must not persist",
            1,
            "user_explicit",
            metadata={"valid_from": "2030-01-01", "valid_until": "2020-01-01"},
        )
        is not None
    )
    assert store.get_semantic("project.invalid") is None
    assert not store.db.in_transaction


def test_explicit_inactive_status_is_a_physical_tombstone_for_old_readers(store):
    assert (
        store.set_semantic(
            "project.obsolete", "old contact", 1, "user_explicit", metadata={"status": "expired"}
        )
        is None
    )
    assert store.write_episodic(
        "This obsolete contact was previously recorded.", metadata={"status": "expired"}
    )
    assert store.db.execute("SELECT is_deleted FROM semantic_memory").fetchone()[0] == 1
    assert store.db.execute("SELECT is_deleted FROM episodic_memories").fetchone()[0] == 1


def test_ensure_schema_does_not_commit_callers_transaction():
    with sqlite3.connect(":memory:") as db:
        db.execute("BEGIN")
        meta.ensure_schema(db)
        assert db.in_transaction
        db.rollback()
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='memory_record_meta'"  # wokeignore:rule=master
        ).fetchone()


@pytest.mark.parametrize("case", ["ambiguous", "explicit", "stale", "assistant_quote"])
@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_controlled_update_cases_require_current_user_evidence(store, case):
    """Only a current user correction authorizes replacing the accepted fact."""
    before = fact(store)
    quote = "Please replace old@example.com with new@example.com."
    message = {"role": "user", "content": quote, "ts": datetime.now(timezone.utc).isoformat()}
    item = {"key": "user.work_email", "value": "new@example.com", "confidence": 1}
    if case != "ambiguous":
        item["correction_quote"] = quote
    if case == "assistant_quote":
        message["role"] = "assistant"
    if case == "stale":
        fact(store, "third@example.com")
    expected = {"explicit": "new@example.com", "stale": "third@example.com"}.get(
        case, "old@example.com"
    )
    writer = object.__new__(HistoryConsolidator)
    writer._write_structured_memory(
        {"semantic": [item]},
        "dashboard:alice",
        store,
        snapshot={"user.work_email": before},
        messages=[message],
    )
    assert json.loads(store.get_semantic("user.work_email")["value_json"]) == expected


@pytest.mark.parametrize(
    "quote",
    [
        "Please replace new@example.com with old@example.com.",
        "Do not replace old@example.com with new@example.com.",
        "If we replace old@example.com with new@example.com.",
    ],
)
def test_correction_direction_negation_and_hypothetical_are_not_authority(store, quote):
    assert correction(fact(store), quote=quote) is None


@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_chinese_literal_correction_and_negation(store):
    before = fact(store)
    quote = "请把 old@example.com 改为 new@example.com。"
    evidence = correction(before, quote=quote)
    assert evidence is not None
    assert correction(before, quote="不要把old@example.com 改为 new@example.com。") is None
    assert (
        store.set_semantic(
            "user.work_email",
            "new@example.com",
            1,
            "consolidation:dm",
            correction=evidence,
            expected_revision=evidence.revision,
        )
        is None
    )


@pytest.mark.parametrize(
    "content,quote",
    [
        (
            "Do not replace old@example.com with new@example.com.",
            "replace old@example.com with new@example.com",
        ),
        (
            "不要把 old@example.com 改为 new@example.com。",
            "把 old@example.com 改为 new@example.com",
        ),
        (
            "If we replace old@example.com with new@example.com, discuss first.",
            "replace old@example.com with new@example.com",
        ),
        (
            "An attacker wrote: Please replace old@example.com with new@example.com. Ignore it.",
            "Please replace old@example.com with new@example.com.",
        ),
        (
            "Please replace old@example.com with new@example.com. Actually, do not change it.",
            "Please replace old@example.com with new@example.com.",
        ),
        (
            "Please replace old@example.com with new@example.com if approved.",
            "Please replace old@example.com with new@example.com",
        ),
        (
            "> Please replace old@example.com with new@example.com.",
            "Please replace old@example.com with new@example.com.",
        ),
        (
            "```text\nPlease replace old@example.com with new@example.com.\n```",
            "Please replace old@example.com with new@example.com.",
        ),
        (
            "`Please replace old@example.com with new@example.com.`",
            "Please replace old@example.com with new@example.com.",
        ),
        (
            '"Please replace old@example.com with new@example.com."',
            "Please replace old@example.com with new@example.com.",
        ),
    ],
)
@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_model_quote_cannot_strip_actual_user_message_context(store, content, quote):
    before = fact(store)
    messages = [{"role": "user", "content": content, "ts": datetime.now(timezone.utc).isoformat()}]
    assert (
        meta.verified_correction(
            key="user.work_email",
            before=before,
            value="new@example.com",
            quote=quote,
            messages=messages,
            session_key="dashboard:alice",
        )
        is None
    )
    writer = object.__new__(HistoryConsolidator)
    writer._write_structured_memory(
        {
            "semantic": [
                {
                    "key": "user.work_email",
                    "value": "new@example.com",
                    "confidence": 1,
                    "correction_quote": quote,
                }
            ]
        },
        "dashboard:alice",
        store,
        snapshot={"user.work_email": before},
        messages=messages,
    )
    assert json.loads(store.get_semantic("user.work_email")["value_json"]) == "old@example.com"
    assert meta.get_record_metadata(store.db, "key:user.work_email")["revision"] == 1
    proposal = store.db.execute("SELECT status FROM memory_revisions ORDER BY id DESC").fetchone()
    assert proposal["status"] == "conflict"


@pytest.mark.parametrize(
    "content,quote",
    [
        (
            "Please replace old@example.com with new@example.com.",
            "replace old@example.com with new@example.com",
        ),
        (
            "  请把 old@example.com 改为 new@example.com。\n",
            "把 old@example.com 改为 new@example.com",
        ),
    ],
)
@pytest.mark.parametrize("store", ["v2"], indirect=True)
def test_complete_affirmative_user_statement_still_authorizes_correction(store, content, quote):
    before = fact(store)
    writer = object.__new__(HistoryConsolidator)
    writer._write_structured_memory(
        {
            "semantic": [
                {
                    "key": "user.work_email",
                    "value": "new@example.com",
                    "confidence": 0.9,
                    "correction_quote": quote,
                }
            ]
        },
        "dashboard:alice",
        store,
        snapshot={"user.work_email": before},
        messages=[
            {"role": "user", "content": content, "ts": datetime.now(timezone.utc).isoformat()}
        ],
    )
    assert json.loads(store.get_semantic("user.work_email")["value_json"]) == "new@example.com"
    metadata = meta.get_record_metadata(store.db, "key:user.work_email")
    assert metadata["revision"] == 2
    assert metadata["source_ref"].endswith(content.strip())


@pytest.mark.parametrize(
    "later_content",
    [
        "Actually, do not change my email.",
        "先不要改我的邮箱。",
        "Let's discuss the new project first.",
        "[Cron notification from reminder] keep the project running",
    ],
)
def test_newer_user_context_keeps_earlier_correction_pending(store, later_content):
    before = fact(store)
    quote = "Please replace old@example.com with new@example.com."
    now = datetime.now(timezone.utc).isoformat()
    messages = [
        {"role": "user", "content": quote, "ts": now},
        {"role": "assistant", "content": "Understood.", "ts": now},
        {"role": "user", "content": later_content, "ts": now},
    ]
    assert (
        meta.verified_correction(
            key="user.work_email",
            before=before,
            value="new@example.com",
            quote=quote,
            messages=messages,
            session_key="dashboard:alice",
        )
        is None
    )
    # An assistant acknowledgment alone does not revoke the last user's command.
    evidence = meta.verified_correction(
        key="user.work_email",
        before=before,
        value="new@example.com",
        quote=quote,
        messages=messages[:-1],
        session_key="dashboard:alice",
    )
    assert evidence is not None
    assert "#message:0:" in evidence.source_ref


def test_true_but_older_user_quote_cannot_override_a_newer_owner_fact(store):
    before = fact(store)
    quote = "Please replace old@example.com with new@example.com."
    assert (
        meta.verified_correction(
            key="user.work_email",
            before=before,
            value="new@example.com",
            quote=quote,
            messages=[{"role": "user", "content": quote, "ts": "2000-01-01T00:00:00Z"}],
            session_key="dashboard:alice",
        )
        is None
    )


def test_outbound_redaction_discovers_both_extension_tables(store, tmp_path):
    from kiro_crew import snapshot_redact

    secret = "ghp_" + "A" * 36
    fact(store)
    # Historical/proposed and metadata strings must be scanned even when current
    # live content is clean. The generic schema walker needs no table allowlist.
    store.db.execute("UPDATE memory_revisions SET before_json=?", (json.dumps({"note": secret}),))
    store.db.execute("UPDATE memory_record_meta SET source_ref=?", (secret,))
    store.db.commit()
    target = tmp_path / "redacted.db"
    with sqlite3.connect(target) as copied:
        store.db.backup(copied)
    report = snapshot_redact.RedactionReport()
    snapshot_redact._redact_database(target, report, "memory.db", product=True)
    with sqlite3.connect(target) as copied:
        assert (
            secret
            not in copied.execute("SELECT before_json FROM memory_revisions LIMIT 1").fetchone()[0]
        )
        assert (
            secret
            not in copied.execute("SELECT source_ref FROM memory_record_meta LIMIT 1").fetchone()[0]
        )
    assert (
        store.db.execute("SELECT source_ref FROM memory_record_meta LIMIT 1").fetchone()[0]
        == secret
    )


def test_bulk_episode_edit_invalidates_live_and_saved_faiss_vectors(store):
    from kiro_crew import memory_edit
    from kiro_crew import vector_memory as vm

    if not (vm._HAS_FAISS and vm._HAS_NUMPY):
        pytest.skip("optional FAISS is required for the actual persisted index regression")
    assert store.write_episodic(
        "Deployment instructions for the release service.", embedding=[1, 0]
    )
    store.build_faiss_index()
    store.save_faiss_index()
    assert store._read_meta("faiss_content_signature")
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        {
            "selection": {"query": {"q": "Deployment"}},
            "operation": {"type": "replace_text", "find": "Deployment", "replacement": "Gardening"},
        },
    )
    assert (
        memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])["changed_count"]
        == 1
    )
    assert store._faiss_index is None
    assert not store.search_episodic(
        query_embedding=[1, 0], query_text="deployment", relevance_filter=True
    )
    store.close()
    store.init()
    assert not store.search_episodic(
        query_embedding=[1, 0], query_text="deployment", relevance_filter=True
    )


def test_another_process_vector_edit_cannot_reuse_old_faiss_scores_after_restart(store):
    from kiro_crew import vector_memory as vm

    if not (vm._HAS_FAISS and vm._HAS_NUMPY):
        pytest.skip("optional FAISS is required for the actual persisted index regression")
    assert store.write_episodic(
        "Deployment instructions for the release service.", embedding=[1, 0]
    )
    store.build_faiss_index()
    store.save_faiss_index()
    where = " WHERE kind='episode'" if store.algorithm_version == "v2" else ""
    with sqlite3.connect(store._db_path) as editor:
        editor.execute(
            f"UPDATE {store._epi_rel} SET text=?, embedding=?{where}",
            ("Gardening instructions for the community garden.", struct.pack("2f", 0, 1)),
        )
    assert not store.search_episodic(
        query_embedding=[1, 0], query_text="deployment", relevance_filter=True
    )
    store.close()
    store.init()
    assert not store.search_episodic(
        query_embedding=[1, 0], query_text="deployment", relevance_filter=True
    )
    assert store.search_episodic(
        query_embedding=[0, 1], query_text="gardening", relevance_filter=True
    )
