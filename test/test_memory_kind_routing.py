"""Consolidation tags each ``project.*`` write with a ``kind``, and code routes by it.

Pins the kind parser (missing and unknown read as ``fact``), the exemptions
(``pref.*``, ``user.*``, ``lesson.*``, deletes, user-stated writes), that a
stored ``shadow`` reads as ``enforce``, that ``enforce`` asks ``off``'s
consolidation prompt and labels the routed items in a separate call, ``enforce``
routing on both the V1 writer and the member store, and the bounded counter log.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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


# the parser


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
        ("shadow", "enforce"),
        ("enforce", "enforce"),
        ("ENFORCE", "enforce"),
        (None, "enforce"),
        (1, "enforce"),
    ],
)
def test_the_setting_defaults_to_enforce(raw, mode) -> None:
    data = {} if raw is None else {"kind_routing": raw}
    assert _build_memory_config(data).kind_routing == mode


def test_work_state_is_an_audited_reject_code() -> None:
    assert SemanticRejectCode.WORK_STATE.value == "work_state"
    assert SemanticRejectCode.WORK_STATE in _AUDITABLE_REJECT_CODES


# routing


def test_off_hands_back_the_answer_itself() -> None:
    answer = _answer()
    routing = mkr.route_semantic_by_kind(answer, "off")
    assert routing.result is answer
    assert routing.decisions == ()


def test_a_stored_shadow_routes_as_the_default_enforce() -> None:
    assert mkr.route_semantic_by_kind(_answer(), "shadow") == mkr.route_semantic_by_kind(
        _answer(), "enforce"
    )


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


# the V1 writer


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


def _labels_as_tagged(answer: dict):
    """A label call answering each routed item's own ``kind`` tag, by its index."""
    routed = mkr.route_semantic_by_kind(answer, "enforce").decisions
    kinds = {str(index): decision.kind for index, decision in enumerate(routed)}

    async def ask(prompt: str) -> dict:
        return {"kinds": kinds}

    return ask


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
        assert list(log) == [mkr.COUNTS_PREFIX + "2026-10-01"], "enforce keeps counters only"
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


# the member store


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


# the counter log


def _routing(count: int, *, value: str = "v", mode: str = "enforce") -> mkr.KindRouting:
    decisions = tuple(mkr.KindDecision(f"project.k{i}", "state", value) for i in range(count))
    return mkr.KindRouting({}, mode, decisions)


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
        mkr.record_kind_routing(store, _routing(2), "src", now=_T0)
        log = _kind_log(store)
        assert log[mkr.COUNTS_PREFIX + "2026-10-01"] == {"enforce": {"state": 2}}
    finally:
        store.close()


# rows the removed shadow mode left


def test_rows_the_removed_shadow_wrote_stay_as_they_are(tmp_path) -> None:
    store = _v1(tmp_path, "old-shadow")
    old = {
        "memory_kind_log:shadow_started_at": (_T0 - timedelta(days=20)).isoformat(),
        "memory_kind_log:rules_version": "4",
        "memory_kind_log:samples:2026-10-01": json.dumps([{"key": "project.a", "kind": "fact"}]),
        mkr.COUNTS_PREFIX + "2026-10-01": json.dumps({"shadow": {"fact": 1}}),
    }
    try:
        for key, value in old.items():
            store._write_meta(key, value)
        before = {key: value for key, value in _kind_log(store).items() if "counts:" not in key}
        assert len(before) == 3
        assert mkr.record_kind_routing(store, _routing(2), "src", now=_T0)
        log = _kind_log(store)
        assert {key: value for key, value in log.items() if "counts:" not in key} == before
        # The counts row keeps its old entry beside the new one.
        assert log[mkr.COUNTS_PREFIX + "2026-10-01"] == {
            "shadow": {"fact": 1},
            "enforce": {"state": 2},
        }
    finally:
        store.close()


# what the store held before the pass


