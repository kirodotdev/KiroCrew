"""Tests for the resource sampler's CPU-delta pass.

CPU is the one figure a single ``/proc`` read cannot answer: it is a *rate*, so
the sampler must remember the previous tick total per root pid and divide the
difference by elapsed time. The behaviours that must hold, and that a naive delta
gets wrong:

* the *first* observation of a root has no baseline, so its CPU is unavailable
  (``None``), never ``0.0`` — "I have not measured yet" is not "it is idle";
* a computed percentage is bounded: never negative (a torn read or clock skew
  cannot report anti-work) and never above ``100 * cpu_count`` (a tree cannot use
  more CPU than the host has);
* a root that leaves the live set has its baseline pruned, so the cache neither
  leaks nor lets a recycled pid delta against a dead tree's ticks;
* off Linux there are no per-pid tick reads, so CPU is unavailable.

The ``/proc`` layer is faked by monkeypatching the probes the module imports (the
same seam ``test_resource_monitor`` uses), so these tests are hermetic and touch
neither the host's real process table nor the concurrently-authored registry.

The property test (Property 2: CPU bounds) drives ``_cpu_pct_for_root`` over
random tick/time sequences and asserts the bound and the first-sight rule hold
for every successive pair.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.acp import resource_monitor as rm

# A sentinel gateway self-pid, distinct from every runtime pid these tests use,
# so the gateway-self entry the sampler now always walks (task 2.3) does not
# collide with a runtime under test. Tests pin ``rm.os.getpid`` to it.
_GW = 999_001


class _FakeRuntime:
    """A live runtime as the sampler reads it: a pid plus identity attributes."""

    def __init__(
        self,
        pid: int | None,
        *,
        agent: str = "kirocrew",
        spawn_monotonic: float | None = 100.0,
    ) -> None:
        self.pid = pid
        self._agent = agent
        self._spawn_monotonic = spawn_monotonic


def _fake_linux_proc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tree: dict[int, list[int]],
    ticks: dict[int, int | None],
) -> None:
    """Install a faked Linux ``/proc`` layer for CPU reads.

    ``tree`` maps a root pid to the pids ``_iter_descendant_pids`` returns.
    ``ticks`` maps a pid to the utime+stime total ``read_pid_stat`` reports; a
    ``None`` models a pid whose stat line is gone/malformed (skipped in the sum).
    RSS is stubbed to a constant so the CPU path is exercised in isolation.
    """
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(
        rm, "_iter_descendant_pids", lambda pid, max_pids=None: list(tree.get(pid, [pid]))
    )
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)

    def _stat(proc_root: str, pid: int):  # type: ignore[no-untyped-def]
        t = ticks.get(pid)
        if t is None:
            return None
        # (state, starttime, cpu_ticks) — only the cpu tick field is consumed.
        return ("R", 0.0, t)

    monkeypatch.setattr(rm, "read_pid_stat", _stat)


# ── first-sight None ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_sight_reports_cpu_unavailable_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The very first snapshot of a root has no baseline, so CPU is None."""
    _fake_linux_proc(monkeypatch, tree={7: [7, 8]}, ticks={7: 100, 8: 50})
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()
    assert snap.entries[0].cpu_pct is None
    # But the baseline was recorded for the next pass to delta against.
    assert sampler._cpu_prev[7][0] == 150


@pytest.mark.asyncio
async def test_second_sight_reports_a_bounded_percentage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second observation with more ticks over known elapsed time yields a real
    percentage. With SC_CLK_TCK ticks of work in 1s, the tree used one full core
    → 100%."""
    monkeypatch.setattr(rm, "_CLK_TCK", 100)
    clock = {"t": 1000.0}
    monkeypatch.setattr(rm.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm, "_iter_descendant_pids", lambda pid, max_pids=None: [pid])
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    current = {"t": {7: 100}}
    monkeypatch.setattr(
        rm,
        "read_pid_stat",
        lambda proc_root, pid: ("R", 0.0, current["t"].get(pid, 0)),
    )

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=0.0)
    first = await sampler.snapshot()
    assert first.entries[0].cpu_pct is None

    # Advance one second and add exactly _CLK_TCK ticks of work.
    clock["t"] = 1001.0
    current["t"] = {7: 200}
    second = await sampler.snapshot()
    # 100 ticks / 100 Hz / 1.0s = 1 core = 100%.
    assert second.entries[0].cpu_pct == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_unreadable_pid_ticks_are_skipped_in_the_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid whose stat line is gone contributes 0 to the tick sum, not an error."""
    _fake_linux_proc(monkeypatch, tree={7: [7, 8, 9]}, ticks={7: 40, 8: None, 9: 60})
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    await sampler.snapshot()
    # 40 + 60; the unreadable 8 is skipped.
    assert sampler._cpu_prev[7][0] == 100


