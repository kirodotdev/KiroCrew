"""An unsatisfiable cron expression warns once, not on every timer re-arm."""

from __future__ import annotations

import logging
import time

import pytest

import kiro_crew.cron as cron
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.cron_service import schedule


def _job(expr: str) -> CronJob:
    return CronJob(
        id="bad-expr-job",
        name="n",
        message="m",
        timezone="UTC",
        schedule=CronSchedule(kind="cron", cron_expr=expr),
    )


def _boundary_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Bad cron")]


def test_bad_expression_warns_once_without_traceback_and_good_one_clears(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "_BAD_CRON_WARNED", {})
    caplog.set_level(logging.WARNING, logger="kiro_crew.cron")
    now = time.time()
    bad = _job("0 0 30 2 *")  # Feb 30 never happens

    assert cron._next_cron_boundary_ts(bad, now) is None
    assert cron._next_cron_boundary_ts(bad, now + 30) is None

    records = _boundary_warnings(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert "0 0 30 2 *" in records[0].getMessage()

    # A different bad expression on the same job warns again.
    assert cron._next_cron_boundary_ts(_job("0 0 31 2 *"), now) is None
    assert len(_boundary_warnings(caplog)) == 2

    good = _job("*/5 * * * *")
    assert cron._next_cron_boundary_ts(good, now) is not None
    assert hash("bad-expr-job") not in schedule._BAD_CRON_WARNED

    # After a good evaluation, a new bad expression warns again (once).
    assert cron._next_cron_boundary_ts(bad, now) is None
    assert cron._next_cron_boundary_ts(bad, now + 30) is None
    assert len(_boundary_warnings(caplog)) == 3


def test_full_memo_refuses_new_rows_and_says_so_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "_BAD_CRON_WARNED", {})
    monkeypatch.setattr(schedule, "_BAD_CRON_WARNED_MAX", 2)
    monkeypatch.setattr(schedule, "_BAD_CRON_REFUSED", [0])
    caplog.set_level(logging.WARNING, logger="kiro_crew.cron")
    now = time.time()
    jobs = [_job("0 0 30 2 *") for _ in range(3)]
    for n, job in enumerate(jobs):
        job.id = f"job-{n}"
    for _poll in range(3):  # three timer re-arms over the same three bad jobs
        for job in jobs:
            assert cron._next_cron_boundary_ts(job, now) is None
            assert len(schedule._BAD_CRON_WARNED) <= 2

    # Two admitted jobs warn once each; the refused one never gets a row.
    assert len(_boundary_warnings(caplog)) == 3  # 2 job warnings + 1 "memo is full"
    assert hash("job-2") not in schedule._BAD_CRON_WARNED
    assert schedule._BAD_CRON_REFUSED == [3]
    schedule._BAD_CRON_REFUSED[0] = 2**31
    assert cron._next_cron_boundary_ts(jobs[2], now) is None
    assert schedule._BAD_CRON_REFUSED == [2**31]  # saturates


def test_memo_retains_fixed_size_values_not_the_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule, "_BAD_CRON_WARNED", {})
    job = _job("0 0 30 2 *")
    job.id = "x" * 100_000
    assert cron._next_cron_boundary_ts(job, time.time()) is None
    assert len(schedule._BAD_CRON_WARNED) == 1
    assert all(
        isinstance(k, int) and isinstance(v, int) for k, v in schedule._BAD_CRON_WARNED.items()
    )
