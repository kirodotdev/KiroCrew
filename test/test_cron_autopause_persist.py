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

import json
from pathlib import Path
from unittest.mock import patch

from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronSchedule, CronService


class TestRecordFailureSuccess:
    def test_record_failure_auto_pauses_at_threshold(self) -> None:
        job = CronJob(id="j", name="n", message="m", schedule=CronSchedule(kind="every", every_secs=60))
        for _ in range(_AUTO_PAUSE_THRESHOLD - 1):
            job.record_failure()
            assert job.enabled is True
            assert job.auto_paused is False
        job.record_failure()  # threshold hit
        assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD
        assert job.enabled is False
        assert job.auto_paused is True

    def test_record_success_lifts_auto_pause_but_not_user_pause(self) -> None:
        job = CronJob(id="j", name="n", message="m", schedule=CronSchedule(kind="every", every_secs=60))
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
        job = CronJob(id="j", name="n", message="m", schedule=CronSchedule(kind="every", every_secs=60), script="x.py:run")
        with patch("kiro_crew.sel.sel") as mock_sel:
            for _ in range(_AUTO_PAUSE_THRESHOLD):
                job.record_failure()
            # Extra failures past the threshold must NOT re-audit (already paused).
            job.record_failure()
            pause_calls = [
                c for c in mock_sel.return_value.log_tool_invocation.call_args_list
                if c.kwargs.get("outcome") == "auto_paused"
            ]
            assert len(pause_calls) == 1
            assert pause_calls[0].kwargs["tool_kind"] == "cron_auto_pause"

            job.record_success()
            clear_calls = [
                c for c in mock_sel.return_value.log_tool_invocation.call_args_list
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
        assert all(j.id != job_id for j in svc2.list_jobs()), "auto-paused job must not be scheduled"

    def test_auto_pause_persisted_to_disk(self, tmp_path: Path) -> None:
        self._paused_job_service(tmp_path)
        raw = json.loads((tmp_path / "crons.json").read_text())
        stored = raw["jobs"][0]
        assert stored["auto_paused"] is True
        assert stored["enabled"] is False

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
