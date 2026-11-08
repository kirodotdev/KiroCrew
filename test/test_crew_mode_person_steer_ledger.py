"""A person's steer as one ``steer`` event on a crew member's work-ledger item.

``record_person_steer_result`` is the one write; the route that types into a crew
member and calls it lands with the Crew panel. Pinned here: the event lands with
actor ``person`` and moves no field; an unknown item and a crew-log append that does
not land commit nothing; a steer newer than a ``question`` answers it on the board.
Reuses the two-conductor fixture of ``test_work_ledger_tools``.
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
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.handlers import work_ledger_worker as worker_routes


@pytest.fixture(autouse=True)
def _a_unit_for_the_steer(_floor_monkeypatch):
    # ``_steer_unit`` resolves the crew member's live crew-log unit from the state's
    # sessions; a mocked state has none, so the unit is named here.
    _floor_monkeypatch.setattr(
        worker_routes, "_steer_unit", lambda state, keys, board: "unit:steer"
    )


def _events(board: str, item_id: str) -> list[dict]:
    return [dict(e) if isinstance(e, dict) else e.__dict__ for e in wl.read_events(board, item_id)]


@pytest.mark.asyncio
async def test_a_steer_lands_as_one_person_event_and_moves_no_field(monkeypatch):
    ids = await two_by_two()
    status, _ = await _report(WORKER_A, {"status": "progress", "summary": "A moving"})
    assert status == 200
    before = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    recorded: list[dict] = []
    monkeypatch.setattr(
        routes.crew_log_emit, "on_work_recorded", lambda unit, data: recorded.append(data) or True
    )

    item_id, why = await worker_routes.record_person_steer_result(
        object(), CONDUCTOR_A, ids["item_a"], "look at the second file first"
    )

    assert (item_id, why) == (ids["item_a"], "")
    after = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    assert after is not None and before is not None
    # No conductor- or worker-owned field moved: the steer is only an event line.
    for field in ("status", "summary", "decision", "verdict", "acceptance", "state"):
        assert getattr(after, field) == getattr(before, field), field
    steers = [e for e in _events(CONDUCTOR_A, ids["item_a"]) if e.get("kind") == "steer"]
    assert len(steers) == 1
    assert "second file" in str(steers[0].get("text"))
    # One ``work/recorded`` entry, actor ``person``, action ``steer``, into the named unit.
    entry = next(e for e in recorded if e.get("action") == "steer")
    assert entry["actor"] == "person" and entry["event_kind"] == "steer"
    # The other conductor's ledger is untouched.
    assert not [e for e in _events(CONDUCTOR_B, ids["item_b"]) if e.get("kind") == "steer"]


@pytest.mark.asyncio
async def test_an_unknown_item_and_a_closed_item_record_nothing():
    ids = await two_by_two()
    assert await worker_routes.record_person_steer_result(
        object(), CONDUCTOR_A, "it_00000000", "hello"
    ) == ("", worker_routes.STEER_NO_ITEM)
    # An item on the OTHER conductor's ledger is unknown to this one.
    assert await worker_routes.record_person_steer_result(
        object(), CONDUCTOR_A, ids["item_b"], "hello"
    ) == ("", worker_routes.STEER_NO_ITEM)
    assert not [e for e in _events(CONDUCTOR_B, ids["item_b"]) if e.get("kind") == "steer"]


@pytest.mark.asyncio
async def test_an_append_that_does_not_land_undoes_the_event(monkeypatch):
    """A refusal is not a commit: when the crew log does not confirm the entry, the
    cache shows no steer and the caller learns it was not recorded."""
    ids = await two_by_two()
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)

    assert await worker_routes.record_person_steer_result(
        object(), CONDUCTOR_A, ids["item_a"], "go left"
    ) == ("", worker_routes.STEER_NOT_RECORDED)
    assert not [e for e in _events(CONDUCTOR_A, ids["item_a"]) if e.get("kind") == "steer"]


def test_a_newer_steer_answers_an_outstanding_question():
    """The person outranks the conductor: a steer after a ``question`` report is the
    answer the board shows, the same as a conductor's decision would be; a steer from
    before the question is not."""
    from kiro_crew.dashboard.handlers.work_ledger_board import _is_outstanding

    item = wl.WorkItem(status="question", last_report_at="2026-09-22T04:00:00+00:00")
    asked = {"ts": "2026-09-22T04:00:00+00:00", "kind": "report", "status": "question"}
    assert _is_outstanding(item, [asked]) is True
    later = {"ts": "2026-09-22T04:30:00+00:00", "kind": "steer", "status": None}
    assert _is_outstanding(item, [asked, later]) is False
    earlier = {"ts": "2026-09-22T03:30:00+00:00", "kind": "steer", "status": None}
    assert _is_outstanding(item, [earlier, asked]) is True
