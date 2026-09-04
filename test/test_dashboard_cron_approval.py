"""Tests for dashboard cron handler approval_mode and silent fields."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from body_stream_helpers import attach_body

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
        mock_state.crons.add_job_async = AsyncMock(return_value=mock_job)
        request = MagicMock()
        request.app = {"state": mock_state}
        attach_body(request, body)
        return request

    @pytest.mark.asyncio
    async def test_valid_approval_mode_auto(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "approval_mode": "auto"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.add_job_async.call_args
        assert kwargs.get("approval_mode") == "auto"

    @pytest.mark.asyncio
    async def test_invalid_approval_mode_rejected(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "approval_mode": "evil"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        request.app["state"].crons.add_job_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_silent_flag_set(self):
        request = self._make_request({"name": "t", "message": "m", "every": 300, "silent": True})
        resp = await api_crons_create(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.add_job_async.call_args
        assert kwargs.get("silent") is True

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


class TestCronCreateTimezonePersistenceOwner:
    """Arbiter item 2 / Design finding 2: the dashboard create path folded
    ``timezone`` AFTER ``add_job`` via ``job.timezone = ...`` + a SECOND
    ``_save()`` — leaving a crash/concurrent-read window where a job persists
    WITHOUT its timezone, the exact hole ``add_job``'s docstring claims to
    close. The dashboard caller must instead pass ``timezone`` THROUGH the
    create call so it lands in the single first ``_save()``.

    Post-rebase over PR #331: the create path is now the event-loop-safe
    ``add_job_async`` (single locked build+persist, all fields folded in) — so
    the same intent is asserted against ``add_job_async`` and the absence of any
    handler-side ``_save()``.
    """

    def _make_request(self, body: dict):
        mock_state = MagicMock()
        mock_state.has_slot.return_value = False
        mock_job = MagicMock()
        mock_job.id = "abc"
        mock_state.crons.add_job_async = AsyncMock(return_value=mock_job)
        request = MagicMock()
        request.app = {"state": mock_state}
        attach_body(request, body)
        return request, mock_state

    @pytest.mark.asyncio
    async def test_timezone_passed_through_add_job_every(self):
        request, state = self._make_request(
            {"name": "t", "message": "m", "every": 300, "timezone": "America/New_York"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        # timezone reaches the create call (folded into the single first _save),
        # NOT a post-hoc job.timezone assignment.
        _, kwargs = state.crons.add_job_async.call_args
        assert kwargs.get("timezone") == "America/New_York"

    @pytest.mark.asyncio
    async def test_timezone_passed_through_add_job_cron_expr(self):
        request, state = self._make_request(
            {"name": "t", "message": "m", "cron": "0 9 * * *", "timezone": "Europe/London"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        _, kwargs = state.crons.add_job_async.call_args
        assert kwargs.get("timezone") == "Europe/London"

    @pytest.mark.asyncio
    async def test_no_post_hoc_timezone_only_save_not_called_for_tz_alone(self):
        """When timezone is the ONLY non-schedule field, the handler must not
        fire a post-hoc ``_save()`` (timezone is folded into the single locked
        ``add_job_async`` build+persist)."""
        request, state = self._make_request(
            {"name": "t", "message": "m", "every": 300, "timezone": "UTC"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        state.crons._save.assert_not_called()


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
        mock_state.crons.add_job_async = AsyncMock(return_value=mock_job)
        request = MagicMock()
        request.app = {"state": mock_state}
        attach_body(request, body)
        return request

    @pytest.mark.asyncio
    async def test_valid_model_accepted(self):
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "model": "sonnet"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.add_job_async.call_args
        assert kwargs.get("model", "") != ""

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
    async def test_malformed_model_rejected(self):
        # A value that violates _MODEL_NAME_RE (contains a space and "!") is
        # still rejected by the FORMAT gate — that gate is retained.
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "model": "bad model!"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        body = json.loads(resp.body)
        assert "invalid model format" in body["error"]

    @pytest.mark.asyncio
    async def test_arbitrary_kiro_model_accepted(self):
        # There is no membership gate against the claude_code registry: the
        # model dropdown is sourced from the live kiro-cli --list-models, so an
        # arbitrary well-formed kiro id (not in the claude_code family) is
        # accepted and persisted verbatim. Matches the chat model path.
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "model": "glm-4.7"}
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.add_job_async.call_args
        assert kwargs.get("model") == "glm-4.7"

    @pytest.mark.asyncio
    async def test_non_string_model_rejected(self):
        # A numeric/bool JSON `model` must be rejected as a clean 400, not raise
        # AttributeError on .strip() and leak an HTTP 500.
        request = self._make_request(
            {"name": "t", "message": "m", "every": 300, "model": 123}
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        body = json.loads(resp.body)
        assert "invalid model format" in body["error"]


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
        mock_job.folder_id = ""
        mock_job.session_key = ""

        mock_state = MagicMock()
        mock_state.has_slot.return_value = False
        mock_state.crons.list_jobs.return_value = [mock_job]
        mock_state.crons.list_jobs_async = AsyncMock(return_value=[mock_job])
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
