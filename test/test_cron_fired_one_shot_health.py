"""A failed one-shot its own run parked stays a health signal.

A fired ``at`` job that keeps its row is parked with the same flags a user pause
writes (``enabled`` False, ``user_paused`` True). The doctor's store readers
skip a user pause, so without telling the two apart a failed one-shot was
reported as "paused by the user" and dropped from the errored bucket. A user's
own pause of a job that has run must still read as a user pause. The real
``_on_timer``, ``_run_job_isolated`` and merge run here; only the timer arm is
replaced so the test ticks it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kiro_crew.cron as cron_mod
from kiro_crew.cron import CronJob, CronService
from kiro_crew.testing.wait import await_until

_ADMITTED = SimpleNamespace(admitted=True, reason="")
_QUIET_AUDIT = SimpleNamespace(sel=lambda: SimpleNamespace(log_api_access=lambda **_: None))


async def _service(tmp_path: Path) -> CronService:
    """A started service whose jobs all fail and whose timer only fires when ticked."""

    async def on_job(job: CronJob) -> None:
        raise RuntimeError("agent failed")

    service = CronService(base_dir=tmp_path, on_job=on_job)
    await service.start()
    service._arm_timer = lambda: None  # type: ignore[method-assign]
    task, service._timer_task = service._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return service


async def _fire(service: CronService, job_id: str) -> None:
    with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
        await service._on_timer()
        claim = service._claims.get(job_id)
        if claim is not None and claim.task is not None:
            await await_until(claim.task, what="the one-shot's run")


@pytest.mark.asyncio
async def test_a_failed_fired_one_shot_is_errored_and_a_user_pause_is_not(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cron_mod, "sel", _QUIET_AUDIT)
    service = await _service(tmp_path)
    try:
        fired = await service.add_job_async("fired", "m", at_ts=time.time() - 1)
        paused = await service.add_job_async("paused", "m", at_ts=time.time() + 3600)
        await service.enable_job_async(paused.id, False)
        await _fire(service, fired.id)
    finally:
        await service.stop()

    stored = CronService(base_dir=tmp_path).get_job(fired.id)
    assert stored is not None and stored.enabled is False, "the fired one-shot was not parked"
    monkeypatch.setattr(cron_mod, "config_dir", lambda: tmp_path)
    auto_paused, errored, loadable = cron_mod.unhealthy_jobs_from_disk()
    assert (auto_paused, errored, loadable) == ([], [(fired.id, "fired")], True)
    assert cron_mod.job_pause_state_from_disk(fired.id) == "parked after its one-shot run"
    assert cron_mod.job_pause_state_from_disk(paused.id) == "paused by the user"


@pytest.mark.asyncio
async def test_a_user_pause_after_a_run_stays_a_user_pause(tmp_path, monkeypatch) -> None:
    # The job has run (its `last_run_ts` is set) and the user then resumes and
    # pauses it. That pause is the user's, so it is not reported as a failure.
    monkeypatch.setattr(cron_mod, "sel", _QUIET_AUDIT)
    service = await _service(tmp_path)
    try:
        job = await service.add_job_async("fired", "m", at_ts=time.time() - 1)
        await _fire(service, job.id)
        await service.enable_job_async(job.id, True)
        await service.enable_job_async(job.id, False)
    finally:
        await service.stop()

    stored = CronService(base_dir=tmp_path).get_job(job.id)
    assert stored is not None and stored.last_run_ts is not None
    monkeypatch.setattr(cron_mod, "config_dir", lambda: tmp_path)
    assert cron_mod.unhealthy_jobs_from_disk() == ([], [], True)
    assert cron_mod.job_pause_state_from_disk(job.id) == "paused by the user"


@pytest.mark.asyncio
async def test_a_resume_after_a_reload_clears_the_pause_marker(tmp_path, monkeypatch) -> None:
    # The marker a pause saved is read back by a fresh service, and the resume
    # there must clear it on disk, or the one-shot's later park reads as a pause.
    monkeypatch.setattr(cron_mod, "sel", _QUIET_AUDIT)
    first = await _service(tmp_path)
    try:
        job = await first.add_job_async("held", "m", at_ts=time.time() - 1)
        await first.enable_job_async(job.id, False)
    finally:
        await first.stop()

    second = await _service(tmp_path)
    try:
        await second.enable_job_async(job.id, True)
        await _fire(second, job.id)
    finally:
        await second.stop()

    monkeypatch.setattr(cron_mod, "config_dir", lambda: tmp_path)
    assert cron_mod.unhealthy_jobs_from_disk() == ([], [(job.id, "held")], True)
    assert cron_mod.job_pause_state_from_disk(job.id) == "parked after its one-shot run"
