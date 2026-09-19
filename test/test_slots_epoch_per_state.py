"""A second state in one process must not reuse the epoch its generation restarted under."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from kiro_crew.dashboard.state import SLOTS_EPOCH, DashboardState, _slots_ws_frame


def _state() -> DashboardState:
    return DashboardState(
        sessions=MagicMock(),
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
    )


def _frame(state: DashboardState) -> dict:
    built = _slots_ws_frame(
        [],
        slots_epoch=state.slots_epoch,
        yolo=False,
        channel_trusted=False,
        gitlab_hosts_gen=0,
        folders=[],
        folders_gen=0,
        governance_gen=0,
        slots_gen=state._slots_generation,
    )
    return built if isinstance(built, dict) else json.loads(built)


def test_each_state_mints_its_own_epoch() -> None:
    first = _state()
    second = _state()

    assert first.slots_epoch != second.slots_epoch
    assert len(first.slots_epoch) == 32


def test_a_restarted_generation_is_not_comparable_to_the_old_one() -> None:
    first = _state()
    for _ in range(3):
        first.next_slots_generation()
    advanced = _frame(first)

    # an in-process restart: the counter goes back to 0
    second = _state()
    restarted = _frame(second)

    assert restarted["slotsGeneration"] < advanced["slotsGeneration"]
    # ... so the pair MUST differ in the epoch, or the client reads the fresh
    # snapshot as stale and refuses it
    assert restarted["slotsEpoch"] != advanced["slotsEpoch"]


def test_the_module_default_remains_for_callers_without_a_state() -> None:
    assert len(SLOTS_EPOCH) == 32
