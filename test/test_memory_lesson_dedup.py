"""A private store's consolidation lessons fold into stored copies of the same rule.

``kiro_crew.memory_item_identity.same_item`` is the one rule; ``memory.member_lesson_dedup``
decides what it changes: nothing under ``off``, a bounded log under ``shadow``,
under ``enforce`` a skipped textual copy at write time and a retired new near-copy
when the backfill gives it a vector. A person's lesson is never folded.
"""

from __future__ import annotations

import json
import math
import struct
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import injection_receipts
from kiro_crew import memory_item_identity as mii
from kiro_crew import memory_lesson_dedup as mld
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.vector_memory import VectorMemoryStore, _lesson_key, open_member_database

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
_SIG = "test-space"

RULE = "Run the linter before every commit"
RULE_CASE = "run the  LINTER before every commit"
RULE_NEAR = "Execute lint checks prior to committing"


def _vec(angle: float) -> list[float]:
    """A 4-d vector at *angle* radians from the x axis (cosine to x is cos(angle))."""
    return [math.cos(angle), math.sin(angle), 0.0, 0.0]


def _item(key="k", text="t", scope=None, tier=mii.TIER_RULE, vec=None, space=_SIG, taught=False):
    blob = struct.pack("4f", *vec) if vec is not None else None
    return mii.Item(
        key=key,
        text=text,
        scope=scope,
        tier=tier,
        vector=mii.unit_vector(blob, 4),
        space=space,
        user_taught=taught,
    )


# same_item


def test_scope_and_tier_must_match_even_for_equal_text() -> None:
    a = _item(text=RULE)
    assert not mii.same_item(a, _item(text=RULE, scope="org/repo"))
    assert not mii.same_item(a, _item(text=RULE, tier=mii.TIER_FINDINGS))


def test_normalized_text_is_the_same_item() -> None:
    assert mii.same_item(_item(text=RULE), _item(text=RULE_CASE))
    assert mii.same_item(_item(text="ｒｕｎ tests"), _item(text="run TESTS"))  # NFKC


def test_vectors_at_or_above_the_threshold_are_the_same_item() -> None:
    at = math.acos(0.951)
    below = math.acos(0.949)
    assert mii.same_item(_item(text="a", vec=_vec(0)), _item(text="b", vec=_vec(at)))
    assert not mii.same_item(_item(text="a", vec=_vec(0)), _item(text="b", vec=_vec(below)))


@pytest.mark.parametrize(
    "other",
    [
        _item(text="b", vec=None),  # missing vector
        _item(text="b", vec=[0.0, 0.0, 0.0, 0.0]),  # zero vector
        _item(text="b", vec=_vec(0), space="other-space"),  # another space
        _item(text="b", vec=_vec(0), space=None),  # unattributed space
    ],
)
def test_an_unusable_vector_is_never_a_match(other) -> None:
    assert not mii.same_item(_item(text="a", vec=_vec(0)), other)


def test_a_vector_of_another_width_is_unusable() -> None:
    assert mii.unit_vector(struct.pack("3f", 1.0, 0.0, 0.0), 4) is None
    assert mii.unit_vector(struct.pack("4f", float("nan"), 0, 0, 0), 4) is None


def test_leader_fold_never_chains() -> None:
    # a~b and b~c at 0.96, a~c below 0.95: c must not join a's group through b.
    step = math.acos(0.96)
    a = _item("a", "a", vec=_vec(0))
    b = _item("b", "b", vec=_vec(step))
    c = _item("c", "c", vec=_vec(2 * step))
    groups = mii.leader_fold([a, b, c])
    assert [(g[0].key, [f.key for f in g[1]]) for g in groups] == [("a", ["b"]), ("c", [])]


def test_a_user_taught_row_is_always_a_leader() -> None:
    plain = _item("plain", RULE)
    taught = _item("taught", RULE, taught=True)
    groups = mii.leader_fold([plain, taught])
    assert [g[0].key for g in groups] == ["plain", "taught"]
    assert mii.leader_of(taught, [plain]) is None


# config


