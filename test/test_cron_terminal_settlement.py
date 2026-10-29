"""Scheduled terminal writes retain admission until a store write returns."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import (
    _AUTO_PAUSE_THRESHOLD,
    CronJob,
    CronPauseNotSaved,
    CronSchedule,
    CronService,
    CronStoreBusy,
    _RunClaim,
)
from kiro_crew.cron_service import execution
from kiro_crew.cron_service import settlement as settlement_rules
from kiro_crew.testing.wait import async_wait_until, await_until, finished

_OCCURRENCE_AT = 1_767_333_600.0
_ADMITTED = SimpleNamespace(admitted=True, reason="")


def _service(tmp_path: Path) -> tuple[CronService, CronJob]:
    service = CronService(base_dir=tmp_path)
    job = CronJob(
        id="scheduled-terminal",
        name="minutely",
        message="go",
        schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        timezone="UTC",
        last_run_ts=_OCCURRENCE_AT - 60,
    )
    service._jobs = [job]
    service._save()
    return service, job


def _terminal_kwargs() -> dict[str, object]:
    return {
        "last_status": "error",
        "last_error": "cancelled",
        "last_run_ts": _OCCURRENCE_AT + 10,
        "run_generation": 1,
    }


def _stored(tmp_path: Path) -> CronJob | None:
    return CronService(base_dir=tmp_path).get_job("scheduled-terminal")


async def _start_without_timer(service: CronService) -> None:
    await service.start()
    service._arm_timer = lambda: None  # type: ignore[method-assign]
    task, service._timer_task = service._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await await_until(task, what="the cron timer task to stop")


@pytest.mark.asyncio
async def test_cancel_save_failure_cannot_readmit_occurrence_after_reload(tmp_path: Path) -> None:
    """Regression: this same occurrence is admitted twice on the parent commit."""
    runs: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def on_job(job: CronJob) -> None:
        runs.append(job.id)
        started.set()
        await await_until(release, what="the cancelled cron run to be released")

    service = CronService(base_dir=tmp_path, on_job=on_job)
    await _start_without_timer(service)
    job = await service.add_job_async("remind-once", "msg", at_ts=1.0)
    real_merge = service._merge_terminal_state_locked

    def fail_terminal_write(*args: object, **kwargs: object) -> str | None:
        with patch.object(service, "_save", side_effect=OSError(28, "No space left on device")):
            return real_merge(*args, **kwargs)

    service._merge_terminal_state_locked = fail_terminal_write  # type: ignore[method-assign]
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            await service._on_timer()
            await await_until(started, what="the scheduled cron run to start")
            cancel = asyncio.create_task(service.cancel(job.id))
            with pytest.raises(CronPauseNotSaved, match="may run again"):
                await await_until(cancel, what="the cron cancellation to finish")

            other = CronService(base_dir=tmp_path)
            other.add_job("unrelated", "msg", every_secs=3600)
            await service._on_timer()

        assert runs == [job.id]
        assert job.id not in service._claims
    finally:
        release.set()
        await service.stop()


@pytest.mark.asyncio
async def test_failed_terminal_write_retries_once_on_next_tick(tmp_path: Path) -> None:
    service, job = _service(tmp_path)
    calls = 0

    def fail_before_commit() -> None:
        nonlocal calls
        calls += 1
        raise OSError("pre-commit")

    with patch.object(service, "_save", side_effect=fail_before_commit):
        reason = await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, **_terminal_kwargs()
        )

    assert reason == "OSError: pre-commit"
    assert calls == 1
    assert set(service._held_run_records) == {job.id}
    stale = _stored(tmp_path)
    assert stale is not None
    assert stale.run_generation == 0, "in-memory settlement must not count as durable"
    assert stale.last_status is None
    live = service.get_job(job.id)
    assert live is not None
    assert not service._is_due(live, _OCCURRENCE_AT)

    CronService(base_dir=tmp_path).add_job("unrelated", "msg", every_secs=3600)
    service._tick_scan_locked()

    assert service._held_run_records == {}
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.run_generation == 1
    assert persisted.last_status == "error"
    assert persisted.last_error == "cancelled"


@pytest.mark.asyncio
async def test_persistent_write_failure_gets_one_attempt_per_tick(tmp_path: Path) -> None:
    service, job = _service(tmp_path)
    calls = 0

    def fail_before_commit(*_args: object) -> None:
        nonlocal calls
        calls += 1
        raise OSError("pre-commit")

    with patch.object(service, "_save", side_effect=fail_before_commit):
        reason = await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, **_terminal_kwargs()
        )
        service._tick_scan_locked()

    assert reason == "OSError: pre-commit"
    assert calls == 2
    assert set(service._held_run_records) == {job.id}


@pytest.mark.asyncio
async def test_calendar_change_keeps_failed_write_report_and_terminal_record(
    tmp_path: Path,
) -> None:
    service, job = _service(tmp_path)
    replacement = CronService(base_dir=tmp_path)
    assert replacement.update_job(job.id, cron_expr="0 7 * * *")

    with patch.object(service, "_save", side_effect=OSError("pre-commit")):
        reason = await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, **_terminal_kwargs()
        )

    assert reason == "OSError: pre-commit"
    assert set(service._held_run_records) == {job.id}

    service._tick_scan_locked()

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.schedule.cron_expr == "0 7 * * *"
    assert persisted.run_generation == 1
    assert persisted.last_status == "error"
    assert persisted.last_error == "cancelled"


@pytest.mark.asyncio
async def test_store_busy_before_merge_holds_only_a_scheduled_terminal_record(
    tmp_path: Path,
) -> None:
    scheduled, job = _service(tmp_path)
    with patch.object(
        scheduled,
        "_file_lock",
        side_effect=CronStoreBusy("another writer held the store lock"),
    ):
        reason = await scheduled._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, **_terminal_kwargs()
        )

    assert reason == "CronStoreBusy: another writer held the store lock"
    assert scheduled._held_run_records[job.id].last_error == "cancelled"

    manual, manual_job = _service(tmp_path / "manual")
    with patch.object(
        manual,
        "_file_lock",
        side_effect=CronStoreBusy("another writer held the store lock"),
    ):
        with pytest.raises(CronStoreBusy, match="another writer"):
            await manual._merge_terminal_state_with_retry(
                manual_job, manual_job.id, scheduled=False, **_terminal_kwargs()
            )
    assert manual._held_run_records == {}


@pytest.mark.asyncio
async def test_other_service_edit_and_resume_survive_terminal_retry(tmp_path: Path) -> None:
    service, job = _service(tmp_path)
    job.schedule = CronSchedule(kind="at", at_ts=_OCCURRENCE_AT)
    service._save()

    with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
        reason = await service._merge_terminal_state_with_retry(
            job,
            job.id,
            scheduled=True,
            park_one_shot=True,
            **_terminal_kwargs(),
        )

    assert reason == "OSError: terminal write failed"
    assert set(service._held_run_records) == {job.id}

    other = CronService(base_dir=tmp_path)
    changed = other.update_job(
        job.id,
        name="morning run",
        message="new choice",
        cron_expr="0 7 * * *",
        timezone="America/Los_Angeles",
    )
    assert changed is not None
    assert other.enable_job(job.id)

    service._tick_scan_locked()

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.name == "morning run"
    assert persisted.message == "new choice"
    assert persisted.schedule == CronSchedule(kind="cron", cron_expr="0 7 * * *")
    assert persisted.timezone == "America/Los_Angeles"
    assert persisted.enabled is True
    assert persisted.user_paused is False
    assert persisted.run_generation == 1
    assert persisted.last_error == "cancelled"
    assert service._held_run_records == {}


@pytest.mark.asyncio
async def test_cancelled_dispatched_delete_after_run_freezes_auto_pause(
    tmp_path: Path,
) -> None:
    """A cancel snapshot taken before the task finalizer still parks the occurrence."""
    service, job = _service(tmp_path)
    job.schedule = CronSchedule(kind="at", at_ts=_OCCURRENCE_AT)
    job.delete_after_run = True
    service._save()

    with (
        patch.object(service, "_save", side_effect=OSError("terminal write failed")),
        patch.object(CronJob, "_audit_pause_change", autospec=True) as audit,
    ):
        reason = await service._merge_terminal_state_with_retry(
            job,
            job.id,
            scheduled=True,
            dispatched=True,
            **_terminal_kwargs(),
        )
        # The task finalizer runs the same rule later and finds the job paused.
        execution.auto_pause_failed_one_shot(job, "error")

    assert reason == "OSError: terminal write failed"
    frozen = service._held_run_records[job.id]
    assert frozen.auto_paused is True
    assert frozen.enabled is False
    assert (job.enabled, job.auto_paused) == (False, True)
    assert [c.args[1:] for c in audit.call_args_list] == [("auto_paused",)]

    service._tick_scan_locked()

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.auto_paused is True
    assert persisted.enabled is False
    assert persisted.last_error == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["immediate", "held"])
async def test_newer_resume_survives_stale_frozen_terminal_record(
    tmp_path: Path, settlement: str
) -> None:
    service, job = _service(tmp_path)
    job.enabled = False
    job.auto_paused = True
    job.consecutive_failures = 5
    service._save()
    frozen = settlement_rules.freeze_terminal_record(
        job,
        ran=job,
        result_produced=False,
        count_failure=False,
        park_one_shot=False,
        auto_paused_at_start=job.auto_paused,
        **_terminal_kwargs(),
    )

    other = CronService(base_dir=tmp_path)
    assert other.enable_job(job.id)

    if settlement == "immediate":
        service._merge_terminal_state_locked(
            job.id,
            terminal_record=frozen,
            **_terminal_kwargs(),
        )
    else:
        service._hold_run_record(job, frozen, settlement=True)
        service._tick_scan_locked()

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.user_pause_generation == 1
    assert persisted.enabled is True
    assert persisted.user_paused is False
    assert persisted.auto_paused is False
    assert persisted.consecutive_failures == 0
    assert persisted.run_generation == 1
    assert persisted.last_error == "cancelled"


@pytest.mark.asyncio
async def test_delayed_terminal_failure_does_not_replace_newer_held_run(tmp_path: Path) -> None:
    service, job = _service(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def delayed_failure(*_args: object) -> None:
        entered.set()
        assert release.wait(timeout=30.0), "test did not release the terminal save"
        raise OSError("older terminal write failed")

    with patch.object(service, "_save", side_effect=delayed_failure):
        merge = asyncio.create_task(
            service._merge_terminal_state_with_retry(
                job, job.id, scheduled=True, **_terminal_kwargs()
            )
        )
        await async_wait_until(entered.is_set)
        newer = copy.copy(job)
        newer.run_generation = 2
        newer.last_status = "ok"
        newer.last_error = "newer run"
        newer.last_run_ts = _OCCURRENCE_AT + 20
        service._hold_run_record(job, newer)
        release.set()
        reason = await await_until(merge, what="the delayed terminal merge to fail")

    assert reason == "OSError: older terminal write failed"
    assert service._held_run_records[job.id] is newer
    assert service._held_run_records[job.id].run_generation == 2
    assert service._held_run_records[job.id].last_error == "newer run"


@pytest.mark.asyncio
async def test_held_terminal_record_releases_only_after_durable_write_returns(
    tmp_path: Path,
) -> None:
    service, job = _service(tmp_path)
    with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
        await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, **_terminal_kwargs()
        )

    entered = threading.Event()
    release = threading.Event()
    real_save = service._save

    def delayed_success(*args: object) -> None:
        entered.set()
        assert release.wait(timeout=30.0), "test did not release the durable write"
        real_save(*args)  # type: ignore[arg-type]

    with patch.object(service, "_save", side_effect=delayed_success):
        retry = asyncio.create_task(asyncio.to_thread(service._tick_scan_locked))
        await async_wait_until(entered.is_set)
        assert set(service._held_run_records) == {job.id}
        release.set()
        await await_until(retry, what="the durable terminal retry to return")

    assert service._held_run_records == {}
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.run_generation == 1
    assert persisted.last_error == "cancelled"


@pytest.mark.asyncio
async def test_abrupt_restart_before_retry_reads_the_stale_store(tmp_path: Path) -> None:
    service, job = _service(tmp_path)
    with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
        await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, **_terminal_kwargs()
        )

    restarted = CronService(base_dir=tmp_path)
    stale = restarted.get_job(job.id)
    assert stale is not None
    assert stale.run_generation == 0
    assert stale.last_status is None
    assert restarted._is_due(stale, _OCCURRENCE_AT)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancel", "reap"])
@pytest.mark.parametrize("trigger", ["scheduled", "manual"])
async def test_terminal_paths_retry_only_scheduled_failures(
    tmp_path: Path, terminal: str, trigger: str
) -> None:
    service, job = _service(tmp_path)
    claim = _RunClaim(
        trigger=trigger,
        claimed_at=_OCCURRENCE_AT,
        started_monotonic=1.0,
        outcome_reset=True,
        job=job,
        task=MagicMock(done=MagicMock(return_value=False)),
    )
    service._claims[job.id] = claim
    service._history.append = AsyncMock()  # type: ignore[method-assign]
    service._merge_terminal_state_with_retry = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with (
        patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
        patch("kiro_crew.cron.sel.sel"),
        patch("kiro_crew.sel.sel"),
    ):
        if terminal == "cancel":
            assert await service.cancel(job.id) is True
        else:
            await service._force_reap(job.id, elapsed=1900, deadline=1800, claim=claim)

    awaited = service._merge_terminal_state_with_retry.await_args
    assert awaited is not None
    assert awaited.kwargs["scheduled"] is (trigger == "scheduled")
    assert awaited.kwargs["dispatched"] is True
    assert awaited.kwargs["ran"] is job


@pytest.mark.asyncio
async def test_a_landed_settlement_park_survives_a_later_save_of_the_list(
    tmp_path: Path,
) -> None:
    """The tick lands a held cancel's park on its copy. The listed job takes it, so the
    next save of the list does not write the unparked spelling back."""
    service, job = _service(tmp_path)
    job.schedule = CronSchedule(kind="at", at_ts=_OCCURRENCE_AT)
    service._save()
    with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
        await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, park_one_shot=True, **_terminal_kwargs()
        )
    listed = service.get_job(job.id)
    assert listed is not None
    assert (listed.enabled, listed.user_paused) == (False, False), "the park is not on disk yet"

    service._tick_scan_locked()
    assert service._held_run_records == {}
    assert (listed.enabled, listed.user_paused) == (False, True)
    service.add_job(name="unrelated", message="m", every_secs=3600)

    restarted = _stored(tmp_path)
    assert restarted is not None
    assert (restarted.enabled, restarted.user_paused) == (False, True)


def test_only_a_pause_transition_or_a_one_shot_resume_is_ordered(tmp_path: Path) -> None:
    """A repeated request on a recurring job records nothing. A real pause or resume,
    and any resume of a one-shot, moves ``user_pause_generation``."""
    service, job = _service(tmp_path)
    assert service.enable_job(job.id, enabled=True)
    assert service.get_job(job.id).user_pause_generation == 0  # type: ignore[union-attr]
    assert service.enable_job(job.id, enabled=False)
    assert service.enable_job(job.id, enabled=False)
    assert service.get_job(job.id).user_pause_generation == 1  # type: ignore[union-attr]
    assert service.enable_job(job.id, enabled=True)
    assert service.get_job(job.id).user_pause_generation == 2  # type: ignore[union-attr]

    one_shot = service.add_job(name="once", message="m", at_ts=_OCCURRENCE_AT + 3600)
    assert service.enable_job(one_shot.id, enabled=True)
    assert service.get_job(one_shot.id).user_pause_generation == 1  # type: ignore[union-attr]


def _one_shot_service(tmp_path: Path) -> tuple[CronService, CronJob]:
    service, job = _service(tmp_path)
    job.schedule = CronSchedule(kind="at", at_ts=_OCCURRENCE_AT)
    service._save()
    return service, job


def _reload(service: CronService) -> CronJob:
    """Reload ``service`` from the store under its lock, as a tick or a reader does."""
    with service._file_lock():
        service._sync()
    listed = service.get_job("scheduled-terminal")
    assert listed is not None
    return listed


def _rewrite_stored_job(tmp_path: Path, **fields: object) -> None:
    """Write the job's record as a build without ``user_pause_generation`` would."""
    store = tmp_path / "crons.json"
    document = json.loads(store.read_text(encoding="utf-8"))
    for record in document["jobs"]:
        if record["id"] == "scheduled-terminal":
            record.update(fields)
    store.write_text(json.dumps(document, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_tick_that_wins_the_lock_before_the_first_merge_keeps_the_park(
    tmp_path: Path,
) -> None:
    """Regression: the settlement is held before the first terminal write, not after
    it fails. Another writer changes the store, and a timer tick takes the lock
    before the merge thread does: the tick's reload must still find the cancelled
    one-shot off the schedule, and its save lands the park."""
    service, job = _one_shot_service(tmp_path)
    job.enabled = False  # the cancel's provisional hold
    real_merge = service._merge_terminal_state_locked
    at_tick: dict[str, object] = {}

    def tick_takes_the_lock_first(*args: object, **kwargs: object) -> str | None:
        CronService(base_dir=tmp_path).add_job("external", "m", every_secs=3600)
        snapshot = service._tick_scan_locked()
        listed = next(j for j in snapshot if j.id == job.id)
        at_tick["reloaded"] = listed is not job
        at_tick["admitted"] = listed.enabled and service._is_due(listed, _OCCURRENCE_AT + 30)
        return real_merge(*args, **kwargs)

    service._merge_terminal_state_locked = tick_takes_the_lock_first  # type: ignore[method-assign]
    reason = await service._merge_terminal_state_with_retry(
        job, job.id, scheduled=True, park_one_shot=True, **_terminal_kwargs()
    )

    assert at_tick == {"reloaded": True, "admitted": False}
    assert reason is None
    assert service._held_run_records == {}
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.user_paused) == (False, True)
    assert persisted.run_generation == 1


@pytest.mark.asyncio
async def test_a_newer_held_run_is_not_reported_as_the_cancel_s_settlement(
    tmp_path: Path,
) -> None:
    """A newer ordinary run held in place of the cancel's settlement is not a pause
    hold the cancel made, so the cancel reads the saved store instead of claiming one."""
    service, job = _one_shot_service(tmp_path)
    claim = _RunClaim(
        trigger="scheduled",
        claimed_at=_OCCURRENCE_AT,
        started_monotonic=1.0,
        outcome_reset=False,
        job=job,
        task=MagicMock(done=MagicMock(return_value=False)),
    )
    service._claims[job.id] = claim
    service._history.append = AsyncMock()  # type: ignore[method-assign]
    real_merge = service._merge_terminal_state_locked

    def newer_run_held_in_the_gap(*args: object, **kwargs: object) -> str | None:
        # Paused on disk by a build that does not move the pause counter.
        _rewrite_stored_job(tmp_path, user_paused=True, enabled=False)
        listed = service.get_job(job.id)
        assert listed is not None
        newer = copy.copy(listed)
        newer.run_generation = 2
        newer.last_status = "ok"
        newer.last_error = ""
        newer.last_run_ts = _OCCURRENCE_AT + 20
        service._hold_run_record(listed, newer)
        return real_merge(*args, **kwargs)

    service._merge_terminal_state_locked = newer_run_held_in_the_gap  # type: ignore[method-assign]
    with (
        patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
        patch("kiro_crew.cron.sel.sel"),
    ):
        assert await service.cancel(job.id) is True

    assert service._held_run_records[job.id].run_generation == 2
    assert job.id not in service._held_settlements
    entry = service._history.append.await_args.args[0]
    assert entry.error.endswith(", and the job is paused"), entry.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count_failure", "failures"),
    [
        # A manual cancel never counts a failure.
        (False, 0),
        # A manual reap counts its own failure on the synced copy, not the stale one.
        (True, 1),
    ],
)
async def test_a_resume_saved_during_a_manual_run_survives_its_terminal_write(
    tmp_path: Path, count_failure: bool, failures: int
) -> None:
    """Regression: a manual cancel or reap wrote the listed job's stale auto-pause and
    failure count over a resume another process saved during the run. A second
    service saves the resume before the locked merge reads the store."""
    service, job = _service(tmp_path)
    job.enabled = False
    job.auto_paused = True
    job.consecutive_failures = 5
    service._save()
    real_merge = service._merge_terminal_state_locked
    records: list[object] = []

    def resumed_before_the_merge(*args: object, **kwargs: object) -> str | None:
        records.append(kwargs["terminal_record"])
        assert CronService(base_dir=tmp_path).enable_job(job.id)  # e.g. `kirocrew cron resume`
        return real_merge(*args, **kwargs)

    service._merge_terminal_state_locked = resumed_before_the_merge  # type: ignore[method-assign]
    reason = await service._merge_terminal_state_with_retry(
        job,
        job.id,
        scheduled=False,
        count_failure=count_failure,
        **_terminal_kwargs(),
    )

    # A manual merge has no frozen record carrying the listed job's stale auto-pause.
    assert records == [None]
    assert reason is None
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.auto_paused) == (True, False)
    assert persisted.consecutive_failures == failures
    assert persisted.run_generation == 1
    assert persisted.last_error == "cancelled"


