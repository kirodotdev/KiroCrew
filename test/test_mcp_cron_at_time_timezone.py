"""``cron_add`` reads a one-shot ``at_time`` clock time in the job's own timezone.

The job's ``timezone`` decides how its ``cron_expr`` fields are read, which
dates its ``skip_dates`` name, and the zone every confirmation renders in. An
``at_time`` read in any other zone -- the configured one, UTC when nothing is
set -- stores an instant the confirmation then renders as a clock time the
caller never asked for: ``at_time="9am", timezone="America/Los_Angeles"`` on a
UTC gateway would be scheduled, and confirmed, as 2am in Los Angeles.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kiro_crew.cron import CronService

#: 14 hours ahead of UTC with no DST, so a clock time read in the wrong zone
#: lands on a different hour whatever the date.
_ZONE = "Pacific/Kiritimati"


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """These tests exercise schedule field handling, not authorization."""


@pytest.fixture()
def call_tool(monkeypatch, tmp_path):
    """The real ``cron_add`` against a private store, with the configured zone UTC."""
    from kiro_crew.mcp_cron import _call_tool_locally

    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "UTC")

    def _call(args: dict) -> tuple[str, list]:
        name = f"attz-{uuid.uuid4().hex[:8]}"
        result = _call_tool_locally("cron_add", {"name": name, "message": "ping", **args})
        jobs = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name]
        return result, jobs

    return _call


def test_clock_time_is_read_in_the_job_timezone(call_tool):
    result, jobs = call_tool({"at_time": "23:59", "timezone": _ZONE})

    assert "Added job" in result
    assert len(jobs) == 1
    job = jobs[0]
    assert job.timezone == _ZONE
    local = datetime.fromtimestamp(job.schedule.at_ts, tz=ZoneInfo(_ZONE))
    assert (local.hour, local.minute) == (23, 59)
    # The confirmation names the clock time the caller asked for.
    assert "11:59 PM" in result


def test_clock_time_without_a_timezone_still_uses_the_configured_zone(call_tool):
    result, jobs = call_tool({"at_time": "23:59"})

    assert "Added job" in result
    local = datetime.fromtimestamp(jobs[0].schedule.at_ts, tz=ZoneInfo("UTC"))
    assert (local.hour, local.minute) == (23, 59)


def test_invalid_timezone_with_at_time_is_refused_before_parsing(call_tool):
    result, jobs = call_tool({"at_time": "23:59", "timezone": "Not/AZone"})

    assert result.startswith("Error: invalid timezone")
    assert jobs == []


def test_spring_forward_gap_is_refused_without_creating_a_job(call_tool):
    result, jobs = call_tool(
        {
            "at_time": "2027-03-14 02:30",
            "timezone": "America/Los_Angeles",
        }
    )

    assert result.startswith("Error: 02:30 on 2027-03-14 does not exist")
    assert jobs == []


def test_wall_clock_outside_datetime_range_is_refused_without_creating_a_job(call_tool):
    result, jobs = call_tool(
        {
            "at_time": "9999-12-31 23:59",
            "timezone": "America/Los_Angeles",
        }
    )

    assert result == (
        "Error: 9999-12-31 23:59 in America/Los_Angeles is outside the supported date range"
    )
    assert jobs == []