@pytest.mark.parametrize(
    "raw, want",
    [("shadow", "shadow"), ("enforce", "enforce"), ("on", "off"), (None, "off"), (1, "off")],
)
def test_unknown_values_read_as_the_default(raw, want) -> None:
    assert _build_memory_config({"member_lesson_dedup": raw}).member_lesson_dedup == want


# The private store


@pytest.fixture
def store(tmp_path, monkeypatch):
    from kiro_crew import memory_stores

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    monkeypatch.setattr(injection_receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    monkeypatch.setattr(mld, "automatic_writes_off", lambda: False)
    # Every store write gets its own, strictly later stamp, so the vector step's
    # "written before" order never rests on the wall clock's resolution.
    from kiro_crew import vector_memory

    ticks = iter(range(1, 1_000_000))
    monkeypatch.setattr(
        vector_memory,
        "_now_iso",
        lambda: (_T0 + timedelta(microseconds=next(ticks))).isoformat(),
    )
    directory = declare_v2_store(tmp_path, "member-alice")
    opened = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=4
    )
    opened.reconcile_embedding_space(_SIG)
    yield opened
    opened.close()


def _mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(mld, "configured_mode", lambda: mode)


def _consolidate(store, lessons: list[dict], *, mode: str, n: int = 0) -> dict:
    return store.apply_consolidation(
        source_id=f"src-{n}",
        session_key=f"s{n}",
        source_total=1,
        result={"lessons": lessons},
        snapshot={},
        messages=[{"role": "user", "content": "hi"}],
        lesson_dedup_mode=mode,
    )


def _live(store) -> dict[str, dict]:
    with store._db_lock:
        rows = store.db.execute(
            "SELECT key, value_json, source FROM semantic_memory "
            "WHERE is_deleted = 0 AND key LIKE 'lesson.%'"
        ).fetchall()
    return {r["key"]: dict(r) for r in rows}


def _meta(store) -> dict[str, Any]:
    with store._db_lock:
        rows = store.db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB 'memory_dedup_log:*'"
        ).fetchall()
    return {str(k): json.loads(v) if str(v)[:1] in "[{" else v for k, v in rows}


def _counts(store, mode: str) -> dict:
    merged: dict = {}
    for key, value in _meta(store).items():
        if key.startswith(mld.COUNTS_PREFIX):
            for name, n in value.get(mode, {}).items():
                merged[name] = merged.get(name, 0) + n
    return merged


def test_off_writes_the_copy_and_logs_nothing(store) -> None:
    _consolidate(store, [{"rule": RULE}], mode="off", n=1)
    _consolidate(store, [{"rule": RULE_CASE}], mode="off", n=2)
    assert len(_live(store)) == 2
    assert _meta(store) == {}


def test_shadow_counts_a_textual_copy_once(store, monkeypatch) -> None:
    _mode(monkeypatch, "shadow")
    _embedder(store, {RULE: _vec(0), RULE_CASE: _vec(0)})
    _consolidate(store, [{"rule": RULE}], mode="shadow", n=1)
    _consolidate(store, [{"rule": RULE_CASE}], mode="shadow", n=2)
    store.backfill_missing_embeddings(pace=False)
    counts = _counts(store, "shadow")
    assert counts["text_folds"] == 1 and "vector_folds" not in counts


def test_shadow_writes_the_copy_and_counts_the_fold(store) -> None:
    _consolidate(store, [{"rule": RULE}], mode="shadow", n=1)
    receipt = _consolidate(store, [{"rule": RULE_CASE}], mode="shadow", n=2)
    assert len(_live(store)) == 2
    assert "lessons_merged" not in receipt
    assert _counts(store, "shadow")["text_folds"] == 1
    ids = [v for k, v in _meta(store).items() if k.startswith(mld.IDS_PREFIX)][0]
    assert ids == [
        {"key": _lesson_key(RULE_CASE), "leader": _lesson_key(RULE), "step": "text", "rules": 1}
    ]
    # Ids and counts only: no lesson text reaches the log.
    assert "linter" not in json.dumps(_meta(store)).lower()


