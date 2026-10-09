"""A delete_after_run one-shot must run once when its result merge meets a busy store.

``_run_job_isolated`` hands a finished run to ``_merge_job_result``, whose first
step is the bounded store lock. When that lock stays contended the merge raises
``CronStoreBusy`` before it can consume the one-shot. ``_execute`` leaves a
``delete_after_run`` at-job enabled on purpose (the consume is what stops it), so
the finalizer must hand the owed consume to ``defer_removal``: the job is
disabled in memory at once and deleted by the next tick that wins the lock.
Without that, the job stays enabled and due, and every tick runs it again.

Contention is simulated by making the store lock raise the exception the real
spin raises; the real ``_on_timer``, ``_run_job_isolated`` and
``_merge_job_result`` run unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kiro_crew.cron as cron_mod
from kiro_crew.cron import CronJob, CronService, CronStoreBusy

_ADMITTED = SimpleNamespace(admitted=True, reason="")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_api_access(self, **event) -> None:
        self.events.append(event)


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    value = _Recorder()
    monkeypatch.setattr(cron_mod, "sel", SimpleNamespace(sel=lambda: value))
    return value


class _BusyStore:
    """While ``on``, the store lock raises CronStoreBusy.

    ``merges_only`` limits the contention to the result merge's own lock
    attempt; otherwise every attempt loses, the timer tick's included.
    """

    def __init__(self, service: CronService, *, merges_only: bool) -> None:
        self.on = False
        self._merging = False
        real_lock = service._file_lock
        real_merge = service._merge_job_result

        def lock(*args, **kwargs):
            if self.on and (self._merging or not merges_only):
                raise CronStoreBusy("simulated: another writer held the store lock")
            return real_lock(*args, **kwargs)

        def merge(terminal: CronJob) -> None:
            self._merging = True
            try:
                real_merge(terminal)
            finally:
                self._merging = False

        service._file_lock = lock  # type: ignore[method-assign]
        service._merge_job_result = merge  # type: ignore[method-assign]


async def _service(tmp_path, runs: list[str], *, flag: str | None = None) -> CronService:
    """A started service whose timer only fires when the test ticks it."""

    async def on_job(job: CronJob) -> None:
        runs.append(job.id)
        if flag is not None:
            setattr(job, flag, True)

    service = CronService(base_dir=tmp_path, on_job=on_job)
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


def _one_shot_removals(recorder: _Recorder) -> list[dict]:
    return [e for e in recorder.events if e.get("outcome") == "one_shot_completed"]


@pytest.mark.asyncio
async def test_one_shot_runs_once_when_its_result_merge_hits_a_busy_store(tmp_path, recorder):
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await service.add_job_async(
        "remind-once", "msg", at_ts=time.time() - 1, delete_after_run=True
    )
    store = _BusyStore(service, merges_only=True)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.on = True
            await _tick(service, job.id)  # fires; its merge cannot lock the store
            store.on = False
            await _tick(service, job.id)  # the store is free again
    finally:
        store.on = False
        await service.stop()

    assert runs == [job.id], f"one-shot at-job ran {len(runs)} times"
    assert CronService(base_dir=tmp_path).get_job(job.id) is None, "consumed one-shot left on disk"
    removals = _one_shot_removals(recorder)
    assert [e["resources"] for e in removals] == [f"job_id={job.id} path=cron_deferred_drain"]


@pytest.mark.asyncio
async def test_one_shot_does_not_refire_while_the_store_stays_busy(tmp_path, recorder):
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await service.add_job_async(
        "remind-once", "msg", at_ts=time.time() - 1, delete_after_run=True
    )
    store = _BusyStore(service, merges_only=False)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.on = True
            for _ in range(3):  # three ticks, at most 30 s apart in production
                await _tick(service, job.id)
            store.on = False
            await _tick(service, job.id)  # the first tick that wins the lock
    finally:
        store.on = False
        await service.stop()

    assert runs == [job.id], f"one-shot at-job ran {len(runs)} times in 4 ticks"
    assert CronService(base_dir=tmp_path).get_job(job.id) is None, "consumed one-shot left on disk"
    assert len(_one_shot_removals(recorder)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delete_after_run", "flag"),
    [(False, None), (True, "run_never_started"), (True, "fire_time_denied")],
)
async def test_busy_merge_keeps_a_one_shot_that_owes_no_delete(
    tmp_path, recorder, delete_after_run, flag
):
    # Same rule as the consume inside _merge_job_result: a plain one-shot is
    # kept (disabled), and a run that never started or was denied at fire time
    # owes no delete. A busy merge must not turn any of them into a removal.
    runs: list[str] = []
    service = await _service(tmp_path, runs, flag=flag)
    job = await service.add_job_async(
        "remind-once", "msg", at_ts=time.time() - 1, delete_after_run=delete_after_run
    )
    store = _BusyStore(service, merges_only=True)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.on = True
            await _tick(service, job.id)
    finally:
        store.on = False
        await service.stop()

    assert runs == [job.id]
    assert job.id not in service._pending_removals
    assert CronService(base_dir=tmp_path).get_job(job.id) is not None
    assert _one_shot_removals(recorder) == []
