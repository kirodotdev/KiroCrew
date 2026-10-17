"""Integration tests for a fired cron job's run lifecycle.

Auto-pause after repeated failures, the status and result the run funnel keeps,
queue time in the bounded worker pool (not charged against the job's own timeout),
and the claim: a claim-backstop timeout counts toward auto-pause only when its
payload dispatched, and neither it nor a cancellation at the claim await consumes a
one-shot whose payload never ran.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import (
    _make_command_job,
    _make_gw,
    _make_script_job,
    _run_command_callback,
    _run_script_callback,
)

import kiro_crew.executors as ex
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout, finished


async def _stalled_gate(*_args, **_kwargs):
    """Stand in for a fire-time gate that outlives the wake deadline.

    Models a recoverable event-loop stall rather than a slow gate: it is the
    whole ``run_in_cron_gate_pool`` call that fails to complete, so neither of
    the gate's OWN bounds is what stops it. That is the only way to reach the
    state where ``asyncio.wait_for`` in ``_execute_with_timeout`` cancels the
    coroutine AT the await -- no internal deadline sizing can prevent it.
    """
    await asyncio.Event().wait()  # parked until the wake deadline cancels it


async def _occupy_every_worker(executor, workers, release, hold_secs):
    """Submit one blocker per worker and return once each holds a thread.

    Returns the blockers' futures for :func:`_drain_blockers`.
    """
    entered: list[int] = []
    lock = threading.Lock()

    def _block() -> None:
        with lock:
            entered.append(1)
        release.wait(hold_secs)

    futures = [executor.submit(_block) for _ in range(workers)]
    await async_wait_until(
        lambda: len(entered) == workers, describe=lambda: f"{len(entered)} of {workers} started"
    )
    return futures


async def _drain_blockers(futures) -> None:
    """Wait for released blockers to return before their pool is shut down."""
    if futures:
        await finished(
            asyncio.gather(*(asyncio.wrap_future(f) for f in futures)), "the released blockers"
        )


class TestAutoPause:
    """Test that jobs auto-pause after 5 consecutive failures."""

    @pytest.mark.asyncio
    async def test_script_auto_pauses_after_5_errors(self):
        gw = _make_gw()
        job = _make_script_job()
        for i in range(4):
            await _run_script_callback(gw, job, {"status": "error", "error": f"fail {i}"})
            assert job.enabled is True, f"Should not pause after {i+1} failures"
            assert job.auto_paused is False
        await _run_script_callback(gw, job, {"status": "error", "error": "fail 4"})
        assert job.enabled is False
        # auto_paused is the durable reason the pause survives a reload; without
        # it, _load re-derives enabled=True (user_paused stays False) and the
        # failing job resurrects on the next daemon restart.
        assert job.auto_paused is True
        assert job.consecutive_failures == 5

    @pytest.mark.asyncio
    async def test_command_auto_pauses_after_5_errors(self):
        gw = _make_gw()
        job = _make_command_job()
        for i in range(4):
            await _run_command_callback(
                gw, job, {"status": "error", "output": f"err {i}", "exit_code": 1}
            )
            assert job.enabled is True
        await _run_command_callback(gw, job, {"status": "error", "output": "err 4", "exit_code": 1})
        assert job.enabled is False
        assert job.auto_paused is True
        assert job.consecutive_failures == 5

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        gw = _make_gw()
        job = _make_script_job()
        for _ in range(4):
            await _run_script_callback(gw, job, {"status": "error", "error": "fail"})
        assert job.consecutive_failures == 4
        await _run_script_callback(gw, job, {"status": "ok"})
        assert job.consecutive_failures == 0
        assert job.enabled is True
        assert job.auto_paused is False


class TestExecutePreservesCallbackStatus:
    """CronService._execute must not clobber a failure the callback reported by
    mutating the job. Command/script callbacks return NORMALLY and signal
    failure via job.last_status="error"; only the LLM path raises. Overwriting
    unconditionally with "ok" mis-reported failed runs as healthy on the
    dashboard and in cron_list.
    """

    @pytest.mark.asyncio
    async def test_execute_preserves_callback_error(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)

        async def failing_cb(job):
            # command/script contract: report failure by mutation, return normally
            job.last_status = "error"
            job.last_error = "command failed (exit_code=1)"

        svc._on_job = failing_cb
        job = svc.add_job("failing", "false", every_secs=3600)
        await svc._execute(job)
        assert job.last_status == "error"
        assert job.last_error == "command failed (exit_code=1)"

    @pytest.mark.asyncio
    async def test_execute_marks_ok_when_callback_clean(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)

        async def clean_cb(job):
            return None  # no error mutation, no raise

        svc._on_job = clean_cb
        job = svc.add_job("okjob", "echo hi", every_secs=3600)
        await svc._execute(job)
        assert job.last_status == "ok"
        assert job.last_error is None

    @pytest.mark.asyncio
    async def test_execute_marks_error_when_callback_raises(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)

        async def raising_cb(job):
            raise RuntimeError("boom")

        svc._on_job = raising_cb
        job = svc.add_job("raises", "x", every_secs=3600)
        await svc._execute(job)
        assert job.last_status == "error"
        assert "boom" in job.last_error

    @pytest.mark.asyncio
    async def test_execute_clears_stale_error_on_success(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        results = ["error", "clean"]

        async def cb(job):
            if results.pop(0) == "error":
                job.last_status = "error"
                job.last_error = "prior failure"

        svc._on_job = cb
        job = svc.add_job("flappy", "cmd", every_secs=3600)
        await svc._execute(job)  # first run fails
        assert job.last_status == "error"
        await svc._execute(job)  # second run clean → must reset to ok, not stay error
        assert job.last_status == "ok"
        assert job.last_error is None


def test_shutdown_cancel_keeps_the_last_completed_result(tmp_path) -> None:
    """A shutdown cancel must not wipe the previous run's result.

    stop() cancels the in-flight task but never adds the job to the cancel markers,
    so the funnel's result-less clear would otherwise run on every gateway stop
    and persist an empty result over the last completed run's output.
    """
    import asyncio

    from kiro_crew.cron import CronJob, CronSchedule, CronService

    started = asyncio.Event()

    async def _hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()  # parked until the test cancels the run

    async def _drive() -> CronJob:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="test",
            message="go",
            command="echo hi",
            schedule=CronSchedule(kind="every", every_secs=60),
            last_result="42 widgets",
        )
        svc._jobs = [job]
        svc._save()
        with patch.object(svc, "_execute", side_effect=_hang):
            claim = svc._claim_run(job.id, "scheduled")
            task = asyncio.create_task(svc._run_job_isolated(job, claim))
            # Cancel the run inside its execute, the way a gateway stop lands.
            await await_until(started, what="the run's execute")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await await_until(task, what="the cancelled run")
        return job

    job = asyncio.run(_drive())
    assert job.last_result == "42 widgets", "a shutdown cancel wiped a completed result"


async def _run_script_callback_behind_a_busy_worker(gw, job, script_result, hold_secs):
    """Drive the script branch with the cron pool's only worker already busy.

    The blocker is submitted from INSIDE the fire-time gate. That ordering is
    what puts it ahead of the script in the cron pool's FIFO queue, so the script
    genuinely waits for a worker -- the production condition. Submitting it up
    front would let the gate run first and the script would then find a free
    worker and never queue.

    The gate itself does not touch this pool: it runs on the dedicated
    ``mc-crongate`` pool with a bound of its own, so starving the cron pool
    cannot starve the gate. That independence is the point -- it is why this
    fixture can saturate the cron pool without perturbing the gate under test.
    """
    captured_cb = None
    release = threading.Event()

    def _occupy() -> None:
        release.wait(timeout=60)

    def _vet(_job):
        ex.cron_executor().submit(_occupy)
        return None

    ex.shutdown_maintenance_executor()
    with (
        patch.object(ex, "_MAX_CRON_WORKERS", 1),
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch(
            "kiro_crew.slack.gateway.run_script_sandboxed", return_value=script_result
        ) as mock_run,
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", side_effect=_vet),
        patch("kiro_crew.slack.gateway.sel"),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        try:
            await gw._init_cron()
            assert captured_cb is not None
            asyncio.get_running_loop().call_later(hold_secs, release.set)
            return await captured_cb(job), mock_run
        finally:
            release.set()
            ex.shutdown_maintenance_executor()


class TestCronPoolQueueWait:
    """Queue time in the bounded cron pool must not be charged to the job.

    ``run_in_executor`` only SUBMITS. The cron pool is bounded at
    ``_MAX_CRON_WORKERS``, so a job whose workers are all busy sits in the
    pool's queue having run nothing -- while the old
    ``wait_for(run_in_executor(...), timeout=job_timeout + 5)`` counted from the
    moment it was awaited. Queue time was therefore spent out of the job's own
    budget, and the kill was recorded as ``timeout (Ns)`` naming the script, so
    one wedged job reads as N broken ones.
    """

    @pytest.mark.asyncio
    async def test_fast_script_waiting_for_a_worker_is_not_killed_as_a_script_timeout(self):
        """Negative control: fails pre-fix, for the reason the defect describes.

        The script here is instant (mocked ok) but the only worker is held for
        6.5s, past the 6s the pre-fix budget allowed a 1s-timeout job. Pre-fix
        this is killed and labelled ``timeout (6s)``; the fix lets it wait for a
        worker untimed and then run.
        """
        gw = _make_gw()
        job = _make_script_job(timeout=1)  # pre-fix budget: 1 + 5 = 6s

        result, mock_run = await _run_script_callback_behind_a_busy_worker(
            gw, job, {"status": "ok"}, hold_secs=6.5
        )

        assert mock_run.called, "the script never ran -- it was killed while queued"
        assert job.last_status == "ok", f"expected ok, got {job.last_status}: {job.last_error}"
        assert job.last_error == ""
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_starved_script_says_queued_never_ran_not_timeout(self):
        """The distinct text is the point: saturation must not read as N defects."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        result, _ = await _run_script_callback(gw, job, side_effect=ex.CronQueueTimeout(300.0))
        assert result is None
        assert job.last_status == "error"
        assert job.last_error == "queued 300s, never ran"
        # Must NOT be mistakable for the script itself overrunning.
        assert "timeout (" not in job.last_error
        # A failed run must not present the previous run's output as its own.
        assert job.last_result == ""

    @pytest.mark.asyncio
    async def test_starved_command_says_queued_never_ran_not_timeout(self):
        """The command branch carries the same defect and the same fix."""
        gw = _make_gw()
        job = _make_command_job(last_result="42 widgets")
        result, _ = await _run_command_callback(
            gw, job, side_effect=ex.CronQueueTimeout(300.0), vet_reason=None
        )
        assert result is None
        assert job.last_status == "error"
        assert job.last_error == "queued 300s, never ran"
        assert "timeout (" not in job.last_error
        assert job.last_result == ""

    @pytest.mark.asyncio
    async def test_a_real_script_overrun_is_still_reported_as_a_timeout(self):
        """The backstop label must survive: only the QUEUE case gets new text.

        CronQueueTimeout subclasses asyncio.TimeoutError, so this also pins the
        except-order -- a plain TimeoutError must not be captured by the queue
        branch and mislabelled.
        """
        gw = _make_gw()
        job = _make_script_job(timeout=30)
        result, _ = await _run_script_callback(gw, job, side_effect=asyncio.TimeoutError())
        assert result is None
        assert job.last_status == "error"
        assert job.last_error == "timeout (35s)"
        assert "queued" not in job.last_error

    @pytest.mark.asyncio
    async def test_repeated_starvation_never_auto_pauses_a_healthy_script(self):
        """Starvation must not consume the auto-pause budget.

        ``record_failure`` auto-pauses at ``_AUTO_PAUSE_THRESHOLD`` by clearing
        ``enabled``, and a paused job never fires again. Counting a queue timeout
        there means a saturated pool permanently disables jobs that never ran a
        line, so when the pool recovers the work is silently unscheduled. The
        fire-time governance deny path already declines to count for exactly this
        reason. Asserts the counter and the pause flag, not merely the absence of
        an exception.
        """
        from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD

        gw = _make_gw()
        job = _make_script_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD + 2):
            result, _ = await _run_script_callback(gw, job, side_effect=ex.CronQueueTimeout(300.0))
            assert result is None
        assert job.consecutive_failures == 0, "starvation must not consume the auto-pause budget"
        assert not job.auto_paused, "a job that never ran must not be auto-paused"
        assert job.enabled, "auto-pause clears enabled -- the job would never fire again"
        # Still reported as an error, with the legible starvation text.
        assert job.last_status == "error"
        assert job.last_error == "queued 300s, never ran"

    @pytest.mark.asyncio
    async def test_repeated_starvation_never_auto_pauses_a_healthy_command(self):
        """The command branch carries the same counter and the same fix."""
        from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD

        gw = _make_gw()
        job = _make_command_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD + 2):
            result, _ = await _run_command_callback(
                gw, job, side_effect=ex.CronQueueTimeout(300.0), vet_reason=None
            )
            assert result is None
        assert job.consecutive_failures == 0
        assert not job.auto_paused
        assert job.enabled

    @pytest.mark.asyncio
    async def test_the_enclosing_wake_deadline_also_excludes_queue_wait(self, tmp_path):
        """Excluding queue wait from the per-call kwarg is only half the job.

        ``CronService._execute_with_timeout`` wraps the whole run and the pool's
        queue wait happens inside it, so without a queue term there a job still
        sitting in the queue is killed by the WAKE deadline and reported as an
        execution overrun -- the same misdiagnosis, one frame out. Worse, a thread
        cannot be interrupted, so a worker that claimed the call as the deadline
        fired keeps running while the overlap guards clear.

        Fails pre-fix: the command job below is killed at its 2s wake budget with
        ``Timed out after 2s`` and its payload never runs.
        """
        from kiro_crew.cron import CronService

        ex.shutdown_maintenance_executor()
        release = threading.Event()
        blockers: list = []
        try:
            svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
            job = _make_command_job(timeout_secs=2)
            blockers = await _occupy_every_worker(
                ex.cron_executor(), ex._MAX_CRON_WORKERS, release, 3.0
            )

            ran = threading.Event()

            async def _execute(_job, _meta=None):
                return await ex.run_in_cron_pool(ran.set, timeout=30, queue_timeout=60)

            svc._execute = _execute  # type: ignore[method-assign]
            await svc._execute_with_timeout(job)

            assert ran.is_set(), "the job was killed by the wake deadline while still queued"
            assert job.last_error != "Timed out after 2s"
        finally:
            release.set()
            await _drain_blockers(blockers)
            ex.shutdown_maintenance_executor()

    @pytest.mark.asyncio
    async def test_a_message_job_wake_budget_is_not_widened(self, tmp_path):
        """The queue term is scoped: a message job never touches the pool.

        Widening every job's wake budget would loosen the backstop that exists so
        a wedged worker cannot leave an entry un-failed forever. Only command and
        script jobs dispatch through the pool, so only they get the allowance.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = CronJob(
            id="mj1",
            name="msg-job",
            message="hello",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        job.timeout_secs = 2

        async def _slow(_job, _meta=None):
            await asyncio.Event().wait()  # parked until the wake budget cancels it

        svc._execute = _slow  # type: ignore[method-assign]
        await svc._execute_with_timeout(job)
        assert job.last_status == "error"
        assert job.last_error == "Timed out after 2s"

    @pytest.mark.asyncio
    async def test_a_starved_one_shot_job_is_retained_not_deleted(self, tmp_path):
        """A one-shot that never ran must not be consumed by the run it never had.

        ``_merge_job_result`` deletes a ``delete_after_run`` job unless the run was
        refused, and the queue-timeout paths return without marking a refusal -- so
        a job that never got a worker is destroyed exactly as if it had completed,
        and its scheduled work is gone with it. The fire-time deny path already
        carries the retain-on-refusal property; starvation needs the same one for
        the same reason, without borrowing the *policy* meaning of that flag.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot",
            message="",
            every_secs=300,
            command="echo hi",
            delete_after_run=True,
        )
        assert any(j.id == created.id for j in svc.list_jobs())

        gw = _make_gw()
        result, _ = await _run_command_callback(
            gw, created, side_effect=ex.CronQueueTimeout(300.0), vet_reason=None
        )
        assert result is None
        assert created.last_error == "queued 300s, never ran"

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, created)
        assert any(
            j.id == created.id for j in svc.list_jobs()
        ), "a one-shot that never ran was deleted as though it had run"

    @pytest.mark.asyncio
    async def test_a_starved_one_shot_at_job_is_not_parked_disabled(self, tmp_path):
        """Retention must not borrow the policy-denial meaning.

        ``fire_time_denied`` is read in three places: besides suppressing the
        one-shot delete it also forces an ``at`` job disabled, and it is documented
        as a governance refusal. Reusing it for a capacity event would park a
        starved one-shot needing an operator to re-enable it, and would mislabel
        pool saturation as a policy denial in history. Starvation clears on its own,
        so the job must stay schedulable.
        """
        import time

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot-at",
            message="",
            at_ts=time.time() + 3600,
            command="echo hi",
            delete_after_run=True,
        )

        gw = _make_gw()
        result, _ = await _run_command_callback(
            gw, created, side_effect=ex.CronQueueTimeout(300.0), vet_reason=None
        )
        assert result is None
        assert not created.fire_time_denied, "starvation is not a governance denial"

        await asyncio.to_thread(svc._merge_job_result, created)
        survivors = [j for j in svc.list_jobs(include_disabled=True) if j.id == created.id]
        assert survivors, "the starved one-shot at-job was deleted"
        assert survivors[0].enabled, "a starved job must stay schedulable, not be parked"
        assert not survivors[0].user_paused

    @staticmethod
    async def _one_reaper_sweep(svc, job, elapsed_secs):
        """Drive exactly one reaper sweep and report whether it force-killed.

        Registers the job as in-flight for ``elapsed_secs`` with a task that is
        NOT done, so the loop's ``task.done()`` early-continue cannot mask the
        decision, then runs the loop just long enough for one sweep.
        """
        import time as _time
        from unittest.mock import AsyncMock as _AsyncMock

        from kiro_crew.cron import _RunClaim

        never = asyncio.get_running_loop().create_future()  # a task that is not done
        holder = asyncio.ensure_future(asyncio.wait_for(never, timeout=30))
        svc._claims[job.id] = _RunClaim(
            trigger="scheduled", claimed_at=_time.time() - elapsed_secs, task=holder
        )
        svc._jobs = [job]
        reaped = _AsyncMock()
        svc._force_reap = reaped  # type: ignore[method-assign]
        # Every sweep asks the claim whether its run finished before it decides,
        # so a second ask proves the first sweep's decision has been taken.
        sweeps = 0
        real_discard = svc.discard_finished_run

        def _counted_discard(job_id):
            nonlocal sweeps
            sweeps += 1
            return real_discard(job_id)

        svc.discard_finished_run = _counted_discard  # type: ignore[method-assign]
        with patch("kiro_crew.cron._REAPER_INTERVAL", 0.01):
            loop_task = asyncio.ensure_future(svc._reaper_loop())
            await async_wait_until(lambda: sweeps >= 2, describe=lambda: f"sweeps={sweeps}")
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        never.cancel()
        holder.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await holder
        return reaped.called

    @pytest.mark.asyncio
    async def test_the_reaper_does_not_preempt_a_job_inside_the_widened_window(self, tmp_path):
        """The reaper must account for queue wait too, or it cancels a queued job.

        Two deadlines bound one run. Widening only the execution guard leaves the
        reaper's own threshold 900s lower for every command/script job at or above
        the default wake budget, so a job whose gate and payload queued through
        that window is force-killed and the firing is skipped without ever
        executing -- the mirror image of the misdiagnosis the widening removed.

        Fails pre-fix: with the reaper unchanged its deadline is 1800s, the job
        below has been in flight 1850s, and it is reaped.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = _make_command_job(timeout_secs=1800)  # the default wake budget
        # 1850s: past the reaper's un-widened 1800s, inside the 2700s execution
        # deadline the job is actually entitled to.
        assert (
            await self._one_reaper_sweep(svc, job, 1850) is False
        ), "the reaper force-killed a job still inside its own execution deadline"

    @pytest.mark.asyncio
    async def test_the_reaper_still_kills_a_job_past_the_widened_deadline(self, tmp_path):
        """The defence-in-depth sweep must keep its teeth.

        The point of the reaper is to catch a run whose ``wait_for`` never fired.
        Accounting for queue wait must move that threshold, not remove it.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = _make_command_job(timeout_secs=1800)
        assert (
            await self._one_reaper_sweep(svc, job, 2800) is True
        ), "the reaper stopped killing a genuinely overrunning job"

    @pytest.mark.asyncio
    async def test_the_reaper_window_is_not_widened_for_a_message_job(self, tmp_path):
        """Scoped, like the execution deadline: a message job never uses the pool.

        Widening the reaper for every job would delay the force-kill backstop by a
        quarter of an hour for runs that can never have queued.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = CronJob(
            id="mj-reap",
            name="msg-job",
            message="hello",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        job.timeout_secs = 1800
        assert (
            await self._one_reaper_sweep(svc, job, 1850) is True
        ), "a message job got a pool queue allowance it can never use"

    @pytest.mark.asyncio
    async def test_a_message_jobs_fire_time_gate_does_not_spend_its_execution_budget(
        self, tmp_path
    ):
        """The fire-time governance gate must not queue behind long-running cron work.

        Both deadlines deliberately withhold the pool allowance from a message job,
        on the premise that it never touches the pool. The FIRE-TIME GOVERNANCE GATE
        breaks that premise. ``vet_job_at_fire_time`` is dispatched off-loop for
        every job kind, and dispatching it to the CRON pool puts it behind however
        many long-running command/script jobs hold that pool's
        ``_MAX_CRON_WORKERS`` workers. The wait happens inside the deadline
        ``_execute_with_timeout`` arms BEFORE the callback runs, so a saturated pool
        can spend a message job's entire execution budget before its own dispatch is
        even reached -- and a one-shot is then deleted having never run.

        Widening the message job's budget is the wrong lever: it would delay the
        wedged-delivery backstop by the whole allowance for every message job,
        which is exactly what the two "not widened" tests above pin. The gate
        belongs off the pool that long-running work occupies -- it is a short,
        bounded policy check, not job execution.

        Fails pre-fix: with every cron worker held, the gate queues and the job is
        killed at its 2s budget without ever dispatching.
        """
        from kiro_crew.cron import CronService

        ex.shutdown_maintenance_executor()
        release = threading.Event()
        blockers: list = []
        try:
            gw = _make_gw()
            captured_cb = None

            with (
                patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
                patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
                patch("kiro_crew.slack.gateway.sel"),
            ):

                def capture_cron(on_job=None, **kw):
                    nonlocal captured_cb
                    captured_cb = on_job
                    svc = MagicMock()
                    svc.start = AsyncMock()
                    svc.remove_job_async = AsyncMock(return_value=True)
                    return svc

                mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
                await gw._init_cron()
                assert captured_cb is not None

                # The defect's own premise: long-running pool jobs holding every
                # worker, held well past the message job's whole 2s budget.
                blockers = await _occupy_every_worker(
                    ex.cron_executor(), ex._MAX_CRON_WORKERS, release, 5.0
                )

                job = CronJob(
                    id="mj-gate",
                    name="msg-job",
                    message="hello",
                    schedule=CronSchedule(kind="every", every_secs=60),
                )
                job.timeout_secs = 2

                svc = CronService(base_dir=tmp_path, on_job=captured_cb)
                await svc._execute_with_timeout(job)

            assert gw.sessions.get_or_create.called, (
                "the fire-time gate wait consumed the message job's whole execution "
                "budget: it was killed before its own dispatch was reached"
            )
            assert job.last_error != "Timed out after 2s"
        finally:
            release.set()
            await _drain_blockers(blockers)
            ex.shutdown_maintenance_executor()

    @pytest.fixture
    def gate_pool(self):
        """Fresh pools, plus a release event so a starved gate never outlives the test.

        The blockers a starved run parks on the gate pool are drained after the
        release, so none of their threads is still running past the shutdown.
        """
        ex.shutdown_maintenance_executor()
        release = threading.Event()
        self._gate_blockers: list = []
        try:
            yield release
        finally:
            release.set()
            _, still_running = concurrent.futures.wait(
                self._gate_blockers, timeout=default_timeout()
            )
            ex.shutdown_maintenance_executor()
            assert not still_running, f"{len(still_running)} gate blocker(s) never returned"

    async def _message_one_shot_run(
        self,
        tmp_path,
        release,
        *,
        starve: bool,
        budget: int = 8,
        slow_gate: bool = False,
        cancel_gate: bool = False,
    ):
        """Drive the REAL message callback under the REAL wake deadline.

        Returns ``(svc, job, gw)`` after ``_execute_with_timeout`` has returned, so
        the caller can run the real ``_merge_job_result`` and inspect retention.
        ``starve`` occupies the gate pool so the call never gets a worker (the
        QUEUE bound). ``slow_gate`` leaves the pool free and makes the gate's own
        work overrun (the EXECUTION bound). Those two bounds fail differently
        inside ``run_in_cron_pool``, which is the whole reason both are driven.
        ``cancel_gate`` stalls the whole gate call so NEITHER bound fires and the
        real wake deadline cancels the await instead -- a third outcome the other
        two are structurally blind to, because they raise something the caller's
        ``except`` clause catches and a ``CancelledError`` is not caught there.
        """
        from kiro_crew.cron import CronService

        gw = _make_gw()
        captured_cb = None
        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch("kiro_crew.slack.gateway.sel"),
        ):

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                stub = MagicMock()
                stub.start = AsyncMock()
                stub.remove_job_async = AsyncMock(return_value=True)
                return stub

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
            await gw._init_cron()
            assert captured_cb is not None

            if starve:
                self._gate_blockers += await _occupy_every_worker(
                    ex.cron_gate_executor(), ex._MAX_CRON_GATE_WORKERS, release, 20.0
                )

            svc = CronService(base_dir=tmp_path, on_job=captured_cb)
            svc._load()
            job = svc.add_job(
                name="msg-one-shot",
                message="hello",
                every_secs=300,
                delete_after_run=True,
            )
            job.timeout_secs = budget
            assert any(j.id == job.id for j in svc.list_jobs())
            # slow_gate leaves the pool FREE, so the call is claimed at once and
            # then overruns its own bound -- the execution phase, not the queue
            # phase, and the two raise different exception types.
            gate_work = (
                patch(
                    "kiro_crew.slack.gateway.vet_job_at_fire_time",
                    lambda job: release.wait(20.0),
                )
                if slow_gate
                else nullcontext()
            )
            gate_stall = (
                patch("kiro_crew.slack.gateway.run_in_cron_gate_pool", _stalled_gate)
                if cancel_gate
                else nullcontext()
            )
            with gate_work, gate_stall:
                await svc._execute_with_timeout(job)
        return svc, job, gw

    @pytest.mark.asyncio
    async def test_a_starved_message_one_shot_survives_the_wake_deadline(self, tmp_path, gate_pool):
        """A message one-shot whose GATE is starved must not be consumed.

        The fire-time gate is awaited inside the wake deadline
        ``_execute_with_timeout`` has already armed, and a message job carries no
        ``_pool_queue_allowance`` to cover it. Pre-fix the gate had no bound of its
        own, so a busy pool let the WAKE deadline expire first --
        ``_execute_with_timeout`` then caught the ``TimeoutError`` and returned
        normally, ``_merge_job_result`` saw an ordinary finished run, and the
        ``delete_after_run`` job was deleted having never dispatched.

        The gate's bound is now capped below the wake budget so starvation
        surfaces as ``CronQueueTimeout`` FIRST, which is what makes the existing
        ``run_never_started`` retention marker reachable on this path.

        Fails pre-fix: with the gate unbounded the job is gone from the store.
        """
        svc, job, gw = await self._message_one_shot_run(tmp_path, gate_pool, starve=True)

        assert job.run_never_started is True, "starvation did not mark the run as never started"
        assert job.last_error.startswith(
            "fire-time gate "
        ), f"gate starvation was not reported as such: {job.last_error!r}"
        assert not gw.sessions.get_or_create.called, "the job dispatched; this is not a starved run"

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, job)
        assert any(
            j.id == job.id for j in svc.list_jobs()
        ), "the starved message one-shot was deleted by a run that never dispatched"

    @pytest.mark.asyncio
    async def test_a_dispatched_message_one_shot_is_still_deleted(self, tmp_path, gate_pool):
        """Negative control: retention must not leak onto a run that DID dispatch.

        Same path with the gate pool free. The one-shot reaches its dispatch and
        must then be consumed exactly as before -- otherwise the retention marker
        has stopped meaning "never started" and every message one-shot would
        survive forever. This fails for that reason if the marker is set
        unconditionally rather than only on ``CronQueueTimeout``.
        """
        svc, job, gw = await self._message_one_shot_run(tmp_path, gate_pool, starve=False)

        assert (
            gw.sessions.get_or_create.called
        ), "the control never dispatched, so it proves nothing"
        assert job.run_never_started is False, "a dispatched run was marked never-started"

        await asyncio.to_thread(svc._merge_job_result, job)
        assert not any(
            j.id == job.id for j in svc.list_jobs()
        ), "a message one-shot that DID dispatch was retained; the marker is too broad"

    @pytest.mark.asyncio
    async def test_the_execution_deadline_accounts_for_the_gate_bound(self, tmp_path):
        """The gate's bound is a THIRD term the run deadline has to carry.

        The gate is awaited before the pool dispatch and INSIDE the deadline armed
        for the whole run, so a gate spending its full bound leaves the subprocess
        that much less. A thread cannot be interrupted, so when the deadline then
        fires with a worker already claimed, the overlap guards clear while the
        subprocess runs on and the next wake duplicates its side effects. That is
        the hazard ``_SUBPROC_CLEANUP_ALLOWANCE_SECS`` exists for; the queue wait
        was a second term it did not account for, and this is a third.

        Observes the deadline the code actually arms, by capturing the ``timeout``
        handed to the enclosing ``wait_for`` -- asserting on the allowance helper
        instead would pass with the term missing from the deadline entirely.

        Fails pre-fix: 2700s armed where 2730s is owed.
        """
        import math

        import kiro_crew.cron as cron_mod
        from kiro_crew.cron import (
            CronService,
            _pool_queue_allowance,
            _vet_allowance,
            effective_wake_budget,
        )

        real_wait_for = asyncio.wait_for

        async def _capture(aw, timeout=None):
            captured.setdefault("timeout", timeout)
            return await real_wait_for(aw, timeout=timeout)

        for job in (_make_command_job(timeout_secs=1800), _make_command_job(timeout_secs=8)):
            captured: dict[str, object] = {}
            svc = CronService(base_dir=tmp_path, on_job=AsyncMock(return_value=None))
            with patch.object(cron_mod.asyncio, "wait_for", _capture):
                await svc._execute_with_timeout(job)
            wake = effective_wake_budget(job)
            gate = ex.cron_gate_budget(wake)
            owed = wake + _pool_queue_allowance(job) + math.ceil(gate) + _vet_allowance(job)
            assert captured["timeout"] == owed, (
                f"armed {captured['timeout']}s for a run owed {owed}s "
                f"(wake {wake} + queue {_pool_queue_allowance(job)} + gate {gate} "
                f"+ claim-time vet {_vet_allowance(job)})"
            )
            # Integer, or "Timed out after 2s" reaches the operator as "2.0s".
            assert isinstance(captured["timeout"], int)

        # A message job dispatches nothing through the pool, so it carries no
        # claimed-worker hazard and its budget stays exactly as set -- its
        # protection is that the gate's bound lands strictly below it.
        captured = {}
        msg = CronJob(id="m", name="n", message="hi", timeout_secs=8)
        svc = CronService(base_dir=tmp_path, on_job=AsyncMock(return_value=None))
        with patch.object(cron_mod.asyncio, "wait_for", _capture):
            await svc._execute_with_timeout(msg)
        assert captured["timeout"] == 8, "a message job's wake budget was widened"
        assert ex.cron_gate_budget(8) < 8

    @pytest.mark.asyncio
    async def test_the_reaper_window_also_accounts_for_the_gate_bound(self, tmp_path):
        """Both deadlines must move together or one pre-empts the other.

        This branch already fixed that once for the queue allowance: widening only
        the execution guard left the reaper lower, so it force-killed runs still
        inside their own deadline. Adding the gate term to one and not the other
        re-opens exactly that gap, 30s wide at the default budget.

        Fails pre-fix: the reaper's threshold is 2700s, the job below has been in
        flight 2715s, and it is reaped while still inside its execution deadline.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = _make_command_job(timeout_secs=1800)
        assert (
            await self._one_reaper_sweep(svc, job, 2715) is False
        ), "the reaper force-killed a job inside the gate-inclusive execution deadline"

    @pytest.mark.asyncio
    async def test_a_slow_gate_does_not_consume_a_message_one_shot(self, tmp_path, gate_pool):
        """A gate that RUNS but overruns its bound must retain its one-shot.

        The sibling case covers a STARVED gate, which never gets a worker and
        raises ``CronQueueTimeout``. This one gets a worker immediately and then
        overruns -- ``run_in_cron_pool``'s execution phase, which raised a plain
        ``asyncio.TimeoutError``. That bypassed the caller's
        ``except CronQueueTimeout``, so ``run_never_started`` stayed false, the
        wake deadline caught the bare timeout as an ordinary overrun, and the
        ``delete_after_run`` job was deleted having never dispatched.

        Fails pre-fix on the job being gone from the store.
        """
        svc, job, gw = await self._message_one_shot_run(
            tmp_path, gate_pool, starve=False, slow_gate=True
        )

        assert job.run_never_started is True, "a gate that overran its bound was not retained"
        assert not gw.sessions.get_or_create.called, "the job dispatched; the gate did not overrun"
        await asyncio.to_thread(svc._merge_job_result, job)
        assert any(
            j.id == job.id for j in svc.list_jobs()
        ), "a message one-shot was deleted by a gate that overran its own bound"

    @pytest.mark.asyncio
    async def test_a_gate_cancelled_by_the_wake_deadline_keeps_its_one_shot(
        self, tmp_path, gate_pool
    ):
        """A cancellation landing ON the gate await must still retain the one-shot.

        The gate's bounds are sized to fire before the wake deadline, but no
        internal sizing survives a stall that pushes wall clock past BOTH of them:
        ``asyncio.wait_for`` in ``_execute_with_timeout`` then cancels this
        coroutine AT the await, and a ``CancelledError`` is not a
        ``CronQueueTimeout``, so the ``except`` clause that sets the retention
        marker never executes at all. ``_execute_with_timeout`` catches the
        timeout and returns normally, so ``_merge_job_result`` saw an ordinary
        finished run and consumed a ``delete_after_run`` job that never
        dispatched.

        The starvation and overrun siblings both RAISE something the handler
        catches, which is exactly why they are blind to this: they can only prove
        the handler works, never that it runs. The marker is therefore set BEFORE
        the await and cleared only once a verdict comes back.

        Fails pre-fix on the job being gone from the store.
        """
        svc, job, gw = await self._message_one_shot_run(
            tmp_path, gate_pool, starve=False, budget=1, cancel_gate=True
        )

        assert (
            not gw.sessions.get_or_create.called
        ), "the job dispatched, so the gate was never cancelled and this proves nothing"
        assert (
            job.run_never_started is True
        ), "a run cancelled at the fire-time gate was not marked never-started"

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, job)
        assert any(
            j.id == job.id for j in svc.list_jobs()
        ), "the one-shot was deleted by a run cancelled at the gate before it dispatched"

    @pytest.mark.asyncio
    async def test_a_returned_verdict_clears_the_retention_marker(self, tmp_path):
        """The opposite direction: pre-setting the marker must not leak.

        Defaulting to retain is only safe if every path that reaches a verdict
        clears it again. Miss one and a HEALTHY gate leaves the marker set, so the
        delete site retains a one-shot that already ran -- silent duplicate firing
        or a job that never leaves the queue, which is the same class of bug
        pointing the other way.

        Both verdict shapes are driven, since an allow and a deny leave the gate
        by the same return: the deny still has to clear this marker, because its
        retention is owned by ``fire_time_denied``, whose readers also park an
        at-job disabled. Conflating them would park a job for a policy decision
        that was never made.
        """
        from kiro_crew.slack.gateway import _await_cron_fire_time_gate

        for verdict in (None, "denied by policy"):
            job = CronJob(
                id="j1",
                name="n",
                message="hello",
                schedule=CronSchedule(kind="every", every_secs=300),
            )
            job.run_never_started = True  # a stale True must not survive a verdict

            async def _verdict_gate(*_a, _v=verdict, **_kw):
                return _v

            with patch("kiro_crew.slack.gateway.run_in_cron_gate_pool", _verdict_gate):
                reason, starved = await _await_cron_fire_time_gate(
                    job, tool_name="t", tool_kind="k"
                )

            assert reason == verdict
            assert starved is False
            assert (
                job.run_never_started is False
            ), f"a returned verdict ({verdict!r}) left the retention marker set"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind,extra,retained",
        [
            ("message", {}, True),
            ("command", {"command": "echo hi"}, False),
            ("script", {"script": "~/.meshclaw/crons/f.py:g"}, False),
        ],
    )
    async def test_gate_starvation_clears_the_carried_result_only_for_command_and_script(
        self, kind, extra, retained
    ):
        """Starvation at the gate must not drop a message job's dedup context.

        ``last_result`` is a CROSS-RUN field: build_cron_session_context prepends
        it as "[Previous run result -- do NOT repeat the same content]" for a
        persistent-session job, so a result-less run deliberately leaves it in
        place. ``_run_job_isolated`` scopes its own clear to
        ``job.command or job.script`` for exactly that reason, and the message
        fire-time deny path never clears at all -- but the shared gate helper
        cleared for ALL kinds, so a message cron starved at the gate lost the
        context and the next run repeated content it had already sent.

        Parametrised over all three kinds because asserting retention ALONE
        would also pass if the clear were deleted outright: the command and
        script cases are what pin the clear still firing where it belongs.
        """
        import kiro_crew.executors as ex
        from kiro_crew.slack.gateway import _await_cron_fire_time_gate

        job = CronJob(
            id="j1",
            name="n",
            message="hello",
            schedule=CronSchedule(kind="every", every_secs=300),
            **extra,
        )
        job.last_result = "previously sent content"
        assert job.result_produced is False, "this run produced nothing; the clear must be live"

        async def _starved_gate(*_a, **_kw):
            raise ex.CronQueueTimeout(300.0)

        with patch("kiro_crew.slack.gateway.run_in_cron_gate_pool", _starved_gate):
            reason, starved = await _await_cron_fire_time_gate(job, tool_name="t", tool_kind="k")

        assert starved is True and reason is None, "starvation did not report as starved"
        assert job.run_never_started is True, "a starved run was not marked never-started"
        if retained:
            assert job.last_result == "previously sent content", (
                f"a starved {kind} job lost its cross-run dedup context, so the next run "
                "repeats content it already sent"
            )
        else:
            assert job.last_result == "", (
                f"a starved {kind} job kept a previous run's output, which would display "
                "beside this run's status"
            )


