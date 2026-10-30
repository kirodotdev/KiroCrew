"""Tests for the cron reaper that force-kills zombie cron jobs.

This file holds the sweep's deadline, the release of a finished task, the off-loop
scans, and how the reap reads its process handles. Its siblings hold the rest:
``test_cron_reaper_failed_kill.py``, ``test_cron_reaper_kill_set.py``,
``test_cron_reaper_ending_fence.py`` and ``test_cron_reaper_process_identity.py``. The
fixtures and fakes they share are in ``cron_reaper_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import cron_reaper_helpers
import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew import platform_compat
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronJob, CronService, _RunClaim
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.process_identity import ProcessHandle, process_survived_async

#: These tests fire cron jobs through the due-scan, which defers firings while
#: the host's memory posture is critical (see ``ample_host_free_memory``).
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")

# The shared fixtures and helpers, bound here by name: pytest registers the fixtures
# from this namespace, and test_cron_cancel.py and test_cron_run_claim.py import the
# kill-seam names from this module.
_KILL_PRIMITIVES = cron_reaper_helpers._KILL_PRIMITIVES
_START_ID = cron_reaper_helpers._START_ID
_group_gone = cron_reaper_helpers._group_gone
_isolated_leader = cron_reaper_helpers._isolated_leader
_kill_seam = cron_reaper_helpers._kill_seam
_live_task = cron_reaper_helpers._live_task
_make_job = cron_reaper_helpers._make_job
_mock_sessions = cron_reaper_helpers._mock_sessions
_overdue_reap_fixture = cron_reaper_helpers._overdue_reap_fixture
_refuse_unpinned_signal = cron_reaper_helpers._refuse_unpinned_signal
_refuse_unpinned_signal_async = cron_reaper_helpers._refuse_unpinned_signal_async
_retain_torn_down = cron_reaper_helpers._retain_torn_down
_session_with_pid = cron_reaper_helpers._session_with_pid
_start_id_reads = cron_reaper_helpers._start_id_reads
_torn_down_by_the_run = cron_reaper_helpers._torn_down_by_the_run


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
        claim = svc._claims["expired1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 120,
            task=_live_task(),
        )

        with patch("kiro_crew.sel.sel") as mock_sel, patch.object(svc, "_save"):
            await svc._force_reap("expired1", _JOB_TIMEOUT_SECS + 120, claim=claim)

        assert job.last_status == "error"
        assert "Reaped" in (job.last_error or "")
        assert claim.reaped
        assert "expired1" not in svc._claims  # released
        # ``ends_conversation``: the reaper has given up on the run, so its conversation
        # is over and its sub-agent runs end with it. Asserting the whole call keeps a
        # later edit from dropping that and leaving a reaped job's children running.
        sessions.reset.assert_awaited_once_with("cron:expired1", ends_conversation=True, scope=ANY)
        mock_sel().log_tool_invocation.assert_called_once_with(
            session_key="cron:expired1",
            source="cron",
            tool_name="reaper_force_kill",
            outcome="reaped",
            metadata={
                "job_id": "expired1",
                "session_key": "cron:expired1",
                "session_keys": ["cron:expired1"],
                "elapsed": _JOB_TIMEOUT_SECS + 120,
            },
        )

    @pytest.mark.asyncio
    async def test_reaper_skips_jobs_within_deadline(self) -> None:
        """Reaper does not touch jobs still within the timeout."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        sessions = _mock_sessions()
        svc._sessions = sessions

        claim = svc._claims["ok1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 60
        )  # 60s old

        with (
            patch("kiro_crew.sel.sel"),
            patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not claim.reaped
        sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaper_skips_done_tasks(self) -> None:
        """Reaper skips jobs whose asyncio task already completed (race guard)."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        done_task = MagicMock()
        done_task.done.return_value = True
        claim = svc._claims["done1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=done_task
        )

        with (
            patch("kiro_crew.sel.sel"),
            patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not claim.reaped  # nothing reaped
        assert "done1" not in svc._claims  # cleaned up

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
        claim = svc._claims["hang1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel"),
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.05),
            patch.object(
                svc, "_sigkill_session", new_callable=lambda: AsyncMock(return_value=None)
            ) as mock_kill,
            patch.object(svc, "_save"),
        ):
            await svc._force_reap("hang1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert job.last_status == "error"
        # ``popped`` is the session the reset actually popped, forwarded so the kill
        # can release that session's own lease even when the reset was cancelled
        # before ``provider.shutdown()`` and the torn-down table has unwound.
        mock_kill.assert_awaited_once_with("cron:hang1", None, who="Reaper", popped=[])

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
        claim = svc._claims["exc1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel"),
            patch.object(
                svc, "_sigkill_session", new_callable=lambda: AsyncMock(return_value=None)
            ) as mock_kill,
            patch.object(svc, "_save"),
        ):
            await svc._force_reap("exc1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert job.last_status == "error"
        # ``popped`` is the session the reset actually popped, forwarded so the kill
        # can release that session's own lease even when the reset was cancelled
        # before ``provider.shutdown()`` and the torn-down table has unwound.
        mock_kill.assert_awaited_once_with("cron:exc1", None, who="Reaper", popped=[])

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
        claim = svc._claims["cancel1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=mock_task
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("cancel1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        mock_task.cancel.assert_called_once_with()
        assert claim.reaped
        assert "cancel1" not in svc._claims

    @pytest.mark.asyncio
    async def test_reaper_persists_state(self, tmp_path: object) -> None:
        """Reaper calls _save() after updating job state."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("persist1")
        svc._jobs = [job]
        claim = svc._claims["persist1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=_live_task()
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save") as mock_save:
            await svc._force_reap("persist1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        mock_save.assert_called_once()
        # What the save persists: the reaped outcome on the job record.
        assert job.last_status == "error"
        assert "Reaped" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_reaped_runs_count_toward_auto_pause(self, tmp_path: object) -> None:
        """A started run the reaper kills is a failure: enough of them auto-pause the job."""
        from kiro_crew.cron_service.model import _AUTO_PAUSE_THRESHOLD

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        job = _make_job("hang1")
        svc._jobs = [job]

        for n in range(1, _AUTO_PAUSE_THRESHOLD + 1):
            job.failure_recorded = False  # each run starts with no failure recorded
            claim = svc._claims["hang1"] = _RunClaim(
                trigger="scheduled",
                claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10,
                task=_live_task(),
                started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 10,
            )
            with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
                await svc._force_reap("hang1", _JOB_TIMEOUT_SECS + 10, claim=claim)
            assert job.consecutive_failures == n

        assert job.auto_paused is True
        assert job.enabled is False

    @pytest.mark.asyncio
    async def test_a_reaped_run_that_never_started_is_not_counted(self, tmp_path: object) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        job = _make_job("nostart1")
        svc._jobs = [job]
        claim = svc._claims["nostart1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=_live_task()
        )
        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("nostart1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_a_reaped_run_whose_payload_never_started_is_not_counted(
        self, tmp_path: object
    ) -> None:
        """A run stamped but stopped at the fire-time gate never ran a line: no count."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        job = _make_job("gated1")
        job.run_never_started = True
        svc._jobs = [job]
        claim = svc._claims["gated1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10,
            task=_live_task(),
            started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 10,
        )
        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("gated1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert job.consecutive_failures == 0

    @pytest.mark.parametrize("raises", [False, True])
    def test_a_pause_the_store_did_not_take_is_rolled_back(self, raises: bool) -> None:
        """A failed save keeps the in-memory job scheduled as the disk says."""
        from kiro_crew.cron_service.model import _AUTO_PAUSE_THRESHOLD
        from kiro_crew.cron_service.store import CronStoreUnreadable

        svc = CronService(base_dir=None, on_job=AsyncMock())
        job = _make_job("unread1")
        job.consecutive_failures = _AUTO_PAUSE_THRESHOLD - 1
        svc._jobs = [job]
        error = OSError(28, "No space left on device") if raises else CronStoreUnreadable("x")
        with (
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save", side_effect=error),
            pytest.raises(OSError) if raises else nullcontext(),
        ):
            svc._merge_terminal_state_locked(
                "unread1",
                last_status="error",
                last_error="Reaped",
                last_run_ts=time.time(),
                run_generation=1,
                count_failure=True,
            )

        assert (job.enabled, job.auto_paused) == (True, False)
        assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD - 1

    @pytest.mark.asyncio
    async def test_reaped_flag_prevents_merge(self, tmp_path: object) -> None:
        """When reaper kills a job, _run_job_isolated skips _merge_job_result."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("reaped1")
        svc._jobs = [job]
        claim = svc._claim_run("reaped1", "scheduled")
        claim.reaped = True

        with (
            patch.object(svc, "_execute_with_timeout", new_callable=AsyncMock),
            patch.object(svc, "_merge_job_result") as mock_merge,
        ):
            await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        # Only a merge writes the run's generation, so the job keeps the old one.
        assert job.run_generation == 0

    @pytest.mark.asyncio
    async def test_reaped_flag_prevents_merge_on_cancel(self) -> None:
        """Reaped job skips merge even when CancelledError propagates."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("reaped2")
        svc._jobs = [job]
        claim = svc._claim_run("reaped2", "scheduled")
        claim.reaped = True

        with (
            patch.object(svc, "_execute_with_timeout", side_effect=asyncio.CancelledError),
            patch.object(svc, "_merge_job_result") as mock_merge,
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_reaped_job_merges_normally(self) -> None:
        """Normal (non-reaped) job still merges results."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("normal1")

        with (
            patch.object(svc, "_execute_with_timeout", new_callable=AsyncMock),
            patch.object(svc, "_merge_job_result") as mock_merge,
        ):
            await svc._run_job_isolated(job, svc._claim_run("normal1", "scheduled"))

        mock_merge.assert_called_once()
        (record,) = mock_merge.call_args.args
        # The record is the job as the run left it, plus this run's own
        # generation, which only a merge ever writes to the job.
        assert record.run_generation == 1
        assert job.run_generation == 0
        record.run_generation = 0
        assert record == job

    @pytest.mark.asyncio
    async def test_start_reaper_creates_task(self) -> None:
        """start_reaper creates a background asyncio task."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        sessions = _mock_sessions()

        svc.start_reaper(sessions)
        assert svc._reaper_task is not None
        assert svc._sessions is sessions

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
        claim = svc._claims["nosess1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("nosess1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert job.last_status == "error"
        assert claim.reaped

    @pytest.mark.asyncio
    async def test_job_start_time_tracked(self) -> None:
        """_run_job_isolated records and cleans up start time."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        job = _make_job("track1")

        start_captured: list[bool] = []

        async def capture_start(j: CronJob, claim: object = None) -> None:
            start_captured.append(svc.running_since("track1") is not None)

        with (
            patch.object(svc, "_execute_with_timeout", side_effect=capture_start),
            patch.object(svc, "_merge_job_result"),
        ):
            await svc._run_job_isolated(job, svc._claim_run("track1", "scheduled"))

        assert start_captured == [True]  # was tracked during execution
        assert "track1" not in svc._claims  # cleaned up after

    @pytest.mark.asyncio
    async def test_reaper_loop_invokes_force_reap_for_expired_job(self) -> None:
        """Reaper loop calls _force_reap for an expired, non-done job."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        svc._claims["exp1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with (
            patch.object(svc, "_force_reap", new_callable=AsyncMock) as mock_reap,
            patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        mock_reap.assert_awaited_once()
        assert mock_reap.call_args[0][0] == "exp1"

    @pytest.mark.asyncio
    async def test_force_reap_cleans_up_executing_and_running_tasks(self, tmp_path: object) -> None:
        """_force_reap releases the job's claim -- tracked task and all -- directly."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("cleanup1")
        svc._jobs = [job]
        mock_task = MagicMock(done=MagicMock(return_value=False))
        claim = svc._claims["cleanup1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=mock_task
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("cleanup1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert "cleanup1" not in svc._claims
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
        claim = svc._claims["custom1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 2000, task=_live_task()
        )

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not claim.reaped  # nothing reaped

    @pytest.mark.asyncio
    async def test_reaper_kills_job_exceeding_custom_timeout(self, tmp_path: object) -> None:
        """Reaper kills a job that exceeds its custom timeout_secs."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("custom2")
        job.timeout_secs = 5400
        svc._jobs = [job]
        claim = svc._claims["custom2"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 5500, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save"),
            patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert claim.reaped
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
        claim = svc._claims["floor1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 1000, task=_live_task()
        )

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not claim.reaped  # nothing reaped

    @pytest.mark.asyncio
    async def test_reaper_caps_at_86400(self, tmp_path: object) -> None:
        """Reaper caps deadline at 86400 even if job.timeout_secs exceeds it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("cap1")
        job.timeout_secs = 100000  # exceeds 86400 cap
        svc._jobs = [job]
        claim = svc._claims["cap1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 86500, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save"),
            patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert claim.reaped
        assert "exceeded 86400s deadline" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_reaper_falls_back_for_deleted_job(self, tmp_path: object) -> None:
        """Reaper uses the default deadline when the job is absent from self._jobs."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        # No job in self._jobs, but the claim still stands (race: job removed while running)
        svc._jobs = []
        claim = svc._claims["ghost1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save"),
            patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert claim.reaped


def _one_sweep() -> Any:
    """Patch ``asyncio.sleep`` so ``_reaper_loop`` runs one sweep then unwinds."""
    return patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))


class TestReaperMonotonicDeadline:
    """The backstop must measure elapsed runtime on the same clock as ``wait_for``.

    ``_execute_with_timeout`` arms ``asyncio.wait_for``, whose deadline runs on
    the event loop's monotonic clock. If the reaper decides on the wall clock the
    two deadlines disagree whenever the wall clock jumps (host suspend, NTP step)
    and the backstop pre-empts a run the primary path considers healthy.
    """

    @pytest.mark.asyncio
    async def test_reaper_ignores_wallclock_jump_from_host_sleep(self) -> None:
        """A wall-clock start older than the deadline is not reaped while the
        monotonic runtime is still short (the host slept mid-run)."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("slept1")
        svc._jobs = [job]
        # Wall clock says the job has been running past its deadline only
        # because the host was suspended; it has actually executed for 60s.
        claim = svc._claims["slept1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 600,
            started_monotonic=time.monotonic() - 60,
            task=_live_task(),
        )

        with patch.object(svc, "_force_reap", new_callable=AsyncMock) as mock_reap, _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        mock_reap.assert_not_awaited()
        assert not claim.reaped  # nothing reaped

    @pytest.mark.asyncio
    async def test_reaper_still_kills_genuine_overrun_on_monotonic_clock(self) -> None:
        """A job whose monotonic runtime exceeds the deadline is still reaped."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("over1")
        svc._jobs = [job]
        svc._claims["over1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
            started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 60,
            task=_live_task(),
        )

        with patch.object(svc, "_force_reap", new_callable=AsyncMock) as mock_reap, _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        mock_reap.assert_awaited_once()
        assert mock_reap.call_args[0][0] == "over1"

    @pytest.mark.asyncio
    async def test_run_job_isolated_stamps_and_clears_monotonic_start(self) -> None:
        """_run_job_isolated stamps the monotonic start on the claim it releases."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        job = _make_job("mono1")
        claim = svc._claim_run("mono1", "scheduled")

        seen: list[bool] = []

        async def capture(j: CronJob, claim_arg: object = None) -> None:
            seen.append(claim.started_monotonic is not None)

        with patch.object(svc, "_merge_job_result"):
            with patch.object(svc, "_execute_with_timeout", side_effect=capture):
                await svc._run_job_isolated(job, claim)

        assert seen == [True]  # was tracked during execution
        assert "mono1" not in svc._claims  # cleaned up after

    @pytest.mark.asyncio
    async def test_force_reap_clears_monotonic_start(self, tmp_path: object) -> None:
        """_force_reap pops the claim, monotonic stamp and all, so a reap never repeats."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("reap1")
        svc._jobs = [job]
        claim = svc._claims["reap1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
            started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 60,
            task=_live_task(),
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("reap1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "reap1" not in svc._claims

    @pytest.mark.asyncio
    async def test_force_reap_releases_the_jitter_stamp_its_fenced_finalizer_skips(
        self, tmp_path: object
    ) -> None:
        """A reaped run's jitter stamp is released by the reap itself.

        ``_run_job_isolated`` stamps the jitter on its claim and its ``finally``
        releases the claim only while the run still holds it (identity).
        ``_force_reap`` takes that claim before the finalizer runs, so the fence
        is False for a reaped run and the finalizer leaves it alone; the reap
        has to release the claim itself, or a one-shot that is reaped and then
        removed keeps its entry for the process lifetime.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("reapjit1")
        svc._jobs = [job]
        claim = svc._claims["reapjit1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60
        )

        async def reap_mid_run(job_arg: CronJob, claim_arg: Any = None) -> None:
            # The sweep fires while this run is in flight: it takes the claim
            # and cancels the task, whose CancelledError lands here.
            assert claim.jitter is not None  # stamped by the run itself
            await svc._force_reap("reapjit1", _JOB_TIMEOUT_SECS + 60, claim=claim)
            raise asyncio.CancelledError

        with (
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save"),
            patch.object(svc, "_execute_with_timeout", side_effect=reap_mid_run),
            patch.object(svc, "_merge_job_result") as mock_merge,
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        assert "reapjit1" not in svc._claims, (
            "the reaped run's claim was orphaned: _force_reap took the claim, the "
            "finalizer's ownership fence then skipped its release, and the claim "
            f"still stands with jitter {claim.jitter!r}"
        )

    @pytest.mark.asyncio
    async def test_reaper_done_task_cleanup_clears_monotonic_start(self) -> None:
        """The done-task cleanup branch releases the whole claim, monotonic stamp included."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        svc._claims["done2"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
            started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 60,
            task=MagicMock(done=MagicMock(return_value=True)),
        )

        with patch("kiro_crew.sel.sel"), _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert "done2" not in svc._claims

    @pytest.mark.asyncio
    async def test_reaper_falls_back_to_wallclock_without_monotonic_stamp(
        self, tmp_path: object
    ) -> None:
        """A claim with no monotonic stamp (a run that has not stamped one)
        still reaps, on the wall-clock elapsed."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("legacy1")
        svc._jobs = [job]
        claim = svc._claims["legacy1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        assert claim.started_monotonic is None

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert claim.reaped


class TestReaperReleasesFinishedTask:
    """A finished task the sweep meets never ran its ``finally``: release it.

    ``_run_job_isolated``'s ``finally`` releases the run's claim before its task
    ends, so a task that is ``done()`` while its claim is still stored exited
    without reaching that ``finally``, and the claim -- the job's occupancy --
    is still standing. The due-scan (``_on_timer``), ``_next_wake_secs`` and
    ``run_job`` all skip a claimed job, so
    until something releases it the job silently misses every scheduled fire.
    The manual-run route releases it only when a user clicks Run; the sweep is
    the consumer that runs on its own, so it must do the same release -- and
    on the sweep that meets the finished task, not at the run's deadline,
    which is at least ``_JOB_TIMEOUT_SECS`` and up to a day away.
    """

    @pytest.mark.asyncio
    async def test_scheduled_fire_resumes_after_the_sweep_meets_a_finished_task(
        self, tmp_path: Path
    ) -> None:
        ran: list[str] = []

        async def callback(job: CronJob) -> None:
            ran.append(job.id)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        svc._sessions = _mock_sessions()
        await svc.start()
        try:
            svc.add_job("watch", "go", every_secs=60)
            job = svc._jobs[0]
            job.last_run_ts = time.time() - 120  # due now

            async def _died_before_cleanup() -> None:
                raise RuntimeError("run ended without reaching its finally")

            # What a run leaves behind when its task ends ahead of the
            # try/finally: its claim still stored, holding the finished task
            # and the stamps -- and well inside the deadline, so the sweep's
            # timeout path is not what releases it.
            stale = asyncio.get_running_loop().create_task(_died_before_cleanup())
            await asyncio.gather(stale, return_exceptions=True)
            assert stale.done()
            claim = svc._claims[job.id] = _RunClaim(
                trigger="scheduled",
                claimed_at=time.time() - 60,
                started_monotonic=time.monotonic() - 60,
                jitter=0.0,
                task=stale,
            )

            # Control: while the leftovers stand, the due-scan skips the job.
            await svc._on_timer()
            assert svc._claims[job.id].task is stale
            assert ran == []

            with patch("kiro_crew.sel.sel"), _one_sweep():
                with pytest.raises(asyncio.CancelledError):
                    await svc._reaper_loop()

            assert job.id not in svc._claims, (
                "the sweep met a finished task and left the job claimed, "
                "so the due-scan keeps skipping every scheduled fire of it"
            )
            # Released, not reaped: the run was over, there was nothing to kill.
            assert not claim.reaped
            svc._sessions.reset.assert_not_awaited()

            # The next tick fires the job again.
            await svc._on_timer()
            fresh = svc._claims[job.id].task
            assert fresh is not None and fresh is not stale
            await fresh
            assert ran == [job.id]
        finally:
            await svc.stop()


class TestTheScansLeaveTheLoop:
    """The per-child identity and liveness reads never run on the event loop.

    ``process_survived`` walks every recorded child with a synchronous ``/proc``
    or ``sysctl`` read, as do the post-sweep standing read and the group-member
    vouch; a client can record thousands of descendants, and on the gateway loop
    the walk would stall chat and heartbeat mid-reap. The loop-side entries run
    them on the subprocess executor -- pinned here by the thread the platform
    read observes.
    """

    @staticmethod
    def _reads_off_the_loop() -> tuple[Any, list[int]]:
        threads: list[int] = []

        def _start_id(pid: int) -> str:
            threads.append(threading.get_ident())
            return _START_ID

        return (
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
            threads,
        )

    @pytest.mark.asyncio
    async def test_process_survived_async_runs_the_scan_on_a_worker_thread(self) -> None:
        handle = ProcessHandle(
            pid=_UNALLOCATABLE_PID,
            start_id=_START_ID,
            pgid=None,
            child_pids={_UNALLOCATABLE_PID + 1: (_START_ID, "kiro-cli")},
        )
        reads, threads = self._reads_off_the_loop()
        with (
            reads,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
        ):
            assert await process_survived_async(handle) is True

        assert threads and all(
            ident != threading.get_ident() for ident in threads
        ), "the per-child scan ran on the event loop"

    @pytest.mark.asyncio
    async def test_the_post_sweep_standing_read_runs_on_a_worker_thread(self) -> None:
        from kiro_crew.process_identity import _sweep_children

        child = _UNALLOCATABLE_PID + 2
        reads, threads = self._reads_off_the_loop()
        with (
            reads,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
        ):
            failure = await _sweep_children(
                {child: (_START_ID, "kiro-cli")},
                who="Reaper",
                key="cron:x",
                kill_escaped_children=MagicMock(),
            )

        assert failure is None
        assert threads and all(
            ident != threading.get_ident() for ident in threads
        ), "the post-sweep standing read ran on the event loop"

    @pytest.mark.asyncio
    async def test_the_group_member_vouch_runs_on_a_worker_thread(self) -> None:
        from kiro_crew.process_identity import _finish_gone_leader

        pid, pgid = _UNALLOCATABLE_PID, _UNALLOCATABLE_PID
        handle = ProcessHandle(pid=pid, start_id=_START_ID, pgid=pgid, child_pids={})
        reads, threads = self._reads_off_the_loop()
        with (
            reads,
            patch("kiro_crew.platform_compat.IS_WINDOWS", False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=pgid),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            failure = await _finish_gone_leader(handle, who="Reaper", key="cron:x")

        assert failure is None
        group_kill.assert_called_once_with(pgid, platform_compat.SIGKILL)
        assert threads and all(
            ident != threading.get_ident() for ident in threads
        ), "the group-member vouch ran on the event loop"


class TestTheHandleOfATornDownSession:
    """``_session_process_handles`` reads a session the run's own teardown popped."""

    def test_a_live_miss_falls_back_to_the_manager_s_torn_down_table(self) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:torn", 7171)
        _torn_down_by_the_run(svc, "cron:torn")

        handles = svc._session_process_handles("cron:torn")

        assert len(handles) == 1, "the torn-down session was not read"
        assert handles[0].pid == 7171 and handles[0].start_id == _START_ID

    def test_a_live_session_beside_a_torn_down_one_is_a_second_candidate(self) -> None:
        """Both are read: the torn-down run process first, the live successor after it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:live", 7272)
        _torn_down_by_the_run(svc, "cron:live")
        _session_with_pid(svc, "cron:live", 7373)

        handles = svc._session_process_handles("cron:live")

        assert [h.pid for h in handles] == [7272, 7373]

    def test_a_key_with_neither_has_no_handle(self) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        assert svc._session_process_handles("cron:nothing") == []
        svc._sessions.tearing_down.assert_called_once_with("cron:nothing")


class TestTheHandleOfAnOwnedPopen:
    """A provider that runs its process through its own ``Popen`` still yields a handle.

    The claude harness keeps the process on ``provider._proc`` / ``_active_proc``
    and records no start id on an ACP client, so the handle would carry no pid
    at all -- and a hung reset there would be audited ``reaped`` with nothing
    verified. The pid is read off the live owned handle and its identity read
    fresh: a ``Popen`` whose ``returncode`` is unset pins the pid for its parent,
    so the read names the process the provider owns.
    """

    def _session_with_proc(self, pid: int | None, returncode: int | None) -> MagicMock:
        session = MagicMock()
        session.provider = MagicMock(spec=["_proc", "_active_proc"])
        proc = MagicMock(spec=["pid", "returncode"])
        proc.pid = pid
        proc.returncode = returncode
        session.provider._proc = proc
        session.provider._active_proc = None
        return session

    def test_a_live_owned_popen_supplies_the_pid_and_a_fresh_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.process_identity import process_handle_of

        # POSIX seam: the group read runs, and is stubbed. ``os.getpgid`` does not
        # exist on win32, so the patch creates the attribute there.
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        pid = _UNALLOCATABLE_PID
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id", return_value="7770001"
            ) as reads,
            patch(
                "os.getpgid",
                side_effect=ProcessLookupError("[Errno 3] No such process"),
                create=True,
            ),
        ):
            handle = process_handle_of(self._session_with_proc(pid, None))

        assert (handle.pid, handle.start_id) == (pid, "7770001")
        assert reads.call_args_list[0] == call(pid)

    def test_an_exited_owned_popen_names_no_process(self) -> None:
        from kiro_crew.process_identity import process_handle_of

        handle = process_handle_of(self._session_with_proc(_UNALLOCATABLE_PID, 0))

        assert handle.pid is None and handle.start_id is None

    def test_the_acp_client_s_pid_takes_precedence(self) -> None:
        from kiro_crew.process_identity import process_handle_of

        client_pid = _UNALLOCATABLE_PID + 1
        session = self._session_with_proc(_UNALLOCATABLE_PID, None)
        session.provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        session.provider._client = MagicMock(_pid=client_pid, _start_time=_START_ID, _child_pids={})
        session.provider._proc = MagicMock(pid=_UNALLOCATABLE_PID, returncode=None)
        session.provider._active_proc = None

        with patch("kiro_crew.platform_compat.get_process_start_id", return_value=None):
            handle = process_handle_of(session)

        assert (handle.pid, handle.start_id) == (client_pid, _START_ID)


class TestCronReapRefreshesTheTreeBeforeTheHandle:
    """The cron reap takes its handle after re-walking the live leader's tree.

    The client's startup record misses children a run's turns started, and only
    a recorded member may vouch for a leaderless group.
    """

    @pytest.mark.asyncio
    async def test_a_late_child_is_on_the_handle_the_reap_takes(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, _job, claim = _overdue_reap_fixture(tmp_path, "late1")
        client = _session_with_pid(svc, "cron:late1", 7979)

        async def _refresh() -> None:
            client._child_pids[8979] = ("5150001", b"sleep")

        client.refresh_process_tree = AsyncMock(side_effect=_refresh)
        original = svc._sessions_under
        taken: list[list[tuple[Any, ProcessHandle]]] = []

        def _capture(session_key: str) -> list[tuple[Any, ProcessHandle]]:
            pairs = original(session_key)
            taken.append(pairs)
            return pairs

        with (
            patch.object(svc, "_sessions_under", side_effect=_capture),
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7979: [_START_ID, _START_ID, None]}),
            ),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("os.getpgid", return_value=7979, create=True),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_group"),
        ):
            await svc._force_reap("late1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        client.refresh_process_tree.assert_awaited()
        assert taken, "the reap never took a handle"
        handles = [handle for _session, handle in taken[0]]
        assert handles and all(
            8979 in handle.child_pids for handle in handles
        ), "the cron reap's handle missed the child the run started"
