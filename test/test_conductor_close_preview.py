"""The close preview names exactly the sessions the conductor's close then closes.

``GET /api/chat/slots/{slot}/close-preview`` is what the dashboard's close prompt
counts. It must answer the set ``DELETE`` on the same slot closes with a conductor,
including where a session's tree stamp and its manager disagree: a tab adopted under
the conductor keeps its own manager and stays open, and a session handed to the
conductor closes with it. Each case closes for real through the DELETE handler and
compares that answer with the preview read just before.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import crew_mode
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_managers, token_auth
from kiro_crew.dashboard.routes import register_all
from kiro_crew.dashboard.session_control_parts import caller_access, crew_filing

_NOT_FOUND = {"error": "not found", "code": "slot_not_found"}


class _Req:
    """The few parts of the aiohttp request the two handlers read."""

    def __init__(self, state, slot: str, app: str = "") -> None:
        self.app = {"state": state}
        self.match_info = {"slot": slot}
        self._app = app

    def get(self, key: str, default: str = "") -> str:
        return self._app if key == "app" else default


@pytest.fixture(autouse=True)
def _quiet_close(_floor_monkeypatch):
    session_managers.reset_cache()
    _floor_monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock(return_value=True))
    _chain(_floor_monkeypatch, False)
    yield
    session_managers.reset_cache()


def _chain(monkeypatch, on: bool) -> None:
    """The manager chain switch, patched through the facade, which writes it into
    every owner that binds it: the close's hand-back and the preview read the same."""
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: on)


def _crew(tmp_path, monkeypatch, *extra: str, edges: dict | None = None):
    """A Bot thread conductor, two workers it opened, a worker one of them opened, a
    bystander, and *extra* sessions created by the bystander. *edges* adds tree edges
    (an adoption or a handoff moves the edge, never ``_created_by``)."""
    state = _make_state(tmp_path)
    state.sessions.remove = AsyncMock()
    names = ("cond", "w1", "w2", "w1-sub", "other", *extra)
    slots = {n: state.get_or_create_slot(n) for n in names}
    slots["cond"].mode = crew_mode.MATE_SLOT_MODE
    slots["w1"]._created_by = "cond"
    slots["w2"]._created_by = "cond"
    slots["w1-sub"]._created_by = "w1"
    for name in extra:
        slots[name]._created_by = "other"
    if edges:
        parents = {k: ({"slot": v, "key": v} if v else None) for k, v in edges.items()}
        monkeypatch.setattr(
            crew_filing, "_frame_parents", lambda s: {k: parents.get(k) for k in s._slots}
        )
    return state, slots


def _hand(slots, worker: str, manager: str) -> None:
    session_managers.write({worker: (manager, str(slots[worker]._tab_id or ""))})


async def _preview(state, name: str, app: str = ""):
    resp = await handlers.api_chat_slot_close_preview(_Req(state, name, app))
    return resp.status, json.loads(resp.text)


async def _preview_then_close(state, name: str = "cond") -> tuple[set[str], set[str]]:
    """The keys the preview names, then the keys DELETE closes with *name*."""
    _floor = handlers._close_preview(state, name, caller_access.manager_chain_enabled())
    status, body = await _preview(state, name)
    assert status == 200 and body == {"workers": len(_floor)}
    resp = await handlers.api_chat_slot_delete(_Req(state, name))
    closed = json.loads(resp.text).get("closed_workers", [])
    return set(_floor), set(closed)


@pytest.mark.asyncio
async def test_the_preview_names_the_whole_line_a_close_takes(tmp_path, _floor_monkeypatch):
    state, _ = _crew(tmp_path, _floor_monkeypatch)
    previewed, closed = await _preview_then_close(state)
    assert previewed == closed == {"w1", "w2", "w1-sub"}


