"""A cron mutation whose SAVE fails must not survive in the in-memory job list.

``CronService`` mutators edit ``self._jobs`` in place and only then call
``_save()``. The unreadable-store LATCH (``_load_failed`` ->
``CronStoreUnreadable``) is refused up front by ``_sync_for_write`` and covered
by ``test_cron_store_unreadable_boundaries.py``. A DIFFERENT failure is not: a
save that gets PAST that latch and then raises a bare ``OSError`` from the
atomic write itself -- ``ENOSPC`` (disk full), ``EROFS`` (read-only remount),
``EIO``, or a fault in the ``mkdir`` / ``encode_store`` / ``atomic_write``
chain. On that path the in-memory mutation has already landed, the caller is
told the write failed, yet the change stays in ``self._jobs`` -- visible to
``list_jobs``, firable by the timer, and persisted by the next save that
succeeds.

The contract: a mutator whose owned state is not shared with the execution
thread rolls its in-memory change back on ANY save failure, the way the remove
and update cores already do, extended to the add (×2), ack, unack and adopt
cores plus the loop-stall breaker. ``enable_job`` (pause/resume) is DELIBERATELY
excluded: its ``enabled``/``auto_paused``/``consecutive_failures`` are
execution-owned, so a field-scoped rollback races a concurrent
``record_failure``/``record_success`` and a correct fix needs an execution-state
revision fence (deferred, see the PR's Not a goal). These tests inject the
failure at the ``atomic_write`` boundary (a real ``OSError``, NOT
``CronStoreUnreadable``), so the real ``_save`` -> raise chain runs and the
``_load_failed`` latch is deliberately NOT what is being exercised.

The vacuous-pass trap: asserting only that the mutator RAISES passes whether or
not the rollback exists, because ``_save`` already raised. Every assertion below
is about ``self._jobs`` (and the live object's fields) AFTER the failed save,
and a reload from the untouched file -- the state a timer or a later save would
see.
"""

from __future__ import annotations

import contextlib
import errno
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew.cron import CronService

# One xdist worker for the whole file, matching the sibling cron suites. One
# group PER FILE, never shared: a shared group would serialize unrelated modules.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_cron_save_failure_rollback")


@contextlib.contextmanager
def _disk_full_on_save() -> Iterator[None]:
    """Make the next ``atomic_write`` raise ENOSPC, like a full disk would.

    Injected at ``kiro_crew.atomic_write.atomic_write`` -- the exact call
    ``_save`` makes AFTER its ``_load_failed`` guard and its ``mkdir`` -- so the
    real ``_save`` runs, the in-memory mutation has already landed, and a bare
    ``OSError`` (NOT ``CronStoreUnreadable``) comes back out. That distinction is
    the whole point: the latch path is already guarded and tested elsewhere;
    this is the write-fault path the issue names.
    """
    import kiro_crew.atomic_write as aw

    real = aw.atomic_write

    def failing(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    aw.atomic_write = failing  # type: ignore[assignment]
    try:
        yield
    finally:
        aw.atomic_write = real  # type: ignore[assignment]


def _reloaded(tmp_path: Path) -> CronService:
    """A fresh service reading the store from disk -- the timer's/next-save's view."""
    return CronService(base_dir=tmp_path)


# ── add_job ──


def test_a_failed_save_does_not_leave_an_added_job_in_memory(tmp_path: Path) -> None:
    svc = CronService(base_dir=tmp_path)
    assert not svc._load_failed, "precondition: a fresh store loads cleanly"

    with _disk_full_on_save():
        with pytest.raises(OSError) as exc:
            svc.add_job(name="rejected", message="m", every_secs=3600)
    assert exc.value.errno == errno.ENOSPC, "the write fault, not the unreadable latch"

    assert [j.name for j in svc._jobs] == [], (
        "a job whose write failed must not stay in the live list; the caller was "
        f"told the write failed but the scheduler can still see {[j.name for j in svc._jobs]}"
    )
    assert (
        _reloaded(tmp_path).list_jobs(include_disabled=True) == []
    ), "and nothing must be on disk for a later successful save to persist"


def test_a_failed_save_does_not_leave_an_add_if_absent_job_in_memory(tmp_path: Path) -> None:
    svc = CronService(base_dir=tmp_path)

    with _disk_full_on_save():
        with pytest.raises(OSError):
            svc.add_job_if_absent(
                lambda existing: existing.name == "rejected-2",
                name="rejected-2",
                message="m",
                every_secs=3600,
            )

    assert [j.name for j in svc._jobs] == [], "add_job_if_absent left the job in memory"


# ── ack / unack ──


def test_a_failed_save_does_not_record_an_ack_in_memory(tmp_path: Path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="ackable", message="m", every_secs=3600)
    assert svc._jobs[0].acked_items == [], "precondition: nothing acked yet"

    with _disk_full_on_save():
        with pytest.raises(OSError):
            svc.ack_job(job.id, "summary-text")

    assert svc._jobs[0].acked_items == [], "a refused ack must not be recorded in memory"


def test_a_failed_save_does_not_drop_an_unack_in_memory(tmp_path: Path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="ackable", message="m", every_secs=3600)
    svc.ack_job(job.id, "keep-me")
    assert svc._jobs[0].acked_items == ["keep-me"], "precondition: one ack recorded"

    with _disk_full_on_save():
        with pytest.raises(OSError):
            svc.unack_job(job.id)

    assert svc._jobs[0].acked_items == [
        "keep-me"
    ], "a refused unack must not drop the ack from the live cache"


# ── adopt_job (ownership) ──


def test_a_failed_save_does_not_leave_an_adopted_owner_in_memory(tmp_path: Path) -> None:
    """`adopt_job` repoints a job's `session_key` then saves; a failed save
    must not leave the new owner in the live cache (every ownership decision
    in this process reads it)."""
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="ownable", message="m", every_secs=3600)
    svc.adopt_job(job.id, "dashboard:original-owner")
    assert svc._jobs[0].session_key == "dashboard:original-owner", "precondition"

    with _disk_full_on_save():
        with pytest.raises(OSError):
            svc.adopt_job(job.id, "dashboard:new-owner")

    assert (
        svc._jobs[0].session_key == "dashboard:original-owner"
    ), "a refused adoption must leave the cached owner unchanged"
    reloaded = _reloaded(tmp_path).get_job(job.id)
    assert reloaded is not None and reloaded.session_key == "dashboard:original-owner"


