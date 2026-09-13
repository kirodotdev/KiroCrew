"""Fairness lanes and the child reserve (RFC overload-resilience §6, §13 Q3/Q5).

Three layers, each on the real code:

* ``taskq.lanes`` -- lane keys and the smooth weighted round-robin, pure.
* ``taskq.store`` -- the ``lane`` column (schema v3, backfill), lane-fair
  dispatch queries.
* ``SubagentManager`` admission -- the pump's lane order, the child reserve at
  cap 2 on a three-level tree, the adaptive lift, and the
  event-driven resume hold behind ``spawn_sub_agents``.

Fake worker, injected store clock, tmp ``KIROCREW_HOME``; no kiro-cli, no sockets.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from overload_fakes import Clock, ManagerHarness, open_task_store

from kiro_crew.dashboard.handlers import spawn_resume
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_manager.admission import (
    CapacityView,
    FairnessSettings,
)
from kiro_crew.taskq import lanes, model
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# ── lanes.py: pure ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key,lane",
    [
        ("dash:abc", "dash:abc"),
        ("slack:C1:171", "slack:C1:171"),
        ("cron:job1", lanes.SYSTEM_LANE),
        ("cron_legacy", lanes.SYSTEM_LANE),
        ("hook:default:1", lanes.SYSTEM_LANE),
        ("webhook:x", lanes.SYSTEM_LANE),
        ("_hb", lanes.SYSTEM_LANE),
        ("_bg", lanes.SYSTEM_LANE),
        ("", lanes.SYSTEM_LANE),
        (None, lanes.SYSTEM_LANE),
        ("subagent:p1", "subagent:p1"),
    ],
)
def test_lane_key_for(key, lane) -> None:
    assert lanes.lane_key_for(key) == lane


def test_lane_weight_precedence_and_clamp() -> None:
    assert lanes.lane_weight("dash:a") == 1
    assert lanes.lane_weight(lanes.SYSTEM_LANE, system_weight=3) == 3
    assert lanes.lane_weight(lanes.SYSTEM_LANE, weights={"system": 5}, system_weight=3) == 5
    assert lanes.lane_weight("dash:a", weights={"dash:a": 0}) == 1
    assert lanes.lane_weight("dash:a", weights={"dash:a": 999}) == lanes.MAX_WEIGHT
    assert lanes.lane_weight("dash:a", weights={"dash:a": "x"}) == 1


def test_equal_weights_is_round_robin_and_carries_credit() -> None:
    s = lanes.LaneScheduler()
    picks = [s.pick(["b", "a", "system"]) for _ in range(6)]
    assert picks == ["b", "a", "system", "b", "a", "system"], "ties go to the first listed"
    # A lane that leaves is forgotten; the others keep their balance.
    s.forget(["system"])
    assert "system" not in s.credit


def test_weighted_picks_are_smooth_not_bursty() -> None:
    s = lanes.LaneScheduler(weights={"big": 3})
    picks = [s.pick(["big", "small"]) for _ in range(8)]
    assert picks.count("big") == 6 and picks.count("small") == 2
    # Smooth WRR spreads the heavy lane instead of a 3-run then 1.
    assert picks[:4] == ["big", "big", "small", "big"]


def test_interleave_is_fifo_inside_a_lane() -> None:
    s = lanes.LaneScheduler()
    out = s.interleave({"a": ["a1", "a2", "a3"], "b": ["b1"]}, limit=10)
    assert out == ["a1", "b1", "a2", "a3"]
    assert s.interleave({"a": ["a1", "a2"]}, limit=1) == ["a1"]


def test_pick_index_honours_eligibility() -> None:
    s = lanes.LaneScheduler()
    entries = [
        {"lane": "a", "child": False},
        {"lane": "a", "child": True},
        {"lane": "b", "child": False},
    ]
    idx = s.pick_index(entries, lane_of=lambda e: e["lane"], eligible=lambda e: e["child"])
    assert idx == 1
    assert s.pick_index(entries, lane_of=lambda e: e["lane"], eligible=lambda e: False) is None


# ── store: lane column + fair queries ─────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path):
    yield from open_task_store(tmp_path, Clock())


def _row(store: TaskStore, task_id: str, session: str, *, parent: str | None = None) -> None:
    store.accept_one(
        model.TaskRecord(
            id=task_id, kind=model.KIND_SUBAGENT, session_key=session, parent_id=parent
        )
    )
    store._clock.t += 1  # type: ignore[attr-defined]


def test_accept_derives_lane_root_system_and_nested(store: TaskStore) -> None:
    _row(store, "r1", "dash:1")
    _row(store, "c1", "subagent:r1", parent="r1")
    _row(store, "cr", "cron:job")
    _row(store, "hk", "hook:default:5")
    _row(store, "orphan", "subagent:gone", parent="gone")
    assert store.get("r1").lane == "dash:1"
    assert store.get("c1").lane == "dash:1", "a nested row inherits its root's lane"
    assert store.get("cr").lane == lanes.SYSTEM_LANE
    assert store.get("hk").lane == lanes.SYSTEM_LANE
    assert store.get("orphan").lane == "subagent:gone", "never guessed into another lane"
    assert "lane" in model.TaskRecord.COLUMNS


def test_fetch_dispatchable_fair_interleaves_lanes_fifo_within(store: TaskStore) -> None:
    for i in range(5):
        _row(store, f"a{i}", "dash:a")
    _row(store, "b0", "dash:b")
    _row(store, "k0", "cron:1")
    sched = lanes.LaneScheduler()
    order = [
        r.id for r in store.fetch_dispatchable_fair(model.KIND_SUBAGENT, limit=4, scheduler=sched)
    ]
    assert order == ["a0", "b0", "k0", "a1"]
    # Plain FIFO would have been a0..a3: the fair order is a different set.
    fifo = [r.id for r in store.fetch_dispatchable(model.KIND_SUBAGENT, limit=4)]
    assert fifo == ["a0", "a1", "a2", "a3"]
    assert store.pending_lanes(model.KIND_SUBAGENT) == {"dash:a": 5, "dash:b": 1, "system": 1}
    # One lane pending: exactly the FIFO order.
    excl = ["b0", "k0"]
    assert [
        r.id
        for r in store.fetch_dispatchable_fair(
            model.KIND_SUBAGENT, limit=3, scheduler=lanes.LaneScheduler(), exclude_ids=excl
        )
    ] == ["a0", "a1", "a2"]


def test_fetch_dispatchable_fair_children_only(store: TaskStore) -> None:
    _row(store, "r1", "dash:1")
    store.claim("r1")
    _row(store, "r2", "dash:1")
    _row(store, "c1", "subagent:r1", parent="r1")
    kids = store.fetch_dispatchable_fair(
        model.KIND_SUBAGENT, limit=5, scheduler=lanes.LaneScheduler(), children_only=True
    )
    assert [r.id for r in kids] == ["c1"]
    assert store.pending_lanes(model.KIND_SUBAGENT, children_only=True) == {"dash:1": 1}
    assert (
        store.fetch_dispatchable_fair(model.KIND_SUBAGENT, limit=0, scheduler=lanes.LaneScheduler())
        == []
    )


# ── manager harness ───────────────────────────────────────────────────────────


@pytest.fixture
def quiet():
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield


@pytest.fixture
def h(quiet):
    made: list[ManagerHarness] = []

    async def make(
        max_concurrent: int = 2, settings: FairnessSettings | None = None
    ) -> ManagerHarness:
        # The fairness suite always pins its settings so config cannot leak in.
        hz = ManagerHarness(max_concurrent, settings or FairnessSettings())
        made.append(hz)
        await hz.mgr.wait_taskq_ready()
        return hz

    yield make
    for hz in made:
        hz.close()


# ── starvation: 200:1 demand across two sessions plus the system lane ─────────


@pytest.mark.asyncio
async def test_small_lane_and_system_lane_get_their_turn_within_three_grants(h) -> None:
    hz = await h(max_concurrent=1)
    hz.mgr._taskq._window = 16
    first = hz.spawn("a-first", parent="dash:a")
    await hz.settle()
    big = [hz.spawn(f"a{i}", parent="dash:a") for i in range(200)]
    small = hz.spawn("b-only", parent="dash:b")
    system = hz.spawn("nightly", parent="cron:job-7")
    assert hz.state(small) == model.QUEUED and hz.state(system) == model.QUEUED
    assert hz.store.get(system.id).lane == lanes.SYSTEM_LANE
    # Grant 1 ends the running one; the pump takes the next lane in turn.
    grants: list[str] = []
    current = first
    for _ in range(3):
        await hz.end(current)
        assert hz.mgr._running_count == 1
        started_now = [i for i in hz.started if i not in grants and i != first.id]
        assert len(started_now) == 1
        grants.append(started_now[0])
        current = hz.mgr._agents[started_now[0]]
    assert small.id in grants and system.id in grants, grants
    assert sum(1 for g in grants if g in {b.id for b in big}) == 1
    # Inside the big lane the order stayed FIFO.
    big_started = [g for g in grants if g in {b.id for b in big}]
    assert big_started == [big[0].id]
    snap = hz.mgr._admission.lane_snapshot()
    assert snap["lanes"]["dash:a"]["queued"] == 199
    assert snap["lanes"][lanes.SYSTEM_LANE]["weight"] == 1


@pytest.mark.asyncio
async def test_lane_weights_shape_the_share(h) -> None:
    hz = await h(
        max_concurrent=1,
        settings=FairnessSettings(lane_weights={"dash:a": 3}),
    )
    first = hz.spawn("warm", parent="dash:z")
    await hz.settle()
    a = [hz.spawn(f"a{i}", parent="dash:a") for i in range(8)]
    b = [hz.spawn(f"b{i}", parent="dash:b") for i in range(8)]
    lanes_started: list[str] = []
    current = first
    for _ in range(8):
        await hz.end(current)
        new = [i for i in hz.started if i not in lanes_started and i != first.id]
        assert len(new) == 1
        lanes_started.append(new[0])
        current = hz.mgr._agents[new[0]]
    a_ids, b_ids = {x.id for x in a}, {x.id for x in b}
    assert sum(1 for g in lanes_started if g in a_ids) == 6
    assert sum(1 for g in lanes_started if g in b_ids) == 2
    # FIFO inside each lane.
    assert [g for g in lanes_started if g in a_ids] == [x.id for x in a[:6]]


@pytest.mark.asyncio
async def test_system_entry_in_lane_weights_is_applied(h) -> None:
    hz = await h(max_concurrent=1, settings=FairnessSettings(lane_weights={"system": 2}))
    first = hz.spawn("warm", parent="dash:z")
    await hz.settle()
    crons = [hz.spawn(f"cron{i}", parent=f"cron:{i}") for i in range(6)]
    people = [hz.spawn(f"p{i}", parent="dash:p") for i in range(6)]
    got: list[str] = []
    current = first
    for _ in range(6):
        await hz.end(current)
        new = [i for i in hz.started if i not in got and i != first.id]
        got.append(new[0])
        current = hz.mgr._agents[new[0]]
    assert sum(1 for g in got if g in {c.id for c in crons}) == 4
    assert sum(1 for g in got if g in {p.id for p in people}) == 2
    assert hz.mgr._admission.lane_scheduler().weight_of(lanes.SYSTEM_LANE) == 2


# ── child reserve: three-level tree at cap 2 with roots competing ─────────────


@pytest.mark.asyncio
async def test_child_reserve_three_level_tree_at_cap_2(h) -> None:
    hz = await h(max_concurrent=2)
    s = hz.spawn("S", parent="dash:1")
    await hz.settle()
    r1 = hz.spawn("R1", parent="dash:other")
    r2 = hz.spawn("R2", parent="dash:other")
    await hz.settle()
    assert hz.state(s) == model.RUNNING and hz.state(r1) == model.RUNNING
    assert hz.state(r2) == model.QUEUED
    # S blocks in spawn_sub_agents on A: S yields, A is a pending child, so the
    # freed slot is the RESERVE -- R2 (a root) may not take it, A does.
    hz.block_in_spawn_sub_agents(s, "call-s")
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.state(s) == model.WAITING_CHILDREN
    assert hz.state(a) == model.RUNNING, "the child took the reserved slot"
    assert hz.state(r2) == model.QUEUED, "a root never takes the last slot while a child waits"
    assert hz.mgr._running_count == 2
    # Level 3: A blocks on B; same rule one level down.
    hz.block_in_spawn_sub_agents(a, "call-a")
    b = hz.child_of(a, "B")
    await hz.settle()
    assert hz.state(a) == model.WAITING_CHILDREN
    assert hz.state(b) == model.RUNNING and hz.state(r2) == model.QUEUED
    view = hz.mgr._admission.capacity_view()
    assert view.cap_total == 2 and view.running == 2 and view.waiting_parents == 2
    # B ends: A's resume is the pending tree start, so A -- not R2 -- gets the slot.
    await hz.end(b)
    assert hz.state(a) == model.RUNNING and not hz.live(a)._slot_released
    assert hz.state(r2) == model.QUEUED
    await hz.end(a)
    assert hz.state(s) == model.RUNNING and not hz.live(s)._slot_released
    assert hz.state(r2) == model.QUEUED
    # Nothing nested pending any more: roots fill the cap again.
    await hz.end(s)
    assert hz.state(r2) == model.RUNNING
    await hz.end(r1)
    await hz.end(r2)
    assert hz.mgr._running_count == 0
    assert hz.store.count_by_state() == {model.DONE: 5}


@pytest.mark.asyncio
async def test_reserve_is_inactive_without_pending_nested_work(h) -> None:
    hz = await h(max_concurrent=2)
    s = hz.spawn("S", parent="dash:1")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.state(a) == model.RUNNING and hz.mgr._running_count == 1
    # A parent waiting while its child RUNS reserves nothing: unrelated roots
    # fill the cap (RFC §14.3, siblings and other sessions keep going).
    u = hz.spawn("U", parent="dash:other")
    await hz.settle()
    assert hz.state(u) == model.RUNNING and hz.mgr._running_count == 2
    view = hz.mgr._admission.capacity_view()
    assert view.reserve_active is False and view.roots_cap == 2


@pytest.mark.asyncio
async def test_child_reserve_zero_disables_the_rule(h) -> None:
    hz = await h(max_concurrent=1, settings=FairnessSettings(child_reserve=0))
    s = hz.spawn("S", parent="dash:1")
    await hz.settle()
    r = hz.spawn("R", parent="dash:other")
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    # With no reserve the pump is plain lane round-robin: one of the two
    # lanes got the freed slot, and roots were eligible for it.
    running = [x for x in (r, a) if hz.state(x) == model.RUNNING]
    assert len(running) == 1
    view = hz.mgr._admission.capacity_view()
    assert view.child_reserve == 0 and view.reserve_active is False


# ── the reserve under an adaptive squeeze ─────────────────────────────────────


@pytest.mark.asyncio
async def test_adaptive_squeeze_is_lifted_to_floor_plus_reserve_for_children(h) -> None:
    hz = await h(max_concurrent=4, settings=FairnessSettings(child_reserve=1, adaptive_floor=1))
    assert hz.mgr.set_effective_cap(1) == 1
    s = hz.spawn("S", parent="dash:1")
    await hz.settle()
    r = hz.spawn("R", parent="dash:other")
    await hz.settle()
    assert hz.state(s) == model.RUNNING and hz.state(r) == model.QUEUED
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    # S yielded (running 0). Both R and A are pending; the cap of 1 is lifted
    # to floor + reserve = 2 while S waits. Roots still get only the unlifted
    # cap (R takes it, being the older head); the lifted slot is the reserve,
    # which only the child may take: A starts too, so the tree progresses
    # under the squeeze instead of waiting behind R.
    assert hz.state(r) == model.RUNNING and hz.state(a) == model.RUNNING
    view = hz.mgr._admission.capacity_view()
    assert view.cap_total == 2 and view.lifted_from == 1 and view.roots_cap == 1
    assert view.running == 2 and view.root_slot is False and view.any_slot is False
    # A second root cannot use the lifted slot.
    r2 = hz.spawn("R2", parent="dash:other")
    await hz.settle()
    assert hz.state(r2) == model.QUEUED
    # Ending A wakes S; S's resume takes the reserve slot back before R2.
    await hz.end(a)
    assert hz.state(s) == model.RUNNING and not hz.live(s)._slot_released
    assert hz.state(r2) == model.QUEUED
    await hz.end(s)
    # S done: no parent waits, no lift; cap is 1 again and R holds it.
    assert hz.mgr._admission.capacity_view().cap_total == 1
    assert hz.state(r2) == model.QUEUED
    await hz.end(r)
    assert hz.state(r2) == model.RUNNING


@pytest.mark.asyncio
async def test_no_lift_when_the_cap_is_not_adaptive(h) -> None:
    hz = await h(max_concurrent=4)
    hz.mgr._max_concurrent = 1  # a user cap or a test pin, not the controller
    s = hz.spawn("S", parent="dash:1")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.state(a) == model.RUNNING
    view = hz.mgr._admission.capacity_view()
    assert view.cap_total == 1 and view.lifted_from is None


@pytest.mark.asyncio
async def test_lift_never_exceeds_the_user_ceiling(h) -> None:
    hz = await h(max_concurrent=1, settings=FairnessSettings(child_reserve=3, adaptive_floor=1))
    hz.mgr.set_effective_cap(1)
    waiting = SubagentInfo(id="p", task="p", parent_session_key="dash:1")
    waiting._slot_released = True
    waiting._wait_record = WaitRecord.children(["c"], since=0.0).to_dict()
    hz.mgr._agents["p"] = waiting
    view = hz.mgr._admission.capacity_view()
    assert view.waiting_parents == 1
    assert view.cap_total == 1 and view.lifted_from is None, "min(user_max, ...) binds"


# ── settings ──────────────────────────────────────────────────────────────────


def test_fairness_settings_from_agent_config_clamps() -> None:
    agent = SimpleNamespace(
        lane_weights={"dash:a": 0, "system": 99, "": 7},
        child_reserve=42,
        adaptive_floor=0,
    )
    s = FairnessSettings.from_agent_config(agent)
    assert s.lane_weights == {"dash:a": 1, "system": lanes.MAX_WEIGHT}
    assert s.child_reserve == 8 and s.adaptive_floor == 1
    defaults = FairnessSettings.from_agent_config(SimpleNamespace())
    assert defaults == FairnessSettings()


def test_agent_config_declares_the_keys_with_defaults() -> None:
    from kiro_crew.config.sections import AgentConfig

    cfg = AgentConfig()
    assert not hasattr(cfg, "system_lane_weight")
    assert cfg.lane_weights == {}
    assert cfg.child_reserve == 1
    # RFC Q3: checkpoint-pause is not built, so no flag for it exists.
    assert not hasattr(cfg, "parent_checkpoint_pause")


def test_capacity_view_arithmetic() -> None:
    v = CapacityView(
        cap_total=4, running=3, child_reserve=1, reserve_active=True, waiting_parents=0
    )
    assert v.roots_cap == 3 and v.root_slot is False and v.any_slot is True
    v2 = CapacityView(
        cap_total=4, running=3, child_reserve=1, reserve_active=False, waiting_parents=0
    )
    assert v2.roots_cap == 4 and v2.root_slot is True
    assert v.to_dict()["roots_cap"] == 3


# ── resume hold: event-driven, no grant interval ─────────────────────────────


@pytest.mark.asyncio
async def test_wait_resume_granted_is_immediate_for_a_run_holding_its_slot(h) -> None:
    hz = await h(max_concurrent=2)
    s = hz.spawn("S")
    await hz.settle()
    assert await hz.mgr._admission.wait_resume_granted(s.id, timeout=0.05) is True
    assert await hz.mgr._admission.wait_resume_granted("nope", timeout=0.05) is True


@pytest.mark.asyncio
async def test_wait_resume_granted_wakes_on_the_grant_not_on_a_timer(h) -> None:
    hz = await h(max_concurrent=1)
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.live(s)._slot_released is True
    # A hold started BEFORE the grant returns False at its bound...
    assert await hz.mgr._admission.wait_resume_granted(s.id, timeout=0.02) is False
    # ...and one in flight is released by the grant itself.
    waiter = asyncio.ensure_future(hz.mgr._admission.wait_resume_granted(s.id, timeout=5.0))
    await hz.settle()
    assert not waiter.done()
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await hz.end(a)
    assert await asyncio.wait_for(waiter, timeout=0.5) is True
    assert loop.time() - t0 < 0.1, "released by the grant event, not by the bound"
    assert hz.mgr._admission.resume_granted(s.id) is True
    assert hz.live(s)._resume_event is None, "one event per wait"


@pytest.mark.asyncio
async def test_api_spawn_resume_reports_and_holds(h) -> None:
    hz = await h(max_concurrent=1)
    s = hz.spawn("S")
    await hz.settle()
    state = SimpleNamespace(subagents=hz.mgr)

    def req(agent_id: str, wait: str = "0") -> SimpleNamespace:
        return SimpleNamespace(
            app={"state": state}, match_info={"agent_id": agent_id}, query={"wait_secs": wait}
        )

    body = json.loads((await spawn_resume.api_spawn_resume(req(s.id))).text)
    assert body == {
        "id": s.id, "known": True, "granted": True, "slot_released": False,
        "resume_pending": False, "done": False,
    }  # fmt: skip
    unknown = json.loads((await spawn_resume.api_spawn_resume(req("zzz"))).text)
    assert unknown["known"] is False and unknown["granted"] is True
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    held = json.loads((await spawn_resume.api_spawn_resume(req(s.id, "0.02"))).text)
    assert held["granted"] is False and held["slot_released"] is True
    assert spawn_resume._wait_secs(req(s.id, "999")) == spawn_resume.MAX_HOLD_SECS
    # Request input never reaches the hold as NaN/inf/negative: each is refused
    # with the handler's typed error, not clamped.
    for bad in ("nan", "inf", "-inf", "-1", "1e400", "nan?", "abc", ""):
        assert spawn_resume._wait_secs(req(s.id, bad)) is None, bad
        refused = await spawn_resume.api_spawn_resume(req(s.id, bad))
        assert refused.status == 400, bad
        assert json.loads(refused.text)["code"] == spawn_resume.INVALID_WAIT_CODE
    assert spawn_resume._wait_secs(req(s.id, "0")) == 0.0
    pending = asyncio.ensure_future(spawn_resume.api_spawn_resume(req(s.id, "5")))
    await hz.settle()
    await hz.end(a)
    granted = json.loads((await asyncio.wait_for(pending, timeout=0.5)).text)
    assert granted["granted"] is True and granted["slot_released"] is False
    lanes_body = json.loads((await spawn_resume.api_spawn_lanes(req("x"))).text)
    assert "dash:1" in lanes_body["lanes"] and "capacity" in lanes_body
    no_mgr = await spawn_resume.api_spawn_resume(
        SimpleNamespace(
            app={"state": SimpleNamespace(subagents=None)}, match_info={"agent_id": "a"}, query={}
        )
    )
    assert no_mgr.status == 503


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


def test_hold_for_parent_resume_returns_when_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    answers = [
        {"known": True, "granted": False},
        {"known": True, "granted": False},
        {"known": True, "granted": True},
    ]
    calls: list[str] = []

    def fake_get(path: str, *a, **k):
        calls.append(path)
        clock.t += 1.0
        return answers.pop(0)

    monkeypatch.setattr(spawn_tools.mcp_core, "_get", fake_get)
    monkeypatch.setattr(spawn_tools.mcp_core, "time", clock)
    monkeypatch.setattr(spawn_tools, "is_tool_cancelled", lambda: False)
    note = spawn_tools._hold_for_parent_resume("subagent:p1", clock.t + 60)
    assert note is None
    assert len(calls) == 3 and all(c.startswith("/api/spawn/p1/resume?wait_secs=") for c in calls)
    assert calls[0].endswith("8.0"), "each request is held server-side up to the bound"


def test_hold_for_parent_resume_deadline_reports_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()

    def fake_get(path: str, *a, **k):
        clock.t += 8.0
        return {"known": True, "granted": False}

    monkeypatch.setattr(spawn_tools.mcp_core, "_get", fake_get)
    monkeypatch.setattr(spawn_tools.mcp_core, "time", clock)
    monkeypatch.setattr(spawn_tools, "is_tool_cancelled", lambda: False)
    note = spawn_tools._hold_for_parent_resume("subagent:p1", clock.t + 20)
    assert note is not None and note["status"] == "resume_pending" and note["parent"] == "p1"
    # A deadline already spent on the children observes nothing about the slot.
    assert spawn_tools._hold_for_parent_resume("subagent:p1", clock.t - 1) is None


def test_hold_for_parent_resume_skips_chat_parents_and_unknown_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    calls: list[str] = []

    def fake_get(path: str, *a, **k):
        calls.append(path)
        return {"known": False, "granted": True}

    monkeypatch.setattr(spawn_tools.mcp_core, "_get", fake_get)
    monkeypatch.setattr(spawn_tools.mcp_core, "time", clock)
    monkeypatch.setattr(spawn_tools, "is_tool_cancelled", lambda: False)
    assert spawn_tools._hold_for_parent_resume("dashboard:abc", clock.t + 60) is None
    assert spawn_tools._hold_for_parent_resume("", clock.t + 60) is None
    assert calls == []
    assert spawn_tools._hold_for_parent_resume("subagent:gone", clock.t + 60) is None
    assert len(calls) == 1
    # Legacy gateway without the route: an error payload releases the hold.
    monkeypatch.setattr(spawn_tools.mcp_core, "_get", lambda *a, **k: {"error": "not found"})
    assert spawn_tools._hold_for_parent_resume("subagent:p1", clock.t + 60) is None


def test_spawn_sub_agents_holds_then_returns_children_results() -> None:
    seen: list[str] = []
    resume = iter([{"known": True, "granted": False}, {"known": True, "granted": True}])

    def fake_get(path: str, *a, **k):
        seen.append(path)
        if "/resume" in path:
            return next(resume)
        return {"done": True, "agent": "w", "result": "ok"}

    with (
        patch("kiro_crew.mcp_core._post", return_value={"id": "c1"}),
        patch("kiro_crew.mcp_core._get", side_effect=fake_get),
        patch("kiro_crew.mcp_core.sel"),
        patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "subagent:p9"}),
    ):
        out = spawn_tools.spawn_sub_agents(
            "spawn_sub_agents", {"agents": [{"agent_or_mode": "w", "prompt": "do"}]}
        )
    assert '"completed"' in out and "resume_pending" not in out
    holds = [p for p in seen if p.startswith("/api/spawn/p9/resume")]
    assert len(holds) == 2, "held until the second answer said granted"
    # The children were read before the hold and their results collected after it.
    assert seen.index("/api/spawn/c1") < seen.index(holds[0])


@pytest.mark.asyncio
async def test_reserve_pulls_a_store_only_child_into_a_window_full_of_roots(h) -> None:
    hz = await h(max_concurrent=2)
    hz.mgr._taskq._window = 2
    s = hz.spawn("S", parent="dash:1")
    r1 = hz.spawn("R1", parent="dash:other")
    await hz.settle()
    r2 = hz.spawn("R2", parent="dash:other")
    r3 = hz.spawn("R3", parent="dash:other")
    r4 = hz.spawn("R4", parent="dash:other")
    assert [p["_preassigned_id"] for p in hz.mgr._queue] == [r2.id, r3.id]
    assert hz.state(r4) == model.QUEUED and hz.mgr._admission.taskq_overflow() == 1
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    # The window was full of roots and A was store-only; the freed slot is the
    # reserve, so the pump made room for the child rather than starting R2.
    assert hz.state(a) == model.RUNNING
    assert hz.state(r2) == model.QUEUED and hz.state(r3) == model.QUEUED
    assert hz.mgr._running_count == 2
    assert len(hz.mgr._queue) <= 2
    await hz.end(a)
    await hz.end(s)
    await hz.end(r1)
    # Roots resume in FIFO order once nothing nested is pending.
    assert hz.state(r2) == model.RUNNING and hz.state(r3) == model.RUNNING
    assert hz.state(r4) == model.QUEUED


@pytest.mark.asyncio
async def test_reserve_pulls_a_child_into_a_window_holding_one_entry_per_lane(h) -> None:
    """A window with no lane to spare still makes room for the reserved child.

    Keeping every lane's head frees NOTHING when each lane holds exactly one
    entry, and a ``children_only`` top-up with no room hydrates no row at all --
    so the slot the child reserve is holding open could never be filled from
    disk and the tree would wait on its own child for as long as the process
    lives. One head goes back to store-only instead.
    """
    hz = await h(max_concurrent=2)
    hz.mgr._taskq._window = 2
    s = hz.spawn("S", parent="dash:1")
    r1 = hz.spawn("R1", parent="dash:zero")
    await hz.settle()
    assert hz.state(s) == model.RUNNING and hz.state(r1) == model.RUNNING
    ra = hz.spawn("RA", parent="dash:a")
    rb = hz.spawn("RB", parent="dash:b")
    assert [p["_preassigned_id"] for p in hz.mgr._queue] == [ra.id, rb.id]
    assert {hz.mgr._admission.lane_of_entry(p) for p in hz.mgr._queue} == {"dash:a", "dash:b"}
    # S blocks on A. A is store-only (the window is full) and the slot S frees
    # is the reserve, so A is the only row that may take it.
    hz.block_in_spawn_sub_agents(s, "call-s")
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.state(s) == model.WAITING_CHILDREN
    assert hz.state(a) == model.RUNNING, "the reserved slot was never filled from disk"
    assert hz.mgr._running_count == 2
    # Neither root was lost making that room: a head dropped back to store-only
    # is a queued row the refill refetches, so both are still waiting and each
    # is in exactly one of the two places.
    assert hz.state(ra) == model.QUEUED and hz.state(rb) == model.QUEUED
    windowed = [p["_preassigned_id"] for p in hz.mgr._queue]
    assert set(windowed) <= {ra.id, rb.id}
    assert len(windowed) + hz.mgr._admission.taskq_overflow() == 2
    await hz.end(a)
    await hz.end(s)
    await hz.end(r1)
    assert hz.state(ra) == model.RUNNING and hz.state(rb) == model.RUNNING
    await hz.end(ra)
    await hz.end(rb)
    assert hz.store.count_by_state() == {model.DONE: 5}


@pytest.mark.asyncio
async def test_eviction_frees_one_lane_head_per_call_and_never_a_resume(h) -> None:
    """The head-dropping arm is a floor under the refill, not a second policy.

    A lane with a spare entry gives that up and every head stands; with no
    spare, exactly one head goes per call, so the window churns by one entry
    rather than wholesale.
    """
    hz = await h(max_concurrent=1)
    hz.mgr._queue.extend(
        [
            {"_resume_id": "res", "_preassigned_id": "res", "parent_session_key": "dash:r"},
            {"_preassigned_id": "a1", "parent_session_key": "dash:a", "_lane": "dash:a"},
            {"_preassigned_id": "b1", "parent_session_key": "dash:b", "_lane": "dash:b"},
            {"_preassigned_id": "c1", "parent_session_key": "dash:c", "_lane": "dash:c"},
        ]
    )
    assert hz.mgr._admission._evict_for_lanes(3) == 1
    assert [p["_preassigned_id"] for p in hz.mgr._queue] == ["res", "a1", "b1"]
    hz.mgr._queue.append(
        {"_preassigned_id": "a2", "parent_session_key": "dash:a", "_lane": "dash:a"}
    )
    assert hz.mgr._admission._evict_for_lanes(1) == 1
    assert [p["_preassigned_id"] for p in hz.mgr._queue] == ["res", "a1", "b1"]
