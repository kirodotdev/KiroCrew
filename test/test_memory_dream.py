"""Dreaming in ``propose`` mode lists changes for approval and never changes a memory row.

``memory.dream`` is ``off`` (nothing scheduled, nothing read) or ``propose``: a
bounded, idle-only, resumable pass finds candidates by code, sends only the
uncertain band to the model, gates every proposal, and keeps an approval list in
``memory_meta``.
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import memory_dream as md
from kiro_crew.config.memory_sections import dream_mode_from
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.vector_memory import VectorMemoryStore

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
_SIG = "test-space"


def _vec(cos: float) -> list[float]:
    """A 4-d vector whose cosine to the x axis is *cos*."""
    angle = math.acos(cos)
    return [math.cos(angle), math.sin(angle), 0.0, 0.0]


@pytest.fixture
def store(tmp_path):
    opened = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=4)
    opened.init()
    opened.reconcile_embedding_space(_SIG)
    yield opened
    opened.close()


_tick = iter(range(1, 10_000_000))


def _put(
    store: VectorMemoryStore,
    key: str,
    value: object,
    *,
    source: str = "consolidation",
    vec: list[float] | None = None,
) -> None:
    """Insert one live row, each with a strictly later ``updated_at``."""
    stamp = (_T0 + timedelta(seconds=next(_tick))).isoformat()
    blob = struct.pack("4f", *vec) if vec is not None else None
    with store._db_lock:
        store.db.execute(
            "INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, "
            "updated_at, is_deleted, embedding) VALUES (?, ?, 0.5, ?, ?, ?, 0, ?)",
            (key, json.dumps(value), source, stamp, stamp, blob),
        )
        store.db.commit()


def _episode(store: VectorMemoryStore, n: int) -> None:
    stamp = (_T0 + timedelta(seconds=next(_tick))).isoformat()
    with store._db_lock:
        store.db.execute(
            "INSERT INTO episodic_memories (id, conversation_id, text, tags, importance, "
            "created_at, is_deleted) VALUES (?, ?, ?, '[]', 0.5, ?, 0)",
            (f"ep{n}", f"s{n}", f"episode {n}", stamp),
        )
        store.db.commit()


def _lesson(store, key: str, rule: str, *, vec=None, applies="on_topic", **extra) -> None:
    source = extra.pop("source", "consolidation")
    _put(
        store, f"lesson.{key}", {"rule": rule, "applies": applies, **extra}, source=source, vec=vec
    )


def _dump(store: VectorMemoryStore) -> dict[str, list]:
    """Every memory row table, for a before/after comparison."""
    out = {}
    with store._db_lock:
        for table in ("semantic_memory", "episodic_memories", "memory_events"):
            out[table] = [tuple(r) for r in store.db.execute(f"SELECT * FROM {table} ORDER BY 1")]
        names = [
            r[0] for r in store.db.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        ]
        for table in ("memory_revisions", "memory_record_meta"):
            if table in names:
                out[table] = [tuple(r) for r in store.db.execute(f"SELECT * FROM {table}")]
    return out


def _listing(store) -> dict:
    with store._db_lock:
        return md.read_list(store.db)


def _run(store, *, should_stop=lambda: False, merge=None, clock=None) -> md.Receipt:
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return asyncio.run(
        md.run_store(
            store,
            store_name="default",
            trigger="manual",
            should_stop=should_stop,
            merge=merge,
            now=lambda: _T0,
            **kwargs,
        )
    )


def _kinds(store) -> dict[str, list[list[str]]]:
    out: dict[str, list[list[str]]] = {}
    for p in _listing(store)["proposals"]:
        out.setdefault(p["kind"], []).append(p["keys"])
    return out


# config


@pytest.mark.parametrize(
    ("raw", "want"),
    [(None, "off"), ("propose", "propose"), ("apply", "off"), ("PROPOSE", "off"), (1, "off")],
)
def test_the_flag_defaults_off_and_unknown_reads_off(raw, want) -> None:
    assert dream_mode_from(raw) == want
    assert _build_memory_config({"dream": raw}).dream == want


# off


def test_off_reads_nothing_and_schedules_nothing(monkeypatch) -> None:
    monkeypatch.setattr(md, "configured_mode", lambda: "off")

    def boom(*_a, **_k):
        raise AssertionError("off must not read anything")

    monkeypatch.setattr(md, "_data_home", boom)
    monkeypatch.setattr(md, "open_stores", boom)
    monkeypatch.setattr(md, "person_turn_running", lambda _o: False)
    orchestrator = SimpleNamespace()
    assert asyncio.run(md.gateway_visit(orchestrator)) == 0
    assert not hasattr(orchestrator, "_dream_checked_at")


# trigger


def test_due_needs_twenty_new_lessons_or_five_sessions(store) -> None:
    for n in range(19):
        _lesson(store, f"a{n}", f"rule number {n}")
    assert not md.due(store, now=_T0)
    _lesson(store, "a19", "rule number nineteen")
    assert md.due(store, now=_T0)


def test_due_counts_sessions_since_the_last_run(store) -> None:
    for n in range(4):
        _episode(store, n)
    assert not md.due(store, now=_T0 + timedelta(days=400))
    _episode(store, 4)
    assert md.due(store, now=_T0 + timedelta(days=400))


def test_due_waits_a_day_after_a_completed_run_and_runs_once_a_day(store) -> None:
    for n in range(25):
        _lesson(store, f"a{n}", f"rule number {n}")
    _run(store)
    # The whole slice was read, so nothing is new; add twenty more.
    for n in range(25, 45):
        _lesson(store, f"a{n}", f"rule number {n}")
    assert not md.due(store, now=_T0 + timedelta(hours=23))
    assert md.due(store, now=_T0 + timedelta(hours=25))
    with store._db_lock:
        state = md.read_state(store.db)
        state["last_auto_day"] = (_T0 + timedelta(hours=25)).date().isoformat()
        store.db.execute(
            "UPDATE memory_meta SET value = ? WHERE key = ?", (json.dumps(state), md.STATE_KEY)
        )
        store.db.commit()
    assert not md.due(store, now=_T0 + timedelta(hours=25))


def test_an_automatic_run_marks_the_day(store) -> None:
    asyncio.run(
        md.run_store(
            store,
            store_name="default",
            trigger="auto",
            should_stop=lambda: False,
            merge=None,
            now=lambda: _T0,
        )
    )
    with store._db_lock:
        assert md.read_state(store.db)["last_auto_day"] == _T0.date().isoformat()


# lock and idle


def test_the_host_lock_admits_one_holder(tmp_path) -> None:
    first, second = md.HostLock(tmp_path), md.HostLock(tmp_path)
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Orchestrator(SimpleNamespace):
    """Enough of the gateway for its real turn census to run."""

    def _in_flight_work_counts(self) -> tuple[int, int]:
        from kiro_crew.slack.gateway import GatewayOrchestrator

        return GatewayOrchestrator._in_flight_work_counts(self)

    def _wheel_apply_module(self) -> None:
        return None


class _Provider:
    def __init__(self, active: bool) -> None:
        self._active = active

    def has_active_turn(self) -> bool:
        return self._active


def _orchestrator(*, slot_running=False, session_tasks=None, providers=()) -> Any:
    sessions = SimpleNamespace(active_providers=lambda: list(providers))
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(task=_Task(not slot_running))},
        sessions=sessions,
        workflow_service=None,
    )
    return _Orchestrator(
        dashboard_state=state,
        _session_tasks=session_tasks or {},
        sessions=None,
        _channel_handles={},
        subagent_mgr=None,
        _running_script_ids=set(),
        task_runner=None,
    )


def test_a_running_chat_turn_is_busy_and_an_idle_host_is_not() -> None:
    assert md.person_turn_running(_orchestrator(slot_running=True))
    assert md.person_turn_running(_orchestrator(session_tasks={"slack:c1": _Task(False)}))
    assert not md.person_turn_running(_orchestrator(providers=[_Provider(False)]))
    assert not md.person_turn_running(_orchestrator())


def test_a_turn_running_on_another_channel_stops_dreaming() -> None:
    # A Telegram (or Teams, WhatsApp, WeChat) turn writes no dashboard slot and no
    # session task; only its session's provider reports the running turn.
    assert md.person_turn_running(_orchestrator(providers=[_Provider(True)]))


def test_a_probe_that_fails_reads_as_busy() -> None:
    class Broken:
        @property
        def dashboard_state(self):
            raise RuntimeError("boom")

    assert md.person_turn_running(Broken())


def test_a_visit_takes_no_run_while_a_chat_turn_runs(tmp_path, monkeypatch, store) -> None:
    monkeypatch.setattr(md, "configured_mode", lambda: "propose")
    monkeypatch.setattr(md, "_data_home", lambda: tmp_path)
    monkeypatch.setattr(md._dedup, "automatic_writes_off", lambda: False)
    monkeypatch.setattr(md, "open_stores", lambda _o: [("default", store)])
    md.request_manual_run(tmp_path)
    busy = _orchestrator(slot_running=True)
    assert asyncio.run(md.gateway_visit(busy)) == 0
    # The request waits for an idle visit, and is used up by it.
    assert (tmp_path / md.REQUEST_FILE).exists()
    assert asyncio.run(md.gateway_visit(_orchestrator())) == 1
    assert not (tmp_path / md.REQUEST_FILE).exists()


def test_a_visit_skips_while_another_process_holds_the_lock(tmp_path, monkeypatch, store) -> None:
    monkeypatch.setattr(md, "configured_mode", lambda: "propose")
    monkeypatch.setattr(md, "_data_home", lambda: tmp_path)
    monkeypatch.setattr(md._dedup, "automatic_writes_off", lambda: False)
    monkeypatch.setattr(md, "open_stores", lambda _o: [("default", store)])
    md.request_manual_run(tmp_path)
    holder = md.HostLock(tmp_path)
    assert holder.acquire()
    try:
        assert asyncio.run(md.gateway_visit(_orchestrator())) == 0
    finally:
        holder.release()


def test_writes_off_starts_no_run(tmp_path, monkeypatch, store) -> None:
    monkeypatch.setattr(md, "configured_mode", lambda: "propose")
    monkeypatch.setattr(md, "_data_home", lambda: tmp_path)
    monkeypatch.setattr(md._dedup, "automatic_writes_off", lambda: True)
    monkeypatch.setattr(md, "open_stores", lambda _o: [("default", store)])
    md.request_manual_run(tmp_path)
    assert asyncio.run(md.gateway_visit(_orchestrator())) == 0


# scope, caps and resume


def test_a_chat_turn_stops_the_run_at_the_next_batch_and_the_next_run_resumes(store) -> None:
    for n in range(120):
        _lesson(store, f"r{n:03d}", f"distinct rule {n}")
    calls = iter([False, True])
    first = _run(store, should_stop=lambda: next(calls, True))
    assert (first.rows, first.stopped) == (md.BATCH_ROWS, "chat")
    with store._db_lock:
        state = md.read_state(store.db)
    assert state["cursor"][1] == f"lesson.r{md.BATCH_ROWS - 1:03d}"
    assert "last_completed_at" not in state
    second = _run(store)
    assert (second.rows, second.stopped) == (120 - md.BATCH_ROWS, "done")


def test_a_run_reads_at_most_its_row_cap_and_the_backlog_resumes(store) -> None:
    for n in range(md.ROWS_PER_RUN + 60):
        _put(store, f"project.topic_{chr(97 + n % 26)}{n // 26}x.note", f"value {n}")
    assert _run(store).rows == md.ROWS_PER_RUN
    assert _run(store).rows == 60
    assert _run(store).rows == 0


def test_the_wall_clock_cap_stops_the_run(store) -> None:
    for n in range(120):
        _lesson(store, f"r{n:03d}", f"distinct rule {n}")
    times = iter([0.0, 0.0, md.RUN_SECONDS + 1])
    receipt = _run(store, clock=lambda: next(times, md.RUN_SECONDS + 1))
    assert (receipt.rows, receipt.stopped) == (md.BATCH_ROWS, "time")


def test_a_row_without_a_vector_is_counted(store) -> None:
    _lesson(store, "novec", "a rule with no vector yet")
    _lesson(store, "vec", "a rule with a vector", vec=_vec(1.0))
    assert _run(store).missing_vectors == 1


# candidates


def test_an_exact_copy_and_a_near_duplicate_are_proposed_by_code(store) -> None:
    _lesson(store, "old", "Run the linter before every commit", vec=_vec(1.0))
    _lesson(store, "copy", "run the  LINTER before every commit", vec=[0.0, 0.0, 1.0, 0.0])
    _lesson(store, "far", "Deploy only on weekdays", vec=[0.0, 1.0, 0.0, 0.0])
    _lesson(store, "near", "Deploy just on weekdays", vec=[0.0, 0.97, 0.0, math.sqrt(1 - 0.97**2)])
    _run(store)
    proposals = {p["kind"]: p for p in _listing(store)["proposals"]}
    exact = proposals[md.KIND_EXACT]
    assert set(exact["keys"]) == {"lesson.old", "lesson.copy"}
    assert len(exact["changes"]) == 1 and exact["changes"][0] in exact["keys"]
    near = proposals[md.KIND_NEAR]
    assert set(near["keys"]) == {"lesson.far", "lesson.near"}
    assert all(p["gate"] == "pass" for p in proposals.values())


def _uncertain_pair(store) -> None:
    # Kept apart from the x axis the other fixtures use.
    _lesson(store, "a", "Prefer ripgrep when searching code", vec=[0.0, 1.0, 0.0, 0.0])
    _lesson(
        store,
        "b",
        "Use ripgrep for searching the code",
        vec=[0.0, 0.92, 0.0, math.sqrt(1 - 0.92**2)],
    )


def test_only_the_uncertain_band_reaches_the_model(store) -> None:
    _uncertain_pair(store)
    _lesson(store, "c", "Something else entirely", vec=[0.0, 0.0, 0.0, 1.0])
    prompts: list[str] = []

    async def merge(prompt: str) -> object:
        prompts.append(prompt)
        return {"same": True, "merged": "Prefer ripgrep for searching code"}

    receipt = _run(store, merge=merge)
    assert len(prompts) == 1 and "ripgrep" in prompts[0]
    assert receipt.model_calls == 1 and receipt.model_tokens > 0
    (proposal,) = [p for p in _listing(store)["proposals"] if p["kind"] == md.KIND_UNCERTAIN]
    assert set(proposal["keys"]) == {"lesson.a", "lesson.b"}
    assert proposal["text"] == "Prefer ripgrep for searching code"
    assert len(proposal["changes"]) == 1


def test_a_model_that_declines_proposes_nothing(store) -> None:
    _uncertain_pair(store)

    async def merge(_prompt: str) -> object:
        return {"same": False}

    receipt = _run(store, merge=merge)
    assert receipt.model_declined == 1
    assert md.KIND_UNCERTAIN not in _kinds(store)


def test_no_model_wired_skips_the_band(store) -> None:
    _uncertain_pair(store)
    receipt = _run(store, merge=None)
    assert receipt.budget_skipped == 1 and receipt.model_calls == 0


def test_the_token_cap_bounds_model_input(store, monkeypatch) -> None:
    monkeypatch.setattr(md, "MODEL_INPUT_TOKENS", 10)
    _uncertain_pair(store)

    async def merge(_prompt: str) -> object:
        raise AssertionError("over the cap")

    receipt = _run(store, merge=merge)
    assert receipt.budget_skipped == 1 and receipt.model_tokens == 0


def test_work_state_keys_are_proposed(store) -> None:
    _put(store, "project.kirocrew.pr_35231_status", "green, awaiting review")
    _put(store, "project.kirocrew.memory_rfc_next_action", "rebase onto main")
    _put(store, "project.kirocrew.frontend_path", "~/workspace/KiroCrew/website")
    _run(store)
    keys = sorted(k for ks in _kinds(store).get(md.KIND_STATE, []) for k in ks)
    assert keys == ["project.kirocrew.memory_rfc_next_action", "project.kirocrew.pr_35231_status"]


def test_numbered_key_families_are_grouped_for_review(store) -> None:
    _put(store, "project.kirocrew.r15_fixer_rule", "a")
    _put(store, "project.kirocrew.r16_fixer_rule", "b")
    _put(store, "project.kirocrew.only_one_7", "c")
    _run(store)
    (family,) = _listing(store)["proposals"]
    assert family["kind"] == md.KIND_FAMILY
    assert family["keys"] == ["project.kirocrew.r15_fixer_rule", "project.kirocrew.r16_fixer_rule"]
    assert family["changes"] == []


def test_a_legacy_rule_tier_lesson_is_proposed_on_topic(store) -> None:
    _lesson(store, "always", "Always say hello", applies="always", vec=_vec(1.0))
    _lesson(store, "untagged", "Never say goodbye", applies=None, vec=[0.0, 1.0, 0.0, 0.0])
    _lesson(store, "finding", "A finding stays", vec=[0.0, 0.0, 1.0, 0.0])
    _run(store)
    tiers = _listing(store)["proposals"]
    assert {p["keys"][0] for p in tiers} == {"lesson.always", "lesson.untagged"}
    assert all(p["kind"] == md.KIND_TIER and p["applies"] == "on_topic" for p in tiers)


# protected rows


def test_user_taught_and_user_quoted_rows_are_never_proposed_for_change(store) -> None:
    rule = "Run the linter before every commit"
    _lesson(store, "taught", rule, applies="always", source="user_explicit", vec=_vec(1.0))
    _lesson(store, "quoted", rule.lower(), applies="always", user_quote="run the linter")
    _lesson(store, "taught2", "Keep PRs small", source="user_explicit", vec=_vec(0.92))
    _put(store, "project.kirocrew.pr_1_status", "open", source="user_explicit")

    async def merge(_prompt: str) -> object:
        raise AssertionError("a protected row reached the model")

    _run(store, merge=merge)
    changed = {k for p in _listing(store)["proposals"] for k in p["changes"]}
    assert changed.isdisjoint(
        {"lesson.taught", "lesson.quoted", "lesson.taught2", "project.kirocrew.pr_1_status"}
    )


# the gate


def _row(key: str, rule: str, source: str = "consolidation", **extra) -> md.Row:
    return md.Row(key, {"rule": rule, **extra}, source, None, "t")


def test_the_gate_refuses_each_unsafe_proposal() -> None:
    a, b = _row("lesson.a", "use ripgrep to search"), _row("lesson.b", "search with ripgrep")
    taught = _row("lesson.t", "keep it", source="user_explicit")
    live = {r.key: r for r in (a, b, taught)}
    read = set(live) | {"lesson.gone"}

    def check(p: md.Proposal, claimed: set[str] | None = None) -> str | None:
        return md.gate(p, read=read, live=live, claimed=claimed or set())

    keys = ["lesson.a", "lesson.b"]
    ok = md.Proposal(md.KIND_UNCERTAIN, keys, ["lesson.b"], text="use ripgrep", group=keys)
    assert check(ok) is None
    assert check(md.Proposal(md.KIND_EXACT, ["lesson.x"], ["lesson.x"])) == md.REFUSE_UNREAD
    assert check(md.Proposal(md.KIND_EXACT, ["lesson.gone"], [])) == md.REFUSE_NOT_LIVE
    assert check(md.Proposal(md.KIND_TIER, ["lesson.t"], ["lesson.t"])) == md.REFUSE_PROTECTED
    added = md.Proposal(md.KIND_UNCERTAIN, keys, ["lesson.b"], text="use grep daily", group=keys)
    assert check(added) == md.REFUSE_NEW_WORDS
    outside = md.Proposal(md.KIND_UNCERTAIN, keys, ["lesson.b"], text="use", group=["lesson.a"])
    assert check(outside) == md.REFUSE_OUTSIDE_GROUP
    empty = md.Proposal(md.KIND_UNCERTAIN, keys, ["lesson.b"], text="", group=keys)
    assert check(empty) == md.REFUSE_EMPTY
    assert check(ok, {"lesson.a"}) == md.REFUSE_CLAIMED
    cites_taught = md.Proposal(
        md.KIND_UNCERTAIN, ["lesson.a", "lesson.t"], ["lesson.a"], text="keep", group=None
    )
    assert check(cites_taught) == md.REFUSE_PROTECTED


def test_a_merge_that_adds_a_content_word_is_refused_and_counted(store) -> None:
    _uncertain_pair(store)

    async def merge(_prompt: str) -> object:
        return {"same": True, "merged": "Prefer ripgrep or silversearcher for searching code"}

    receipt = _run(store, merge=merge)
    assert receipt.refused == {md.REFUSE_NEW_WORDS: 1}
    assert receipt.refused_ids[0]["reason"] == md.REFUSE_NEW_WORDS
    assert md.KIND_UNCERTAIN not in _kinds(store)
    assert _listing(store)["last_run"]["refused"] == {md.REFUSE_NEW_WORDS: 1}


def test_a_row_deleted_before_the_record_is_refused(store, monkeypatch) -> None:
    _lesson(store, "old", "Run the linter before every commit", vec=_vec(1.0))
    _lesson(store, "copy", "run the linter before every commit", vec=_vec(1.0))
    real = md.find_candidates

    def then_delete(*args):
        found = real(*args)
        with store._db_lock:
            store.db.execute("UPDATE semantic_memory SET is_deleted = 1 WHERE key = 'lesson.old'")
            store.db.commit()
        return found

    monkeypatch.setattr(md, "find_candidates", then_delete)
    receipt = _run(store)
    assert receipt.refused == {md.REFUSE_NOT_LIVE: 1}


# the list


def test_an_open_proposal_is_not_proposed_again(store) -> None:
    _lesson(store, "old", "Run the linter before every commit", vec=_vec(1.0))
    _lesson(store, "copy", "run the linter before every commit", vec=_vec(1.0))
    _run(store)
    with store._db_lock:
        store.db.execute(
            "UPDATE semantic_memory SET updated_at = ? WHERE key = 'lesson.copy'",
            ((_T0 + timedelta(days=9)).isoformat(),),
        )
        store.db.commit()
    _run(store)
    assert len(_listing(store)["proposals"]) == 1


def test_receipts_and_counters_hold_ids_and_counts_only(store) -> None:
    _lesson(store, "old", "Run the linter before every commit", vec=_vec(1.0))
    _lesson(store, "copy", "run the linter before every commit", vec=_vec(1.0))
    _run(store)
    with store._db_lock:
        rows = dict(
            store.db.execute(
                "SELECT key, value FROM memory_meta WHERE key GLOB 'memory_dream:*'"
            ).fetchall()
        )
    assert "linter" not in rows[md.RUNS_KEY].lower()
    day = md.COUNTS_PREFIX + _T0.date().isoformat()
    assert json.loads(rows[day]) == {"proposed_exact_copy": 1, "rows": 2, "runs": 1}


def test_the_run_log_is_capped(store) -> None:
    for _ in range(md.RUNS_KEPT + 3):
        _run(store)
    with store._db_lock:
        assert len(md.read_runs(store.db)) == md.RUNS_KEPT


# propose writes no memory row


def test_propose_never_writes_a_memory_row(store) -> None:
    _lesson(store, "old", "Run the linter before every commit", vec=_vec(1.0))
    _lesson(store, "copy", "run the linter before every commit", vec=_vec(1.0))
    _uncertain_pair(store)
    _lesson(store, "tier", "Always say hello", applies="always", vec=[0.0, 0.0, 1.0, 0.0])
    _put(store, "project.kirocrew.pr_9_status", "open")
    _put(store, "project.kirocrew.r1_rule", "a")
    _put(store, "project.kirocrew.r2_rule", "b")
    _episode(store, 1)

    async def merge(_prompt: str) -> object:
        return {"same": True, "merged": "Prefer ripgrep for searching code"}

    before = _dump(store)
    _run(store, merge=merge)
    assert len(_kinds(store)) == 5
    assert _dump(store) == before


def test_read_list_reads_a_read_only_connection(store) -> None:
    from kiro_crew._sqlite_compat import sqlite3

    _lesson(store, "old", "Run the linter before every commit", vec=_vec(1.0))
    _lesson(store, "copy", "run the linter before every commit", vec=_vec(1.0))
    _run(store)
    path = store._db_path
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        listing = md.read_list(conn)
    finally:
        conn.close()
    assert [p["kind"] for p in listing["proposals"]] == [md.KIND_EXACT]
    assert listing["last_run"]["trigger"] == "manual"


# the model step honours the run's bounds


def test_a_slow_model_call_is_cut_at_the_time_cap_and_the_batch_is_read_again(
    store, monkeypatch
) -> None:
    _uncertain_pair(store)
    bounds: list[float] = []

    async def expired(awaitable, timeout):
        # The call is bounded by what is left of the run, and that bound expires.
        bounds.append(timeout)
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(md.asyncio, "wait_for", expired)

    async def merge(_prompt: str) -> object:
        return {"same": True, "merged": "Prefer ripgrep"}

    ticks = iter([0.0, 0.0, 0.0, 100.0])
    receipt = _run(store, merge=merge, clock=lambda: next(ticks, 100.0))
    assert bounds == [md.RUN_SECONDS]
    assert (receipt.stopped, receipt.rows, receipt.model_failed) == ("time", 0, 1)
    with store._db_lock:
        assert md.read_state(store.db)["cursor"] == ["", ""]


def test_a_chat_that_starts_before_a_model_call_stops_the_run(store) -> None:
    _uncertain_pair(store)
    calls = iter([False])

    async def merge(_prompt: str) -> object:
        raise AssertionError("the chat had started")

    receipt = _run(store, should_stop=lambda: next(calls, True), merge=merge)
    assert (receipt.stopped, receipt.rows, receipt.model_calls) == ("chat", 0, 0)


def test_a_manual_request_waits_when_no_store_is_open(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "configured_mode", lambda: "propose")
    monkeypatch.setattr(md, "_data_home", lambda: tmp_path)
    monkeypatch.setattr(md._dedup, "automatic_writes_off", lambda: False)
    monkeypatch.setattr(md, "open_stores", lambda _o: [])
    md.request_manual_run(tmp_path)
    assert asyncio.run(md.gateway_visit(_orchestrator())) == 0
    assert (tmp_path / md.REQUEST_FILE).exists()


@pytest.mark.parametrize(
    ("member", "want"), [(True, {"memory_store": "member-alice"}), (False, {}), (None, None)]
)
def test_a_private_store_merges_through_its_own_session(member, want) -> None:
    seen: list[dict] = []

    async def call(prompt: str, **kwargs: Any) -> object:
        seen.append(kwargs)
        return {}

    orchestrator = SimpleNamespace(consolidator=SimpleNamespace(_call_llm=call))
    merge = md._consolidation_merge(orchestrator, "member-alice", member)
    if want is None:
        assert merge is None
        return
    assert merge is not None
    asyncio.run(merge("p"))
    assert seen == [want]


@pytest.mark.parametrize("answer", [{"same": False}, None])
def test_an_unmerged_rule_tier_pair_is_still_proposed_on_topic(store, answer) -> None:
    _lesson(store, "a", "Prefer ripgrep when searching code", applies="always", vec=[0, 1, 0, 0])
    _lesson(
        store,
        "b",
        "Use ripgrep for searching the code",
        applies="always",
        vec=[0.0, 0.92, 0.0, math.sqrt(1 - 0.92**2)],
    )

    async def merge(_prompt: str) -> object:
        return answer

    _run(store, merge=merge if answer is not None else None)
    tiers = sorted(k for ks in _kinds(store).get(md.KIND_TIER, []) for k in ks)
    assert tiers == ["lesson.a", "lesson.b"]
    assert md.KIND_UNCERTAIN not in _kinds(store)


def test_a_failing_store_does_not_stop_the_others(tmp_path, monkeypatch, store) -> None:
    monkeypatch.setattr(md, "configured_mode", lambda: "propose")
    monkeypatch.setattr(md, "_data_home", lambda: tmp_path)
    monkeypatch.setattr(md._dedup, "automatic_writes_off", lambda: False)
    monkeypatch.setattr(md, "_is_member_store", lambda _n: False)

    class Broken:
        @property
        def _db_lock(self):
            raise RuntimeError("store unavailable")

    monkeypatch.setattr(md, "open_stores", lambda _o: [("a-broken", Broken()), ("default", store)])
    md.request_manual_run(tmp_path)
    assert asyncio.run(md.gateway_visit(_orchestrator())) == 1
    assert not (tmp_path / md.REQUEST_FILE).exists()


def test_a_copy_with_its_own_not_clause_is_not_proposed(store) -> None:
    _lesson(store, "old", "Run the linter before every commit", vec=_vec(1.0), negative="skip it")
    _lesson(store, "copy", "run the linter before every commit", vec=_vec(1.0))
    _run(store)
    assert md.KIND_EXACT not in _kinds(store)


def test_an_uncertain_pair_with_a_distinct_not_clause_skips_the_model(store) -> None:
    _lesson(store, "a", "Prefer ripgrep when searching code", vec=[0.0, 1.0, 0.0, 0.0])
    _lesson(
        store,
        "b",
        "Use ripgrep for searching the code",
        vec=[0.0, 0.92, 0.0, math.sqrt(1 - 0.92**2)],
        negative="grep -r",
    )

    async def merge(_prompt: str) -> object:
        raise AssertionError("a clause-bearing pair reached the model")

    assert _run(store, merge=merge).model_calls == 0


def test_a_failed_model_call_keeps_the_watermark_and_the_next_run_asks_again(store) -> None:
    _uncertain_pair(store)
    prompts: list[str] = []

    async def failing(prompt: str) -> object:
        prompts.append(prompt)
        raise RuntimeError("not dispatched")

    first = _run(store, merge=failing)
    assert (first.rows, first.model_failed) == (0, 1)
    with store._db_lock:
        assert md.read_state(store.db)["cursor"] == ["", ""]

    async def merge(prompt: str) -> object:
        prompts.append(prompt)
        return {"same": True, "merged": "Prefer ripgrep for searching code"}

    _run(store, merge=merge)
    assert len(prompts) == 2 and prompts[0] == prompts[1]
    assert md.KIND_UNCERTAIN in _kinds(store)
