"""A host with no timezone data still assembles prompts and lists cron jobs.

A stock ``ubuntu``/``debian`` container image or a slim server can lack both the
system zoneinfo tree and the ``tzdata`` package. ``ZoneInfo`` then raises
``ZoneInfoNotFoundError`` for every key, UTC included, so a UTC fallback built
from ``ZoneInfo("UTC")`` raises from the same call it was meant to absorb.
``get_local_tz`` runs in prompt assembly on every chat turn and in the Schedule
page's ``GET /api/crons``; ``_job_tz`` runs for every cron job on every timer
tick.

The missing data is simulated at the seam the product calls:
``cron_service.schedule`` binds ``ZoneInfo`` at import, so replacing that module
global makes every lookup there fail the way an empty ``TZPATH`` does, without
touching the process-wide zone cache other tests rely on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import pytest

from kiro_crew import cron, dep_sync
from kiro_crew.cron_service import schedule

ROOT = Path(__file__).resolve().parents[1]


def _no_tz_data(key: str):
    raise ZoneInfoNotFoundError(f"No time zone found with key {key}")


@pytest.fixture
def no_tz_data(monkeypatch):
    monkeypatch.setattr(schedule, "ZoneInfo", _no_tz_data)


@pytest.mark.parametrize("configured", ["", "UTC", "Asia/Tokyo"])
def test_get_local_tz_degrades_to_fixed_utc(no_tz_data, monkeypatch, configured) -> None:
    monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: configured)

    tz_name, tz = cron.get_local_tz()

    assert tz_name == "UTC"
    assert tz is timezone.utc


def test_current_date_line_renders_without_tz_data(no_tz_data, monkeypatch) -> None:
    """The ``[CURRENT DATE]`` line formats ``datetime.now(tz)`` with ``%Z``."""
    monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "")
    _, tz = cron.get_local_tz()

    stamp = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc).astimezone(tz)

    assert stamp.strftime("%A, %Y-%m-%d %H:%M %Z") == "Wednesday, 2026-10-21 09:30 UTC"


@pytest.mark.parametrize("job_zone", ["", "America/Toronto"])
def test_job_tz_degrades_to_fixed_utc(no_tz_data, monkeypatch, job_zone) -> None:
    monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "")
    job = cron.CronJob(id="j1", name="t", message="m", timezone=job_zone)

    assert schedule._job_tz(job) is timezone.utc


def test_cron_job_next_run_resolves_without_tz_data(no_tz_data, monkeypatch) -> None:
    """``compute_next_run_ts`` reaches ``_job_tz``; a daily 09:00 job lands at 09:00 UTC."""
    monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "")
    job = cron.CronJob(
        id="j1",
        name="t",
        message="m",
        schedule=cron.CronSchedule(kind="cron", cron_expr="0 9 * * *"),
    )
    now = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc).timestamp()

    next_ts = cron.compute_next_run_ts(job, now)

    assert next_ts == datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc).timestamp()


def test_tzdata_is_declared_without_a_platform_marker() -> None:
    """``zoneinfo`` falls back to ``tzdata`` wherever no system tree exists, on any OS."""
    specs = dep_sync.declared_requirements(ROOT)
    assert specs is not None

    tzdata = [spec for spec in specs if spec.split(">")[0].split("=")[0].strip() == "tzdata"]

    assert tzdata == ["tzdata>=2026.2"]