def test_enforce_skips_a_textual_copy(store) -> None:
    _consolidate(store, [{"rule": RULE}], mode="enforce", n=1)
    receipt = _consolidate(store, [{"rule": RULE_CASE}], mode="enforce", n=2)
    assert list(_live(store)) == [_lesson_key(RULE)]
    assert receipt["lessons_merged"] == 1 and receipt["lessons"] == 0


def test_enforce_folds_a_copy_inside_one_span(store) -> None:
    receipt = _consolidate(store, [{"rule": RULE}, {"rule": RULE_CASE}], mode="enforce", n=1)
    assert list(_live(store)) == [_lesson_key(RULE)]
    assert receipt["lessons_merged"] == 1


def test_enforce_writes_a_copy_that_adds_a_clause(store) -> None:
    _consolidate(store, [{"rule": RULE}], mode="enforce", n=1)
    _consolidate(store, [{"rule": RULE_CASE, "negative": "skipping it"}], mode="enforce", n=2)
    assert len(_live(store)) == 2


def test_another_scope_or_tier_is_not_a_copy(store) -> None:
    _consolidate(store, [{"rule": RULE}], mode="enforce", n=1)
    _consolidate(
        store,
        [{"rule": RULE_CASE, "repo_scope": "org/repo"}, {"rule": RULE_CASE, "applies": "on_topic"}],
        mode="enforce",
        n=2,
    )
    assert len(_live(store)) == 3


def test_a_copy_of_a_user_taught_rule_leaves_that_rule_as_it_was(store) -> None:
    store.write_lesson(RULE, source="user_explicit")
    before = _live(store)
    _consolidate(store, [{"rule": RULE_CASE}], mode="enforce", n=1)
    assert _live(store) == before


# The vector step


def _embedder(store, table: dict[str, list[float] | None]) -> None:
    store.embed_fn = lambda text: table.get(text)


def _near_pair(store, *, mode: str, near_angle: float = 0.1) -> tuple[str, str]:
    """A stored lesson with a vector, then a new near-copy waiting for its vector."""
    _embedder(store, {RULE: _vec(0), RULE_NEAR: _vec(near_angle)})
    _consolidate(store, [{"rule": RULE}], mode="off", n=1)
    store.backfill_missing_embeddings(pace=False)
    _consolidate(store, [{"rule": RULE_NEAR}], mode=mode, n=2)
    return _lesson_key(RULE), _lesson_key(RULE_NEAR)


