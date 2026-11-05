"""A person's answer to a worker's approval clears the item's "needs you".

A worker that reports ``blocked`` with reason ``approval`` waits on a person. When
the person answers a tool approval in that worker's own chat, the answer is written
as one ``steer`` event on the item (``work_ledger_worker.record_approval_answered``),
so ``work_ledger_board.awaits_person`` -- the one test behind the Board's Needs your
input, its Milestones rows, the sidebar count and goal-board -- stops reading the
item as waiting. Pinned here: raised -> answered -> gone; still unanswered -> stays;
one event per ask; nothing for an item that does not wait on an approval; a newer
report asks again. Reuses the two-conductor fixture of ``test_work_ledger_tools``.
"""

from __future__ import annotations

import pytest
from test_work_ledger_tools import (  # noqa: F401 - the autouse fixtures ride along
    CONDUCTOR_A,
    CONDUCTOR_B,
    WORKER_A,
    _clean_slots,
    _isolated_home,
    _open_route,
    _report,
    two_by_two,
)

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard import crew_work
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.handlers import work_ledger_worker as worker_routes
from kiro_crew.dashboard.handlers.work_ledger_board import awaits_person, with_worker_facts


@pytest.fixture(autouse=True)
def _the_crew_log_takes_it(_floor_monkeypatch):
    # The steer resolves a live crew-log unit from the state's sessions; a mocked
    # state has none.
    def _unit(state, keys, board):
        # Into the BOARD's unit: the ``work`` fold behind the sidebar and goal-board
        # reads only the board's own units, never the worker's.
        assert keys == ()
        return "unit:approval"

    # ``_open_route`` already turns the crew log on and lands every append; patching
    # those again on the floor monkeypatch would restore its stubs after teardown.
    _floor_monkeypatch.setattr(worker_routes, "_steer_unit", _unit)


def _events(board: str, item_id: str) -> list[dict]:
    return [e.to_dict() for e in wl.read_events(board, item_id)]


def _waits(board: str, item_id: str) -> bool:
    item = wl.read_work_item(board, item_id)
    assert item is not None
    return awaits_person(item, _events(board, item_id))


def _steers(board: str, item_id: str) -> list[dict]:
    return [e for e in _events(board, item_id) if e.get("kind") == "steer"]


async def _blocked_on_approval() -> dict[str, str]:
    ids = await two_by_two()
    status, _ = await _report(
        WORKER_A,
        {
            "status": "blocked",
            "summary": "Shell approval for git status timed out (600s).",
            "reason": "approval",
        },
    )
    assert status == 200
    return ids


@pytest.mark.asyncio
async def test_an_answered_approval_clears_the_ask():
    ids = await _blocked_on_approval()
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True

    landed = await worker_routes.record_approval_answered(object(), WORKER_A)

    assert landed == ids["item_a"]
    assert _waits(CONDUCTOR_A, ids["item_a"]) is False
    (steer,) = _steers(CONDUCTOR_A, ids["item_a"])
    assert steer["text"] == worker_routes.APPROVAL_ANSWERED_TEXT
    # The worker's own fields are untouched: the item still says what it said.
    item = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    assert item is not None and (item.status, item.reason) == ("blocked", "approval")
    # The other conductor's ledger is not touched.
    assert not _steers(CONDUCTOR_B, ids["item_b"])


@pytest.mark.asyncio
async def test_an_unanswered_ask_stays():
    ids = await _blocked_on_approval()
    # No approval was answered in the worker's chat: nothing is written, it still waits.
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True
    assert not _steers(CONDUCTOR_A, ids["item_a"])


@pytest.mark.asyncio
async def test_one_event_per_ask_and_a_new_report_asks_again(monkeypatch):
    # Stamps are seconds-precision and an event id is its stamp plus its text, so two
    # answers in one second are one event. A clock that ticks a second per write keeps
    # the two asks apart, as they are in real use.
    from datetime import datetime, timedelta, timezone

    from kiro_crew import work_ledger_store, work_ledger_writes

    start = datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc)
    ticks = iter(range(10_000))

    def _tick() -> str:
        return (start + timedelta(seconds=next(ticks))).isoformat(timespec="seconds")

    for module in (work_ledger_store, work_ledger_writes):
        monkeypatch.setattr(module, "_now_iso", _tick)
    ids = await _blocked_on_approval()
    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ids["item_a"]
    # A second answer to the same ask writes nothing more.
    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ""
    assert len(_steers(CONDUCTOR_A, ids["item_a"])) == 1

    # The worker reports a new wait on an approval: the board asks again, and the next
    # answer clears that one too.
    status, _ = await _report(
        WORKER_A, {"status": "blocked", "summary": "Needs gh approval.", "reason": "approval"}
    )
    assert status == 200
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True
    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ids["item_a"]
    assert _waits(CONDUCTOR_A, ids["item_a"]) is False


