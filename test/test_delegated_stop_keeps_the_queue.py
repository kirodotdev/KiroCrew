"""A delegated Stop ends the running turn and nothing more.

A granted app reaches ``POST /api/chat/slots/{slot}/stop`` through the dashboard proxy
with a ``via_app`` token (one bare stop is a ``READ_ALLOW`` row). A second press while the
cooperative cancel is pending escalates to the hard kill for the owner, and that branch
clears the queue and drops pending steers. An app must not be able to discard the owner's
queued work that way, so its stop never escalates, and its audit names the app.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

_TEST_DIR = str(Path(__file__).resolve().parent)

SLOT = "chat-41001"
KEY = "dashboard:" + SLOT


def _dashboard_state(tmp_path):
    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from chat_test_helpers import _make_state

    return _make_state(tmp_path)


def _running(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _fences(monkeypatch, state, *, kills: list) -> list[dict]:
    state.sessions.is_compacting = MagicMock(return_value=False)

    async def _stop_turn(*_args, **kwargs):
        kills.append(kwargs.get("force"))
        return "hard" if kwargs.get("force") else "soft"

    state.sessions.stop_turn = AsyncMock(side_effect=_stop_turn)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.deny_app_slot_access", lambda *_a, **_k: None
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.effective_session_key", lambda slot: KEY)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.own_session_key", lambda slot: KEY)
    lines: list[dict] = []

    def _sel():
        sel = MagicMock()
        sel.log_tool_invocation.side_effect = lambda **kwargs: lines.append(kwargs)
        return sel

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", _sel)
    return lines


def _wire_route(state, *, via_app: str) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_identity(request: web.Request, handler):
        # What ``enforce_delegated`` publishes for a delegated token; no ``app`` claim.
        request["app"] = ""
        if via_app:
            request["via_app"] = via_app
        return await handler(request)

    app.middlewares.append(_publish_identity)
    app.router.add_post("/api/chat/slots/{slot}/stop", api_chat_slot_stop)
    return app


def _stop_lines(lines: list[dict]) -> list[dict]:
    return [kw for kw in lines if kw.get("tool_name") == "dashboard_stop"]


@pytest.mark.asyncio
async def test_a_delegated_second_stop_does_not_escalate_or_clear_the_queue(
    tmp_path, monkeypatch
) -> None:
    state = _dashboard_state(tmp_path)
    slot = _running(state.get_or_create_slot(SLOT))
    slot._queue = [{"id": "q1", "text": "the owner's next message"}]
    kills: list = []
    lines = _fences(monkeypatch, state, kills=kills)
    async with TestClient(TestServer(_wire_route(state, via_app="tunnel"))) as client:
        first = await client.post(f"/api/chat/slots/{SLOT}/stop")
        second = await client.post(f"/api/chat/slots/{SLOT}/stop")
        assert first.status == 200 and second.status == 200
    # One cooperative stop; the repeat is not a hard kill.
    assert kills == [False]
    assert [item["id"] for item in slot._queue] == ["q1"]
    assert not any(line["metadata"].get("escalated") for line in _stop_lines(lines))
    assert {line["metadata"]["via"] for line in _stop_lines(lines)} == {"delegated:tunnel"}


@pytest.mark.asyncio
async def test_the_owner_second_stop_still_escalates(tmp_path, monkeypatch) -> None:
    state = _dashboard_state(tmp_path)
    _running(state.get_or_create_slot(SLOT))
    kills: list = []
    lines = _fences(monkeypatch, state, kills=kills)
    async with TestClient(TestServer(_wire_route(state, via_app=""))) as client:
        await client.post(f"/api/chat/slots/{SLOT}/stop")
        await client.post(f"/api/chat/slots/{SLOT}/stop")
    assert _stop_lines(lines)[-1]["metadata"]["escalated"] is True
    assert {line["metadata"]["via"] for line in _stop_lines(lines)} == {"dashboard"}


async def _cron_cancel_allowed(
    monkeypatch, *, via_app: str = "", app: str = ""
) -> tuple[bool, AsyncMock]:
    """Whether a Stop on a ``cron:`` tab may run the cron service's cancel.

    The owner gate passes, as it does for the owner and for a delegated token on the
    owner's machine (it is stubbed and its call asserted). The foreign-app-job guard is
    the real one, so an app caller meets its refusal.
    """
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.chat_api import turn_stop

    owner_gate = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request", owner_gate
    )
    request = make_mocked_request("POST", "/api/chat/slots/cron:job1/stop")
    request["app"] = app
    if via_app:
        request["via_app"] = via_app
    state = MagicMock()
    # The owner's job: no ``app:<name>`` stamp, so no app owns it.
    state.crons.get_job.return_value = MagicMock(created_by="dashboard")
    allowed = await turn_stop._may_cancel_cron_run(request, state, "cron:job1")
    return allowed, owner_gate


@pytest.mark.asyncio
async def test_a_delegated_stop_on_a_cron_tab_never_cancels_the_job(monkeypatch) -> None:
    """The cron cancel pauses a one-shot job: a control-plane write no stop row allows."""
    allowed, owner_gate = await _cron_cancel_allowed(monkeypatch, via_app="tunnel")
    assert allowed is False
    owner_gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_owner_stop_on_a_cron_tab_still_cancels_the_job(monkeypatch) -> None:
    allowed, owner_gate = await _cron_cancel_allowed(monkeypatch)
    assert allowed is True
    owner_gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_app_stop_on_another_owners_cron_tab_meets_the_real_guard(monkeypatch) -> None:
    """The foreign-app-job guard runs for real: an app does not own the owner's job."""
    allowed, owner_gate = await _cron_cancel_allowed(monkeypatch, app="notes")
    assert allowed is False
    owner_gate.assert_not_awaited()