async def _end_a_manual_run(
    service: CronService, job: CronJob, terminal: str, *, trigger: str = "manual"
) -> None:
    """Cancel or reap a dispatched run of ``job`` (manual by default) through its real entry point."""
    claim = _RunClaim(
        trigger=trigger,
        claimed_at=_OCCURRENCE_AT,
        started_monotonic=1.0,
        outcome_reset=True,
        job=job,
        task=MagicMock(done=MagicMock(return_value=False)),
    )
    service._claims[job.id] = claim
    service._history.append = AsyncMock()  # type: ignore[method-assign]
    with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
        if terminal == "cancel":
            assert await service.cancel(job.id) is True
        else:
            await service._force_reap(job.id, elapsed=1900, deadline=1800, claim=claim)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "resume"),
    [
        ("cancel", "this build"),
        ("reap", "this build"),
        # A build without the counter resumes without moving it. The cancel and the
        # reap keep that resume too: the run started auto-paused, so the frozen pause
        # is not its own.
        ("cancel", "older build"),
        ("reap", "older build"),
    ],
)
async def test_a_manual_run_keeps_a_resume_of_its_auto_paused_one_shot(
    tmp_path: Path, terminal: str, resume: str
) -> None:
    """Regression: a manual cancel or reap of an auto-paused ``delete_after_run``
    one-shot wrote the auto-pause the run started from over a resume another process
    saved between the freeze and the merge. A cancel still parks the one-shot, as a
    cancel parks every one-shot."""
    service, job = _one_shot_service(tmp_path)
    job.delete_after_run = True
    job.enabled = False
    job.auto_paused = True
    job.consecutive_failures = 1
    service._save()
    job.auto_paused_at_start = True  # stamped when the manual run started
    real_merge = service._merge_terminal_state_locked
    records: list[object] = []

    def resumed_before_the_merge(*args: object, **kwargs: object) -> str | None:
        records.append(kwargs["terminal_record"])
        if resume == "this build":
            assert CronService(base_dir=tmp_path).enable_job(job.id)
        else:
            _rewrite_stored_job(tmp_path, auto_paused=False, enabled=True, consecutive_failures=0)
            # An unrelated write changes the store's size, so the merge's reload is certain.
            CronService(base_dir=tmp_path).add_job("external", "m", every_secs=3600)
        return real_merge(*args, **kwargs)

    service._merge_terminal_state_locked = resumed_before_the_merge  # type: ignore[method-assign]
    with patch("kiro_crew.sel.sel"):
        await _end_a_manual_run(service, job, terminal)

    assert records == [None]
    persisted = _stored(tmp_path)
    assert persisted is not None
    parked = terminal == "cancel"
    assert (persisted.enabled, persisted.auto_paused, persisted.user_paused) == (
        not parked,
        False,
        parked,
    )
    assert persisted.consecutive_failures == 0
    assert persisted.run_generation == 1
    assert persisted.last_status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["reap", "direct merge"])
