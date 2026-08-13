"""Tests for _handle_cron_command next_run display in Slack keyword handler."""
from __future__ import annotations

import asyncio
import re
import time
from unittest.mock import patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.slack.handler import _handle_cron_command


@pytest.fixture()
def cron_service(tmp_path):
    return CronService(base_dir=tmp_path)


def _make_job(*, enabled: bool = True, last_status: str = "ok") -> CronJob:
    job = CronJob(
        id="abc123",
        name="test-job",
        message="do something important",
        schedule=CronSchedule(kind="cron", cron_expr="0 13 * * *"),
        enabled=enabled,
    )
    job.last_status = last_status
    return job


class TestHandleCronListNextRun:
    """Verify cron list keyword includes next run info."""

    def test_includes_next_run(self, cron_service: CronService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=now + 7200):
            result = asyncio.run(_handle_cron_command("cron list", cron_service, "C123", "t123"))
        assert result is not None
        assert "⏭ in" in result
        assert re.search(r"⏭ in \d+h", result)

    def test_no_next_run_for_disabled(self, cron_service: CronService) -> None:
        cron_service._jobs = [_make_job(enabled=False)]
        with patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=None):
            result = asyncio.run(_handle_cron_command("cron list", cron_service, "C123", "t123"))
        assert result is not None
        assert "⏭" not in result

    def test_next_run_days(self, cron_service: CronService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=now + 3 * 86400 + 7200):
            result = asyncio.run(_handle_cron_command("cron list", cron_service, "C123", "t123"))
        assert result is not None
        assert "⏭ in 3d" in result

    def test_next_run_minutes(self, cron_service: CronService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=now + 1800):
            result = asyncio.run(_handle_cron_command("cron list", cron_service, "C123", "t123"))
        assert result is not None
        assert "⏭ in" in result
        assert re.search(r"⏭ in \d+m", result)

    def test_next_run_less_than_one_minute(self, cron_service: CronService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=now + 30):
            result = asyncio.run(_handle_cron_command("cron list", cron_service, "C123", "t123"))
        assert result is not None
        assert "⏭ in <1m" in result

    def test_next_run_past_due(self, cron_service: CronService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=now - 5):
            result = asyncio.run(_handle_cron_command("cron list", cron_service, "C123", "t123"))
        assert result is not None
        assert "⏭ now" in result

    def test_message_is_redacted(self, cron_service: CronService) -> None:
        job = _make_job()
        job.message = "token=AKIAIOSFODNN7EXAMPLE"
        cron_service._jobs = [job]
        with patch("kiro_crew.slack.handler.compute_next_run_ts", return_value=None), \
             patch("kiro_crew.slack.handler.redact_exfiltration_urls",
                   return_value=("[URL_REDACTED]", True)) as mock_url, \
             patch("kiro_crew.slack.handler.redact_credentials",
                   return_value=("[REDACTED]", True)) as mock_cred:
            result = asyncio.run(_handle_cron_command("cron list", cron_service, "C123", "t123"))
        mock_url.assert_any_call(job.message)
        mock_cred.assert_any_call("[URL_REDACTED]")
        assert result is not None
        assert "[REDACTED]" in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_remove_all_redacts_name(self, cron_service: CronService) -> None:
        # `cron remove all` echoes each removed job's name to Slack (and the
        # persisted conversation log); j.name is free-form user/LLM-supplied,
        # so it must be redacted like j.message is in the `cron list` branch.
        job = _make_job()
        job.name = "token=AKIAIOSFODNN7EXAMPLE"
        cron_service._jobs = [job]
        with patch("kiro_crew.slack.handler.redact_exfiltration_urls",
                   return_value=("[URL_REDACTED]", True)) as mock_url, \
             patch("kiro_crew.slack.handler.redact_credentials",
                   return_value=("[REDACTED]", True)) as mock_cred:
            result = asyncio.run(_handle_cron_command("cron remove all", cron_service, "C123", "t123"))
        mock_url.assert_any_call(job.name)
        mock_cred.assert_any_call("[URL_REDACTED]")
        assert result is not None
        assert "[REDACTED]" in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert not cron_service.list_jobs(include_disabled=True)  # jobs still removed
