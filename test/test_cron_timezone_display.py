"""Tests for per-job timezone display in cron list outputs."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from kiro_crew.cron import CronJob, CronSchedule, CronService, format_schedule
from kiro_crew.mcp_cron import _call_tool
from kiro_crew.slack.handler import _handle_cron_command

#: The session the MCP ``cron_list`` tests call as, and the owner stamped on every
#: fixture job. ``cron_list`` scopes to the caller's own rows, so a job left at the
#: default ``session_key=""`` is not listed and a timezone-FORMAT assertion would
#: read "No cron jobs." instead. See test/test_mcp_cron_caller_identity.py for the
#: scope rule; nothing here is about authorization.
_TZ_OWNER = "dashboard:tz-format-slot"


def _make_job(timezone: str = "", **kwargs) -> CronJob:
    defaults = dict(
        id="tz-test-1",
        name="tz-job",
        message="hello",
        schedule=CronSchedule(kind="cron", cron_expr="0 9 * * 1"),
        created_ts=time.time() - 3600,
        timezone=timezone,
        session_key=_TZ_OWNER,
    )
    defaults.update(kwargs)
    return CronJob(**defaults)


class TestFormatScheduleTimezone:
    """format_schedule uses the provided tz_name for display."""

    def test_utc_timezone_shows_utc(self) -> None:
        sched = CronSchedule(kind="cron", cron_expr="0 9 * * 1")
        result = format_schedule(sched, tz_name="UTC")
        assert "UTC" in result

    def test_specific_timezone_shows_that_timezone(self) -> None:
        sched = CronSchedule(kind="cron", cron_expr="0 9 * * 1")
        result = format_schedule(sched, tz_name="Asia/Tokyo")
        assert "JST" in result or "Tokyo" in result or "Asia" in result


class TestDashboardCronsTimezone:
    """GET /api/crons uses job.timezone for schedule display and returns timezone field."""

    @pytest.mark.asyncio
    async def test_api_crons_uses_job_timezone(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard.handlers.cron import api_crons

        job = _make_job(timezone="UTC")
        mock_state = MagicMock()
        mock_state.has_slot.return_value = False
        mock_state.crons.list_jobs.return_value = [job]
        mock_state.crons.list_jobs_async = AsyncMock(return_value=[job])
        mock_state.crons.is_running.return_value = False
        mock_state.crons.running_since.return_value = None

        request = MagicMock()
        request.app = {"state": mock_state}

        with patch(
            "kiro_crew.cron.get_local_tz",
            return_value=("America/New_York", ZoneInfo("America/New_York")),
        ):
            resp = await api_crons(request)

        import json

        data = json.loads(resp.body)
        assert "UTC" in data["jobs"][0]["schedule"]
        assert data["jobs"][0]["timezone"] == "UTC"

    @pytest.mark.asyncio
    async def test_api_crons_falls_back_to_server_tz(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard.handlers.cron import api_crons

        job = _make_job(timezone="")
        mock_state = MagicMock()
        mock_state.has_slot.return_value = False
        mock_state.crons.list_jobs.return_value = [job]
        mock_state.crons.list_jobs_async = AsyncMock(return_value=[job])
        mock_state.crons.is_running.return_value = False
        mock_state.crons.running_since.return_value = None

        request = MagicMock()
        request.app = {"state": mock_state}

        with patch(
            "kiro_crew.cron.get_local_tz",
            return_value=("America/New_York", ZoneInfo("America/New_York")),
        ):
            resp = await api_crons(request)

        import json

        data = json.loads(resp.body)
        # Should use server TZ (EDT/EST) not UTC
        assert "EDT" in data["jobs"][0]["schedule"] or "EST" in data["jobs"][0]["schedule"]
        assert data["jobs"][0]["timezone"] is None


class TestMcpCronListTimezone:
    """cron_list MCP tool uses job.timezone for schedule display."""

    @pytest.fixture(autouse=True)
    def _calls_as_the_fixture_owner(self, monkeypatch) -> None:
        """Call as the session ``_make_job`` stamps as owner; see _TZ_OWNER."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", _TZ_OWNER)
        monkeypatch.delenv("KIROCREW_CLI", raising=False)

    def test_cron_list_uses_job_timezone(self, tmp_path) -> None:
        job = _make_job(timezone="UTC")
        mock_tz = ("America/New_York", ZoneInfo("America/New_York"))
        with patch("kiro_crew.mcp_cron.CronService") as mock_cls, \
                patch("kiro_crew.mcp_cron.get_local_tz", return_value=mock_tz):
            mock_cls.return_value.list_jobs.return_value = [job]
            result = _call_tool("cron_list", {})
        assert "UTC" in result

    def test_cron_list_falls_back_to_local_tz(self, tmp_path) -> None:
        job = _make_job(timezone="")
        mock_tz = ("America/New_York", ZoneInfo("America/New_York"))
        with patch("kiro_crew.mcp_cron.CronService") as mock_cls, \
                patch("kiro_crew.mcp_cron.get_local_tz", return_value=mock_tz):
            mock_cls.return_value.list_jobs.return_value = [job]
            result = _call_tool("cron_list", {})
        assert "EDT" in result or "EST" in result


class TestSlackCronListTimezone:
    """Slack cron list keyword uses job.timezone for display."""

    def test_uses_job_timezone(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._jobs = [_make_job(timezone="UTC")]
        with patch(
            "kiro_crew.slack.handler.get_local_tz",
            return_value=("America/New_York", ZoneInfo("America/New_York")),
        ), patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=None):
            result = asyncio.run(_handle_cron_command("cron list", svc, "C123", "t123"))
        assert result is not None
        assert "UTC" in result

    def test_falls_back_to_local_tz(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._jobs = [_make_job(timezone="")]
        with patch(
            "kiro_crew.slack.handler.get_local_tz",
            return_value=("America/New_York", ZoneInfo("America/New_York")),
        ), patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=None):
            result = asyncio.run(_handle_cron_command("cron list", svc, "C123", "t123"))
        assert result is not None
        assert "EDT" in result or "EST" in result