async def test_a_manual_failure_at_the_limit_audits_one_auto_pause(
    tmp_path: Path, entry: str
) -> None:
    """Regression: a manual reap at one failure below the limit counted the failure on
    the frozen copy and again on the synced copy, and each count wrote its own
    ``cron_auto_pause`` row. The one row comes from the write that saves the pause."""
    service, job = _service(tmp_path)
    job.consecutive_failures = _AUTO_PAUSE_THRESHOLD - 1
    service._save()
    job.auto_paused_at_start = False  # stamped when the manual run started
    with patch("kiro_crew.sel.sel") as sel:
        if entry == "reap":
            await _end_a_manual_run(service, job, "reap")
        else:
            service._merge_terminal_state_locked(job.id, count_failure=True, **_terminal_kwargs())

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.auto_paused) == (False, True)
    assert persisted.consecutive_failures == _AUTO_PAUSE_THRESHOLD
    rows = [
        c.kwargs
        for c in sel.return_value.log_tool_invocation.call_args_list
        if c.kwargs.get("tool_kind") == "cron_auto_pause"
    ]
    assert rows == [
        {
            "session_key": f"cron:{job.id}",
            "tool_name": "cron_job",
            "tool_kind": "cron_auto_pause",
            "outcome": "auto_paused",
            "metadata": {"job_id": job.id, "consecutive_failures": _AUTO_PAUSE_THRESHOLD},
        }
    ]


