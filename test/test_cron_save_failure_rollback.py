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

The contract: a mutator rolls its in-memory change back on ANY save failure, the
way the remove and update cores already do, extended to the add (×2), ack,
unack and adopt cores plus the loop-stall breaker. ``enable_job`` (pause/resume)
cannot restore the values it saw: its ``enabled``/``user_paused``/
``auto_paused``/``consecutive_failures`` are also written by a run in flight on
the loop thread, so a field restore would undo the run's write. It reloads the
store instead, which re-applies every held run record, so the live list is the
store plus the runs not yet saved. These tests inject the
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
import copy
import errno
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron import CronJob, CronService
from kiro_crew.cron_service.store import CronStoreUnreadable

# One xdist worker for the whole file, matching the sibling cron suites. One
# group PER FILE, never shared: a shared group would serialize unrelated modules.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_cron_save_failure_rollback")


@contextlib.contextmanager
def _disk_full_on_save(during: Callable[[], None] | None = None) -> Iterator[None]:
    """Make the next ``atomic_write`` raise ENOSPC, like a full disk would.

    Injected at ``kiro_crew.atomic_write.atomic_write`` -- the exact call
    ``_save`` makes AFTER its ``_load_failed`` guard and its ``mkdir`` -- so the
    real ``_save`` runs, the in-memory mutation has already landed, and a bare
    ``OSError`` (NOT ``CronStoreUnreadable``) comes back out. That distinction is
    the whole point: the latch path is already guarded and tested elsewhere;
    this is the write-fault path the issue names.

    ``during`` runs inside the failing write, standing in for a run on the loop
    thread that writes the same job while the save is in flight.
    """
    import kiro_crew.atomic_write as aw

    real = aw.atomic_write

    def failing(*_args: object, **_kwargs: object) -> None:
        if during is not None:
            during()
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


# ── enable_job (pause / resume) ──


def _finish_a_run(svc: CronService, job_id: str, *, park: bool) -> CronJob:
    """Land a run's result the way a finalizer whose merge is still pending does.

    The run writes its outcome on the listed job it executed on, then holds a
    record of it (``_hold_run_record``) until a save under the lock persists it.
    ``park`` parks a fired one-shot, as ``_execute`` does.
    """
    live = next(j for j in svc._jobs if j.id == job_id)
    live.last_status = "ok"
    live.last_run_ts = time.time()
    live.record_success()
    if park:
        live.enabled = False
    record = copy.copy(live)
    record.run_generation = live.run_generation + 1
    svc._hold_run_record(live, record)
    return record


def test_a_failed_save_does_not_leave_a_pause_in_memory(tmp_path: Path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="pausable", message="m", every_secs=3600)

    with _disk_full_on_save():
        with pytest.raises(OSError):
            svc.enable_job(job.id, enabled=False)

    live = svc._jobs[0]
    assert live.enabled and not live.user_paused, (
        "a pause whose save failed must not stay in the live list, where the "
        "due-scan skips a job the store still has enabled"
    )
    reloaded = _reloaded(tmp_path).get_job(job.id)
    assert reloaded is not None and reloaded.enabled and not reloaded.user_paused


def test_a_failed_save_does_not_clear_an_auto_pause_in_memory(tmp_path: Path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="failing", message="m", every_secs=3600)
    with svc._file_lock():
        svc._sync()
        stored = svc._jobs[0]
        stored.enabled, stored.auto_paused, stored.consecutive_failures = False, True, 5
        svc._save()

    with _disk_full_on_save():
        with pytest.raises(OSError):
            svc.enable_job(job.id, enabled=True)

    live = svc._jobs[0]
    assert (live.enabled, live.auto_paused, live.consecutive_failures) == (False, True, 5), (
        "a resume whose save failed must leave the auto-pause and its failure count, "
        "or the timer fires a job the store still has paused"
    )


