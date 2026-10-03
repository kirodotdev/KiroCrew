"""Runner-lane load earns an exec-cap increase, not only a cut.

The adaptive exec track reads demand, completions and progress from the
sub-agent manager. Workflow ``ctx.agent()`` and TaskRunner steps run on the
runner lane instead, so their load could cut the cap (start timeouts feed
``record_start``) but never earn a step above the fresh-start value. These
tests pin that a lane the controller reads feeds the SAME ``Sample`` fields:
occupancy as demand, committed ``done`` settles as completions, fresh settles
as progress -- and that the lane's own completion counter counts a committed
``done``, never a grant or a fail/cancel.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest
from overload_fakes import Clock

from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.config.loader import KiroCrewConfig

pytestmark = pytest.mark.timeout(30)


class FakeManager:
    """The ``ExecActuator`` surface with an empty run table -- no sub-agent
    load at all, so every increase here is earned on runner-lane evidence."""

    def __init__(self, user_max: int = 64) -> None:
        self._user = user_max
        self.running_count = 0
        self._queue: list[dict[str, Any]] = []
        self._agents: dict[str, Any] = {}
        self.effective: Optional[int] = None

    @property
    def user_max_concurrent(self) -> int:
        return self._user

    def set_effective_cap(self, cap: Optional[int]) -> int:
        self.effective = cap
        return self._user if cap is None else min(self._user, cap)


class FakeLane:
    """Stands in for ``RunnerLane.stats()``: workflow occupancy and the
    cumulative committed-``done`` counter the controller diffs."""

    def __init__(self) -> None:
        self.running = 0
        self.waiting = 0
        self.settled_ok = 0

    def stats(self) -> dict[str, Any]:
        return {
            "name": "runner-lane",
            "running": self.running,
            "waiting": self.waiting,
            "settled_ok": self.settled_ok,
        }


def _cfg(**agent_over: object) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    for k, v in agent_over.items():
        setattr(cfg.agent, k, v)
    return cfg


def _controller(
    manager: FakeManager, lane: Optional[FakeLane], *, cfg: Optional[KiroCrewConfig] = None
) -> tuple[AdaptiveController, Clock]:
    clock = Clock()
    host = HostSample(free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000)
    ctl = AdaptiveController(
        manager,  # type: ignore[arg-type]
        cfg=cfg or _cfg(),
        read_runner_lane=(lane.stats if lane else None),
        host_probe=lambda: host,
        clock=clock,
        sleep=AsyncMock(),
    )
    return ctl, clock


class TestRunnerLaneFeedsTheExecTrack:
    @pytest.mark.asyncio
    async def test_build_sample_folds_lane_occupancy_and_completions(self) -> None:
        mgr = FakeManager()
        lane = FakeLane()
        ctl, clock = _controller(mgr, lane)
        # Workflow load only: six running on the lane, forty queued, and three
        # committed completions since the controller started watching.
        lane.running = 6
        lane.waiting = 40
        await ctl.tick(loop_lag_ms=5.0)  # first read seeds the completion base
        s0 = ctl._samples[-1]
        assert s0.running == 6 and s0.queued == 40
        assert s0.completions == 0  # nothing credited before the base is seeded
        lane.settled_ok = 3
        clock.advance(5)
        await ctl.tick(loop_lag_ms=5.0)
        s1 = ctl._samples[-1]
        assert s1.running == 6 and s1.queued == 40
        assert s1.completions == 3  # the delta since the seeded base
        assert s1.progressing >= 1  # a fresh settle marks the lane progressing

    @pytest.mark.asyncio
    async def test_the_first_lane_reading_credits_no_stale_completion(self) -> None:
        mgr = FakeManager()
        lane = FakeLane()
        lane.settled_ok = 99  # work that finished before the controller watched
        ctl, _ = _controller(mgr, lane)
        await ctl.tick(loop_lag_ms=5.0)
        assert ctl._samples[-1].completions == 0

    @pytest.mark.asyncio
    async def test_a_lane_counter_reset_is_absorbed_not_read_negative(self) -> None:
        mgr = FakeManager()
        lane = FakeLane()
        ctl, clock = _controller(mgr, lane)
        lane.settled_ok = 10
        await ctl.tick(loop_lag_ms=5.0)
        clock.advance(5)
        lane.settled_ok = 14
        await ctl.tick(loop_lag_ms=5.0)
        assert ctl._samples[-1].completions == 4
        # A re-wire builds a fresh lane: the counter drops. The base resets to
        # it, crediting nothing, rather than reading a negative delta.
        clock.advance(5)
        lane.settled_ok = 2
        await ctl.tick(loop_lag_ms=5.0)
        assert ctl._samples[-1].completions == 4  # unchanged, no negative
        clock.advance(5)
        lane.settled_ok = 5
        await ctl.tick(loop_lag_ms=5.0)
        assert ctl._samples[-1].completions == 7  # 4 + (5 - 2)

    @pytest.mark.asyncio
    async def test_workflow_only_load_climbs_past_adaptive_initial(self) -> None:
        """Acceptance: with only workflow lane load at the cap and a clear
        host, the exec cap climbs past ``adaptive_initial`` toward the user
        ceiling under the existing earn rules."""
        mgr = FakeManager(user_max=64)
        lane = FakeLane()
        ctl, clock = _controller(mgr, lane, cfg=_cfg(adaptive_initial=4, adaptive_slow_start=False))
        assert mgr.effective == 4  # fresh-start cap

        # Keep the lane saturated at the live cap with a deep queue, and land a
        # full wave of committed completions each clean window.
        done = 0
        for _ in range(12):
            clock.advance(31)  # past the 30s clean/increase window
            cap = mgr.effective or 4
            lane.running = cap
            lane.waiting = 60
            done += cap  # a full wave of the current cap completes
            lane.settled_ok = done
            await ctl.tick(loop_lag_ms=5.0)

        assert (mgr.effective or 0) > 4, ctl.state()
        # It keeps climbing toward the ceiling, not stalling one step above.
        assert (mgr.effective or 0) >= 8, ctl.state()

    @pytest.mark.asyncio
    async def test_no_lane_reader_is_a_noop(self) -> None:
        mgr = FakeManager()
        ctl, _ = _controller(mgr, None)
        await ctl.tick(loop_lag_ms=5.0)
        s = ctl._samples[-1]
        assert s.running == 0 and s.queued == 0 and s.completions == 0

    @pytest.mark.asyncio
    async def test_build_sample_under_mixed_load_keeps_floor_and_judges_per_point(self) -> None:
        """What ``build_sample`` ACTUALLY produces under mixed manager + lane
        load -- the layer the hand-set policy tests cannot cover.

        Two running on each admission point with the cap at 4 (each point
        bounded by the same cap). Pins both reviewer concerns so neither
        regression can return silently:

        * ``healthy_in_flight`` counts only the stall-detected manager runs,
          never the lane slots (the lane has no stall signal), so the cut
          floor stays manager-only -- a corroborated halving is not propped up.
        * ``saturating`` / ``saturating_demand`` are the busier point, NOT the
          sum: two + two at cap 4 leaves both at 2, so neither point is
          saturated and the earn gate does not fire on a crossed sum.
        """
        mgr = FakeManager(user_max=64)
        mgr.running_count = 2  # two sub-agent runs in flight
        mgr._queue = [{}, {}, {}]  # three queued on the manager
        mgr._agents = {}  # none stalled -> all manager running is healthy
        lane = FakeLane()
        lane.running = 2  # two workflow/TaskRunner runs on the lane
        lane.waiting = 5  # five queued on the lane
        ctl, _ = _controller(mgr, lane)

        await ctl.tick(loop_lag_ms=5.0)
        s = ctl._samples[-1]

        # Demand is the sum across both admission points.
        assert s.running == 4  # 2 manager + 2 lane
        assert s.queued == 8  # 3 manager + 5 lane

        # Finding 1: the cut floor is manager-only. Lane running (2) is NOT
        # folded into healthy_in_flight, so with no stalled manager runs the
        # floor is exactly the manager running count.
        assert s.healthy_in_flight == 2

        # Finding 2: at-cap is judged PER point via the busier one, never the
        # sum. Running per point is 2 and 2 -> saturating is 2 (not 4). Demand
        # per point is 2+3=5 (manager) and 2+5=7 (lane) -> saturating_demand is
        # 7, the busier point, not the 12-sum.
        assert s.saturating == 2
        assert s.saturating_demand == 7

    @pytest.mark.asyncio
    async def test_build_sample_single_point_at_cap_saturates(self) -> None:
        """The mirror of the mixed case: when ONE point alone reaches the cap,
        ``saturating`` reflects it (so a genuinely saturated point still earns),
        proving the per-point max is a max and not a floor of the sum."""
        mgr = FakeManager(user_max=64)
        mgr.running_count = 0
        mgr._agents = {}
        lane = FakeLane()
        lane.running = 4  # the lane alone is at cap 4
        lane.waiting = 10
        ctl, _ = _controller(mgr, lane)

        await ctl.tick(loop_lag_ms=5.0)
        s = ctl._samples[-1]
        assert s.saturating == 4  # the busier (and only) running point
        assert s.saturating_demand == 14  # 4 + 10 on the lane point
        assert s.healthy_in_flight == 0  # no manager runs -> empty floor