def _auto_pause_rows(sel: MagicMock) -> list[dict[str, object]]:
    return [
        c.kwargs
        for c in sel.return_value.log_tool_invocation.call_args_list
        if c.kwargs.get("tool_kind") == "cron_auto_pause"
    ]


def _failing_job_with_stored_limit(
    tmp_path: Path, failures: int, stored_limit: int | None
) -> tuple[CronService, CronJob]:
    """A scheduled run's job at ``failures``, its limit rewritten by another process
    (``stored_limit``; None leaves the default) after the run started."""
    service, job = _service(tmp_path)
    job.consecutive_failures = failures
    service._save()
    job.auto_paused_at_start = False  # stamped when the scheduled run started
    if stored_limit is not None:
        other = CronService(base_dir=tmp_path)
        assert other.update_job(job.id, auto_pause_after_failures=stored_limit)
    return service, job


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_limit", [0, 10])
async def test_a_scheduled_reap_keeps_a_limit_saved_during_the_run(
    tmp_path: Path, stored_limit: int
) -> None:
    """Regression: a scheduled reap decided the auto-pause from the limit on the job the
    run started with. Another process saved a limit (0 = never, or a raised one) that
    this failure does not reach, and the reap paused the job anyway. The stored limit
    wins, as on main."""
    service, job = _failing_job_with_stored_limit(tmp_path, _AUTO_PAUSE_THRESHOLD - 1, stored_limit)
    with patch("kiro_crew.sel.sel") as sel:
        await _end_a_manual_run(service, job, "reap", trigger="scheduled")

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.auto_paused) == (True, False)
    assert persisted.auto_pause_after_failures == stored_limit
    assert persisted.consecutive_failures == _AUTO_PAUSE_THRESHOLD
    assert persisted.run_generation == 1
    assert _auto_pause_rows(sel) == []
    assert service._held_run_records == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("first_save", ["lands", "fails"])
