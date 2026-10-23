"""A bell note an agent publishes links back to the session that published it.

``send_notification`` and the ``send_message`` bell fallback stamp the note's ``slot``
and ``history_key`` from the caller's attested session key. The notification detail
panel's Go to chat reads ``slot``, and its Resume chat resumes with ``history_key``,
because the bare slot name names no transcript. Only the publisher's own slot counts: a
cron run with no tab of its own gets no link, rather than the tab of the chat that
created the cron. Nothing in the request body chooses the slot.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from chat_test_helpers import _make_state as _make_chat_state

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers.messaging import (
    _publishing_session_meta,
    api_notification_agent_push,
)
from kiro_crew.dashboard.messaging_api import proactive_send
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

ATTESTED = {"peer_verified": True}


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _open(state: DashboardState, key: str) -> str:
    """Register a live slot and return the session key a caller presents for it."""
    slot = _ChatSlot(key)
    state._slots[key] = slot
    return effective_session_key(slot)


def _cron_run(state: DashboardState, created_by: str) -> str:
    """Register cron job ``abc`` created by the session *created_by*; return a run's key."""
    job = SimpleNamespace(id="abc", name="nightly", session_key=created_by)
    state.crons.list_jobs.return_value = [job]
    return "cron:abc:run-1"


def _open_cron_tab(state: DashboardState, monkeypatch) -> None:
    """Give cron job ``abc`` an open dashboard tab bound to its cron session."""
    monkeypatch.setattr("kiro_crew.session_surface._dashboard_surfaced", frozenset({"cron:abc"}))
    slot = _ChatSlot("cron-abc")
    slot.linked_session_key = "cron:abc"
    state._slots[slot.key] = slot


