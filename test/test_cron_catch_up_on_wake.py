"""Catch-up for cron-expression boundaries the scheduler never scanned.

A cron-expression job is due only while its expression matches the current
minute, so a boundary that falls while the host is asleep (or the loop is
stalled) needs its own due rule. ``every``/``at`` jobs fire once on wake
without one because they stay due until they run. These tests pin the catch-up
rule: the LATEST missed boundary fires once, never once per missed tick.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiro_crew.cron import CronJob, CronService
from kiro_crew.cron_service.schedule import missed_cron_boundary

# A fixed Tuesday afternoon (15:00:30 UTC), well away from any minute edge.
_NOW = datetime(2026, 3, 10, 15, 0, 30, tzinfo=timezone.utc).timestamp()
_NINE_TODAY = datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc).timestamp()
_DAY = 86400.0


def _cron_job(expr: str, **kw: object) -> CronJob:
    job = CronJob(id="j1", name="daily", message="m")
    job.schedule.kind = "cron"
    job.schedule.cron_expr = expr
    job.timezone = "UTC"
    for k, v in kw.items():
        setattr(job, k, v)
    return job


class TestMissedCronBoundary:
    def test_a_boundary_inside_the_gap_is_returned(self) -> None:
        job = _cron_job("0 9 * * *")
        assert missed_cron_boundary(job, _NOW - 8 * 3600, _NOW) == _NINE_TODAY

    def test_three_missed_days_collapse_to_the_latest_boundary(self) -> None:
        job = _cron_job("0 9 * * *")
        assert missed_cron_boundary(job, _NOW - 3 * _DAY, _NOW) == _NINE_TODAY

    def test_no_boundary_after_the_previous_scan_is_nothing_to_catch_up(self) -> None:
        job = _cron_job("0 9 * * *")
        assert missed_cron_boundary(job, _NINE_TODAY + 60, _NOW) is None

    def test_the_current_minute_belongs_to_the_ordinary_due_path(self) -> None:
        job = _cron_job("0 15 * * *")
        assert missed_cron_boundary(job, _NOW - 3600, _NOW) is None

    def test_a_job_that_already_ran_at_the_boundary_is_not_caught_up(self) -> None:
        job = _cron_job("0 9 * * *", last_run_ts=_NINE_TODAY + 5)
        assert missed_cron_boundary(job, _NOW - 8 * 3600, _NOW) is None

    def test_a_missed_boundary_on_a_skip_date_is_not_caught_up(self) -> None:
        job = _cron_job("0 9 * * *", skip_dates=["2026-03-10"])
        assert missed_cron_boundary(job, _NOW - 8 * 3600, _NOW) is None

    def test_a_catch_up_that_would_run_on_a_skip_date_is_not_fired(self) -> None:
        # The boundary was yesterday at 09:00; waking before 09:00 on a skipped day.
        early = datetime(2026, 3, 10, 8, 0, 30, tzinfo=timezone.utc).timestamp()
        job = _cron_job("0 9 * * *", skip_dates=["2026-03-10"])
        assert missed_cron_boundary(job, early - _DAY, early) is None
        job.skip_dates = []
        assert missed_cron_boundary(job, early - _DAY, early) == _NINE_TODAY - _DAY

    def test_a_backward_clock_step_catches_nothing_up(self) -> None:
        job = _cron_job("0 9 * * *")
        assert missed_cron_boundary(job, _NOW + 60, _NOW) is None

    def test_interval_and_one_shot_jobs_are_left_to_their_own_due_rule(self) -> None:
        every = CronJob(id="e1", name="e", message="m")
        every.schedule.kind = "every"
        every.schedule.every_secs = 3600
        at = CronJob(id="a1", name="a", message="m")
        at.schedule.kind = "at"
        at.schedule.at_ts = _NINE_TODAY
        assert missed_cron_boundary(every, _NOW - _DAY, _NOW) is None
        assert missed_cron_boundary(at, _NOW - _DAY, _NOW) is None


def _recent_boundary_expr(minutes_ago: int) -> str:
    """A daily UTC expression whose only boundary today was ``minutes_ago`` ago."""
    then = datetime.fromtimestamp(time.time() - minutes_ago * 60, tz=timezone.utc)
    return f"{then.minute} {then.hour} * * *"


async def _drain(svc: CronService) -> None:
    for claim in list(svc._claims.values()):
        if claim.task is not None:
            await claim.task


class TestOnTimerCatchUp:
    @pytest.mark.asyncio
    async def test_a_boundary_slept_through_fires_once_on_the_next_scan(
        self, tmp_path: Path
    ) -> None:
        fired: list[str] = []

        async def callback(job: CronJob) -> None:
            fired.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        svc.add_job(
            "daily",
            "msg",
            cron_expr=_recent_boundary_expr(5),
            timezone="UTC",
            strict_schedule=True,
        )
        # The previous scan ran an hour ago; the host then slept through the boundary.
        svc._last_scan_wall = time.time() - 3600

        await svc._on_timer()
        await _drain(svc)
        assert fired == ["daily"]

        # The next scan saw no new boundary: the catch-up does not repeat.
        await svc._on_timer()
        await _drain(svc)
        assert fired == ["daily"]

    @pytest.mark.asyncio
    async def test_a_boundary_before_the_first_scan_of_this_process_is_not_caught_up(
        self, tmp_path: Path
    ) -> None:
        """RESIDUAL, pinned: a boundary missed while the gateway was not running is dropped.

        The gap is measured from this process's previous scan, so a fresh
        gateway has no gap to measure. Catching that case up needs a persisted
        scan watermark (an owner decision); this test asserts today's answer.
        """
        fired: list[str] = []

        async def callback(job: CronJob) -> None:
            fired.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        svc.add_job(
            "daily",
            "msg",
            cron_expr=_recent_boundary_expr(5),
            timezone="UTC",
            strict_schedule=True,
        )
        assert svc._last_scan_wall is None

        before = time.time()
        await svc._on_timer()
        await _drain(svc)
        assert fired == []
        # ...but that first scan is the watermark a later sleep is measured from.
        assert svc._last_scan_wall is not None and svc._last_scan_wall >= before
