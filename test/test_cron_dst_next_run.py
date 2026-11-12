"""A cron job's next run keeps its configured wall time across a DST change."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kiro_crew.cron import (
    CronJob,
    CronSchedule,
    _next_cron_boundary_ts,
    compute_next_run_ts,
    is_due,
)

LA = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize(
    ("expr", "now", "expected"),
    [
        # The day before the fall-back: 03:00 PST, not 04:00.
        ("0 3 * * *", datetime(2026, 10, 31, 12, tzinfo=LA), datetime(2026, 11, 1, 3, tzinfo=LA)),
        # A Friday night before it: Monday 22:00 PST, not 23:00.
        (
            "0 22 * * 1-5",
            datetime(2026, 10, 30, 23, tzinfo=LA),
            datetime(2026, 11, 2, 22, tzinfo=LA),
        ),
        # The day before the spring-forward: 22:00 PDT, not 21:00.
        ("0 22 * * *", datetime(2027, 3, 13, 23, tzinfo=LA), datetime(2027, 3, 14, 22, tzinfo=LA)),
        # A wall time the spring-forward skips: 02:30 runs at 03:30 PDT.
        (
            "30 2 * * *",
            datetime(2027, 3, 14, 1, tzinfo=LA),
            datetime(2027, 3, 14, 3, 30, tzinfo=LA),
        ),
        # In the repeated hour's second pass: its 01:30 PST, not the 01:30 PDT already gone.
        (
            "30 1 * * *",
            datetime(2026, 11, 1, 1, 15, fold=1, tzinfo=LA),
            datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=LA),
        ),
    ],
)
def test_the_next_run_is_the_configured_wall_time(
    expr: str, now: datetime, expected: datetime
) -> None:
    job = CronJob(
        id="j",
        name="n",
        message="m",
        schedule=CronSchedule(kind="cron", cron_expr=expr),
        timezone="America/Los_Angeles",
    )
    want = expected.timestamp()

    assert compute_next_run_ts(job, now.timestamp()) == want
    assert _next_cron_boundary_ts(job, now.timestamp()) == want
    assert is_due(job, want)
