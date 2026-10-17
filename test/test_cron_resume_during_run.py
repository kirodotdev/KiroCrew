"""A ``cron resume`` issued while a run of the job is in flight must stick.

A run takes its own copy of the job when it starts, so a run of an auto-paused
job starts with ``auto_paused=True``. When the owner resumes the job while that
run is in flight, the store holds ``auto_paused=False`` and a fresh failure
count. The run's terminal write -- its result merge, or the reaper's timeout
record for a hung run -- must not put the old pause, or a failure the resumed
job never spent, back over that resume.

The resume comes from a second ``CronService`` on the same directory, run in a
worker thread like the CLI's separate process, so the merge's store reload is
real and the store copy it writes to is not the run's own object.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronService
from kiro_crew.testing.wait import await_until


def _resume_from_another_process(tmp_path, job_id: str) -> None:
    assert CronService(base_dir=tmp_path).enable_job(job_id, enabled=True) is True


def _stored(tmp_path, job_id: str) -> CronJob:
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None
    return job


def _job_with_failures(svc: CronService, failures: int) -> CronJob:
    """A recurring job whose store copy carries ``failures`` consecutive failures."""
    job = svc.add_job(name="flaky", message="boom", every_secs=3600)
    for _ in range(failures):
        job.record_failure()
    svc._merge_job_result(job)
    return job


async def _manual_run(svc: CronService, job_id: str) -> bool:
    """Run the job by hand, as the manual-run route does, to its end."""
    return await await_until(asyncio.ensure_future(svc.run_job(job_id)), what="the manual run")


async def _failing_run(j: CronJob) -> None:
    # The gateway callback's contract: count the failure, then raise.
    j.record_failure()
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_a_failed_run_does_not_undo_a_resume_issued_during_it(tmp_path) -> None:
    async def resumed_then_failed(j: CronJob) -> None:
        await asyncio.to_thread(_resume_from_another_process, tmp_path, j.id)
        await _failing_run(j)

    svc = CronService(base_dir=tmp_path, on_job=resumed_then_failed)
    job = _job_with_failures(svc, _AUTO_PAUSE_THRESHOLD)
    assert _stored(tmp_path, job.id).auto_paused is True

    assert await _manual_run(svc, job.id) is True

    stored = _stored(tmp_path, job.id)
    assert stored.last_status == "error", "the run's own outcome still lands"
    assert (stored.auto_paused, stored.enabled) == (False, True), "the resume was undone"
    assert stored.consecutive_failures == 0, "the resume's fresh attempts were spent"


@pytest.mark.asyncio
async def test_a_failed_run_without_a_resume_keeps_the_job_paused(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path, on_job=_failing_run)
    job = _job_with_failures(svc, _AUTO_PAUSE_THRESHOLD)

    assert await _manual_run(svc, job.id) is True

    stored = _stored(tmp_path, job.id)
    assert (stored.auto_paused, stored.enabled) == (True, False)
    assert stored.consecutive_failures == _AUTO_PAUSE_THRESHOLD + 1


@pytest.mark.asyncio
async def test_the_failure_that_reaches_the_threshold_still_pauses(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path, on_job=_failing_run)
    job = _job_with_failures(svc, _AUTO_PAUSE_THRESHOLD - 1)

    assert await _manual_run(svc, job.id) is True

    stored = _stored(tmp_path, job.id)
    assert (stored.auto_paused, stored.enabled) == (True, False)
    assert stored.consecutive_failures == _AUTO_PAUSE_THRESHOLD


@pytest.mark.asyncio
async def test_the_reaper_does_not_count_a_hung_run_against_a_resume_issued_during_it(
    tmp_path,
) -> None:
    entered = asyncio.Event()
    never = asyncio.Event()

    async def hangs(j: CronJob) -> None:
        entered.set()
        await never.wait()

    svc = CronService(base_dir=tmp_path, on_job=hangs)
    job = _job_with_failures(svc, _AUTO_PAUSE_THRESHOLD)
    run = asyncio.ensure_future(svc.run_job(job.id))
    try:
        await await_until(entered, what="the run reaching its payload")
        claim = svc._claims[job.id]
        await asyncio.to_thread(_resume_from_another_process, tmp_path, job.id)

        with patch("kiro_crew.sel.sel"):
            await svc._force_reap(job.id, 9999.0, claim=claim)
    finally:
        run.cancel()
        await await_until(asyncio.gather(run, return_exceptions=True), what="the reaped run")

    stored = _stored(tmp_path, job.id)
    assert stored.last_status == "error"
    assert "Reaped" in (stored.last_error or "")
    assert (stored.auto_paused, stored.enabled) == (False, True)
    assert stored.consecutive_failures == 0, "the hung run spent one of the resume's attempts"


@pytest.mark.asyncio
async def test_a_failed_run_does_not_undo_a_resume_issued_in_the_same_process(tmp_path) -> None:
    # The dashboard's resume runs in the gateway's own process, so it mutates
    # the very job object the run is executing on, not a store copy.
    async def resumed_then_failed(j: CronJob) -> None:
        assert await svc.enable_job_async(j.id, True) is True
        await _failing_run(j)

    svc = CronService(base_dir=tmp_path, on_job=resumed_then_failed)
    job = _job_with_failures(svc, _AUTO_PAUSE_THRESHOLD)
    assert _stored(tmp_path, job.id).auto_paused is True

    assert await _manual_run(svc, job.id) is True

    stored = _stored(tmp_path, job.id)
    assert stored.last_status == "error", "the run's own outcome still lands"
    assert (stored.auto_paused, stored.enabled) == (False, True), "the resume was undone"
    assert stored.consecutive_failures == 0, "the resume's fresh attempts were spent"


@pytest.mark.asyncio
async def test_a_run_that_lifts_its_own_pause_still_counts_its_failure(tmp_path) -> None:
    # A Run Now on an auto-paused job whose gate verdict records a success
    # (lifting the pause) and whose delivery then fails: the pause was lifted
    # by the run, not from outside it, so the failure is this run's to count.
    async def succeeded_then_failed(j: CronJob) -> None:
        j.record_success()
        await _failing_run(j)

    svc = CronService(base_dir=tmp_path, on_job=succeeded_then_failed)
    job = _job_with_failures(svc, _AUTO_PAUSE_THRESHOLD)
    assert _stored(tmp_path, job.id).auto_paused is True

    assert await _manual_run(svc, job.id) is True

    stored = _stored(tmp_path, job.id)
    assert stored.last_status == "error"
    assert (stored.auto_paused, stored.enabled) == (False, True)
    assert stored.consecutive_failures == 1, "the run's own failure was not counted"