@pytest.mark.parametrize(
    ("failures", "stored_limit"),
    [
        # A limit lowered during the run, so this failure reaches it.
        (2, 3),
        # The limit unchanged, and this failure is the one that reaches it.
        (_AUTO_PAUSE_THRESHOLD - 1, None),
    ],
)
async def test_a_scheduled_reap_at_the_stored_limit_audits_one_auto_pause(
    tmp_path: Path, failures: int, stored_limit: int | None, first_save: str
) -> None:
    """The pause a scheduled reap's failure causes lands with exactly one
    ``cron_auto_pause`` row, written by the save that lands it: none while a failed
    first save leaves it held, and none again when a later save writes it a second
    time."""
    service, job = _failing_job_with_stored_limit(tmp_path, failures, stored_limit)
    with patch("kiro_crew.sel.sel") as sel:
        if first_save == "fails":
            with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
                await _end_a_manual_run(service, job, "reap", trigger="scheduled")
            assert set(service._held_run_records) == {job.id}
            assert _auto_pause_rows(sel) == [], "no row before a save lands the pause"
            service._tick_scan_locked()
        else:
            await _end_a_manual_run(service, job, "reap", trigger="scheduled")
        assert service._held_run_records == {}
        service.add_job(name="unrelated", message="m", every_secs=3600)
        service._tick_scan_locked()

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.auto_paused) == (False, True)
    assert persisted.consecutive_failures == failures + 1
    assert _auto_pause_rows(sel) == [
        {
            "session_key": f"cron:{job.id}",
            "tool_name": "cron_job",
            "tool_kind": "cron_auto_pause",
            "outcome": "auto_paused",
            "metadata": {"job_id": job.id, "consecutive_failures": failures + 1},
        }
    ]


