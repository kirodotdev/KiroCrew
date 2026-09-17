"""Regression tests: cron jobs wake ON their next boundary, not on a flat poll.

CronService._next_wake_secs arms the timer with the exact delay to a cron job's
next fire boundary (via compute_next_run_ts), the same way the ``at`` and
``every`` branches do. _effective_delay still caps a distant boundary at
_TIMER_POLL_SECS so an externally-added job is picked up within one poll.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kiro_crew.cron import _TIMER_POLL_SECS, CronService


class TestCronWakesOnBoundary:
    def test_next_wake_targets_cron_boundary_when_sooner_than_poll(self, tmp_path: Path) -> None:
        """An every-minute cron 15s from its boundary yields ~15s, under the poll."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        svc.add_job(name="boundary", message="m", cron_expr="* * * * *", timezone="UTC")

        # 45s past a minute boundary -> the next boundary is 15s away, under the poll.
        pinned = 1_800_000_045.0
        with patch("kiro_crew.cron.time.time", return_value=pinned):
            delay = svc._next_wake_secs()

        assert delay is not None
        assert 14.0 <= delay <= 16.0
        assert delay < _TIMER_POLL_SECS

    def test_next_wake_reports_true_far_boundary_uncapped(self, tmp_path: Path) -> None:
        """A weekly cron reports its real (large) boundary delay from _next_wake_secs.

        _next_wake_secs returns the true delay to the next boundary; _effective_delay
        applies the poll cap afterwards (asserted separately below).
        """
        svc = CronService(base_dir=tmp_path)
        svc._load()
        # 10:30 UTC on Wednesdays.
        svc.add_job(name="weekly", message="m", cron_expr="30 10 * * 3", timezone="UTC")

        # A Thursday 00:00:00 UTC: the next Wed 10:30 is ~6 days away, far above the poll.
        pinned = 1_789_603_200.0
        with patch("kiro_crew.cron.time.time", return_value=pinned):
            delay = svc._next_wake_secs()

        assert delay is not None
        assert delay > _TIMER_POLL_SECS
        # A weekly boundary is on the order of days, not seconds.
        assert delay > 24 * 3600

    def test_effective_delay_still_capped_for_distant_boundary(self, tmp_path: Path) -> None:
        """_effective_delay clamps the far weekly boundary back to the poll interval."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        svc.add_job(name="weekly", message="m", cron_expr="30 10 * * 3", timezone="UTC")

        assert svc._effective_delay() <= _TIMER_POLL_SECS

    def test_recent_run_in_current_minute_targets_next_minute(self, tmp_path: Path) -> None:
        """A cron whose last_run_ts is in the current minute re-arms to the next minute.

        _next_wake_secs uses compute_next_run_ts (croniter get_next), which returns
        the strictly next boundary rather than the current minute, so the delay is
        the following minute (~60s), not ~0s. This rules out a same-minute
        zero-delay refire spin.
        """
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="minutely", message="m", cron_expr="* * * * *", timezone="UTC")

        # now = an exact minute boundary; last_run_ts sits 0.5s back in this minute.
        pinned = 1_800_000_060.0  # divisible by 60 -> top of a minute
        job.last_run_ts = pinned - 0.5
        with patch("kiro_crew.cron.time.time", return_value=pinned):
            delay = svc._next_wake_secs()

        assert delay is not None
        # The next boundary is the following minute (~60s away), not ~0s.
        assert 55.0 <= delay <= 60.0

    def test_arming_does_not_traverse_skip_dates(self, tmp_path: Path) -> None:
        """A minutely cron with many skip_dates arms in O(1), not by scanning them.

        Timer arming needs only the next boundary; _is_due enforces skip_dates at
        fire time. _next_wake_secs must not walk the skip_dates set, so a job with
        a year of consecutive skip_dates cannot stall the event loop on re-arm.
        """
        import datetime as _dt

        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="skipped", message="m", cron_expr="* * * * *", timezone="UTC")

        pinned = 1_800_000_045.0
        start = _dt.datetime.fromtimestamp(pinned, _dt.timezone.utc).date()
        job.skip_dates = [(start + _dt.timedelta(days=n)).strftime("%Y-%m-%d") for n in range(366)]

        with patch("kiro_crew.cron.time.time", return_value=pinned):
            delay = svc._next_wake_secs()

        # Arms at the immediate next boundary (~15s), unaffected by skip_dates.
        assert delay is not None
        assert 14.0 <= delay <= 16.0
