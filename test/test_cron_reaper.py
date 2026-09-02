"""Tests for the cron reaper that force-kills zombie cron jobs."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import (
    _JOB_TIMEOUT_SECS,
    CronJob,
    CronSchedule,
    CronService,
)
from kiro_crew.cron_history import CronHistoryStore


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    sessions._sessions = {}
    return sessions


def _make_job(job_id: str = "job1", name: str = "test job") -> CronJob:
    return CronJob(
        id=job_id,
        name=name,
        message="do something",
        schedule=CronSchedule(kind="every", every_secs=300),
        created_ts=time.time(),
    )


class TestCronReaper:
    """Tests for the periodic reaper that force-kills zombie cron jobs."""

    @pytest.mark.asyncio
    async def test_reaper_kills_expired_job(self, tmp_path: object) -> None:
        """Reaper marks expired job as error and emits SEL event."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        sessions = _mock_sessions()
        svc._sessions = sessions

        job = _make_job("expired1")
        svc._jobs = [job]
        svc._job_start_times["expired1"] = time.time() - _JOB_TIMEOUT_SECS - 120
        svc._running_tasks["expired1"] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.sel.sel") as mock_sel, patch.object(svc, "_save"):
            await svc._force_reap("expired1", _JOB_TIMEOUT_SECS + 120)

        assert job.last_status == "error"
        assert "Reaped" in (job.last_error or "")
        assert "expired1" in svc._reaped_jobs
        assert "expired1" not in svc._job_start_times  # popped early
        sessions.reset.assert_awaited_once_with("cron:expired1")
        mock_sel().log_tool_invocation.assert_called_once_with(
            session_key="cron:expired1",
            source="cron",
            tool_name="reaper_force_kill",
            outcome="reaped",
            metadata={
                "job_id": "expired1",
                "session_key": "cron:expired1",
                "elapsed": _JOB_TIMEOUT_SECS + 120,
            },
        )

    @pytest.mark.asyncio
    async def test_reaper_skips_jobs_within_deadline(self) -> None:
        """Reaper does not touch jobs still within the timeout."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        sessions = _mock_sessions()
        svc._sessions = sessions

        svc._job_start_times["ok1"] = time.time() - 60  # only 60s old

        with patch("kiro_crew.sel.sel"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        # Should not have been reaped
        assert "ok1" not in svc._reaped_jobs
        sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaper_skips_done_tasks(self) -> None:
        """Reaper skips jobs whose asyncio task already completed (race guard)."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        svc._job_start_times["done1"] = time.time() - _JOB_TIMEOUT_SECS - 60
        done_task = MagicMock()
        done_task.done.return_value = True
        svc._running_tasks["done1"] = done_task

        with patch("kiro_crew.sel.sel"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert "done1" not in svc._reaped_jobs
        assert "done1" not in svc._job_start_times  # cleaned up

    @pytest.mark.asyncio
    async def test_reaper_handles_reset_timeout(self, tmp_path: object) -> None:
        """Reaper falls back to SIGKILL when reset() hangs."""
        sessions = _mock_sessions()

        async def hanging_reset(key: str) -> None:
            await asyncio.sleep(999)

        sessions.reset = hanging_reset

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = sessions

        job = _make_job("hang1")
        svc._jobs = [job]
        svc._running_tasks["hang1"] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.sel.sel"), patch(
            "kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.05
        ), patch.object(
            svc, "_sigkill_session", new_callable=AsyncMock
        ) as mock_kill, patch.object(svc, "_save"):
            await svc._force_reap("hang1", _JOB_TIMEOUT_SECS + 60)

        assert job.last_status == "error"
        mock_kill.assert_awaited_once_with("cron:hang1")

    @pytest.mark.asyncio
    async def test_reaper_sigkill_on_reset_exception(self, tmp_path: object) -> None:
        """Reaper falls back to SIGKILL when reset() raises a non-timeout exception."""
        sessions = _mock_sessions()
        sessions.reset = AsyncMock(side_effect=RuntimeError("broken"))

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = sessions

        job = _make_job("exc1")
        svc._jobs = [job]
        svc._running_tasks["exc1"] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.sel.sel"), patch.object(
            svc, "_sigkill_session", new_callable=AsyncMock
        ) as mock_kill, patch.object(svc, "_save"):
            await svc._force_reap("exc1", _JOB_TIMEOUT_SECS + 10)

        assert job.last_status == "error"
        mock_kill.assert_awaited_once_with("cron:exc1")

    @pytest.mark.asyncio
    async def test_reaper_cancels_asyncio_task(self, tmp_path: object) -> None:
        """Reaper cancels the running asyncio task for the job."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("cancel1")
        svc._jobs = [job]
        mock_task = MagicMock()
        mock_task.done.return_value = False
        svc._running_tasks["cancel1"] = mock_task

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("cancel1", _JOB_TIMEOUT_SECS + 10)

        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_reaper_persists_state(self, tmp_path: object) -> None:
        """Reaper calls _save() after updating job state."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("persist1")
        svc._jobs = [job]
        svc._running_tasks["persist1"] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save") as mock_save:
            await svc._force_reap("persist1", _JOB_TIMEOUT_SECS + 10)

        mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_reaped_flag_prevents_merge(self, tmp_path: object) -> None:
        """When reaper kills a job, _run_job_isolated skips _merge_job_result."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("reaped1")
        svc._jobs = [job]
        svc._reaped_jobs.add("reaped1")
        svc._executing.add("reaped1")

        with patch.object(svc, "_execute_with_timeout", new_callable=AsyncMock), patch.object(
            svc, "_merge_job_result"
        ) as mock_merge:
            await svc._run_job_isolated(job)

        mock_merge.assert_not_called()
        assert "reaped1" not in svc._reaped_jobs  # cleaned up

    @pytest.mark.asyncio
    async def test_reaped_flag_prevents_merge_on_cancel(self) -> None:
        """Reaped job skips merge even when CancelledError propagates."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("reaped2")
        svc._jobs = [job]
        svc._reaped_jobs.add("reaped2")
        svc._executing.add("reaped2")

        with patch.object(
            svc, "_execute_with_timeout", side_effect=asyncio.CancelledError
        ), patch.object(svc, "_merge_job_result") as mock_merge:
            with pytest.raises(asyncio.CancelledError):
                await svc._run_job_isolated(job)

        mock_merge.assert_not_called()
        assert "reaped2" not in svc._reaped_jobs

    @pytest.mark.asyncio
    async def test_non_reaped_job_merges_normally(self) -> None:
        """Normal (non-reaped) job still merges results."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("normal1")
        svc._executing.add("normal1")

        with patch.object(svc, "_execute_with_timeout", new_callable=AsyncMock), patch.object(
            svc, "_merge_job_result"
        ) as mock_merge:
            await svc._run_job_isolated(job)

        mock_merge.assert_called_once_with(job)

    @pytest.mark.asyncio
    async def test_start_reaper_creates_task(self) -> None:
        """start_reaper creates a background asyncio task."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        sessions = _mock_sessions()

        svc.start_reaper(sessions)
        assert svc._reaper_task is not None
        assert svc._sessions is sessions

        # Cleanup
        svc._reaper_task.cancel()
        try:
            await svc._reaper_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_stop_cancels_reaper(self) -> None:
        """stop() cancels the reaper task."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc.start_reaper(_mock_sessions())
        assert svc._reaper_task is not None

        await svc.stop()
        assert svc._reaper_task is None

    @pytest.mark.asyncio
    async def test_force_reap_without_sessions(self, tmp_path: object) -> None:
        """_force_reap handles missing sessions gracefully."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = None

        job = _make_job("nosess1")
        svc._jobs = [job]

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("nosess1", _JOB_TIMEOUT_SECS + 10)

        assert job.last_status == "error"
        assert "nosess1" in svc._reaped_jobs

    @pytest.mark.asyncio
    async def test_job_start_time_tracked(self) -> None:
        """_run_job_isolated records and cleans up start time."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        job = _make_job("track1")
        svc._executing.add("track1")

        start_captured: list[bool] = []

        async def capture_start(j: CronJob) -> None:
            start_captured.append("track1" in svc._job_start_times)

        with patch.object(svc, "_execute_with_timeout", side_effect=capture_start), patch.object(
            svc, "_merge_job_result"
        ):
            await svc._run_job_isolated(job)

        assert start_captured == [True]  # was tracked during execution
        assert "track1" not in svc._job_start_times  # cleaned up after

    @pytest.mark.asyncio
    async def test_reaper_loop_invokes_force_reap_for_expired_job(self) -> None:
        """Reaper loop calls _force_reap for an expired, non-done job."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        svc._job_start_times["exp1"] = time.time() - _JOB_TIMEOUT_SECS - 60
        svc._running_tasks["exp1"] = MagicMock(done=MagicMock(return_value=False))

        with patch.object(
            svc, "_force_reap", new_callable=AsyncMock
        ) as mock_reap, patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        mock_reap.assert_awaited_once()
        assert mock_reap.call_args[0][0] == "exp1"

    @pytest.mark.asyncio
    async def test_force_reap_cleans_up_executing_and_running_tasks(self, tmp_path: object) -> None:
        """_force_reap removes job from _executing and _running_tasks directly."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("cleanup1")
        svc._jobs = [job]
        mock_task = MagicMock(done=MagicMock(return_value=False))
        svc._running_tasks["cleanup1"] = mock_task
        svc._executing.add("cleanup1")

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("cleanup1", _JOB_TIMEOUT_SECS + 10)

        assert "cleanup1" not in svc._executing
        assert "cleanup1" not in svc._running_tasks
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_reaper_respects_custom_timeout_secs(self) -> None:
        """Reaper does not kill a job still within its custom timeout_secs."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("custom1")
        job.timeout_secs = 5400  # 90 min
        svc._jobs = [job]
        # Running for 2000s — past default 1800 but within custom 5400
        svc._job_start_times["custom1"] = time.time() - 2000
        svc._running_tasks["custom1"] = MagicMock(done=MagicMock(return_value=False))

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert "custom1" not in svc._reaped_jobs

    @pytest.mark.asyncio
    async def test_reaper_kills_job_exceeding_custom_timeout(self, tmp_path: object) -> None:
        """Reaper kills a job that exceeds its custom timeout_secs."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("custom2")
        job.timeout_secs = 5400
        svc._jobs = [job]
        svc._job_start_times["custom2"] = time.time() - 5500
        svc._running_tasks["custom2"] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert "custom2" in svc._reaped_jobs
        assert job.last_status == "error"
        assert "exceeded 5400s deadline" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_reaper_enforces_floor_for_low_timeout(self) -> None:
        """Reaper uses _JOB_TIMEOUT_SECS as floor even if job.timeout_secs is lower."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("floor1")
        job.timeout_secs = 600  # below 1800 floor
        svc._jobs = [job]
        # Running for 1000s — past job.timeout_secs but within floor
        svc._job_start_times["floor1"] = time.time() - 1000
        svc._running_tasks["floor1"] = MagicMock(done=MagicMock(return_value=False))

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert "floor1" not in svc._reaped_jobs

    @pytest.mark.asyncio
    async def test_reaper_caps_at_86400(self, tmp_path: object) -> None:
        """Reaper caps deadline at 86400 even if job.timeout_secs exceeds it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("cap1")
        job.timeout_secs = 100000  # exceeds 86400 cap
        svc._jobs = [job]
        svc._job_start_times["cap1"] = time.time() - 86500
        svc._running_tasks["cap1"] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert "cap1" in svc._reaped_jobs
        assert "exceeded 86400s deadline" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_reaper_falls_back_for_deleted_job(self, tmp_path: object) -> None:
        """Reaper uses default deadline when job is no longer in self._jobs."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        # No job in self._jobs, but start time still tracked (race: job removed while running)
        svc._jobs = []
        svc._job_start_times["ghost1"] = time.time() - _JOB_TIMEOUT_SECS - 60
        svc._running_tasks["ghost1"] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert "ghost1" in svc._reaped_jobs
