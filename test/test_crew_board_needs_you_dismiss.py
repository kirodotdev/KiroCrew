"""A "needs you" ask clears once its worker moves on, and the person can dismiss it.

Two halves of one rule, ``work_ledger_board.awaits_person``:

* a worker whose turn runs again (the conductor messaged it, a loop woke it) has
  moved on, though nothing wrote a ledger answer, so the ask is not the person's;
* the person's dismiss (``POST /api/crew-board/action`` with ``dismiss``) writes one
  person ``steer`` on the item, so every reader drops it until a NEWER report asks.

Reuses the two-conductor fixture of ``test_work_ledger_tools``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request
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
from kiro_crew.dashboard.handlers import work_ledger_board as board
from kiro_crew.dashboard.handlers import work_ledger_worker as worker_routes

OWNER = "owner-subject"


@pytest.fixture(autouse=True)
def _the_crew_log_takes_it(_floor_monkeypatch):
    _floor_monkeypatch.setattr(worker_routes, "_steer_unit", lambda state, keys, b: "unit:dismiss")


@pytest.fixture(autouse=True)
def _ticking_clock(_floor_monkeypatch):
    # Stamps are seconds-precision; a clock that ticks per write keeps reports apart.
    from kiro_crew import work_ledger_store, work_ledger_writes

    start = datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc)
    ticks = iter(range(10_000))

    def _tick() -> str:
        return (start + timedelta(seconds=next(ticks))).isoformat(timespec="seconds")

    for module in (work_ledger_store, work_ledger_writes):
        _floor_monkeypatch.setattr(module, "_now_iso", _tick)


def _state(*, running: bool):
    slots = {WORKER_A: SimpleNamespace(running=running)}
    return SimpleNamespace(get_slot=slots.get, owner_id=OWNER)


def _rows(item_id: str) -> list[dict]:
    item = wl.read_work_item(CONDUCTOR_A, item_id)
    assert item is not None
    events = [e.to_dict() for e in wl.read_events(CONDUCTOR_A, item_id)]
    return [{**item.to_dict(), "events": events}]


async def _asks(text: str = "Pick a region.") -> None:
    status, _ = await _report(
        WORKER_A, {"status": "question", "summary": text, "reason": "needs_human"}
    )
    assert status == 200


def _newest_report(item_id: str) -> str:
    return [e.id for e in wl.read_events(CONDUCTOR_A, item_id) if e.kind == "report"][-1]


async def _dismiss(item_id: str, report_id: str | None = None):
    """Dismiss as the page does: naming the newest report it shows (default: the
    item's newest one now)."""
    request = make_mocked_request(
        "POST", "/api/crew-board/action", app={"state": _state(running=False)}
    )
    request["user"] = OWNER
    request["app"] = ""
    body = {"conductor": CONDUCTOR_A, "item_id": item_id, "action": "dismiss"}
    body["report_id"] = _newest_report(item_id) if report_id is None else report_id

    async def _json():
        return body

    request.json = _json
    response = await board.api_work_ledger_board_action(request)
    return response.status, json.loads(response.body.decode())


@pytest.mark.asyncio
async def test_a_worker_running_again_no_longer_needs_the_person():
    ids = await two_by_two()
    await _asks()
    rows = _rows(ids["item_a"])

    assert board.with_worker_facts(rows, _state(running=False))[0]["awaits_person"] is True
    # The conductor messaged the worker: its turn runs, no ledger answer was written.
    assert board.with_worker_facts(rows, _state(running=True))[0]["awaits_person"] is False


@pytest.mark.asyncio
async def test_a_dismiss_clears_the_ask_everywhere_until_a_newer_report():
    ids = await two_by_two()
    await _asks()
    item_id = ids["item_a"]

    status, body = await _dismiss(item_id)

    assert (status, body) == (200, {"ok": True, "action": "dismiss", "item_id": item_id})
    rows = _rows(item_id)
    assert board.with_worker_facts(rows, _state(running=False))[0]["awaits_person"] is False
    summary = crew_work.summarize(rows, closed=lambda k: False, looping=lambda k: False)
    assert summary["person"] == 0
    steers = [e for e in rows[0]["events"] if e["kind"] == "steer"]
    assert [e["text"] for e in steers] == [board.DISMISSED_TEXT]
    # The worker's own fields are untouched, and the other ledger is not written.
    assert (rows[0]["status"], rows[0]["reason"]) == ("question", "needs_human")
    assert not [e for e in wl.read_events(CONDUCTOR_B, ids["item_b"]) if e.kind == "steer"]

    # A second dismiss of the same ask writes nothing more.
    status, body = await _dismiss(item_id)
    assert status == 409 and body["code"] == worker_routes.STEER_NOT_WAITING

    # A newer ask brings the card back.
    await _asks("Pick a zone.")
    assert board.with_worker_facts(_rows(item_id), _state(running=False))[0]["awaits_person"]


def test_a_steer_in_the_same_second_as_the_question_answers_it():
    """Stamps are seconds-precision: a dismiss filed in the second the question was
    reported must still clear the Board's ``outstanding``, by the tail's order."""
    stamp = "2026-11-06T07:00:00+00:00"
    item = SimpleNamespace(status="question", last_report_at=stamp)
    report = {"kind": "report", "status": "question", "ts": stamp}
    steer = {"kind": "steer", "ts": stamp}
    assert board._is_outstanding(item, [report]) is True
    assert board._is_outstanding(item, [report, steer]) is False
    # A steer before a newer question in the same second does not answer it.
    assert board._is_outstanding(item, [steer, report]) is True


@pytest.mark.asyncio
async def test_a_newer_ask_filed_after_the_page_looked_is_not_dismissed():
    """The page showed ask A; the worker filed ask B before the click. The dismiss
    names A, so B is refused and still waits on the person."""
    ids = await two_by_two()
    item_id = ids["item_a"]
    await _asks("Pick a region.")
    shown = _newest_report(item_id)
    await _asks("Pick a zone.")

    status, body = await _dismiss(item_id, shown)

    assert status == 409 and body["code"] == worker_routes.STEER_NOT_WAITING
    assert board.with_worker_facts(_rows(item_id), _state(running=False))[0]["awaits_person"]
    # A dismiss that names no report is refused before anything is read.
    status, body = await _dismiss(item_id, "")
    assert status == 400 and body["code"] == "missing_report_id"


@pytest.mark.asyncio
async def test_an_item_that_does_not_wait_on_the_person_is_not_dismissed():
    ids = await two_by_two()
    status, _ = await _report(WORKER_A, {"status": "progress", "summary": "moving"})
    assert status == 200

    status, body = await _dismiss(ids["item_a"])

    assert status == 409 and body["code"] == worker_routes.STEER_NOT_WAITING
    assert not [e for e in wl.read_events(CONDUCTOR_A, ids["item_a"]) if e.kind == "steer"]


@pytest.mark.asyncio
async def test_a_non_owner_cannot_dismiss():
    ids = await two_by_two()
    await _asks()
    request = make_mocked_request(
        "POST", "/api/crew-board/action", app={"state": _state(running=False)}
    )
    request["user"] = "someone-else"
    request["app"] = ""

    response = await board.api_work_ledger_board_action(request)

    assert response.status == 403
    assert not [e for e in wl.read_events(CONDUCTOR_A, ids["item_a"]) if e.kind == "steer"]
