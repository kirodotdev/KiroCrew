"""Tests for cron skip_dates and timezone feature."""

from __future__ import annotations

import time
import uuid
from calendar import timegm
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron import (
    _MAX_SKIP_DATE_HORIZON_SECS,
    CronJob,
    CronSchedule,
    CronService,
    compute_next_run_ts,
)


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Every test in this module exercises cron field handling, not authorization.

    ``mcp_cron`` refuses a write from a caller it cannot name, so this states the
    precondition these tests always assumed. See the ``named_cron_caller``
    fixture in ``test/conftest.py``.
    """


class TestIsDueSkipDates:
    """Unit tests for _is_due() skip_dates logic."""

    def _make_cron_job(self, **kwargs) -> CronJob:
        defaults = dict(
            id="test-123",
            name="test",
            message="hello",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            created_ts=time.time() - 3600,
        )
        defaults.update(kwargs)
        return CronJob(**defaults)

    def test_no_skip_dates_fires_normally(self) -> None:
        job = self._make_cron_job()
        now = time.time()
        assert CronService._is_due(job, now)

    def test_skip_today_blocks_firing(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        job = self._make_cron_job(skip_dates=[today], timezone="UTC")
        now = time.time()
        assert not CronService._is_due(job, now)

    def test_skip_other_date_fires_normally(self) -> None:
        job = self._make_cron_job(skip_dates=["2099-12-25"], timezone="UTC")
        now = time.time()
        assert CronService._is_due(job, now)

    def test_skip_dates_uses_job_timezone(self) -> None:
        """A date that is 'today' in one timezone but not another."""
        # Use a timezone far ahead of UTC so the local date may differ
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Pacific/Auckland")  # UTC+12/+13
        local_today = datetime.now(tz).strftime("%Y-%m-%d")

        job = self._make_cron_job(
            skip_dates=[local_today], timezone="Pacific/Auckland"
        )
        now = time.time()
        # Should be skipped because we're checking in Auckland's timezone
        assert not CronService._is_due(job, now)

    def test_skip_dates_falls_back_to_global_config_tz(self) -> None:
        """When job.timezone is empty, falls back to global config."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        job = self._make_cron_job(skip_dates=[today], timezone="")

        with patch("kiro_crew.cron.KiroCrewConfig.load") as mock_cfg:
            mock_cfg.return_value.timezone = "UTC"
            assert not CronService._is_due(job, time.time())

    def test_skip_dates_empty_list_fires(self) -> None:
        job = self._make_cron_job(skip_dates=[])
        assert CronService._is_due(job, time.time())

    def test_skip_dates_uses_now_parameter_not_wall_clock(self) -> None:
        """skip_dates check should use the now parameter, not datetime.now()."""
        # Synthetic now: 2026-04-06 12:00 UTC
        synthetic_now = timegm((2026, 4, 6, 12, 0, 0, 0, 0, 0))
        job = self._make_cron_job(
            skip_dates=["2026-04-06"], timezone="UTC",
            created_ts=synthetic_now - 3600,
        )
        assert not CronService._is_due(job, synthetic_now)

    def test_last_run_ts_not_updated_on_skip(self, tmp_path: Path) -> None:
        """Skipped jobs should not update last_run_ts."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="test", message="hello", cron_expr="* * * * *")
        job.skip_dates = [today]
        job.timezone = "UTC"
        svc._save()

        original_ts = job.last_run_ts
        assert not CronService._is_due(job, time.time())
        assert job.last_run_ts == original_ts


class TestIsDueSkipWithEverySchedule:
    """Skip dates apply to all schedule types including 'every'."""

    def test_every_schedule_skipped(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        job = CronJob(
            id="test-every",
            name="test",
            message="hello",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=time.time() - 120,  # due
            skip_dates=[today],
            timezone="UTC",
        )
        assert not CronService._is_due(job, time.time())

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        job = CronJob(
            id="test-badtz",
            name="test",
            message="hello",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            created_ts=time.time() - 3600,
            skip_dates=[today],
            timezone="NotATimezone",
        )
        # Should not crash; falls back to UTC and still skips
        assert not CronService._is_due(job, time.time())


class TestMcpCronSkipDates:
    """Integration tests for skip_dates via MCP tool layer."""

    def test_cron_add_with_skip_dates(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.mcp_cron import _call_tool

        # Isolate the cron store to tmp_path so parallel (xdist) cron tests don't
        # race on the shared default jobs file. _call_tool builds its service from
        # config_dir(), so point that at tmp_path too.
        monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"skip-test-{uuid.uuid4().hex[:8]}"

        result = _call_tool(
            "cron_add",
            {
                "name": name,
                "message": "hello",
                "cron_expr": "0 9 * * 1-5",
                "skip_dates": ["2026-04-06", "2026-12-25"],
                "timezone": "Europe/Luxembourg",
            },
        )
        assert "Added job" in result

        jobs = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name]
        assert len(jobs) == 1
        assert jobs[0].skip_dates == ["2026-04-06", "2026-12-25"]
        assert jobs[0].timezone == "Europe/Luxembourg"

    def test_cron_update_skip_dates(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.mcp_cron import _call_tool

        monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"update-skip-{uuid.uuid4().hex[:8]}"

        result = _call_tool(
            "cron_add",
            {"name": name, "message": "hello", "cron_expr": "0 9 * * *"},
        )
        assert "Added job" in result

        jobs = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name]
        job_id = jobs[0].id

        result = _call_tool(
            "cron_update",
            {
                "job_id": job_id,
                "skip_dates": ["2026-05-01"],
                "timezone": "Europe/Luxembourg",
            },
        )
        assert "Updated" in result

        jobs = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.id == job_id]
        assert jobs[0].skip_dates == ["2026-05-01"]
        assert jobs[0].timezone == "Europe/Luxembourg"


class TestComputeNextRunTsSkipDates:
    """Unit tests for compute_next_run_ts advancing past skip_dates."""

    def _make_cron_job(self, **kwargs) -> CronJob:
        defaults = dict(
            id="test-next-run",
            name="test",
            message="hello",
            schedule=CronSchedule(kind="cron", cron_expr="0 9 * * 5"),  # Fridays 9AM
            created_ts=time.time() - 3600,
            timezone="UTC",
        )
        defaults.update(kwargs)
        return CronJob(**defaults)

    def test_no_skip_dates_returns_next_cron_match(self) -> None:

        job = self._make_cron_job(skip_dates=[])
        result = compute_next_run_ts(job)
        assert result is not None

    def test_skip_dates_advances_past_skipped_friday(self) -> None:

        # Fix "now" to Thursday 2026-05-28 12:00 UTC so next Friday = 2026-05-29
        synthetic_now = timegm((2026, 5, 28, 12, 0, 0, 0, 0, 0))
        job = self._make_cron_job(
            skip_dates=["2026-05-29"],  # skip the immediate next Friday
            created_ts=synthetic_now - 3600,
        )
        result = compute_next_run_ts(job, now=synthetic_now)
        assert result is not None
        # Should skip May 29 and return June 5
        result_date = datetime.fromtimestamp(result, tz=timezone.utc).strftime("%Y-%m-%d")
        assert result_date == "2026-06-05"

    def test_all_dates_skipped_returns_none(self) -> None:

        # The binding constraint is the ~2y wall-clock horizon, not the absolute
        # iteration ceiling. Skip every Friday from the first fire through past
        # the horizon so compute_next_run_ts exhausts the horizon and returns
        # None. (Iterating _MAX_SKIP_DATE_LOOKAHEAD weeks would overflow datetime
        # past year 9999 -- the horizon is what actually bounds the search.)
        base_date = datetime(2026, 5, 29, tzinfo=timezone.utc)  # first Friday
        horizon_weeks = _MAX_SKIP_DATE_HORIZON_SECS // (7 * 24 * 3600)
        skip_dates = [
            (base_date + timedelta(weeks=i)).strftime("%Y-%m-%d")
            for i in range(horizon_weeks + 4)  # cover the horizon plus a margin
        ]
        synthetic_now = timegm((2026, 5, 28, 12, 0, 0, 0, 0, 0))
        job = self._make_cron_job(
            skip_dates=skip_dates,
            created_ts=synthetic_now - 3600,
        )
        result = compute_next_run_ts(job, now=synthetic_now)
        assert result is None

    def test_multiple_consecutive_skips(self) -> None:

        synthetic_now = timegm((2026, 5, 28, 12, 0, 0, 0, 0, 0))
        job = self._make_cron_job(
            skip_dates=["2026-05-29", "2026-06-05", "2026-06-12"],
            created_ts=synthetic_now - 3600,
        )
        result = compute_next_run_ts(job, now=synthetic_now)
        assert result is not None
        result_date = datetime.fromtimestamp(result, tz=timezone.utc).strftime("%Y-%m-%d")
        assert result_date == "2026-06-19"
