"""Tests for api_cron_to_chat HTTP handler (handlers/cron.py L169-L205)."""

from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.cron import api_cron_to_chat


def _make_app(state):
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/crons/{job_id}/to-chat", api_cron_to_chat)
    return app


def _make_state(jobs=None, history_messages=None, notifications=None):
    state = MagicMock()
    slots = {}

    def get_or_create_slot(name=None, agent="", origin=""):
        # ``origin`` is recorded, not just tolerated: the cron paths must
        # declare SlotOrigin.CRON, and a fake that swallowed the kwarg
        # would let that regress silently (a cron slot relabelled USER is
        # readable by any app holding `slots:user`).
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot._origin = origin
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""

            def append(role, content, cls, broadcast=True):
                slot.messages.append({"role": role, "content": content, "cls": cls})

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.crons = MagicMock()
    state.crons.list_jobs.return_value = jobs or []
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state._notification_log = notifications or []
    state.push_slots_update = MagicMock()
    state.has_slot = MagicMock(return_value=False)
    return state


def _make_job(job_id="abc123", name="test-cron", last_result="Hello world"):
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.last_result = last_result
    job.agent_id = ""
    return job


class TestApiCronToChat:
    """HTTP handler tests for POST /api/crons/{job_id}/to-chat."""

    @pytest.mark.asyncio
    async def test_existing_job_injects_result(self):
        job = _make_job()
        state = _make_state(jobs=[job])
        with patch(
            "kiro_crew.dashboard.handlers.cron.inject_cron_result_to_dashboard"
        ) as mock_inject:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/crons/abc123/to-chat")
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert data["slot"] == "cron-abc123"
                mock_inject.assert_called_once_with(state, job, "Hello world", history=ANY)

    @pytest.mark.asyncio
    async def test_deleted_job_with_history_creates_slot(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        state = _make_state(history_messages=history)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/deleted123/to-chat")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            slot = state.get_or_create_slot(name="cron-deleted123")
            assert slot.linked_session_key == "cron:deleted123"
            assert len(slot.messages) == 2

    @pytest.mark.asyncio
    async def test_deleted_job_no_history_uses_notification(self):
        notifications = [{"job_id": "notif123", "body": "Cron completed successfully"}]
        state = _make_state(notifications=notifications)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/notif123/to-chat")
            assert resp.status == 200
            slot = state.get_or_create_slot(name="cron-notif123")
            assert len(slot.messages) == 1
            assert "Cron completed successfully" in slot.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_deleted_job_no_history_no_notification_returns_404(self):
        state = _make_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/missing999/to-chat")
            assert resp.status == 404
            data = await resp.json()
            assert "not found" in data["error"]

    @pytest.mark.asyncio
    async def test_notification_dedup_prevents_duplicate(self):
        notifications = [{"job_id": "dup123", "body": "Result text"}]
        state = _make_state(notifications=notifications)
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/crons/dup123/to-chat")
            await client.post("/api/crons/dup123/to-chat")
            slot = state.get_or_create_slot(name="cron-dup123")
            assert len(slot.messages) == 1
