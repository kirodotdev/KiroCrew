"""Regression: cancel_all() on a remote-update job whose task has not taken its
first step must still settle the job.

The CancelledError is thrown into an unstarted coroutine, so _run()'s
``except CancelledError: job.fail(...)`` never executes. Before the fix the job
stayed ``running`` forever and every later update click for that instance was
refused (``start`` returns None while a job is running).
"""

import asyncio

import pytest

from kiro_crew.instances.remote_update import RemoteUpdateJobs


@pytest.mark.asyncio
async def test_cancel_all_right_after_start_reaches_a_terminal_state():
    jobs = RemoteUpdateJobs()
    body_ran = False

    async def _body(job):
        nonlocal body_ran
        body_ran = True
        await asyncio.Event().wait()

    job = jobs.start("box", _body)
    assert job is not None
    jobs.cancel_all()  # e.g. shutdown() ran before the job task's first step

    # Let the cancelled task fully settle.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not jobs._tasks, "task should be done and discarded"
    assert body_ran is False

    # cancel_all is "gateway shutdown" and the job must end failed with the
    # documented message, not stay stuck.
    assert job.state == "failed", f"job stuck in {job.state!r} phase={job.phase!r}"
    assert "shut down" in job.error


@pytest.mark.asyncio
async def test_stuck_job_blocks_every_later_click():
    jobs = RemoteUpdateJobs()

    async def _body(job):
        job.finish("succeeded")

    jobs.start("box", _body)
    jobs.cancel_all()
    for _ in range(5):
        await asyncio.sleep(0)
    # No task is running for "box"; start() must not refuse as "already running".
    assert jobs.start("box", _body) is not None, "orphaned running job wedges the instance"
