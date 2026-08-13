"""Tests for dynamic sub-agent cap sizing (subagent.compute/resolve_max_subagents).

Validates the formula against the worked examples in
``dynamic-subagent-sizing.md`` §3.3 plus routing/clamp/reservation edge cases.
Stage 2 uses fallback costs only (learned costs land in a later stage), and the
cgroup clamp lands in Stage 8 — here we feed effective memory directly via the
patched ``_available_memory_gb``.
"""

from __future__ import annotations

import types

import pytest

import kiro_crew.subagent as subagent
from kiro_crew.subagent import compute_max_subagents, resolve_max_subagents


@pytest.fixture(autouse=True)
def _no_learned_cost(monkeypatch):
    """Isolate from the machine's learned-cost store (~/.kirocrew/subagents/
    cost_samples.jsonl). compute_max_subagents prefers read_learned_cost over
    the cfg fallback, so on a dev box with a populated store these tests would
    read the real mem_gb/cpu_cores instead of the per-case fallback costs and
    assert against the wrong cap. These cases exercise the fallback path by
    design, so force the learned lookup to miss."""
    monkeypatch.setattr(subagent, "read_learned_cost", lambda *a, **k: None)


def _cfg(
    *,
    max_subagents: int = 0,
    buffer_pct: int = 20,
    mem_cost: float = 0.315,
    cpu_cost: float = 0.8,
    hard_cap: int = 16,
    pool_size: int = 0,
) -> types.SimpleNamespace:
    """Minimal duck-typed stand-in for KiroCrewConfig (agent + session)."""
    return types.SimpleNamespace(
        agent=types.SimpleNamespace(
            max_subagents=max_subagents,
            subagent_mem_buffer_pct=buffer_pct,
            subagent_cost_gb=mem_cost,
            subagent_cpu_cost_cores=cpu_cost,
            subagent_auto_max=hard_cap,
        ),
        session=types.SimpleNamespace(pool_size=pool_size),
    )


@pytest.fixture
def patch_host(monkeypatch):
    """Patch available memory + cpu_count to deterministic values."""

    def _apply(avail_gb: float, cpu_count: int) -> None:
        monkeypatch.setattr(subagent, "_available_memory_gb", lambda: avail_gb)
        monkeypatch.setattr(subagent.os, "cpu_count", lambda: cpu_count)

    return _apply


# --- Worked examples from dynamic-subagent-sizing.md §3.3 -------------------


def test_example_a_hard_cap_binds(patch_host) -> None:
    # 174.7 GB / 48 cores: mem_term=443, cpu_term=48, clamp(min,3,16) = 16
    patch_host(174.7, 48)
    cfg = _cfg(mem_cost=0.315, cpu_cost=0.8, hard_cap=16)
    assert compute_max_subagents(cfg) == 16


def test_example_b_floor(patch_host) -> None:
    # 8 GB / 4 cores, fallback costs: mem_term=12, cpu_term=3, floor = 3
    patch_host(8.0, 4)
    cfg = _cfg(mem_cost=0.5, cpu_cost=1.0, hard_cap=16)
    assert compute_max_subagents(cfg) == 3


def test_example_c_cpu_binds_with_pool(patch_host) -> None:
    # 32 GB / 12 cores, pool=5: mem_term=59, cpu_term=12, min = 12
    patch_host(32.0, 12)
    cfg = _cfg(mem_cost=0.4, cpu_cost=0.8, hard_cap=16, pool_size=5)
    assert compute_max_subagents(cfg) == 12


def test_example_d_memory_binds(patch_host) -> None:
    # Effective 4 GB (cgroup headroom fed directly) / 48 cores:
    # mem_term=10, cpu_term=48, min = 10
    patch_host(4.0, 48)
    cfg = _cfg(mem_cost=0.315, cpu_cost=0.8, hard_cap=16)
    assert compute_max_subagents(cfg) == 10


def test_shared_marginal_cost_binds_on_provider_ceiling(patch_host) -> None:
    # Stage 1: with session-shared marginal costs (mem≈0.05 GB, cpu≈0.25 core),
    # even a modest 8 GB / 4 core host is no longer RAM-bound — the cap rises to
    # the provider ceiling (hard_cap) instead of the legacy floor of 3.
    # mem_term = floor((8*0.8)/0.05) = 128; cpu_term = floor((4*0.8)/0.25) = 12;
    # min(128, 12, 16) = 12 (was 3 when the whole shared process was charged).
    patch_host(8.0, 4)
    cfg = _cfg(mem_cost=0.05, cpu_cost=0.25, hard_cap=16)
    assert compute_max_subagents(cfg) == 12


# --- Edge cases ------------------------------------------------------------


