"""``session_handoff bind=true``: the right to bind a session moves with the handoff.

Without the flag a handoff moves management only, and ``work_ledger_record
action=bind`` still admits only the session's creator. With it, the session that
holds the right names the recipient, which then binds the session on its own ledger;
the giver's open item for that session is closed as abandoned on the giver's own
board, unless it waits on a person.

Driven through the real routes (``api_session_control_handoff``, ``handoff_target``,
``api_work_ledger_record``, ``api_work_report``) over a real store, real slots and
real ``session_managers`` records in a temporary data home.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- autouse fixtures apply by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew import session_ledger
from kiro_crew import work_ledger as wl
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers import session_control as sc_routes
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.testing.wait import async_wait_until, await_until
from kiro_crew.validation import SESSION_HANDOFF_SCHEMA, ValidationError, validate_tool_args


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)
    asyncio.run(sc.refresh_manager_chain())
    session_managers.reset_cache()
    yield
    session_managers.reset_cache()


@pytest.fixture(autouse=True)
def _open_ledger_routes(_floor_monkeypatch):
    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    _floor_monkeypatch.setattr(routes, "_recognize_session", _recognized)
    _floor_monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    _floor_monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    _floor_monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    _floor_monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    _floor_monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)


def _chain_on(monkeypatch) -> None:
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)
    asyncio.run(sc.refresh_manager_chain())


def _team(state):
    """A (a fenced conductor) created B, C's creator-to-be, and the worker W."""
    owner = _slot(state, "chat-owner")
    a = _slot(state, "chat-a")
    b = _slot(state, "chat-b")
    w = _slot(state, "chat-w")
    a._created_by = owner.key
    b._created_by = a.key
    w._created_by = a.key
    return a, b, w


