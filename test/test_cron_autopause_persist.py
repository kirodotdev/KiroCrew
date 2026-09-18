"""Auto-pause must survive a daemon restart (round-2 bugfix).

A script/command cron auto-pauses after N consecutive failures. Before the fix
this only set `job.enabled = False` in memory: `_merge_job_result` refused to
persist `enabled` for recurring jobs (user_paused was the sole authority), and
`_load()` derived `enabled = not user_paused`. So an auto-paused recurring job
came back **enabled** on the next reload and kept firing its failing run forever.

The fix adds an execution-owned `auto_paused` flag, persists it for every job,
and folds it into the effective-enabled derivation on load. These tests pin the
whole round-trip.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronSchedule, CronService


class TestRecordFailureSuccess:
    def test_record_failure_auto_pauses_at_threshold(self) -> None:
        job = CronJob(
            id="j", name="n", message="m", schedule=CronSchedule(kind="every", every_secs=60)
        )
        for _ in range(_AUTO_PAUSE_THRESHOLD - 1):
            job.record_failure()
            assert job.enabled is True
            assert job.auto_paused is False
        job.record_failure()  # threshold hit
        assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD
        assert job.enabled is False
        assert job.auto_paused is True

    def test_record_success_lifts_auto_pause_but_not_user_pause(self) -> None:
        job = CronJob(
            id="j", name="n", message="m", schedule=CronSchedule(kind="every", every_secs=60)
        )
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is True
        # A job the user ALSO paused: a recovery must not silently re-enable it.
        job.user_paused = True
        job.record_success()
        assert job.consecutive_failures == 0
        assert job.auto_paused is False
        # record_success clears the auto-pause reason but intentionally does not
        # force enabled back to True — a user pause must survive a success.
        assert job.user_paused is True
        assert job.enabled is False

    def test_auto_pause_transition_emits_sel_audit_event(self) -> None:
        # The pause/unpause is a permission decision (revokes/restores execute
        # ability), so it must emit a SEL audit event exactly once per transition.
        job = CronJob(
            id="j",
            name="n",
            message="m",
            schedule=CronSchedule(kind="every", every_secs=60),
            script="x.py:run",
        )
        with patch("kiro_crew.sel.sel") as mock_sel:
            for _ in range(_AUTO_PAUSE_THRESHOLD):
                job.record_failure()
            # Extra failures past the threshold must NOT re-audit (already paused).
            job.record_failure()
            pause_calls = [
                c
                for c in mock_sel.return_value.log_tool_invocation.call_args_list
                if c.kwargs.get("outcome") == "auto_paused"
            ]
            assert len(pause_calls) == 1
            assert pause_calls[0].kwargs["tool_kind"] == "cron_auto_pause"

            job.record_success()
            clear_calls = [
                c
                for c in mock_sel.return_value.log_tool_invocation.call_args_list
                if c.kwargs.get("outcome") == "auto_pause_cleared"
            ]
            assert len(clear_calls) == 1


class TestAutoPausePersistence:
    def _paused_job_service(self, tmp_path: Path) -> tuple[CronService, str]:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="flaky", message="boom", every_secs=60)
        # Simulate the gateway's auto-pause after repeated failures.
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.enabled is False and job.auto_paused is True
        svc._merge_job_result(job)  # what the run loop calls after each execution
        return svc, job.id

    def test_auto_pause_survives_reload(self, tmp_path: Path) -> None:
        _, job_id = self._paused_job_service(tmp_path)

        # Fresh service = daemon restart.
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        # The bug: the job reappears enabled. list_jobs() (enabled-only) hid it.
        assert svc2.get_job(job_id).enabled is False
        assert svc2.get_job(job_id).auto_paused is True
        assert all(
            j.id != job_id for j in svc2.list_jobs()
        ), "auto-paused job must not be scheduled"

    def test_auto_pause_persisted_to_disk(self, tmp_path: Path) -> None:
        self._paused_job_service(tmp_path)
        raw = json.loads((tmp_path / "crons.json").read_text(encoding="utf-8"))
        stored = raw["jobs"][0]
        assert stored["auto_paused"] is True
        assert stored["enabled"] is False

    def test_retry_telemetry_survives_reload(self, tmp_path: Path) -> None:
        """the field this store schema exists to add.

        `_merge_job_result` must copy `last_retry_count` from
        the in-memory job or nothing a run set on them ever reaches disk --
        the same silent-drop shape every OTHER per-run field on this call
        guards against.
        """
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="flaky", message="boom", every_secs=60)
        job.last_retry_count = 2
        svc._merge_job_result(job)

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        reloaded = svc2.get_job(job.id)
        assert reloaded.last_retry_count == 2

    def test_the_retry_count_carries_the_run_stamp_it_describes(self, tmp_path: Path) -> None:
        """The count alone cannot say WHICH run it describes.

        A cancelled run advances `last_run_ts` (the `every` scheduler needs it to
        or the schedule drifts) while deliberately not overwriting the count, so
        without a stamp binding the two the Schedule page reads a completed run's
        retries as the cancelled run's. Displays compare the pair.
        """
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="flaky", message="boom", every_secs=60)
        job.last_retry_count = 3
        job.last_retry_run_ts = 1770000000.0
        svc._merge_job_result(job)

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        reloaded = svc2.get_job(job.id)
        assert reloaded.last_retry_count == 3
        assert reloaded.last_retry_run_ts == 1770000000.0

    def test_a_legacy_record_without_the_retry_stamp_loads_at_zero(self, tmp_path: Path) -> None:
        """A record written before the stamp existed must load unchanged, and its
        zero stamp cannot match any real `last_run_ts` — so the count is withheld
        rather than misattributed."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="old", message="hi", every_secs=60)
        job.last_retry_count = 2
        svc._merge_job_result(job)
        store_path = tmp_path / "crons.json"
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        del raw["jobs"][0]["last_retry_run_ts"]
        store_path.write_text(json.dumps(raw), encoding="utf-8")

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2.get_job(job.id).last_retry_run_ts == 0.0

    def test_a_cancelled_run_does_not_overwrite_the_last_completed_count(
        self, tmp_path: Path
    ) -> None:
        """A run cancelled mid-callback owes no retry telemetry.

        ``_execute`` stamps both retry fields only after the callback returns.
        A cancellation lands inside the callback, so the stamp is never reached
        and the merge copies the values the last COMPLETED run left -- the only
        number the Schedule page can honestly show. The spent attempt counter
        is still cleared, or the next run would start with a smaller budget.
        """
        job_id = self._store_a_completed_run_with_three_retries(tmp_path)
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.get_job(job_id)

        async def cancelled_mid_retry(j: CronJob) -> str | None:
            j._transient_attempts = 1  # type: ignore[attr-defined]
            raise asyncio.CancelledError

        svc._on_job = cancelled_mid_retry
        try:
            asyncio.run(svc._execute(job))
        except asyncio.CancelledError:
            pass
        svc._merge_job_result(job)

        assert getattr(job, "_transient_attempts", 0) == 0
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2.get_job(job_id).last_retry_count == 3

    def test_a_completed_run_still_records_zero_retries(self, tmp_path: Path) -> None:
        """A run that finishes without retrying legitimately reports 0, and that
        has to overwrite a previous run's count -- otherwise a single flaky run
        would pin a stale number on the job forever."""
        job_id = self._store_a_completed_run_with_three_retries(tmp_path)
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.get_job(job_id)

        async def clean(j: CronJob) -> str | None:
            return "ok"

        svc._on_job = clean
        asyncio.run(svc._execute(job))
        svc._merge_job_result(job)

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2.get_job(job_id).last_retry_count == 0

    def test_a_retried_run_lands_on_disk_bound_to_its_own_last_run_ts(self, tmp_path: Path) -> None:
        """End to end through ``_run_job_isolated``: the count a retried run took
        reaches disk stamped with THAT run's ``last_run_ts``.

        The Schedule page renders the note only when ``last_retry_run_ts ==
        last_run_ts``. Stamping the pair from inside the callback bound the count
        to the PREVIOUS run's ``last_run_ts`` (``_execute`` writes this run's only
        after the callback returns) so the two never matched and the note never
        rendered. Pinned here on the reloaded record, not the in-memory job.
        """
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="flaky", message="boom", every_secs=60)
        job.last_run_ts = 1_700_000_000.0  # a previous run's stamp

        async def retried_twice(j: CronJob) -> str | None:
            # What the gateway callback leaves behind after two transient retries.
            j._transient_attempts = 2  # type: ignore[attr-defined]
            return "ok"

        svc._on_job = retried_twice
        svc._jobs = [job]
        asyncio.run(svc._run_job_isolated(job))  # every=60s: sub-hourly, no jitter sleep

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        reloaded = svc2.get_job(job.id)
        assert reloaded.last_retry_count == 2
        assert reloaded.last_retry_run_ts == reloaded.last_run_ts
        assert reloaded.last_run_ts > 1_700_000_000.0  # this run's, not the previous one's

    @staticmethod
    def _store_a_completed_run_with_three_retries(tmp_path: Path) -> str:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="flaky", message="boom", every_secs=60)
        job.last_retry_count = 3
        svc._merge_job_result(job)
        assert svc.get_job(job.id).last_retry_count == 3
        return job.id

    def test_legacy_record_without_retry_keys_loads_at_the_default(self, tmp_path: Path) -> None:
        """A record that lacks this field must load unchanged."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="old", message="hi", every_secs=60)
        svc._merge_job_result(job)

        store_path = tmp_path / "crons.json"
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        del raw["jobs"][0]["last_retry_count"]
        store_path.write_text(json.dumps(raw), encoding="utf-8")

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        reloaded = svc2.get_job(job.id)
        assert reloaded.last_retry_count == 0

    def test_reenable_clears_auto_pause_and_persists(self, tmp_path: Path) -> None:
        svc, job_id = self._paused_job_service(tmp_path)
        assert svc.enable_job(job_id, enabled=True) is True

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        job = svc2.get_job(job_id)
        assert job.enabled is True
        assert job.auto_paused is False
        assert any(j.id == job_id for j in svc2.list_jobs())

    def test_reenable_resets_failure_counter(self, tmp_path: Path) -> None:
        # A user who re-enables an auto-paused job expects a fresh set of attempts.
        # If the counter stayed at the threshold, the next single failure would
        # immediately re-auto-pause the job.
        svc, job_id = self._paused_job_service(tmp_path)
        assert svc.get_job(job_id).consecutive_failures == _AUTO_PAUSE_THRESHOLD
        svc.enable_job(job_id, enabled=True)
        job = svc.get_job(job_id)
        assert job.consecutive_failures == 0
        # One post-resume failure must NOT re-pause (counter starts fresh at 0).
        job.record_failure()
        assert job.enabled is True
        assert job.auto_paused is False

    def test_success_after_pause_persists_recovery(self, tmp_path: Path) -> None:
        svc, job_id = self._paused_job_service(tmp_path)
        # A later successful run recovers the job and merges the recovery.
        job = svc.get_job(job_id)
        job.enabled = True  # the scheduler wouldn't fire it, but a manual run can
        job.record_success()
        svc._merge_job_result(job)

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        reloaded = svc2.get_job(job_id)
        assert reloaded.auto_paused is False
        assert reloaded.enabled is True

    def test_user_pause_still_survives_reload(self, tmp_path: Path) -> None:
        # Regression guard: the pre-existing user-pause persistence must be intact.
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="userpaused", message="x", every_secs=60)
        svc.enable_job(job.id, enabled=False)

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        reloaded = svc2.get_job(job.id)
        assert reloaded.enabled is False
        assert reloaded.user_paused is True
        assert reloaded.auto_paused is False


class TestExecuteSuccessResetsCounter:
    """CronService._execute must reset the auto-pause budget on success.

    Before the fix, record_failure() fired on the error/timeout paths but
    record_success() was only ever called from the gateway callback's own
    branches — and callback shapes that return without touching bookkeeping
    (e.g. a script cron's Skip outcome) left the counter monotonic. A healthy
    cron accumulating _AUTO_PAUSE_THRESHOLD transient failures over its
    lifetime, successes in between notwithstanding, silently auto-paused.
    """

    def _job(self) -> CronJob:
        return CronJob(
            id="j", name="n", message="m", schedule=CronSchedule(kind="every", every_secs=60)
        )

    def test_successful_run_resets_consecutive_failures(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = self._job()
        job.consecutive_failures = _AUTO_PAUSE_THRESHOLD - 1

        async def ok_callback(j: CronJob) -> None:
            return None  # returns normally, no bookkeeping of its own (Skip-like)

        svc._on_job = ok_callback
        asyncio.run(svc._execute(job))
        assert job.last_status == "ok"
        assert job.consecutive_failures == 0, (
            "a successful run must refill the auto-pause budget; a monotonic "
            "counter silently pauses healthy jobs after 5 lifetime failures"
        )

    def test_fail_then_succeed_never_auto_pauses(self, tmp_path: Path) -> None:
        # Alternating failure/success far past the threshold in TOTAL failures:
        # with intervening successes the job must never auto-pause.
        svc = CronService(base_dir=tmp_path)
        job = self._job()

        async def failing(j: CronJob) -> None:
            # Faithful to the production contract: the gateway callback counts
            # the failure itself, then re-raises for _execute to observe.
            j.record_failure()
            raise RuntimeError("transient")

        async def succeeding(j: CronJob) -> None:
            return None

        for _ in range(_AUTO_PAUSE_THRESHOLD * 2):
            svc._on_job = failing
            asyncio.run(svc._execute(job))
            svc._on_job = succeeding
            asyncio.run(svc._execute(job))
        assert job.auto_paused is False
        assert job.enabled is True
        assert job.consecutive_failures == 0

    def test_callback_reported_error_does_not_reset(self, tmp_path: Path) -> None:
        # A callback that signals failure by mutating the job (command/script
        # contract) — and the deliberately-neutral governance-denial paths,
        # which use the same last_status="error" shape — must NOT have the
        # counter reset by _execute.
        svc = CronService(base_dir=tmp_path)
        job = self._job()
        job.consecutive_failures = 3

        async def mutating_error(j: CronJob) -> None:
            j.last_status = "error"
            j.last_error = "policy denial or command failure"

        svc._on_job = mutating_error
        asyncio.run(svc._execute(job))
        assert job.last_status == "error"
        assert job.consecutive_failures == 3

    def test_raising_callback_does_not_reset(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = self._job()
        job.consecutive_failures = 4

        async def raising(j: CronJob) -> None:
            raise RuntimeError("boom")

        svc._on_job = raising
        asyncio.run(svc._execute(job))
        assert job.last_status == "error"
        assert job.consecutive_failures == 4

    def test_success_lifts_auto_pause_via_execute(self, tmp_path: Path) -> None:
        # Manual "Run Now" on an auto-paused job that then succeeds must lift
        # the pause (record_success semantics reached from the _execute path).
        svc = CronService(base_dir=tmp_path)
        job = self._job()
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is True

        async def succeeding(j: CronJob) -> None:
            return None

        svc._on_job = succeeding
        asyncio.run(svc._execute(job))
        assert job.auto_paused is False
        assert job.enabled is True
        assert job.consecutive_failures == 0

    def test_cancelled_run_does_not_reset_counter(self, tmp_path: Path) -> None:
        # Cancel race: cancel() kills the sandboxed subprocess BEFORE
        # task.cancel(), and the gateway's cancelled branch returns None
        # without setting last_status — so the callback can return normally
        # while the cancel marker is set. cancel() documents that it leaves
        # consecutive_failures untouched; the ok-branch reset must not fire.
        svc = CronService(base_dir=tmp_path)
        job = self._job()
        job.consecutive_failures = 3

        async def cancelled_shape(j: CronJob) -> None:
            return None  # gateway cancelled branch: no bookkeeping, no last_status

        svc._on_job = cancelled_shape
        svc._cancelled_jobs.add(job.id)
        try:
            asyncio.run(svc._execute(job))
        finally:
            svc._cancelled_jobs.discard(job.id)
        assert job.consecutive_failures == 3
