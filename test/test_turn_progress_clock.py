"""What the chat's live progress reads off the gateway: the run clock and the step clock.

* ``run_started_ts`` on a slot's projection: the epoch second the running run was
  stamped, so the chat counts a live clock up from it, and null between runs.
* ``step_started_at`` on the todo payload: the epoch second the AGENT's own snapshot
  reached the current step. A person's tick in the pill neither moves nor counts it.
* The progress-checklist rule a person's dashboard message carries, and every other
  turn does not.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import run_clock, slot_projection
from kiro_crew.dashboard.chat_turn.checklist_rule import CHECKLIST_RULE, checklist_rule_for
from kiro_crew.dashboard.state import _ChatSlot


def _bare_slot() -> _ChatSlot:
    slot = _ChatSlot.__new__(_ChatSlot)
    slot._todo = None
    slot._todo_overrides = {}
    return slot


def _plan(*rows: tuple[str, bool]) -> dict[str, Any]:
    return {
        "description": "Ship it",
        "tasks": [{"id": str(i + 1), "text": t, "completed": d} for i, (t, d) in enumerate(rows)],
    }


@pytest.fixture
def wall(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The step clock's wall reading; the test sets the next value it returns."""
    now = [1_000.0]
    monkeypatch.setattr(run_clock, "wall_now", lambda: now[0])
    return now


class TestStepClock:
    def test_the_first_agent_snapshot_starts_the_step_clock(self, wall: list[float]) -> None:
        slot = _bare_slot()
        assert slot.set_todo(_plan(("build", False), ("test", False)))
        assert slot.todo_payload()["step_started_at"] == 1_000.0

    def test_an_echo_of_the_same_step_keeps_the_clock(self, wall: list[float]) -> None:
        slot = _bare_slot()
        slot.set_todo(_plan(("build", False), ("test", False)))
        wall[0] = 1_300.0
        # Same rows, a new description: the list moved but the step did not.
        assert slot.set_todo({**_plan(("build", False), ("test", False)), "description": "x"})
        assert slot.todo_payload()["step_started_at"] == 1_000.0

    def test_an_agent_completion_starts_the_next_step(self, wall: list[float]) -> None:
        slot = _bare_slot()
        slot.set_todo(_plan(("build", False), ("test", False)))
        wall[0] = 1_720.0
        slot.set_todo(_plan(("build", True), ("test", False)))
        assert slot.todo_payload()["step_started_at"] == 1_720.0

    def test_a_person_tick_neither_resets_nor_counts(self, wall: list[float]) -> None:
        slot = _bare_slot()
        slot.set_todo(_plan(("build", False), ("test", False)))
        wall[0] = 1_100.0
        assert slot.set_todo_task_completed("1", True)
        # The pill now shows "test", a step the agent has not reached: no time.
        assert "step_started_at" not in slot.todo_payload()
        # The agent's stale echo keeps the person's tick (an override), and the
        # step the pill shows is unchanged by it, so no clock starts.
        wall[0] = 1_200.0
        slot.set_todo(_plan(("build", False), ("test", False)))
        assert "step_started_at" not in slot.todo_payload()
        # The agent confirming the person's tick is not a step of its own either.
        wall[0] = 1_300.0
        slot.set_todo(_plan(("build", True), ("test", False)))
        assert "step_started_at" not in slot.todo_payload()
        # The agent's own next step starts the clock again.
        wall[0] = 1_400.0
        slot.set_todo(_plan(("build", True), ("test", True), ("ship", False)))
        assert slot.todo_payload()["step_started_at"] == 1_400.0

    def test_a_tick_on_a_later_row_hides_the_clock_too(self, wall: list[float]) -> None:
        slot = _bare_slot()
        slot.set_todo(_plan(("build", False), ("test", False), ("ship", False)))
        wall[0] = 1_100.0
        # Ticking a later row leaves "build" current but changes the done set:
        # a step marker the agent did not reach, so the timer hides.
        assert slot.set_todo_task_completed("3", True)
        assert "step_started_at" not in slot.todo_payload()

    def test_a_cleared_list_drops_the_clock(self, wall: list[float]) -> None:
        slot = _bare_slot()
        slot.set_todo(_plan(("build", False)))
        slot.set_todo(None)
        assert slot.todo_payload() is None
        wall[0] = 2_000.0
        slot.set_todo(_plan(("build", False)))
        assert slot.todo_payload()["step_started_at"] == 2_000.0

    def test_a_slot_with_no_agent_snapshot_carries_no_stamp(self) -> None:
        slot = _bare_slot()
        slot._todo = _plan(("build", False))
        assert "step_started_at" not in slot.todo_payload()