class TestClaimBackstopRetainsAnUnstartedOneShot:
    """The claim backstop must not consume a one-shot whose payload never ran.

    ``_claim_backstop`` bounds the whole submitted call -- the claim-time vet AND
    the payload. When it expires the caller lands in ``except
    asyncio.TimeoutError``, and that arm fires for two materially different runs:

    * the vet burned the bound before ``handoff.claim()`` was ever granted, so
      NOTHING executed; and
    * ``claim()`` was granted, the payload started, and the subprocess overran.

    Only the first is a never-started run. ``abandon()`` is the sole thing that
    separates them -- it reports whether the payload had started -- and
    the cron service's delete site reads ``run_never_started`` to decide whether a
    ``delete_after_run`` job is retained or consumed.

    Both directions are asserted, plus the deny path, because each of the three
    is a silent failure pointing a different way: retain a run that DID dispatch
    and the one-shot fires twice or never leaves the queue; set this marker on a
    deny and an at-job is parked disabled for a policy decision never made,
    whose retention belongs to ``fire_time_denied``.
    """

    @staticmethod
    def _pool(*, grant_claim: bool):
        """Stand in for run_in_cron_pool, deciding whether claim() is reached.

        grant_claim=False models the backstop expiring DURING the vet: the
        submitted wrapper is never invoked, so ``_started`` stays False.
        grant_claim=True models a payload that started and then overran: the
        wrapper runs (reaching ``handoff.claim()``) before the bound expires.
        """

        async def _fake(func, /, *args, timeout, **kw):
            if grant_claim:
                func(*args)
            raise asyncio.TimeoutError

        return _fake

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expect_retained", [(False, True), (True, False)])
    async def test_backstop_retains_only_the_run_that_never_started(
        self, tmp_path, grant_claim, expect_retained
    ):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot",
            message="",
            every_secs=300,
            command="echo hi",
            delete_after_run=True,
        )
        assert any(j.id == created.id for j in svc.list_jobs())

        gw = _make_gw()
        with patch("kiro_crew.slack.gateway.run_in_cron_pool", self._pool(grant_claim=grant_claim)):
            await _run_command_callback(gw, created, {"status": "ok", "output": "x"})

        assert created.run_never_started is expect_retained, (
            "the claim backstop expired before claim() yet the run was not marked "
            "never-started, so _merge_job_result will delete a one-shot that "
            "dispatched nothing"
            if expect_retained
            else "a payload that DID start was marked never-started, so a healthy "
            "one-shot is retained and will fire again or never leave the queue"
        )

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, created)
        still_present = any(j.id == created.id for j in svc.list_jobs(include_disabled=True))
        # Retained either way, by a DIFFERENT owner: the unstarted run by
        # run_never_started, the overrun because a failed one-shot is kept.
        assert created.last_status == "error"
        assert still_present, (
            "the one-shot was deleted by a run that never dispatched"
            if expect_retained
            else "a one-shot whose run timed out was deleted instead of kept"
        )

    @pytest.mark.asyncio
    async def test_a_deny_leaves_retention_to_fire_time_denied(self, tmp_path):
        """A deny must NOT borrow this marker -- its readers park an at-job disabled."""
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot-denied",
            message="",
            every_secs=300,
            command="echo hi",
            delete_after_run=True,
        )

        async def _denying_pool(func, /, *args, timeout, **kw):
            return func(*args)  # the wrapper raises CronClaimTimeDenied itself

        gw = _make_gw()
        with patch("kiro_crew.slack.gateway.run_in_cron_pool", _denying_pool):
            await _run_command_callback(
                gw, created, {"status": "ok", "output": "x"}, vet_reason="denied by policy"
            )

        assert created.fire_time_denied is True, "a policy deny did not record fire_time_denied"
        assert created.run_never_started is False, (
            "a policy deny set run_never_started, conflating a governance refusal with "
            "a never-started run -- that parks an at-job disabled for a decision never made"
        )
        # Retained either way, but by the RIGHT owner.
        await asyncio.to_thread(svc._merge_job_result, created)
        assert any(j.id == created.id for j in svc.list_jobs()), "a denied one-shot was consumed"