def test_pool_reservation_reduces_memory_budget(patch_host) -> None:
    # Same host, with vs without a warm pool: reservation lowers mem_term.
    patch_host(20.0, 64)  # CPU generous so memory binds
    no_pool = compute_max_subagents(_cfg(mem_cost=0.5, cpu_cost=0.1, pool_size=0, hard_cap=100))
    with_pool = compute_max_subagents(_cfg(mem_cost=0.5, cpu_cost=0.1, pool_size=10, hard_cap=100))
    # no_pool: floor(20*0.8/0.5)=32 ; with_pool: floor((16-5)/0.5)=22
    assert no_pool == 32
    assert with_pool == 22


def test_hard_cap_clamps_high(patch_host) -> None:
    patch_host(174.7, 48)
    cfg = _cfg(mem_cost=0.315, cpu_cost=0.8, hard_cap=8)
    assert compute_max_subagents(cfg) == 8


def test_floor_never_below_three(patch_host) -> None:
    patch_host(1.0, 1)  # tiny host
    cfg = _cfg(mem_cost=0.5, cpu_cost=1.0, hard_cap=16)
    assert compute_max_subagents(cfg) == 3


def test_hard_cap_below_floor_is_raised_to_three(patch_host) -> None:
    # A misconfigured subagent_auto_max < 3 no longer drops the cap below 3:
    # compute_max_subagents enforces a hard floor of 3 (the loader also clamps
    # subagent_auto_max up to 3, but compute defends independently).
    patch_host(174.7, 48)
    cfg = _cfg(hard_cap=2)
    assert compute_max_subagents(cfg) == 3


def test_unreadable_memory_fails_open_to_legacy_default(patch_host) -> None:
    patch_host(-1.0, 48)  # /proc/meminfo unreadable
    cfg = _cfg(hard_cap=16)
    assert compute_max_subagents(cfg) == 3


# --- Sentinel routing ------------------------------------------------------


def test_resolve_explicit_value_bypasses_compute(patch_host) -> None:
    patch_host(174.7, 48)
    cfg = _cfg(max_subagents=5, hard_cap=16)
    assert resolve_max_subagents(cfg) == 5  # explicit, not the computed 16


def test_resolve_zero_sentinel_triggers_compute(patch_host) -> None:
    patch_host(174.7, 48)
    cfg = _cfg(max_subagents=0, mem_cost=0.315, cpu_cost=0.8, hard_cap=16)
    assert resolve_max_subagents(cfg) == 16


def test_resolve_floors_explicit_pin_below_three(patch_host) -> None:
    # A stray explicit pin of 1 or 2 (e.g. from a directly-constructed config
    # that bypassed the loader/API clamps) is floored to 3 at resolve time — it
    # must never drop the runtime cap below the legacy default. 0 stays "auto".
    patch_host(174.7, 48)
    assert resolve_max_subagents(_cfg(max_subagents=1)) == 3
    assert resolve_max_subagents(_cfg(max_subagents=2)) == 3
    # A valid explicit pin (>= 3) is returned unchanged, not auto-computed.
    assert resolve_max_subagents(_cfg(max_subagents=5, hard_cap=16)) == 5


# --- Gateway wiring contract (Stage 3) -------------------------------------


def test_manager_reports_resolved_cap_for_auto_sentinel(patch_host) -> None:
    """The cap the gateway feeds SubagentManager is what `.max_concurrent` reports."""
    from unittest.mock import MagicMock

    from kiro_crew.subagent import SubagentManager

    patch_host(174.7, 48)
    cfg = _cfg(max_subagents=0, mem_cost=0.315, cpu_cost=0.8, hard_cap=16)
    cap = resolve_max_subagents(cfg)
    assert cap == 16  # computed, not the raw 0 sentinel

    mgr = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        max_concurrent=cap,
    )
    assert mgr.max_concurrent == 16  # live manager is the source of truth (§5.2)


# ---------------------------------------------------------------------------
# Unified spawn staggering (Stage 4, dynamic-subagent-sizing.md §5.3)
# ---------------------------------------------------------------------------


def _mgr(*, running: int, max_concurrent: int, last_ts: float, stagger: float = 2.0):
    """Build a SubagentManager with stagger state set, mock heavy deps."""
    from unittest.mock import MagicMock

    from kiro_crew.subagent import SubagentManager

    m = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        max_concurrent=max_concurrent,
    )
    m._running_count = running
    m._last_spawn_ts = last_ts
    m._spawn_stagger_secs = stagger
    return m


class TestStaggerGate:
    """_should_stagger_queue: capacity + stagger gate (initial-fill burst guard)."""

    def test_at_capacity_always_queues(self) -> None:
        import time as _t

        m = _mgr(running=4, max_concurrent=4, last_ts=0.0)
        should_queue, slot_free = m._should_stagger_queue(_t.monotonic())
        assert should_queue is True
        assert slot_free is False

    def test_slot_free_but_too_soon_queues(self) -> None:
        import time as _t

        now = _t.monotonic()
        m = _mgr(running=1, max_concurrent=16, last_ts=now)  # just spawned
        should_queue, slot_free = m._should_stagger_queue(now)
        assert should_queue is True  # stagger gate
        assert slot_free is True

    def test_slot_free_and_interval_elapsed_starts(self) -> None:
        import time as _t

        now = _t.monotonic()
        m = _mgr(running=1, max_concurrent=16, last_ts=now - 5.0, stagger=2.0)
        should_queue, _ = m._should_stagger_queue(now)
        assert should_queue is False  # ok to start now

    def test_first_ever_spawn_starts_immediately(self) -> None:
        import time as _t

        m = _mgr(running=0, max_concurrent=16, last_ts=0.0)
        should_queue, _ = m._should_stagger_queue(_t.monotonic())
        assert should_queue is False  # last_ts=0 → interval long elapsed