@pytest.mark.asyncio
async def test_two_answers_in_the_same_second_stay_two_events(monkeypatch):
    """Event ids are the stamp plus the text, and the answer text is fixed: the id of
    the report each answer meets keeps two same-second answers apart."""
    from kiro_crew import work_ledger_store, work_ledger_writes

    for module in (work_ledger_store, work_ledger_writes):
        monkeypatch.setattr(module, "_now_iso", lambda: "2026-11-04T07:00:00+00:00")
    ids = await _blocked_on_approval()
    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ids["item_a"]
    status, _ = await _report(
        WORKER_A, {"status": "blocked", "summary": "Needs gh approval.", "reason": "approval"}
    )
    assert status == 200
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True

    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ids["item_a"]
    assert len(_steers(CONDUCTOR_A, ids["item_a"])) == 2
    assert _waits(CONDUCTOR_A, ids["item_a"]) is False


@pytest.mark.asyncio
async def test_an_item_not_waiting_on_an_approval_records_nothing():
    ids = await two_by_two()
    for body in (
        {"status": "progress", "summary": "moving"},
        {"status": "blocked", "summary": "needs a person", "reason": "needs_human"},
        {"status": "blocked", "summary": "CI is down"},
    ):
        status, _ = await _report(WORKER_A, body)
        assert status == 200
        assert await worker_routes.record_approval_answered(object(), WORKER_A) == ""
    assert not _steers(CONDUCTOR_A, ids["item_a"])
    # A session bound to nothing records nothing.
    assert await worker_routes.record_approval_answered(object(), "chat-unbound") == ""


@pytest.mark.asyncio
async def test_a_report_filed_after_the_ask_was_picked_is_not_answered(monkeypatch):
    """The ask is judged again under the board lock: a newer report the worker filed
    while the answer was on its way (here a needs_human ask) keeps waiting."""
    ids = await _blocked_on_approval()
    picked = worker_routes._approval_ask_of(WORKER_A)
    assert picked[1] == ids["item_a"]
    status, _ = await _report(
        WORKER_A, {"status": "blocked", "summary": "Pick a region.", "reason": "needs_human"}
    )
    assert status == 200
    monkeypatch.setattr(worker_routes, "_approval_ask_of", lambda slot_key: picked)

    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ""
    assert not _steers(CONDUCTOR_A, ids["item_a"])
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True


@pytest.mark.asyncio
async def test_a_newer_approval_report_is_not_answered_by_an_older_answer(monkeypatch):
    """Same as above with a newer ``approval`` ask: the answer is pinned to the report
    it was matched to, so the newer one still waits."""
    from datetime import datetime, timedelta, timezone

    from kiro_crew import work_ledger_store, work_ledger_writes

    start = datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc)
    ticks = iter(range(10_000))

    def _tick() -> str:
        return (start + timedelta(seconds=next(ticks))).isoformat(timespec="seconds")

    for module in (work_ledger_store, work_ledger_writes):
        monkeypatch.setattr(module, "_now_iso", _tick)
    ids = await _blocked_on_approval()
    picked = worker_routes._approval_ask_of(WORKER_A)
    assert picked[1] == ids["item_a"] and picked[3]
    status, _ = await _report(
        WORKER_A, {"status": "blocked", "summary": "Needs gh approval.", "reason": "approval"}
    )
    assert status == 200
    monkeypatch.setattr(worker_routes, "_approval_ask_of", lambda slot_key: picked)

    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ""
    assert not _steers(CONDUCTOR_A, ids["item_a"])
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True


@pytest.mark.asyncio
async def test_a_crew_log_that_does_not_take_it_leaves_the_ask(monkeypatch):
    ids = await _blocked_on_approval()
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)

    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ""
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True
    assert not _steers(CONDUCTOR_A, ids["item_a"])


@pytest.mark.asyncio
async def test_the_sidebar_count_and_goal_board_agree_after_the_answer():
    ids = await _blocked_on_approval()

    def rows() -> list[dict]:
        item = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
        assert item is not None
        return [{**item.to_dict(), "events": _events(CONDUCTOR_A, ids["item_a"])}]

    def counts() -> int:
        return crew_work.summarize(rows(), closed=lambda k: False, looping=lambda k: False)[
            "person"
        ]

    class _State:
        def get_slot(self, name):
            return None

    assert counts() == 1
    assert with_worker_facts(rows(), _State())[0]["awaits_person"] is True

    await worker_routes.record_approval_answered(object(), WORKER_A)

    assert counts() == 0
    assert with_worker_facts(rows(), _State())[0]["awaits_person"] is False
