"""Real SQLite previews reject concurrent updates and never widen a store."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from kiro_crew import memory_edit, memory_stores
from kiro_crew.config import loader
from kiro_crew.vector_memory import VectorMemoryStore


@pytest.fixture(params=["v1", "v2"])
def store(tmp_path, monkeypatch, request):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    if request.param == "v2":
        name = "member-alice"
        record = {"memory_version": 2, "owner_member": "alice"}
        directory = tmp_path / "memory_stores" / name
        directory.mkdir(parents=True)
        (directory / "member-memory.json").write_text(json.dumps(record), encoding="utf-8")
        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "memory_stores": {"default": {}, name: record},
                    "agents": {"alice": {"memory_store": name}},
                }
            ),
            encoding="utf-8",
        )
    else:
        directory = tmp_path
    loader._invalidate_config_cache()
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
    instance = VectorMemoryStore(db_path=directory / "memory.db")
    instance.init()
    try:
        yield instance
    finally:
        instance.close()
        loader._invalidate_config_cache()


def seed(store, count=65):
    for index in range(count):
        assert (
            store.set_semantic(
                f"user.email{index:03}",
                {"address": f"person{index}@old.example", "note": "contact preference"},
                1.0,
                "user_explicit",
            )
            is None
        )
    assert store.set_semantic("user.language", "Chinese", 1.0, "user_explicit") is None


def body(**kwargs):
    return {
        "selection": {"query": {"q": "email"}},
        "operation": {
            "type": "replace_text",
            "find": "@old.example",
            "replacement": "@new.example",
        },
        **kwargs,
    }


def test_paged_literal_search_and_atomic_batch(store):
    seed(store)
    assert (
        store.set_semantic(
            "user.mailpolicy", "Email policy: archive 邮件 after thirty days.", 1.0, "user_explicit"
        )
        is None
    )
    first = memory_edit.list_records(store, {"q": "email"}, limit=50)
    second = memory_edit.list_records(store, {"q": "email"}, offset=50)
    assert first["total"] == 66 and first["has_more"]
    assert len(second["entries"]) == 16 and not second["has_more"]
    assert {r["id"] for r in first["entries"]}.isdisjoint(r["id"] for r in second["entries"])
    preview = memory_edit.preview_edit(store, "chosen", b"secret", body())
    assert preview["matched_count"] == 66 and preview["changed_count"] == 65
    assert len(preview["entries"]) == 25 and preview["preview_has_more"]
    assert preview["preview_offset"] == 0 and preview["preview_limit"] == 25
    with mock.patch.object(
        store, "embed_fn", side_effect=AssertionError("owner editing must not embed")
    ):
        result = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert result["changed_count"] == 65
    assert memory_edit.list_records(store, {"q": "@new.example"})["total"] == 65
    assert json.loads(store.get_semantic("user.language")["value_json"]) == "Chinese"


def test_every_preview_page_reuses_the_signed_operation_and_original_expiry(store, monkeypatch):
    seed(store)
    monkeypatch.setattr(memory_edit.time, "time", lambda: 1000)
    first = memory_edit.preview_edit(store, "chosen", b"secret", body())
    monkeypatch.setattr(memory_edit.time, "time", lambda: 1200)
    second = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        {"preview_id": first["preview_id"], "offset": 25},
    )
    third = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        {"preview_id": first["preview_id"], "offset": 50},
    )
    assert second["preview_id"] == third["preview_id"] == first["preview_id"]
    assert second["expires_at"] == third["expires_at"] == first["expires_at"]
    assert (second["preview_offset"], third["preview_offset"]) == (25, 50)
    assert (len(second["entries"]), len(third["entries"])) == (25, 15)
    assert second["preview_has_more"] and not third["preview_has_more"]
    pages = [first["entries"], second["entries"], third["entries"]]
    identities = [{entry["before"]["id"] for entry in page} for page in pages]
    assert len(set().union(*identities)) == 65
    assert all(
        identities[left].isdisjoint(identities[right]) for left, right in ((0, 1), (0, 2), (1, 2))
    )
    result = memory_edit.apply_edit(store, "chosen", b"secret", first["preview_id"])
    assert result["changed_count"] == 65


@pytest.mark.parametrize("offset", [True, False, -25, 1, 25, 10000, 10**100, "25", None])
def test_preview_page_rejects_noncanonical_or_unbounded_offsets(store, offset):
    seed(store, 1)
    preview = memory_edit.preview_edit(store, "chosen", b"secret", body())
    with pytest.raises(memory_edit.MemoryEditError) as error:
        memory_edit.preview_edit(
            store,
            "chosen",
            b"secret",
            {"preview_id": preview["preview_id"], "offset": offset},
        )
    assert error.value.status == 400
    assert error.value.code == "invalid_memory_preview_offset"


def test_preview_page_refuses_stale_records_and_never_extends_expiry(store, monkeypatch):
    seed(store, 30)
    monkeypatch.setattr(memory_edit.time, "time", lambda: 1000)
    preview = memory_edit.preview_edit(store, "chosen", b"secret", body())
    store.set_semantic("user.email000", "changed elsewhere", 1.0, "user_explicit")
    with pytest.raises(memory_edit.MemoryEditError) as error:
        memory_edit.preview_edit(
            store,
            "chosen",
            b"secret",
            {"preview_id": preview["preview_id"], "offset": 25},
        )
    assert error.value.status == 409
    assert error.value.code == "stale_memory_preview"
    monkeypatch.setattr(memory_edit.time, "time", lambda: 1000 + memory_edit.PREVIEW_TTL + 1)
    with pytest.raises(memory_edit.MemoryEditError) as expired:
        memory_edit.preview_edit(
            store,
            "chosen",
            b"secret",
            {"preview_id": preview["preview_id"], "offset": 25},
        )
    assert expired.value.code == "expired_memory_preview"


def test_preview_page_rejects_unsigned_selection_or_operation_overrides(store):
    seed(store, 30)
    preview = memory_edit.preview_edit(store, "chosen", b"secret", body())
    with pytest.raises(memory_edit.MemoryEditError) as error:
        memory_edit.preview_edit(
            store,
            "chosen",
            b"secret",
            {
                "preview_id": preview["preview_id"],
                "offset": 25,
                "operation": {"type": "forget"},
            },
        )
    assert error.value.status == 400
    assert memory_edit.list_records(store, {"q": "@old.example"})["total"] == 30


@pytest.mark.parametrize("change", ["update", "new_match", "delete", "aba"])
def test_stale_preview_rejects_entire_batch(store, change):
    seed(store, 3)
    preview = memory_edit.preview_edit(store, "chosen", b"secret", body())
    if change == "update":
        store.set_semantic("user.email000", "changed@elsewhere.example", 1.0, "user_explicit")
    elif change == "new_match":
        store.set_semantic("user.emailnew", "new@old.example", 1.0, "user_explicit")
    elif change == "delete":
        store.delete_semantic("user.email000", source="user_explicit")
    else:
        original = json.loads(store.get_semantic("user.email000")["value_json"])
        store.set_semantic("user.email000", "other", 1.0, "user_explicit")
        store.set_semantic("user.email000", original, 1.0, "user_explicit")
    with pytest.raises(memory_edit.MemoryEditError, match="changed") as error:
        memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert error.value.status == 409
    assert "@old.example" in store.get_semantic("user.email001")["value_json"]
    assert memory_edit.list_records(store, {"q": "@new.example"})["total"] == 0


def test_explicit_cross_page_exclusions_and_single_correct(store):
    seed(store)
    entries = memory_edit.list_records(store, {"q": "email"}, limit=100)["entries"]
    excluded = [{"kind": entries[0]["kind"], "id": entries[0]["id"]}]
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(selection={"query": {"q": "email"}, "exclude": excluded}),
    )
    assert preview["changed_count"] == 64
    memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert "old.example" in store.get_semantic(entries[0]["id"])["value_json"]
    selected = memory_edit.list_records(store, {"q": "email064"})["entries"][0]
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(
            selection={"items": [{key: selected[key] for key in ("kind", "id", "revision")}]},
            operation={"type": "set", "value": "individual@example.org"},
        ),
    )
    memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert json.loads(store.get_semantic(selected["id"])["value_json"]) == "individual@example.org"


@pytest.mark.parametrize(
    "before,after",
    [
        ({"enabled": 1, "disabled": 0}, {"enabled": True, "disabled": False}),
        ({"enabled": True}, {"enabled": 1}),
        ({"flags": [{"enabled": 1}, 0]}, {"flags": [{"enabled": True}, False]}),
    ],
)
def test_owner_correction_preserves_json_boolean_and_number_types(store, before, after):
    assert store.set_semantic("project.flags", before, 1.0, "user_explicit") is None
    row = memory_edit.list_records(store, {})["entries"][0]
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(
            selection={"items": [{key: row[key] for key in ("kind", "id", "revision")}]},
            operation={"type": "set", "value": after},
        ),
    )
    assert preview["changed_count"] == 1
    result = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert result["changed_count"] == 1
    # Serialized equality detects types that Python's value equality erases.
    stored = json.loads(store.get_semantic("project.flags")["value_json"])
    assert json.dumps(stored, sort_keys=True) == json.dumps(after, sort_keys=True)
    assert memory_edit.list_records(store, {})["entries"][0]["revision"] != row["revision"]


def test_reordered_json_object_is_still_an_unchanged_correction(store):
    assert (
        store.set_semantic("project.flags", {"enabled": True, "count": 1}, 1.0, "user_explicit")
        is None
    )
    row = memory_edit.list_records(store, {})["entries"][0]
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(
            selection={"items": [{key: row[key] for key in ("kind", "id", "revision")}]},
            operation={"type": "set", "value": {"count": 1, "enabled": True}},
        ),
    )
    assert preview["changed_count"] == 0


@pytest.mark.parametrize("failure", ["scope", "tampered", "expired"])
def test_preview_cannot_cross_scope_or_be_rewritten(store, monkeypatch, failure):
    seed(store, 1)
    preview = memory_edit.preview_edit(store, "chosen", b"secret", body())
    token = preview["preview_id"]
    scope = "other" if failure == "scope" else "chosen"
    if failure == "tampered":
        token = "X" + token[1:]
    if failure == "expired":
        monkeypatch.setattr(memory_edit.time, "time", lambda: 99999999999)
    with pytest.raises(memory_edit.MemoryEditError):
        memory_edit.apply_edit(store, scope, b"secret", token)
    assert "@old.example" in store.get_semantic("user.email000")["value_json"]


def test_transaction_rolls_back_prior_rows_on_failure(store, monkeypatch):
    seed(store, 3)
    preview = memory_edit.preview_edit(store, "chosen", b"secret", body())
    original = memory_edit._write
    calls = 0

    def failing_write(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("disk failed")
        original(*args)

    monkeypatch.setattr(memory_edit, "_write", failing_write)
    with pytest.raises(RuntimeError, match="disk failed"):
        memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert memory_edit.list_records(store, {"q": "@old.example"})["total"] == 3
    assert not store.db.in_transaction


def test_replace_edits_values_not_scope_or_object_keys(store):
    value = {
        "rule": "Send email to old@example.org",
        "category": "knowledge",
        "repo_scope": "old@example.org",
    }
    assert store.set_semantic("lesson.email", value, 1.0, "user_explicit") is None
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(operation={"type": "replace_text", "find": "old", "replacement": "new"}),
    )
    memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    updated = json.loads(store.get_semantic("lesson.email")["value_json"])
    assert updated["rule"] == "Send email to new@example.org"
    assert updated["repo_scope"] == "old@example.org"


def test_forget_episode_and_semantic_is_reversible_history(store):
    seed(store, 1)
    episode = store.write_episodic(
        "Discussed the email old@example.org and agreed on future replies.",
        conversation_id="test-conversation",
        tags=["email"],
    )
    assert episode
    preview = memory_edit.preview_edit(
        store, "chosen", b"secret", body(operation={"type": "forget"})
    )
    assert preview["changed_count"] == 2
    memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert memory_edit.list_records(store, {"q": "email"})["total"] == 0
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_events WHERE event_type = 'delete'"
        ).fetchone()[0]
        >= 2
    )
    assert store.db.execute("SELECT count(*) FROM memory_revisions").fetchone()[0] >= 2


def test_retry_of_acknowledgement_lost_apply_is_idempotent(store):
    seed(store, 1)
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(operation={"type": "replace_text", "find": "old", "replacement": "oldold"}),
    )
    first = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    second = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert first == second == {"ok": True, "changed_count": 1}
    assert "oldoldold" not in store.get_semantic("user.email000")["value_json"]


def test_refresh_exact_identity_retains_changed_record_and_reports_missing(store):
    seed(store, 2)
    old = memory_edit.list_records(store, {"q": "email000"})["entries"][0]
    store.set_semantic(old["id"], "new@elsewhere.example", 1.0, "user_explicit")
    store.delete_semantic("user.email001", "user_explicit")
    result = memory_edit.refresh_records(
        store, [{"kind": "fact", "id": "user.email000"}, {"kind": "fact", "id": "user.email001"}]
    )
    assert result["entries"][0]["revision"] != old["revision"]
    assert result["missing"] == [{"kind": "fact", "id": "user.email001"}]
    wrong_kind = memory_edit.refresh_records(store, [{"kind": "directive", "id": "user.email000"}])
    assert wrong_kind["entries"] == []


@pytest.mark.parametrize("cap", ["MAX_BATCH_ROWS", "MAX_BATCH_BYTES"])
def test_query_refresh_enforces_the_same_selection_caps_without_writes(store, monkeypatch, cap):
    seed(store, 3)
    before = store.db.total_changes
    monkeypatch.setattr(memory_edit, cap, 1)
    with pytest.raises(memory_edit.MemoryEditError) as error:
        memory_edit.refresh_records(store, selection={"query": {"q": "email"}})
    assert error.value.status == 413
    assert error.value.code == "memory_selection_too_large"
    assert store.db.total_changes == before


@pytest.mark.parametrize(
    "selection",
    [
        {"query": {}, "exclude": [{"kind": "fact", "id": "user.contact"}] * 501},
        {"query": {"kind": "invalid"}},
        {"query": {"topic": "email"}},
        {"query": {}, "items": []},
    ],
)
def test_query_refresh_rejects_invalid_or_unbounded_selection(store, selection):
    with pytest.raises(memory_edit.MemoryEditError) as error:
        memory_edit.refresh_records(store, selection=selection)
    assert error.value.status == 400


def test_owner_correction_preserves_each_lineages_conflict_evidence(store):
    seed(store, 1)
    private_policy = store.algorithm_version == "v2"
    before = dict(store.get_semantic("user.email000"))
    other_record = dict(store.get_semantic("user.language"))
    assert (
        store.set_semantic("user.email000", "proposed@elsewhere.example", 0.9, "consolidation")
        is not None
    )
    assert dict(store.get_semantic("user.email000")) == before
    record = memory_edit.list_records(store, {"q": "email000"})["entries"][0]
    assert record["metadata"]["pending_conflicts"] == int(private_policy)
    history = memory_edit.record_history(store, {"kind": "fact", "id": record["id"]})
    proposals = [row for row in history["entries"] if row["status"] == "conflict"]
    assert len(proposals) == int(private_policy)
    if private_policy:
        assert "proposed@elsewhere.example" in proposals[0]["after_json"]
        assert proposals[0]["base_revision"] == history["current_revision"]
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(
            selection={"items": [{key: record[key] for key in ("kind", "id", "revision")}]},
            operation={"type": "set", "value": "proposed@elsewhere.example"},
        ),
    )
    assert preview["changed_count"] == 1
    assert preview["entries"][0]["operation"] == "correct"
    applied = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert applied["changed_count"] == 1
    current = memory_edit.list_records(store, {"q": "email000"})["entries"][0]
    assert current["metadata"]["pending_conflicts"] == 0
    assert current["metadata"]["revision"] == record["metadata"]["revision"] + 1
    assert json.loads(current["value_json"]) == "proposed@elsewhere.example"
    assert current["source"] == "user_explicit"
    assert dict(store.get_semantic("user.language")) == other_record
    history = memory_edit.record_history(store, {"kind": "fact", "id": record["id"]})
    assert history["entries"][0]["operation"] == "correct"
    assert {row["id"] for row in proposals} <= {row["id"] for row in history["entries"]}
    assert sum(row["status"] == "conflict" for row in history["entries"]) == int(private_policy)


def test_keep_current_value_resolves_only_pending_proposals_without_mutating_content(store):
    seed(store, 1)
    private_policy = store.algorithm_version == "v2"
    assert (
        store.set_semantic("user.email000", "wrong@elsewhere.example", 0.9, "consolidation")
        is not None
    )
    before = dict(store.get_semantic("user.email000"))
    record = memory_edit.list_records(store, {"q": "email000"})["entries"][0]
    history_before = memory_edit.record_history(store, {"kind": "fact", "id": record["id"]})
    assert record["metadata"]["pending_conflicts"] == int(private_policy)
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(
            selection={"items": [{key: record[key] for key in ("kind", "id", "revision")}]},
            operation={"type": "set", "value": json.loads(record["value_json"])},
        ),
    )
    assert preview["changed_count"] == int(private_policy)
    if private_policy:
        assert preview["entries"][0]["operation"] == "resolve"
        assert preview["entries"][0]["before"] == preview["entries"][0]["after"]
    else:
        # V1 rejected the automated write without creating a proposal to review.
        assert preview["entries"] == []
    applied = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert applied["changed_count"] == int(private_policy)
    assert applied == memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert dict(store.get_semantic("user.email000")) == before
    current = memory_edit.list_records(store, {"q": "email000"})["entries"][0]
    assert current["metadata"]["pending_conflicts"] == 0
    assert current["metadata"]["revision"] == record["metadata"]["revision"] + int(private_policy)
    history = memory_edit.record_history(store, {"kind": "fact", "id": record["id"]})
    if private_policy:
        assert history["entries"][0]["operation"] == "resolve"
        assert any(row["status"] == "conflict" for row in history["entries"])
    else:
        assert history == history_before


def test_only_a_new_proposal_invalidates_keep_current_preview(store):
    seed(store, 1)
    private_policy = store.algorithm_version == "v2"
    before = dict(store.get_semantic("user.email000"))
    assert (
        store.set_semantic("user.email000", "wrong@elsewhere.example", 0.9, "consolidation")
        is not None
    )
    record = memory_edit.list_records(store, {"q": "email000"})["entries"][0]
    assert record["metadata"]["pending_conflicts"] == int(private_policy)
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(
            selection={"items": [{key: record[key] for key in ("kind", "id", "revision")}]},
            operation={"type": "set", "value": json.loads(record["value_json"])},
        ),
    )
    assert (
        store.set_semantic("user.email000", "another@elsewhere.example", 0.9, "consolidation")
        is not None
    )
    if private_policy:
        with pytest.raises(memory_edit.MemoryEditError) as error:
            memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
        assert error.value.status == 409
    else:
        # Another rejected V1 write changes neither the row nor the preview.
        applied = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
        assert applied == {"ok": True, "changed_count": 0}
        assert applied == memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    current = memory_edit.list_records(store, {"q": "email000"})["entries"][0]
    assert current["metadata"]["pending_conflicts"] == 2 * int(private_policy)
    assert current["metadata"]["revision"] == record["metadata"]["revision"]
    assert dict(store.get_semantic("user.email000")) == before


@pytest.mark.parametrize(
    "selection",
    [
        {"query": {"kind": []}},
        {"items": [{"kind": {}, "id": "x", "revision": "a" * 64}]},
        {"query": {}, "exclude": "bad"},
    ],
)
def test_malformed_selection_is_an_explicit_client_error(store, selection):
    with pytest.raises(memory_edit.MemoryEditError) as error:
        memory_edit.preview_edit(store, "chosen", b"secret", body(selection=selection))
    assert error.value.status == 400


def test_owner_records_keep_episode_source_and_copy_provenance(store):
    from kiro_crew import memory_schema

    assert store.write_episodic(
        "We discussed emails and remembered the copied source correctly.",
        source="user_seed",
        facets=memory_schema.MemoryFacets(derived_from="default:episode:original"),
    )
    row = memory_edit.list_records(store, {"kind": "episode"})["entries"][0]
    if store.algorithm_version == "v2":
        assert row["source"] == "user_seed"
        assert row["derived_from"] == "default:episode:original"
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(
            selection={"items": [{key: row[key] for key in ("kind", "id", "revision")}]},
            operation={
                "type": "set",
                "text": "We corrected the email discussion and kept its original source.",
            },
        ),
    )
    memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    updated = memory_edit.list_records(store, {"kind": "episode"})["entries"][0]
    assert updated["updated_at"] != row["updated_at"]
    assert updated["derived_from"] == row["derived_from"]


def test_legacy_string_lesson_cannot_gain_scope_through_content_conversion(store):
    original = "Run the project checks before submitting changes."
    assert store.set_semantic("lesson.project_checks", original, 1.0, "user_explicit") is None
    row = memory_edit.list_records(store, {"kind": "directive"})["entries"][0]
    selection = {"items": [{key: row[key] for key in ("kind", "id", "revision")}]}
    with pytest.raises(memory_edit.MemoryEditError, match="scope"):
        memory_edit.preview_edit(
            store,
            "chosen",
            b"secret",
            body(
                selection=selection,
                operation={"type": "set", "value": {"rule": original, "repo_scope": "/other"}},
            ),
        )
    assert json.loads(store.get_semantic(row["id"])["value_json"]) == original
    # A format conversion that does not add authority remains a valid correction.
    corrected = {"rule": "Run the complete project checks before submitting changes."}
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        body(selection=selection, operation={"type": "set", "value": corrected}),
    )
    result = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert result["changed_count"] == 1
    assert json.loads(store.get_semantic(row["id"])["value_json"]) == corrected