@pytest.mark.asyncio
async def test_a_held_reap_at_the_stored_limit_pauses_a_job_whose_schedule_changed(
    tmp_path: Path,
) -> None:
    """Regression: a held scheduled reap whose failure reached the stored limit landed
    after another process edited the schedule, and saved the threshold count with the
    job still enabled, so it ran once more unattended. The schedule edit drops only the
    old schedule's park; the stored limit still pauses the job, audited once."""
    service, job = _failing_job_with_stored_limit(tmp_path, _AUTO_PAUSE_THRESHOLD - 1, None)
    with patch("kiro_crew.sel.sel") as sel:
        with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
            await _end_a_manual_run(service, job, "reap", trigger="scheduled")
        assert set(service._held_run_records) == {job.id}
        assert CronService(base_dir=tmp_path).update_job(job.id, cron_expr="0 7 * * *")
        service._tick_scan_locked()
        assert service._held_run_records == {}
        service.add_job(name="unrelated", message="m", every_secs=3600)
        service._tick_scan_locked()

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.schedule == CronSchedule(kind="cron", cron_expr="0 7 * * *")
    assert persisted.consecutive_failures == _AUTO_PAUSE_THRESHOLD
    assert (persisted.enabled, persisted.auto_paused, persisted.user_paused) == (
        False,
        True,
        False,
    )
    assert [row["metadata"] for row in _auto_pause_rows(sel)] == [
        {"job_id": job.id, "consecutive_failures": _AUTO_PAUSE_THRESHOLD}
    ]


@pytest.mark.asyncio
async def test_a_reloaded_copy_does_not_audit_a_failed_one_shot_s_pause_twice(
    tmp_path: Path,
) -> None:
    """Regression: a reload replaced the listed job while a failed ``delete_after_run``
    one-shot waited in its teardown. The cancel's freeze paused the listed copy and
    the run's finally paused the run's own object, each with its own
    ``cron_auto_pause`` row. The pause happens once, on the run's object, and the
    frozen record carries it."""
    service, ran = _one_shot_service(tmp_path)
    ran.delete_after_run = True
    service._save()
    ran.last_status = "error"  # the run failed, below its failure limit
    listed = _reload_from_another_writer(service, tmp_path)
    assert listed is not ran

    with (
        patch.object(service, "_save", side_effect=OSError("terminal write failed")),
        patch.object(CronJob, "_audit_pause_change", autospec=True) as audit,
    ):
        await service._merge_terminal_state_with_retry(
            listed, listed.id, scheduled=True, dispatched=True, ran=ran, **_terminal_kwargs()
        )
        # The task finalizer runs the same rule on the run's object afterwards.
        execution.auto_pause_failed_one_shot(ran, ran.last_status)

    audited = [("run" if c.args[0] is ran else "copy", c.args[1]) for c in audit.call_args_list]
    assert audited == [("run", "auto_paused")]
    assert (ran.enabled, ran.auto_paused) == (False, True)
    frozen = service._held_run_records[listed.id]
    assert (frozen.enabled, frozen.auto_paused) == (False, True)

    service._tick_scan_locked()
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.auto_paused) == (False, True)


@pytest.mark.parametrize(
    ("scheduled", "answer", "on_disk"),
    [
        # The scheduled settlement yields to the later resume.
        (True, "newer choice", (True, False)),
        # A manual cancel or reap keeps main's single attempt and parks.
        (False, None, (False, True)),
    ],
)
def test_only_a_scheduled_settlement_yields_to_a_resume_after_the_freeze(
    tmp_path: Path, scheduled: bool, answer: str | None, on_disk: tuple[bool, bool]
) -> None:
    from kiro_crew import cron as seams

    service, job = _one_shot_service(tmp_path)
    frozen = settlement_rules.freeze_terminal_record(
        job,
        ran=job,
        result_produced=False,
        count_failure=False,
        park_one_shot=True,
        auto_paused_at_start=job.auto_paused,
        **_terminal_kwargs(),
    )
    assert CronService(base_dir=tmp_path).enable_job(job.id, enabled=True)

    reason = service._merge_terminal_state_locked(
        job.id,
        terminal_record=frozen,
        park_one_shot=True,
        retry_failed_save=scheduled,
        **_terminal_kwargs(),
    )

    assert reason == (seams._PARK_NEWER_CHOICE if answer == "newer choice" else None)
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.user_paused) == on_disk
    assert persisted.run_generation == 1


@pytest.mark.asyncio
async def test_a_reload_at_the_settlement_s_generation_keeps_the_job_off_the_schedule(
    tmp_path: Path,
) -> None:
    """A save of the list writes the settlement's generation without its park. The
    reload after another writer's edit must still find the job held, and the next
    healthy tick lands the park."""
    service, job = _one_shot_service(tmp_path)
    with patch.object(
        service, "_file_lock", side_effect=CronStoreBusy("another writer held the store lock")
    ):
        await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, park_one_shot=True, **_terminal_kwargs()
        )
    service.add_job(name="unrelated", message="m", every_secs=3600)
    on_disk = _stored(tmp_path)
    assert on_disk is not None
    assert (on_disk.run_generation, on_disk.user_paused) == (1, False)

    CronService(base_dir=tmp_path).add_job("external", "m", every_secs=3600)
    listed = _reload(service)

    assert listed is not job, "the reload must hand back the store's copy"
    assert (listed.enabled, listed.user_paused) == (False, False)

    service._tick_scan_locked()
    assert service._held_run_records == {}
    landed = _stored(tmp_path)
    assert landed is not None
    assert (landed.enabled, landed.user_paused) == (False, True)