class TestDrainPump:
    """_drain_queue: one start per interval, reschedules when too soon."""

    def test_too_soon_does_not_pop(self) -> None:
        import asyncio
        import time as _t
        from unittest.mock import MagicMock

        async def run() -> None:
            now = _t.monotonic()
            m = _mgr(running=0, max_concurrent=16, last_ts=now, stagger=2.0)
            m._queue = [{"task": "task", "parent_session_key": "", "agent": "", "max_turns": 0, "model": None, "allowed_tools": None, "bare": False, "cwd": "", "approval_mode": None, "silent": False}]
            m.spawn = MagicMock()  # type: ignore[method-assign]
            m._drain_queue()
            m.spawn.assert_not_called()  # too soon → no burst
            assert len(m._queue) == 1  # item retained

        asyncio.run(run())

    def test_ready_pops_and_spawns_one(self) -> None:
        import asyncio
        import time as _t
        from unittest.mock import MagicMock

        async def run() -> None:
            now = _t.monotonic()
            m = _mgr(running=0, max_concurrent=16, last_ts=now - 5.0, stagger=2.0)
            m._queue = [
                {"task": "task-a", "parent_session_key": "", "agent": "", "max_turns": 0, "model": None, "allowed_tools": None, "bare": False, "cwd": "", "approval_mode": None, "silent": False},
                {"task": "task-b", "parent_session_key": "", "agent": "", "max_turns": 0, "model": None, "allowed_tools": None, "bare": False, "cwd": "", "approval_mode": None, "silent": False},
            ]
            m.spawn = MagicMock()  # type: ignore[method-assign]
            m._drain_queue()
            assert m.spawn.call_count == 1  # exactly one per pump cycle
            assert len(m._queue) == 1  # one popped

        asyncio.run(run())

    def test_at_capacity_does_not_pop(self) -> None:
        import asyncio
        import time as _t
        from unittest.mock import MagicMock

        async def run() -> None:
            m = _mgr(running=16, max_concurrent=16, last_ts=_t.monotonic() - 99, stagger=2.0)
            m._queue = [{"task": "task", "parent_session_key": "", "agent": "", "max_turns": 0, "model": None, "allowed_tools": None, "bare": False, "cwd": "", "approval_mode": None, "silent": False}]
            m.spawn = MagicMock()  # type: ignore[method-assign]
            m._drain_queue()
            m.spawn.assert_not_called()
            assert len(m._queue) == 1

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Learned-cost sampling (Stage 6, dynamic-subagent-sizing.md §4.1)
# ---------------------------------------------------------------------------


class TestQueuedDepthEmission:
    """_queued_depth / _emit_queue_depth: advisory 'waiting to start' count
    surfaced to the UI as subagent_queued events so the chip can show queued
    agents, not only running/completed ones."""

    def test_queued_depth_counts_per_parent(self) -> None:
        import time as _t

        m = _mgr(running=0, max_concurrent=4, last_ts=_t.monotonic())
        m._queue = [
            {"task": "a", "parent_session_key": "dashboard:s1"},
            {"task": "b", "parent_session_key": "dashboard:s1"},
            {"task": "c", "parent_session_key": "dashboard:s2"},
        ]
        assert m._queued_depth("dashboard:s1") == 2
        assert m._queued_depth("dashboard:s2") == 1
        assert m._queued_depth("dashboard:absent") == 0

    def test_emit_queue_depth_fires_event_with_count(self) -> None:
        import asyncio
        import time as _t

        events: list = []

        async def on_event(etype, info, extra):
            events.append((etype, info.parent_session_key, dict(extra)))

        async def run() -> None:
            m = _mgr(running=0, max_concurrent=4, last_ts=_t.monotonic())
            m._on_event = on_event
            m._queue = [
                {"task": "a", "parent_session_key": "dashboard:s1"},
                {"task": "b", "parent_session_key": "dashboard:s1"},
            ]
            m._emit_queue_depth("dashboard:s1")
            # scheduled via create_task — yield so it runs
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(run())
        assert ("subagent_queued", "dashboard:s1", {"queued": 2}) in events

    def test_emit_queue_depth_zero_when_parent_drained(self) -> None:
        import asyncio
        import time as _t

        events: list = []

        async def on_event(etype, info, extra):
            events.append((etype, extra.get("queued")))

        async def run() -> None:
            m = _mgr(running=0, max_concurrent=4, last_ts=_t.monotonic())
            m._on_event = on_event
            # only a *different* parent has items queued — the drained parent
            # reports 0 so the chip clears its "waiting" count.
            m._queue = [{"task": "c", "parent_session_key": "dashboard:other"}]
            m._emit_queue_depth("dashboard:s1")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(run())
        assert ("subagent_queued", 0) in events