@pytest.mark.asyncio
async def test_an_adopted_tab_is_stamped_but_neither_previewed_nor_closed(
    tmp_path, _floor_monkeypatch
):
    # Adopted under the conductor: the tree edge moved, the manager did not.
    state, slots = _crew(tmp_path, _floor_monkeypatch, "adopted", edges={"adopted": "cond"})
    line = handlers._conductor_line(state, "cond")
    assert line is not None and "adopted" in line[0]
    assert session_managers.holder(slots["adopted"]) == "other"

    previewed, closed = await _preview_then_close(state)

    assert previewed == closed == {"w1", "w2", "w1-sub"}
    assert "adopted" in state._slots


@pytest.mark.asyncio
async def test_a_session_handed_to_the_conductor_is_previewed_and_closed(
    tmp_path, _floor_monkeypatch
):
    state, slots = _crew(tmp_path, _floor_monkeypatch, "handed", edges={"handed": "cond"})
    _hand(slots, "handed", "cond")

    previewed, closed = await _preview_then_close(state)

    assert previewed == closed == {"w1", "w2", "w1-sub", "handed"}


@pytest.mark.asyncio
async def test_with_the_chain_on_a_hand_back_keeps_the_handed_session_out_of_both(
    tmp_path, _floor_monkeypatch
):
    # The conductor has no manager above it, so its hand-back drops the record and
    # the handed session falls back to its creator; its own workers stay its own.
    _chain(_floor_monkeypatch, True)
    state, slots = _crew(tmp_path, _floor_monkeypatch, "handed", edges={"handed": "cond"})
    _hand(slots, "handed", "cond")

    previewed, closed = await _preview_then_close(state)

    assert previewed == closed == {"w1", "w2", "w1-sub"}
    assert "handed" in state._slots


@pytest.mark.asyncio
async def test_a_plain_chat_and_a_worker_preview_zero(tmp_path, _floor_monkeypatch):
    state, _ = _crew(tmp_path, _floor_monkeypatch)
    assert await _preview(state, "other") == (200, {"workers": 0})
    assert await _preview(state, "w1") == (200, {"workers": 0})


@pytest.mark.asyncio
async def test_the_preview_closes_nothing_and_writes_no_manager_record(
    tmp_path, _floor_monkeypatch
):
    _chain(_floor_monkeypatch, True)
    state, slots = _crew(tmp_path, _floor_monkeypatch, "handed", edges={"handed": "cond"})
    _hand(slots, "handed", "cond")
    before = dict(session_managers.load())

    assert await _preview(state, "cond") == (200, {"workers": 3})

    assert set(state._slots) == set(slots)
    assert session_managers.load() == before


@pytest.mark.asyncio
async def test_a_slot_that_is_not_live_is_the_uniform_not_found(tmp_path, _floor_monkeypatch):
    state, _ = _crew(tmp_path, _floor_monkeypatch)
    assert await _preview(state, "gone") == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_an_app_is_refused_a_slot_it_does_not_own_and_answered_zero_on_its_own(
    tmp_path, _floor_monkeypatch
):
    state, slots = _crew(tmp_path, _floor_monkeypatch)
    # Another app's slot, and an unscoped one, read as not existing.
    slots["w1"]._app = "other-app"
    assert await _preview(state, "w1", app="some-app") == (404, _NOT_FOUND)
    assert await _preview(state, "cond", app="some-app") == (404, _NOT_FOUND)
    # Its own slot: an app's close never takes workers, so its preview counts none.
    slots["cond"]._app = "some-app"
    assert await _preview(state, "cond", app="some-app") == (200, {"workers": 0})


@contextlib.asynccontextmanager
async def _serve(state):
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_an_unauthenticated_request_is_refused_before_the_handler(
    tmp_path, _floor_monkeypatch
):
    state, _ = _crew(tmp_path, _floor_monkeypatch)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    called = MagicMock()
    _floor_monkeypatch.setattr(handlers, "_close_preview", called)
    async with _serve(state) as client:
        resp = await client.get("/api/chat/slots/cond/close-preview")
        text = await resp.text()
    assert resp.status in (401, 403), text
    assert "workers" not in text
    called.assert_not_called()
