"""A run record refused by a busy result merge must survive a store reload.

``_run_job_isolated`` hands a finished run to ``_merge_job_result``. When the
store lock stays contended the merge raises ``CronStoreBusy`` before it writes
anything, so the run's record exists only in memory. When another writer then
changes ``crons.json``, the next ``_sync`` replaces the job list with the disk
copies, which never received the run. The record must be re-applied to that
reload and persisted by the next save under the lock (the timer tick, or
``stop``), or an ``every`` job runs again early and a fired one-shot runs again.

Contention is simulated by making the store lock raise the exception the real
spin raises. The other writer is a second ``CronService`` on the same directory,
run in a worker thread like a separate process. The real ``_on_timer``,
``_run_job_isolated``, ``_merge_job_result``, ``_sync`` and ``_load`` run
unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.cron import CronJob, CronService, CronStoreBusy

_ADMITTED = SimpleNamespace(admitted=True, reason="")
_LONG_AGO = 7200.0


class _BusyStore:
    """Makes the store lock raise CronStoreBusy for the chosen callers.

    ``scope`` is ``"off"``, ``"merge"`` (only the result merge's lock attempt
    loses) or ``"all"`` (every attempt loses, the timer tick's included).
    """

    def __init__(self, service: CronService) -> None:
        self.scope = "off"
        self._merging = False
        real_lock = service._file_lock
        real_merge = service._merge_job_result

        def lock(*args, **kwargs):
            if self.scope == "all" or (self.scope == "merge" and self._merging):
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


async def _service(tmp_path, runs: list[str], on_run=None) -> CronService:
    """A started service whose timer only fires when the test ticks it."""

    async def on_job(job: CronJob) -> None:
        runs.append(job.id)
        if on_run is not None:
            await on_run()

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


def _other_writer(tmp_path) -> None:
    """Another process changes the store: it adds an unrelated job."""
    CronService(base_dir=tmp_path).add_job("unrelated", "msg", every_secs=86400)


async def _every_job(service: CronService) -> CronJob:
    """An interval job whose previous run was long ago, so it is due now."""
    job = await service.add_job_async("hourly", "msg", every_secs=3600, strict_schedule=True)

    def backdate() -> None:
        with service._file_lock():
            service._sync()
            for j in service._jobs:
                if j.id == job.id:
                    j.last_run_ts = time.time() - _LONG_AGO
            service._save()

    await asyncio.to_thread(backdate)
    return job


async def _one_shot(service: CronService) -> CronJob:
    """A one-shot that keeps its row after it runs (no delete_after_run)."""
    return await service.add_job_async("remind-once", "msg", at_ts=time.time() - 1)


def _stored(tmp_path, job_id: str) -> CronJob:
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None
    return job


def _held_record(service: CronService, job_id: str) -> CronJob:
    """The record the run left in memory because its merge could not lock the store."""
    record = service._held_run_records.get(job_id)
    assert record is not None, "the run's record is not held"
    return record


async def _until(predicate) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=5.0)


def _assert_run_recorded(tmp_path, job: CronJob, record: CronJob) -> None:
    stored = _stored(tmp_path, job.id)
    assert stored.last_status == "ok"
    if job.schedule.kind == "every":
        assert stored.last_run_ts == record.last_run_ts, "the run's last_run_ts never reached disk"
    else:
        assert stored.enabled is False, "the fired one-shot is enabled on disk"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_busy_merge_then_reload_runs_the_job_once(tmp_path, make_job):
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)  # runs; its merge cannot lock the store
            record = _held_record(service, job.id)
            store.scope = "off"
            await asyncio.to_thread(_other_writer, tmp_path)
            await _tick(service, job.id)  # reloads the changed store
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id], f"{job.schedule.kind} job ran {len(runs)} times"
    _assert_run_recorded(tmp_path, job, record)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_reload_by_a_reader_keeps_the_record_while_the_tick_is_busy(tmp_path, make_job):
    # The reload comes from a read path, and the tick cannot lock the store, so
    # nothing is saved: the reloaded job list itself must carry the record.
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            store.scope = "off"
            await asyncio.to_thread(_other_writer, tmp_path)
            await service.list_jobs_async(include_disabled=True)  # reloads
            store.scope = "all"
            await _tick(service, job.id)  # in-memory snapshot only
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id], f"{job.schedule.kind} job ran {len(runs)} times"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_reload_during_the_run_keeps_the_record(tmp_path, make_job):
    # The store changes while the job runs, so its finalizer holds a job object
    # the reload already replaced. The record has to reach the replacement.
    runs: list[str] = []
    holder: dict[str, CronService] = {}

    async def reload_mid_run() -> None:
        await asyncio.to_thread(_other_writer, tmp_path)
        await holder["service"].list_jobs_async(include_disabled=True)

    service = await _service(tmp_path, runs, on_run=reload_mid_run)
    holder["service"] = service
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            record = _held_record(service, job.id)
            store.scope = "off"
            await _tick(service, job.id)
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id], f"{job.schedule.kind} job ran {len(runs)} times"
    _assert_run_recorded(tmp_path, job, record)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_stop_persists_a_record_its_merge_could_not_save(tmp_path, make_job):
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            record = _held_record(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()  # a gateway restart

    assert runs == [job.id]
    restarted = CronService(base_dir=tmp_path)
    stored = restarted.get_job(job.id)
    assert stored is not None
    # The due-scan in _on_timer fires a job that is enabled and due. It is
    # judged at the run's own time (the one-shot's at_ts), so a clock step
    # between the run and this line cannot change the answer.
    ran_at = record.last_run_ts if job.schedule.kind == "every" else record.schedule.at_ts
    assert not (
        stored.enabled and restarted._is_due(stored, ran_at)
    ), "the restarted service would run the job again"
    _assert_run_recorded(tmp_path, job, record)


@pytest.mark.asyncio
async def test_a_newer_stored_run_wins_over_a_held_record(tmp_path):
    # Same fence as the merge: a held record never overwrites a newer run that
    # another process already stored.
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await _every_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            store.scope = "off"
            generation = service._runs.generations[job.id]

            def store_newer_run() -> None:
                other = CronService(base_dir=tmp_path)
                with other._file_lock():
                    other._sync()
                    newer = next(j for j in other._jobs if j.id == job.id)
                    newer.run_generation = generation + 1
                    newer.last_status = "error"
                    newer.last_error = "newer run"
                    newer.last_run_ts = time.time()
                    other._save()

            await asyncio.to_thread(store_newer_run)
            await _tick(service, job.id)
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id]
    stored = _stored(tmp_path, job.id)
    assert stored.last_error == "newer run"
    assert stored.run_generation == generation + 1


@pytest.mark.asyncio
async def test_a_manual_run_after_a_busy_merge_leaves_the_record_on_disk(tmp_path):
    # A manual run of the same job starts before the held record is saved. Its
    # _execute resets the shared job's status in place without a new
    # generation, so the job looks as if it already carries the record. The
    # next tick must still write the completed run's record, and must not
    # touch the job the manual run is using.
    runs: list[str] = []
    release = asyncio.Event()

    async def hold_the_manual_run() -> None:
        if len(runs) == 2:
            await release.wait()

    service = await _service(tmp_path, runs, on_run=hold_the_manual_run)
    job = await _every_job(service)
    store = _BusyStore(service)
    manual = None
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)  # runs; its merge cannot lock the store
            record = _held_record(service, job.id)
            store.scope = "off"
            manual = asyncio.create_task(service.run_job(job.id))
            await _until(lambda: len(runs) == 2)
            live = next(j for j in service._jobs if j.id == job.id)
            assert live.last_status is None, "the manual run did not reset the job"
            await service._on_timer()  # saves the held record under the lock
            assert live.last_status is None, "the save changed the running job"
            _assert_run_recorded(tmp_path, job, record)
    finally:
        release.set()
        if manual is not None:
            await asyncio.wait_for(manual, timeout=5.0)
        store.scope = "off"
        await service.stop()

    assert runs == [job.id, job.id]


@pytest.mark.asyncio
async def test_a_user_re_enable_after_a_busy_merge_reaches_disk(tmp_path):
    # The user re-enables a fired one-shot while its run's record is held. The
    # save of the held record (here at stop, as at a restart) must record the
    # run and keep the user's edit. A tick is not used: the re-enabled one-shot
    # is due again, and its next run would rewrite the job.
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await _one_shot(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)  # fires; its merge cannot lock the store
            record = _held_record(service, job.id)
            assert record.enabled is False, "the fired one-shot's record is not disabled"
            store.scope = "off"
            assert await service.enable_job_async(job.id)
    finally:
        store.scope = "off"
        await service.stop()  # saves the held record under the lock

    assert runs == [job.id]
    stored = _stored(tmp_path, job.id)
    assert stored.enabled is True, "the save of the held record reverted the user's re-enable"
    assert stored.user_paused is False
    assert stored.last_status == "ok"
    assert stored.run_generation == record.run_generation


@pytest.mark.asyncio
async def test_a_user_resume_after_a_busy_merge_reaches_disk(tmp_path):
    # The held run auto-paused the job; the user resumes it before the record
    # is saved. The next tick must save the run's record and keep the resume.
    runs: list[str] = []

    async def on_job(job: CronJob) -> None:
        runs.append(job.id)
        # Stands in for a run that reaches the auto-pause threshold: the
        # timeout branch counts its failure through the same record_failure.
        while not job.auto_paused:
            job.record_failure()
        raise RuntimeError("the run failed")

    service = CronService(base_dir=tmp_path, on_job=on_job)
    await service.start()
    service._arm_timer = lambda: None  # type: ignore[method-assign]
    task, service._timer_task = service._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    job = await _every_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)  # fails and auto-pauses; its merge cannot lock
            record = _held_record(service, job.id)
            assert record.auto_paused is True, "the held run did not auto-pause the job"
            store.scope = "off"
            assert await service.enable_job_async(job.id)  # the user's resume
            await _tick(service, job.id)  # saves the held record under the lock
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id]
    stored = _stored(tmp_path, job.id)
    assert stored.auto_paused is False, "the save of the held record reverted the user's resume"
    assert stored.enabled is True
    assert stored.consecutive_failures == 0
    assert stored.last_status == "error"
    assert stored.last_run_ts == record.last_run_ts


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_reload_during_the_run_then_a_busy_tick_runs_the_job_once(tmp_path, make_job):
    # The store changes while the job runs, so the finalizer holds a job object
    # a reload already replaced; its merge cannot lock the store, and neither
    # can the next tick, which works from memory. The replacement listed there
    # has to carry the run already, or the tick finds it due and runs it again.
    runs: list[str] = []
    holder: dict[str, CronService] = {}

    async def reload_mid_run() -> None:
        await asyncio.to_thread(_other_writer, tmp_path)
        await holder["service"].list_jobs_async(include_disabled=True)

    service = await _service(tmp_path, runs, on_run=reload_mid_run)
    holder["service"] = service
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            record = _held_record(service, job.id)
            store.scope = "all"
            await _tick(service, job.id)
            store.scope = "off"
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id], f"{job.schedule.kind} job ran {len(runs)} times"
    _assert_run_recorded(tmp_path, job, record)


def _other_writer_makes_it_hourly(tmp_path, job_id: str) -> None:
    """Another process turns the job into an hourly schedule."""
    CronService(base_dir=tmp_path).update_job(job_id, every_secs=3600)


@pytest.mark.asyncio
async def test_a_one_shot_made_recurring_after_a_busy_merge_keeps_running(tmp_path):
    # The one-shot fires and its merge cannot lock the store. Another process
    # then gives the job an hourly schedule. The reload takes the run's result,
    # but the fired one-shot's disable belongs to the schedule that fired, not
    # to the hourly one.
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await _one_shot(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            record = _held_record(service, job.id)
            store.scope = "off"
            await asyncio.to_thread(_other_writer_makes_it_hourly, tmp_path, job.id)
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id]
    stored = _stored(tmp_path, job.id)
    assert stored.schedule.kind == "every"
    assert stored.enabled is True, "the fired one-shot's disable was applied to the hourly schedule"
    assert stored.user_paused is False
    assert stored.last_status == "ok"
    assert stored.last_run_ts == record.last_run_ts


class _GuardThatReloadsFirst:
    """The held-record guard, with one reload on the hold's first entry to it.

    ``reload`` runs once, before the real lock is taken, on the first entry after
    ``arm()``: a reload a worker thread finishes right as the hold reaches the guard.
    The reload's own re-apply takes the guard again, unarmed, so it does not repeat.
    """

    def __init__(self, lock, reload) -> None:
        self._lock = lock
        self._reload = reload
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    def __enter__(self):
        if self._armed:
            self._armed = False
            self._reload()
        return self._lock.__enter__()

    def __exit__(self, *exc) -> None:
        self._lock.__exit__(*exc)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_a_reload_as_the_record_is_held_then_a_busy_tick_runs_the_job_once(
    tmp_path, make_job
):
    # Another writer changes the store, and a reload lands while the finalizer
    # holds the run's record: after the record was applied to the listed job and
    # before it was registered, the reload's re-apply finds nothing held and its
    # disk copy keeps the job due. The merge and the next tick cannot lock the
    # store, so that tick works from the reloaded list.
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await make_job(service)
    store = _BusyStore(service)

    def reload() -> None:
        _other_writer(tmp_path)
        with service._file_lock():
            service._sync()

    guard = _GuardThatReloadsFirst(service._held_run_records_guard, reload)
    service._held_run_records_guard = guard  # type: ignore[assignment]
    real_hold = service._hold_run_record
    holds: list[str] = []

    def hold(run_job: CronJob, record: CronJob) -> None:
        if not holds:  # the first run's hold; a second run would be the defect
            guard.arm()
        holds.append(record.id)
        real_hold(run_job, record)

    service._hold_run_record = hold  # type: ignore[method-assign]
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            record = _held_record(service, job.id)
            store.scope = "all"
            await _tick(service, job.id)
            store.scope = "off"
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id], f"{job.schedule.kind} job ran {len(runs)} times"
    _assert_run_recorded(tmp_path, job, record)


def _due_now(service: CronService) -> list[str]:
    """The jobs the timer tick would dispatch now: its own due predicate (``_on_timer``)."""
    now = time.time()
    return [
        j.id
        for j in service._jobs
        if j.enabled and j.id not in service._claims and service._is_due(j, now)
    ]


@pytest.mark.asyncio
async def test_a_reload_after_the_claim_release_finds_the_job_carrying_its_run(tmp_path):
    # The finalizer releases the run's claim, then notifies the dashboard. A reload
    # of a store another writer changed lands there, and the tick's due predicate is
    # applied to what it loaded: the job must not read as due, unclaimed and without
    # the run it just finished.
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await _every_job(service)
    due_after_release: list[list[str]] = []

    def refresh(_kind: str) -> None:
        if job.id in service._claims or due_after_release:
            return
        _other_writer(tmp_path)
        with service._file_lock():
            service._sync()
        due_after_release.append(_due_now(service))

    service._push_refresh = refresh  # type: ignore[assignment]
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _tick(service, job.id)
    finally:
        await service.stop()

    assert runs == [job.id]
    assert due_after_release, "the finalizer never notified after releasing the claim"
    assert job.id not in due_after_release[0], (
        "after the run's claim was released, a reload left the job due without its run: "
        f"due={due_after_release[0]}"
    )


@pytest.mark.asyncio
async def test_a_reload_never_publishes_copies_without_the_held_record(tmp_path, monkeypatch):
    # A reload replaces the job list with the store's copies. Anything that reads
    # the list while the reload is still running (the timer's due check on the
    # loop, while a worker thread reloads) must see copies that already carry the
    # held record of a run whose merge the lock refused.
    import kiro_crew.cron as cron_module

    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await _every_job(service)
    store = _BusyStore(service)
    seen_mid_reload: list[list[str]] = []
    real_digest = cron_module.store_digest

    def digest_then_look(raw):  # runs inside _load, after it decoded the store
        seen_mid_reload.append(_due_now(service))
        return real_digest(raw)

    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            _held_record(service, job.id)
            store.scope = "off"
            _other_writer(tmp_path)
            monkeypatch.setattr(cron_module, "store_digest", digest_then_look)
            with service._file_lock():
                service._sync()
            monkeypatch.setattr(cron_module, "store_digest", real_digest)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id]
    assert seen_mid_reload, "the reload never reached its digest step"
    leaked = [due for due in seen_mid_reload if job.id in due]
    assert not leaked, f"a reload published store copies without the held run record: {leaked}"
