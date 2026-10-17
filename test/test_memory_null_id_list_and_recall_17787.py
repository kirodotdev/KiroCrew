"""A legacy row with no identity must not break the memory list, recall or edit.

Opening the store tolerates a row whose ``id`` (episode) or ``key`` (semantic)
is NULL or empty. Such a row reaches ``record_id_for`` from the memory list
(``memory_edit._rows``) and from recall (``vector_memory._eligible_rows``),
which raise on a non-string or empty identity. Both read paths tolerate the
row through ``record_identity_or_none``, matching the ``reconcile`` skip
contract: the row stays on disk, untouched. The bulk-edit collection path
(``memory_edit._collect``) excludes the row from edits for the same reason --
``_write`` looks a record up by that identity, and ``WHERE id = NULL`` matches
nothing -- so the row remains visible in the list but can never enter a batch.
"""

from kiro_crew import memory_edit
from kiro_crew.vector_memory import VectorMemoryStore

_SECRET = b"test-secret-key-for-memory-edit-preview-signing"
_STORE_NAME = "test-store"


def _store_with_null_id_rows(tmp_path):
    """A store that opens over a good row, an ineligible row and two bad rows."""
    tier = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    tier.init()
    # A real eligible episode and semantic fact so the read paths have content.
    tier.write_episodic("a good memory", source="user_explicit")
    assert tier.set_semantic("user.city", "Seattle", 1.0, "user_explicit") is None
    # Mark one record ineligible so _ineligible_ids() is non-empty: that is the
    # branch in _eligible_rows that calls record_id_for for every row.
    tier.db.execute(
        "UPDATE memory_record_meta SET status = 'superseded' WHERE record_id = 'key:user.city'"
    )
    # Legacy rows SQLite allows but older importers wrote: identity is NULL/empty.
    tier.db.execute(
        "INSERT INTO episodic_memories (id, text, created_at) VALUES (NULL, 'x', '2026-01-01')"
    )
    tier.db.execute(
        "INSERT INTO episodic_memories (id, text, created_at) VALUES ('', 'y', '2026-01-01')"
    )
    tier.db.execute(
        "INSERT INTO semantic_memory (key, value_json, source, created_at, updated_at) "
        "VALUES (NULL, '1', 'user_explicit', '2026-01-01', '2026-01-01')"
    )
    tier.db.commit()
    return tier


def test_memory_list_and_recall_tolerate_null_id_legacy_rows(tmp_path):
    tier = _store_with_null_id_rows(tmp_path)
    try:
        # The dashboard Memory list: must not raise on the NULL-id rows.
        listed = memory_edit.list_records(tier, {"q": "", "kind": "all"})
        assert listed["total"] >= 1
        # Recall: _ineligible_ids() is non-empty, so _eligible_rows runs its
        # record_id_for branch over every row, including the NULL-id ones.
        recalled = tier.recall("good", cap=3000)
        assert isinstance(recalled, dict)
        # The bad rows are left on disk, untouched (same contract as reconcile).
        assert tier.db.execute("SELECT COUNT(*) FROM episodic_memories").fetchone()[0] == 3
        assert tier.db.execute("SELECT COUNT(*) FROM semantic_memory").fetchone()[0] == 2
    finally:
        tier.close()


def test_recall_tolerates_null_id_episode_with_empty_blocked_set(tmp_path):
    """Recall must not crash on a NULL-id episode that a keyword query matches.

    With no ineligible rows the eligibility filter takes its empty-``blocked``
    fast path, which once returned every row. A NULL-id episode kept that way
    reaches the recall payload builder, whose context reads ``row["id"]`` after
    ``recall_evidence`` has dropped the non-string id, raising ``KeyError``.
    The eligibility filter now drops identityless rows before ranking on both
    paths, so recall returns a payload and the row stays on disk.
    """
    tier = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    tier.init()
    try:
        tier.write_episodic("a good memory about cats", source="user_explicit")
        # A NULL-id legacy episode that the keyword query matches; no row is
        # ineligible, so _ineligible_ids() is empty and the fast path runs.
        tier.db.execute(
            "INSERT INTO episodic_memories (id, text, created_at) "
            "VALUES (NULL, 'a legacy memory', '2026-01-01')"
        )
        tier.db.commit()
        recalled = tier.recall("legacy", cap=3000)
        assert isinstance(recalled, dict)
        # The NULL-id episode is left on disk, untouched.
        assert tier.db.execute("SELECT COUNT(*) FROM episodic_memories").fetchone()[0] == 2
    finally:
        tier.close()


def test_query_forget_excludes_null_id_rows_and_applies_cleanly(tmp_path):
    """A query-selected forget over every memory must not crash on a NULL-id row.

    Without the guard the row enters ``_collect`` -> ``_write`` looks it up with
    ``WHERE id = NULL``, gets no row, and ``dict(None)`` raises a TypeError that
    rolls the whole batch back as an HTTP 500. With the guard the NULL-id rows
    are excluded from the batch, the good row is forgotten, and the bad rows
    stay on disk.
    """
    tier = _store_with_null_id_rows(tmp_path)
    try:
        selection = {
            "selection": {"query": {"q": "", "kind": "all"}},
            "operation": {"type": "forget"},
        }
        preview = memory_edit.preview_edit(tier, _STORE_NAME, _SECRET, selection)
        # The NULL/empty-id rows are excluded from the batch; only real records change.
        assert preview["changed_count"] >= 1
        result = memory_edit.apply_edit(tier, _STORE_NAME, _SECRET, preview["preview_id"])
        assert result["ok"] is True
        # The legacy NULL/empty-id rows survive untouched -- never deleted, never crashed.
        bad_epi = tier.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE id IS NULL OR id = ''"
        ).fetchone()[0]
        assert bad_epi == 2
        bad_sem = tier.db.execute(
            "SELECT COUNT(*) FROM semantic_memory WHERE key IS NULL OR key = ''"
        ).fetchone()[0]
        assert bad_sem == 1
    finally:
        tier.close()
