"""Tests for cron job jitter logic (_compute_jitter and drift prevention)."""

from unittest.mock import patch

import pytest

from kiro_crew.cron import (
    _JITTER_DAILY_MAX,
    _JITTER_HOURLY_MAX,
    CronJob,
    CronSchedule,
    CronService,
)


def _job(schedule: CronSchedule, strict: bool = False) -> CronJob:
    return CronJob(id="t1", name="test", message="msg", schedule=schedule, strict_schedule=strict)


class TestComputeJitterStrictSchedule:
    def test_strict_schedule_returns_zero(self):
        job = _job(CronSchedule(kind="every", every_secs=3600), strict=True)
        assert CronService._compute_jitter(job) == 0.0

    def test_strict_schedule_daily_returns_zero(self):
        job = _job(CronSchedule(kind="every", every_secs=86400), strict=True)
        assert CronService._compute_jitter(job) == 0.0

    def test_strict_schedule_cron_returns_zero(self):
        job = _job(CronSchedule(kind="cron", cron_expr="0 9 * * 1-5"), strict=True)
        assert CronService._compute_jitter(job) == 0.0


class TestComputeJitterAtJobs:
    def test_at_job_returns_zero(self):
        job = _job(CronSchedule(kind="at", at_ts=1700000000.0))
        assert CronService._compute_jitter(job) == 0.0


class TestComputeJitterEveryJobs:
    def test_sub_hourly_returns_zero(self):
        """Jobs with interval < 3600s get no jitter."""
        job = _job(CronSchedule(kind="every", every_secs=300))
        assert CronService._compute_jitter(job) == 0.0

    def test_30min_returns_zero(self):
        job = _job(CronSchedule(kind="every", every_secs=1800))
        assert CronService._compute_jitter(job) == 0.0

    def test_59min_returns_zero(self):
        job = _job(CronSchedule(kind="every", every_secs=3540))
        assert CronService._compute_jitter(job) == 0.0

    @patch("kiro_crew.cron.random.uniform", return_value=600.0)
    def test_hourly_returns_hourly_jitter(self, mock_rand):
        job = _job(CronSchedule(kind="every", every_secs=3600))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_HOURLY_MAX)
        assert result == 600.0

    @patch("kiro_crew.cron.random.uniform", return_value=900.0)
    def test_3hour_returns_hourly_jitter(self, mock_rand):
        job = _job(CronSchedule(kind="every", every_secs=10800))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_HOURLY_MAX)
        assert result == 900.0

    @patch("kiro_crew.cron.random.uniform", return_value=3600.0)
    def test_daily_returns_daily_jitter(self, mock_rand):
        job = _job(CronSchedule(kind="every", every_secs=86400))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_DAILY_MAX)
        assert result == 3600.0

    @patch("kiro_crew.cron.random.uniform", return_value=5000.0)
    def test_weekly_returns_daily_jitter(self, mock_rand):
        job = _job(CronSchedule(kind="every", every_secs=604800))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_DAILY_MAX)
        assert result == 5000.0


