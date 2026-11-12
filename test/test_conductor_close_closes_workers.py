"""The person closing a conductor closes the sessions it manages with it.

Driven through the real DELETE handler and the real close path, with the
history save and the session teardown stubbed out: what is pinned here is
which sessions close, not how one close persists.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import crew_mode
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_managers
from kiro_crew.testing.wait import await_until, finished, until_parked


class _Req:
    """The few parts of the aiohttp request the delete handler reads."""

    def __init__(self, state, slot: str, app: str = "") -> None:
        self.app = {"state": state}
        self.match_info = {"slot": slot}
        self.query: dict[str, str] = {}
        self._app = app

    def get(self, key: str, default: str = "") -> str:
        return self._app if key == "app" else default


@pytest.fixture(autouse=True)
def _quiet_close(_floor_monkeypatch):
    session_managers.reset_cache()
    _floor_monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock(return_value=True))
    _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)
    yield
    session_managers.reset_cache()


def _crew(tmp_path):
    """A Bot thread conductor, two workers it opened, a worker one of them opened, and a bystander."""
    state = _make_state(tmp_path)
    state.sessions.remove = AsyncMock()
    slots = {n: state.get_or_create_slot(n) for n in ("cond", "w1", "w2", "w1-sub", "other")}
    slots["cond"].mode = crew_mode.MATE_SLOT_MODE
    slots["w1"]._created_by = "cond"
    slots["w2"]._created_by = "cond"
    slots["w1-sub"]._created_by = "w1"
    return state, slots


def _hand(slots, worker: str, manager: str) -> None:
    session_managers.write({worker: (manager, str(slots[worker]._tab_id or ""))})


async def _close(state, name: str, app: str = ""):
    resp = await handlers.api_chat_slot_delete(_Req(state, name, app))
    return resp.status, json.loads(resp.text)


@pytest.mark.asyncio
async def test_closing_a_conductor_closes_its_whole_line_of_workers(tmp_path):
    state, _ = _crew(tmp_path)

    status, body = await _close(state, "cond")

    assert status == 200
    assert sorted(body.get("closed_workers", [])) == ["w1", "w1-sub", "w2"]
    assert body.get("failed_workers") == []
    assert set(state._slots) == {"other"}


@pytest.mark.asyncio
async def test_closing_an_ultra_mode_root_closes_its_workers(tmp_path, monkeypatch):
    state, slots = _crew(tmp_path)
    slots["cond"].mode = ""
    slots["cond"].agent = "conductor-x"
    monkeypatch.setattr(crew_mode, "conductor_agents", lambda: {"conductor-x"})

    _, body = await _close(state, "cond")

    assert sorted(body.get("closed_workers", [])) == ["w1", "w1-sub", "w2"]


@pytest.mark.asyncio
async def test_closing_a_plain_chat_keeps_the_session_it_opened(tmp_path):
    state, slots = _crew(tmp_path)
    slots["cond"].mode = ""

    status, body = await _close(state, "cond")

    assert status == 200
    assert body == {"ok": True}
    assert {"w1", "w2", "w1-sub"} <= set(state._slots)


@pytest.mark.asyncio
async def test_a_session_with_no_workers_answers_as_before(tmp_path):
    state, _ = _crew(tmp_path)

    status, body = await _close(state, "other")

    assert status == 200
    assert body == {"ok": True}
    assert "w1" in state._slots


@pytest.mark.asyncio
async def test_a_worker_handed_to_another_manager_stays_open(tmp_path):
    state, slots = _crew(tmp_path)
    _hand(slots, "w2", "other")

    _, body = await _close(state, "cond")

    assert sorted(body.get("closed_workers", [])) == ["w1", "w1-sub"]
    assert "w2" in state._slots


@pytest.mark.asyncio
async def test_a_handoff_during_an_earlier_workers_close_keeps_the_next_worker_open(
    tmp_path, monkeypatch
):
    state, slots = _crew(tmp_path)
    # A released peer stays outside the tree but is still managed by the conductor.
    _hand(slots, "other", "cond")
    earlier_close = asyncio.Event()
    real_close = handlers.close_slot

    async def _pending_handoff():
        await await_until(earlier_close, what="earlier worker close")
        async with session_managers.HANDOFF_LOCK:
            await asyncio.to_thread(_hand, slots, "w2", "other")

    async def _close_slot(state_, slot, name, **kw):
        if name == "w1":
            earlier_close.set()
            # Queue the write during this close, before the next selected child
            # takes the same lock and rechecks its manager.
            await until_parked(session_managers.HANDOFF_LOCK)
        return await real_close(state_, slot, name, **kw)

    monkeypatch.setattr(handlers, "close_slot", _close_slot)
    handoff = asyncio.create_task(_pending_handoff())
    try:
        status, body = await finished(_close(state, "cond"), "conductor close")
        await await_until(handoff)
    finally:
        handoff.cancel()
        await await_until(asyncio.gather(handoff, return_exceptions=True))

    assert status == 200
    assert sorted(body.get("closed_workers", [])) == ["w1", "w1-sub"]
    assert body.get("failed_workers") == []
    assert state.get_slot("w2") is slots["w2"]
    assert state.get_slot("other") is slots["other"]
    assert session_managers.holder(slots["w2"]) == "other"
    assert all(
        call.args[1] is not slots["w2"] for call in handlers.save_slot_off_loop.await_args_list
    )


@pytest.mark.asyncio
async def test_with_the_chain_on_workers_under_a_lead_are_handed_up_not_closed(
    tmp_path, monkeypatch
):
    state, slots = _crew(tmp_path)
    state.get_or_create_slot("lead")
    slots["cond"]._created_by = "lead"
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)

    _, body = await _close(state, "cond")

    assert "closed_workers" not in body
    assert {"w1", "w2"} <= set(state._slots)


@pytest.mark.asyncio
async def test_with_the_chain_on_a_worker_given_back_to_its_creator_stays_open(
    tmp_path, monkeypatch
):
    """The conductor has no manager, so its close returns a handed-in worker to its creator."""
    state, slots = _crew(tmp_path)
    slots["w2"]._created_by = "other"
    _hand(slots, "w2", "cond")
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)

    _, body = await _close(state, "cond")

    assert sorted(body.get("closed_workers", [])) == ["w1", "w1-sub"]
    assert "w2" in state._slots


@pytest.mark.asyncio
async def test_an_app_closing_its_own_session_does_not_close_workers(tmp_path):
    state, slots = _crew(tmp_path)
    slots["cond"]._app = "acme"

    status, body = await _close(state, "cond", app="acme")

    assert status == 200
    assert "closed_workers" not in body
    assert {"w1", "w2", "w1-sub"} <= set(state._slots)


@pytest.mark.asyncio
async def test_a_worker_opened_while_the_line_is_closing_closes_too(tmp_path, monkeypatch):
    state, _ = _crew(tmp_path)
    real_close = handlers.close_slot

    async def _close_slot(state_, slot, name, **kw):
        if name == "w1":
            state_.get_or_create_slot("late")._created_by = "w1"
        return await real_close(state_, slot, name, **kw)

    monkeypatch.setattr(handlers, "close_slot", _close_slot)

    _, body = await _close(state, "cond")

    assert "late" in body.get("closed_workers", [])
    assert set(state._slots) == {"other"}


@pytest.mark.asyncio
async def test_a_worker_that_fails_to_close_is_named_and_stays_open(tmp_path, monkeypatch):
    state, _ = _crew(tmp_path)
    real_close = handlers.close_slot

    async def _close_slot(state_, slot, name, **kw):
        if name == "w2":
            raise handlers.SlotCloseError("save failed", "history_save_failed")
        return await real_close(state_, slot, name, **kw)

    monkeypatch.setattr(handlers, "close_slot", _close_slot)

    _, body = await _close(state, "cond")

    assert body.get("failed_workers") == ["w2"]
    assert "w2" in state._slots
    assert "cond" not in state._slots


@pytest.mark.asyncio
async def test_a_worker_that_left_the_tree_stays_open(tmp_path, monkeypatch):
    """``session_release`` and an adoption elsewhere move only the tree edge: the
    worker still names the conductor as its manager, and is still left open."""
    from kiro_crew.dashboard.session_control_parts import crew_filing

    state, slots = _crew(tmp_path)
    real_parents = crew_filing._frame_parents

    def _parents(state_):
        found = dict(real_parents(state_) or {})
        found["w2"] = {"slot": "other"}
        return found

    monkeypatch.setattr(crew_filing, "_frame_parents", _parents)
    assert session_managers.holder(slots["w2"]) == "cond"

    status, body = await _close(state, "cond")

    assert status == 200
    assert sorted(body.get("closed_workers", [])) == ["w1", "w1-sub"]
    assert body.get("failed_workers") == []
    assert set(state._slots) == {"w2", "other"}
