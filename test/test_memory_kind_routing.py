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
            assert log[mkr.RESERVOIR_KEY] == {
                "day": "2026-10-01",
                "rules": mkr.RULES_VERSION,
                "seen": 4,
            }
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


def test_samples_cap_at_150_a_day_and_each_value_at_200_chars(tmp_path) -> None:
    store = _v1(tmp_path, "day")
    try:
        mkr.record_kind_routing(store, _routing(100, value="x" * 500), "src", now=_T0)
        mkr.record_kind_routing(store, _routing(100), "src", now=_T0 + timedelta(hours=1))
        rows = _samples(store)
        assert list(rows) == [mkr.SAMPLES_PREFIX + "2026-10-01"]
        today = rows[mkr.SAMPLES_PREFIX + "2026-10-01"]
        assert len(today) == mkr.SAMPLES_PER_DAY == 150
        assert {len(s["value"]) for s in today if s["value"] != "v"} == {200}
        assert set(today[0]) == {"key", "kind", "value", "store", "ts", "rules"}
        assert {s["rules"] for s in today} == {mkr.RULES_VERSION}
        counts = _kind_log(store)[mkr.COUNTS_PREFIX + "2026-10-01"]
        assert counts["shadow"] == {"state": 200}, "counters count every item"
        assert counts == {"shadow": {"state": 200}}, "the counts row keeps its shape"
        assert _kind_log(store)[mkr.RESERVOIR_KEY]["seen"] == 200
    finally:
        store.close()


def test_samples_stop_fourteen_days_after_shadow_first_ran(tmp_path) -> None:
    assert mkr.SHADOW_SAMPLE_WINDOW_SECS == 14 * 86_400
    store = _v1(tmp_path, "window")
    try:
        mkr.record_kind_routing(store, _routing(3), "src", now=_T0)
        mkr.record_kind_routing(store, _routing(3), "src", now=_T0 + timedelta(days=13, hours=23))
        mkr.record_kind_routing(store, _routing(3), "src", now=_T0 + timedelta(days=14))
        mkr.record_kind_routing(store, _routing(3), "src", now=_T0 + timedelta(days=30))
        assert sorted(_samples(store)) == [
            mkr.SAMPLES_PREFIX + "2026-10-01",
            mkr.SAMPLES_PREFIX + "2026-10-15",
        ]
        log = _kind_log(store)
        assert log[mkr.COUNTS_PREFIX + "2026-10-31"] == {"shadow": {"state": 3}}
        assert log[mkr.SHADOW_STARTED_KEY] == _T0.isoformat()
    finally:
        store.close()


def test_samples_ring_holds_two_thousand_and_drops_the_oldest(tmp_path, monkeypatch) -> None:
    # Lift the 14-day stop so the ring itself is what bounds the rows.
    monkeypatch.setattr(mkr, "SHADOW_SAMPLE_WINDOW_SECS", 10**9)
    store = _v1(tmp_path, "ring")
    try:
        for day in range(45):
            mkr.record_kind_routing(store, _routing(150), "src", now=_T0 + timedelta(days=day))
        rows = _samples(store)
        assert sum(len(v) for v in rows.values()) == mkr.SAMPLES_TOTAL == 2_000
        # 2,000 = 13 whole days of 150 plus 50 left of the oldest day kept.
        assert len(rows[min(rows)]) == 50
        assert min(rows) == mkr.SAMPLES_PREFIX + (_T0 + timedelta(days=31)).date().isoformat()
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
                store, _routing(150, value=wide), "src", now=_T0 + timedelta(days=day)
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
        assert len(newest) == 150, "the newest day survives whole"
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
        assert log[mkr.COUNTS_PREFIX + "2026-10-01"]["shadow"] == {"state": 2}
        assert len(log[mkr.SAMPLES_PREFIX + "2026-10-01"]) == 2
    finally:
        store.close()


# --- the reservoir ----------------------------------------------------------------------


