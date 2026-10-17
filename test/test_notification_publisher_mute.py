"""A muted session's own bell notes are delivered silenced.

The dashboard mutes a session in two ways (its own ``muted`` row flag, and
``mutes_opened`` on a session up its ``created_by`` chain). A note that session
publishes through the agent routes is delivered like a note on a muted channel
(``silenced`` and passive), unless it asks for the user (critical) or is an
approval. The decision is made at the delivery sink from the attested publishing
session, never from the note's text.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers.messaging import api_notification_agent_push
from kiro_crew.dashboard.messaging_api import proactive_send
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.notifications.bus import (
    NotificationPayload,
    NotificationValidationError,
    payload_from_legacy,
)
from kiro_crew.testing.wait import async_wait_until


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _open(
    state: DashboardState,
    key: str,
    *,
    created_by: str = "",
    muted: bool = False,
    mutes_opened: bool = False,
) -> str:
    """Register a live slot and return the session key a caller presents for it."""
    slot = _ChatSlot(key)
    slot._created_by = created_by
    slot.muted = muted
    slot.mutes_opened = mutes_opened
    state._slots[key] = slot
    return effective_session_key(slot)


def _publish(state: DashboardState, session: str | None, **over) -> dict:
    fields = dict(
        source="system",
        channel="system.agent",
        title="Build finished",
        body="42 rows",
        publisher_session=session,
    )
    fields.update(over)
    return state.notification_bus.push(NotificationPayload(**fields))


class TestSessionIsMuted:
    # Imported here, not at module level, so the route and sink cases below stay
    # collectable on a tree that does not have the predicate yet.
    @staticmethod
    def _muted(state: DashboardState, session: str) -> bool:
        from kiro_crew.dashboard.notification_coordinator import session_is_muted

        return session_is_muted(state, session)

    def test_a_sessions_own_row_mute_mutes_it(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-1-100", muted=True)
        assert self._muted(state, worker)

    def test_a_creator_that_mutes_what_it_opens_mutes_the_worker(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", mutes_opened=True)
        worker = _open(state, "chat-2-200", created_by="chat-1-100")
        assert self._muted(state, worker)

    def test_the_flag_reaches_a_worker_several_creators_down(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", mutes_opened=True)
        _open(state, "chat-2-200", created_by="chat-1-100")
        worker = _open(state, "chat-3-300", created_by="chat-2-200")
        assert self._muted(state, worker)

    def test_the_creator_itself_is_not_muted_by_its_own_flag(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        conductor = _open(state, "chat-1-100", mutes_opened=True)
        assert not self._muted(state, conductor)

    def test_an_unflagged_chain_is_not_muted(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100")
        worker = _open(state, "chat-2-200", created_by="chat-1-100")
        assert not self._muted(state, worker)

    def test_a_creator_that_is_gone_ends_the_walk_unmuted(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", created_by="chat-1-100")
        assert not self._muted(state, worker)

    def test_a_created_by_cycle_ends_unmuted(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", created_by="chat-2-200")
        worker = _open(state, "chat-2-200", created_by="chat-1-100")
        assert not self._muted(state, worker)

    def test_a_cycle_above_the_session_ends_unmuted(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", created_by="chat-2-200")
        _open(state, "chat-2-200", created_by="chat-1-100")
        worker = _open(state, "chat-3-300", created_by="chat-1-100")
        assert not self._muted(state, worker)

    def test_an_unknown_session_is_not_muted(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", muted=True)
        assert not self._muted(state, "dashboard:chat-9-900")


class TestSinkAppliesThePublishersMute:
    def test_a_note_from_a_muted_worker_is_silenced_and_passive(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", mutes_opened=True)
        worker = _open(state, "chat-2-200", created_by="chat-1-100")
        note = _publish(state, worker)
        assert note["silenced"] is True
        assert note["priority"] == "passive"
        assert state._notification_log[-1]["silenced"] is True
        assert state._unread_count == 0

    def test_a_note_from_an_unmuted_session_is_untouched(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200")
        note = _publish(state, worker)
        assert note["priority"] == "default"
        assert "silenced" not in note
        assert state._unread_count == 1

    def test_a_note_with_no_publishing_session_is_untouched(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", muted=True)
        note = _publish(state, None)
        assert note["priority"] == "default"
        assert "silenced" not in note

    def test_an_approval_from_a_muted_worker_still_notifies(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", muted=True)
        note = _publish(state, worker, channel="system.approval", kind="approval")
        assert note["priority"] == "critical"
        assert "silenced" not in note
        assert state._unread_count == 1

    def test_a_protected_channel_stays_audible_whatever_its_priority(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", muted=True)
        note = _publish(state, worker, channel="system.approval", priority="default")
        assert note["priority"] == "default"
        assert "silenced" not in note

    def test_a_critical_note_from_a_muted_worker_still_notifies(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", muted=True)
        note = _publish(state, worker, priority="critical")
        assert note["priority"] == "critical"
        assert "silenced" not in note
        assert state._unread_count == 1

    def test_a_channel_priority_override_neither_undoes_nor_exempts_the_mute(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.agent", priority="critical")
        worker = _open(state, "chat-2-200", muted=True)
        note = _publish(state, worker)
        assert note["priority"] == "passive"
        assert note["silenced"] is True

    def test_the_publishing_session_is_not_stored_or_broadcast(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", muted=True)
        note = _publish(state, worker)
        assert "publisher_session" not in note
        assert all("publisher_session" not in row for row in state._notification_log)

    def test_meta_cannot_forge_the_publishing_session(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        victim = _open(state, "chat-2-200", muted=True)
        note = state.notification_bus.push(
            NotificationPayload(
                source="system",
                channel="system.agent",
                title="t",
                body="b",
                meta={"publisher_session": victim},
            )
        )
        assert note["priority"] == "default"
        assert "silenced" not in note

    def test_a_malformed_publishing_session_is_rejected(self):
        for bad in ("", 7, ["chat-1-100"]):
            with pytest.raises(NotificationValidationError):
                NotificationPayload(
                    source="system",
                    channel="system.agent",
                    title="t",
                    body="",
                    publisher_session=bad,
                ).validate()

    def test_the_legacy_adapter_carries_the_publishing_session(self):
        payload = payload_from_legacy("agent", "t", "b", publisher_session="dashboard:chat-1-100")
        assert payload.publisher_session == "dashboard:chat-1-100"


ATTESTED = {"peer_verified": True}


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


class TestAgentPushRoute:
    @pytest.mark.asyncio
    async def test_a_workers_note_is_silenced_by_a_creator_that_mutes_what_it_opens(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", mutes_opened=True)
        _open(state, "chat-2-200", created_by="chat-1-100")
        worker = _open(state, "chat-3-300", created_by="chat-2-200")
        async with TestClient(TestServer(_agent_push_app(state, ATTESTED))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Review ready"},
                headers={"X-Session-Key": worker},
            )
            assert resp.status == 200
        row = state._notification_log[-1]
        assert (row["priority"], row.get("silenced")) == ("passive", True)

    @pytest.mark.asyncio
    async def test_the_conductor_that_set_the_mute_is_not_silenced(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        conductor = _open(state, "chat-1-100", mutes_opened=True)
        async with TestClient(TestServer(_agent_push_app(state, ATTESTED))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Goal reached"},
                headers={"X-Session-Key": conductor},
            )
            assert resp.status == 200
        row = state._notification_log[-1]
        assert row["priority"] == "default"
        assert "silenced" not in row

    @pytest.mark.asyncio
    async def test_a_muted_workers_send_notification_is_delivered_silenced(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", mutes_opened=True)
        worker = _open(state, "chat-2-200", created_by="chat-1-100")
        async with TestClient(TestServer(_agent_push_app(state, ATTESTED))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Review ready"},
                headers={"X-Session-Key": worker},
            )
            assert resp.status == 200
            note = (await resp.json())["note"]
        assert note["priority"] == "passive"
        assert note["silenced"] is True
        assert "publisher_session" not in note
        assert state._notification_log[-1]["silenced"] is True

    @pytest.mark.asyncio
    async def test_a_critical_request_from_a_muted_worker_still_rings(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", muted=True)
        async with TestClient(TestServer(_agent_push_app(state, ATTESTED))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Need your decision", "priority": "critical"},
                headers={"X-Session-Key": worker},
            )
            assert resp.status == 200
            note = (await resp.json())["note"]
        assert note["priority"] == "critical"
        assert "silenced" not in note

    @pytest.mark.asyncio
    async def test_an_unattested_caller_cannot_claim_a_session_to_be_silenced(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", muted=True)
        async with TestClient(TestServer(_agent_push_app(state, {}))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Review ready"},
                headers={"X-Session-Key": worker},
            )
            assert resp.status == 200
            note = (await resp.json())["note"]
        assert note["priority"] == "default"
        assert "silenced" not in note

    @pytest.mark.asyncio
    async def test_an_unmuted_workers_note_is_unchanged(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200")
        async with TestClient(TestServer(_agent_push_app(state, ATTESTED))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Review ready"},
                headers={"X-Session-Key": worker},
            )
            assert resp.status == 200
            note = (await resp.json())["note"]
        assert note["priority"] == "default"
        assert "silenced" not in note


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
    # The note's append is queued on the single notification executor, and nothing else in
    # these tests waits for it. A busy executor would run it after ``_make_state``'s patched
    # ``config_dir`` is put back, writing outside the test's ``tmp_path``. Bounded so a
    # stuck executor fails here by name instead of hanging the worker.
    persist = state.last_notification_persist
    if persist is not None:
        await asyncio.wait_for(persist, timeout=30)


class TestSendMessageBellFallback:
    @pytest.mark.asyncio
    async def test_the_helper_waits_for_the_append_a_busy_executor_has_queued(
        self, monkeypatch, tmp_path
    ):
        """The notification executor is held, so the note's append queues behind the hold: the
        helper must still be waiting for that append, and hand back only once it is on disk."""
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", muted=True)
        release = threading.Event()
        busy = dashboard_state._notification_io_executor().submit(release.wait, 60)
        helper = asyncio.ensure_future(_send_message_fallback(state, worker))
        try:
            await async_wait_until(
                lambda: state.last_notification_persist is not None,
                describe=lambda: "the helper never queued the note's append",
            )
            assert not helper.done()
        finally:
            release.set()
        await asyncio.wait_for(helper, timeout=30)
        assert busy.done()
        lines = dashboard_state._notifications_path().read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert '"silenced": true' in lines[0]

    @pytest.mark.asyncio
    async def test_a_muted_workers_fallback_note_is_delivered_silenced(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-1-100", mutes_opened=True)
        worker = _open(state, "chat-2-200", created_by="chat-1-100")
        await _send_message_fallback(state, worker)
        assert state._notification_log[-1]["priority"] == "passive"
        assert state._notification_log[-1]["silenced"] is True
        assert state._unread_count == 0

    @pytest.mark.asyncio
    async def test_a_muted_workers_question_with_choices_still_rings(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200", muted=True)
        await _send_message_fallback(state, worker, options=["Ship it", "Hold"])
        assert state._notification_log[-1]["priority"] == "default"
        assert "silenced" not in state._notification_log[-1]
        assert state._unread_count == 1

    @pytest.mark.asyncio
    async def test_an_unmuted_workers_fallback_note_rings_as_before(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        worker = _open(state, "chat-2-200")
        await _send_message_fallback(state, worker)
        assert state._notification_log[-1]["priority"] == "default"
        assert state._unread_count == 1

    @pytest.mark.asyncio
    async def test_a_caller_with_no_attested_session_rings_as_before(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _open(state, "chat-2-200", muted=True)
        await _send_message_fallback(state, "")
        assert state._notification_log[-1]["priority"] == "default"
        assert state._unread_count == 1
