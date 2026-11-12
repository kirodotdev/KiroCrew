"""``session_handoff bind=true``: the new manager binds the session on its ledger.

A handoff moves management but, by default, leaves work-ledger bind rights with the
creator, so the new manager is refused ``worker_not_owned`` on
``work_ledger_record action=bind`` (``session-control.md``). The opt-in ``bind`` flag
moves bind rights too: the recipient binds the session on its own ledger, the giver's
open item is released with a handoff event, the giver's later bind is refused, and
``work_report`` resolves to the recipient's item. Without the flag, nothing changes.

Driven through the real ``work_ledger_record`` route over a real store in a temporary
data home, with a real ``session_managers`` record, two conductors and one worker --
the two-real-sessions path the acceptance asks for.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.handlers import work_ledger as routes

GIVER = "chat-a-conductor"  # A: created the worker, dispatched it
TAKER = "chat-b-conductor"  # B: receives the session with bind rights
WORKER = "chat-worker"
WORKER_TAB = "tab-worker-1"


class _Slot:
    """What the bind route and ``session_managers`` read off a slot: ``_created_by``,
    ``workspace``, ``key`` and ``_tab_id``. A real session_create stamps the first;
    the last two are the identity ``session_managers.holder`` matches a record against."""

    def __init__(self, key: str, *, created_by: str = "", tab_id: str = "") -> None:
        self.key = key
        self._created_by = created_by
        self._tab_id = tab_id
        self.workspace = "default"
        self.running = True


_SLOTS: dict[str, _Slot] = {}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    session_managers.reset_cache()
    _SLOTS.clear()
    yield
    _SLOTS.clear()
    session_managers.reset_cache()


@pytest.fixture(autouse=True)
def _open_route(_floor_monkeypatch):
    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    _floor_monkeypatch.setattr(routes, "_recognize_session", _recognized)
    _floor_monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    _floor_monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    _floor_monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    _floor_monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    _floor_monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)


def _req(method: str, path: str, *, body: Any = ..., sk: str) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: _SLOTS.get(key))
    app["state"] = state
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _record(sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    import json

    resp = await routes.api_work_ledger_record(
        _req("POST", "/api/work-ledger/record", body=body, sk=sk)
    )
    return resp.status, json.loads(resp.text)


async def _report(sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    import json

    resp = await routes.api_work_report(_req("POST", "/api/work-ledger/report", body=body, sk=sk))
    return resp.status, json.loads(resp.text)


async def _dispatch_on(conductor: str, title: str) -> str:
    """Open *conductor*'s ledger and bind WORKER to a fresh item. Returns the item id.

    WORKER must already be a slot *conductor* created for the bind to be owned.
    """
    status, body = await _record(conductor, {"action": "goal", "goal": f"goal {title}", "round": 1})
    assert status == 200, body
    status, body = await _record(
        conductor, {"action": "create", "title": title, "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    status, body = await _record(
        conductor, {"action": "bind", "item_id": item_id, "worker_session_key": WORKER}
    )
    assert status == 200, body
    return item_id


def _hand_off_with_bind_rights() -> None:
    """Record the manager + bind-rights the ``session_handoff bind=true`` route writes."""
    session_managers.write({WORKER: (TAKER, WORKER_TAB, True)})
    session_managers.reset_cache()


async def _taker_create(title: str) -> str:
    status, body = await _record(TAKER, {"action": "goal", "goal": f"goal {title}", "round": 1})
    assert status == 200, body
    status, body = await _record(
        TAKER, {"action": "create", "title": title, "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    return body["item"]["item_id"]


# ── acceptance 5: no flag, today's behaviour ──────────────────────────────


@pytest.mark.asyncio
async def test_without_the_flag_the_new_manager_is_refused_worker_not_owned():
    """A plain handoff records a manager with no bind rights; B is still refused."""
    _SLOTS[WORKER] = _Slot(WORKER, created_by=GIVER, tab_id=WORKER_TAB)
    await _dispatch_on(GIVER, "A item")
    # A plain handoff: manager moves, bind rights do not (two-element write).
    session_managers.write({WORKER: (TAKER, WORKER_TAB)})
    session_managers.reset_cache()

    item_b = await _taker_create("B item")
    status, body = await _record(
        TAKER, {"action": "bind", "item_id": item_b, "worker_session_key": WORKER}
    )
    assert status == 403, body
    assert body["code"] == "worker_not_owned"


# ── acceptance 1: B binds ok ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_the_flag_the_new_manager_binds_the_session():
    _SLOTS[WORKER] = _Slot(WORKER, created_by=GIVER, tab_id=WORKER_TAB)
    await _dispatch_on(GIVER, "A item")
    _hand_off_with_bind_rights()

    item_b = await _taker_create("B item")
    status, body = await _record(
        TAKER, {"action": "bind", "item_id": item_b, "worker_session_key": WORKER}
    )
    assert status == 200, body
    assert wl.read_binding(WORKER) == (TAKER, item_b)


# ── acceptance 4: A's old item is released with a handoff event ────────────


@pytest.mark.asyncio
async def test_the_givers_open_item_is_released_with_a_handoff_event():
    _SLOTS[WORKER] = _Slot(WORKER, created_by=GIVER, tab_id=WORKER_TAB)
    item_a = await _dispatch_on(GIVER, "A item")
    _hand_off_with_bind_rights()

    item_b = await _taker_create("B item")
    status, body = await _record(
        TAKER, {"action": "bind", "item_id": item_b, "worker_session_key": WORKER}
    )
    assert status == 200, body

    released = wl.read_work_item(GIVER, item_a)
    assert released is not None
    assert released.state == "abandoned"
    assert released.is_terminal
    events = wl.read_events(GIVER, item_a)
    closes = [e for e in events if getattr(e, "kind", "") == "close"]
    assert closes, "the release must leave a close event on the giver's board"
    assert "handoff" in (closes[-1].text or "").lower()


# ── acceptance 3: work_report lands on B's item ───────────────────────────


@pytest.mark.asyncio
async def test_the_sessions_report_lands_on_the_new_managers_item():
    _SLOTS[WORKER] = _Slot(WORKER, created_by=GIVER, tab_id=WORKER_TAB)
    item_a = await _dispatch_on(GIVER, "A item")
    _hand_off_with_bind_rights()
    item_b = await _taker_create("B item")
    status, _ = await _record(
        TAKER, {"action": "bind", "item_id": item_b, "worker_session_key": WORKER}
    )
    assert status == 200

    status, body = await _report(WORKER, {"status": "progress", "summary": "moved to B"})
    assert status == 200, body

    on_b = wl.read_work_item(TAKER, item_b)
    assert on_b is not None and on_b.summary == "moved to B"
    # A's item was released before the report, so it carries no worker summary.
    on_a = wl.read_work_item(GIVER, item_a)
    assert on_a is not None and on_a.summary == ""


# ── acceptance 2: A's later bind is refused ───────────────────────────────


@pytest.mark.asyncio
async def test_after_the_handoff_the_giver_can_no_longer_bind_the_session():
    _SLOTS[WORKER] = _Slot(WORKER, created_by=GIVER, tab_id=WORKER_TAB)
    await _dispatch_on(GIVER, "A item")
    _hand_off_with_bind_rights()
    item_b = await _taker_create("B item")
    status, _ = await _record(
        TAKER, {"action": "bind", "item_id": item_b, "worker_session_key": WORKER}
    )
    assert status == 200

    # A opens a second item and tries to bind the session it gave away. Once a record
    # names a bind-rights holder (B), that holder is the SOLE admission: A, the
    # creator, does not qualify, so the ownership check refuses it.
    status, body = await _record(
        GIVER,
        {"action": "create", "title": "A take-back", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200
    status, body = await _record(
        GIVER, {"action": "bind", "item_id": body["item"]["item_id"], "worker_session_key": WORKER}
    )
    assert status == 403, body
    assert body["code"] == "worker_not_owned"
    # B still holds the binding.
    assert wl.read_binding(WORKER) == (TAKER, item_b)


# ── a malformed / tab-mismatched record withholds bind rights (fail closed) ─


@pytest.mark.asyncio
async def test_a_tab_mismatched_record_does_not_move_bind_rights():
    """A record whose tab is not the slot's reads as no bind rights, so B is refused."""
    _SLOTS[WORKER] = _Slot(WORKER, created_by=GIVER, tab_id=WORKER_TAB)
    await _dispatch_on(GIVER, "A item")
    session_managers.write({WORKER: (TAKER, "some-other-tab", True)})
    session_managers.reset_cache()

    item_b = await _taker_create("B item")
    status, body = await _record(
        TAKER, {"action": "bind", "item_id": item_b, "worker_session_key": WORKER}
    )
    assert status == 403, body
    assert body["code"] == "worker_not_owned"