def _numbered(start: int, count: int) -> mkr.KindRouting:
    decisions = tuple(
        mkr.KindDecision(f"project.k{i}", "fact", str(i)) for i in range(start, start + count)
    )
    return mkr.KindRouting({}, "shadow", decisions)


def test_the_reservoir_spans_the_whole_day_and_never_passes_the_cap(tmp_path, monkeypatch) -> None:
    import random

    monkeypatch.setattr(mkr, "_RNG", random.Random(1234))
    store = _v1(tmp_path, "reservoir")
    per_pass, passes = 20, 150  # 3,000 items in one UTC day
    try:
        for n in range(passes):
            now = _T0 - timedelta(hours=11) + timedelta(minutes=4 * n)
            mkr.record_kind_routing(store, _numbered(n * per_pass, per_pass), "src", now=now)
            today = _samples(store)[mkr.SAMPLES_PREFIX + "2026-10-01"]
            assert len(today) <= mkr.SAMPLES_PER_DAY
        kept = sorted(int(s["value"]) for s in today)
        assert len(kept) == len(set(kept)) == mkr.SAMPLES_PER_DAY
        total = per_pass * passes
        tenths = [
            sum(1 for i in kept if t * total // 10 <= i < (t + 1) * total // 10) for t in range(10)
        ]
        # 15 expected in each tenth of the day; first-N sampling would put all 150 in the first.
        assert all(5 <= n <= 28 for n in tenths), tenths
        counts = _kind_log(store)[mkr.COUNTS_PREFIX + "2026-10-01"]
        assert counts["shadow"] == {"fact": total}
        assert _kind_log(store)[mkr.RESERVOIR_KEY]["seen"] == total
    finally:
        store.close()


def test_a_window_restarted_mid_day_samples_its_own_stream_evenly(tmp_path, monkeypatch) -> None:
    """Items counted before the restart do not number the new reservoir's stream."""
    import random

    monkeypatch.setattr(mkr, "_RNG", random.Random(99))
    store = _v1(tmp_path, "restart")
    legacy = [
        {"key": f"project.old{i}", "kind": "fact", "value": "x", "store": "default", "ts": "t"}
        for i in range(50)
    ]
    try:
        # Shadow under the old rules: its window long closed, 1,000 items counted today.
        store._write_meta(mkr.SHADOW_STARTED_KEY, (_T0 - timedelta(days=30)).isoformat())
        store._write_meta(mkr.COUNTS_PREFIX + "2026-10-01", json.dumps({"shadow": {"fact": 1000}}))
        store._write_meta(mkr.SAMPLES_PREFIX + "2026-10-01", json.dumps(legacy))
        per_pass, passes = 20, 100  # 2,000 items after the restart
        for n in range(passes):
            mkr.record_kind_routing(
                store, _numbered(n * per_pass, per_pass), "src", now=_T0 + timedelta(minutes=n)
            )
        today = _samples(store)[mkr.SAMPLES_PREFIX + "2026-10-01"]
        assert today[:50] == legacy, "old-rules samples are kept as they were"
        assert len(today) == mkr.SAMPLES_PER_DAY
        kept = sorted(int(s["value"]) for s in today[50:])
        assert len(kept) == 100
        total = per_pass * passes
        tenths = [
            sum(1 for i in kept if t * total // 10 <= i < (t + 1) * total // 10) for t in range(10)
        ]
        # 10 expected in each tenth; numbering from the day's 1,000 would crowd the first.
        assert all(2 <= n <= 20 for n in tenths), tenths
        counts = _kind_log(store)[mkr.COUNTS_PREFIX + "2026-10-01"]
        assert counts["shadow"] == {"fact": 1000 + total}, "counters keep going"
        assert _kind_log(store)[mkr.RESERVOIR_KEY]["seen"] == total
    finally:
        store.close()


def test_enforce_never_restarts_the_sample_window(tmp_path) -> None:
    store = _v1(tmp_path, "enforce-window")
    long_ago = (_T0 - timedelta(days=20)).isoformat()
    try:
        store._write_meta(mkr.SHADOW_STARTED_KEY, long_ago)
        mkr.record_kind_routing(store, _routing(2, mode="enforce"), "src", now=_T0)
        log = _kind_log(store)
        assert log[mkr.SHADOW_STARTED_KEY] == long_ago
        assert mkr.RULES_VERSION_KEY not in log
        assert log[mkr.COUNTS_PREFIX + "2026-10-01"] == {"enforce": {"state": 2}}
    finally:
        store.close()


def test_old_format_rows_still_read(tmp_path) -> None:
    store = _v1(tmp_path, "old")
    old_sample = {"key": "project.a", "kind": "fact", "value": "x", "store": "default", "ts": "t"}
    try:
        store._write_meta(mkr.COUNTS_PREFIX + "2026-10-01", json.dumps({"shadow": {"fact": 1}}))
        store._write_meta(mkr.SAMPLES_PREFIX + "2026-10-01", json.dumps([old_sample]))
        store._write_meta(mkr.SHADOW_STARTED_KEY, _T0.isoformat())
        store._write_meta(mkr.RULES_VERSION_KEY, str(mkr.RULES_VERSION))
        assert mkr.record_kind_routing(store, _routing(2), "src", now=_T0)
        log = _kind_log(store)
        assert log[mkr.COUNTS_PREFIX + "2026-10-01"]["shadow"] == {"fact": 1, "state": 2}
        today = log[mkr.SAMPLES_PREFIX + "2026-10-01"]
        assert today[0] == old_sample
        assert len(today) == 3
    finally:
        store.close()


# --- the rules version ----------------------------------------------------------------


def test_a_new_rules_version_restarts_the_sample_window(tmp_path) -> None:
    store = _v1(tmp_path, "rules")
    long_ago = _T0 - timedelta(days=20)
    try:
        # A log from before the version existed, its window long since closed.
        store._write_meta(mkr.SHADOW_STARTED_KEY, long_ago.isoformat())
        store._write_meta(mkr.COUNTS_PREFIX + long_ago.date().isoformat(), "{}")
        mkr.record_kind_routing(store, _routing(2), "src", now=_T0)
        log = _kind_log(store)
        assert log[mkr.RULES_VERSION_KEY] == mkr.RULES_VERSION == 2
        assert log[mkr.RULES_CHANGED_KEY] == _T0.isoformat()
        assert log[mkr.SHADOW_STARTED_KEY] == _T0.isoformat()
        assert len(log[mkr.SAMPLES_PREFIX + "2026-10-01"]) == 2
        # The same version later does not restart it again.
        later = _T0 + timedelta(days=15)
        mkr.record_kind_routing(store, _routing(2), "src", now=later)
        log = _kind_log(store)
        assert log[mkr.SHADOW_STARTED_KEY] == _T0.isoformat()
        assert log[mkr.RULES_CHANGED_KEY] == _T0.isoformat()
        assert mkr.SAMPLES_PREFIX + later.date().isoformat() not in log
        assert log[mkr.COUNTS_PREFIX + later.date().isoformat()] == {"shadow": {"state": 2}}
    finally:
        store.close()


def test_a_fresh_store_starts_its_window_at_the_first_log(tmp_path) -> None:
    store = _v1(tmp_path, "fresh")
    try:
        mkr.record_kind_routing(store, _routing(1), "src", now=_T0)
        log = _kind_log(store)
        assert log[mkr.SHADOW_STARTED_KEY] == _T0.isoformat()
        assert log[mkr.RULES_VERSION_KEY] == mkr.RULES_VERSION
    finally:
        store.close()


# --- what the store held before the pass ---------------------------------------------


def _seed(store: VectorMemoryStore, facts: dict) -> None:
    for key, value in facts.items():
        assert store.set_semantic(key=key, value=value, confidence=0.9, source="seed") is None


def test_v1_samples_say_whether_the_key_existed_and_kept_its_value(tmp_path, monkeypatch) -> None:
    _with_mode(monkeypatch, "shadow")
    store = _v1(tmp_path, "prior")
    try:
        _seed(store, {"project.app.ci_status": "green", "project.app.auth_design": "older text"})
        _consolidator(store)._write_structured_memory(_answer(), key="s1")
        samples = {s["key"]: s for s in _samples(store)[mkr.SAMPLES_PREFIX + "2026-10-01"]}
        assert (
            samples["project.app.ci_status"]["existed"],
            samples["project.app.ci_status"]["same_value"],
        ) == (True, True)
        assert (
            samples["project.app.auth_design"]["existed"],
            samples["project.app.auth_design"]["same_value"],
        ) == (True, False)
        assert (
            samples["project.app.untagged"]["existed"],
            samples["project.app.untagged"]["same_value"],
        ) == (False, False)
    finally:
        store.close()


def test_member_samples_read_the_store_before_the_span_writes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mkr, "_utcnow", lambda: _T0)
    store = _member(tmp_path, monkeypatch)
    try:
        _apply(store, "shadow")
        first = {s["key"]: s["existed"] for s in _samples(store)[mkr.SAMPLES_PREFIX + "2026-10-01"]}
        assert set(first.values()) == {False}
        _apply(store, "shadow", source_id="src-2")
        rows = _samples(store)[mkr.SAMPLES_PREFIX + "2026-10-01"]
        second = [s for s in rows[len(first) :]]
        assert second and all(s["existed"] and s["same_value"] for s in second)
    finally:
        store.close()


def test_a_sample_without_a_prior_read_leaves_the_fields_out(tmp_path) -> None:
    store = _v1(tmp_path, "noprior")
    try:
        mkr.record_kind_routing(store, _routing(1), "src", now=_T0)
        (sample,) = _samples(store)[mkr.SAMPLES_PREFIX + "2026-10-01"]
        assert "existed" not in sample and "same_value" not in sample
    finally:
        store.close()


@pytest.mark.parametrize(
    "stored, key, counted",
    [
        ({"project.app.plan": "a"}, "project.app.plan_final", True),
        ({"project.app.plan_latest": "a"}, "project.app.plan_final", True),
        ({"project.app.plan_v2": "a"}, "project.app.plan_v12", True),
        ({"project.app.plan_final": "a"}, "project.app.plan_final", False),
        ({}, "project.app.plan_final", False),
        ({"project.app.plan": "a"}, "project.app.plan_finally", False),
        ({"project.app.plan": "a"}, "project.app.plan_v1", False),
        ({"project.app.plan_extra": "a"}, "project.app.plan_new", False),
    ],
)
def test_a_sibling_suffix_key_is_counted_and_written_as_before(
    tmp_path, monkeypatch, stored, key, counted
) -> None:
    _with_mode(monkeypatch, "shadow")
    store = _v1(tmp_path, "sibling")
    try:
        _seed(store, stored)
        answer = {"semantic": [{"key": key, "value": "b", "confidence": 0.9, "kind": "fact"}]}
        _consolidator(store)._write_structured_memory(answer, key="s1")
        counts = _kind_log(store)[mkr.COUNTS_PREFIX + "2026-10-01"]
        assert counts.get(mkr.SIBLING_SUFFIX_COUNT) == ({"shadow": 1} if counted else None)
        assert counts["shadow"] == {"fact": 1}
        assert _facts(store)[key][0] == '"b"'
        assert set(_facts(store)) == set(stored) | {key}
    finally:
        store.close()


def test_capture_never_changes_what_enforce_writes(tmp_path) -> None:
    store = _v1(tmp_path, "enforce-prior")
    try:
        _seed(store, {"project.app.ci_status": "green", "project.app.release": "x"})
        routing = mkr.route_semantic_by_kind(_answer(), "enforce")
        captured = mkr.capture_prior(store, routing)
        assert captured.result is routing.result
        assert captured.decisions == routing.decisions
        assert captured.refused == routing.refused
        assert captured.prior is not None
        assert set(captured.prior.values) == {"project.app.ci_status"}
    finally:
        store.close()


def test_capture_on_an_unreadable_store_hands_the_routing_back() -> None:
    routing = mkr.route_semantic_by_kind(_answer(), "shadow")
    broken = MagicMock()
    broken.db.execute.side_effect = RuntimeError("no table")
    broken._db_lock = __import__("threading").RLock()
    assert mkr.capture_prior(broken, routing) is routing
    assert mkr.capture_prior(broken, mkr.route_semantic_by_kind(_answer(), "off")).prior is None


# --- the prompt -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_off_prompt_carries_no_kind_text(monkeypatch) -> None:
    from test_consolidation_semantic_cap import _prompt_for

    prompts = {}
    for mode in ("off", "shadow"):
        monkeypatch.setattr(mkr, "configured_kind_routing_mode", lambda m=mode: m)
        prompts[mode], _ = await _prompt_for([], include_history=False)
    assert mkr.SHADOW_KIND_RULES in prompts["shadow"]
    # Off is shadow with exactly the two kind segments taken out, and nothing else.
    stripped = prompts["shadow"].replace('"kind": "fact" | "event" | "state", ', "", 1)
    assert stripped.replace(mkr.SHADOW_KIND_RULES, "", 1) == prompts["off"]
    for phrase in ("Tag every item with kind", "pending action"):
        assert phrase not in prompts["off"]


#: The ``kind`` rules the enforce prompt carried before shadow trialled a
#: candidate, copied byte for byte. Changing them is a promotion: its own change.
_ADOPTED_KIND_RULES = (
    "Tag every item with kind. Ask two questions: Will this still be true in a month? "
    "Can this be looked up somewhere else? "
    'kind "fact" = still true in a month and not looked up elsewhere '
    "(a decision, a root cause, how something works). "
    'kind "event" = something that happened, worth a dated record. '
    'kind "state" = the current status of a PR, issue, CI run, branch, round or SHA, '
    "which GitHub, git or the work ledger already holds. "
    "pref.* and user.* items are always facts. "
)


@pytest.mark.asyncio
async def test_the_enforce_prompt_keeps_the_adopted_rules(monkeypatch) -> None:
    from test_consolidation_semantic_cap import _prompt_for

    assert mkr.ENFORCE_KIND_RULES == _ADOPTED_KIND_RULES
    prompts = {}
    for mode in ("shadow", "enforce"):
        monkeypatch.setattr(mkr, "configured_kind_routing_mode", lambda m=mode: m)
        prompts[mode], _ = await _prompt_for([], include_history=False)
    assert _ADOPTED_KIND_RULES in prompts["enforce"]
    assert mkr.CANDIDATE_STATE_RULES not in prompts["enforce"]
    # Enforce is shadow with the candidate taken out and only the key-naming rule added.
    key_naming = "Name keys by topic, never by PR, issue or round number. "
    expected = prompts["shadow"].replace(mkr.SHADOW_KIND_RULES, key_naming + _ADOPTED_KIND_RULES, 1)
    assert prompts["enforce"] == expected


def test_the_wider_state_rules_name_what_shadow_found_mistagged() -> None:
    rules = mkr.CANDIDATE_STATE_RULES
    assert rules in mkr.SHADOW_KIND_RULES and rules not in mkr.ENFORCE_KIND_RULES
    for phrase in (
        "a next step",
        "a pending action",
        "what something is blocked on",
        "a count (tests, cycles, budgets)",
        "the local path of a worktree or scratch folder",
        "a value marked final or latest that will change again",
    ):
        assert phrase in rules


@pytest.mark.parametrize(
    "stored, new, same",
    [
        ('"x"', "x", True),
        ("1", 1, True),
        ("1", True, False),
        ("1", 1.0, False),
        ('{"enabled": 0}', {"enabled": False}, False),
        ("[1, 2]", [1, True], False),
        ('{"a": [1]}', {"a": [1]}, True),
        ("not json", "not json", True),
    ],
)
def test_same_value_is_type_strict_all_the_way_down(stored, new, same) -> None:
    assert mkr._same_value(stored, new) is same
