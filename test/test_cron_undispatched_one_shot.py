"""A one-shot whose fire never started stays scheduled and retries.

The gateway refuses some fires before anything runs (admission closed during a
drain, another run of the job in flight, a session held under another agent),
through ``_defer_cron_before_dispatch``. Its contract is to retain the run
without counting success or failure, and ``run_never_started`` marks it. A
one-shot that keeps its row must then stay enabled and run once dispatch is
possible, the same as a ``delete_after_run`` one-shot, whose consume the result
merge already suppresses. A fire-time denial is a policy decision, not a fire
that could not start, so it is still parked.

The real ``_defer_cron_before_dispatch`` is called from the job callback; the
real ``_on_timer``, ``_run_job_isolated``, ``_execute``, ``_merge_job_result``
and ``apply_run_record`` run unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.cron import CronJob, CronService
from kiro_crew.slack.gateway_runtime.cron_dispatch import _defer_cron_before_dispatch

_ADMITTED = SimpleNamespace(admitted=True, reason="")


class _Gateway:
    """A job callback that refuses to dispatch while ``refusing`` is set."""

    def __init__(self) -> None:
        self.refusing = True
        self.ran: list[str] = []
        self.during_refusal = None

    async def on_job(self, job: CronJob) -> None:
        if self.refusing:
            if self.during_refusal is not None:
                await self.during_refusal(job)
            _defer_cron_before_dispatch(job, "gateway admission is closed")
            return
        self.ran.append(job.id)


async def _service(tmp_path, gateway: _Gateway) -> CronService:
    """A started service whose timer only fires when the test ticks it."""
    service = CronService(base_dir=tmp_path, on_job=gateway.on_job)
    await service.start()
    service._arm_timer = lambda: None  # type: ignore[method-assign]
    task, service._timer_task = service._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return service


async def _tick(service: CronService, job_id: str) -> None:
    """One timer tick, then wait for the run it dispatched, if any."""
    await service._on_timer()
    claim = service._claims.get(job_id)
    if claim is not None and claim.task is not None:
        await asyncio.wait_for(claim.task, timeout=5.0)


def _stored(tmp_path, job_id: str) -> CronJob:
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_after_run", [False, True], ids=["plain", "delete_after_run"])
async def test_a_refused_one_shot_stays_enabled_and_runs_once_dispatch_opens(
    tmp_path, delete_after_run
):
    gateway = _Gateway()
    service = await _service(tmp_path, gateway)
    job = await service.add_job_async(
        "remind-once", "msg", at_ts=time.time() - 1, delete_after_run=delete_after_run
    )
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _tick(service, job.id)  # refused before dispatch
            stored = _stored(tmp_path, job.id)
            assert stored.enabled and not stored.user_paused, "a refused one-shot was parked"
            assert service._is_due(stored, time.time()), "a refused one-shot is not due again"
            gateway.refusing = False
            await _tick(service, job.id)  # dispatch is possible again
            await _tick(service, job.id)
    finally:
        await service.stop()

    assert gateway.ran == [job.id], f"the one-shot's payload ran {len(gateway.ran)} times"
    remaining = CronService(base_dir=tmp_path).get_job(job.id)
    if delete_after_run:
        assert remaining is None, "the one-shot was not consumed after it ran"
    else:
        assert remaining is not None and not remaining.enabled, "the one-shot was not parked"


@pytest.mark.asyncio
@pytest.mark.parametrize("never_started", [False, True], ids=["verdict", "never-started-too"])
async def test_a_fire_time_denied_one_shot_is_still_parked(tmp_path, never_started):
    async def deny(job: CronJob) -> None:
        job.last_status = "error"
        job.last_error = "fire-time policy denied the job"
        job.fire_time_denied = True
        job.run_never_started = never_started

    service = await _service(tmp_path, _Gateway())
    service._on_job = deny  # type: ignore[assignment]
    job = await service.add_job_async("remind-once", "msg", at_ts=time.time() - 1)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _tick(service, job.id)
    finally:
        await service.stop()

    stored = _stored(tmp_path, job.id)
    assert not stored.enabled and stored.user_paused, "a denied one-shot was left due"


@pytest.mark.asyncio
async def test_a_pause_made_while_the_fire_was_refused_is_kept(tmp_path):
    # The merge must not write the refused run's `enabled` over the store: the
    # user paused the job while the gateway was refusing its fire.
    gateway = _Gateway()
    service = await _service(tmp_path, gateway)
    job = await service.add_job_async("remind-once", "msg", at_ts=time.time() - 1)

    async def pause(_job: CronJob) -> None:
        assert await service.enable_job_async(job.id, False)

    gateway.during_refusal = pause
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _tick(service, job.id)
    finally:
        await service.stop()

    stored = _stored(tmp_path, job.id)
    assert not stored.enabled and stored.user_paused, "the user's pause was overwritten"
    assert gateway.ran == []