def test_a_failed_pause_keeps_a_concurrent_run_and_the_stored_enable(tmp_path: Path) -> None:
    """A run of the job finishes while the pause's save is failing. The live
    job must carry the run's result AND stay enabled, as the store says: keeping
    the failed pause's ``enabled=False`` would leave a recurring job the
    due-scan skips although the caller was told the pause failed."""
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="recurring", message="m", every_secs=3600)
    finished: list[CronJob] = []

    with _disk_full_on_save(lambda: finished.append(_finish_a_run(svc, job.id, park=False))):
        with pytest.raises(OSError):
            svc.enable_job(job.id, enabled=False)

    (record,) = finished
    live = svc._jobs[0]
    assert live.enabled and not live.user_paused, "the failed pause survived the rollback"
    assert (live.last_status, live.last_run_ts, live.run_generation) == (
        "ok",
        record.last_run_ts,
        record.run_generation,
    ), "the rollback dropped the concurrent run's result"
    assert svc._held_run_records.get(job.id) is record, "the run's record must still await a save"


def test_a_failed_pause_keeps_a_concurrent_one_shot_park(tmp_path: Path) -> None:
    """A one-shot fires and parks while the pause's save is failing. The park
    is the run's write, not the pause's, so the rollback must keep it, or the
    fired one-shot is due again on the next tick."""
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="once", message="m", at_ts=time.time() + 3600)

    with _disk_full_on_save(lambda: _finish_a_run(svc, job.id, park=True)):
        with pytest.raises(OSError):
            svc.enable_job(job.id, enabled=False)

    live = svc._jobs[0]
    assert live.enabled is False, "the rollback re-enabled a one-shot its run had parked"
    assert live.last_status == "ok"


def test_a_failed_save_whose_reload_also_fails_reloads_on_the_next_sync(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The reload after a failed pause save can fail too. The caller must still
    get the save's own error, and the next ``_sync`` must reload, so the failed
    pause does not outlive the next store read."""
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="pausable", message="m", every_secs=3600)
    real_sync = svc._sync

    with caplog.at_level("ERROR", logger="kiro_crew.cron"):
        with _disk_full_on_save():
            with patch.object(svc, "_sync", side_effect=[None, RuntimeError("reload failed")]):
                with pytest.raises(OSError) as exc:
                    svc.enable_job(job.id, enabled=False)
    assert exc.value.errno == errno.ENOSPC, "the save's error, not the reload's, reaches the caller"
    assert "reload after a failed pause/resume save did not run" in caplog.text

    real_sync()
    live = svc._jobs[0]
    assert live.enabled and not live.user_paused, "the next sync did not reload the store"


def test_a_failed_save_over_an_unreadable_store_refuses_every_later_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The store cannot be read back after the pause's save fails. No crash and
    no partly reloaded list: the save's error reaches the caller, the failure is
    logged, and the store is latched unreadable so no later save can persist the
    pause the caller was told failed. Once the store reads again, the next sync
    reloads it."""
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="pausable", message="m", every_secs=3600)
    other = svc.add_job(name="other", message="m", every_secs=3600)
    store = tmp_path / "crons.json"
    real_read = Path.read_bytes
    reads = {"n": 0}

    def unreadable_after_the_save(path: Path) -> bytes:
        if path == store:
            reads["n"] += 1
            if reads["n"] > 1:  # the first read is the call's own _sync_for_write
                raise OSError(errno.EIO, "Input/output error")
        return real_read(path)

    with caplog.at_level("ERROR", logger="kiro_crew.cron"):
        with _disk_full_on_save():
            with patch.object(Path, "read_bytes", unreadable_after_the_save):
                with pytest.raises(OSError) as exc:
                    svc.enable_job(job.id, enabled=False)
    assert exc.value.errno == errno.ENOSPC, "the save's error, not the reload's"
    assert "store unreadable after a failed pause/resume save" in caplog.text
    assert svc._load_failed, "an unreadable reload must latch the store"
    assert [j.id for j in svc._jobs] == [job.id, other.id], "the list was partly replaced"
    with pytest.raises(CronStoreUnreadable):
        svc._save()

    svc._sync()
    live = svc._jobs[0]
    assert not svc._load_failed
    assert live.enabled and not live.user_paused, "the readable store did not reload"


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
