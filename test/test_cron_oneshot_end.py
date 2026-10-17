"""A one-shot whose run the system ends, by timeout or by the reaper, does not run again.

``_execute`` finishes a one-shot after its run: a plain one is disabled and the
result merge persists it. A run that hits the wake deadline is cancelled before
that disable, and a run the reaper kills skips the result merge altogether, so
neither disable happened and the one-shot stayed enabled and due. Both endings
are the system judging the run stuck, so both park a plain one-shot the way a
completed run does. A ``delete_after_run`` one is not parked here: a failed run
keeps it and auto-pauses it. A run whose payload never started is not finished,
so the one-shot stays due and retries.

The callbacks wait on an event that is never set. The timeout tests patch the
wake budget to a fraction of a second so the real ``asyncio.wait_for`` deadline
fires; the reaper tests call the real ``_force_reap`` with the live claim, as the
reaper sweep does. The real tick, finalizer, merges and drain run unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kiro_crew.cron as cron_mod
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronJob, CronService, CronStoreBusy
from kiro_crew.testing.wait import await_until

_ADMITTED = SimpleNamespace(admitted=True, reason="")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_api_access(self, **event) -> None:
        self.events.append(event)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    value = _Recorder()
    monkeypatch.setattr(cron_mod, "sel", SimpleNamespace(sel=lambda: value))
    return value


@pytest.fixture(autouse=True)
def strict_loop_safety(_floor_monkeypatch) -> None:
    # A store lock taken on the event loop raises instead of warning. Pinned on
    # the floor's own MonkeyPatch, so a test's monkeypatch.undo() cannot lift it.
    _floor_monkeypatch.setenv(cron_mod._STRICT_LOOP_SAFETY_ENV, "1")


@pytest.fixture
def tiny_deadline(monkeypatch) -> None:
    monkeypatch.setattr(cron_mod, "effective_wake_budget", lambda job: 0.05)
    for name in ("_pool_queue_allowance", "_gate_budget_allowance", "_vet_allowance"):
        monkeypatch.setattr(cron_mod, name, lambda job: 0)


class _Payload:
    """A job callback. Runs listed in ``hang`` never finish."""

    def __init__(self, *, hang: int, never_started: bool = False) -> None:
        self.hang = hang
        self.never_started = never_started
        self.runs: list[str] = []
        self.job: CronJob | None = None
        self.started = asyncio.Event()

    async def __call__(self, job: CronJob) -> None:
        self.runs.append(job.id)
        self.job = job
        if self.never_started:
            # What the fire-time gate does while it waits: nothing has run yet.
            job.run_never_started = True
        self.started.set()
        if len(self.runs) <= self.hang:
            await asyncio.Event().wait()


async def _service(tmp_path, payload: _Payload) -> CronService:
    """A started service whose timer only fires when the test ticks it."""
    service = CronService(base_dir=tmp_path, on_job=payload)
    await service.start()
    service._arm_timer = lambda: None  # type: ignore[method-assign]
    task, service._timer_task = service._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await await_until(task, what="the cancelled timer task")
    return service


async def _tick(service: CronService, job_id: str) -> None:
    """One timer tick, then wait for the run it dispatched, if any."""
    await service._on_timer()
    claim = service._claims.get(job_id)
    if claim is not None and claim.task is not None:
        await await_until(claim.task, what="the dispatched run to end")


async def _reap(
    service: CronService, payload: _Payload, job_id: str, *, unstamped: bool = False
) -> None:
    """Start the run, then kill it the way the reaper sweep does.

    ``unstamped`` takes the claim's monotonic start stamp off first: a claim the
    sweep reaps by its wall-clock age, as a manual run still parked before its
    payload is.
    """
    await service._on_timer()
    await await_until(payload.started, what="the run to start")
    claim = service._claims[job_id]
    task = claim.task
    if unstamped:
        claim.started_monotonic = None
    await service._force_reap(job_id, _JOB_TIMEOUT_SECS + 10, claim=claim)
    if task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await await_until(task, what="the reaped run to unwind")


async def _one_shot(service: CronService, *, delete_after_run: bool = False) -> CronJob:
    return await service.add_job_async(
        "remind-once", "msg", at_ts=time.time() - 1, delete_after_run=delete_after_run
    )


def _stored(tmp_path, job_id: str) -> CronJob | None:
    return CronService(base_dir=tmp_path).get_job(job_id)


def _removals(recorder: _Recorder) -> list[str]:
    return [e["resources"] for e in recorder.events if e.get("outcome") == "one_shot_completed"]


# ── The wake deadline ──


@pytest.mark.asyncio
async def test_a_timed_out_one_shot_is_disabled_and_does_not_run_again(tmp_path, tiny_deadline):
    payload = _Payload(hang=1)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _tick(service, job.id)  # the run times out
            stored = _stored(tmp_path, job.id)
            assert stored is not None and stored.last_error.startswith("Timed out")
            assert not stored.enabled and stored.user_paused, "a timed-out one-shot was left due"
            await _tick(service, job.id)
    finally:
        await service.stop()

    assert payload.runs == [job.id], f"a timed-out one-shot ran {len(payload.runs)} times"


@pytest.mark.asyncio
async def test_a_one_shot_that_always_times_out_runs_once(tmp_path, tiny_deadline):
    payload = _Payload(hang=99)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            for _ in range(8):
                await _tick(service, job.id)
    finally:
        await service.stop()

    assert len(payload.runs) == 1, f"ran {len(payload.runs)} times in 8 ticks"


@pytest.mark.asyncio
async def test_a_timed_out_delete_after_run_one_shot_is_kept_paused(
    tmp_path, tiny_deadline, recorder
):
    payload = _Payload(hang=1)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service, delete_after_run=True)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _tick(service, job.id)
            await _tick(service, job.id)
    finally:
        await service.stop()

    assert payload.runs == [job.id]
    stored = _stored(tmp_path, job.id)
    assert stored is not None and not stored.enabled and stored.auto_paused
    assert _removals(recorder) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_after_run", [False, True], ids=["plain", "delete_after_run"])
async def test_a_timeout_before_the_payload_started_keeps_the_one_shot_due(
    tmp_path, tiny_deadline, delete_after_run
):
    payload = _Payload(hang=1, never_started=True)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service, delete_after_run=delete_after_run)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _tick(service, job.id)
    finally:
        await service.stop()

    stored = _stored(tmp_path, job.id)
    assert stored is not None, "a one-shot whose payload never started was consumed"
    assert stored.enabled and not stored.user_paused, "a never-started one-shot was disabled"
    assert service._is_due(stored, time.time())


# ── The reaper ──


@pytest.mark.asyncio
async def test_a_reaped_one_shot_is_parked_like_a_timed_out_one(tmp_path):
    payload = _Payload(hang=1)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _reap(service, payload, job.id)
            stored = _stored(tmp_path, job.id)
            assert stored is not None and stored.last_error.startswith("Reaped")
            assert not stored.enabled and stored.user_paused, "a reaped one-shot was left due"
            await _tick(service, job.id)  # the next tick
    finally:
        await service.stop()

    assert payload.runs == [job.id], f"a reaped one-shot ran {len(payload.runs)} times"


@pytest.mark.asyncio
async def test_a_reaped_one_shot_stays_disabled_when_the_store_changed_meanwhile(tmp_path):
    # The reaper's merge reloads a store another writer changed, so the job it
    # writes is the disk copy, not the object the run used: the disable must be
    # applied to that copy under the lock.
    payload = _Payload(hang=1)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service)
    real_merge = service._merge_terminal_state_locked

    def merge_after_another_writer(*args, **kwargs) -> None:
        CronService(base_dir=tmp_path).add_job("unrelated", "msg", every_secs=86400)
        real_merge(*args, **kwargs)

    service._merge_terminal_state_locked = merge_after_another_writer  # type: ignore[method-assign]
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _reap(service, payload, job.id)
            await _tick(service, job.id)
    finally:
        await service.stop()

    assert payload.runs == [job.id], f"a reaped one-shot ran {len(payload.runs)} times"
    stored = _stored(tmp_path, job.id)
    assert stored is not None and not stored.enabled and stored.user_paused


@pytest.mark.asyncio
async def test_a_reaped_one_shot_whose_pause_could_not_be_saved_stays_off_the_schedule(tmp_path):
    # The store is busy when the reaper saves, so the merge never runs and the
    # pause never reaches the disk. The job is still held off the schedule in this
    # process, so the next tick does not run it again.
    payload = _Payload(hang=1)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service)

    def busy_merge(*args, **kwargs) -> None:
        raise CronStoreBusy("cron store busy")

    service._merge_terminal_state_locked = busy_merge  # type: ignore[method-assign]
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _reap(service, payload, job.id)
            stored = _stored(tmp_path, job.id)
            assert stored is not None and stored.enabled, "the busy store took the pause"
            await _tick(service, job.id)
    finally:
        await service.stop()

    assert payload.runs == [job.id], f"a reaped one-shot ran {len(payload.runs)} times"


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_after_run", [False, True], ids=["plain", "delete_after_run"])
async def test_a_reaped_run_whose_payload_never_started_keeps_the_one_shot_due(
    tmp_path, delete_after_run
):
    payload = _Payload(hang=1, never_started=True)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service, delete_after_run=delete_after_run)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _reap(service, payload, job.id)
    finally:
        await service.stop()

    assert job.id not in service._pending_removals
    stored = _stored(tmp_path, job.id)
    assert stored is not None and stored.enabled and not stored.user_paused
    assert service._is_due(stored, time.time())


@pytest.mark.asyncio
async def test_a_reaped_claim_with_no_start_stamp_keeps_the_one_shot_due(tmp_path):
    # The sweep also reaps a claim the run never stamped (a manual run still
    # parked before its payload). Nothing of that run counts as having run.
    payload = _Payload(hang=1)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await _reap(service, payload, job.id, unstamped=True)
    finally:
        await service.stop()

    stored = _stored(tmp_path, job.id)
    assert stored is not None and stored.enabled and not stored.user_paused
    assert service._is_due(stored, time.time())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("never_started", "parked"),
    [(True, False), (False, True)],
    ids=["payload-never-started", "payload-started"],
)
async def test_a_store_reload_during_the_run_does_not_change_how_the_reaper_ends_it(
    tmp_path, never_started, parked
):
    # Every store reload replaces the registry's job objects, and the runtime-only
    # ``run_never_started`` marker stays on the object the run executed on. The
    # reaper reads it there: read off the registry's replacement it is always False,
    # which parked a one-shot whose payload never ran and counted that run as a
    # failure toward auto-pause.
    payload = _Payload(hang=1, never_started=never_started)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await service._on_timer()
            await await_until(payload.started, what="the run to start")
            claim = service._claims[job.id]
            task = claim.task
            # Another writer edits the store; the next tick's scan reloads it.
            other_writer = CronService(base_dir=tmp_path)
            await asyncio.to_thread(other_writer.add_job, "unrelated", "msg", every_secs=86400)
            await service._on_timer()
            registered = next(j for j in service._jobs if j.id == job.id)
            assert registered is not payload.job, "the store did not reload during the run"
            await service._force_reap(job.id, _JOB_TIMEOUT_SECS + 10, claim=claim)
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await await_until(task, what="the reaped run to unwind")
    finally:
        await service.stop()

    stored = _stored(tmp_path, job.id)
    assert stored is not None and stored.last_error.startswith("Reaped")
    if parked:
        assert not stored.enabled and stored.user_paused, "a reaped one-shot was left due"
    else:
        assert stored.enabled and not stored.user_paused, "a never-started one-shot was parked"
        assert service._is_due(stored, time.time())
    assert stored.consecutive_failures == (0 if never_started else 1)


# ── Loop safety ──


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["timeout", "reap"])
async def test_parking_the_one_shot_does_no_store_io_on_the_event_loop(
    tmp_path, monkeypatch, ending
):
    # The park adds only in-memory writes on the loop; every store read or
    # save stays in the worker-thread transactions the paths already use.
    monkeypatch.setattr(cron_mod, "effective_wake_budget", lambda job: 0.05)
    for name in ("_pool_queue_allowance", "_gate_budget_allowance", "_vet_allowance"):
        monkeypatch.setattr(cron_mod, name, lambda job: 0)
    payload = _Payload(hang=1)
    service = await _service(tmp_path, payload)
    job = await _one_shot(service)
    on_loop: list[str] = []
    for name in ("_save", "_sync"):
        real = getattr(service, name)

        def watched(*args, _real=real, _name=name, **kwargs):
            if _on_loop():
                on_loop.append(_name)
            return _real(*args, **kwargs)

        setattr(service, name, watched)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            if ending == "timeout":
                await _tick(service, job.id)
            else:
                await _reap(service, payload, job.id)
            await _tick(service, job.id)
    finally:
        await service.stop()

    assert payload.runs == [job.id]
    assert on_loop == [], f"store I/O on the event loop: {on_loop}"
