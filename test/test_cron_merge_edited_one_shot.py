"""A one-shot another writer made recurring keeps its new schedule after its run ends.

A finished run's record carries the schedule that fired. When the stored row
holds a different one by the time the run lands, the fired one-shot's park and
its ``delete_after_run`` consume belong to the old schedule: the result merge
takes the run's result and generation and leaves the row enabled, and a consume
queued for the next tick (a busy store refused the merge) is dropped, whether or
not the run's record is still held. A one-shot made recurring also stops being
deleted after a run, so its next interval run keeps it too.

The other writer is a second ``CronService`` on the same directory, so the edit
takes the ordinary locked reload and write path. The real ``update_job``,
``_run_job_isolated``, ``_merge_job_result``, ``_tick_scan_locked``, ``_sync`` and
``_load`` run unmodified; a busy store is the lock raising what its spin raises.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from test_cron_merge_result_busy_store import _ADMITTED, _BusyStore, _service, _tick

from kiro_crew.cron import CronJob, CronService

_AT = 1_800_000_000.0


def _one_shot(service: CronService, *, delete_after_run: bool) -> CronJob:
    return service.add_job(
        name="reminder", message="go", at_ts=_AT, timezone="UTC", delete_after_run=delete_after_run
    )


def _finished_run(job: CronJob, *, enabled: bool) -> CronJob:
    """The record a completed run of ``job`` hands the merge (``close_run``'s copy)."""
    record = copy.copy(job)
    record.schedule = copy.copy(job.schedule)
    record.last_run_ts = _AT + 1
    record.last_status = "ok"
    record.enabled = enabled
    record.run_generation = 1
    return record


def _stored(tmp_path: Path, job_id: str) -> CronJob | None:
    return CronService(base_dir=tmp_path).get_job(job_id)


_MERGED = (1, "ok", _AT + 1)


@pytest.mark.parametrize(
    ("delete_after_run", "every_secs", "expected"),
    [
        (False, None, ("at", False, True, *_MERGED)),
        (True, None, None),
        (False, 3600, ("every", True, False, *_MERGED)),
        (True, 3600, ("every", True, False, *_MERGED)),
    ],
    ids=["plain-parked", "consumed", "plain-made-recurring", "consumable-made-recurring"],
)
def test_a_fired_one_shot_merge_keeps_a_schedule_saved_since(
    tmp_path: Path, delete_after_run: bool, every_secs: int | None, expected: tuple | None
) -> None:
    service = CronService(base_dir=tmp_path)
    job = _one_shot(service, delete_after_run=delete_after_run)
    record = _finished_run(job, enabled=False)
    if every_secs is not None:
        assert CronService(base_dir=tmp_path).update_job(job.id, every_secs=every_secs)

    service._merge_job_result(record)

    stored = _stored(tmp_path, job.id)
    assert expected == (
        None
        if stored is None
        else (
            stored.schedule.kind,
            stored.enabled,
            stored.user_paused,
            stored.run_generation,
            stored.last_status,
            stored.last_run_ts,
        )
    )


@pytest.mark.parametrize(
    ("delete_after_run", "editor", "expected"),
    [
        (True, "none", None),
        (True, "same-service", ("every", True, True)),
        (True, "other-service", ("every", True, True)),
        # A Done() removal is the script's own request, not the fired schedule's.
        (False, "other-service", None),
    ],
    ids=["consumed", "same-service-edit", "other-service-edit", "done-removal"],
)
@pytest.mark.asyncio
async def test_a_consume_queued_by_a_busy_merge_keeps_a_schedule_saved_since(
    tmp_path: Path, delete_after_run: bool, editor: str, expected: tuple | None
) -> None:
    service = await _service(tmp_path, [])
    job = await service.add_job_async(
        "reminder", "go", at_ts=time.time() - 1, delete_after_run=delete_after_run
    )
    store = _BusyStore(service, merges_only=True)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            # The run's merge cannot lock the store, so its finalizer holds the record
            # and queues the consume. The Done() path queues its removal alone.
            store.on = True
            await _tick(service, job.id)
            store.on = False
            if not delete_after_run:
                service.defer_removal(job.id)
            # A tick that drained before the queue saves and releases the record.
            with service._file_lock():
                service._sync()
                service._save_held_run_records_locked()
            if editor != "none":
                writer = service if editor == "same-service" else CronService(base_dir=tmp_path)
                assert writer.update_job(job.id, every_secs=3600)
            service._tick_scan_locked()
            listed = service.get_job(job.id)
    finally:
        store.on = False
        await service.stop()

    stored = _stored(tmp_path, job.id)
    assert expected == (
        None
        if stored is None or listed is None
        else (stored.schedule.kind, stored.enabled, listed.enabled)
    )


def test_a_one_shot_made_recurring_survives_its_next_run(tmp_path: Path) -> None:
    service = CronService(base_dir=tmp_path)
    job = _one_shot(service, delete_after_run=True)
    edited = service.update_job(job.id, every_secs=3600)
    assert edited is not None

    service._merge_job_result(_finished_run(edited, enabled=True))

    stored = _stored(tmp_path, job.id)
    assert stored is not None
    assert (stored.schedule.kind, stored.enabled, stored.run_generation) == ("every", True, 1)