class TestClaimBackstopDoesNotConsumeTheFailureBudget:
    """A claim-backstop timeout must only count when the payload dispatched.

    ``_claim_backstop`` bounds the vet AND the payload, so its
    ``asyncio.TimeoutError`` arm fires for two different runs: a vet that burned
    the bound before ``handoff.claim()`` (nothing executed), and a payload that
    started and then overran. Counting the first auto-pauses at
    ``_AUTO_PAUSE_THRESHOLD`` -- and a paused job never fires again -- so
    repeated wedged wakes would permanently disable a healthy job that has not
    run a line, exactly the harm the starvation, gate-deny and vet-overrun arms
    already decline to cause.

    NOTE ON WHY THIS LIVES HERE. ``test_cron_timeout_autopause_424.py`` pins the
    same invariant one layer down, but it monkeypatches ``asyncio.wait_for`` and
    drives ``CronService._execute_with_timeout`` directly -- it never enters
    ``gateway.py``, so it is structurally blind to these two arms and would pass
    whether or not they are fixed. This test drives the gateway callback so the
    arm itself is what is measured.

    Parametrised over BOTH arms and BOTH directions: asserting only the
    exemption would also pass if the count were deleted outright, so the
    started=True cases are what pin the counter still firing for a real overrun.
    """

    @staticmethod
    def _pool(*, grant_claim: bool):
        """Stand in for run_in_cron_pool, choosing whether claim() is reached.

        grant_claim=False models the backstop expiring DURING the vet: the
        submitted wrapper is never invoked, so ``_started`` stays False and
        ``abandon()`` reports the payload as never started. grant_claim=True
        invokes the wrapper first (reaching ``handoff.claim()``) and only then
        expires, modelling a payload that ran and overran.
        """

        async def _fake(func, /, *args, timeout, **kw):
            if grant_claim:
                func(*args)
            raise asyncio.TimeoutError

        return _fake

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expected_count", [(False, 3), (True, 4)])
    async def test_command_backstop_counts_only_a_dispatched_run(self, grant_claim, expected_count):
        gw = _make_gw()
        job = _make_command_job(consecutive_failures=3)

        with patch("kiro_crew.slack.gateway.run_in_cron_pool", self._pool(grant_claim=grant_claim)):
            await _run_command_callback(gw, job, {"status": "ok", "output": "x"})

        assert job.consecutive_failures == expected_count, (
            "a claim-backstop timeout whose payload never started incremented the "
            "failure budget, so repeated wedged wakes will auto-pause a job that "
            "has not run a line"
            if not grant_claim
            else "a genuine execution overrun stopped counting, so a job that times "
            "out on every run would never auto-pause"
        )
        assert job.auto_paused is False, "the job was auto-paused by this single run"
        # The run is still reported as an error either way -- only the counter differs.
        assert job.last_status == "error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expected_count", [(False, 3), (True, 4)])
    async def test_script_backstop_counts_only_a_dispatched_run(self, grant_claim, expected_count):
        gw = _make_gw()
        job = _make_script_job(consecutive_failures=3)

        with patch("kiro_crew.slack.gateway.run_in_cron_pool", self._pool(grant_claim=grant_claim)):
            await _run_script_callback(gw, job, {"status": "ok"})

        assert job.consecutive_failures == expected_count, (
            "the SCRIPT claim-backstop arm counted a run whose payload never "
            "started -- the command arm was fixed and this one was not"
            if not grant_claim
            else "the script arm stopped counting a genuine execution overrun"
        )
        assert job.auto_paused is False, "the job was auto-paused by this single run"
        assert job.last_status == "error"


