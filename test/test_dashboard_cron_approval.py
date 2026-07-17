"""Tests for dashboard cron handler approval_mode and silent fields."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronSchedule
from kiro_crew.dashboard.handlers import api_crons, api_crons_create


class TestCronCreateApprovalMode:
    def _make_request(self, body: dict) -> MagicMock:
        mock_state = MagicMock()
        mock_state.has_slot.return_value = False
        mock_job = MagicMock()
        mock_job.id = "abc"
        mock_job.agent_id = ""
        mock_job.approval_mode = ""
        mock_job.silent = False
        mock_state.crons.add_job.return_value = mock_job
        request = MagicMock()
        request.app = {"state": mock_state}
        request.json = AsyncMock(return_value=body)
        return request

    @pytest.mark.asyncio
    async def test_valid_approval_mode_auto(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "approval_mode": "auto"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        job = request.app["state"].crons.add_job.return_value
        assert job.approval_mode == "auto"

    @pytest.mark.asyncio
    async def test_invalid_approval_mode_rejected(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "approval_mode": "evil"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        request.app["state"].crons.add_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_silent_flag_set(self):
        request = self._make_request({"name": "t", "message": "m", "every": 300, "silent": True})
        resp = await api_crons_create(request)
        assert resp.status == 200
        job = request.app["state"].crons.add_job.return_value
        assert job.silent is True

    @pytest.mark.asyncio
    async def test_no_approval_mode_accepted(self):
        request = self._make_request({"name": "t", "message": "m", "every": 300})
        resp = await api_crons_create(request)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_null_agent_does_not_crash(self):
        """JSON null for 'agent' is coerced to empty string, not AttributeError on .strip()."""
        request = self._make_request({"name": "t", "message": "m", "every": 300, "agent": None})
        resp = await api_crons_create(request)
        assert resp.status == 200


class TestCronCreateModel:
    """Test model validation on cron create (dashboard handler)."""

    def _make_request(self, body: dict) -> MagicMock:
        mock_state = MagicMock()
        mock_state.has_slot.return_value = False
        mock_job = MagicMock()
        mock_job.id = "abc"
        mock_job.agent_id = ""
        mock_job.approval_mode = ""
        mock_job.silent = False
        mock_job.model = ""
        mock_state.crons.add_job.return_value = mock_job
        request = MagicMock()
        request.app = {"state": mock_state}
        request.json = AsyncMock(return_value=body)
        return request

    @pytest.mark.asyncio
    async def test_valid_model_accepted(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "model": "sonnet"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        job = request.app["state"].crons.add_job.return_value
        assert job.model != ""

    @pytest.mark.asyncio
    async def test_empty_model_accepted(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "model": ""}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_invalid_model_format_rejected(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "model": "../../etc/passwd"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        body = json.loads(resp.body)
        assert "invalid model format" in body["error"]

    @pytest.mark.asyncio
    async def test_unknown_model_rejected(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "model": "nonexistent-model-xyz"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        body = json.loads(resp.body)
        assert "unknown model" in body["error"]


class TestCronListFields:
    @pytest.mark.asyncio
    async def test_response_includes_approval_mode_and_silent(self):
        mock_job = MagicMock()
        mock_job.id = "j1"
        mock_job.name = "test"
        mock_job.message = "msg"
        mock_job.enabled = True
        mock_job.last_status = "ok"
        mock_job.agent_id = ""
        mock_job.channel = "C123"
        mock_job.approval_mode = "auto"
        mock_job.silent = True
        mock_job.strict_schedule = False
        mock_job.hide_in_chat = False
        mock_job.schedule = CronSchedule(kind="every", every_secs=300)
        mock_job.last_run_ts = None
        mock_job.last_result = None
        mock_job.created_ts = None
        mock_job.timezone = ""
        mock_job.skip_dates = []
        mock_job.script = ""
        mock_job.command = ""
        mock_job.last_error = ""
        mock_job.model = ""

        mock_state = MagicMock()
        mock_state.has_slot.return_value = False
        mock_state.crons.list_jobs.return_value = [mock_job]
        mock_state.crons.running_since.return_value = None
        mock_state.crons.is_running.return_value = False

        request = MagicMock()
        request.app = {"state": mock_state}

        resp = await api_crons(request)

        data = json.loads(resp.body)
        job_data = data["jobs"][0]
        assert job_data["approval_mode"] == "auto"
        assert job_data["silent"] is True
        assert job_data["hide_in_chat"] is False
        assert job_data["channel"] == "C123"
        assert job_data["skip_dates"] is None
        # server_tz top-level field exposes the dashboard's local TZ for client rendering
        assert "server_tz" in data
