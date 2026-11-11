"""A failed one-shot its own run parked stays a health signal.

A fired ``at`` job that keeps its row is parked with the same flags a user pause
writes (``enabled`` False, ``user_paused`` True). The doctor's store readers
skip a user pause, so without telling the two apart a failed one-shot was
reported as "paused by the user" and dropped from the errored bucket. The real
``_on_timer``, ``_run_job_isolated`` and merge run here; only the timer arm is
replaced so the test ticks it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kiro_crew.cron as cron_mod
from kiro_crew.cron import CronJob, CronService
from kiro_crew.testing.wait import await_until

_ADMITTED = SimpleNamespace(admitted=True, reason="")


_QUIET_AUDIT = SimpleNamespace(sel=lambda: SimpleNamespace(log_api_access=lambda **_: None))


@pytest.mark.asyncio
async def test_a_failed_fired_one_shot_is_errored_and_a_user_pause_is_not(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cron_mod, "sel", _QUIET_AUDIT)

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
    try:
        fired = await service.add_job_async("fired", "m", at_ts=time.time() - 1)
        paused = await service.add_job_async("paused", "m", at_ts=time.time() + 3600)
        await service.enable_job_async(paused.id, False)
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await service._on_timer()
            claim = service._claims.get(fired.id)
            if claim is not None and claim.task is not None:
                await await_until(claim.task, what="the one-shot's run")
    finally:
        await service.stop()

    stored = CronService(base_dir=tmp_path).get_job(fired.id)
    assert stored is not None and stored.enabled is False, "the fired one-shot was not parked"
    monkeypatch.setattr(cron_mod, "config_dir", lambda: tmp_path)
    auto_paused, errored, loadable = cron_mod.unhealthy_jobs_from_disk()
    assert (auto_paused, errored, loadable) == ([], [(fired.id, "fired")], True)
    assert cron_mod.job_pause_state_from_disk(fired.id) == "parked after its one-shot run"
    assert cron_mod.job_pause_state_from_disk(paused.id) == "paused by the user"
