"""A timezone that cannot be resolved warns once per job, not on every tick."""

from __future__ import annotations

import logging
from datetime import timezone

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.cron_service import schedule


def _job(job_id: str, tz: str = "America/New_York") -> CronJob:
    return CronJob(
        id=job_id,
        name="n",
        message="m",
        timezone=tz,
        schedule=CronSchedule(kind="cron", cron_expr="*/5 * * * *"),
    )


@pytest.fixture
def no_tzdata(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with no timezone database: every ZoneInfo lookup fails."""
    from zoneinfo import ZoneInfoNotFoundError

    def _missing(name: str) -> None:
        raise ZoneInfoNotFoundError(f"No time zone found with key {name}")

    monkeypatch.setattr(schedule, "ZoneInfo", _missing)
    # raising=False keeps the behaviour test meaningful against a build without the memo.
    monkeypatch.setattr(schedule, "_BAD_TZ_WARNED", {}, raising=False)
    monkeypatch.setattr(schedule, "_BAD_TZ_REFUSED", [0], raising=False)


def _tz_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Failed to resolve timezone")]


@pytest.mark.usefixtures("no_tzdata")
def test_unresolvable_zone_warns_once_per_job_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="kiro_crew.cron")
    jobs = [_job(f"job-{n}") for n in range(3)]
    for _tick in range(10):
        for job in jobs:
            assert schedule._job_tz(job) is timezone.utc

    records = _tz_warnings(caplog)
    assert len(records) == 3
    assert all(r.levelno == logging.WARNING and r.exc_info is None for r in records)
    assert "America/New_York" in records[0].getMessage()

    # A different zone on the same job warns again, once.
    changed = _job("job-0", "Europe/Paris")
    schedule._job_tz(changed)
    schedule._job_tz(changed)
    assert len(_tz_warnings(caplog)) == 4


def test_successful_resolution_clears_the_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule, "_BAD_TZ_WARNED", {hash("job-0"): hash("America/New_York")})
    assert getattr(schedule._job_tz(_job("job-0", "UTC")), "key", None) == "UTC"
    assert hash("job-0") not in schedule._BAD_TZ_WARNED


@pytest.mark.usefixtures("no_tzdata")
def test_full_memo_refuses_new_rows_and_says_so_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "_BAD_TZ_WARNED_MAX", 2)
    caplog.set_level(logging.WARNING, logger="kiro_crew.cron")
    jobs = [_job(f"job-{n}") for n in range(3)]
    for _tick in range(3):
        for job in jobs:
            schedule._job_tz(job)
            assert len(schedule._BAD_TZ_WARNED) <= 2

    assert len(_tz_warnings(caplog)) == 2
    full = [r for r in caplog.records if "memo is full" in r.getMessage()]
    assert len(full) == 1
    assert schedule._BAD_TZ_REFUSED == [3]
