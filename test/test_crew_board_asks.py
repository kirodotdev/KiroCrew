"""The Crew board read carries the conductor's offered answers (goal-board's ``asks``).

The conductor mode Board's Needs your input draws them as Reply buttons, the way the
goal-board page does, so the read hands them over keyed by item: only for this
board's open items, only non-empty strings, at most six each, and ``{}`` when the
conductor's agentic fold cannot be read.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers import work_ledger_board as board

CONDUCTOR = "chat-1-conductor"
OWNER = "owner-subject"
WORKER = "chat-2-worker"


def _item(*, title: str = "port the gate") -> str:
    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    result = wl.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}
    )
    return result["item"].item_id


def _state():
    return SimpleNamespace(_slots={}, get_slot=lambda _key: None, owner_id=OWNER)


async def _call(state, conductor: str):
    request = make_mocked_request(
        "GET", f"/api/crew-board?conductor={conductor}", app={"state": state}
    )
    request["user"] = OWNER
    request["app"] = ""
    response = await board.api_work_ledger_board(request)
    return json.loads(response.body.decode()), response


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _fold(monkeypatch, asks, seen: list[tuple[str, str]] | None = None):
    def read(slot, name, **_kw):
        if seen is not None:
            seen.append((slot, name))
        return SimpleNamespace(value={"fields": {"asks": {"value": asks, "at": "t"}}})

    monkeypatch.setattr(projection, "read_slot_projection", read)


@pytest.mark.asyncio
async def test_asks_are_keyed_by_open_item_and_cleaned(monkeypatch):
    open_id = _item(title="pick a tea")
    closed_id = _item(title="old one")
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=closed_id, state="abandoned")
    seen: list[tuple[str, str]] = []
    _fold(
        monkeypatch,
        {
            open_id: {"options": ["Oolong", "", "  ", 3, "Green", *[f"o{i}" for i in range(9)]]},
            closed_id: {"options": ["stale"]},
            "it_deadbeef": {"options": ["not on this board"]},
        },
        seen,
    )
    body, response = await _call(_state(), CONDUCTOR)
    assert response.status == 200
    assert body["asks"] == {open_id: ["Oolong", "Green", "o0", "o1", "o2", "o3"]}
    # Read from the conductor's own slot, the fold its page is filled from.
    assert (CONDUCTOR, "agentic") in seen


def _ask_person(item_id: str, reason: str | None = "needs_human", *, relay: bool = True) -> None:
    """A worker asks; with *relay* the conductor relays it with ``work_ask_record``."""
    worker = f"{WORKER}-{item_id}"
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=worker)
    wl.apply_worker_report(
        CONDUCTOR, item_id, status="question", summary="which tea?", reason=reason
    )
    if relay:
        wl.apply_ask_record(CONDUCTOR, item_id, what="Pick a tea")


async def _awaits(item_id: str) -> bool:
    body, _ = await _call(_state(), CONDUCTOR)
    return next(row for row in body["items"] if row["item_id"] == item_id)["awaits_person"]


@pytest.mark.asyncio
async def test_a_question_for_the_person_awaits_them_until_steered():
    item_id = _item()
    _ask_person(item_id)
    assert await _awaits(item_id) is True
    # The person answers by steering the worker: no item field moves, but it is answered.
    wl.apply_person_steer(CONDUCTOR, item_id, "oolong")
    assert await _awaits(item_id) is False


@pytest.mark.asyncio
async def test_a_conductor_decision_answers_it_and_a_plain_question_never_awaits():
    answered = _item(title="answered")
    _ask_person(answered)
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=answered, decision="use oolong")
    plain = _item(title="plain")
    _ask_person(plain, reason=None)
    assert await _awaits(answered) is False
    assert await _awaits(plain) is False


@pytest.mark.asyncio
async def test_a_decision_just_before_a_new_question_does_not_answer_it():
    # Back-to-back writes share a seconds-precision stamp: decide, then ask again.
    item_id = _item()
    _ask_person(item_id)
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="try oolong")
    wl.apply_worker_report(
        CONDUCTOR, item_id, status="question", summary="still unsure", reason="needs_human"
    )
    # A new question is not the person's until the conductor relays it.
    assert await _awaits(item_id) is False
    wl.apply_ask_record(CONDUCTOR, item_id, what="Still unsure: pick a tea")
    assert await _awaits(item_id) is True


@pytest.mark.asyncio
async def test_no_asks_field_or_a_bad_shape_reads_empty(monkeypatch):
    item_id = _item()
    _fold(monkeypatch, {item_id: "not an object"})
    body, _ = await _call(_state(), CONDUCTOR)
    assert body["asks"] == {}
    _fold(monkeypatch, ["a list"])
    body, _ = await _call(_state(), CONDUCTOR)
    assert body["asks"] == {}


@pytest.mark.asyncio
async def test_an_unreadable_fold_still_serves_the_board(monkeypatch):
    item_id = _item()

    def boom(*_a, **_kw):
        raise OSError("log gone")

    monkeypatch.setattr(projection, "read_slot_projection", boom)
    body, response = await _call(_state(), CONDUCTOR)
    assert response.status == 200
    assert body["asks"] == {}
    assert [row["item_id"] for row in body["items"]] == [item_id]


def _row(body, item_id: str) -> dict:
    return next(row for row in body["items"] if row["item_id"] == item_id)


@pytest.mark.asyncio
async def test_a_current_ask_record_reaches_the_card_and_a_newer_report_drops_it():
    item_id = _item()
    _ask_person(item_id, relay=False)
    # Not vetted yet: the worker's question carries no ask record and is not the person's.
    body, _ = await _call(_state(), CONDUCTOR)
    assert "ask" not in _row(body, item_id)
    assert _row(body, item_id)["awaits_person"] is False
    wl.apply_ask_record(
        CONDUCTOR,
        item_id,
        what="Pick the tea for the menu",
        why="Only you know the guests",
        options=[{"label": "Oolong", "outcome": "ships today"}, {"label": "Green"}],
        pick="Oolong",
        pick_why="already stocked",
    )
    body, _ = await _call(_state(), CONDUCTOR)
    ask = _row(body, item_id)["ask"]
    assert (ask["what"], ask["why"], ask["pick"], ask["pick_why"]) == (
        "Pick the tea for the menu",
        "Only you know the guests",
        "Oolong",
        "already stocked",
    )
    assert [o["label"] for o in ask["options"]] == ["Oolong", "Green"]
    # A newer report is a new question the conductor has not vetted: the old record goes.
    wl.apply_worker_report(
        CONDUCTOR, item_id, status="question", summary="also the cups?", reason="needs_human"
    )
    body, _ = await _call(_state(), CONDUCTOR)
    row = _row(body, item_id)
    assert row["awaits_person"] is False
    assert "ask" not in row


@pytest.mark.asyncio
async def test_a_remote_item_carries_its_current_ask_record_too():
    item_id = _item()
    wl.apply_remote_bind(
        CONDUCTOR, item_id, worker_session_key="chat-peer", worker_instance_id="crew-peer"
    )
    wl.apply_worker_report(
        CONDUCTOR, item_id, status="question", summary="which tea?", reason="needs_human"
    )
    wl.apply_ask_record(CONDUCTOR, item_id, what="Pick the tea", options=[{"label": "Oolong"}])
    body, _ = await _call(_state(), CONDUCTOR)
    row = _row(body, item_id)
    assert row["ask"]["what"] == "Pick the tea"
    # An option list with no pick: no pick is sent.
    assert row["ask"]["pick"] == ""
