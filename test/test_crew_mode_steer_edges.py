"""A person's steer is one ``steer`` event on the ledger that holds the worker's item,
whichever way it was typed (edge review R1: X1, E5, E7, E11).

One person-steer transaction (``crew_workers.person_steer``) serves the side panel's
steer route and the worker's own composer; it lands on a sub-conductor's ledger when
that is where the item lives, records into a unit even when the root has no live
session, and answers an outstanding question on the board.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _new_item

from kiro_crew import work_ledger as wl

_isolated_home = work_ledger_helpers._isolated_home

SUB = "chat-3-subconductor"
GRANDCHILD = "chat-4-grandchild"


@pytest.fixture(autouse=True)
def _routing_live(_floor_monkeypatch):
    # The routing is off on main until the Crew panel turns it on; these tests pin
    # the composer steer as it runs then.
    from kiro_crew import crew_mode

    _floor_monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", True)


def _bound(board: str = CONDUCTOR, worker: str = WORKER) -> str:
    wl.ensure_conductor(board, goal="g")
    item_id = wl.apply_conductor_action(
        board, "create", title="t", acceptance={"kind": "human_approval"}
    )["item"].item_id
    wl.apply_conductor_action(board, "bind", item_id=item_id, worker_session_key=worker)
    return item_id


@pytest.fixture
def _log(monkeypatch):
    from kiro_crew.crew_log import emit as crew_log_emit

    entries: list[tuple[str, dict[str, Any]]] = []

    def fake_record(unit: str, data: dict[str, Any], **_k: Any) -> bool:
        entries.append((unit, data))
        return True

    monkeypatch.setattr(crew_log_emit, "on_work_recorded", fake_record)
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
    return entries


class _Sessions:
    def __init__(self, live: dict[str, str]):
        self.live = live

    def get_provider(self, key: str):
        unit = self.live.get(key)
        return SimpleNamespace(session_id=unit) if unit else None


# ── E5: a unit to record into, root session or not ──────────────────────────


@pytest.mark.asyncio
async def test_E5_a_root_with_no_live_session_records_into_the_workers_unit(_log):
    from kiro_crew.dashboard.handlers import work_ledger_worker as wlw

    item_id = _new_item()
    state = SimpleNamespace(sessions=_Sessions({f"dashboard:{WORKER}": "unit-w"}))
    got, why = await wlw.record_person_steer_result(
        state, CONDUCTOR, item_id, "go", unit_keys=(WORKER,)
    )
    assert (got, why) == (item_id, "")
    [(unit, entry)] = _log
    assert unit == "unit-w" and entry["by"] == CONDUCTOR and entry["actor"] == "person"


@pytest.mark.asyncio
async def test_W1_a_steer_on_a_closed_item_says_closed_not_ledger_failed(_log):
    from kiro_crew.dashboard.handlers import work_ledger_worker as wlw

    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="abandoned")
    state = SimpleNamespace(sessions=_Sessions({f"dashboard:{WORKER}": "unit-w"}))
    got, why = await wlw.record_person_steer_result(
        state, CONDUCTOR, item_id, "go", unit_keys=(WORKER,)
    )
    assert (got, why) == ("", wlw.STEER_ITEM_CLOSED) and _log == []


@pytest.mark.asyncio
async def test_E5_no_live_session_at_all_falls_back_to_the_boards_newest_unit(_log, monkeypatch):
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import work_ledger_worker as wlw

    item_id = _new_item()
    monkeypatch.setattr(session_ledger, "work_crew_log_units", lambda slot: ("u-old", "u-new"))
    got, _why = await wlw.record_person_steer_result(
        SimpleNamespace(sessions=_Sessions({})), CONDUCTOR, item_id, "go", unit_keys=(WORKER,)
    )
    assert got == item_id and _log[0][0] == "u-new"


@pytest.mark.asyncio
async def test_E5_with_no_unit_anywhere_the_reason_is_named_not_silent(_log, monkeypatch):
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import crew_workers

    _bound()
    monkeypatch.setattr(session_ledger, "work_crew_log_units", lambda slot: ())
    out = await crew_workers.person_steer(
        SimpleNamespace(sessions=_Sessions({})), CONDUCTOR, WORKER, "go", rows=[]
    )
    assert out == {"ledger_event": None, "ledger_error": "crew_log_unit_unknown"}
    assert _log == []


# ── E7: the ledger that holds the item, a sub-conductor's included ───────────


@pytest.mark.asyncio
async def test_E7_a_grandchild_is_steered_on_its_sub_conductors_ledger(_log):
    from kiro_crew.dashboard.handlers import crew_workers

    item_id = _bound(SUB, GRANDCHILD)
    rows = [
        {"key": SUB, "crew_root": CONDUCTOR},
        {"key": GRANDCHILD, "crew_root": CONDUCTOR},
    ]
    state = SimpleNamespace(sessions=_Sessions({f"dashboard:{GRANDCHILD}": "unit-g"}))
    out = await crew_workers.person_steer(state, CONDUCTOR, GRANDCHILD, "rebase", rows=rows)
    assert out["ledger_event"] == item_id
    assert [e.kind for e in wl.read_events(SUB, item_id)][-1] == "steer"
    assert _log[0][1]["by"] == SUB


@pytest.mark.asyncio
async def test_E7_a_binding_outside_the_roots_tree_is_not_followed(_log):
    from kiro_crew.dashboard.handlers import crew_workers

    item_id = _bound("chat-9-elsewhere", GRANDCHILD)
    state = SimpleNamespace(sessions=_Sessions({f"dashboard:{GRANDCHILD}": "unit-g"}))
    out = await crew_workers.person_steer(state, CONDUCTOR, GRANDCHILD, "x", rows=[])
    assert out == {"ledger_event": None, "ledger_error": "no_item"}
    assert "steer" not in [e.kind for e in wl.read_events("chat-9-elsewhere", item_id)]


# ── E11: a steer answers an outstanding question ─────────────────────────────


def test_E11_a_steer_newer_than_the_question_answers_it():
    from kiro_crew.dashboard.handlers.work_ledger_board import _is_outstanding

    item = SimpleNamespace(status="question", last_report_at="")
    asked = {"kind": "report", "status": "question", "ts": "2026-10-23T10:00:00Z"}
    steer = {"kind": "steer", "ts": "2026-10-23T10:05:00Z"}
    old_steer = {"kind": "steer", "ts": "2026-10-23T09:00:00Z"}
    assert _is_outstanding(item, [asked]) is True  # type: ignore[arg-type]
    assert _is_outstanding(item, [asked, steer]) is False  # type: ignore[arg-type]
    assert _is_outstanding(item, [old_steer, asked]) is True  # type: ignore[arg-type]


# ── X1: the worker's own composer runs the same transaction ──────────────────


class _Req(dict):
    def __init__(self, state: Any) -> None:
        super().__init__()
        self.app = {"state": state}


@pytest.mark.asyncio
async def test_X1_a_composer_send_to_a_worker_records_the_steer_and_says_so(monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    calls: list[tuple[str, str, str]] = []

    async def fake_send(request):
        request[chat_send._CREW_PERSON_STEER] = (CONDUCTOR, WORKER, "use main")
        return web.json_response({"ok": True, "slot": WORKER})

    async def fake_steer(state, root, worker, message, **_k):
        calls.append((root, worker, message))
        return {"ledger_event": "it_1", "ledger_error": None}

    # The send path runs on the chat_handlers facade (``_facade_compose``).
    monkeypatch.setattr(chat_handlers, "_api_chat_send", fake_send)
    monkeypatch.setattr(crew_workers, "person_steer", fake_steer)
    resp = await chat_handlers.api_chat(_Req(SimpleNamespace()))  # type: ignore[arg-type]
    assert calls == [(CONDUCTOR, WORKER, "use main")]
    assert json.loads(resp.text) == {
        "ok": True,
        "slot": WORKER,
        "ledger_event": "it_1",
        "ledger_error": None,
    }


@pytest.mark.asyncio
async def test_X1_a_refused_send_records_nothing(monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    async def fake_send(request):
        request[chat_send._CREW_PERSON_STEER] = (CONDUCTOR, WORKER, "x")
        return web.json_response({"error": "busy"}, status=409)

    async def boom(*_a, **_k):
        raise AssertionError("a refused send must not be recorded")

    # The send path runs on the chat_handlers facade (``_facade_compose``).
    monkeypatch.setattr(chat_handlers, "_api_chat_send", fake_send)
    monkeypatch.setattr(crew_workers, "person_steer", boom)
    resp = await chat_handlers.api_chat(_Req(SimpleNamespace()))  # type: ignore[arg-type]
    assert resp.status == 409


def test_X1_only_the_owners_own_send_to_a_worker_is_a_steer(monkeypatch):
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda r: True)
    root = SimpleNamespace(
        key="r",
        agent="kirocrew-conductor",
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _saved_created_by="",
        _app="",
        instance_id="",
    )
    worker = SimpleNamespace(
        **{**vars(root), "key": "w", "agent": "kirocrew-worker", "_created_by": "r"}
    )
    state = SimpleNamespace(_slots={"r": root, "w": worker})
    assert chat_send._person_crew_worker_root(object(), state, worker, "") == "r"  # type: ignore[arg-type]
    assert chat_send._person_crew_worker_root(object(), state, root, "") == ""  # type: ignore[arg-type]
    assert chat_send._person_crew_worker_root(object(), state, worker, "an-app") == ""  # type: ignore[arg-type]
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda r: False)
    assert chat_send._person_crew_worker_root(object(), state, worker, "") == ""  # type: ignore[arg-type]


def test_X1_the_composer_send_refuses_an_over_long_steer_before_queueing_it():
    from kiro_crew.dashboard.chat_api import chat_send

    source = open(chat_send.__file__, encoding="utf-8").read()
    body = source.split("async def _api_chat_send", 1)[1]
    guard = body.index("_person_crew_worker_root(request, state, slot, request_app)")
    assert body.index('"code": "message_too_long"', guard) < body.index("responsive_chat.eligible")


# ── R2 F5: a queued steer is recorded when it is delivered ──────────────────


@pytest.mark.asyncio
async def test_F5_a_queued_send_is_not_recorded_until_the_drain_delivers_it(monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    calls: list[tuple[str, str, str]] = []
    entry: dict[str, Any] = {"id": "q1", "content": "use main"}
    state = SimpleNamespace(_slots={WORKER: SimpleNamespace(_queue=[entry])})

    async def fake_send(request):
        request[chat_send._CREW_PERSON_STEER] = (CONDUCTOR, WORKER, "use main")
        return web.json_response({"ok": True, "queued": True, "queue_id": "q1"})

    async def fake_steer(state, root, worker, message, **_k):
        calls.append((root, worker, message))
        return {"ledger_event": "it_1", "ledger_error": None}

    monkeypatch.setattr(chat_handlers, "_api_chat_send", fake_send)
    monkeypatch.setattr(crew_workers, "person_steer", fake_steer)
    resp = await chat_handlers.api_chat(_Req(state))  # type: ignore[arg-type]
    receipt = json.loads(resp.text)
    # Enqueued only: nothing on the ledger yet, and the receipt says it is owed.
    assert calls == []
    assert receipt["ledger_pending"] is True and receipt["ledger_error"] is None
    assert entry[chat_send.CREW_STEER_QUEUE_KEY] == (CONDUCTOR, WORKER)


@pytest.mark.asyncio
async def test_F5_the_drain_records_the_edited_text_once_and_a_cancel_records_nothing(
    monkeypatch,
):
    import asyncio

    from kiro_crew.dashboard import chat_handlers  # noqa: F401  (wires the drain module)
    from kiro_crew.dashboard.chat_api.chat_send import CREW_STEER_QUEUE_KEY
    from kiro_crew.dashboard.chat_turn import queue_drain
    from kiro_crew.dashboard.handlers import crew_workers

    calls: list[tuple[str, str, str]] = []

    async def fake_steer(state, root, worker, message, **_k):
        calls.append((root, worker, message))
        return {"ledger_event": "it_1", "ledger_error": None}

    monkeypatch.setattr(crew_workers, "person_steer", fake_steer)
    state = SimpleNamespace(_background_tasks=set())
    # Edited while queued: the worker gets the edited text, and so does the ledger.
    edited = {"id": "q1", "content": "use main, not dev", CREW_STEER_QUEUE_KEY: (CONDUCTOR, WORKER)}
    plain = {"id": "q2", "content": "an ordinary queued send"}
    queue_drain._record_queued_crew_steers(state, [edited, plain])  # type: ignore[arg-type]
    await asyncio.gather(*list(state._background_tasks))
    assert calls == [(CONDUCTOR, WORKER, "use main, not dev")]
    # The marker is consumed, so a re-drain of the same entry records nothing more.
    queue_drain._record_queued_crew_steers(state, [edited])  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert len(calls) == 1
    # A cancelled entry never reaches the drain, so it is never recorded.


@pytest.mark.asyncio
async def test_K8_a_requeued_steer_is_left_to_its_hold_on_both_routes(monkeypatch):
    """R5 K8: a steer the turn's teardown moved into the queue is recorded by the
    drain from the hold ``crew_steer_requeued`` moved onto its entry. Neither route
    records it, and neither looks an entry up by its text: the drain may already
    have delivered it, and an older entry with the same text is not this steer."""
    from aiohttp import web

    from kiro_crew.dashboard import chat_delivery, chat_handlers
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    async def boom(*_a, **_k):
        raise AssertionError("a requeued steer must not be recorded by the route")

    async def requeued(*_a, **_k):
        return chat_delivery.STEER_REQUEUED

    monkeypatch.setattr(crew_workers, "person_steer", boom)
    monkeypatch.setattr(chat_delivery, "steer_into_running_turn", requeued)
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control.containment_meta", lambda *_a, **_k: {}
    )

    # The worker's own composer: the receipt says queued, with no queue_id. The
    # requeued entry is already gone (drained), and an older one has the same text.
    older: dict[str, Any] = {"id": "q6", "content": "use main"}
    state = SimpleNamespace(_slots={WORKER: SimpleNamespace(_queue=[older])})

    async def fake_send(request):
        request[chat_send._CREW_PERSON_STEER] = (CONDUCTOR, WORKER, "use main")
        return web.json_response({"ok": True, "queued": True})

    monkeypatch.setattr(chat_handlers, "_api_chat_send", fake_send)
    receipt = json.loads((await chat_handlers.api_chat(_Req(state))).text)  # type: ignore[arg-type]
    assert receipt["ledger_pending"] is True and receipt["ledger_event"] is None
    assert chat_send.CREW_STEER_QUEUE_KEY not in older

    # The side panel's route: the delivery says requeued and names no entry. The
    # RPC can return before the teardown requeued it, so the hold stays for it.
    held: dict[str, dict[str, tuple[str, str]]] = {}
    monkeypatch.setattr(chat_send, "_HELD_CREW_STEERS", held)
    monkeypatch.setattr(chat_handlers, "_HELD_CREW_STEERS", held)
    panel = {"id": "q8", "content": "use dev"}
    slot = SimpleNamespace(key=WORKER, turn_running=True, _queue=[panel])
    got = await crew_workers._deliver_as_person(
        SimpleNamespace(), slot, "use dev", root=CONDUCTOR, worker=WORKER
    )
    assert got["requeued"] is True and got["queue_id"] == "" and got["queued"] is True
    assert chat_send.CREW_STEER_QUEUE_KEY not in panel
    assert held == {WORKER: {"use dev": (CONDUCTOR, WORKER)}}
    # The teardown's requeue then moves the hold onto the entry it made.
    later: dict[str, Any] = {"id": "q9", "content": "use dev"}
    chat_handlers.crew_steer_requeued(slot, "use dev", later)
    assert later[chat_send.CREW_STEER_QUEUE_KEY] == (CONDUCTOR, WORKER)
    assert held == {}


# ── R2 Y2 / X5: no worker send while its conductor is closed ────────────────


@pytest.mark.asyncio
async def test_Y2_the_main_pane_send_to_a_worker_of_a_closed_root_is_refused(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew import crew_mode
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.handlers import crew_workers, source_providers
    from kiro_crew.dashboard.state import SlotOrigin

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(crew_mode, "_known_roots", {"root"})
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda r: True)

    async def boom(*_a, **_k):
        raise AssertionError("a refused send must not be recorded")

    monkeypatch.setattr(crew_workers, "person_steer", boom)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    worker = state.get_or_create_slot("w", origin=SlotOrigin.USER)
    worker.agent = "kirocrew-worker"
    worker._created_by = "root"
    # The root's tab is closed: no "root" slot, but it is still a known root.
    assert "root" not in state._slots

    @web.middleware
    async def owner(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json={"slot": "w", "message": "go on"})
        assert resp.status == 409, await resp.text()
        body = await resp.json()
        assert body["code"] == "crew_root_closed" and body["root"] == "root"
    # Nothing queued, no turn started, no user row written.
    assert list(worker._queue) == []
    assert not worker.running
    assert not any(m.get("role") == "user" for m in worker.messages)


# ── R3 G2: the side panel's steer route records at delivery too ─────────────


class _SteerReq(dict):
    def __init__(self, state: Any, root: str, worker: str, message: str) -> None:
        super().__init__()
        self.app = {"state": state}
        self.match_info = {"slot": root, "worker": worker}
        self._body = {"message": message}

    async def json(self) -> dict[str, Any]:
        return self._body


def _steer_route(monkeypatch, delivered: dict[str, Any], queue: list[dict[str, Any]]):
    from kiro_crew import crew_mode
    from kiro_crew.dashboard.handlers import crew_workers

    calls: list[tuple[str, str, str]] = []
    worker = SimpleNamespace(key=WORKER, _app="", _queue=queue)
    state = SimpleNamespace(get_slot=lambda key: worker if key == WORKER else None)

    async def no_gate(*_a, **_k):
        return None

    async def fake_deliver(_state, _slot, _message, **_k):
        return dict(delivered)

    async def fake_steer(state, root, worker, message, **_k):
        calls.append((root, worker, message))
        return {"ledger_event": "it_1", "ledger_error": None}

    monkeypatch.setattr(crew_workers, "_gate", no_gate)
    monkeypatch.setattr(crew_workers, "_stamped_rows", lambda _s: [])
    monkeypatch.setattr(crew_workers, "_root_row", lambda _r, _k: {"key": CONDUCTOR})
    monkeypatch.setattr(crew_mode, "workers_of", lambda _r, _k: [{"key": WORKER}])
    monkeypatch.setattr(crew_workers, "_deliver_as_person", fake_deliver)
    monkeypatch.setattr(crew_workers, "person_steer", fake_steer)
    return state, calls


@pytest.mark.asyncio
async def test_the_panel_steer_route_loads_the_workers_window_off_the_loop_first(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    state, _calls = _steer_route(
        monkeypatch,
        {"ok": True, "steered": False, "queued": False, "started": True, "queue_id": ""},
        [],
    )
    order: list[str] = []

    async def loaded(slot):
        order.append("load")

    real_deliver = crew_workers._deliver_as_person

    async def deliver(*a, **k):
        order.append("deliver")
        return await real_deliver(*a, **k)

    monkeypatch.setattr(crew_workers, "_load_window", loaded)
    monkeypatch.setattr(crew_workers, "_deliver_as_person", deliver)
    await crew_workers.api_crew_worker_steer(
        _SteerReq(state, CONDUCTOR, WORKER, "use main")  # type: ignore[arg-type]
    )
    # A restored worker's pending window is read off the loop before delivery can
    # start a turn that reads it.
    assert order == ["load", "deliver"]


@pytest.mark.asyncio
async def test_a_worker_closed_while_its_window_loads_is_not_steered(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    state, calls = _steer_route(monkeypatch, {"ok": True, "steered": True}, [])
    live = state.get_slot

    async def close_while_loading(slot):
        state.get_slot = lambda key: None

    async def boom(*_a, **_k):
        raise AssertionError("a closed worker must not be steered")

    monkeypatch.setattr(crew_workers, "_load_window", close_while_loading)
    monkeypatch.setattr(crew_workers, "_deliver_as_person", boom)
    assert live(WORKER) is not None
    resp = await crew_workers.api_crew_worker_steer(
        _SteerReq(state, CONDUCTOR, WORKER, "use main")  # type: ignore[arg-type]
    )
    assert resp.status == 409 and json.loads(resp.text)["code"] == "worker_closed"
    assert calls == []


@pytest.mark.asyncio
async def test_G2_the_panel_steer_route_records_a_queued_steer_only_at_delivery(monkeypatch):
    from kiro_crew.dashboard.chat_api.chat_send import CREW_STEER_QUEUE_KEY
    from kiro_crew.dashboard.handlers import crew_workers

    entry: dict[str, Any] = {"id": "q9", "content": "use main"}
    state, calls = _steer_route(
        monkeypatch,
        {"ok": True, "steered": False, "queued": True, "started": False, "queue_id": "q9"},
        [entry],
    )
    resp = await crew_workers.api_crew_worker_steer(
        _SteerReq(state, CONDUCTOR, WORKER, "use main")  # type: ignore[arg-type]
    )
    body = json.loads(resp.text)
    # Queued: no event yet, the receipt says it is owed, the drain will record it.
    assert calls == []
    assert body["ledger_pending"] is True and "queue_id" not in body
    assert entry[CREW_STEER_QUEUE_KEY] == (CONDUCTOR, WORKER)
    # Nothing durable: the marker is never written to the queue file.
    assert "meta" not in entry


@pytest.mark.asyncio
async def test_G2_the_panel_steer_route_never_records_a_requeued_steer_itself(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    state, calls = _steer_route(
        monkeypatch,
        {
            "ok": True,
            "steered": False,
            "queued": True,
            "started": False,
            "queue_id": "",
            "requeued": True,
        },
        [],
    )
    resp = await crew_workers.api_crew_worker_steer(
        _SteerReq(state, CONDUCTOR, WORKER, "use main")  # type: ignore[arg-type]
    )
    body = json.loads(resp.text)
    # The drain records it from the entry's hold; recording here would write it twice.
    assert calls == []
    assert body["ledger_pending"] is True and "requeued" not in body


@pytest.mark.asyncio
async def test_G2_the_panel_steer_route_records_a_steer_into_a_running_turn_when_read(
    monkeypatch,
):
    from kiro_crew.dashboard.handlers import crew_workers

    state, calls = _steer_route(
        monkeypatch,
        {"ok": True, "steered": True, "queued": False, "started": False},
        [],
    )
    resp = await crew_workers.api_crew_worker_steer(
        _SteerReq(state, CONDUCTOR, WORKER, "now")  # type: ignore[arg-type]
    )
    body = json.loads(resp.text)
    # Written into the running turn, not read yet: nothing is recorded until the
    # turn reads it, so a take-back leaves no event behind.
    assert calls == []
    assert body["ledger_pending"] is True and body["ledger_event"] is None


@pytest.mark.asyncio
async def test_G2_the_panel_steer_route_records_an_idle_start_at_once(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    state, calls = _steer_route(
        monkeypatch,
        {"ok": True, "steered": False, "queued": False, "started": True},
        [],
    )
    resp = await crew_workers.api_crew_worker_steer(
        _SteerReq(state, CONDUCTOR, WORKER, "now")  # type: ignore[arg-type]
    )
    body = json.loads(resp.text)
    assert calls == [(CONDUCTOR, WORKER, "now")]
    assert body["ledger_event"] == "it_1" and "ledger_pending" not in body


# ── R3 G6: a queued steer restored after a restart is delivered, never recorded ──


@pytest.mark.asyncio
async def test_G6_a_restored_entry_is_never_recorded_as_the_persons_steer(monkeypatch):
    import asyncio

    from kiro_crew.dashboard import chat_handlers  # noqa: F401  (wires the drain module)
    from kiro_crew.dashboard.chat_api.chat_send import mark_crew_steer
    from kiro_crew.dashboard.chat_turn import queue_drain
    from kiro_crew.dashboard.handlers import crew_workers
    from kiro_crew.dashboard.slot_queue_repository import (
        durable_queue_entries,
        sanitize_restored_queue,
    )

    calls: list[tuple[str, str, str]] = []

    async def fake_steer(state, root, worker, message, **_k):
        calls.append((root, worker, message))
        return {"ledger_event": "it_1", "ledger_error": None}

    monkeypatch.setattr(crew_workers, "person_steer", fake_steer)
    entry: dict[str, Any] = {"id": "q1", "content": "use main", "kind": ""}
    mark_crew_steer(entry, CONDUCTOR, WORKER)
    [written] = durable_queue_entries([entry])
    # No steer marker reaches the file.
    assert "crew" not in json.dumps(written).lower()
    # A queue file a sandboxed process edited is still not the person's word.
    forged = dict(written, meta={"crewSteer": True})
    [restored] = sanitize_restored_queue(json.loads(json.dumps([forged])))
    state = SimpleNamespace(_background_tasks=set())
    queue_drain._record_queued_crew_steers(state, [restored])  # type: ignore[arg-type]
    await asyncio.gather(*list(state._background_tasks))
    assert calls == []
