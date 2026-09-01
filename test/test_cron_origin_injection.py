"""Origin routing for cron notify().

When a cron sends session="origin", api_send_message injects the message into the
originating dashboard chat if it can both resolve and reach that chat's slot;
otherwise it posts a dashboard notification.

Injection requires:
  1. caller_session="cron:<id>" present and the job has a session_key
     (_resolve_session_target maps these to a slot key).
  2. the slot key matches a live or rehydratable slot (api_send_message).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

if "kiro_crew.slack.handler" not in sys.modules:
    _stub = types.ModuleType("kiro_crew.slack.handler")
    _stub.is_allowed_user = lambda uid: False  # type: ignore[attr-defined]
    _stub.is_tracked_channel = lambda cid: False  # type: ignore[attr-defined]
    sys.modules["kiro_crew.slack.handler"] = _stub

from kiro_crew.cron import CronJob  # noqa: E402
from kiro_crew.cron_script import ScriptContext  # noqa: E402
from kiro_crew.dashboard.handlers import api_send_message  # noqa: E402

JOB_ID = "job12345"


def _make_app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    return app


def _state_with_job(*, slot, session_key="dashboard:chat-2-origin"):
    job = CronJob(id=JOB_ID, name="autosde-Z", message="", schedule=None, session_key=session_key)
    state = MagicMock()
    state.slack_client = None
    state.owner_id = ""
    state.crons.list_jobs.return_value = [job]
    state.get_slot.return_value = slot
    return state


def _live_running_slot():
    slot = MagicMock()
    slot.running = True
    slot._in_stage_execution = False
    slot._queue = []
    slot.queue_append.return_value = "q1"
    return slot


def _mid_plan_slot():
    """A slot whose plan is executing but is BETWEEN stages: the orchestrator
    has set slot.task = None (so slot.running is False) while _in_stage_execution
    stays True. slot.running alone would misread this as idle."""
    slot = MagicMock()
    slot.running = False
    slot._in_stage_execution = True
    slot._queue = []
    slot.queue_append.return_value = "q1"
    return slot


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


# ── resolution ──


@pytest.mark.asyncio
async def test_codecron_no_caller_session_falls_back_to_bell(mock_sel):
    """A request with no caller_session resolves no origin slot, so it posts a
    notification and never looks up a slot."""
    state = _state_with_job(slot=_live_running_slot())
    async with TestClient(TestServer(_make_app(state))) as c:
        resp = await c.post("/api/send-message", json={"text": "comments!", "session": "origin"})
        data = await resp.json()
    assert data["session"] is False
    state.notify.assert_called_once()
    state.get_slot.assert_not_called()


@pytest.mark.asyncio
async def test_caller_session_resolves_and_injects(mock_sel):
    """caller_session plus a live slot queues the message into that slot and
    sends no notification."""
    slot = _live_running_slot()
    state = _state_with_job(slot=slot)
    async with TestClient(TestServer(_make_app(state))) as c:
        resp = await c.post(
            "/api/send-message",
            json={"text": "comments!", "session": "origin", "caller_session": f"cron:{JOB_ID}"},
        )
        data = await resp.json()
    assert data["session"] is True
    slot.queue_append.assert_called_once()
    state.notify.assert_not_called()


@pytest.mark.asyncio
async def test_mid_plan_stage_window_queues_not_concurrent_turn(mock_sel):
    """Regression: an origin injection landing in a plan's between-stages window
    must QUEUE, not start a concurrent turn.

    During multi-stage execution the orchestrator nulls slot.task between stages,
    so slot.running reads False even though the plan is still running. Guarding on
    slot.running alone let the cron injection start a second turn that scattered
    the plan's output. The guard now also checks _in_stage_execution.
    """
    slot = _mid_plan_slot()
    state = _state_with_job(slot=slot)
    with (
        patch("kiro_crew.dashboard.chat_runner._run_chat") as run_chat,
        patch("kiro_crew.dashboard.turn_dispatch.spawn_guarded_turn") as spawn,
    ):
        async with TestClient(TestServer(_make_app(state))) as c:
            resp = await c.post(
                "/api/send-message",
                json={"text": "comments!", "session": "origin", "caller_session": f"cron:{JOB_ID}"},
            )
            data = await resp.json()
    assert data["session"] is True
    slot.queue_append.assert_called_once()  # queued, not run
    spawn.assert_not_called()
    run_chat.assert_not_called()
    state.notify.assert_not_called()


# ── slot lookup ──


@pytest.mark.asyncio
async def test_resolved_key_with_no_live_slot_falls_back_to_bell(mock_sel):
    """When the resolved slot key has no live slot and cannot be rehydrated,
    it posts a notification."""
    state = _state_with_job(slot=None)
    with patch(
        "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async",
        return_value=None,
    ):
        async with TestClient(TestServer(_make_app(state))) as c:
            resp = await c.post(
                "/api/send-message",
                json={"text": "comments!", "session": "origin", "caller_session": f"cron:{JOB_ID}"},
            )
            data = await resp.json()
    assert data["session"] is False
    state.notify.assert_called_once()


# ── notify payload ──


def test_notify_sets_caller_session():
    """notify() sets caller_session to cron:<job.id> in the payload."""
    ctx = ScriptContext(job=CronJob(id=JOB_ID, name="x", message="", schedule=None))
    ctx._post = MagicMock(return_value={})
    ctx.notify("hello", session="origin")
    _path, payload = ctx._post.call_args.args
    assert payload["caller_session"] == f"cron:{JOB_ID}"
    assert payload["session"] == "origin"


def test_notify_caller_session_cannot_be_overridden():
    """A script-supplied caller_session cannot override the cron's own id."""
    ctx = ScriptContext(job=CronJob(id=JOB_ID, name="x", message="", schedule=None))
    ctx._post = MagicMock(return_value={})
    ctx.notify("hello", session="origin", caller_session="cron:evil")
    _path, payload = ctx._post.call_args.args
    assert payload["caller_session"] == f"cron:{JOB_ID}"