class TestQueuedDepthWiring:
    """The two producer call-sites are wired: spawn()'s queue branch and
    _drain_queue() each emit subagent_queued. Guards against silently
    reverting the wiring (which would reintroduce the invisible-queue bug
    while helper-only tests stayed green)."""

    def test_spawn_queue_branch_emits_depth(self, monkeypatch) -> None:
        import asyncio
        import time as _t

        import kiro_crew.subagent as sub

        # Bypass governance so we deterministically reach the queue branch.
        monkeypatch.setattr(sub, "_vet_spawn_governance", lambda *a, **k: None)

        events: list = []

        async def on_event(etype, info, extra):
            if etype == "subagent_queued":
                events.append((info.parent_session_key, extra.get("queued")))

        async def run() -> None:
            # At capacity → the spawn must be queued, not started.
            m = _mgr(running=2, max_concurrent=2, last_ts=_t.monotonic())
            m._on_event = on_event
            info = m.spawn(task="x", parent_session_key="dashboard:s1")
            assert info is not None and info.queued is True  # queued, not started
            assert len(m._queue) == 1  # actually appended
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(run())
        assert ("dashboard:s1", 1) in events

    def test_drain_emits_queued_depth_on_pop(self) -> None:
        import asyncio
        import time as _t
        from unittest.mock import MagicMock

        events: list = []

        async def on_event(etype, info, extra):
            if etype == "subagent_queued":
                events.append((info.parent_session_key, extra.get("queued")))

        async def run() -> None:
            now = _t.monotonic()
            m = _mgr(running=0, max_concurrent=16, last_ts=now - 5.0, stagger=2.0)
            m._on_event = on_event
            m.spawn = MagicMock()  # type: ignore[method-assign]
            m._queue = [
                {"task": "a", "parent_session_key": "dashboard:s1"},
                {"task": "b", "parent_session_key": "dashboard:s1"},
            ]
            m._drain_queue()  # pops one → s1's remaining depth is 1
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(run())
        assert ("dashboard:s1", 1) in events


class TestQueuedIdentityRoundTrip:
    """A queued member must START under the id its caller was handed.

    Regression: spawn() used to return a throwaway ``q<n>`` sentinel for any
    spawn that hit the stagger/concurrency gate, and _drain_queue minted a FRESH
    uuid when it actually started the agent. With the default 2s stagger that is
    every wave member after the first, so ``spawn_run``'s printed wave roster
    listed one real id plus N placeholders no agent ever had — the inline
    SubagentRunCard, which resolves a wave by matching those ids against live
    per-agent events, could never observe more than one member and reported
    "1 agent running" for a 2-agent wave the sidebar counted correctly.
    """

    def test_drained_spawn_reuses_the_announced_id(self, monkeypatch) -> None:
        import re
        import time as _t
        from unittest.mock import MagicMock

        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub, "_vet_spawn_governance", lambda *a, **k: None)

        m = _mgr(running=1, max_concurrent=16, last_ts=_t.monotonic(), stagger=2.0)
        info = m.spawn(task="x", parent_session_key="dashboard:s1")

        assert info is not None and info.queued is True
        assert re.fullmatch(r"[0-9a-f]{8}", info.id), "queued id must be a real agent id"

        # Drain: the gate is open now (stagger elapsed, slot free), so the
        # popped entry must be re-spawned under the SAME id.
        m._last_spawn_ts = _t.monotonic() - 10.0
        m.spawn = MagicMock()  # type: ignore[method-assign]
        m._drain_queue()

        assert m.spawn.call_count == 1
        kwargs = m.spawn.call_args.kwargs
        assert kwargs["_preassigned_id"] == info.id
        assert kwargs["_from_queue"] is True

    def test_requeue_preserves_the_id(self, monkeypatch) -> None:
        """A drained spawn that hits the gate AGAIN keeps the same id."""
        import time as _t

        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub, "_vet_spawn_governance", lambda *a, **k: None)

        m = _mgr(running=2, max_concurrent=2, last_ts=_t.monotonic(), stagger=2.0)
        first = m.spawn(task="x", parent_session_key="dashboard:s1")
        assert first is not None

        # Re-enter spawn() with the id already assigned (what _drain_queue does)
        # while the gate is still closed → queued a second time, id unchanged.
        m._queue.clear()
        again = m.spawn(
            task="x",
            parent_session_key="dashboard:s1",
            _from_queue=True,
            _preassigned_id=first.id,
        )
        assert again is not None and again.queued is True
        assert again.id == first.id
        assert m._queue[0]["_preassigned_id"] == first.id

    def test_rejection_on_drain_uses_the_announced_id(self, monkeypatch) -> None:
        """A member REJECTED when its queued spawn drains is announced under the
        id its caller was handed, not a fresh one.

        The guards that refuse a spawn (empty task, low memory, bad cwd,
        governance, bad agent name) re-run on the drain pass, so a spawn accepted
        at queue time can still be refused when it starts. Minting a fresh uuid
        there would announce the failure under an id the caller never saw — the
        same identity break this class of bug is about, just on the error path.
        """
        import time as _t

        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub, "_vet_spawn_governance", lambda *a, **k: None)
        # Refuse on memory so the guard fires ahead of the queue gate.
        monkeypatch.setattr(sub, "check_memory_available", lambda min_gb=0: (False, 0.5))

        m = _mgr(running=1, max_concurrent=16, last_ts=_t.monotonic(), stagger=2.0)
        announced = "deadbeef"
        info = m.spawn(
            task="x",
            parent_session_key="dashboard:s1",
            _from_queue=True,
            _preassigned_id=announced,
        )

        assert info is not None
        assert info.done and "memory" in info.error
        assert info.id == announced