class TestACancellationAtTheClaimAwaitKeepsItsOneShot:
    """A cancellation landing on the claim await must not consume a one-shot.

    ``asyncio.CancelledError`` is a ``BaseException``, so none of the command or
    script block's ``except`` arms caught it -- not the claim backstop's
    ``asyncio.TimeoutError``, and not the trailing ``except Exception``. The
    shared ``finally`` did call ``handoff.abandon()``, but DISCARDED its return,
    which is the one bit separating "the payload never started" from "the payload
    ran". So a shutdown or wake deadline landing mid-vet reached the cron service's
    delete site with neither ``run_never_started`` nor ``fire_time_denied`` set,
    and a ``delete_after_run`` job was consumed having never executed.

    The same class of bug was already cured one path over for the message-job
    fire-time gate (see
    ``test_a_gate_cancelled_by_the_wake_deadline_keeps_its_one_shot``, whose
    docstring names this exact mechanism); the command and script claim awaits
    were the ones still exposed.

    NOTE ON PLACEMENT, which is what the tests below actually pin. The fix is an
    ``except asyncio.CancelledError`` arm, NOT an assignment in the shared
    ``finally``: the fire-time deny path reaches that ``finally`` with the
    payload equally unstarted, and setting this marker there would park an at-job
    disabled for a policy decision that was never made -- retention on that path
    is owned by ``fire_time_denied``. ``test_deny`` below is what keeps that
    closed, and ``grant_claim=True`` is what keeps the mirror closed: a payload
    that DID run must stay deletable, or the one-shot fires twice.
    """

    @staticmethod
    def _cancelling_pool(*, grant_claim: bool):
        """Stand in for run_in_cron_pool, cancelling instead of timing out.

        grant_claim=False models the deadline cancelling the await DURING the
        claim-time vet: the submitted wrapper is never invoked, so ``_started``
        stays False and ``abandon()`` reports the payload as never started.
        grant_claim=True invokes the wrapper first, reaching ``handoff.claim()``,
        so the cancellation lands on a run that did dispatch.
        """

        async def _fake(func, /, *args, timeout, **kw):
            if grant_claim:
                func(*args)
            raise asyncio.CancelledError

        return _fake

    @staticmethod
    def _one_shot(tmp_path, **kw):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot", message="", every_secs=300, delete_after_run=True, **kw
        )
        assert any(j.id == created.id for j in svc.list_jobs()), "fixture did not store the job"
        return svc, created

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expect_retained", [(False, True), (True, False)])
    async def test_command_cancellation_retains_only_the_run_that_never_started(
        self, tmp_path, grant_claim, expect_retained
    ):
        svc, created = self._one_shot(tmp_path, command="echo hi")
        gw = _make_gw()

        # Re-raised, so cooperative cancellation is preserved: swallowing it here
        # would leave the caller believing the run completed.
        with patch(
            "kiro_crew.slack.gateway.run_in_cron_pool",
            self._cancelling_pool(grant_claim=grant_claim),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _run_command_callback(gw, created, {"status": "ok", "output": "x"})

        assert created.run_never_started is expect_retained, (
            "a cancellation landed on the claim await before claim() yet the run "
            "was not marked never-started, so _merge_job_result will delete a "
            "one-shot that dispatched nothing"
            if expect_retained
            else "a payload that DID start was marked never-started, so a healthy "
            "one-shot is retained and will fire again or never leave the queue"
        )
        assert created.fire_time_denied is False, "a cancellation recorded a denial it never made"

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, created)
        survived = any(j.id == created.id for j in svc.list_jobs())
        assert survived is expect_retained, (
            "the one-shot was deleted by a run cancelled before it dispatched"
            if expect_retained
            else "a one-shot that DID execute was retained, so it fires a second time"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expect_retained", [(False, True), (True, False)])
    async def test_script_cancellation_retains_only_the_run_that_never_started(
        self, tmp_path, grant_claim, expect_retained
    ):
        svc, created = self._one_shot(tmp_path, script="~/.kirocrew/crons/monitor.py:run")
        gw = _make_gw()

        with patch(
            "kiro_crew.slack.gateway.run_in_cron_pool",
            self._cancelling_pool(grant_claim=grant_claim),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _run_script_callback(gw, created, {"status": "ok"})

        assert created.run_never_started is expect_retained, (
            "the SCRIPT claim await was cancelled before claim() yet the run was "
            "not marked never-started -- the command arm was fixed and this one "
            "was not"
            if expect_retained
            else "the script arm marked a payload that DID start as never-started"
        )

        await asyncio.to_thread(svc._merge_job_result, created)
        survived = any(j.id == created.id for j in svc.list_jobs())
        assert survived is expect_retained, (
            "the script one-shot was deleted by a run cancelled before it dispatched"
            if expect_retained
            else "a script one-shot that DID execute was retained"
        )

    @pytest.mark.asyncio
    async def test_a_deny_still_leaves_retention_to_fire_time_denied(self, tmp_path):
        """The placement guard: the deny path must not acquire this marker.

        A deny reaches the same shared ``finally`` with the payload unstarted, so
        an assignment placed THERE would set ``run_never_started`` here too. That
        is why the fix lives in the cancellation arm instead, and this is the
        test that would go red if it were ever moved.
        """
        svc, created = self._one_shot(tmp_path, command="echo hi")

        async def _denying_pool(func, /, *args, timeout, **kw):
            return func(*args)  # the wrapper raises CronClaimTimeDenied itself

        gw = _make_gw()
        with patch("kiro_crew.slack.gateway.run_in_cron_pool", _denying_pool):
            await _run_command_callback(
                gw, created, {"status": "ok", "output": "x"}, vet_reason="denied by policy"
            )

        assert created.fire_time_denied is True, "a policy deny did not record fire_time_denied"
        assert created.run_never_started is False, (
            "a policy deny set run_never_started, conflating a governance refusal "
            "with a never-started run -- that parks an at-job disabled for a "
            "decision never made"
        )

        await asyncio.to_thread(svc._merge_job_result, created)
        assert any(j.id == created.id for j in svc.list_jobs()), "a denied one-shot was consumed"
