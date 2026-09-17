"""Continue and project refuse a slot recreated under the same name while queued.

``_slot_replaced_while_queued`` closes a window every handler that resolves a
slot by NAME and then awaits a lock shares: slot removal and same-name
re-registration take none of those locks, so ``name`` can belong to a different
``_ChatSlot`` by the time the request resumes, while ``effective_session_key``
resolves both objects to the same ``dashboard:<name>`` session. Everything the
handler does after the await reads the STALE object, so the authorization taken
against it lands its effect on the REPLACEMENT's session.

Both handlers below have exactly that shape -- resolve the slot by name, await
the lock, then act on it -- so both need the re-check immediately after the
await, before any read of ``slot`` that feeds an authorization:

* ``api_chat_slot_continue`` takes ``slot._lock`` and then DISPATCHES a turn --
  ``_start_next_queued_turn`` runs it under ``effective_session_key``, so the
  stale request's authorization starts an agent turn, running tools and writing
  to the repo, on the replacement's session. Its own docstring states that this
  path "is not a read".
* ``api_chat_slot_project`` takes ``slot._lock`` and then arms
  ``slot._pending_reset_history_key`` -- a DEFERRED session reset carrying that
  same shared key -- and records a ``chat_slot_project`` **allowed** SEL row
  naming ``slot=<name>``, which resolves to the replacement.

Each test parks the request on the lock-acquisition await, swaps in a same-named
replacement, releases, and asserts the handler refuses without reaching its
effect. The uncontended controls pin that the re-check is a no-op when the slot
is still the registered one.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import api_chat_slot_continue, api_chat_slot_project
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

_SLOT = "s1"
_OLD_PROJECT = "/old/project"
_NEW_PROJECT = "/new/project"

# Scheduler turns a bounded yield will spend before giving up -- turns, not
# seconds, for the reason the sibling seam tests give.
_MAX_TURNS = 500


async def _yield_until(predicate: Callable[[], bool]) -> bool:
    for _ in range(_MAX_TURNS):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


def _make_app(state: DashboardState) -> web.Application:
    # Mirror production: token_auth sets request["app"] on every authenticated
    # path ("" = dashboard user); the isolation guards fail closed without it.
    @web.middleware
    async def dashboard_auth_marker(request, handler):
        if "app" not in request:
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[dashboard_auth_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


def _conversational_slot(key: str = _SLOT) -> _ChatSlot:
    """An idle slot holding a real turn -- what both handlers require to act.

    Continue refuses an empty slot outright (``_has_conversation``), so without a
    transcript the request would never reach the await this test is about.
    """
    s = _ChatSlot(key)
    s.project = _OLD_PROJECT
    s.messages.append({"role": "user", "content": "do the thing", "cls": "msg msg-u"})
    s.messages.append({"role": "assistant", "content": "done", "cls": "msg msg-a"})
    return s


@pytest.fixture
def slot() -> _ChatSlot:
    return _conversational_slot()


@pytest.fixture
def state(slot: _ChatSlot) -> DashboardState:
    st = MagicMock(spec=DashboardState)
    st._slots = {slot.key: slot}
    st.sessions = MagicMock()
    st.sessions.reset = AsyncMock(return_value=True)
    return st


@pytest.fixture
def sel_log(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    monkeypatch.setattr(chat_handlers, "sel", lambda: fake)
    return fake


@pytest.fixture
def start_turn(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stand in for the real dispatch so a refusal is observable as "never called"."""
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    # Continue's children guard and project's eager respawn both reach well past
    # the seam under test; neither decides the outcome here.
    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "_save_recent_project", MagicMock(return_value=None))
    # Project validates the path against the real filesystem before the lock.
    monkeypatch.setattr(chat_handlers.os.path, "isdir", lambda p: True)


