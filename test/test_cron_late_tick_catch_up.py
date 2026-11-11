"""Regression test: a timer tick that lands late still runs the cron minute it skipped.

A cron-expression job is due when its expression matches the current minute, so a
minute no tick landed in (the event loop, or the worker thread the tick's store
scan waits on, was busy) lost its run. The tick also runs the latest matching
minute since the previous tick, once, when the two ticks are at most the grace
apart.

Synthetic data only; no network; virtual clock.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kiro_crew.cron as cron_mod
from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.cron_service import fields as fields_mod
from kiro_crew.cron_service.schedule import is_due
from kiro_crew.testing.clock import ManualClock

_ADMITTED = SimpleNamespace(admitted=True, reason="")


def _utc(*parts: int) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def _job(expr: str, tz: str = "UTC", **fields: float) -> CronJob:
    schedule = CronSchedule(kind="cron", cron_expr=expr)
    return CronJob(id="j", name="t", message="m", schedule=schedule, timezone=tz, **fields)


async def _retire_real_timer(svc: CronService) -> None:
    """Make the test's own ``_on_timer()`` calls the only ticks."""
    svc._arm_timer = lambda: None  # type: ignore[method-assign]
    task, svc._timer_task = svc._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_late_tick_runs_the_minute_it_skipped_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticks at 17:06:58 and 17:08:03 skip 17:07; the second tick runs it, the third does not."""
    runs: list[float] = []

    async def on_job(job: CronJob) -> None:
        runs.append(cron_mod.time.time())

    svc = CronService(base_dir=tmp_path, on_job=on_job)
    await svc.start()
    await _retire_real_timer(svc)
    clock = ManualClock(start=_utc(2026, 11, 6, 17, 6, 58))
    clock.install(monkeypatch, cron_mod)
    clock.install(monkeypatch, fields_mod)  # the job's created_ts
    await svc.add_job_async(
        "half-hourly", "msg", cron_expr="7,37 * * * *", timezone="UTC", strict_schedule=True
    )
    job_id = svc._jobs[0].id
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            for step in (0, 65, 37):  # ticks at 17:06:58, 17:08:03 and 17:08:40
                clock.advance(step)
                await svc._on_timer()
                claim = svc._claims.get(job_id)
                if claim is not None and claim.task is not None:
                    await asyncio.wait_for(claim.task, timeout=30)
    finally:
        await svc.stop()
    assert runs == [_utc(2026, 11, 6, 17, 8, 3)]


@pytest.mark.parametrize(
    ("since", "fields"),
    [
        pytest.param(_utc(2026, 11, 6, 16, 58, 2), {}, id="ticks-more-than-the-grace-apart"),
        pytest.param(
            _utc(2026, 11, 6, 17, 6, 58),
            {"created_ts": _utc(2026, 11, 6, 17, 7, 30)},
            id="created-after-the-minute",
        ),
        pytest.param(
            _utc(2026, 11, 6, 17, 6, 58),
            {"last_run_ts": _utc(2026, 11, 6, 17, 7, 40)},
            id="ran-after-the-minute",
        ),
    ],
)
def test_a_late_tick_leaves_a_skipped_minute_outside_its_bounds(
    since: float, fields: dict[str, float]
) -> None:
    """17:07 lies between the ticks, but the gap is too long or the job could not run it."""
    assert not is_due(_job("7,37 * * * *", **fields), _utc(2026, 11, 6, 17, 8, 3), since)


def test_a_late_tick_does_not_rerun_a_spring_forward_occurrence() -> None:
    """New York's ``30 2 * * *`` matches 07:29Z and 07:30Z on a spring-forward day: one occurrence.

    It ran in 07:29Z; the tick at 07:31:50Z must not run the skipped 07:30Z again.
    """
    job = _job("30 2 * * *", tz="America/New_York", last_run_ts=_utc(2026, 3, 8, 7, 29, 6))
    assert not is_due(job, _utc(2026, 3, 8, 7, 31, 50), _utc(2026, 3, 8, 7, 29, 31))
