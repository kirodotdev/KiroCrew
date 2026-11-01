"""A predicted run must be eligible under the job's local skip calendar."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.cron_service.schedule import (
    _MAX_SKIP_DATE_HORIZON_SECS,
    compute_next_run_ts,
    is_due,
)

_NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc).timestamp()


def _future_job(kind: str, zone: str = "UTC") -> CronJob:
    schedule = (
        CronSchedule(kind="every", every_secs=60)
        if kind == "every"
        else CronSchedule(kind="at", at_ts=_NOW + 60)
    )
    return CronJob(
        id="abcdef01",
        name="preview",
        message="run",
        schedule=schedule,
        created_ts=_NOW,
        timezone=zone,
        skip_dates=["2026-10-26"],
    )


@pytest.mark.parametrize("kind", ["every", "at"])
@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("UTC", datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp()),
        ("Asia/Taipei", datetime(2026, 10, 26, 16, tzinfo=timezone.utc).timestamp()),
    ],
)
def test_the_preview_advances_to_the_first_allowed_local_date(
    kind: str, zone: str, expected: float
) -> None:
    job = _future_job(kind, zone)
    original_schedule = (job.schedule.at_ts, job.schedule.every_secs)

    assert compute_next_run_ts(job, _NOW) == expected
    assert is_due(job, expected)
    assert job.last_run_ts is None
    assert (job.schedule.at_ts, job.schedule.every_secs) == original_schedule


@pytest.mark.parametrize("kind", ["every", "at"])
@pytest.mark.parametrize("skip_dates", [[], ["2026-10-25"]])
def test_an_unexcluded_preview_keeps_its_existing_deadline(
    kind: str, skip_dates: list[str]
) -> None:
    job = _future_job(kind)
    job.skip_dates = skip_dates

    assert compute_next_run_ts(job, _NOW) == _NOW + 60
    assert is_due(job, _NOW + 60)


@pytest.mark.parametrize("kind", ["every", "at"])
def test_consecutive_exclusions_advance_by_date_without_mutating_the_job(kind: str) -> None:
    job = _future_job(kind)
    job.skip_dates = ["2026-10-26", "2026-10-27", "2026-10-28"]
    original = asdict(job)
    expected = datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp()

    assert compute_next_run_ts(job, _NOW) == expected
    assert is_due(job, expected)
    assert asdict(job) == original


@pytest.mark.parametrize("kind", ["every", "at"])
@pytest.mark.parametrize("day", ["2026-03-08", "2026-11-01"])
def test_offset_changes_use_the_next_local_midnight(kind: str, day: str) -> None:
    tz = ZoneInfo("America/New_York")
    start = datetime.fromisoformat(day).replace(tzinfo=tz)
    job = _future_job(kind, "America/New_York")
    job.created_ts = start.timestamp() - 60
    job.schedule.at_ts = start.timestamp()
    job.skip_dates = [day]
    expected = (start + timedelta(days=1)).timestamp()

    assert compute_next_run_ts(job, start.timestamp() - 60) == expected
    assert is_due(job, expected)


@pytest.mark.parametrize("kind", ["every", "at"])
def test_skip_dates_use_the_job_date_instead_of_the_utc_date(kind: str) -> None:
    now = datetime(2026, 10, 26, 23, 59, tzinfo=timezone.utc).timestamp()
    job = _future_job(kind, "Asia/Taipei")
    job.created_ts = now
    job.schedule.at_ts = now + 60
    job.skip_dates = ["2026-10-27"]
    expected = datetime(2026, 10, 27, 16, tzinfo=timezone.utc).timestamp()

    assert compute_next_run_ts(job, now) == expected
    assert is_due(job, expected)


@pytest.mark.parametrize("kind", ["every", "at"])
def test_backoff_cannot_move_a_preview_onto_an_excluded_date(kind: str) -> None:
    now = datetime(2026, 10, 25, 23, 58, tzinfo=timezone.utc).timestamp()
    job = _future_job(kind)
    job.created_ts = now
    job.last_run_ts = now
    job.schedule.at_ts = now + 60
    job.auto_pause_after_failures = 0
    job.consecutive_failures = 5
    expected = datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp()
    original = asdict(job)

    assert not is_due(job, job.failure_backoff_until())
    assert compute_next_run_ts(job, now) == expected
    assert is_due(job, expected)
    assert asdict(job) == original


def test_an_overdue_interval_waits_for_the_first_allowed_date() -> None:
    job = _future_job("every")
    job.last_run_ts = _NOW - 120

    assert compute_next_run_ts(job, _NOW) == datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("kind", ["every", "at"])
def test_disabled_jobs_have_no_preview(kind: str) -> None:
    job = _future_job(kind)
    job.enabled = False

    assert compute_next_run_ts(job, _NOW) is None


def test_a_past_one_shot_keeps_its_absent_preview() -> None:
    job = _future_job("at")
    job.schedule.at_ts = _NOW - 60

    assert compute_next_run_ts(job, _NOW) is None


@pytest.mark.parametrize("kind", ["every", "at"])
def test_an_all_excluded_horizon_has_no_preview(kind: str) -> None:
    job = _future_job(kind)
    first = datetime.fromtimestamp(_NOW, tz=timezone.utc)
    job.skip_dates = [
        (first + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(int(_MAX_SKIP_DATE_HORIZON_SECS // 86400) + 2)
    ]

    assert compute_next_run_ts(job, _NOW) is None
