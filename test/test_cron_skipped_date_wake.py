"""Skipped local dates must not turn overdue jobs into immediate timer rescans."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiro_crew import cron
from kiro_crew.cron import _TIMER_POLL_SECS, CronJob, CronSchedule, CronService
from kiro_crew.cron_service.schedule import is_due, next_wake_secs
from kiro_crew.testing.clock import ManualClock

_NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc).timestamp()


def _overdue_job(kind: str, now: float = _NOW) -> CronJob:
    schedule = (
        CronSchedule(kind="every", every_secs=60)
        if kind == "every"
        else CronSchedule(kind="at", at_ts=now - 60)
    )
    return CronJob(
        id="abcdef01",
        name="skipped",
        message="run",
        schedule=schedule,
        created_ts=now - 120,
        timezone="UTC",
        skip_dates=["2026-10-26"],
    )


@pytest.mark.parametrize("kind", ["every", "at"])
def test_each_skipped_wake_waits_for_the_poll_cadence(kind: str) -> None:
    job = _overdue_job(kind)
    for elapsed in (0, 30, 60):
        now = _NOW + elapsed
        assert not is_due(job, now)
        assert next_wake_secs([job], set(), now) == _TIMER_POLL_SECS
    assert job.last_run_ts is None


@pytest.mark.parametrize("kind", ["every", "at"])
def test_the_job_local_date_controls_the_wake(kind: str) -> None:
    now = datetime(2026, 10, 26, 16, 0, 15, tzinfo=timezone.utc).timestamp()
    job = _overdue_job(kind, now)
    job.timezone = "Asia/Taipei"
    job.skip_dates = ["2026-10-27"]

    assert not is_due(job, now)
    assert next_wake_secs([job], set(), now) == _TIMER_POLL_SECS

    job.timezone = "UTC"
    assert is_due(job, now)
    assert next_wake_secs([job], set(), now) == 0


@pytest.mark.parametrize("kind", ["every", "at"])
def test_the_job_can_run_when_its_local_skip_date_ends(kind: str) -> None:
    now = datetime(2026, 10, 26, 15, 59, 45, tzinfo=timezone.utc).timestamp()
    job = _overdue_job(kind, now)
    job.timezone = "Asia/Taipei"

    assert not is_due(job, now)
    assert next_wake_secs([job], set(), now) == _TIMER_POLL_SECS
    assert is_due(job, now + _TIMER_POLL_SECS)
    assert next_wake_secs([job], set(), now + _TIMER_POLL_SECS) == 0
    assert job.last_run_ts is None


@pytest.mark.parametrize("kind", ["every", "at"])
def test_a_skipped_job_does_not_delay_another_job(kind: str) -> None:
    job = _overdue_job(kind)
    other = CronJob(
        id="abcdef02",
        name="next",
        message="run",
        schedule=CronSchedule(kind="every", every_secs=60),
        created_ts=_NOW - 55,
        timezone="UTC",
    )
    assert next_wake_secs([job, other], set(), _NOW) == 5


@pytest.mark.parametrize("kind", ["every", "at"])
def test_a_future_deadline_keeps_its_uncapped_wake(kind: str) -> None:
    job = _overdue_job(kind)
    if kind == "every":
        job.last_run_ts = _NOW
    else:
        job.schedule.at_ts = _NOW + 60
    assert next_wake_secs([job], set(), _NOW) == 60


@pytest.mark.parametrize("kind", ["every", "at"])
@pytest.mark.parametrize("occupied", ["disabled", "claimed"])
def test_an_unavailable_job_does_not_add_a_wake(kind: str, occupied: str) -> None:
    job = _overdue_job(kind)
    job.enabled = occupied != "disabled"
    claims = {job.id} if occupied == "claimed" else set()
    assert next_wake_secs([job], claims, _NOW) is None


@pytest.mark.parametrize("kind", ["every", "at"])
def test_the_service_does_not_arm_an_immediate_tick(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(start=_NOW)
    clock.install(monkeypatch, cron)
    service = CronService(base_dir=tmp_path)
    service._jobs = [_overdue_job(kind)]

    assert service._effective_delay() == _TIMER_POLL_SECS
