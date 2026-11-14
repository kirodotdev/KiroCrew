"""A conductor mode run's settled verdict: done, stopped, or still going.

A run is settled once its board has items and no active loop is on the conductor's
slot: ``done`` when every item is closed, ``stopped`` when some are open. The same
verdict reaches the Sessions row (``crew_work``'s ``run``) and the Board
(``GET /api/crew-board``'s ``run``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import bus
from kiro_crew.dashboard import conductor_mode_run, crew_work
from kiro_crew.dashboard.handlers import work_ledger_board as board
from kiro_crew.monitoring.models import MonitorOutcome


def _facts(open_items, accepted, not_done, patrol):
    return conductor_mode_run.run_facts(
        open_items=open_items, accepted=accepted, not_done=not_done, patrol=patrol
    )


@pytest.mark.parametrize(
    ("counts", "patrol", "settled", "at_limit"),
    [
        # No item yet: nothing to call finished.
        ((0, 0, 0), "off", "", False),
        # The patrol is still on: between rounds, or the final report is not sent.
        ((0, 3, 0), "on", "", False),
        ((2, 1, 0), "on", "", False),
        ((0, 3, 0), "off", "done", False),
        ((0, 2, 1), "off", "done", False),
        ((0, 2, 1), "limit", "done", False),
        ((2, 1, 0), "off", "stopped", False),
        ((2, 1, 0), "limit", "stopped", True),
    ],
)
def test_the_verdict_reads_the_item_states_and_the_patrol(counts, patrol, settled, at_limit):
    run = _facts(*counts, patrol)
    assert run["settled"] == settled
    assert run["at_limit"] is at_limit
    assert (run["open"], run["accepted"], run["not_done"]) == counts


def test_a_new_goal_with_no_item_yet_is_not_settled_by_the_old_goal_s_items():
    assert _facts(0, 3, 0, "off")["settled"] == "done"
    assert goal_run(goal_items=0)["settled"] == ""
    assert goal_run(goal_items=2)["settled"] == "done"
    # None: the caller holds no goal count (the Sessions row), so nothing is gated.
    assert goal_run(goal_items=None)["settled"] == "done"


def goal_run(*, goal_items):
    return conductor_mode_run.run_facts(
        open_items=0, accepted=3, not_done=0, patrol="off", goal_items=goal_items
    )


def test_item_counts_split_accepted_from_not_done_and_add_forgotten_closes():
    states = ["open", "accepted", "rejected", "abandoned", "accepted"]
    forgotten = {"accepted": 4, "rejected": 1, "abandoned": 2, "items": 7, "created": 9}
    assert conductor_mode_run.item_counts(states) == (1, 2, 2)
    assert conductor_mode_run.item_counts(states, forgotten) == (1, 6, 5)
    assert conductor_mode_run.item_counts(states, {"accepted": "x", "rejected": True}) == (1, 2, 2)


def _monitor_loop(active, outcome=None):
    return SimpleNamespace(active=active, monitor=SimpleNamespace(outcome=outcome))


@pytest.mark.parametrize(
    ("loop", "expected"),
    [
        (None, "off"),
        (_monitor_loop(True), "on"),
        # The conductor's own autonudge_stop and a person's stop record the same outcome.
        (_monitor_loop(False, MonitorOutcome.USER_STOP), "off"),
        (_monitor_loop(False, MonitorOutcome.BUDGET), "limit"),
        (_monitor_loop(False, MonitorOutcome.TARGET_UNAVAILABLE), "off"),
        (SimpleNamespace(active=False, monitor=None, stopped_reason="runtime_budget"), "limit"),
        (SimpleNamespace(active=False, monitor=None, stopped_reason="autonudge_stop"), "off"),
    ],
)
def test_patrol_of_reads_the_first_key_holding_a_loop(loop, expected):
    loops = {"chat-7": loop}
    svc = SimpleNamespace(get_by_slot=loops.get)
    assert conductor_mode_run.patrol_of(svc, ("", "nothing-here", "chat-7")) == expected


def test_patrol_of_without_a_service_or_with_a_failing_read_is_off():
    def boom(_key):
        raise RuntimeError("table moved")

    assert conductor_mode_run.patrol_of(None, ("chat-7",)) == "off"
    assert conductor_mode_run.patrol_of(SimpleNamespace(get_by_slot=boom), ("chat-7",)) == "off"


@pytest.fixture
def _clean_crew_work():
    bus.reset_for_tests()
    crew_work.forget_for_tests()
    yield
    crew_work.forget_for_tests()
    bus.reset_for_tests()


def _row_run(monkeypatch, loop, value):
    import kiro_crew.autonudge as autonudge

    loops = {"chat-7": loop}
    monkeypatch.setattr(autonudge, "get_instance", lambda: SimpleNamespace(get_by_slot=loops.get))
    crew_work.remember("chat-7", value)
    return crew_work.summary_for_state("chat-7", SimpleNamespace(_slots={}))["run"]


def test_the_row_s_run_counts_held_and_forgotten_closes(monkeypatch, _clean_crew_work):
    value = {
        "items": [{"item_id": "a", "state": "accepted"}, {"item_id": "b", "state": "rejected"}],
        "forgotten": {"accepted": 3, "rejected": 0, "abandoned": 1},
    }
    run = _row_run(monkeypatch, _monitor_loop(False, MonitorOutcome.USER_STOP), value)
    assert run == {"settled": "done", "open": 0, "accepted": 4, "not_done": 2, "at_limit": False}


def test_the_row_s_run_is_unsettled_while_the_patrol_is_on(monkeypatch, _clean_crew_work):
    value = {"items": [{"item_id": "a", "state": "accepted"}]}
    assert _row_run(monkeypatch, _monitor_loop(True), value)["settled"] == ""


def test_the_row_s_run_is_stopped_with_open_work_and_no_patrol(monkeypatch, _clean_crew_work):
    value = {"items": [{"item_id": "a", "state": "accepted"}, {"item_id": "b", "state": "open"}]}
    assert _row_run(monkeypatch, None, value)["settled"] == "stopped"


def test_a_board_with_no_item_sends_no_run(monkeypatch, _clean_crew_work):
    import kiro_crew.autonudge as autonudge

    monkeypatch.setattr(autonudge, "get_instance", lambda: None)
    crew_work.remember("chat-7", {"items": []})
    assert "run" not in crew_work.summary_for_state("chat-7", SimpleNamespace(_slots={}))


def test_a_closed_count_change_alone_still_moves_the_row(_clean_crew_work):
    items = [{"item_id": "a", "state": "open"}]
    assert crew_work.remember("chat-7", {"items": items}) == "chat-7"
    moved = {"items": items, "forgotten": {"accepted": 1}}
    assert crew_work.remember("chat-7", moved) == "chat-7"
    assert crew_work.remember("chat-7", moved) is None


CONDUCTOR = "chat-1-conductor"
OWNER = "owner-subject"


@pytest.fixture
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _create(title: str) -> str:
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    result = wl.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}
    )
    return result["item"].item_id


async def _board_run(monkeypatch, loop):
    import kiro_crew.autonudge as autonudge

    loops = {CONDUCTOR: loop}
    monkeypatch.setattr(autonudge, "get_instance", lambda: SimpleNamespace(get_by_slot=loops.get))
    state = SimpleNamespace(_slots={}, get_slot=lambda _key: None, owner_id=OWNER)
    request = make_mocked_request(
        "GET", f"/api/crew-board?conductor={CONDUCTOR}", app={"state": state}
    )
    request["user"] = OWNER
    request["app"] = ""
    response = await board.api_work_ledger_board(request)
    assert response.status == 200
    return json.loads(response.body.decode())["run"]


@pytest.mark.asyncio
async def test_the_board_read_carries_the_run(monkeypatch, _isolated_home):
    first = _create("first")
    second = _create("second")
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=first, state="abandoned")
    stopped = await _board_run(monkeypatch, _monitor_loop(False, MonitorOutcome.USER_STOP))
    assert stopped == {
        "settled": "stopped",
        "open": 1,
        "accepted": 0,
        "not_done": 1,
        "at_limit": False,
    }
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=second, state="abandoned")
    assert (await _board_run(monkeypatch, _monitor_loop(True)))["settled"] == ""
    assert (await _board_run(monkeypatch, None))["settled"] == "done"
    # A new goal that has created nothing yet does not read the old goal's Done.
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="ship the next one", new_goal=True)
    assert (await _board_run(monkeypatch, None))["settled"] == ""
    _create("next")
    third = [i for i in wl.list_work_items(CONDUCTOR) if i.title == "next"][0].item_id
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=third, state="abandoned")
    assert (await _board_run(monkeypatch, None))["settled"] == "done"
