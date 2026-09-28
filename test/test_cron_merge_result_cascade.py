"""Child release must cascade when a one-shot is consumed by _merge_job_result.

Removing a cron retires its principal ``cron:<job id>``. Every locked removal
core releases the jobs that cron owned in the same save. ``_merge_job_result``
consumes a completed ``delete_after_run`` one-shot inline, so it is a removal
core too: a child cron created during that job's own run must have its owner
cleared in the same save, or it strands as owned by a principal that runs of a
deleted job can never present again.
"""

from __future__ import annotations

import pytest

from kiro_crew.cron import CronService, CronStoreUnreadable


def _completed_one_shot(service: CronService) -> object:
    """An 'at' delete_after_run job that has already fired successfully."""
    job = service.add_job("parent", "run", at_ts=1.0, delete_after_run=True)
    job.last_status = "ok"
    job.last_run_ts = 100.0
    job.fire_time_denied = False
    job.run_never_started = False
    return job


def test_merge_job_result_releases_child_of_consumed_one_shot(tmp_path):
    service = CronService(base_dir=tmp_path)
    parent = _completed_one_shot(service)
    child = service.add_job("child", "run", every_secs=3600, session_key=f"cron:{parent.id}")

    service._merge_job_result(parent)

    assert service.get_job(parent.id) is None, "one-shot parent was not consumed"
    surviving_child = service.get_job(child.id)
    assert surviving_child is not None, "child must be released, not deleted"
    assert surviving_child.session_key == "", (
        "child owned by the consumed one-shot must be released to ownerless, "
        "not left stranded under a cron principal that runs can never present"
    )


def test_merge_job_result_rolls_back_child_release_on_save_failure(tmp_path, monkeypatch):
    service = CronService(base_dir=tmp_path)
    parent = _completed_one_shot(service)
    child = service.add_job("child", "run", every_secs=3600, session_key=f"cron:{parent.id}")

    def boom() -> None:
        raise CronStoreUnreadable("store unreadable")

    monkeypatch.setattr(service, "_save", boom)

    # An unreadable store must not surface as a run-path crash; the consume is
    # deferred and retried later.
    service._merge_job_result(parent)

    # The save never landed, so the child's ownership must be exactly what it
    # was before the attempt -- not silently cleared in memory.
    assert child.session_key == f"cron:{parent.id}"
    assert parent.id in service._pending_removals


def test_merge_job_result_queues_consume_on_non_unreadable_save_failure(tmp_path, monkeypatch):
    service = CronService(base_dir=tmp_path)
    parent = _completed_one_shot(service)

    def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(service, "_save", boom)

    # A real write fault must surface, not be swallowed as a quiet no-op.
    with pytest.raises(OSError):
        service._merge_job_result(parent)

    # The fingerprint reset forces a reload and the save never landed, so the
    # disk copy is still enabled; without a queue entry the reloaded one-shot
    # would run a second time. The consume must be queued for retry.
    assert parent.id in service._pending_removals
