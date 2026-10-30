"""Rebinding a worker session whose work item is closed.

A conductor that closes the item of a worker still running cuts that worker's
report channel; binding a new item to the same session restores it. A session
bound to an OPEN item is never bound to a second one. Driven through the real
routes and store, with the harness of ``test_work_ledger_tools``.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import test_work_ledger_tools as tools
from test_work_ledger_tools import (
    _SLOTS,
    CONDUCTOR_A,
    CONDUCTOR_B,
    WORKER_A,
    WORKER_B,
    _brief,
    _dispatched,
    _record,
    _report,
    routes,
    two_by_two,
)

from kiro_crew import work_ledger as wl

_isolated_home = tools._isolated_home
_open_route = tools._open_route
_clean_slots = tools._clean_slots


async def _create(conductor: str, title: str) -> str:
    status, body = await _record(
        conductor,
        {"action": "create", "title": title, "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200, body
    return body["item"]["item_id"]


async def _close(conductor: str, item_id: str, state: str = "accepted") -> dict[str, Any]:
    status, body = await _record(conductor, {"action": "close", "item_id": item_id, "state": state})
    assert status == 200, body
    return body


@pytest.mark.asyncio
async def test_a_live_worker_whose_item_closed_is_rebound_and_its_report_wakes_the_new_item():
    """A conductor closes the item of a worker that is still running. The
    worker's reports are then refused ``item_closed``, and the only way back is a new
    item bound to the SAME session -- a fresh session would lose the worker's state
    and its own sub-workers. After the rebind the worker's brief and report resolve
    to the new item, and the closed one is not touched."""
    ids = await two_by_two()
    await _close(CONDUCTOR_A, ids["item_a"])
    closed_before = wl.item_path(CONDUCTOR_A, ids["item_a"]).read_bytes()

    status, body = await _report(WORKER_A, {"status": "question", "summary": "still here"})
    assert status == 409
    assert body["code"] == wl.CODE_ITEM_CLOSED

    second = await _create(CONDUCTOR_A, "second")
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": second, "worker_session_key": WORKER_A},
    )
    assert status == 200, body
    assert wl.read_binding(WORKER_A) == (CONDUCTOR_A, second)

    status, body = await _brief(WORKER_A)
    assert status == 200, body
    assert body["brief"]["item_id"] == second

    status, body = await _report(WORKER_A, {"status": "question", "summary": "still here"})
    assert status == 200, body
    item = wl.read_work_item(CONDUCTOR_A, second)
    assert item is not None
    assert (item.status, item.summary) == ("question", "still here")
    assert wl.item_path(CONDUCTOR_A, ids["item_a"]).read_bytes() == closed_before


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["accepted", "rejected", "abandoned"])
async def test_every_terminal_state_frees_the_session_for_a_new_item(terminal):
    ids = await two_by_two()
    await _close(CONDUCTOR_A, ids["item_a"], terminal)
    second = await _create(CONDUCTOR_A, "second")
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": second, "worker_session_key": WORKER_A},
    )
    assert status == 200, body
    assert wl.read_binding(WORKER_A) == (CONDUCTOR_A, second)


@pytest.mark.asyncio
async def test_a_session_bound_to_an_open_item_is_never_bound_to_a_second():
    """One live item per session: a second bind would silently repoint the worker's
    only report channel away from an item that is still waiting on it."""
    ids = await two_by_two()
    second = await _create(CONDUCTOR_A, "second")

    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": second, "worker_session_key": WORKER_A},
    )
    assert status == 409
    assert body["code"] == "worker_already_dispatched"
    # The refusal names the way out instead of "create a new session".
    assert "open item" in body["error"]
    assert "close" in body["error"]
    item = wl.read_work_item(CONDUCTOR_A, second)
    assert item is not None and item.worker_session_key is None
    assert wl.read_binding(WORKER_A) == (CONDUCTOR_A, ids["item_a"])

    # A fresh session is still a valid way to staff the new item.
    _dispatched("chat-a-worker-2", CONDUCTOR_A)
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": second, "worker_session_key": "chat-a-worker-2"},
    )
    assert status == 200, body


@pytest.mark.asyncio
async def test_a_session_rebound_twice_reports_only_to_its_latest_item():
    ids = await two_by_two()
    await _close(CONDUCTOR_A, ids["item_a"])
    second = await _create(CONDUCTOR_A, "second")
    assert (
        await _record(
            CONDUCTOR_A,
            {"action": "bind", "item_id": second, "worker_session_key": WORKER_A},
        )
    )[0] == 200
    await _close(CONDUCTOR_A, second, "abandoned")
    third = await _create(CONDUCTOR_A, "third")
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": third, "worker_session_key": WORKER_A},
    )
    assert status == 200, body

    status, _ = await _report(WORKER_A, {"status": "progress", "summary": "on third"})
    assert status == 200
    latest = wl.read_work_item(CONDUCTOR_A, third)
    assert latest is not None and latest.summary == "on third"
    for old in (ids["item_a"], second):
        stale = wl.read_work_item(CONDUCTOR_A, old)
        assert stale is not None and stale.summary != "on third"


@pytest.mark.asyncio
async def test_another_conductors_freed_session_is_still_not_bindable():
    """A terminal binding frees the session for ITS conductor, not for anyone."""
    ids = await two_by_two()
    await _close(CONDUCTOR_A, ids["item_a"])
    theirs = await _create(CONDUCTOR_B, "B wants A's worker")
    status, body = await _record(
        CONDUCTOR_B,
        {"action": "bind", "item_id": theirs, "worker_session_key": WORKER_A},
    )
    assert status == 403
    assert body["code"] == "worker_not_owned"
    assert wl.read_binding(WORKER_A) == (CONDUCTOR_A, ids["item_a"])


def test_the_open_item_check_fails_closed(monkeypatch):
    """An unreadable store must not admit a rebind: the alternative is deciding the
    one question the refusal exists for on a store that could not answer."""

    def _boom(*_a: Any, **_k: Any):
        raise OSError("store unreadable")

    monkeypatch.setattr(routes.work_ledger, "read_binding", _boom)
    assert routes.worker_holds_open_item("chat-whoever") is True


@pytest.mark.asyncio
async def test_the_bind_route_asks_the_open_item_check_off_the_loop(monkeypatch):
    """The check reads the store, whose retry on a transient read error is skipped
    on the loop thread, so the route must ask it from a worker thread."""
    ids = await two_by_two()
    await _close(CONDUCTOR_A, ids["item_a"])
    second = await _create(CONDUCTOR_A, "second")
    loop_thread = threading.get_ident()
    asked_on: list[int] = []
    real = routes.worker_holds_open_item

    def _spy(key: str) -> bool:
        asked_on.append(threading.get_ident())
        return real(key)

    monkeypatch.setattr(routes, "worker_holds_open_item", _spy)
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": second, "worker_session_key": WORKER_A},
    )
    assert status == 200, body
    assert asked_on and loop_thread not in asked_on


@pytest.mark.asyncio
async def test_closing_a_live_workers_item_warns_that_its_report_channel_is_cut():
    ids = await two_by_two()
    body = await _close(CONDUCTOR_A, ids["item_a"])
    warning = body.get("warning") or ""
    assert WORKER_A in warning
    assert "work_report" in warning
    assert "bind" in warning

    # A worker whose session is gone has no channel left to cut.
    _SLOTS.pop(WORKER_B)
    body = await _close(CONDUCTOR_B, ids["item_b"])
    assert "warning" not in body
