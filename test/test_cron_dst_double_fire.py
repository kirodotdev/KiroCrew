"""Regression tests: a DST spring-forward runs a fixed-time cron job once, not twice.

``is_due`` (``kiro_crew.cron_service.schedule``) treats a cron-expression job as
due when ``croniter.match`` accepts the current local minute. On a
spring-forward day croniter matches a skipped wall time such as 02:30 in two
ADJACENT UTC minutes (03:29 and 03:30 local time in America/Toronto). Both
instants belong to one cron occurrence, and ``is_due`` compares occurrences, so
the job runs once.

Every instant is built from a local wall time with an explicit ``ZoneInfo``
(``fold`` picks the pass through the repeated fall-back hour), so no test
depends on the host timezone. Synthetic data only; no network; virtual clock.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import kiro_crew.cron as cron_mod
from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.testing.clock import ManualClock

TORONTO = ZoneInfo("America/Toronto")


def _at(tz: ZoneInfo, *parts: int, fold: int = 0) -> float:
    """Epoch seconds of a local wall time in ``tz``; ``fold=1`` is the second pass."""
    return datetime(*parts, tzinfo=tz, fold=fold).timestamp()


def _local(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S%z")


def _runs(expr: str, tz: ZoneInfo, start: float, minutes: int) -> list[float]:
    """Instants in ``[start, start + minutes)`` at which the timer would run ``expr``.

    Mirrors the real loop: the timer ticks at least every 30 s
    (``_TIMER_POLL_SECS``), a due job runs, and ``_execute`` stamps
    ``last_run_ts`` when the run ends (here 5 s later, in the same minute).
    """
    job = CronJob(
        id="j",
        name="t",
        message="m",
        schedule=CronSchedule(kind="cron", cron_expr=expr),
        timezone=tz.key,
    )
    runs: list[float] = []
    for i in range(minutes):
        for offset in (1, 31):
            now = start + i * 60 + offset
            if CronService._is_due(job, now):
                runs.append(now)
                job.last_run_ts = now + 5
    return runs


def test_normal_day_daily_job_runs_once() -> None:
    day = (2025, 3, 10)
    runs = _runs("30 2 * * *", TORONTO, _at(TORONTO, *day, 0, 0), 360)
    assert runs == [_at(TORONTO, *day, 2, 30, 1)]


def test_zone_without_dst_runs_once_on_the_us_jump_day() -> None:
    """America/Phoenix keeps one offset all year, so its 02:30 exists and runs once."""
    phoenix = ZoneInfo("America/Phoenix")
    day = (2025, 3, 9)
    runs = _runs("30 2 * * *", phoenix, _at(phoenix, *day, 0, 0), 360)
    assert runs == [_at(phoenix, *day, 2, 30, 1)]


@pytest.mark.parametrize(
    ("zone", "expr", "day"),
    [
        ("America/Toronto", "30 2 * * *", (2025, 3, 9)),
        ("America/Toronto", "45 2 * * *", (2025, 3, 9)),
        ("America/Chicago", "30 2 * * *", (2025, 3, 9)),
        ("America/Los_Angeles", "30 2 * * *", (2025, 3, 9)),
        ("Europe/Berlin", "30 2 * * *", (2025, 3, 30)),
    ],
)
def test_spring_forward_daily_job_runs_once(
    zone: str, expr: str, day: tuple[int, int, int]
) -> None:
    """A daily job whose 02:xx wall time the jump skips runs once, in the resumed hour."""
    tz = ZoneInfo(zone)
    runs = _runs(expr, tz, _at(tz, *day, 0, 0), 360)
    assert len(runs) == 1, f"ran {len(runs)} times: {[_local(t, tz) for t in runs]}"
    assert _at(tz, *day, 3, 0) <= runs[0] < _at(tz, *day, 4, 0)


def test_fall_back_daily_job_runs_in_each_repeated_hour() -> None:
    """A fixed-time job in the repeated fall-back hour runs once in each pass.

    01:30 happens twice in America/Toronto on the fall-back day: first in EDT
    (``fold=0``), then in EST (``fold=1``), an hour apart. That is beyond the
    adjacent-minute bound, so ``is_due`` treats the two as separate runs.
    Whether a fixed-time job should run once in the repeated hour is a policy
    question for maintainers; this test pins the behavior as it stands, so a
    change to it is deliberate.
    """
    day = (2025, 11, 2)
    runs = _runs("30 1 * * *", TORONTO, _at(TORONTO, *day, 0, 0), 360)
    assert runs == [
        _at(TORONTO, *day, 1, 30, 1, fold=0),
        _at(TORONTO, *day, 1, 30, 1, fold=1),
    ], [_local(t, TORONTO) for t in runs]


async def _retire_real_timer(svc: CronService) -> None:
    """Make the test's own ``_on_timer()`` calls the only ticks."""
    svc._arm_timer = lambda: None  # type: ignore[method-assign]
    task, svc._timer_task = svc._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_service_runs_spring_forward_slot_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through CronService._on_timer and _execute on a virtual clock."""
    runs: list[float] = []

    async def on_job(job: CronJob) -> None:
        runs.append(cron_mod.time.time())

    svc = CronService(base_dir=tmp_path, on_job=on_job)
    await svc.start()
    await _retire_real_timer(svc)
    clock = ManualClock(start=_at(TORONTO, 2025, 3, 9, 3, 29, 1))
    clock.install(monkeypatch, cron_mod)
    await svc.add_job_async(
        "daily-0230", "msg", cron_expr="30 2 * * *", timezone=TORONTO.key, strict_schedule=True
    )
    job_id = svc._jobs[0].id
    admitted = SimpleNamespace(admitted=True, reason="")
    try:
        with patch("kiro_crew.cron.admission_check", return_value=admitted):
            for _ in range(2):  # one tick at 03:29 local, one at 03:30 local
                await svc._on_timer()
                claim = svc._claims.get(job_id)
                if claim is not None and claim.task is not None:
                    await claim.task
                clock.advance(60)
    finally:
        await svc.stop()
    assert (
        len(runs) == 1
    ), f"one 02:30 slot ran {len(runs)} times: {[_local(t, TORONTO) for t in runs]}"