class TestPublishingSessionMeta:
    def test_a_chat_session_links_to_its_own_slot(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200")
        assert _publishing_session_meta(state, worker) == {
            "slot": "chat-2-200",
            "history_key": "dashboard:chat-2-200",
        }

    def test_a_session_the_dashboard_no_longer_holds_gets_no_link(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-2-200")
        assert _publishing_session_meta(state, "dashboard:chat-9-900") is None

    def test_a_caller_with_no_attested_key_gets_no_link(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-2-200")
        assert _publishing_session_meta(state, "") is None

    def test_a_cron_run_with_its_own_tab_links_there(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        creator = _open(state, "chat-1-100")
        run = _cron_run(state, creator)
        _open_cron_tab(state, monkeypatch)
        assert _publishing_session_meta(state, run) == {
            "slot": "cron-abc",
            "history_key": "cron:abc",
        }

    def test_a_cron_run_without_a_tab_gets_no_link_to_the_chat_that_created_it(
        self, monkeypatch, tmp_path
    ):
        """The banner hides a note whose slot is the focused chat, and the creator's chat
        never shows the cron's output, so borrowing its slot would hide the cron's alert
        exactly while the user sits in that chat."""
        state = _make_state(monkeypatch, tmp_path)
        run = _cron_run(state, _open(state, "chat-1-100"))
        assert _publishing_session_meta(state, run) is None


def _agent_push_app(state: DashboardState, marks: dict) -> web.Application:
    @web.middleware
    async def _auth_marker(request, handler):
        request["internal_auth"] = True
        for key, value in marks.items():
            request[key] = value
        return await handler(request)

    app = web.Application(middlewares=[_auth_marker])
    app["state"] = state
    app.router.add_post("/api/notifications/agent", api_notification_agent_push)
    return app


async def _push(state: DashboardState, marks: dict, caller: str, body: dict) -> dict:
    async with TestClient(TestServer(_agent_push_app(state, marks))) as client:
        resp = await client.post(
            "/api/notifications/agent", json=body, headers={"X-Session-Key": caller}
        )
        assert resp.status == 200
        return (await resp.json())["note"]


class TestAgentPushRoute:
    @pytest.mark.asyncio
    async def test_send_notification_links_to_the_calling_session(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200")
        note = await _push(state, ATTESTED, worker, {"title": "Review ready"})
        assert note["slot"] == "chat-2-200"
        assert note["history_key"] == "dashboard:chat-2-200"
        assert state._notification_log[-1]["slot"] == "chat-2-200"
        assert state._notification_log[-1]["history_key"] == "dashboard:chat-2-200"

    @pytest.mark.asyncio
    async def test_the_request_body_cannot_choose_the_slot(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100")
        worker = _open(state, "chat-2-200")
        note = await _push(state, ATTESTED, worker, {"title": "Review ready", "slot": "chat-1-100"})
        assert note["slot"] == "chat-2-200"
        assert note["history_key"] == "dashboard:chat-2-200"

    @pytest.mark.asyncio
    async def test_a_cron_tab_links_to_its_bound_transcript(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        run = _cron_run(state, _open(state, "chat-1-100"))
        _open_cron_tab(state, monkeypatch)
        note = await _push(state, ATTESTED, run, {"title": "Nightly done"})
        assert note["slot"] == "cron-abc"
        assert note["history_key"] == "cron:abc"

    @pytest.mark.asyncio
    async def test_an_unattested_caller_gets_no_link(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200")
        note = await _push(state, {}, worker, {"title": "Review ready"})
        assert "slot" not in note
        assert "history_key" not in note

    @pytest.mark.asyncio
    async def test_a_closed_publishing_session_resumes_its_messages(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
        state = _make_chat_state(tmp_path)
        worker = _open(state, "chat-2-200")
        history_key = "dashboard:chat-2-200"
        state.conversation_log.append(history_key, "user", "question")
        state.conversation_log.append(history_key, "assistant", "answer")

        note = await _push(state, ATTESTED, worker, {"title": "t"})
        assert note["slot"] == "chat-2-200"
        assert note["history_key"] == history_key
        state._slots.pop("chat-2-200")

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                f"/api/chat/slots/{history_key}/resume",
                json={"name": history_key, "key": history_key, "title": "t"},
            )
            assert response.status == 200
            body = await response.json()

        assert body["key"] == "chat-2-200"
        assert [message["content"] for message in body["messages"]] == ["question", "answer"]


async def _send_message_fallback(
    state: DashboardState, attribution_session: str, options: list[str] | None = None
) -> None:
    await proactive_send._deliver_send_message_fallback(
        state,
        {},
        proactive_send._SendMessageOutcome(),
        text="Done, come look",
        title="t",
        blocks=None,
        options=options or [],
        target_channel="",
        target_user="",
        thread_ts=None,
        reply_broadcast=None,
        target_session="",
        job_name=None,
        channel_target="",
        channel_type="",
        caller_session="",
        declared_session=attribution_session,
        attribution_session=attribution_session,
        is_cron_caller=False,
        send_to_slack=False,
    )
    # The note's append is queued on the single notification executor. Waiting for it
    # keeps the write inside this test's patched config_dir, and the bound makes a stuck
    # executor fail here by name instead of hanging the worker.
    persist = state.last_notification_persist
    if persist is not None:
        await asyncio.wait_for(persist, timeout=30)


class TestSendMessageBellFallback:
    @pytest.mark.asyncio
    async def test_the_bell_copy_links_to_the_calling_session(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200")
        await _send_message_fallback(state, worker)
        assert state._notification_log[-1]["slot"] == "chat-2-200"
        assert state._notification_log[-1]["history_key"] == "dashboard:chat-2-200"

    @pytest.mark.asyncio
    async def test_a_message_offering_choices_still_links(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200")
        await _send_message_fallback(state, worker, options=["Ship it", "Hold"])
        assert state._notification_log[-1]["slot"] == "chat-2-200"
        assert state._notification_log[-1]["history_key"] == "dashboard:chat-2-200"

    @pytest.mark.asyncio
    async def test_a_caller_with_no_attested_session_gets_no_link(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-2-200")
        await _send_message_fallback(state, "")
        assert "slot" not in state._notification_log[-1]
        assert "history_key" not in state._notification_log[-1]
