"""Tests for the dashboard cron HTTP handlers wiring `hide_in_chat`.

Covers the create (`POST /api/crons`) and update (`PATCH /api/crons/{id}`) paths
that copy the request body into the job — the exact path the frontend "Hide in
chat" toggle exercises. The CronService-layer behavior is covered separately in
test_cron_hide_in_chat.py; these tests lock the HTTP body->job wiring so a
renamed key or a dropped branch fails CI (Coverlay flagged these handler lines
as uncovered).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons_create


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


class TestCronCreateHideInChat:
    """POST /api/crons must persist hide_in_chat from the body onto the job."""

    @pytest.mark.asyncio
    async def test_create_with_hide_in_chat_true_persists(self):
        crons = CronService()
        request = _create_request(
            {"name": "digest", "message": "summarize", "every": 86400, "hide_in_chat": True},
            crons,
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        jobs = crons.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].hide_in_chat is True

    @pytest.mark.asyncio
    async def test_create_without_hide_in_chat_defaults_false(self):
        crons = CronService()
        request = _create_request(
            {"name": "chatty", "message": "talk", "every": 3600},
            crons,
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        assert crons.list_jobs()[0].hide_in_chat is False

    @pytest.mark.asyncio
    async def test_create_with_hide_in_chat_false_stays_false(self):
        crons = CronService()
        request = _create_request(
            {"name": "shown", "message": "talk", "every": 3600, "hide_in_chat": False},
            crons,
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        assert crons.list_jobs()[0].hide_in_chat is False


class TestCronUpdateHideInChat:
    """PATCH /api/crons/{id} must forward hide_in_chat to update_job_async."""

    def _update_request(self, body: dict, job_id: str = "abc123") -> MagicMock:
        state = MagicMock()
        mock_job = MagicMock()
        mock_job.id = job_id
        state.crons.update_job_async = AsyncMock(return_value=mock_job)
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"job_id": job_id}
        attach_body(request, body)
        return request

    @pytest.mark.asyncio
    async def test_update_forwards_hide_in_chat_true(self):
        request = self._update_request({"hide_in_chat": True})
        resp = await api_cron_update(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("hide_in_chat") is True

    @pytest.mark.asyncio
    async def test_update_forwards_hide_in_chat_false(self):
        request = self._update_request({"hide_in_chat": False})
        resp = await api_cron_update(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("hide_in_chat") is False
