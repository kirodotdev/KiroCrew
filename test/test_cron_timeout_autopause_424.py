"""Regression: cron timeouts must count toward auto-pause (#424).

The ``asyncio.TimeoutError`` handler in ``_execute_with_timeout`` reset
``consecutive_failures`` to 0 on every timeout, so a job that timed out on
every run never accumulated toward ``_AUTO_PAUSE_THRESHOLD`` — it ran forever
with zero user signal. Timeouts must now increment the counter and eventually
auto-pause.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import cron as cron_mod
from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronService


@pytest.mark.asyncio
async def test_repeated_timeouts_accumulate_and_autopause(tmp_path, monkeypatch):
    svc = CronService(base_dir=tmp_path)
    job = CronJob(id="j1", name="slow", message="hi", timeout_secs=1)

    async def _timeout_immediately(coro, timeout):
        coro.close()  # avoid "coroutine was never awaited" warning
        raise asyncio.TimeoutError

    monkeypatch.setattr(cron_mod.asyncio, "wait_for", _timeout_immediately)

    for i in range(_AUTO_PAUSE_THRESHOLD):
        assert not job.auto_paused
        await svc._execute_with_timeout(job)
        assert job.consecutive_failures == i + 1
        assert job.last_status == "error"

    # Threshold reached: the job auto-paused instead of running forever silently.
    assert job.auto_paused is True
    assert job.enabled is False


@pytest.mark.asyncio
async def test_timeout_clears_failure_dedup_hash(tmp_path, monkeypatch):
    svc = CronService(base_dir=tmp_path)
    job = CronJob(id="j2", name="slow", message="hi", timeout_secs=1)
    job.last_failure_hash = "stale"
    job.last_failure_at = 123.0

    async def _timeout_immediately(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(cron_mod.asyncio, "wait_for", _timeout_immediately)
    await svc._execute_with_timeout(job)
    # Dedup state cleared so a later distinct error isn't suppressed...
    assert job.last_failure_hash == ""
    assert job.last_failure_at == 0.0
    # ...but the failure still counted.
    assert job.consecutive_failures == 1


@pytest.mark.asyncio
async def test_timeout_after_recorded_failure_counts_once(tmp_path, monkeypatch):
    """One failed run is one failure, even when two handlers observe it.

    A delivery-path exception records the failure inside the callback; if the
    run then overruns its deadline during cleanup, the TimeoutError handler
    must not count the same run a second time.
    """
    svc = CronService(base_dir=tmp_path)
    job = CronJob(id="j3", name="slow", message="hi", timeout_secs=1)

    async def _fail_then_timeout(coro, timeout):
        coro.close()
        # The callback counted this run's failure before cleanup overran.
        job.record_failure()
        raise asyncio.TimeoutError

    monkeypatch.setattr(cron_mod.asyncio, "wait_for", _fail_then_timeout)
    await svc._execute_with_timeout(job)
    assert job.consecutive_failures == 1
    assert job.last_status == "error"