@pytest.mark.asyncio
async def test_a_reloaded_copy_does_not_auto_pause_an_undispatched_one_shot(
    tmp_path: Path,
) -> None:
    """The never-started flag is runtime-only. The freeze reads it off the object the
    run executed on, so a reload during the fire-time gate does not park work that
    never ran."""
    service, ran = _one_shot_service(tmp_path)
    ran.delete_after_run = True
    service._save()
    ran.run_never_started = True
    listed = _reload_from_another_writer(service, tmp_path)
    assert listed is not ran and listed.run_never_started is False

    with (
        patch.object(service, "_save", side_effect=OSError("terminal write failed")),
        patch.object(CronJob, "_audit_pause_change", autospec=True) as audit,
    ):
        await service._merge_terminal_state_with_retry(
            listed, listed.id, scheduled=True, dispatched=True, ran=ran, **_terminal_kwargs()
        )

    assert audit.call_args_list == []
    assert listed.auto_paused is False
    assert service._held_run_records[listed.id].auto_paused is False


def _reload_from_another_writer(service: CronService, tmp_path: Path) -> CronJob:
    CronService(base_dir=tmp_path).add_job("external", "m", every_secs=3600)
    return _reload(service)


@pytest.mark.asyncio
async def test_a_failed_manual_reap_of_a_job_made_recurring_leaves_it_on_its_schedule(
    tmp_path: Path,
) -> None:
    """The provisional hold on a one-shot another writer made recurring comes off
    before the rollback snapshot, so a failed save does not put it back."""
    service, job = _one_shot_service(tmp_path)
    assert CronService(base_dir=tmp_path).update_job(job.id, every_secs=3600)
    listed = _reload(service)
    listed.enabled = False  # the reaper's provisional hold

    with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
        with pytest.raises(OSError, match="terminal write failed"):
            await service._merge_terminal_state_with_retry(
                listed, listed.id, scheduled=False, park_one_shot=True, **_terminal_kwargs()
            )

    assert service.get_job(job.id).enabled is True  # type: ignore[union-attr]
    assert service._held_run_records == {}