def _seed(store: VectorMemoryStore, facts: dict) -> None:
    for key, value in facts.items():
        assert store.set_semantic(key=key, value=value, confidence=0.9, source="seed") is None


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
    _with_mode(monkeypatch, "enforce")
    store = _v1(tmp_path, "sibling")
    try:
        _seed(store, stored)
        answer = {"semantic": [{"key": key, "value": "b", "confidence": 0.9, "kind": "fact"}]}
        _consolidator(store)._write_structured_memory(answer, key="s1")
        counts = _kind_log(store)[mkr.COUNTS_PREFIX + "2026-10-01"]
        assert counts.get(mkr.SIBLING_SUFFIX_COUNT) == ({"enforce": 1} if counted else None)
        assert counts["enforce"] == {"fact": 1}
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
        assert captured.prior == mkr.PriorState(())
    finally:
        store.close()


def test_capture_on_an_unreadable_store_hands_the_routing_back() -> None:
    # A sibling-suffix key, so capture has a read to fail.
    answer = {"semantic": [{"key": "project.app.plan_final", "value": "b", "confidence": 0.9}]}
    routing = mkr.route_semantic_by_kind(answer, "enforce")
    broken = MagicMock()
    broken.db.execute.side_effect = RuntimeError("no table")
    broken._db_lock = __import__("threading").RLock()
    assert mkr.capture_prior(broken, routing) is routing
    assert mkr.capture_prior(broken, mkr.route_semantic_by_kind(_answer(), "off")).prior is None


# the prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True], ids=["prefs-only", "history"])
async def test_every_mode_asks_offs_prompt_with_no_kind_text(monkeypatch, include_history) -> None:
    from test_consolidation_semantic_cap import _prompt_for

    prompts = {}
    for mode in ("off", "enforce"):
        monkeypatch.setattr(mkr, "configured_kind_routing_mode", lambda m=mode: m)
        prompts[mode], _ = await _prompt_for([], include_history=include_history)
    assert prompts["enforce"] == prompts["off"], "enforce asks off's prompt, byte for byte"
    for phrase in (
        '"kind"',
        "Tag every item with kind",
        "pending action",
        "Name keys by topic, never by the number of",
    ):
        assert phrase not in prompts["off"]


def test_the_label_call_asks_the_enforce_rules() -> None:
    assert mkr.ENFORCE_KIND_RULES in mkr.label_prompt([])


@pytest.mark.parametrize("vendor_word", ["GitHub", "PR", "pull request", "Gerrit", "Jira"])
def test_the_kind_rules_name_no_single_tracker(vendor_word) -> None:
    assert re.search(rf"\b{re.escape(vendor_word)}\b", mkr.ENFORCE_KIND_RULES) is None


def test_enforce_asks_the_wider_state_rules_and_the_mixed_item_rule() -> None:
    rules = mkr.ENFORCE_KIND_RULES
    assert mkr.WIDER_STATE_RULES in rules and mkr.MIXED_ITEM_RULE in rules
    for phrase in (
        "a code change under review (by any name or number)",
        "a ticket or task",
        "which chat or session owns it",
        "a next step",
        "a pending action",
        "what something is blocked on",
        "a count (tests, cycles, budgets)",
        "the local path of a worktree or scratch folder",
        "a value marked final or latest that will change again",
        "with every work-item number removed",
    ):
        assert phrase in rules


# the label call


_CHAT = {"role": "user", "content": "the transcript says CHAT-ONLY-TEXT"}


async def _run_pass(
    tmp_path, monkeypatch, mode: str, label=None
) -> tuple[list[str], VectorMemoryStore]:
    """One history pass on a real V1 store; returns every prompt sent, and the store.

    The first call answers :func:`_answer`; a later one goes to *label*, which
    defaults to answering each item's own tag.
    """
    from consolidation_lease_helpers import lease_aware_log

    from kiro_crew import executors

    _with_mode(monkeypatch, mode)
    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = ([_CHAT], 1, 0, 0)
    log.consolidation_retry_state.return_value = (0, 0.0)
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    store = _v1(tmp_path, mode)
    c = HistoryConsolidator(
        log=log, memory=memory, sessions=None, vector_store=store, migrated=True
    )
    prompts: list[str] = []
    ask = label or _labels_as_tagged(_answer())

    async def inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_llm(prompt: str, **_: object) -> object:
        prompts.append(prompt)
        if len(prompts) == 1:
            return _answer()
        return await ask(prompt)

    monkeypatch.setattr(executors, "run_in_memory_background_pool", inline)
    with patch.object(c, "_call_llm", side_effect=fake_llm):
        await c._consolidate("s1", include_history=True)
    return prompts, store