class TestCpuJiffiesParser:
    """_parse_cpu_jiffies: utime+stime from raw /proc/<pid>/stat bytes."""

    def test_parses_utime_stime(self) -> None:
        from kiro_crew.subagent import _parse_cpu_jiffies

        # comm with spaces + an embedded ')' — rindex must find the real close.
        # post-comm tokens: state(0) ... utime(11)=120 stime(12)=60
        stat = b"1234 (kiro cli (node)) S 2 3 4 5 6 7 8 9 10 11 120 60 0 0"
        assert _parse_cpu_jiffies(stat) == 180

    def test_malformed_returns_zero(self) -> None:
        from kiro_crew.subagent import _parse_cpu_jiffies

        assert _parse_cpu_jiffies(b"garbage") == 0
        assert _parse_cpu_jiffies(b"") == 0


class TestSubtreeCpuJiffies:
    """_subtree_cpu_jiffies: sums pid + descendants."""

    def test_sums_tree(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        # tree: 1 -> [2, 3]; 2 -> [4]
        children = {1: [2, 3], 2: [4], 3: [], 4: []}
        jiffies = {1: 100, 2: 50, 3: 25, 4: 10}
        monkeypatch.setattr(sub, "_proc_children", lambda pid: children.get(pid, []))
        monkeypatch.setattr(sub, "_proc_cpu_jiffies", lambda pid: jiffies.get(pid, 0))
        assert sub._subtree_cpu_jiffies(1) == 185


class TestSampleLiveCosts:
    """_sample_live_costs: high-water RSS/CPU tracking across polls."""

    def _agent(self):
        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(id="a1", task="t", agent="kirocrew")
        info._pid = 4242
        return info

    def test_rss_high_water(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        m = _mgr(running=1, max_concurrent=16, last_ts=0.0)
        info = self._agent()
        m._agents = {"a1": info}
        monkeypatch.setattr(sub, "_subtree_cpu_jiffies", lambda pid: 0)
        # Two polls: 2 GB then 1 GB — peak must stick at 2.
        rss_seq = iter([2 * 1024 * 1024, 1 * 1024 * 1024])
        monkeypatch.setattr(sub, "_proc_rss_kb", lambda pid: next(rss_seq))
        m._sample_live_costs()
        m._sample_live_costs()
        assert info.peak_rss_gb == pytest.approx(2.0, abs=0.01)

    def test_cpu_high_water_uses_delta(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        m = _mgr(running=1, max_concurrent=16, last_ts=0.0)
        info = self._agent()
        m._agents = {"a1": info}
        monkeypatch.setattr(sub, "_proc_rss_kb", lambda pid: -1)  # ignore RSS
        monkeypatch.setattr(sub, "_CLK_TCK", 100)

        # Control wall-clock: poll1 t=10, poll2 t=11 (dt=1s).
        times = iter([10.0, 11.0])
        monkeypatch.setattr(sub.time, "monotonic", lambda: next(times))
        # jiffies: 1000 then 1100 → 100 jiffies / (100 tck * 1s) = 1.0 core.
        jiff = iter([1000, 1100])
        monkeypatch.setattr(sub, "_subtree_cpu_jiffies", lambda pid: next(jiff))

        m._sample_live_costs()  # seeds baseline, no delta
        assert info.peak_cpu_cores == 0.0
        m._sample_live_costs()  # delta → 1.0 core
        assert info.peak_cpu_cores == pytest.approx(1.0, abs=0.01)

    def test_done_or_pidless_agents_skipped(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        m = _mgr(running=1, max_concurrent=16, last_ts=0.0)
        done = self._agent()
        done.done = True
        m._agents = {"d": done}
        called = {"n": 0}

        def _rss(pid):
            called["n"] += 1
            return 1024 * 1024

        monkeypatch.setattr(sub, "_proc_rss_kb", _rss)
        monkeypatch.setattr(sub, "_subtree_cpu_jiffies", lambda pid: 0)
        m._sample_live_costs()
        assert called["n"] == 0  # done agent not sampled
        assert done.peak_rss_gb == 0.0

    def test_session_shared_agents_record_averaged_share(self, monkeypatch) -> None:
        """Shared subagents share ONE runtime PID; each must be charged the
        measured RSS/CPU divided by the number of live shared sessions on that
        PID (an empirical per-session average), not the whole shared process."""
        import kiro_crew.subagent as sub
        from kiro_crew.subagent import SubagentInfo

        m = _mgr(running=2, max_concurrent=16, last_ts=0.0)
        # Two shared subagents on the SAME runtime PID.
        a = SubagentInfo(id="a1", task="t", agent="kirocrew")
        a._pid = 4242
        a._session_sharing = True
        b = SubagentInfo(id="a2", task="t", agent="kirocrew")
        b._pid = 4242
        b._session_sharing = True
        m._agents = {"a1": a, "a2": b}

        # Shared runtime measures 4 GB RSS; with 2 live shared sessions each
        # agent is charged 2 GB, never the full 4 GB.
        monkeypatch.setattr(sub, "_proc_rss_kb", lambda pid: 4 * 1024 * 1024)
        monkeypatch.setattr(sub, "_subtree_cpu_jiffies", lambda pid: 0)
        m._sample_live_costs()

        assert a.peak_rss_gb == pytest.approx(2.0, abs=0.01)
        assert b.peak_rss_gb == pytest.approx(2.0, abs=0.01)
        # Single shared session → full measured RSS (divisor 1).
        b.done = True
        monkeypatch.setattr(sub, "_proc_rss_kb", lambda pid: 3 * 1024 * 1024)
        m._sample_live_costs()
        assert a.peak_rss_gb == pytest.approx(3.0, abs=0.01)


# ---------------------------------------------------------------------------
# Container / cgroup hardening (Stage 8, dynamic-subagent-sizing.md §9)
# ---------------------------------------------------------------------------


class TestReadIntFile:
    def test_reads_int(self, tmp_path) -> None:
        from kiro_crew.subagent import _read_int_file

        p = tmp_path / "v"
        p.write_text("12345\n")
        assert _read_int_file(str(p)) == 12345

    def test_max_returns_none(self, tmp_path) -> None:
        from kiro_crew.subagent import _read_int_file

        p = tmp_path / "v"
        p.write_text("max\n")
        assert _read_int_file(str(p)) is None

    def test_missing_and_garbage_return_none(self, tmp_path) -> None:
        from kiro_crew.subagent import _read_int_file

        assert _read_int_file(str(tmp_path / "nope")) is None
        g = tmp_path / "g"
        g.write_text("not-a-number\n")
        assert _read_int_file(str(g)) is None


class TestCgroupAvailable:
    def test_v2_headroom(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        vals = {
            "/sys/fs/cgroup/memory.max": 16 * 1024 ** 3,
            "/sys/fs/cgroup/memory.current": 2 * 1024 ** 3,
        }
        monkeypatch.setattr(sub, "_read_int_file", lambda p: vals.get(p))
        assert sub._cgroup_available_gb() == pytest.approx(14.0, abs=0.01)

    def test_v2_unlimited_max_falls_through_to_minus_one(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        # memory.max == 'max' → _read_int_file None; v1 absent → -1.0 (unlimited)
        monkeypatch.setattr(sub, "_read_int_file", lambda p: None)
        assert sub._cgroup_available_gb() == -1.0

    def test_sentinel_large_limit_is_unlimited(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        vals = {"/sys/fs/cgroup/memory.max": sub._CGROUP_UNLIMITED + 1}
        monkeypatch.setattr(sub, "_read_int_file", lambda p: vals.get(p))
        assert sub._cgroup_available_gb() == -1.0

    def test_v1_headroom(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        vals = {
            "/sys/fs/cgroup/memory.max": None,  # v2 absent
            "/sys/fs/cgroup/memory/memory.limit_in_bytes": 8 * 1024 ** 3,
            "/sys/fs/cgroup/memory/memory.usage_in_bytes": 3 * 1024 ** 3,
        }
        monkeypatch.setattr(sub, "_read_int_file", lambda p: vals.get(p))
        assert sub._cgroup_available_gb() == pytest.approx(5.0, abs=0.01)


class TestAvailableMemoryClamp:
    def test_clamps_to_cgroup_when_smaller(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        # Force the Linux branch so the clamp logic runs regardless of test host.
        monkeypatch.setattr(sub.platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(sub.platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(sub, "check_memory_available", lambda **k: (True, 100.0))
        monkeypatch.setattr(sub, "_cgroup_available_gb", lambda: 14.0)
        assert sub._available_memory_gb() == pytest.approx(14.0, abs=0.01)

    def test_unconstrained_uses_host(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(sub.platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(sub, "check_memory_available", lambda **k: (True, 100.0))
        monkeypatch.setattr(sub, "_cgroup_available_gb", lambda: -1.0)
        assert sub._available_memory_gb() == pytest.approx(100.0, abs=0.01)

    def test_linux_unreadable_fails_open(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(sub.platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(sub, "check_memory_available", lambda **k: (True, -1.0))
        # cgroup not even consulted when host is unreadable
        assert sub._available_memory_gb() == -1.0

    def test_cgroup_clamp_lowers_computed_cap(self, monkeypatch) -> None:
        """End-to-end: a 6 GB cgroup cap on a big host caps the count via memory."""
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(sub.platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(sub, "check_memory_available", lambda **k: (True, 174.7))
        monkeypatch.setattr(sub, "_cgroup_available_gb", lambda: 4.0)  # headroom 4 GB
        monkeypatch.setattr(sub.os, "cpu_count", lambda: 48)
        cfg = _cfg(mem_cost=0.315, cpu_cost=0.8, hard_cap=16)
        # mem_term = floor(4*0.8/0.315)=10 ; cpu_term=48 → min 10, clamp(10,3,16)=10
        assert compute_max_subagents(cfg) == 10

    # --- platform dispatch -------------------------------------------------

    def test_macos_branch_uses_macos_probe(self, monkeypatch) -> None:
        """On macOS, dispatch delegates to the vm_stat probe (not /proc)."""
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(sub.platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(sub, "_macos_available_memory_gb", lambda: 42.0)
        assert sub._available_memory_gb() == 42.0

    def test_unsupported_platform_fails_open(self, monkeypatch) -> None:
        """A platform with no probe yet (e.g. Windows) fails open to -1.0."""
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(sub.platform_compat, "IS_MACOS", False)
        assert sub._available_memory_gb() == -1.0


# ---------------------------------------------------------------------------
# Queued-spawn parameter preservation + reap-drains-queue (round-2 bugfix)
# ---------------------------------------------------------------------------


class TestMacosMemoryProbe:
    """macOS available-memory calc, exercised via the mockable page-count seam.

    The Mach ``host_statistics64`` reader itself is macOS-only (pragma: no
    cover, validated live against vm_stat); these tests drive the surrounding
    GB math + failure handling deterministically on any host.
    """

    def test_computes_available_gb_from_pages(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.os, "sysconf", lambda _n: 16384)  # 16 KiB pages
        monkeypatch.setattr(sub, "_macos_vm_reclaimable_pages", lambda: 200000)
        expected = round(200000 * 16384 / (1024 ** 3), 2)
        assert sub._macos_available_memory_gb() == pytest.approx(expected, abs=0.01)

    def test_none_page_count_fails_open(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.os, "sysconf", lambda _n: 16384)
        monkeypatch.setattr(sub, "_macos_vm_reclaimable_pages", lambda: None)
        assert sub._macos_available_memory_gb() == -1.0

    def test_zero_page_count_fails_open(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.os, "sysconf", lambda _n: 16384)
        monkeypatch.setattr(sub, "_macos_vm_reclaimable_pages", lambda: 0)
        assert sub._macos_available_memory_gb() == -1.0

    def test_sysconf_error_fails_open(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        def _boom(_n):
            raise ValueError("SC_PAGE_SIZE unavailable")

        monkeypatch.setattr(sub.os, "sysconf", _boom)
        assert sub._macos_available_memory_gb() == -1.0

    def test_nonpositive_page_size_fails_open(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        monkeypatch.setattr(sub.os, "sysconf", lambda _n: 0)
        # _macos_vm_reclaimable_pages must not even be consulted
        monkeypatch.setattr(
            sub, "_macos_vm_reclaimable_pages", lambda: pytest.fail("should not run")
        )
        assert sub._macos_available_memory_gb() == -1.0


class TestQueuedSpawnParamsPreserved:
    """A queued spawn must drain with ALL its spawn() kwargs intact.

    The queue previously stored only (task, parent, agent, max_turns, cwd), so a
    drained spawn silently lost approval_mode / silent / model / allowed_tools /
    bare — an auto (headless) spawn hit the deny-by-default gate and a silent
    spawn started emitting output.
    """

    def test_drain_forwards_all_spawn_kwargs(self) -> None:
        import time as _t
        from unittest.mock import MagicMock

        m = _mgr(running=0, max_concurrent=16, last_ts=_t.monotonic() - 100.0, stagger=2.0)
        # Seed the queue exactly as spawn() now does, with non-default kwargs.
        m._queue.append(
            {
                "task": "do work",
                "parent_session_key": "p1",
                "agent": "kirocrew",
                "max_turns": 7,
                "model": "claude-x",
                "allowed_tools": ["fs_read"],
                "bare": True,
                "cwd": "/tmp/ws",
                "approval_mode": "auto",
                "silent": True,
            }
        )
        captured = {}
        m.spawn = MagicMock(side_effect=lambda **kw: captured.update(kw))  # type: ignore[method-assign]
        m._drain_queue()
        m.spawn.assert_called_once()
        # Every parameter survives the queue round-trip.
        assert captured["approval_mode"] == "auto"
        assert captured["silent"] is True
        assert captured["model"] == "claude-x"
        assert captured["allowed_tools"] == ["fs_read"]
        assert captured["bare"] is True
        assert captured["max_turns"] == 7
        assert captured["cwd"] == "/tmp/ws"
        assert captured["parent_session_key"] == "p1"


class TestForceReapDrainsQueue:
    """_force_reap frees a slot; it must pump the queue so a queued spawn starts.

    Previously _force_reap decremented _running_count but never called
    _drain_queue, so queued spawns were stranded until an unrelated agent
    finished normally or a new spawn arrived.
    """

    @pytest.mark.asyncio
    async def test_force_reap_calls_drain_queue(self) -> None:
        import time as _t
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentInfo

        m = _mgr(running=3, max_concurrent=3, last_ts=_t.monotonic() - 100.0)
        m._queue.append({"task": "queued", "parent_session_key": "", "agent": "",
                         "max_turns": 0, "model": None, "allowed_tools": None,
                         "bare": False, "cwd": "", "approval_mode": None, "silent": False})
        m._drain_queue = MagicMock()  # type: ignore[method-assign]
        m._sessions = MagicMock()
        m._write_tombstone = MagicMock()  # type: ignore[method-assign]
        m._record_cost = MagicMock()  # type: ignore[method-assign]

        info = SubagentInfo(id="a1", task="running", agent="")
        await m._force_reap("a1", info, elapsed=999.0)

        assert m._running_count == 2  # slot freed
        m._drain_queue.assert_called_once()  # queue pumped


class TestLastSampleAndMemoryRows:
    """The task-manager surface needs the CURRENT sample, which a high-water mark
    cannot express: a peak never comes back down, so a task that grew and then
    released memory would read as still holding it."""

    def _agent(self, **kw):
        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(id=kw.pop("id", "a1"), task=kw.pop("task", "t"), agent="kirocrew")
        info._pid = 4242
        for k, v in kw.items():
            setattr(info, k, v)
        return info

    def test_last_rss_follows_down_while_peak_holds(self, monkeypatch) -> None:
        import kiro_crew.subagent as sub

        m = _mgr(running=1, max_concurrent=16, last_ts=0.0)
        info = self._agent()
        m._agents = {"a1": info}
        monkeypatch.setattr(sub, "_subtree_cpu_jiffies", lambda pid: 0)
        rss_seq = iter([2 * 1024 * 1024, 1 * 1024 * 1024])
        monkeypatch.setattr(sub, "_proc_rss_kb", lambda pid: next(rss_seq))
        m._sample_live_costs()
        m._sample_live_costs()

        assert info.peak_rss_gb == pytest.approx(2.0, abs=0.01)
        assert info.last_rss_gb == pytest.approx(1.0, abs=0.01)

    def test_shared_agents_report_a_divided_last_sample(self, monkeypatch) -> None:
        """Sharing agents all report the SAME runtime pid, so the per-agent figure
        is the runtime's measurement split between them."""
        import kiro_crew.subagent as sub

        m = _mgr(running=2, max_concurrent=16, last_ts=0.0)
        a = self._agent(id="a1", _session_sharing=True)
        b = self._agent(id="a2", _session_sharing=True)
        m._agents = {"a1": a, "a2": b}
        monkeypatch.setattr(sub, "_subtree_cpu_jiffies", lambda pid: 0)
        monkeypatch.setattr(sub, "_proc_rss_kb", lambda pid: 2 * 1024 * 1024)
        m._sample_live_costs()

        assert a.last_rss_gb == pytest.approx(1.0, abs=0.01)

    def test_memory_rows_expose_the_samples_in_mb(self) -> None:
        m = _mgr(running=1, max_concurrent=16, last_ts=0.0)
        info = self._agent(last_rss_gb=1.5, peak_rss_gb=2.0, last_cpu_cores=0.25)
        info.parent_session_key = "dashboard:a"
        m._agents = {"a1": info}

        (row,) = m.task_memory_rows()
        assert row["rss_mb"] == pytest.approx(1536.0)
        assert row["peak_rss_mb"] == pytest.approx(2048.0)
        assert row["cpu_cores"] == pytest.approx(0.25)
        assert row["parent"] == "dashboard:a"
        assert row["sampled"] is True

    def test_never_sampled_task_is_marked_unsampled(self) -> None:
        """0 MB and "not measured yet" are different claims; the reaper may not
        have swept a fresh task, and rendering that as 0 would be a lie."""
        m = _mgr(running=1, max_concurrent=16, last_ts=0.0)
        m._agents = {"a1": self._agent()}

        (row,) = m.task_memory_rows()
        assert row["sampled"] is False

    def test_done_and_queued_agents_are_excluded(self) -> None:
        m = _mgr(running=1, max_concurrent=16, last_ts=0.0)
        m._agents = {
            "a1": self._agent(id="a1", done=True),
            "a2": self._agent(id="a2", queued=True),
            "a3": self._agent(id="a3", last_rss_gb=0.5),
        }

        assert [r["id"] for r in m.task_memory_rows()] == ["a3"]