class _Clocks:
    """``run_clock``'s ``time``: a run clock and a wall clock the test moves apart."""

    def __init__(self) -> None:
        self.mono = 42.25
        self.wall = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall


@pytest.fixture
def clocks(monkeypatch: pytest.MonkeyPatch) -> _Clocks:
    fake = _Clocks()
    monkeypatch.setattr(run_clock, "time", fake)
    return fake


class TestRunStartedTs:
    def test_a_run_stamp_projects_as_the_wall_second_read_with_it(self, clocks: _Clocks) -> None:
        slot = _ChatSlot("stamp-clock")
        slot.task = SimpleNamespace()  # type: ignore[assignment]
        assert slot_projection._run_started_ts(slot) == 1_700_000_000.0

    def test_a_sleep_between_the_clocks_does_not_shift_the_projected_start(
        self, clocks: _Clocks
    ) -> None:
        slot = _ChatSlot("sleep-clock")
        slot.task = SimpleNamespace()  # type: ignore[assignment]
        # The machine slept an hour: the wall clock moved, the run clock did not.
        clocks.wall += 3_600.0
        clocks.mono += 5.0
        assert slot_projection._run_started_ts(slot) == 1_700_000_000.0

    def test_no_stamp_projects_as_null(self) -> None:
        assert slot_projection._run_started_ts(SimpleNamespace(_run_started_at=None)) is None
        assert slot_projection._run_started_ts(SimpleNamespace()) is None
        assert slot_projection._run_started_ts(SimpleNamespace(_run_started_at=1.0)) is None

    def test_a_new_run_restarts_a_step_left_over_from_the_last_one(self, clocks: _Clocks) -> None:
        slot = _ChatSlot("idle-step")
        clocks.wall = 1_000.0
        slot.set_todo(_plan(("build", False), ("test", False)))
        assert slot.todo_payload()["step_started_at"] == 1_000.0
        # The last run ended on "build"; a person comes back 47 s later.
        clocks.wall = 1_047.0
        slot.stamp_run_start(run_clock.now(), run_clock.wall_now())
        assert slot.todo_payload()["step_started_at"] == 1_047.0

    def test_a_step_started_inside_the_run_keeps_its_own_start(self, clocks: _Clocks) -> None:
        slot = _ChatSlot("fresh-step")
        clocks.wall = 1_000.0
        slot.stamp_run_start(run_clock.now(), run_clock.wall_now())
        clocks.wall = 1_030.0
        slot.set_todo(_plan(("build", False)))
        slot.restart_step_clock(1_000.0)
        assert slot.todo_payload()["step_started_at"] == 1_030.0

    def test_an_idle_slot_projects_no_run_start(self) -> None:
        slot = _ChatSlot("idle-clock")
        slot._run_started_at = run_clock.now()
        assert slot.to_dict()["run_started_ts"] is None


def _turn(**overrides: Any) -> SimpleNamespace:
    fields = {
        "_prompt_depth": 0,
        "_directive_user_origin": True,
        "_directive_channel_origin": False,
        "_crew_log_actor": "user",
    }
    return SimpleNamespace(**{**fields, **overrides})


def _client(backend: str) -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend))


class TestChecklistRule:
    def test_a_persons_dashboard_message_on_kiro_cli_carries_the_rule(self) -> None:
        assert checklist_rule_for(_turn(), _client(ACP_BACKEND_KIRO)) == CHECKLIST_RULE

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE, "unknown-harness"])
    def test_a_harness_without_the_tool_gets_none(self, backend: str) -> None:
        assert checklist_rule_for(_turn(), _client(backend)) == ""

    @pytest.mark.parametrize(
        "overrides",
        [
            # A cron script's send_to_session carries user origin, not the user actor.
            {"_crew_log_actor": "cron"},
            # A loop wake and a sub-agent delivery are not user-origin.
            {"_directive_user_origin": False},
            # A follow-up the turn itself sends.
            {"_prompt_depth": 1},
        ],
    )
    def test_a_turn_no_person_sent_gets_none(self, overrides: dict[str, Any]) -> None:
        assert checklist_rule_for(_turn(**overrides), _client(ACP_BACKEND_KIRO)) == ""

    def test_a_turn_typed_in_a_linked_channel_gets_none(self) -> None:
        # Slack, Telegram, Teams and Discord hand-offs all carry channel origin;
        # a non-Slack one stamps no source channel, so only the origin tells.
        turn = _turn(_directive_channel_origin=True, _directive_source_channels=None)
        assert checklist_rule_for(turn, _client(ACP_BACKEND_KIRO)) == ""

    def test_the_rule_names_the_tool(self) -> None:
        assert "todo_list" in CHECKLIST_RULE