def test_enforce_retires_the_new_near_copy_and_leaves_the_leader(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    leader_before = _live(store)[old]
    store.backfill_missing_embeddings(pace=False)
    assert list(_live(store)) == [old]
    assert _live(store)[old] == leader_before
    assert _counts(store, "enforce")["vector_folds"] == 1
    assert mld._kind_log._loads_list(mld._kind_log._read(store.db, mld.PENDING_KEY)) == []


def test_turning_the_flag_off_restores_the_retired_row(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    store.backfill_missing_embeddings(pace=False)
    assert new not in _live(store)
    _mode(monkeypatch, "off")
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}
    assert json.loads(_live(store)[new]["value_json"])["rule"] == RULE_NEAR
    with store._db_lock:
        latest = store.db.execute(
            "SELECT source, operation FROM memory_revisions WHERE record_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (f"key:{new}",),
        ).fetchone()
    assert tuple(latest) == (mld.RETIRE_SOURCE, "restore")
    assert mld._kind_log._read(store.db, mld.RETIRED_KEY) is None
    assert mld.restore_retired(store) == []  # already restored


def test_nothing_changes_while_automatic_writes_are_off(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    monkeypatch.setattr(mld, "automatic_writes_off", lambda: True)
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}
    pending = mld._kind_log._loads_list(mld._kind_log._read(store.db, mld.PENDING_KEY))
    assert [p["key"] for p in pending] == [new]
    monkeypatch.setattr(mld, "automatic_writes_off", lambda: False)
    store.backfill_missing_embeddings(pace=False)
    assert list(_live(store)) == [old]


def test_a_row_queued_during_the_receipt_scan_stays_queued(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    _near_pair(store, mode="enforce")
    scan = mld._served_keys

    def scan_while_a_consolidation_queues(*args, **kwargs):
        with store._db_lock:
            store.db.execute("BEGIN IMMEDIATE")
            mld.remember_pending_in_transaction(store.db, ["lesson.late"])
            store.db.commit()
        return scan(*args, **kwargs)

    monkeypatch.setattr(mld, "_served_keys", scan_while_a_consolidation_queues)
    store.backfill_missing_embeddings(pace=False)
    pending = mld._kind_log._loads_list(mld._kind_log._read(store.db, mld.PENDING_KEY))
    assert [p["key"] for p in pending] == ["lesson.late"]


def test_rollback_restores_a_row_a_later_proposal_left_unchanged(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    store.backfill_missing_embeddings(pace=False)
    assert new not in _live(store)
    # A later consolidation re-extracts the retired rule: the tombstone gets a
    # pending conflict proposal, which changes nothing stored.
    _consolidate(store, [{"rule": RULE_NEAR}], mode="enforce", n=3)
    with store._db_lock:
        statuses = [
            r[0]
            for r in store.db.execute(
                "SELECT status FROM memory_revisions WHERE record_id = ? ORDER BY id",
                (f"key:{new}",),
            )
        ]
    assert statuses[-1] == "conflict"
    _mode(monkeypatch, "off")
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}


def test_rollback_skips_a_row_a_later_change_touched(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    _, new = _near_pair(store, mode="enforce")
    store.backfill_missing_embeddings(pace=False)
    with store._db_lock:
        store.db.execute("BEGIN IMMEDIATE")
        before = dict(
            store.db.execute("SELECT * FROM semantic_memory WHERE key=?", (new,)).fetchone()
        )
        store.db.execute(
            f"UPDATE {store._sem_rel} SET value_json = ? WHERE key = ?{store._sem_guard}",
            (json.dumps({"rule": "edited", "category": "knowledge", "negative": None}), new),
        )
        store._record_mutation("directive", new, before, "user_explicit", operation="update")
        store.db.commit()
    assert mld.restore_retired(store) == []
    assert new not in _live(store)


def test_shadow_counts_the_retirement_and_retires_nothing(store, monkeypatch) -> None:
    _mode(monkeypatch, "shadow")
    old, new = _near_pair(store, mode="shadow")
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}
    ids = [v for k, v in _meta(store).items() if k.startswith(mld.IDS_PREFIX)][0]
    assert {"key": new, "leader": old, "step": "vector", "rules": 1} in ids


def test_a_vector_below_the_threshold_keeps_the_row(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce", near_angle=math.acos(0.9))
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}


def test_a_failed_embed_keeps_the_row_pending(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    _embedder(store, {RULE: _vec(0)})  # the new rule's embed fails
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}
    pending = mld._kind_log._loads_list(mld._kind_log._read(store.db, mld.PENDING_KEY))
    assert [p["key"] for p in pending] == [new]


def test_a_pending_row_expires_without_a_vector(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    later = datetime.now(timezone.utc) + timedelta(seconds=mld.PENDING_MAX_AGE_SECS + 60)
    assert mld.resolve_pending(store, now=later) == 0
    assert set(_live(store)) == {old, new}
    assert mld._kind_log._read(store.db, mld.PENDING_KEY) is None


def test_an_unrecorded_embedding_space_never_matches(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    with store._db_lock:
        store.db.execute("DELETE FROM memory_meta WHERE key = 'embedding_space_sig'")
        store.db.commit()
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}


def test_a_served_row_is_never_retired(store, monkeypatch, tmp_path) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    line = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "memory_store": "member-alice",
        "lessons": [new],
    }
    injection_receipts.write_line(line)
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}
    assert _counts(store, "enforce")["served"] == 1


def test_unreadable_receipts_count_as_served(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    monkeypatch.setattr(mld, "RECEIPT_BYTES_CAP", -1)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    injection_receipts.write_line({"ts": now, "memory_store": "x"})
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}


def test_a_user_taught_row_is_never_retired(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    _embedder(store, {RULE: _vec(0), RULE_NEAR: _vec(0.05)})
    _consolidate(store, [{"rule": RULE}], mode="off", n=1)
    store.write_lesson(RULE_NEAR, source="user_explicit")
    store.backfill_missing_embeddings(pace=False)
    taught = _lesson_key(RULE_NEAR)
    # Even queued by hand, the person's row is a leader, never folded.
    with store._db_lock:
        store.db.execute("BEGIN IMMEDIATE")
        mld.remember_pending_in_transaction(store.db, [taught])
        store.db.commit()
    store.backfill_missing_embeddings(pace=False)
    assert taught in _live(store)


def test_turning_the_flag_off_drops_the_queue(store, monkeypatch) -> None:
    old, new = _near_pair(store, mode="shadow")
    _mode(monkeypatch, "off")
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}
    assert mld._kind_log._read(store.db, mld.PENDING_KEY) is None


# Caps


def test_ids_stop_at_the_daily_cap(store) -> None:
    log = mld.DedupLog("shadow")
    for i in range(mld.IDS_PER_DAY + 30):
        log.fold("text", f"lesson.{i}", "lesson.x")
    assert mld.record(store, log, now=_T0)
    ids = _meta(store)[mld.IDS_PREFIX + _T0.date().isoformat()]
    assert len(ids) == mld.IDS_PER_DAY
    assert _meta(store)[mld.COUNTS_PREFIX + _T0.date().isoformat()]["shadow"]["text_folds"] == (
        mld.IDS_PER_DAY + 30
    )


def test_ids_stop_after_the_shadow_window(store) -> None:
    first = mld.DedupLog("shadow")
    first.fold("text", "lesson.a", "lesson.x")
    mld.record(store, first, now=_T0)
    late = mld.DedupLog("shadow")
    late.fold("text", "lesson.b", "lesson.x")
    mld.record(store, late, now=_T0 + timedelta(seconds=mld._kind_log.SHADOW_SAMPLE_WINDOW_SECS))
    assert all("lesson.b" not in json.dumps(v) for k, v in _meta(store).items() if "ids" in k)


def test_enforce_logs_counts_only(store) -> None:
    log = mld.DedupLog("enforce")
    log.fold("vector", "lesson.a", "lesson.x")
    mld.record(store, log, now=_T0)
    assert not any(k.startswith(mld.IDS_PREFIX) for k in _meta(store))


def test_old_counter_rows_are_pruned(store) -> None:
    log = mld.DedupLog("shadow")
    log.count("checked")
    mld.record(store, log, now=_T0)
    mld.record(store, log, now=_T0 + timedelta(days=mld._kind_log.COUNTER_DAYS_KEPT + 1))
    assert mld.COUNTS_PREFIX + _T0.date().isoformat() not in _meta(store)


def test_the_pending_list_is_capped(store) -> None:
    with store._db_lock:
        store.db.execute("BEGIN IMMEDIATE")
        mld.remember_pending_in_transaction(
            store.db, [f"lesson.{i}" for i in range(mld.PENDING_CAP + 5)]
        )
        store.db.commit()
    pending = mld._kind_log._loads_list(mld._kind_log._read(store.db, mld.PENDING_KEY))
    assert len(pending) == mld.PENDING_CAP
    assert pending[-1]["key"] == f"lesson.{mld.PENDING_CAP + 4}"


# The default store's band count


A_RULE = "Prefer ripgrep for searching code"
B_RULE = "Use rg when scanning sources"


def _v1(tmp_path) -> VectorMemoryStore:
    v1 = VectorMemoryStore(db_path=tmp_path / "v1.db", embedding_dim=4)
    v1.init()
    v1.embed_fn = {A_RULE: _vec(0), B_RULE: _vec(math.acos(0.9))}.get
    return v1


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_default_store_counts_merges_below_the_shared_threshold(
    tmp_path, monkeypatch, mode
) -> None:
    _mode(monkeypatch, mode)
    v1 = _v1(tmp_path)
    try:
        v1.write_lesson(A_RULE, source="consolidation")
        v1.write_lesson(B_RULE, source="consolidation")
        # The merge itself is unchanged: newest wins at > 0.85.
        assert [json.loads(r["value_json"])["rule"] for r in v1.get_lessons()] == [B_RULE]
        assert _counts(v1, mode) == {"default_cosine_merges": 1, "default_below_same_item": 1}
    finally:
        v1.close()


def test_default_store_logs_nothing_when_off(tmp_path, monkeypatch) -> None:
    _mode(monkeypatch, "off")
    v1 = _v1(tmp_path)
    try:
        v1.write_lesson(A_RULE, source="consolidation")
        v1.write_lesson(B_RULE, source="consolidation")
        assert len(v1.get_lessons()) == 1
        assert _meta(v1) == {}
    finally:
        v1.close()


def test_a_flag_off_restore_runs_without_new_work_or_a_model(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    store.backfill_missing_embeddings(pace=False)
    assert new not in _live(store)
    # While the flag is on, the retirement marker is no work for the repair loop.
    assert not store.has_pending_embeddings()
    _mode(monkeypatch, "off")
    # Every live row has its vector, yet with the flag off the restore is work.
    assert store.has_pending_embeddings()
    store.embed_fn = None
    store.backfill_missing_embeddings(pace=False)
    assert set(_live(store)) == {old, new}
    assert not store.has_pending_embeddings()


def test_the_gateway_rolls_back_before_any_model_is_ready(store, monkeypatch) -> None:
    import threading
    from types import SimpleNamespace

    from kiro_crew.slack.gateway import GatewayOrchestrator

    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    store.backfill_missing_embeddings(pace=False)
    assert new not in _live(store)
    _mode(monkeypatch, "off")
    gateway = object.__new__(GatewayOrchestrator)
    gateway._memory_repair_stop = threading.Event()
    gateway._memory_repair_cursor = ""
    gateway._auto_migrate_task = None
    gateway.vector_memory = None
    monkeypatch.setattr(
        "kiro_crew.context.cached_vector_store_entries", lambda: (("member-alice", store),)
    )
    monkeypatch.setattr("kiro_crew.memory_stores.require_memory_store", lambda name: name)
    monkeypatch.setattr("kiro_crew.slack.gateway.peek_ready_shared_embedder", lambda: None)
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.reembed_progress", lambda: SimpleNamespace(is_active=lambda: False)
    )
    gateway._repair_member_memory_once()
    assert set(_live(store)) == {old, new}


def test_rollback_waits_while_the_flag_is_on(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    _, new = _near_pair(store, mode="enforce")
    store.backfill_missing_embeddings(pace=False)
    mld.rollback_if_off(store)
    assert new not in _live(store)


def _expire(store, key: str) -> None:
    with store._db_lock:
        store.db.execute("BEGIN IMMEDIATE")
        before = dict(
            store.db.execute("SELECT * FROM semantic_memory WHERE key=?", (key,)).fetchone()
        )
        store._record_mutation(
            "directive",
            key,
            before,
            "user_explicit",
            metadata={
                "valid_from": "2000-01-01T00:00:00+00:00",
                "valid_until": "2001-01-01T00:00:00+00:00",
            },
        )
        store.db.commit()


def test_an_expired_lesson_is_not_a_text_leader(store) -> None:
    _consolidate(store, [{"rule": RULE}], mode="enforce", n=1)
    _expire(store, _lesson_key(RULE))
    receipt = _consolidate(store, [{"rule": RULE_CASE}], mode="enforce", n=2)
    assert "lessons_merged" not in receipt
    assert _lesson_key(RULE_CASE) in _live(store)


def test_an_expired_lesson_is_not_a_vector_leader(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    _expire(store, old)
    store.backfill_missing_embeddings(pace=False)
    assert new in _live(store)


def test_the_repair_probe_goes_quiet_once_the_queue_is_settled(store, monkeypatch) -> None:
    _mode(monkeypatch, "shadow")
    _near_pair(store, mode="shadow")
    store.backfill_missing_embeddings(pace=False)
    # Every queued row was checked and every vector is filled: no work is left.
    assert mld._kind_log._read(store.db, mld.PENDING_KEY) is None
    assert not store.has_pending_embeddings()


def test_an_empty_queue_row_is_dropped_when_the_flag_is_off(store, monkeypatch) -> None:
    with store._db_lock:
        store.db.execute("BEGIN IMMEDIATE")
        mld._kind_log._upsert(store.db, mld.PENDING_KEY, "[]", _T0.isoformat())
        store.db.commit()
    assert store.has_pending_embeddings()
    _mode(monkeypatch, "off")
    store.backfill_missing_embeddings(pace=False)
    assert not store.has_pending_embeddings()


def _break_scope(store, key: str) -> None:
    with store._db_lock:
        row = store.db.execute(
            "SELECT value_json FROM semantic_memory WHERE key=?", (key,)
        ).fetchone()
        value = {**json.loads(row["value_json"]), "repo_scope": ["not", "a", "scope"]}
        store.db.execute(
            f"UPDATE {store._sem_rel} SET value_json = ? WHERE key = ?{store._sem_guard}",
            (json.dumps(value), key),
        )
        store.db.commit()


def test_a_lesson_with_an_unusable_scope_is_not_a_text_leader(store) -> None:
    _consolidate(store, [{"rule": RULE}], mode="enforce", n=1)
    _break_scope(store, _lesson_key(RULE))
    receipt = _consolidate(store, [{"rule": RULE_CASE}], mode="enforce", n=2)
    assert "lessons_merged" not in receipt
    assert _lesson_key(RULE_CASE) in _live(store)


def test_a_lesson_with_an_unusable_scope_is_not_a_vector_leader(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    _break_scope(store, old)
    store.backfill_missing_embeddings(pace=False)
    assert new in _live(store)


# Shadow writes exactly what off writes


def _frozen_clocks(monkeypatch) -> None:
    """Restart every store clock at the same instant, so two runs stamp alike."""
    from kiro_crew import memory_record_metadata, vector_memory

    ticks = iter(range(1, 1_000_000))

    def stamp() -> str:
        return (_T0 + timedelta(microseconds=next(ticks))).isoformat()

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return datetime.fromisoformat(stamp())

    monkeypatch.setattr(vector_memory, "_now_iso", stamp)
    monkeypatch.setattr(memory_record_metadata, "datetime", _Clock)


def _dump(db_path) -> list[str]:
    """Every row the store holds, except the dedup log, as SQL text."""
    import sqlite3

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [
            line
            for line in db.iterdump()
            if not ("memory_meta" in line and mld._META_PREFIX in line)
        ]
    finally:
        db.close()


def _member_run(tmp_path, monkeypatch, mode: str):
    # The helper copies one cached template database, made on first use, so
    # provisioning stays outside the frozen clocks both runs restart.
    directory = declare_v2_store(tmp_path / mode, "member-alice")
    _frozen_clocks(monkeypatch)
    _mode(monkeypatch, mode)
    path = directory / "memory.db"
    run = open_member_database(path, member_id="alice", store_id="member-alice", embedding_dim=4)
    try:
        run.reconcile_embedding_space(_SIG)
        _embedder(run, {RULE: _vec(0), RULE_CASE: _vec(0), RULE_NEAR: _vec(0.1)})
        _consolidate(run, [{"rule": RULE}], mode=mode, n=1)
        run.backfill_missing_embeddings(pace=False)
        # A textual copy, then a near-copy the backfill step checks.
        _consolidate(run, [{"rule": RULE_CASE}], mode=mode, n=2)
        _consolidate(run, [{"rule": RULE_NEAR}], mode=mode, n=3)
        run.backfill_missing_embeddings(pace=False)
    finally:
        run.close()
    return path


def test_member_shadow_writes_are_byte_identical_to_off(tmp_path, monkeypatch) -> None:
    from kiro_crew import memory_stores

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    monkeypatch.setattr(injection_receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    monkeypatch.setattr(mld, "automatic_writes_off", lambda: False)
    off = _dump(_member_run(tmp_path, monkeypatch, "off"))
    shadow_path = _member_run(tmp_path, monkeypatch, "shadow")
    assert _dump(shadow_path) == off
    # Shadow did decide something: the log holds a text fold and a vector fold.
    import sqlite3

    db = sqlite3.connect(f"file:{shadow_path}?mode=ro", uri=True)
    try:
        logged = " ".join(
            str(v)
            for (v,) in db.execute(
                "SELECT value FROM memory_meta WHERE key GLOB ?", (mld.IDS_PREFIX + "*",)
            )
        )
    finally:
        db.close()
    assert '"step": "text"' in logged and '"step": "vector"' in logged


def _default_run(tmp_path, monkeypatch, mode: str):
    _frozen_clocks(monkeypatch)
    _mode(monkeypatch, mode)
    path = tmp_path / f"default-{mode}.db"
    v1 = VectorMemoryStore(db_path=path, embedding_dim=4)
    v1.init()
    try:
        v1.embed_fn = {A_RULE: _vec(0), B_RULE: _vec(math.acos(0.9))}.get
        v1.write_lesson(A_RULE, source="consolidation")
        v1.write_lesson(B_RULE, source="consolidation")
    finally:
        v1.close()
    return path


def test_default_store_shadow_writes_are_byte_identical_to_off(tmp_path, monkeypatch) -> None:
    off = _dump(_default_run(tmp_path, monkeypatch, "off"))
    shadow_path = _default_run(tmp_path, monkeypatch, "shadow")
    assert _dump(shadow_path) == off
    assert _counts(_reopen(shadow_path), "shadow")["default_cosine_merges"] == 1


def _reopen(path) -> VectorMemoryStore:
    reopened = VectorMemoryStore(db_path=path, embedding_dim=4)
    reopened.init()
    return reopened


VOLATILE_CLAUSE = "current model is unavailable"


def _make_volatile(store, key: str) -> None:
    with store._db_lock:
        row = store.db.execute(
            "SELECT value_json FROM semantic_memory WHERE key=?", (key,)
        ).fetchone()
        value = {**json.loads(row["value_json"]), "negative": VOLATILE_CLAUSE}
        store.db.execute(
            f"UPDATE {store._sem_rel} SET value_json = ? WHERE key = ?{store._sem_guard}",
            (json.dumps(value), key),
        )
        store.db.commit()


def test_a_lesson_the_renderer_withholds_is_not_a_text_leader(store) -> None:
    _consolidate(store, [{"rule": RULE}], mode="enforce", n=1)
    _make_volatile(store, _lesson_key(RULE))
    receipt = _consolidate(store, [{"rule": RULE_CASE}], mode="enforce", n=2)
    assert "lessons_merged" not in receipt
    assert _lesson_key(RULE_CASE) in _live(store)


def test_a_lesson_the_renderer_withholds_is_not_a_vector_leader(store, monkeypatch) -> None:
    _mode(monkeypatch, "enforce")
    old, new = _near_pair(store, mode="enforce")
    _make_volatile(store, old)
    store.backfill_missing_embeddings(pace=False)
    assert new in _live(store)


def test_the_leader_pool_is_what_the_rule_block_serves(store) -> None:
    from kiro_crew.vector_memory_runtime import lessons as _lessons

    _consolidate(store, [{"rule": RULE}, {"rule": RULE_NEAR}], mode="off", n=1)
    _make_volatile(store, _lesson_key(RULE_NEAR))
    with store._db_lock:
        rows = store.db.execute(mld._LIVE_LESSONS_SQL).fetchall()
    served = {
        r["key"]
        for r in store._eligible_rows(rows, "directive")
        if _lessons._renderable_lesson_text(json.loads(r["value_json"]), r["key"])
    }
    assert {r["key"] for r in mld.servable_lessons(store, rows)} == served == {_lesson_key(RULE)}