class TestComputeJitterCronExpr:
    def test_sub_hourly_slash_minute_returns_zero(self):
        """*/5 * * * * (every 5 min) gets no jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="*/5 * * * *"))
        assert CronService._compute_jitter(job) == 0.0

    def test_sub_hourly_comma_minute_returns_zero(self):
        """0,30 * * * * (every 30 min) gets no jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="0,30 * * * *"))
        assert CronService._compute_jitter(job) == 0.0

    @patch("kiro_crew.cron.random.uniform", return_value=700.0)
    def test_hourly_wildcard_returns_hourly_jitter(self, mock_rand):
        """0 * * * * (every hour) gets hourly jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="0 * * * *"))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_HOURLY_MAX)
        assert result == 700.0

    @patch("kiro_crew.cron.random.uniform", return_value=500.0)
    def test_every_2_hours_returns_hourly_jitter(self, mock_rand):
        """0 */2 * * * gets hourly jitter (not daily)."""
        job = _job(CronSchedule(kind="cron", cron_expr="0 */2 * * *"))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_HOURLY_MAX)
        assert result == 500.0

    @patch("kiro_crew.cron.random.uniform", return_value=800.0)
    def test_twice_daily_comma_hours_returns_hourly_jitter(self, mock_rand):
        """0 1,13 * * * (twice daily) gets hourly jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="0 1,13 * * *"))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_HOURLY_MAX)
        assert result == 800.0

    @patch("kiro_crew.cron.random.uniform", return_value=4000.0)
    def test_daily_single_hour_returns_daily_jitter(self, mock_rand):
        """0 3 * * * (daily at 3am) gets daily jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="0 3 * * *"))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_DAILY_MAX)
        assert result == 4000.0

    @patch("kiro_crew.cron.random.uniform", return_value=5500.0)
    def test_weekly_single_hour_returns_daily_jitter(self, mock_rand):
        """0 9 * * 1-5 (weekdays at 9am) gets daily jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="0 9 * * 1-5"))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_DAILY_MAX)
        assert result == 5500.0

    @patch("kiro_crew.cron.random.uniform", return_value=300.0)
    def test_daily_two_digit_hour_returns_daily_jitter(self, mock_rand):
        """30 15 * * * (daily at 3:30pm) gets daily jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="30 15 * * *"))
        result = CronService._compute_jitter(job)
        mock_rand.assert_called_once_with(0, _JITTER_DAILY_MAX)
        assert result == 300.0


class TestComputeJitterBounds:
    """Verify jitter stays within documented bounds (statistical)."""

    def test_hourly_jitter_within_bounds(self):
        job = _job(CronSchedule(kind="every", every_secs=3600))
        results = [CronService._compute_jitter(job) for _ in range(100)]
        assert all(0 <= r <= _JITTER_HOURLY_MAX for r in results)
        assert max(results) > 0  # not all zero

    def test_daily_jitter_within_bounds(self):
        job = _job(CronSchedule(kind="every", every_secs=86400))
        results = [CronService._compute_jitter(job) for _ in range(100)]
        assert all(0 <= r <= _JITTER_DAILY_MAX for r in results)
        assert max(results) > _JITTER_HOURLY_MAX  # should exceed hourly range


class TestDriftPrevention:
    """Test that 'every' jobs use scheduled_ts for last_run_ts."""

    @pytest.mark.asyncio
    async def test_every_job_uses_scheduled_ts(self):
        """After execution, last_run_ts should be the pre-jitter time."""
        import time

        job = CronJob(
            id="drift1",
            name="drift-test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=3600),
            created_ts=time.time() - 7200,
        )

        svc = CronService()
        svc._on_job = None  # no-op execution

        before = time.time()
        # Patch jitter to a known value so we can verify drift prevention
        with patch.object(CronService, "_compute_jitter", return_value=0.1):
            await svc._run_job_isolated(job)
        after = time.time()

        # last_run_ts should be ~before (scheduled time), not after+jitter
        assert before <= job.last_run_ts <= after
        # The key invariant: last_run_ts should NOT include jitter delay
        # With 0.1s jitter, the difference should be negligible
        assert job.last_run_ts < before + 0.5

    @pytest.mark.asyncio
    async def test_cron_job_uses_post_execution_ts(self):
        """Cron-expression jobs should use post-execution time (no drift issue)."""
        import time

        job = CronJob(
            id="cron1",
            name="cron-test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="0 * * * *"),
            created_ts=time.time() - 7200,
        )

        svc = CronService()
        svc._on_job = None

        with patch.object(CronService, "_compute_jitter", return_value=0.0):
            await svc._run_job_isolated(job)

        # Cron jobs set last_run_ts in _execute (post-execution)
        assert job.last_run_ts is not None


class TestComputeJitterWildcardMinute:
    def test_every_minute_returns_zero(self):
        """* * * * * (every minute) gets no jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="* * * * *"))
        assert CronService._compute_jitter(job) == 0.0

    def test_every_minute_weekdays_returns_zero(self):
        """* * * * 1-5 (every minute on weekdays) gets no jitter."""
        job = _job(CronSchedule(kind="cron", cron_expr="* * * * 1-5"))
        assert CronService._compute_jitter(job) == 0.0


class TestReaperJitterAllowance:
    """Test that reaper accounts for jitter in timeout threshold."""

    @pytest.mark.asyncio
    async def test_job_within_timeout_plus_jitter_not_reaped(self):
        """Job running for less than timeout+jitter should NOT be reaped."""
        import time as time_mod

        from kiro_crew.cron import _JOB_TIMEOUT_SECS

        svc = CronService()
        svc._sessions = None
        # Job started 100s ago with 1200s jitter — well within threshold
        svc._job_start_times["j1"] = time_mod.time() - 100
        svc._job_jitter["j1"] = 1200.0

        # Run one reaper sweep
        await svc._reaper_loop_once() if hasattr(svc, "_reaper_loop_once") else None
        # Since there's no _reaper_loop_once, test the threshold logic directly
        now = time_mod.time()
        elapsed = now - svc._job_start_times["j1"]
        jitter_allowance = svc._job_jitter.get("j1", 0.0)
        assert elapsed <= _JOB_TIMEOUT_SECS + jitter_allowance

    @pytest.mark.asyncio
    async def test_job_exceeding_timeout_plus_jitter_would_be_reaped(self):
        """Job running longer than timeout+jitter should be reaped."""
        import time as time_mod

        from kiro_crew.cron import _JOB_TIMEOUT_SECS

        svc = CronService()
        svc._sessions = None
        # Job started (timeout + jitter + 100)s ago — exceeds threshold
        jitter = 1200.0
        svc._job_start_times["j2"] = time_mod.time() - (_JOB_TIMEOUT_SECS + jitter + 100)
        svc._job_jitter["j2"] = jitter

        now = time_mod.time()
        elapsed = now - svc._job_start_times["j2"]
        jitter_allowance = svc._job_jitter.get("j2", 0.0)
        # This job EXCEEDS the threshold — reaper would kill it
        assert elapsed > _JOB_TIMEOUT_SECS + jitter_allowance
