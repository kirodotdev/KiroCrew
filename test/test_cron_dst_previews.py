"""Next-run timestamps retain existing wall times across timezone transitions."""

from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from croniter import croniter

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.cron_service.schedule import (
    _next_cron_boundary_ts,
    compute_next_run_ts,
    is_due,
)


def _job(zone: str, expr: str) -> CronJob:
    return CronJob(
        id="abcdef01",
        name="daily",
        message="run",
        schedule=CronSchedule(kind="cron", cron_expr=expr),
        timezone=zone,
    )


@pytest.mark.parametrize(
    ("zone", "start", "expr", "expected"),
    [
        ("America/Los_Angeles", "2026-10-31T12:00", "0 3 * * *", "2026-11-01T03:00"),
        ("America/New_York", "2026-10-31T12:00", "0 3 * * *", "2026-11-01T03:00"),
        ("America/Los_Angeles", "2027-03-13T12:00", "0 22 * * 1-5", "2027-03-15T22:00"),
        ("America/Los_Angeles", "2026-11-01T01:45", "0 3 * * *", "2026-11-01T03:00"),
        ("Australia/Lord_Howe", "2026-04-04T12:00", "0 3 * * *", "2026-04-05T03:00"),
        ("Australia/Lord_Howe", "2026-10-03T12:00", "0 3 * * *", "2026-10-04T03:00"),
        ("UTC", "2026-10-31T12:00", "0 3 * * *", "2026-11-01T03:00"),
        ("America/Phoenix", "2026-10-31T12:00", "0 3 * * *", "2026-11-01T03:00"),
        ("America/Los_Angeles", "2026-11-03T12:00", "0 3 * * *", "2026-11-04T03:00"),
        ("America/Los_Angeles", "2026-10-31T12:00", "0 3,4 * * *", "2026-11-01T03:00"),
    ],
)
def test_the_preview_and_boundary_name_the_configured_wall_time(
    zone: str, start: str, expr: str, expected: str
) -> None:
    tz = ZoneInfo(zone)
    now = datetime.fromisoformat(start).replace(tzinfo=tz).timestamp()
    expected_ts = datetime.fromisoformat(expected).replace(tzinfo=tz).timestamp()
    job = _job(zone, expr)
    original = asdict(job)

    assert compute_next_run_ts(job, now) == expected_ts
    assert _next_cron_boundary_ts(job, now) == expected_ts
    assert is_due(job, expected_ts)
    assert asdict(job) == original


@pytest.mark.parametrize("skipped", [["2026-10-31"], ["2026-10-31", "2026-11-01"]])
def test_skipping_across_the_transition_keeps_the_next_allowed_wall_time(
    skipped: list[str],
) -> None:
    tz = ZoneInfo("America/Los_Angeles")
    job = _job(tz.key, "0 3 * * *")
    job.skip_dates = skipped
    now = datetime(2026, 10, 31, 0, tzinfo=tz).timestamp()
    day = 1 if len(skipped) == 1 else 2
    expected = datetime(2026, 11, day, 3, tzinfo=tz).timestamp()

    assert compute_next_run_ts(job, now) == expected
    assert is_due(job, expected)
    assert _next_cron_boundary_ts(job, now) == datetime(2026, 10, 31, 3, tzinfo=tz).timestamp()


@pytest.mark.parametrize(
    ("expr", "fold", "expected_day", "expected_hour", "expected_minute", "expected_fold"),
    [
        ("0 * * * *", 0, 1, 2, 0, 0),
        ("0 * * * *", 1, 1, 2, 0, 0),
        ("30 * * * *", 0, 1, 2, 30, 0),
        ("30 * * * *", 1, 1, 2, 30, 0),
        ("30 1 * * *", 0, 2, 1, 30, 0),
        ("30 1 * * *", 1, 2, 1, 30, 0),
    ],
)
def test_the_repeated_hour_keeps_valid_earlier_occurrences(
    expr: str,
    fold: int,
    expected_day: int,
    expected_hour: int,
    expected_minute: int,
    expected_fold: int,
) -> None:
    tz = ZoneInfo("America/Los_Angeles")
    base = datetime(2026, 11, 1, 1, 45, tzinfo=tz, fold=fold)
    expected = datetime(
        2026, 11, expected_day, expected_hour, expected_minute, tzinfo=tz, fold=expected_fold
    ).timestamp()
    job = _job(tz.key, expr)

    assert compute_next_run_ts(job, base.timestamp()) == expected
    assert _next_cron_boundary_ts(job, base.timestamp()) == expected
    assert is_due(job, expected)


def test_a_missing_wall_time_retains_the_existing_resolution() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    base = datetime(2027, 3, 13, 12, tzinfo=tz)
    expr = "30 2 * * *"
    expected = croniter(expr, base).get_next(float)
    job = _job(tz.key, expr)

    assert compute_next_run_ts(job, base.timestamp()) == expected
    assert _next_cron_boundary_ts(job, base.timestamp()) == expected
