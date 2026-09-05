"""A whole plan needs a duration ceiling, not just a per-stage one.

``OrchestratorConfig`` carried only ``stage_timeout_seconds``. That bounds ONE
stage, and a plan multiplies it: ten stages at the 30-minute default is a five-hour
unattended run with nothing to stop it (issue #1783). ``max_plan_duration_seconds``
is the ceiling for the run, checked at each stage boundary — not mid-turn, because
the running stage already has its own ceiling and cutting between stages leaves
every finished stage captured on disk and resumable.

One warning fires at ``PLAN_WARN_FRACTION`` of the budget so the user can
intervene before the cut rather than only learning of it afterwards, and it is
latched in the tracker so a long plan does not re-announce it at every boundary.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import DEFAULT_MAX_PLAN_DURATION
from kiro_crew.context_management import PLAN_WARN_FRACTION, OrchestrationTracker
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    for module in ("state", "chat", "chat_orchestrator"):
        monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)


def _make_state():
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])
    return state


def _make_slot(titles=("First", "Second", "Third"), *, plan_budget=0):
    slot = _ChatSlot("plan-watchdog-slot", mode="orchestrator")
    slot._auto_run = True
    slot._stage_titles = list(titles)
    slot._plan_goal = "Test goal"
    tracker = OrchestrationTracker(stage_timeout_seconds=1800)
    tracker.max_plan_duration_seconds = plan_budget
    slot._orch_tracker = tracker
    return slot


def _stage_turns(monkeypatch, *, age_plan_by=0.0):
    """Mock the stage turn; optionally age the plan clock by *age_plan_by* seconds.

    Ageing is done by rewinding ``_plan_start`` rather than sleeping, so the test
    exercises the real ``time.monotonic()`` comparison without the wall clock.
    """
    box = {"n": 0}

    async def _mock_run_chat(state, slot, message, **kwargs):
        box["n"] += 1
        slot.append("assistant", f"stage {box['n']} output", "msg msg-a")
        if age_plan_by:
            slot._orch_tracker._plan_start -= age_plan_by

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
    return box


def _assistant_text(slot):
    return "\n".join(m.get("content", "") for m in slot.messages if m.get("role") == "assistant")


# ── The tracker's clock ──────────────────────────────────────────────────────


class TestPlanClock:
    def test_config_default_is_two_hours(self):
        from kiro_crew.config.sections import OrchestratorConfig

        assert OrchestratorConfig().max_plan_duration_seconds == DEFAULT_MAX_PLAN_DURATION
        assert DEFAULT_MAX_PLAN_DURATION == 7200

    def test_clock_starts_with_the_plan_not_with_each_stage(self):
        """RED BEFORE: there was no whole-plan clock at all.

        Re-arming it per stage would make every boundary refresh the ceiling the
        watchdog exists to enforce, so a later round must not move it.
        """
        tracker = OrchestrationTracker()
        tracker.record_round(1)
        started = tracker._plan_start
        time.sleep(0.01)
        tracker.record_round(2)

        assert tracker._plan_start == started

    def test_not_timed_out_before_the_plan_starts(self):
        tracker = OrchestrationTracker()
        tracker.max_plan_duration_seconds = 1

        assert tracker.is_plan_timed_out() is False

    def test_timed_out_once_past_the_budget(self):
        tracker = OrchestrationTracker()
        tracker.max_plan_duration_seconds = 100
        tracker.record_round(1)
        tracker._plan_start -= 101

        assert tracker.is_plan_timed_out() is True

    def test_zero_budget_disables_the_watchdog(self):
        """Falsy means disabled everywhere else in the tracker; same here."""
        tracker = OrchestrationTracker()
        tracker.max_plan_duration_seconds = 0
        tracker.record_round(1)
        tracker._plan_start -= 10_000

        assert tracker.is_plan_timed_out() is False

    def test_warning_latches_after_one_read(self):
        tracker = OrchestrationTracker()
        tracker.max_plan_duration_seconds = 100
        tracker.record_round(1)
        tracker._plan_start -= int(100 * PLAN_WARN_FRACTION) + 1

        assert tracker.plan_warning_due() is True
        assert tracker.plan_warning_due() is False

    def test_no_warning_below_the_fraction(self):
        tracker = OrchestrationTracker()
        tracker.max_plan_duration_seconds = 100
        tracker.record_round(1)
        tracker._plan_start -= 10

        assert tracker.plan_warning_due() is False

    def test_human_renderings(self):
        tracker = OrchestrationTracker()
        tracker.max_plan_duration_seconds = 7200

        assert tracker.plan_timeout_human == "2h"
        assert tracker.plan_elapsed_human == "0s"


# ── The loop's boundary check ────────────────────────────────────────────────


class TestPlanWatchdogStopsTheLoop:
    @pytest.mark.asyncio
    async def test_plan_halts_at_the_stage_boundary_past_the_budget(self, monkeypatch):
        """RED BEFORE: only the per-stage timeout existed, so this ran to the end."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot(plan_budget=100)
        box = _stage_turns(monkeypatch, age_plan_by=200)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert box["n"] == 1, "the plan advanced past its total budget"
        text = _assistant_text(slot)
        assert "exceeded its total budget" in text
        assert "✅ All 3 stages complete." not in text
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_halt_names_the_budget_and_the_elapsed_time(self, monkeypatch):
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot(plan_budget=100)
        _stage_turns(monkeypatch, age_plan_by=200)

        await _stage_loop(_make_state(), slot, auto_run=True)

        text = _assistant_text(slot)
        assert "1m40s" in text  # the 100s budget
        assert "before Stage 2" in text

    @pytest.mark.asyncio
    async def test_finished_stages_keep_their_results(self, monkeypatch, tmp_path):
        """The cut is at a boundary, so completed work stays on disk and resumable."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot(plan_budget=100)
        _stage_turns(monkeypatch, age_plan_by=200)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert (tmp_path / "sessions" / slot.key / "stage_1_result.md").exists()
        assert slot._orch_tracker._stage_results.get(1)

    @pytest.mark.asyncio
    async def test_warning_is_emitted_once_and_the_plan_continues(self, monkeypatch):
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot(plan_budget=100)
        # 80s of a 100s budget after stage 1: past 75%, under the ceiling.
        box = _stage_turns(monkeypatch, age_plan_by=40)

        await _stage_loop(_make_state(), slot, auto_run=True)

        text = _assistant_text(slot)
        warnings = [line for line in text.splitlines() if "total budget. It will stop" in line]
        assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"
        # It warns, it does not halt: the plan keeps going until the ceiling.
        assert box["n"] >= 2

    @pytest.mark.asyncio
    async def test_a_stage_gated_plan_is_never_cut_by_the_plan_budget(self, monkeypatch):
        """RED BEFORE: an attended plan was cut, counting the user's own think time.

        The clock is wall-clock from the plan's first round, and a stage-gated plan
        spends most of it parked at an approval prompt — so enforcing the ceiling
        there refused a plan the user was actively stepping through. The budget
        exists to bound UNATTENDED runtime; when the user clicks each stage they
        are the ceiling.

        Driven through a SECOND Go, because that is where the cut landed: one
        `_stage_loop` call runs a stage and returns to wait for approval, so it
        never reaches a second stage boundary — and the boundary is the only place
        the ceiling is evaluated.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        state = _make_state()
        slot = _make_slot(titles=("One", "Two"), plan_budget=100)
        box = _stage_turns(monkeypatch, age_plan_by=200)

        # First Go: stage 1 runs, the plan clock ages past the budget while the
        # user reads the result, and the loop pauses for approval.
        await _stage_loop(state, slot, auto_run=False)
        assert box["n"] == 1, "stage 1 did not run"
        assert "Click **Go**" in _assistant_text(slot), "the loop did not pause for approval"

        # Second Go: enters at stage 2 and evaluates the ceiling at its top.
        await _stage_loop(state, slot, auto_run=False)

        text = _assistant_text(slot)
        assert box["n"] == 2, "the second Go was refused by the plan budget"
        assert "exceeded its total budget" not in text
        assert "total budget. It will stop" not in text

    @pytest.mark.asyncio
    async def test_an_auto_run_plan_is_still_cut(self, monkeypatch):
        """Preservation: the ceiling still applies where it was meant to."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot(plan_budget=100)
        box = _stage_turns(monkeypatch, age_plan_by=200)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert box["n"] == 1
        assert "exceeded its total budget" in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_plan_under_its_budget_is_unaffected(self, monkeypatch):
        """Preservation: a normal plan sees neither the warning nor the halt."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot(plan_budget=DEFAULT_MAX_PLAN_DURATION)
        box = _stage_turns(monkeypatch)

        await _stage_loop(_make_state(), slot, auto_run=True)

        text = _assistant_text(slot)
        assert box["n"] == 3
        assert "✅ All 3 stages complete." in text
        assert "total budget" not in text

    @pytest.mark.asyncio
    async def test_disabled_budget_never_halts(self, monkeypatch):
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot(plan_budget=0)
        box = _stage_turns(monkeypatch, age_plan_by=100_000)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert box["n"] == 3
        assert "total budget" not in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_a_gateway_created_tracker_loads_its_budgets(self, monkeypatch, tmp_path):
        """RED BEFORE: a tracker the loop did not create ran on constructor defaults.

        The load was gated on ``tracker is None`` -- "did this loop create the
        tracker" -- which is not the same question as "does this tracker have its
        budgets". The Slack gateway creates one lazily when a subagent result lands
        on an orchestrator slot, and the plan then ran with ``_plan_timeout`` at 0,
        which means DISABLED.
        """
        import json

        from kiro_crew.dashboard.chat import _stage_loop

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {"orchestrator": {"stage_timeout_seconds": 55, "max_plan_duration_seconds": 66}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

        slot = _make_slot(titles=("One", "Two"))
        # The shape the gateway leaves behind: a tracker with no budgets, which the
        # loop did not build and therefore used to skip the load for.
        slot._orch_tracker = OrchestrationTracker()
        assert slot._orch_tracker.budgets_unset is True
        _stage_turns(monkeypatch)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert slot._orch_tracker.max_plan_duration_seconds == 66
        assert slot._orch_tracker.stage_timeout_seconds == 55

    @pytest.mark.asyncio
    async def test_a_tracker_that_already_has_budgets_does_not_reload(self, monkeypatch, tmp_path):
        """Preservation: a paused plan's later Go still pays for no config load."""
        import json

        from kiro_crew.dashboard.chat import _stage_loop

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"orchestrator": {"max_plan_duration_seconds": 66}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

        loads: list[int] = []
        real_load = KiroCrewConfig.load

        def counting_load(*args, **kwargs):
            loads.append(1)
            return real_load(*args, **kwargs)

        monkeypatch.setattr(KiroCrewConfig, "load", counting_load)

        slot = _make_slot(titles=("Only",), plan_budget=999)
        assert slot._orch_tracker.budgets_unset is False
        _stage_turns(monkeypatch)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert loads == [], "a tracker that already carries its budgets must not reload"
        assert slot._orch_tracker.max_plan_duration_seconds == 999

    @pytest.mark.asyncio
    async def test_a_failed_load_still_arms_the_plan_watchdog(self, monkeypatch, tmp_path):
        """RED BEFORE: an unreadable config silently removed the plan ceiling.

        The tracker constructs with ``_plan_timeout = 0`` and 0 means DISABLED, so
        the ``except`` branch skipping the assignment left the whole-plan watchdog
        off while the stage budget fell back to its own default -- an asymmetry
        with no reading under which it is correct.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        def raising_load(*args, **kwargs):
            raise RuntimeError("config is unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", raising_load)

        slot = _make_slot(titles=("One",))
        slot._orch_tracker = None
        _stage_turns(monkeypatch)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert slot._orch_tracker.max_plan_duration_seconds == DEFAULT_MAX_PLAN_DURATION
        assert slot._orch_tracker.stage_timeout_seconds == 1800

    @pytest.mark.asyncio
    async def test_a_failed_load_is_not_retried_on_the_next_entry(self, monkeypatch, tmp_path):
        """One bad config read must not become one per stage-loop entry."""
        from kiro_crew.dashboard.chat import _stage_loop

        loads: list[int] = []

        def raising_load(*args, **kwargs):
            loads.append(1)
            raise RuntimeError("config is unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", raising_load)

        slot = _make_slot(titles=("One",))
        slot._orch_tracker = None
        _stage_turns(monkeypatch)

        await _stage_loop(_make_state(), slot, auto_run=True)
        assert loads == [1]
        assert slot._orch_tracker.budgets_unset is False

        # A second entry (the user's next Go) re-uses the same tracker.
        await _stage_loop(_make_state(), slot, auto_run=True)

        assert loads == [1], "the failed load was re-attempted on a later entry"

    @pytest.mark.asyncio
    async def test_configured_budget_reaches_the_tracker(self, monkeypatch, tmp_path):
        """The loop's bootstrap load must apply BOTH budgets, not just the stage one."""
        import json

        from kiro_crew.dashboard.chat import _stage_loop

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {"orchestrator": {"stage_timeout_seconds": 55, "max_plan_duration_seconds": 66}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

        slot = _make_slot(titles=("Only",))
        slot._orch_tracker = None  # force the bootstrap path that loads config
        _stage_turns(monkeypatch)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert slot._orch_tracker.stage_timeout_seconds == 55
        assert slot._orch_tracker.max_plan_duration_seconds == 66