def _req(state, path: str, sk: str, body: Any) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _record(state, slot, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_record(
        _req(state, "/api/work-ledger/record", _key(slot), body)
    )
    return resp.status, json.loads(resp.text)


async def _report(state, slot, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_report(_req(state, "/api/work-ledger/report", _key(slot), body))
    return resp.status, json.loads(resp.text)


async def _create(state, conductor, title: str) -> str:
    status, body = await _record(state, conductor, {"action": "goal", "goal": "g", "round": 1})
    assert status == 200, body
    status, body = await _record(
        state,
        conductor,
        {"action": "create", "title": title, "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200, body
    return body["item"]["item_id"]


async def _bind(state, conductor, item_id: str, worker) -> tuple[int, dict[str, Any]]:
    return await _record(
        state, conductor, {"action": "bind", "item_id": item_id, "worker_session_key": worker.key}
    )


async def _hand(state, caller, target, to, *, bind: bool = False) -> dict[str, Any]:
    return await sc.handoff_target(
        state, caller_session_key=_key(caller), target=target.key, to=to.key, bind=bind
    )


async def _a_dispatched_w(state, a, w) -> str:
    """A opens an item and binds W, the session it created, to it."""
    a1 = await _create(state, a, "A1")
    status, body = await _bind(state, a, a1, w)
    assert status == 200, body
    return a1


async def _handed_with_bind(state, a, b, w) -> tuple[str, dict[str, Any]]:
    a1 = await _a_dispatched_w(state, a, w)
    result = await _hand(state, a, w, b, bind=True)
    return a1, result


# ── (1)-(4) a handoff with the flag ──────────────────────────────────────────


def test_the_new_manager_binds_a_session_handed_with_bind(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        await _handed_with_bind(state, a, b, w)
        b1 = await _create(state, b, "B1")
        status, body = await _bind(state, b, b1, w)
        assert status == 200, body
        assert wl.read_binding(w.key) == (b.key, b1)

    asyncio.run(run())


def test_the_giver_cannot_bind_the_session_after_a_bind_handoff(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        await _handed_with_bind(state, a, b, w)
        b1 = await _create(state, b, "B1")
        assert (await _bind(state, b, b1, w))[0] == 200
        a2 = await _create(state, a, "A2")
        status, body = await _bind(state, a, a2, w)
        assert status == 403, body
        assert body["code"] == "worker_not_owned"
        assert wl.read_binding(w.key) == (b.key, b1)

    asyncio.run(run())


def test_the_sessions_next_report_lands_on_the_new_managers_item(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        a1, _ = await _handed_with_bind(state, a, b, w)
        b1 = await _create(state, b, "B1")
        assert (await _bind(state, b, b1, w))[0] == 200
        status, body = await _report(state, w, {"status": "progress", "summary": "on B now"})
        assert status == 200, body
        assert wl.read_work_item(b.key, b1).summary == "on B now"
        assert wl.read_work_item(a.key, a1).summary != "on B now"

    asyncio.run(run())


def test_the_givers_open_item_is_closed_abandoned_with_the_handoff_decision(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        a1, result = await _handed_with_bind(state, a, b, w)
        assert result["bind"] is True
        assert result["released_item"] == a1
        assert b.key in result["release_note"]
        item = wl.read_work_item(a.key, a1)
        assert item.state == "abandoned"
        assert item.decision.startswith(f"Handed to {b.key}; stop reporting here")
        # The close reply's own advice (bind a new item, or session_close it) is
        # advice the giver cannot follow after a bind handoff, so the note carries
        # only the release.
        assert "create a new item" not in result["release_note"]

    asyncio.run(run())


# ── (5) a handoff without the flag ───────────────────────────────────────────


def test_a_plain_handoff_keeps_the_right_to_bind_with_the_creator(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        a1 = await _a_dispatched_w(state, a, w)
        result = await _hand(state, a, w, b)
        assert result["bind"] is False and result["released_item"] == ""
        b1 = await _create(state, b, "B1")
        status, body = await _bind(state, b, b1, w)
        assert status == 403 and body["code"] == "worker_not_owned", body
        assert wl.read_work_item(a.key, a1).state == "open"
        # A still holds the right: its bind passes ownership and stops only at the
        # one-open-item rule, which A1 still holds.
        a2 = await _create(state, a, "A2")
        status, body = await _bind(state, a, a2, w)
        assert status == 409 and body["code"] == "worker_already_dispatched", body

    asyncio.run(run())


# ── (6) the gate ─────────────────────────────────────────────────────────────


def test_a_caller_without_the_right_cannot_pass_it_on(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)
    c = _slot(state, "chat-c")
    c._created_by = b.key

    async def run():
        a1 = await _a_dispatched_w(state, a, w)
        await _hand(state, a, w, b)
        before = dict(session_managers.load())
        with pytest.raises(sc.SessionControlError) as refused:
            await _hand(state, b, w, c, bind=True)
        assert refused.value.code == "not_bind_holder"
        assert refused.value.status == 403
        assert dict(session_managers.load()) == before
        assert wl.read_work_item(a.key, a1).state == "open"

    asyncio.run(run())


# ── (7) the flag's type ──────────────────────────────────────────────────────


def test_a_non_boolean_bind_is_refused_on_the_route_and_by_the_validator(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        req = _req(
            state,
            "/api/session-control/handoff",
            _key(a),
            {"target": w.key, "to": b.key, "bind": "yes"},
        )
        # The unix-socket caller's peer attestation lets the route act on its key.
        req["peer_verified"] = True
        resp = await sc_routes.api_session_control_handoff(req)
        return resp.status, json.loads(resp.text)

    status, body = asyncio.run(run())
    assert status == 400 and body["code"] == "invalid_bind", body
    assert session_managers.load() == {}
    ok = validate_tool_args({"target": "t", "to": "u", "bind": True}, SESSION_HANDOFF_SCHEMA)
    assert ok["bind"] is True
    with pytest.raises(ValidationError):
        validate_tool_args({"target": "t", "to": "u", "bind": "yes"}, SESSION_HANDOFF_SCHEMA)


# ── (8) resume and tab mismatch ──────────────────────────────────────────────


def test_a_history_resume_keeps_the_moved_right(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        await _hand(state, a, w, b, bind=True)
        old_tab = w._tab_id
        w._tab_id = "tab-after-resume"
        await session_managers.rebind_resumed(w, old_tab)
        assert session_managers.bind_holder(w) == b.key

    asyncio.run(run())


def test_a_bind_record_for_another_tab_admits_nobody(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        await _hand(state, a, w, b, bind=True)
        w._tab_id = "tab-not-the-recorded-one"
        assert session_managers.bind_holder(w) == ""
        for conductor in (a, b):
            item = await _create(state, conductor, f"{conductor.key} item")
            status, body = await _bind(state, conductor, item, w)
            assert status == 403 and body["code"] == "worker_not_owned", body

    asyncio.run(run())


# ── (9) the right returns to the creator ─────────────────────────────────────


def test_handing_on_without_the_flag_returns_the_right_to_the_creator(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)
    c = _slot(state, "chat-c")
    c._created_by = b.key

    async def run():
        await _handed_with_bind(state, a, b, w)
        assert session_managers.bind_holder(w) == b.key
        await _hand(state, b, w, c)
        assert session_managers.bind_holder(w) == a.key
        a2 = await _create(state, a, "A2")
        status, body = await _bind(state, a, a2, w)
        assert status == 200, body

    asyncio.run(run())


def test_a_hand_back_on_close_returns_the_right_to_the_creator(tmp_path, _floor_monkeypatch):
    state = _make_state(tmp_path)
    a, b, w = _team(state)
    _chain_on(_floor_monkeypatch)

    async def run():
        await _handed_with_bind(state, a, b, w)
        assert session_managers.bind_holder(w) == b.key
        moved = await sc.hand_back_on_close(state, b)
        assert w.key in moved
        assert session_managers.bind_holder(w) == a.key

    asyncio.run(run())


# ── (10) an item that waits on a person ──────────────────────────────────────


def test_an_item_waiting_on_a_person_is_left_open(tmp_path):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        a1 = await _a_dispatched_w(state, a, w)
        status, body = await _report(
            state, w, {"status": "blocked", "reason": "needs_human", "summary": "ask the owner"}
        )
        assert status == 200, body
        result = await _hand(state, a, w, b, bind=True)
        assert result["released_item"] == ""
        assert "person" in result["release_note"]
        assert session_managers.bind_holder(w) == b.key
        assert wl.read_work_item(a.key, a1).state == "open"
        b1 = await _create(state, b, "B1")
        status, body = await _bind(state, b, b1, w)
        assert status == 409 and body["code"] == "worker_already_dispatched", body

    asyncio.run(run())


def test_an_answered_person_wait_is_closed_by_the_handoff(tmp_path):
    # ``decide`` leaves the report's ``reason`` standing until the worker reports
    # again, so only the Board's rule sees that nobody waits any more.
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        a1 = await _a_dispatched_w(state, a, w)
        status, body = await _report(
            state, w, {"status": "blocked", "reason": "needs_human", "summary": "ask the owner"}
        )
        assert status == 200, body
        status, body = await _record(
            state, a, {"action": "decide", "item_id": a1, "decision": "the owner said go"}
        )
        assert status == 200, body
        assert wl.read_work_item(a.key, a1).reason == "needs_human"
        result = await _hand(state, a, w, b, bind=True)
        assert result["released_item"] == a1, result
        assert wl.read_work_item(a.key, a1).state == "abandoned"
        b1 = await _create(state, b, "B1")
        status, body = await _bind(state, b, b1, w)
        assert status == 200, body

    asyncio.run(run())


def test_a_person_wait_that_lands_during_the_release_keeps_the_item_open(
    tmp_path, _floor_monkeypatch
):
    state = _make_state(tmp_path)
    a, b, w = _team(state)
    real_unit = routes._acting_unit
    real_load = sc.load_code_channel_lineage
    armed: list[bool] = []
    landed: list[tuple[int, dict[str, Any]]] = []

    def unit_after_first_read(*args: Any, **kwargs: Any) -> Any:
        # The release names its acting unit after its first read of the item.
        if args[2] == "session_handoff_release":
            armed.append(True)
        return real_unit(*args, **kwargs)

    async def report_first() -> None:
        # The release's close awaits this before it takes the board lock: the
        # worker's report commits here. The report reaches this function too,
        # so the trigger disarms before the report runs.
        if armed:
            armed.clear()
            landed.append(
                await _report(
                    state,
                    w,
                    {"status": "blocked", "reason": "needs_human", "summary": "ask the owner"},
                )
            )
        await real_load()

    async def run():
        a1 = await _a_dispatched_w(state, a, w)
        _floor_monkeypatch.setattr(routes, "_acting_unit", unit_after_first_read)
        _floor_monkeypatch.setattr(sc, "load_code_channel_lineage", report_first)
        result = await _hand(state, a, w, b, bind=True)
        assert len(landed) == 1 and landed[0][0] == 200, landed
        assert result["released_item"] == ""
        assert "waits on a person" in result["release_note"]
        item = wl.read_work_item(a.key, a1)
        assert item.state == "open"
        assert item.reason == "needs_human"

    asyncio.run(run())


# ── (11) depth ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("receiver_depth", "allowed"), [(1, False), (0, True)])
def test_a_conducting_session_moves_only_under_a_shallower_ledger(
    tmp_path, receiver_depth, allowed
):
    state = _make_state(tmp_path)
    a, b, w = _team(state)

    async def run():
        routes._ensure(w.key, 1, None)
        routes._ensure(b.key, receiver_depth, None)
        if allowed:
            result = await _hand(state, a, w, b, bind=True)
            assert result["manager"] == b.key
            assert session_managers.bind_holder(w) == b.key
            return
        with pytest.raises(sc.SessionControlError) as refused:
            await _hand(state, a, w, b, bind=True)
        assert refused.value.code == "depth_exceeded"
        assert refused.value.status == 409
        assert session_managers.load() == {}

    asyncio.run(run())


# ── (12) a bind and a rights-moving handoff do not interleave ────────────────


class _ObservedLock:
    """HANDOFF_LOCK, with an event set when an acquirer finds it already held."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.contended = asyncio.Event()

    def locked(self) -> bool:
        return self.inner.locked()

    async def __aenter__(self) -> None:
        if self.inner.locked():
            self.contended.set()
        await self.inner.__aenter__()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.inner.__aexit__(*exc_info)


def test_a_bind_in_flight_finishes_before_a_bind_handoff_decides(tmp_path, _floor_monkeypatch):
    from kiro_crew.dashboard.session_control_parts import session_handoff

    state = _make_state(tmp_path)
    a, b, w = _team(state)
    lock = _ObservedLock(session_managers.HANDOFF_LOCK)
    _floor_monkeypatch.setattr(session_managers, "HANDOFF_LOCK", lock)
    _floor_monkeypatch.setattr(session_handoff, "_HANDOFF_LOCK", lock)
    entered, release = asyncio.Event(), asyncio.Event()
    real = routes._refuse_dispatched_worker

    async def held(conductor_key, worker_session_key):
        # Inside the bind's admission, after the ownership check admitted A.
        entered.set()
        await release.wait()
        return await real(conductor_key, worker_session_key)

    async def run():
        a1 = await _create(state, a, "A1")
        _floor_monkeypatch.setattr(routes, "_refuse_dispatched_worker", held)
        bind = asyncio.create_task(_bind(state, a, a1, w))
        await await_until(entered, what="the bind's admission")
        handoff = asyncio.create_task(_hand(state, a, w, b, bind=True))
        # Either the handoff queues behind the bind, or (unserialized) it finishes.
        await async_wait_until(lambda: handoff.done() or lock.contended.is_set())
        release.set()
        status, body = await await_until(bind, what="the bind")
        assert status == 200, body
        result = await await_until(handoff, what="the handoff")
        # The serial order: A bound W, then the handoff closed A's item on A's board.
        assert session_managers.bind_holder(w) == b.key
        assert result["released_item"] == a1
        assert wl.read_work_item(a.key, a1).state == "abandoned"

    asyncio.run(run())


# ── (13) a first ledger and a granted bind do not interleave ─────────────────


def test_a_first_ledger_opened_during_a_granted_bind_takes_its_depth_from_it(
    tmp_path, _floor_monkeypatch
):
    """W, handed with its binding to A's closed item still standing, opens its first
    ledger while B's bind of it is past the depth check, which found no ledger. The
    ledger must take its depth from B's item, or W conducts a third level under B."""
    from kiro_crew.dashboard.session_control_parts import session_handoff

    state = _make_state(tmp_path)
    a, b, w = _team(state)
    lock = _ObservedLock(session_managers.HANDOFF_LOCK)
    _floor_monkeypatch.setattr(session_managers, "HANDOFF_LOCK", lock)
    _floor_monkeypatch.setattr(session_handoff, "_HANDOFF_LOCK", lock)
    entered, release = asyncio.Event(), asyncio.Event()
    real = routes._refuse_dispatched_worker

    async def held(conductor_key, worker_session_key):
        # After the depth check admitted B's bind on W holding no ledger.
        entered.set()
        await release.wait()
        return await real(conductor_key, worker_session_key)

    async def run():
        routes._ensure(b.key, 1, None)
        a1, _ = await _handed_with_bind(state, a, b, w)
        assert wl.read_binding(w.key) == (a.key, a1)
        b1 = await _create(state, b, "B1")
        _floor_monkeypatch.setattr(routes, "_refuse_dispatched_worker", held)
        bind = asyncio.create_task(_bind(state, b, b1, w))
        await await_until(entered, what="B's bind past its depth check")
        goal = asyncio.create_task(_record(state, w, {"action": "goal", "goal": "g", "round": 1}))
        # Either the goal queues behind the bind, or (unserialized) it finishes.
        await async_wait_until(lambda: goal.done() or lock.contended.is_set())
        release.set()
        status, body = await await_until(bind, what="B's bind")
        assert status == 200, body
        assert wl.read_binding(w.key) == (b.key, b1)
        # The serial order: B bound W, so W's ledger would open one level below
        # B's depth-1 ledger, past the default two-level cap.
        status, body = await await_until(goal, what="W's goal")
        assert status == 409 and body["code"] == "depth_exceeded", body
        assert wl.read_conductor(w.key) is None

    asyncio.run(run())


# ── (14) the ledger a handed session is judged by ────────────────────────────


def test_a_channel_linked_receiver_is_refused_before_any_ledger_is_read(tmp_path):
    """A ledger is keyed by the session key a slot's turns run on, which only a
    channel-linked slot spells apart from its slot key. The handoff refuses such a
    slot before it reads a ledger, so the depth check's slot keys name the right one."""
    state = _make_state(tmp_path)
    a, b, w = _team(state)
    dm_key = "discord:kirocrew-conductor:direct:42:gen5"
    dm = _slot(state, dm_key.replace(":", "_"), linked_session_key=dm_key, channel_origin=True)
    dm._created_by = a.key

    async def run():
        routes._ensure(w.key, 1, None)
        routes._ensure(dm_key, 0, None)
        with pytest.raises(sc.SessionControlError) as refused:
            await _hand(state, a, w, dm, bind=True)
        assert refused.value.code == "linked_session_target"
        assert session_managers.load() == {}

    asyncio.run(run())
    assert session_ledger.ledger_key(effective_session_key(dm)) == dm_key != dm.key
    for slot in (a, b, w):
        assert session_ledger.ledger_key(effective_session_key(slot)) == slot.key
