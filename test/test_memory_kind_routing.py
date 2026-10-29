"""Consolidation tags each ``project.*`` write with a ``kind``, and code routes by it.

Pins the kind parser (missing and unknown read as ``fact``), the exemptions
(``pref.*``, ``user.*``, ``lesson.*``, deletes, user-stated writes), that
``shadow`` writes exactly what ``off`` writes, ``enforce`` routing on both the V1
writer and the member store, and every cap on the shadow log.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import memory_kind_routing as mkr
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.history import HistoryConsolidator
from kiro_crew.history_consolidation_span import _PersistenceDisabledMidRun
from kiro_crew.vector_memory import (
    _AUDITABLE_REJECT_CODES,
    SemanticRejectCode,
    VectorMemoryStore,
    open_member_database,
)


def _kind_log(store) -> dict:
    """Every ``memory_kind_log:*`` row, decoded."""
    with store._db_lock:
        rows = store.db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB 'memory_kind_log:*' ORDER BY key"
        ).fetchall()
    out: dict = {}
    for key, raw in rows:
        try:
            out[str(key)] = json.loads(raw)
        except ValueError:
            out[str(key)] = raw
    return out


_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _answer() -> dict:
    return {
        "semantic": [
            {
                "key": "project.app.auth_design",
                "value": "tokens live in the OS keychain",
                "confidence": 0.9,
                "kind": "fact",
            },
            {
                "key": "project.app.release_cut",
                "value": "cut 2.4 after the freeze",
                "confidence": 0.9,
                "kind": "event",
            },
            {"key": "project.app.ci_status", "value": "green", "confidence": 0.9, "kind": "state"},
            {"key": "project.app.untagged", "value": "kept as a fact", "confidence": 0.9},
            {"key": "pref.editor", "value": "vim", "confidence": 0.9, "kind": "state"},
            {"key": "user.name", "value": "Sam", "confidence": 0.9, "kind": "event"},
        ],
        "episodic": [],
    }


# --- the parser ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, kind",
    [
        ("fact", "fact"),
        ("event", "event"),
        (" STATE ", "state"),
        (None, "fact"),
        ("", "fact"),
        ("status", "fact"),
        (3, "fact"),
        (["state"], "fact"),
    ],
)
def test_kind_parses_and_unknown_reads_as_fact(raw, kind) -> None:
    item = {"key": "project.x", "value": 1}
    if raw is not None:
        item["kind"] = raw
    assert mkr.item_kind(item) == kind


def test_an_old_answer_without_kind_routes_as_today() -> None:
    old = {"semantic": [{"key": "project.x.y", "value": "z", "confidence": 0.9}]}
    routing = mkr.route_semantic_by_kind(old, "enforce")
    assert routing.result is old
    assert [d.kind for d in routing.decisions] == ["fact"]


@pytest.mark.parametrize(
    "item",
    [
        {"key": "pref.color", "value": "blue", "kind": "state"},
        {"key": "user.city", "value": "Oslo", "kind": "state"},
        {"key": "lesson.abc", "value": "a rule", "kind": "state"},
        {"key": "project.x", "delete": True, "kind": "state"},
        {"key": "project.x", "value": None, "kind": "state"},
        {"key": "project." + "k" * 200, "value": 1, "kind": "state"},
        {"key": "project.Bad-Key", "value": 1, "kind": "state"},
        {"key": "project.\ud800", "value": 1, "kind": "state"},
        "not a dict",
    ],
)
def test_only_project_creates_and_updates_are_routed(item) -> None:
    assert not mkr.is_routed(item)
    routing = mkr.route_semantic_by_kind({"semantic": [item]}, "enforce")
    assert routing.decisions == ()
    assert routing.result["semantic"] == [item]


@pytest.mark.parametrize(
    "raw, mode",
    [
        ("off", "off"),
        ("shadow", "shadow"),
        ("enforce", "enforce"),
        ("ENFORCE", "off"),
        (None, "off"),
        (1, "off"),
    ],
)
def test_the_setting_defaults_to_off(raw, mode) -> None:
    data = {} if raw is None else {"kind_routing": raw}
    assert _build_memory_config(data).kind_routing == mode


def test_work_state_is_an_audited_reject_code() -> None:
    assert SemanticRejectCode.WORK_STATE.value == "work_state"
    assert SemanticRejectCode.WORK_STATE in _AUDITABLE_REJECT_CODES


# --- routing ---------------------------------------------------------------------


def test_shadow_and_off_hand_back_the_answer_itself() -> None:
    answer = _answer()
    for mode in ("off", "shadow"):
        assert mkr.route_semantic_by_kind(answer, mode).result is answer
    assert mkr.route_semantic_by_kind(answer, "off").decisions == ()
    assert [d.kind for d in mkr.route_semantic_by_kind(answer, "shadow").decisions] == [
        "fact",
        "event",
        "state",
        "fact",
    ]


def test_enforce_drops_state_and_keeps_events_as_facts() -> None:
    routing = mkr.route_semantic_by_kind(_answer(), "enforce")
    keys = [item["key"] for item in routing.result["semantic"]]
    assert keys == [
        "project.app.auth_design",
        "project.app.release_cut",
        "project.app.untagged",
        "pref.editor",
        "user.name",
    ]
    assert routing.result["episodic"] == []
    assert [d.key for d in routing.refused] == ["project.app.ci_status"]
    assert [d.kind for d in routing.decisions] == ["fact", "event", "state", "fact"]


def test_enforce_without_state_hands_back_the_answer_itself() -> None:
    answer = {"semantic": [{"key": "project.a.b", "value": "x", "kind": "event"}]}
    assert mkr.route_semantic_by_kind(answer, "enforce").result is answer


def test_a_surrogate_value_is_logged_not_fatal(tmp_path) -> None:
    store = VectorMemoryStore(db_path=tmp_path / "s.db", embedding_dim=4)
    store.init()
    try:
        routing = mkr.KindRouting(
            {}, "enforce", (mkr.KindDecision("project.a", "state", "x \ud800"),)
        )
        assert mkr.record_kind_routing(store, routing, "src", now=_T0)
        assert len(_events(store, "work_state")) == 1
    finally:
        store.close()


# --- the V1 writer -----------------------------------------------------------------


def _v1(tmp_path, name: str) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / f"{name}.db", embedding_dim=4)
    store.init()
    return store


def _consolidator(store: VectorMemoryStore) -> HistoryConsolidator:
    return HistoryConsolidator(
        log=MagicMock(), memory=MagicMock(), vector_store=store, migrated=True
    )


def _facts(store: VectorMemoryStore) -> dict:
    return {
        r["key"]: (r["value_json"], r["confidence"], r["source"]) for r in store.get_all_semantic()
    }


def _episodes(store: VectorMemoryStore) -> list[str]:
    with store._db_lock:
        rows = store.db.execute(
            "SELECT text FROM episodic_memories WHERE is_deleted=0 ORDER BY text"
        ).fetchall()
    return [r[0] for r in rows]


def _events(store: VectorMemoryStore, kind: str) -> list[tuple]:
    with store._db_lock:
        rows = store.db.execute(
            "SELECT memory_key, new_value FROM memory_events WHERE event_type=?", (kind,)
        ).fetchall()
    return [tuple(r) for r in rows]


def _with_mode(monkeypatch, mode: str) -> None:
    """Pin the mode and the one clock routing and its log read."""
    monkeypatch.setattr(mkr, "configured_kind_routing_mode", lambda: mode)
    monkeypatch.setattr(mkr, "_utcnow", lambda: _T0)


def test_v1_shadow_writes_exactly_what_off_writes(tmp_path, monkeypatch) -> None:
    written = {}
    for mode in ("off", "shadow"):
        _with_mode(monkeypatch, mode)
        store = _v1(tmp_path, mode)
        try:
            _consolidator(store)._write_structured_memory(_answer(), key="s1")
            written[mode] = (_facts(store), _episodes(store), _events(store, "work_state"))
            log = _kind_log(store)
        finally:
            store.close()
        if mode == "off":
            assert log == {}
        else:
            counts = log[mkr.COUNTS_PREFIX + "2026-10-01"]
            assert counts == {"shadow": {"event": 1, "fact": 2, "state": 1}}
    assert written["shadow"] == written["off"]
    assert "project.app.ci_status" in written["shadow"][0]


def test_v1_enforce_skips_state_and_writes_events_as_facts(tmp_path, monkeypatch) -> None:
    _with_mode(monkeypatch, "enforce")
    store = _v1(tmp_path, "enforce")
    try:
        _consolidator(store)._write_structured_memory(_answer(), key="s1")
        facts = _facts(store)
        assert "project.app.ci_status" not in facts
        assert {
            "project.app.auth_design",
            "project.app.release_cut",
            "project.app.untagged",
            "pref.editor",
            "user.name",
        } <= set(facts)
        assert _episodes(store) == []
        assert _events(store, "work_state") == [("project.app.ci_status", "green")]
        assert store.get_rejection_stats().get("work_state") == 1
        log = _kind_log(store)
        assert log[mkr.COUNTS_PREFIX + "2026-10-01"] == {
            "enforce": {"event": 1, "fact": 2, "state": 1}
        }
        assert not any(k.startswith(mkr.SAMPLES_PREFIX) for k in log), "enforce keeps no samples"
    finally:
        store.close()


def test_a_user_stated_write_is_never_routed(tmp_path, monkeypatch) -> None:
    _with_mode(monkeypatch, "enforce")
    store = _v1(tmp_path, "user")
    try:
        assert (
            store.set_semantic(
                key="project.app.ci_status", value="green", confidence=1.0, source="user_explicit"
            )
            is None
        )
        assert "project.app.ci_status" in _facts(store)
        assert _kind_log(store) == {}
    finally:
        store.close()


def test_v1_log_takes_the_publication_hold(tmp_path, monkeypatch) -> None:
    """A state-only enforce answer writes nothing else, so the log alone meets the fence."""
    import kiro_crew.history_consolidation as hc

    _with_mode(monkeypatch, "enforce")
    monkeypatch.setattr(hc, "_persistence_disabled", lambda: True)
    store = _v1(tmp_path, "fence")
    answer = {
        "semantic": [{"key": "project.app.ci", "value": "red", "confidence": 0.9, "kind": "state"}]
    }
    try:
        with pytest.raises(_PersistenceDisabledMidRun):
            _consolidator(store)._write_structured_memory(answer, key="s1")
        assert _kind_log(store) == {}
        assert _events(store, "work_state") == []
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, named", [("shadow", False), ("off", False), ("enforce", True)])
async def test_the_key_naming_rule_is_asked_only_under_enforce(monkeypatch, mode, named) -> None:
    from test_consolidation_semantic_cap import _prompt_for

    monkeypatch.setattr(mkr, "configured_kind_routing_mode", lambda: mode)
    prompt, _ = await _prompt_for([], include_history=False)
    assert ('"kind": "fact" | "event" | "state"' in prompt) is (mode != "off")
    assert ("Will this still be true in a month?" in prompt) is (mode != "off")
    assert ("never by PR, issue or round number" in prompt) is named


# --- the member store --------------------------------------------------------------


def _member(tmp_path, monkeypatch):
    from kiro_crew import memory_stores

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    directory = declare_v2_store(tmp_path, "member-alice")
    return open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=4
    )


def _apply(store, mode: str, source_id: str = "src-1") -> dict:
    routing = mkr.route_semantic_by_kind(_answer(), mode)
    return store.apply_consolidation(
        source_id=source_id,
        session_key="s1",
        source_total=1,
        result=routing.result,
        snapshot={},
        messages=[{"role": "user", "content": "hi"}],
        kind_routing=routing,
    )


def test_member_shadow_writes_what_off_writes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mkr, "_utcnow", lambda: _T0)
    written = {}
    for mode in ("off", "shadow"):
        store = _member(tmp_path / mode, monkeypatch)
        try:
            receipt = _apply(store, mode)
            written[mode] = (receipt, sorted(_facts(store)), _episodes(store))
        finally:
            store.close()
    assert written["shadow"] == written["off"]


def test_member_enforce_routes_inside_the_span_and_replay_logs_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mkr, "_utcnow", lambda: _T0)
    store = _member(tmp_path, monkeypatch)
    try:
        _apply(store, "enforce")
        _apply(store, "enforce")  # the same span again: a replay
        facts = _facts(store)
        assert "project.app.ci_status" not in facts
        assert {"project.app.auth_design", "project.app.release_cut"} <= set(facts)
        assert _episodes(store) == []
        assert _events(store, "work_state") == [("project.app.ci_status", "green")]
        assert _kind_log(store)[mkr.COUNTS_PREFIX + "2026-10-01"] == {
            "enforce": {"event": 1, "fact": 2, "state": 1}
        }
    finally:
        store.close()


# --- the caps ------------------------------------------------------------------------


def _routing(count: int, *, value: str = "v", mode: str = "shadow") -> mkr.KindRouting:
    decisions = tuple(mkr.KindDecision(f"project.k{i}", "state", value) for i in range(count))
    return mkr.KindRouting({}, mode, decisions)


def _samples(store) -> dict[str, list]:
    return {k: v for k, v in _kind_log(store).items() if k.startswith(mkr.SAMPLES_PREFIX)}


def test_samples_cap_at_fifty_a_day_and_each_value_at_200_chars(tmp_path) -> None:
    store = _v1(tmp_path, "day")
    try:
        mkr.record_kind_routing(store, _routing(30, value="x" * 500), "src", now=_T0)
        mkr.record_kind_routing(store, _routing(30), "src", now=_T0 + timedelta(hours=1))
        rows = _samples(store)
        assert list(rows) == [mkr.SAMPLES_PREFIX + "2026-10-01"]
        today = rows[mkr.SAMPLES_PREFIX + "2026-10-01"]
        assert len(today) == mkr.SAMPLES_PER_DAY == 50
        assert {len(s["value"]) for s in today[:30]} == {200}
        assert set(today[0]) == {"key", "kind", "value", "store", "ts"}
        counts = _kind_log(store)[mkr.COUNTS_PREFIX + "2026-10-01"]
        assert counts == {"shadow": {"state": 60}}, "counters count every item"
    finally:
        store.close()


def test_samples_stop_seven_days_after_shadow_first_ran(tmp_path) -> None:
    store = _v1(tmp_path, "window")
    try:
        mkr.record_kind_routing(store, _routing(3), "src", now=_T0)
        mkr.record_kind_routing(store, _routing(3), "src", now=_T0 + timedelta(days=6, hours=23))
        mkr.record_kind_routing(store, _routing(3), "src", now=_T0 + timedelta(days=7))
        mkr.record_kind_routing(store, _routing(3), "src", now=_T0 + timedelta(days=30))
        assert sorted(_samples(store)) == [
            mkr.SAMPLES_PREFIX + "2026-10-01",
            mkr.SAMPLES_PREFIX + "2026-10-08",
        ]
        log = _kind_log(store)
        assert log[mkr.COUNTS_PREFIX + "2026-10-31"] == {"shadow": {"state": 3}}
        assert log[mkr.SHADOW_STARTED_KEY] == _T0.isoformat()
    finally:
        store.close()


def test_samples_ring_holds_two_thousand_and_drops_the_oldest(tmp_path, monkeypatch) -> None:
    # Lift the 7-day stop so the ring itself is what bounds the rows.
    monkeypatch.setattr(mkr, "SHADOW_SAMPLE_WINDOW_SECS", 10**9)
    store = _v1(tmp_path, "ring")
    try:
        for day in range(45):
            mkr.record_kind_routing(store, _routing(50), "src", now=_T0 + timedelta(days=day))
        rows = _samples(store)
        assert sum(len(v) for v in rows.values()) == mkr.SAMPLES_TOTAL == 2_000
        assert min(rows) == mkr.SAMPLES_PREFIX + (_T0 + timedelta(days=5)).date().isoformat()
        assert max(rows) == mkr.SAMPLES_PREFIX + (_T0 + timedelta(days=44)).date().isoformat()
    finally:
        store.close()


def test_samples_never_pass_one_megabyte(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mkr, "SHADOW_SAMPLE_WINDOW_SECS", 10**9)
    store = _v1(tmp_path, "bytes")
    wide = "\U0001f600" * 300  # 12 bytes a character once JSON-escaped
    try:
        for day in range(45):
            mkr.record_kind_routing(
                store, _routing(50, value=wide), "src", now=_T0 + timedelta(days=day)
            )
        with store._db_lock:
            size = store.db.execute(
                "SELECT SUM(LENGTH(CAST(value AS BLOB))) FROM memory_meta WHERE key GLOB ?",
                (mkr.SAMPLES_PREFIX + "*",),
            ).fetchone()[0]
        assert 0 < size <= mkr.SAMPLE_BYTES_CAP == 1_000_000
        rows = _samples(store)
        assert sum(len(v) for v in rows.values()) < mkr.SAMPLES_TOTAL, "bytes bound first here"
        assert max(rows) == mkr.SAMPLES_PREFIX + (_T0 + timedelta(days=44)).date().isoformat()
        newest = rows[max(rows)]
        assert len(newest) == 50, "the newest day survives whole"
    finally:
        store.close()


def test_counter_rows_are_one_per_day_and_pruned_after_400_days(tmp_path) -> None:
    store = _v1(tmp_path, "counts")
    try:
        mkr.record_kind_routing(store, _routing(1), "src", now=_T0)
        mkr.record_kind_routing(store, _routing(1), "src", now=_T0 + timedelta(minutes=5))
        mkr.record_kind_routing(store, _routing(1), "src", now=_T0 + timedelta(days=401))
        counts = {k: v for k, v in _kind_log(store).items() if k.startswith(mkr.COUNTS_PREFIX)}
        assert list(counts) == [mkr.COUNTS_PREFIX + (_T0 + timedelta(days=401)).date().isoformat()]
    finally:
        store.close()


def test_off_and_empty_routings_write_nothing(tmp_path) -> None:
    store = _v1(tmp_path, "none")
    try:
        mkr.record_kind_routing(store, _routing(5, mode="off"), "src", now=_T0)
        mkr.record_kind_routing(store, _routing(0), "src", now=_T0)
        assert _kind_log(store) == {}
    finally:
        store.close()


def test_a_corrupt_log_row_is_replaced_not_fatal(tmp_path) -> None:
    store = _v1(tmp_path, "corrupt")
    try:
        store._write_meta(mkr.COUNTS_PREFIX + "2026-10-01", "not json")
        store._write_meta(mkr.SAMPLES_PREFIX + "2026-10-01", json.dumps({"x": 1}))
        mkr.record_kind_routing(store, _routing(2), "src", now=_T0)
        log = _kind_log(store)
        assert log[mkr.COUNTS_PREFIX + "2026-10-01"] == {"shadow": {"state": 2}}
        assert len(log[mkr.SAMPLES_PREFIX + "2026-10-01"]) == 2
    finally:
        store.close()
