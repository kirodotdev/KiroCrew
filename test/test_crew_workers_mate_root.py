"""The Crew panel's routes answer a crewmate (mate) thread as a root.

``GET .../crew-workers``, ``GET .../crew-timeline`` and ``POST .../crew-workers/{w}/steer``
and ``/approve`` read a mate's sessions by its own ``mate_root`` stamp
(``docs/request-for-change/rfc-crewmate-workers-under-the-crewmate.md``, migration
step 3). The owner-only gate is the same for both kinds of root, so a mate root
widens nobody's reach. Every case names the rule it guards.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew import crew_mode
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.members_parts.member_space import DM_SLOT_MODE

MATE = "dm-ada"


@pytest.fixture(autouse=True)
def _fresh_roots(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "_known_mate_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    _floor_monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: None)
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)


def _row(key: str, **over: Any) -> dict[str, Any]:
    return {
        "key": key,
        "agent": "kirocrew",
        "agent_kind": "template",
        "parent": None,
        **over,
    }


_MATE_TREE = [
    _row(MATE, agent="ada", agent_kind="member", mode=DM_SLOT_MODE),
    _row("m1", created_by=MATE, title="Draft the notes", running=True),
    _row("m2", created_by="m1", title="Check the links", pending_approval=True),
    _row("croot", agent="kirocrew-conductor"),
    _row("cw", created_by="croot", title="Crew worker"),
    _row("plain", title="Not a worker"),
]


def _state(rows: list[dict[str, Any]], live: dict[str, Any] | None = None) -> Any:
    def serialize_slots(**_k: Any) -> list[dict[str, Any]]:
        out = [dict(r) for r in rows]
        crew_mode.stamp_rows(out)
        return out

    live = live or {}
    return SimpleNamespace(
        serialize_slots=serialize_slots,
        get_slot=lambda key: live.get(key),
        push_slots_update=lambda: None,
        _slots={r["key"]: object() for r in rows},
    )


@pytest.fixture
def _open_gate(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _allow)
    monkeypatch.setattr(crew_workers, "_closed_workers", lambda root, live: [])
    monkeypatch.setattr(
        crew_workers,
        "_ledger_items",
        lambda root: (
            {"m1": crew_workers.LedgerFacts("it_1", "Notes half done")} if root == MATE else {}
        ),
    )
    return crew_workers


def _req(method: str, state: Any, match: dict[str, str], body: Any = None) -> Any:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, "/x", app=app, match_info=match)
    if body is not None:

        async def _json() -> Any:
            return body

        req.json = _json  # type: ignore[method-assign]
    return req


# ── the worker list ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_mate_threads_list_is_its_mate_root_tree(_open_gate):
    resp = await _open_gate.api_crew_workers(_req("GET", _state(_MATE_TREE), {"slot": MATE}))
    assert resp.status == 200, resp.text
    body = json.loads(resp.text)
    assert body["root_kind"] == "mate"
    by_key = {w["key"]: w for w in body["workers"]}
    # Child and grandchild, read by mate_root; no Crew Mode worker, no plain chat.
    assert set(by_key) == {"m1", "m2"}
    assert by_key["m1"]["group"] == _open_gate.GROUP_WORKING
    assert by_key["m2"]["group"] == _open_gate.GROUP_APPROVAL
    assert by_key["m1"]["summary"] == "Notes half done"
    assert by_key["m1"]["dispatched_by"] == MATE
    assert by_key["m2"]["dispatched_by"] == "m1"


@pytest.mark.asyncio
async def test_a_crew_mode_roots_answer_is_unchanged(_open_gate):
    resp = await _open_gate.api_crew_workers(_req("GET", _state(_MATE_TREE), {"slot": "croot"}))
    body = json.loads(resp.text)
    assert "root_kind" not in body
    assert [w["key"] for w in body["workers"]] == ["cw"]


@pytest.mark.asyncio
async def test_a_mate_worker_is_no_root_and_a_plain_chat_is_refused(_open_gate):
    for key in ("m1", "plain"):
        resp = await _open_gate.api_crew_workers(_req("GET", _state(_MATE_TREE), {"slot": key}))
        assert resp.status == 404
        assert json.loads(resp.text)["code"] == "not_crew_mode_root"


@pytest.mark.asyncio
async def test_a_chat_in_member_mode_the_walk_did_not_record_is_refused(_open_gate):
    """The root test is the slot frame's own: a ``member``-mode row with a creator is
    no mate root, so naming it reads nothing."""
    rows = [_row("m-child", mode=DM_SLOT_MODE, created_by="plain"), _row("plain")]
    resp = await _open_gate.api_crew_workers(_req("GET", _state(rows), {"slot": "m-child"}))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_a_closed_mate_threads_list_is_read_from_saved_data(_open_gate, monkeypatch):
    monkeypatch.setattr(
        _open_gate, "_closed_titles", lambda state, keys: {k: ("Ada", "") for k in keys}
    )
    crew_mode._known_mate_roots.add(MATE)
    rows = [r for r in _MATE_TREE if r["key"] != MATE]
    resp = await _open_gate.api_crew_workers(_req("GET", _state(rows), {"slot": MATE}))
    assert resp.status == 200, resp.text
    body = json.loads(resp.text)
    assert body["root_closed"] is True and body["root_kind"] == "mate"
    assert body["root_title"] == "Ada"
    assert {w["key"] for w in body["workers"]} == {"m1", "m2"}


# ── the timeline ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_mate_threads_timeline_draws_its_sessions(_open_gate):
    resp = await _open_gate.api_crew_timeline(_req("GET", _state(_MATE_TREE), {"slot": MATE}))
    assert resp.status == 200, resp.text
    lanes = json.loads(resp.text)["lanes"]
    assert [lane["key"] for lane in lanes] == [MATE, "m1", "m2"]
    assert lanes[0]["role"] == "conductor"


# ── steer ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_steer_reaches_a_mate_worker_and_counts_no_crew_mode_steer(_open_gate, monkeypatch):
    from kiro_crew.dashboard import chat_delivery, chat_runner, session_control
    from kiro_crew.metrics import crew_mode as crew_mode_metrics

    queued: list[str] = []
    counted: list[bool] = []
    monkeypatch.setattr(
        chat_delivery,
        "queue_for_next_turn",
        lambda st, slot, msg, **k: queued.append(msg) or "q1",
    )

    async def _start(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _start)
    monkeypatch.setattr(session_control, "containment_meta", lambda st, slot: {})
    monkeypatch.setattr("kiro_crew.dashboard.relay_archive.is_relay_archive", lambda s: False)
    monkeypatch.setattr(crew_mode_metrics, "record_steer", lambda **k: counted.append(True))
    monkeypatch.setattr(_open_gate, "_worker_item", lambda root, worker, tree: ("", ""))
    worker = SimpleNamespace(key="m1", turn_running=False, running=False, _app="", _queue=[])
    resp = await _open_gate.api_crew_worker_steer(
        _req(
            "POST",
            _state(_MATE_TREE, {"m1": worker}),
            {"slot": MATE, "worker": "m1"},
            {"message": "go"},
        )
    )
    assert resp.status == 200, resp.text
    assert queued == ["go"]
    assert counted == []


@pytest.mark.asyncio
async def test_steer_refuses_a_session_outside_the_mates_tree(_open_gate):
    for worker in ("cw", "plain"):
        resp = await _open_gate.api_crew_worker_steer(
            _req(
                "POST",
                _state(_MATE_TREE),
                {"slot": MATE, "worker": worker},
                {"message": "hi"},
            )
        )
        assert resp.status == 404
        assert json.loads(resp.text)["code"] == "not_a_worker"


@pytest.mark.asyncio
async def test_a_crew_mode_root_cannot_steer_a_mates_worker(_open_gate):
    resp = await _open_gate.api_crew_worker_steer(
        _req(
            "POST",
            _state(_MATE_TREE),
            {"slot": "croot", "worker": "m1"},
            {"message": "hi"},
        )
    )
    assert resp.status == 404
    assert json.loads(resp.text)["code"] == "not_a_worker"


@pytest.mark.asyncio
async def test_every_route_keeps_the_owner_gate_for_a_mate_root(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    async def _deny(*_a: Any, **_k: Any) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _deny)
    state = _state(_MATE_TREE)
    answers = [
        await crew_workers.api_crew_workers(_req("GET", state, {"slot": MATE})),
        await crew_workers.api_crew_timeline(_req("GET", state, {"slot": MATE})),
        await crew_workers.api_crew_worker_steer(
            _req("POST", state, {"slot": MATE, "worker": "m1"}, {"message": "hi"})
        ),
        await crew_workers.api_crew_worker_approve(
            _req("POST", state, {"slot": MATE, "worker": "m2"}, {"action": "approve"})
        ),
    ]
    assert [r.status for r in answers] == [403, 403, 403, 403]


# ── approve, through the real per-slot approve route ─────────────────────────


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@web.middleware
async def _owner(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@web.middleware
async def _app_token(request: web.Request, handler):
    request["app"] = "some-app"
    request["user"] = "local-app"
    return await handler(request)


def _server(state, middleware) -> TestServer:
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
    from kiro_crew.dashboard.handlers.crew_workers import api_crew_worker_approve

    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post(
        "/api/chat/slots/{slot}/crew-workers/{worker}/approve", api_crew_worker_approve
    )
    return TestServer(app)


def _mate_crew(state, request_id: str = "rid-1"):
    mate = state.get_or_create_slot(MATE, origin=SlotOrigin.USER)
    mate.mode = DM_SLOT_MODE
    mate.agent = "ada"
    worker = state.get_or_create_slot("mw", origin=SlotOrigin.USER)
    worker.agent = "kirocrew-worker"
    worker._created_by = MATE
    other = state.get_or_create_slot("dm-bob", origin=SlotOrigin.USER)
    other.mode = DM_SLOT_MODE
    other.agent = "bob"
    fut = asyncio.get_running_loop().create_future()
    row = worker.append("permission", "Shell", json.dumps({"request_id": request_id}))
    worker.register_approval(request_id, fut, row)
    return worker, fut, worker.approval_instance(request_id)


@pytest.mark.asyncio
async def test_the_owner_approves_a_mate_workers_request_from_the_mates_card(state):
    worker, fut, mid = _mate_crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            f"/api/chat/slots/{MATE}/crew-workers/mw/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status == 200, await resp.text()
    assert fut.done() and fut.result() == "approved"
    assert not worker._trust


@pytest.mark.asyncio
async def test_an_app_token_cannot_approve_through_a_mate_root(state):
    _worker, fut, mid = _mate_crew(state)
    async with TestClient(_server(state, _app_token)) as client:
        resp = await client.post(
            f"/api/chat/slots/{MATE}/crew-workers/mw/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status in (403, 404)
    assert not fut.done()


@pytest.mark.asyncio
async def test_another_mate_cannot_approve_this_mates_worker(state):
    _worker, fut, mid = _mate_crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/dm-bob/crew-workers/mw/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status == 404
        assert (await resp.json())["code"] == "not_a_worker"
    assert not fut.done()


@pytest.mark.asyncio
async def test_a_mate_worker_is_not_a_root_to_approve_through(state):
    _worker, fut, mid = _mate_crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/mw/crew-workers/mw/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status == 404
        assert (await resp.json())["code"] == "not_crew_mode_root"
    assert not fut.done()


@pytest.mark.asyncio
async def test_a_steer_under_a_root_that_is_no_mate_is_still_counted(monkeypatch):
    """Only a mate's steer is left out of the Crew Mode metric: a steer recorded
    after its Crew Mode root stopped being one (an agent switch) still counts."""
    from kiro_crew.dashboard.handlers import crew_workers
    from kiro_crew.metrics import crew_mode as crew_mode_metrics

    counted: list[bool] = []
    monkeypatch.setattr(crew_mode_metrics, "record_steer", lambda **k: counted.append(True))
    monkeypatch.setattr(crew_workers, "_worker_item", lambda root, worker, tree: ("", ""))
    await crew_workers.person_steer(SimpleNamespace(), "gone-root", "w", "hi", rows=[])
    assert counted == [True]
    crew_mode._known_mate_roots.add(MATE)
    await crew_workers.person_steer(SimpleNamespace(), MATE, "m1", "hi", rows=[])
    assert counted == [True]