@pytest.mark.asyncio
async def test_enforce_asks_offs_prompt_and_labels_the_routed_items_only(
    tmp_path, monkeypatch
) -> None:
    runs = {}
    for mode in ("off", "enforce"):
        prompts, store = await _run_pass(tmp_path, monkeypatch, mode)
        try:
            runs[mode] = (prompts, _facts(store), _kind_log(store))
        finally:
            store.close()
    (off_prompt,) = runs["off"][0]
    enforce_prompt, label_prompt = runs["enforce"][0]
    assert enforce_prompt == off_prompt, "what _call_llm receives is the same, byte for byte"
    assert "project.app.ci_status" in runs["off"][1], "the pass wrote the answer"
    assert runs["off"][2] == {}
    # The label call reads the routed keys and values only.
    assert "CHAT-ONLY-TEXT" not in label_prompt
    assert mkr.ENFORCE_KIND_RULES in label_prompt
    assert "project.app.ci_status" in label_prompt and "pref.editor" not in label_prompt
    assert runs["enforce"][2][mkr.COUNTS_PREFIX + "2026-10-01"] == {
        "enforce": {"event": 1, "fact": 2, "state": 1}
    }


async def _raises(prompt: str) -> object:
    raise RuntimeError("throttled")


async def _not_dispatched(prompt: str) -> object:
    from kiro_crew.history_consolidation_span import _ConsolidationNotDispatched

    raise _ConsolidationNotDispatched("provider returned no output")


async def _unusable(prompt: str) -> object:
    return {"kinds": "not a mapping"}


async def _times_out(prompt: str) -> object:
    raise asyncio.TimeoutError  # what wait_for raises once LABEL_TIMEOUT_SECS passes


async def _labels_unlike_the_tags(prompt: str) -> dict:
    """Labels that disagree with every tag the answer carries, by routed index."""
    return {"kinds": {"0": "state", "1": "event", "2": "fact", "3": "state"}}


@pytest.mark.asyncio
async def test_enforce_drops_what_the_label_call_says_is_state(tmp_path, monkeypatch) -> None:
    _, off = await _run_pass(tmp_path / "o", monkeypatch, "off")
    _, enforce = await _run_pass(
        tmp_path / "e", monkeypatch, "enforce", label=_labels_unlike_the_tags
    )
    try:
        labelled_state = {"project.app.auth_design", "project.app.untagged"}
        assert {key for key, _ in _events(enforce, "work_state")} == labelled_state
        assert set(_facts(off)) - set(_facts(enforce)) == labelled_state
        assert "project.app.ci_status" in _facts(enforce), "the model's own tag never decides"
        assert _kind_log(enforce)[mkr.COUNTS_PREFIX + "2026-10-01"] == {
            "enforce": {"event": 1, "fact": 1, "state": 2}
        }
    finally:
        off.close()
        enforce.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label", [_raises, _not_dispatched, _unusable, _times_out], ids=lambda f: f.__name__
)
async def test_a_failed_label_call_under_enforce_writes_as_off(
    tmp_path, monkeypatch, label
) -> None:
    _, off = await _run_pass(tmp_path / "off", monkeypatch, "off")
    _, enforce = await _run_pass(tmp_path / "enforce", monkeypatch, "enforce", label=label)
    try:
        assert (_facts(enforce), _episodes(enforce)) == (_facts(off), _episodes(off))
        assert _events(enforce, "work_state") == []
    finally:
        off.close()
        enforce.close()


@pytest.mark.asyncio
async def test_enforce_labels_stamp_routed_items_only_and_leave_the_answer_alone() -> None:
    answer = _answer()
    before = json.loads(json.dumps(answer))
    prompts: list[str] = []

    async def ask(prompt: str) -> dict:
        prompts.append(prompt)
        return {"kinds": {"0": "state", "2": "bogus"}}

    out = await mkr.label_enforce_kinds(answer, answer, ask)
    assert answer == before, "the model's answer is not mutated"
    assert prompts == [mkr.label_prompt(mkr.route_semantic_by_kind(answer, "enforce").decisions)]
    assert [item.get("kind") for item in out["semantic"]] == [
        "state",
        "fact",
        "fact",
        "fact",
        "state",  # pref.* is never routed: its own tag stays and is never read
        "event",
    ]
    assert [d.key for d in mkr.route_semantic_by_kind(out, "enforce").refused] == [
        "project.app.auth_design"
    ]