# ── the loop-stall breaker's pause ──


def test_a_failed_save_does_not_leave_a_loop_stall_pause_in_memory(tmp_path: Path) -> None:
    """The breaker pauses the job the previous gateway died running. A save
    that fails with a BARE OSError (not CronStoreUnreadable) must roll the
    pause back AND report the verdict unsettled, so the breaker does not claim
    the dump and sweep the markers for a pause that never persisted -- which
    would make the next boot skip the still-enabled job."""
    from kiro_crew import cron_inflight, stall_attribution

    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="stalled", message="m", every_secs=3600)
    assert svc._jobs[0].enabled and not svc._jobs[0].auto_paused, "precondition"

    dump = tmp_path / "crash-dump.txt"
    dump.write_text("stack", encoding="utf-8")
    marker = cron_inflight.RunningMarker(
        job_id=job.id,
        name="stalled",
        started_at=1_700_000_000.0,
        pid=4242,
        path=tmp_path / "cron-running" / f"{job.id}.run.json",
        pid_domain="host:ns",
        pid_start="abc",
    )
    attribution = stall_attribution.StallAttribution(dump=dump, surface="cron", candidates=[marker])

    with _disk_full_on_save():
        settled, paused = svc._pause_for_loop_stall(attribution)

    assert (settled, paused) == (
        False,
        None,
    ), "a save that failed must leave the verdict unsettled so a later boot retries"
    assert svc._jobs[0].enabled and not svc._jobs[0].auto_paused, (
        "a pause whose save failed must not stay in the live cache -- the breaker "
        "went on to claim the dump and sweep the markers, so a surviving pause would "
        "make the next boot skip a job that was never actually paused on disk"
    )
    reloaded = _reloaded(tmp_path).get_job(job.id)
    assert reloaded is not None and reloaded.enabled and not reloaded.auto_paused


# ── NEGATIVE CONTROL: a healthy save must still mutate on every path ──
#
# A rollback keyed on the wrong thing could revert a SUCCESSFUL write too. These
# prove the mutation lands and survives a reload when the save does not fail.


def test_a_healthy_save_still_mutates_on_every_rolled_back_path(tmp_path: Path) -> None:
    svc = CronService(base_dir=tmp_path)

    added = svc.add_job(name="added", message="m", every_secs=3600)
    absent = svc.add_job_if_absent(
        lambda existing: existing.name == "absent",
        name="absent",
        message="m",
        every_secs=3600,
    )
    assert absent is not None
    svc.enable_job(added.id, enabled=False)
    svc.ack_job(added.id, "ack-1")
    svc.unack_job(added.id)
    svc.enable_job(added.id, enabled=True)
    svc.adopt_job(added.id, "dashboard:owner")

    # Every mutation is in the live cache and on disk after a reload.
    live = {j.name: j for j in svc._jobs}
    assert set(live) == {"added", "absent"}
    assert live["added"].enabled and not live["added"].user_paused
    assert live["added"].acked_items == []
    assert live["added"].session_key == "dashboard:owner"

    reloaded = {j.name: j for j in _reloaded(tmp_path).list_jobs(include_disabled=True)}
    assert set(reloaded) == {"added", "absent"}
    assert reloaded["added"].enabled
    assert reloaded["added"].session_key == "dashboard:owner"
