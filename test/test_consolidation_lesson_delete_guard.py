"""History consolidation must not retire a standing lesson on a model's guess.

A consolidation turn renders stored ``lesson.*`` rows as rule prose and asks the
model to delete stale ones, so a routine turn can emit ``{"key": "lesson.<hash>",
"delete": true}`` for a rule the user taught. The invariant the ``/api/lessons``
contradiction sweep enforces -- a guess may retire an ``on_topic`` finding, never
an ``applies=always`` standing rule (nor an unstated row, which every read path
serves AS a standing rule) -- must hold on this sibling deletion path too.

These tests pin that guard at the consolidation delete site
(:meth:`HistoryConsolidator._write_structured_memory`), for both the v1
``delete_semantic`` path and the v2 private-policy ``propose_semantic_delete``
path, mirroring the parametrization ``test_lesson_contradiction`` added for the
sweep.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew.history import HistoryConsolidator
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_ALWAYS,
    LESSON_APPLIES_ON_TOPIC,
)
from kiro_crew.vector_memory import (
    VectorMemoryStore,
    _lesson_key,
    open_member_database,
)


def _v1_store(tmp_path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


def _v2_store(tmp_path, monkeypatch) -> VectorMemoryStore:
    from kiro_crew import memory_stores

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    directory = declare_v2_store(tmp_path, "member-alice")
    store = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=4
    )
    assert store.algorithm_version == "v2"
    return store


def _consolidator(store: VectorMemoryStore) -> HistoryConsolidator:
    return HistoryConsolidator(
        log=MagicMock(), memory=MagicMock(), vector_store=store, migrated=True
    )


def _delete_result(key: str) -> dict:
    return {"semantic": [{"key": key, "delete": True}]}


def _row_alive(store: VectorMemoryStore, key: str) -> bool:
    return store.get_semantic(key) is not None


@pytest.mark.parametrize("applies", [LESSON_APPLIES_ALWAYS, None])
def test_consolidation_delete_refuses_standing_lesson(tmp_path, applies) -> None:
    """A standing (``always``) or unstated lesson survives a consolidation delete.

    ``applies=None`` writes an unstated row, which reads AS a standing rule -- the
    protected side of the narrowest fail-safe.
    """
    store = _v1_store(tmp_path)
    try:
        rule = "always squash fork PRs to exactly one commit"
        assert store.write_lesson(rule, "preference", source="user_explicit", applies=applies)
        key = _lesson_key(rule)
        assert _row_alive(store, key), "precondition: the standing rule is stored"

        _consolidator(store)._write_structured_memory(_delete_result(key), key="s1")

        assert _row_alive(store, key), "a guessed consolidation delete must not retire it"
    finally:
        store.close()


def test_consolidation_delete_allows_on_topic_finding(tmp_path) -> None:
    """An ``on_topic`` finding remains deletable by consolidation."""
    store = _v1_store(tmp_path)
    try:
        rule = "in repo foo the flaky timing test needs a wider bound"
        assert store.write_lesson(
            rule, "knowledge", source="consolidation", applies=LESSON_APPLIES_ON_TOPIC
        )
        key = _lesson_key(rule)
        assert _row_alive(store, key), "precondition: the finding is stored"

        _consolidator(store)._write_structured_memory(_delete_result(key), key="s1")

        assert not _row_alive(store, key), "an on_topic finding must still be deletable"
    finally:
        store.close()


def test_consolidation_delete_of_non_lesson_key_is_unaffected(tmp_path) -> None:
    """The guard is scoped to ``lesson.*`` keys; a plain fact still deletes."""
    store = _v1_store(tmp_path)
    try:
        store.set_semantic("project.status", "active", 0.9, source="user_explicit")
        assert _row_alive(store, "project.status"), "precondition: the fact is stored"

        _consolidator(store)._write_structured_memory(_delete_result("project.status"), key="s1")

        assert not _row_alive(store, "project.status"), "a non-lesson key is not tier-guarded"
    finally:
        store.close()


@pytest.mark.parametrize("applies", [LESSON_APPLIES_ALWAYS, None])
def test_private_policy_delete_refuses_standing_lesson(tmp_path, monkeypatch, applies) -> None:
    """The v2 private-policy path (propose_semantic_delete) is guarded too.

    The v2 store turns a delete into a review PROPOSAL rather than a hard delete,
    but a model's guess must still never target a standing rule, so no proposal is
    raised for one. ``propose_semantic_delete`` is wrapped to record its calls so
    the assertion is that it is never invoked for a standing key.
    """
    store = _v2_store(tmp_path, monkeypatch)
    try:
        rule = "never force-push shared history"
        assert store.write_lesson(rule, "preference", source="user_explicit", applies=applies)
        key = _lesson_key(rule)
        assert _row_alive(store, key), "precondition: the standing rule is stored"

        proposed: list[str] = []
        original = store.propose_semantic_delete

        def _record(k, source):
            proposed.append(k)
            return original(k, source)

        monkeypatch.setattr(store, "propose_semantic_delete", _record)

        _consolidator(store)._write_structured_memory(_delete_result(key), key="s1")

        assert proposed == [], "no delete proposal may be raised for a standing rule"
        assert _row_alive(store, key)
    finally:
        store.close()


def test_allowed_delete_compare_and_deletes_against_the_checked_body(
    tmp_path, monkeypatch, caplog
) -> None:
    """An allowed delete no-ops if the row moved under the key after the check.

    ``_lesson_key`` keys on rule text plus scope, so a concurrent delete-plus-re-add
    -- the documented way to change a tier -- can replace an ``on_topic`` finding
    with an ``always`` standing rule under the same key between the guard's read
    and the delete. The delete must carry the checked body as ``expect_value_json``
    so it tombstones nothing when the stored body has changed.

    The race is simulated deterministically: the guard reads an ``on_topic`` body
    (so the delete is allowed and compares against THAT body), while the store
    actually holds a standing rule under the key. Compare-and-delete must then
    leave the standing rule intact.
    """
    store = _v1_store(tmp_path)
    try:
        rule = "always keep one commit per PR"
        # The row the store actually holds is a STANDING rule.
        assert store.write_lesson(
            rule, "preference", source="user_explicit", applies=LESSON_APPLIES_ALWAYS
        )
        key = _lesson_key(rule)
        assert _row_alive(store, key)

        # Simulate the guard having read the PRIOR on_topic body (the finding that
        # existed under this key before the re-tier), so the delete is allowed and
        # its expect_value_json is that stale body.
        stale_on_topic_body = json.dumps(
            {"rule": rule, "category": "knowledge", "negative": None, "applies": "on_topic"}
        )
        real_get_semantic = store.get_semantic

        def _stale_read(k):
            row = real_get_semantic(k)
            if isinstance(row, dict) and k == key:
                row = dict(row, value_json=stale_on_topic_body)
            return row

        monkeypatch.setattr(store, "get_semantic", _stale_read)

        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            _consolidator(store)._write_structured_memory(_delete_result(key), key="s1")

        # The stored body never matched the checked body, so nothing was deleted.
        assert (
            real_get_semantic(key) is not None
        ), "compare-and-delete must not tombstone a row that moved under the key"
        # The lost compare-and-delete is announced, not swallowed into "0 deleted".
        assert "compare-and-delete no-op" in caplog.text
        assert "1 stale-skipped" in caplog.text
    finally:
        store.close()


def test_absent_lesson_key_delete_is_refused_not_unconditional(
    tmp_path, monkeypatch, caplog
) -> None:
    """A delete of a lesson.* key with no active row is refused, never unconditional.

    The absent-row state is the intermediate state of the documented
    delete-plus-re-add re-tier, so a concurrent learn_add can recreate the key as
    a standing rule between the guard's read and the delete. If the guard let an
    absent lesson key through, the delete would carry no compare body and tombstone
    that replacement unconditionally. The guard must refuse it, and never reach
    delete_semantic for it. The refusal is logged and counted as ``absent`` --
    NOT as a standing-rule refusal, which would mislead an operator into thinking
    a rule that never existed was protected.
    """
    store = _v1_store(tmp_path)
    try:
        key = _lesson_key("a rule that was never stored")
        assert store.get_semantic(key) is None, "precondition: no row under this lesson key"

        called: list[tuple] = []
        real_delete = store.delete_semantic

        def _record_delete(k, source, **kw):
            called.append((k, kw))
            return real_delete(k, source, **kw)

        monkeypatch.setattr(store, "delete_semantic", _record_delete)

        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            _consolidator(store)._write_structured_memory(_delete_result(key), key="s1")

        assert called == [], "an absent lesson key must be refused, not sent to delete_semantic"
        # Counted and logged as absent, distinct from a standing-rule tier refusal.
        assert "no active lesson row" in caplog.text
        assert "1 absent" in caplog.text
        assert "refused to retire standing lesson" not in caplog.text
    finally:
        store.close()