# ── the creator cannot reclaim the worker after B's item closes (review F3) ─


@pytest.mark.asyncio
async def test_the_creator_cannot_reclaim_the_worker_after_the_new_item_closes():
    """Once bind rights name B, the creator A is refused even after B's item is terminal.

    The spec and tool text promise the giver's later bind is refused. A record naming a
    bind-rights holder is the sole admission, so A -- the creator -- does not qualify,
    whether B's item is open or closed.
    """
    _SLOTS[WORKER] = _Slot(WORKER, created_by=GIVER, tab_id=WORKER_TAB)
    await _dispatch_on(GIVER, "A item")
    _hand_off_with_bind_rights()
    item_b = await _taker_create("B item")
    status, _ = await _record(
        TAKER, {"action": "bind", "item_id": item_b, "worker_session_key": WORKER}
    )
    assert status == 200

    # B closes its item, so the one-open-item guard would not stop A.
    status, _ = await _record(TAKER, {"action": "close", "item_id": item_b, "state": "accepted"})
    assert status == 200
    assert wl.read_work_item(TAKER, item_b).is_terminal

    status, body = await _record(
        GIVER,
        {"action": "create", "title": "A take-back", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200
    status, body = await _record(
        GIVER, {"action": "bind", "item_id": body["item"]["item_id"], "worker_session_key": WORKER}
    )
    assert status == 403, body
    assert body["code"] == "worker_not_owned"
    # The binding still names B's (now terminal) item; A never repointed it.
    assert wl.read_binding(WORKER) == (TAKER, item_b)


# ── a refused destination bind does not abandon the giver's item (review F2) ─


@pytest.mark.asyncio
async def test_a_refused_destination_bind_leaves_the_givers_item_open():
    """B binds to an already-closed destination item: the bind is refused and A's item
    stays OPEN. The release runs only once this bind's destination would take, so a
    refused bind never strands the worker on an abandoned item."""
    _SLOTS[WORKER] = _Slot(WORKER, created_by=GIVER, tab_id=WORKER_TAB)
    item_a = await _dispatch_on(GIVER, "A item")
    _hand_off_with_bind_rights()
    item_b = await _taker_create("B item")
    # B closes its own destination item before trying to bind the worker to it.
    status, _ = await _record(TAKER, {"action": "close", "item_id": item_b, "state": "accepted"})
    assert status == 200

    status, body = await _record(
        TAKER, {"action": "bind", "item_id": item_b, "worker_session_key": WORKER}
    )
    assert status != 200, body
    # A's item is untouched: still open, still the worker's binding.
    on_a = wl.read_work_item(GIVER, item_a)
    assert on_a is not None and not on_a.is_terminal
    assert wl.read_binding(WORKER) == (GIVER, item_a)


# ── a History resume keeps the handoff's bind rights (review F4) ────────────


def test_a_resume_carries_the_bind_flag_through():
    """``recorded_manager_with_bind`` reports the flag, so a resume rewrite keeps it.

    ``rebind_resumed`` rewrites the record for the new tab; a two-element write would
    clear ``"bind": true`` and silently drop the handoff's bind rights.
    """
    session_managers.write({WORKER: (TAKER, WORKER_TAB, True)})
    session_managers.reset_cache()

    manager, bind = session_managers.recorded_manager_with_bind(WORKER, WORKER_TAB)
    assert manager == TAKER
    assert bind is True

    # Simulate the resume rewrite for a new tab, carrying the flag through.
    new_tab = "tab-worker-2"
    session_managers.write({WORKER: (manager, new_tab, bind)})
    session_managers.reset_cache()
    manager2, bind2 = session_managers.recorded_manager_with_bind(WORKER, new_tab)
    assert manager2 == TAKER
    assert bind2 is True
    # A plain two-element rewrite would have dropped it.
    assert (
        session_managers.bind_rights_holder(_Slot(WORKER, created_by=GIVER, tab_id=new_tab))
        == TAKER
    )