async def _race_recreate_while_queued(
    state: DashboardState,
    held_lock: asyncio.Lock,
    route: str,
    body: dict,
) -> tuple[int, dict, _ChatSlot]:
    """POST *route* while *held_lock* is held, swap the slot, release, collect.

    Holding the lock externally parks the request on exactly one
    lock-acquisition await: it has already read the (still-current) slot and
    passed every check before that await. The swap then lands while it is
    queued, and whatever the handler does next runs against the STALE object.
    """
    replacement = _conversational_slot()
    async with TestClient(TestServer(_make_app(state))) as client:
        async with held_lock:
            task = asyncio.create_task(client.post(route, json=body))
            stuck = not await _yield_until(lambda: task.done())
            assert stuck, f"{route} completed without ever contending for the held lock"
            # Same shape a delete-then-recreate under one name produces.
            state._slots[_SLOT] = replacement
        try:
            finished = await _yield_until(lambda: task.done())
            assert finished, f"{route} never completed after the lock was released"
            resp = task.result()
            status = resp.status
            payload = await resp.json()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return status, payload, replacement


def _assert_denied_row(sel_log: MagicMock, operation: str) -> None:
    rows = [
        c.kwargs
        for c in sel_log.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("operation") == operation
    ]
    assert rows, f"no SEL api_access denied row for {operation}"
    assert rows[-1]["resources"] == f"slot={_SLOT}"


def _allowed_rows(sel_log: MagicMock, operation: str) -> list[dict]:
    return [
        c.kwargs
        for c in sel_log.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "allowed" and c.kwargs.get("operation") == operation
    ]


class TestContinueRefusesARecreatedSlot:
    @pytest.mark.asyncio
    async def test_refuses_when_recreated_while_queued_on_slot_lock(
        self, state, slot, sel_log, start_turn
    ):
        status, payload, replacement = await _race_recreate_while_queued(
            state, slot._lock, f"/api/chat/slots/{_SLOT}/continue", {}
        )
        assert status == 404
        assert payload["code"] == "slot_not_found"
        # The effect, not just the status: no turn may be dispatched under the
        # session key the replacement now answers on.
        start_turn.assert_not_awaited()
        assert not slot._queue, "a continuation was queued on the stale slot"
        assert not replacement._queue
        assert state._slots[_SLOT] is replacement
        _assert_denied_row(sel_log, "chat.slot_continue")

    @pytest.mark.asyncio
    async def test_uncontended_continue_still_dispatches(self, state, slot, sel_log, start_turn):
        """Control: the re-check is a no-op while the slot is still registered."""
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/continue", json={})
            assert resp.status == 200, await resp.text()
        start_turn.assert_awaited_once()
        denied = [
            c.kwargs
            for c in sel_log.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert not denied, f"uncontended continue emitted a denial: {denied}"


class TestProjectRefusesARecreatedSlot:
    @pytest.mark.asyncio
    async def test_refuses_when_recreated_while_queued_on_slot_lock(self, state, slot, sel_log):
        status, payload, replacement = await _race_recreate_while_queued(
            state, slot._lock, f"/api/chat/slots/{_SLOT}/project", {"project": _NEW_PROJECT}
        )
        assert status == 404
        # The effect: no deferred session reset may be armed on the shared
        # ``dashboard:<name>`` key, and the replacement's project stands.
        assert slot._pending_reset_history_key is None
        assert replacement._pending_reset_history_key is None
        assert replacement.project == _OLD_PROJECT
        assert state._slots[_SLOT] is replacement
        # A refusal must not also be recorded as a completed project switch.
        assert not _allowed_rows(sel_log, "chat_slot_project")
        _assert_denied_row(sel_log, "chat.slot_project")

    @pytest.mark.asyncio
    async def test_uncontended_project_still_commits(self, state, slot, sel_log):
        """Control: the re-check is a no-op while the slot is still registered."""
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{_SLOT}/project", json={"project": _NEW_PROJECT}
            )
            assert resp.status == 200, await resp.text()
        # The handler stores the REALPATH of what it was sent, so the expectation
        # is derived the same way rather than spelled as a literal -- the two
        # differ on Windows, where a POSIX-looking argument gains a drive.
        assert str(slot.project) == os.path.realpath(os.path.expanduser(_NEW_PROJECT))
        denied = [
            c.kwargs
            for c in sel_log.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert not denied, f"uncontended project set emitted a denial: {denied}"