@pytest.mark.asyncio
async def test_a_cancel_inside_the_fire_time_gate_lands_its_park(tmp_path: Path) -> None:
    """A plain one-shot cancelled before its payload started is parked by the cancel.
    The held record carries that decision, so the retry writes the park."""
    service, job = _one_shot_service(tmp_path)
    job.run_never_started = True

    with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
        await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, park_one_shot=True, ran=job, **_terminal_kwargs()
        )
    service._tick_scan_locked()

    assert service._held_run_records == {}
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.user_paused) == (False, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("older_build", "on_disk"),
    [
        # A pause is kept: the park is a pause too.
        ({"user_paused": True, "enabled": False}, (False, True)),
        # A pause and a resume both made in the gap leave the record as it was
        # frozen, and the counter did not move, so the park lands over the resume.
        ({"user_paused": False, "enabled": True}, (False, True)),
    ],
)
async def test_an_older_build_s_pause_or_resume_does_not_move_the_counter(
    tmp_path: Path, older_build: dict[str, object], on_disk: tuple[bool, bool]
) -> None:
    """A build without ``user_pause_generation`` pauses or resumes without moving it.
    The pinned outcome is the documented mixed-version limit."""
    service, job = _one_shot_service(tmp_path)
    with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
        await service._merge_terminal_state_with_retry(
            job, job.id, scheduled=True, park_one_shot=True, ran=job, **_terminal_kwargs()
        )

    _rewrite_stored_job(tmp_path, **older_build)
    service._tick_scan_locked()

    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.user_pause_generation == 0
    assert (persisted.enabled, persisted.user_paused) == on_disk


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [True, False])
async def test_a_pause_or_resume_after_the_freeze_is_what_the_cancel_reports(
    tmp_path: Path, resume: bool
) -> None:
    """The cancel's save keeps a pause or resume made after the freeze. The report
    names that choice, not a schedule change or a newer run."""
    service, job = _one_shot_service(tmp_path)
    claim = _RunClaim(
        trigger="scheduled",
        claimed_at=_OCCURRENCE_AT,
        started_monotonic=1.0,
        outcome_reset=False,
        job=job,
        task=MagicMock(done=MagicMock(return_value=False)),
    )
    service._claims[job.id] = claim
    service._history.append = AsyncMock()  # type: ignore[method-assign]
    real_merge = service._merge_terminal_state_locked

    def choice_in_the_gap(*args: object, **kwargs: object) -> str | None:
        assert CronService(base_dir=tmp_path).enable_job(job.id, enabled=resume)
        return real_merge(*args, **kwargs)

    service._merge_terminal_state_locked = choice_in_the_gap  # type: ignore[method-assign]
    with (
        patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
        patch("kiro_crew.cron.sel.sel"),
    ):
        if resume:
            with pytest.raises(CronPauseNotSaved, match="resumed meanwhile") as raised:
                await service.cancel(job.id)
            assert "recurring" not in str(raised.value)
            assert "newer run" not in str(raised.value)
        else:
            assert await service.cancel(job.id) is True

    entry = service._history.append.await_args.args[0]
    assert entry.error.endswith(
        "; the job was resumed meanwhile, so it is back on its schedule now"
        if resume
        else "; the job was paused meanwhile"
    ), entry.error
    assert service._held_run_records == {}
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert (persisted.enabled, persisted.user_paused) == (resume, not resume)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancel", "reap"])
@pytest.mark.parametrize("kind", ["recurring", "delete_after_run one-shot"])
async def test_a_manual_cancel_or_reap_freezes_no_terminal_record(
    tmp_path: Path, terminal: str, kind: str
) -> None:
    """Regression: a manual cancel or reap froze a terminal record too, and for a
    dispatched ``delete_after_run`` one-shot that freeze applied the failed-run
    auto-pause before the merge. A manual run's only terminal write is the merge of
    the call's own fields; the run's own teardown applies its auto-pause."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def on_job(_job: CronJob) -> None:
        started.set()
        await await_until(release, what="the test to release the run")

    service = CronService(base_dir=tmp_path, on_job=on_job)
    await _start_without_timer(service)
    real_freeze = settlement_rules.freeze_terminal_record
    frozen: list[CronJob] = []

    def recording_freeze(*args: object, **kwargs: object) -> CronJob:
        record = real_freeze(*args, **kwargs)  # type: ignore[arg-type]
        frozen.append(record)
        return record

    try:
        with (
            patch(
                "kiro_crew.cron_service.service.run_merge.freeze_terminal_record",
                recording_freeze,
            ),
            patch.object(CronJob, "_audit_pause_change", autospec=True),
            patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
        ):
            if kind == "recurring":
                job = await service.add_job_async("manual", "msg", every_secs=3600)
            else:
                job = await service.add_job_async(
                    "manual", "msg", at_ts=_OCCURRENCE_AT + 3600, delete_after_run=True
                )
            task = asyncio.create_task(service.run_job(job.id))
            service.attach_run_task(job.id, task)
            await await_until(started, what="the manual run to start")
            assert service._claims[job.id].trigger == "manual"
            if terminal == "cancel":
                assert await finished(service.cancel(job.id), "the manual cancel") is True
            else:
                claim = service._claims[job.id]
                await finished(
                    service._force_reap(job.id, elapsed=1900, deadline=1800, claim=claim),
                    "the manual reap",
                )
            with contextlib.suppress(asyncio.CancelledError):
                await await_until(task, what="the cancelled run's teardown")
    finally:
        release.set()
        await service.stop()

    assert frozen == []
    assert service._held_run_records == {}
    persisted = CronService(base_dir=tmp_path).get_job(job.id)
    assert persisted is not None
    assert persisted.last_status == "error"


def _start_run_now_on(service: CronService, job: CronJob) -> _RunClaim:
    """Claim ``job`` for a Run Now as ``_run_job_isolated``'s first step does."""
    claim = _RunClaim(trigger="manual", claimed_at=_OCCURRENCE_AT + 20)
    service._claims[job.id] = claim
    claim.generation = service._runs.next_generation(job)
    claim.job = job
    job.auto_paused_at_start = job.auto_paused
    return claim


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", ["unchanged", "edited in place"])
@pytest.mark.parametrize(("newer_run", "count"), [("fails", 2), ("succeeds", 0)])
async def test_a_held_reap_retried_during_a_newer_run_keeps_that_run_s_count(
    tmp_path: Path, newer_run: str, count: int, schedule: str
) -> None:
    """Regression: a timer tick's retry of a held scheduled reap wrote the reap's
    failure count over a Run Now in flight on the listed job, so the run's failure
    was lost (2 became 1) or its success came back as a failure (0 became 1), and
    the run's own merge persisted that. A run claimed after the record owns the
    count."""
    service, job = _service(tmp_path)
    job.auto_paused_at_start = False  # stamped when the scheduled run started
    with patch.object(service, "_save", side_effect=OSError("terminal write failed")):
        await _end_a_manual_run(service, job, "reap", trigger="scheduled")
    held = service._held_run_records[job.id]
    assert service._held_settlements[job.id] is held
    assert job.consecutive_failures == held.consecutive_failures == 1

    claim = _start_run_now_on(service, job)
    assert claim.generation is not None
    assert claim.generation > held.run_generation == job.run_generation
    if newer_run == "fails":
        job.record_failure()
    else:
        job.record_success()
    if schedule == "edited in place":
        job.schedule = CronSchedule(kind="cron", cron_expr="0 7 * * *")
    service._tick_scan_locked()  # the store is unchanged: the tick retries the held reap
    assert job.consecutive_failures == count

    terminal, _status, _error = execution.close_run(
        job, started_at=_OCCURRENCE_AT + 20, generation=claim.generation, being_cancelled=False
    )
    service._hold_run_record(job, terminal)
    service._runs.release(job.id, claim)
    service._merge_job_result(terminal)
    persisted = _stored(tmp_path)
    assert persisted is not None
    assert persisted.consecutive_failures == count
    assert persisted.run_generation == claim.generation


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancel", "reap"])
async def test_a_terminal_merge_keeps_the_count_of_a_run_claimed_before_it(
    tmp_path: Path, terminal: str
) -> None:
    """Regression: a scheduled cancel or reap releases its claim before its merge
    takes the store lock, and a Run Now accepted in between, whose failure moved the
    listed job's count, had that count reset to the record's by the merge (a reap's
    2 became 1, a cancel's 1 became 0). A run claimed after the record owns the
    count."""
    service, job = _service(tmp_path)
    job.auto_paused_at_start = False  # stamped when the scheduled run started
    real_merge = service._merge_terminal_state_locked
    claims: list[_RunClaim] = []
    counted = 1 if terminal == "reap" else 0  # a user cancel counts no failure

    def run_now_before_the_merge(*args: object, **kwargs: object) -> str | None:
        record = kwargs["terminal_record"]
        assert isinstance(record, CronJob)
        # The hold gave the listed job the record before the claim was released.
        assert job.consecutive_failures == record.consecutive_failures == counted
        claims.append(_start_run_now_on(service, job))
        job.record_failure()
        return real_merge(*args, **kwargs)

    service._merge_terminal_state_locked = run_now_before_the_merge  # type: ignore[method-assign]
    await _end_a_manual_run(service, job, terminal, trigger="scheduled")

    assert len(claims) == 1
    assert service._held_run_records == {}
    assert job.consecutive_failures == counted + 1