@pytest.mark.asyncio
async def test_a_pass_with_nothing_routed_makes_no_label_call() -> None:
    called: list[str] = []

    async def ask(prompt: str) -> object:
        called.append(prompt)
        return {}

    answer = {"semantic": [{"key": "pref.editor", "value": "vim", "confidence": 0.9}]}
    assert await mkr.label_enforce_kinds(answer, answer, ask) is answer
    assert called == []


@pytest.mark.asyncio
async def test_the_label_prompt_is_bounded_and_one_over_the_cap_is_not_sent(monkeypatch) -> None:
    called: list[str] = []

    async def ask(prompt: str) -> object:
        called.append(prompt)
        return {"kinds": {str(i): "state" for i in range(20)}}

    # At the store's key bound (_MAX_KEY_LEN, 100), so every one is routed.
    keys = [f"project.{i:02d}" + "k" * 90 for i in range(20)]
    answer = {"semantic": [{"key": k, "value": "v" * 5_000, "confidence": 0.9} for k in keys]}
    prompt = mkr.label_prompt([mkr.KindDecision(k, "fact", "v" * 5_000) for k in keys])
    assert '"' + "v" * mkr.SAMPLE_VALUE_CHARS + '"' in prompt
    assert "v" * (mkr.SAMPLE_VALUE_CHARS + 1) not in prompt
    # Twenty items at the key and value bounds fit, so the cap never trims a real pass.
    assert mkr.label_prompt_tokens(prompt) <= mkr.LABEL_INPUT_TOKENS
    out = await mkr.label_enforce_kinds(answer, answer, ask)
    assert called == [prompt]
    assert {item["kind"] for item in out["semantic"]} == {"state"}
    monkeypatch.setattr(mkr, "LABEL_INPUT_TOKENS", mkr.label_prompt_tokens(prompt) - 1)
    out = await mkr.label_enforce_kinds(answer, answer, ask)
    assert called == [prompt], "a prompt over the cap is never sent"
    assert {item["kind"] for item in out["semantic"]} == {"fact"}, "unlabelled writes as off"


def test_labels_are_read_by_index_and_unknown_kinds_are_none() -> None:
    answer = {"kinds": {"0": " STATE ", "1": "status", "2": 1, "project.c": "fact"}}
    assert mkr.labels_by_index(answer, 3) == ["state", None, None]
    assert mkr.labels_by_index({"kinds": {}}, 2) == [None, None]
    assert mkr.labels_by_index(None, 2) is None


@pytest.mark.asyncio
async def test_a_key_named_twice_is_labelled_per_value() -> None:
    answer = {
        "semantic": [
            {"key": "project.app.ci", "value": "the design keeps CI on one runner"},
            {"key": "project.app.ci", "value": "red on main"},
        ]
    }
    prompts: list[str] = []

    async def ask(prompt: str) -> dict:
        prompts.append(prompt)
        return {"kinds": {"0": "fact", "1": "state"}}

    out = await mkr.label_enforce_kinds(answer, answer, ask)
    assert "the design keeps CI on one runner" in prompts[0] and "red on main" in prompts[0]
    assert [(item["value"], item["kind"]) for item in out["semantic"]] == [
        ("the design keeps CI on one runner", "fact"),
        ("red on main", "state"),
    ]


@pytest.mark.parametrize("unit", ["v", '"\n', "\u0001", "\\", "\u4e2d"])
def test_escaped_values_keep_twenty_items_under_the_bound(unit: str) -> None:
    value = unit * mkr.SAMPLE_VALUE_CHARS
    decisions = tuple(
        mkr.KindDecision(f"project.{i:02d}" + "k" * 96, "fact", value) for i in range(20)
    )
    prompt = mkr.label_prompt(decisions)
    assert mkr.label_prompt_tokens(prompt) <= mkr.LABEL_INPUT_TOKENS
    shown = json.loads(prompt.split("\nItems: ", 1)[1].split("\nReturn ", 1)[0])
    for item in shown:
        assert len(json.dumps(item["value"], ensure_ascii=False)) <= mkr.SAMPLE_VALUE_CHARS + 2
        assert value.startswith(item["value"]) and item["value"]