@pytest.mark.asyncio
async def test_backward_tick_delta_is_none_not_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the tick total drops between passes (pid recycled), the delta is not
    trustworthy → None, never a negative percentage."""
    clock = {"t": 10.0}
    monkeypatch.setattr(rm.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm, "_iter_descendant_pids", lambda pid, max_pids=None: [pid])
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    current = {"t": 500}
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, current["t"]))

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=0.0)
    await sampler.snapshot()
    clock["t"] = 11.0
    current["t"] = 200  # went backward
    snap = await sampler.snapshot()
    assert snap.entries[0].cpu_pct is None


@pytest.mark.asyncio
async def test_zero_elapsed_time_is_none_not_a_divide_by_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two reads at the same instant cannot yield a rate → None, not a crash."""
    monkeypatch.setattr(rm.time, "monotonic", lambda: 42.0)  # frozen clock
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm, "_iter_descendant_pids", lambda pid, max_pids=None: [pid])
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 999))

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=0.0)
    await sampler.snapshot()
    snap = await sampler.snapshot()
    assert snap.entries[0].cpu_pct is None


@pytest.mark.asyncio
async def test_cpu_is_unavailable_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``/proc`` per-pid tick reads exist off Linux, so CPU is None even on a
    second pass (Req 7.2)."""
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    monkeypatch.setattr(rm, "_get_rss_tree_mb", lambda pid: 10.0)
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=0.0)
    await sampler.snapshot()
    snap = await sampler.snapshot()
    assert snap.entries[0].cpu_pct is None
    # No baseline is recorded off Linux — nothing to prune, nothing to leak.
    assert sampler._cpu_prev == {}


# ── pruning ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_absent_root_baseline_is_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A root present in one pass but gone the next has its CPU baseline dropped,
    so the cache does not leak dead runtimes (Req 1.6)."""
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm, "_iter_descendant_pids", lambda pid, max_pids=None: [pid])
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 10))
    # Pin the gateway self-pid to a sentinel so its own CPU baseline (task 2.3
    # walks the gateway tree too) is distinguishable from the runtime pids.
    monkeypatch.setattr(rm.os, "getpid", lambda: _GW)

    live = {"runtimes": [_FakeRuntime(7), _FakeRuntime(8)]}
    sampler = rm.ResourceSampler(live_runtimes=lambda: live["runtimes"], interval_s=0.0)
    await sampler.snapshot()
    assert set(sampler._cpu_prev) - {_GW} == {7, 8}

    # 8 exits; only 7 remains live.
    live["runtimes"] = [_FakeRuntime(7)]
    await sampler.snapshot()
    assert set(sampler._cpu_prev) - {_GW} == {7}


@pytest.mark.asyncio
async def test_vanished_root_does_not_retain_a_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root whose tree vanishes mid-walk yields no entry AND no lingering CPU
    baseline: the walked-root set the pruner uses excludes it."""
    monkeypatch.setattr(rm.sys, "platform", "linux")
    # Root 7 was measured once, then its /proc entry disappears (empty tree). The
    # gateway self-pid keeps a stable one-pid tree so it is not what we assert on.
    monkeypatch.setattr(rm.os, "getpid", lambda: _GW)
    tree = {7: [7], _GW: [_GW]}
    monkeypatch.setattr(
        rm, "_iter_descendant_pids", lambda pid, max_pids=None: list(tree.get(pid, []))
    )
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 10))

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=0.0)
    await sampler.snapshot()
    assert set(sampler._cpu_prev) - {_GW} == {7}

    tree[7] = []  # vanished
    snap = await sampler.snapshot()
    # No runtime entry remains; only the gateway self-entry survives.
    assert [e.pid for e in snap.entries] == [_GW]
    # The dead runtime's baseline is pruned; only the gateway's own remains.
    assert set(sampler._cpu_prev) == {_GW}


# ── Property 2: CPU bounds ───────────────────────────────────────────────────


@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    # A sequence of (tick_total, monotonic_now) observations of one root pid.
    samples=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=10**9),
            st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=12,
    ),
    cpu_count=st.integers(min_value=1, max_value=64),
)
def test_property_cpu_pct_is_none_or_within_bounds(
    monkeypatch: pytest.MonkeyPatch,
    samples: list[tuple[int, float]],
    cpu_count: int,
) -> None:
    """Property 2 (CPU bounds): for every successive sample pair over any tick/time
    sequence, ``_cpu_pct_for_root`` returns ``None`` or a value in
    ``[0, 100 * cpu_count]``, and returns ``None`` on the first sight of a root.

    Validates Requirement 1.3 (CPU as a bounded inter-sample percentage) and
    Requirement 1.4 (first sight → unavailable, not zero).
    """
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm.os, "cpu_count", lambda: cpu_count)

    root_pid = 4321
    sampler = rm.ResourceSampler()
    ceiling = 100.0 * cpu_count

    for index, (ticks, now) in enumerate(samples):
        # The tree's tick total is summed on the executor thread now, so it rides
        # the walk directly; ``_cpu_pct_for_root`` only does arithmetic on it.
        walk = rm._RootWalk(root_pid=root_pid, pids=[root_pid], rss_mb=1.0, cpu_ticks=ticks)
        pct = sampler._cpu_pct_for_root(walk, now)

        if index == 0:
            assert pct is None, "first sight of a root must be unavailable, not a number"
        else:
            assert pct is None or (0.0 <= pct <= ceiling), f"cpu_pct {pct} escaped [0, {ceiling}]"
