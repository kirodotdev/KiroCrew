"""Tests for the cron reaper that force-kills zombie cron jobs."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew import platform_compat
from kiro_crew.cron import (
    _JOB_TIMEOUT_SECS,
    CronJob,
    CronSchedule,
    CronService,
    _RunClaim,
)
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.process_identity import (
    ProcessHandle,
    process_handle_of,
    process_survived,
    process_survived_async,
)
from kiro_crew.session_lifecycle import TornDown, _TeardownScope

#: The platform kill primitives the kill path can reach. Each is pinned to a
#: refusal for the whole module (``_kill_seam``): a test that drives one patches
#: it inside its own block, and an unpinned call surfaces as a loud test failure
#: instead of a real signal to whatever process owns a fabricated pid on the host.
_KILL_PRIMITIVES = (
    "kill_process_group",
    "kill_pid",
    "kill_pid_async",
    "kill_process_tree",
    "kill_process_tree_async",
    "kill_process_tree_pinned",
)


def _refuse_unpinned_signal(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        f"unpinned kill primitive called with {args!r}: patch the seam this test drives"
    )


async def _refuse_unpinned_signal_async(*args: Any, **kwargs: Any) -> Any:
    _refuse_unpinned_signal(*args, **kwargs)


@pytest.fixture(autouse=True)
def _kill_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill path is POSIX-shaped here unless a test pins ``IS_WINDOWS`` itself, and no primitive signals for real.

    ``kill_verified_process`` reads ``platform_compat.IS_WINDOWS`` at call time:
    on POSIX the live root's captured group is signalled through
    ``kill_process_group`` (which the POSIX-shaped tests here patch to land or
    raise), on Windows through ``kill_process_tree_pinned``, which a fabricated
    pid can never pin -- so an unpinned test read differently on a Windows runner
    than on Linux. A test of the Windows shape sets ``IS_WINDOWS`` True in its
    own body, after this fixture, and wins. The other platform reads these tests
    make (the pid liveness, the start id, the group capture, the group probe) are
    stubbed per test; every kill primitive is pinned to a refusal here so a
    seam a test forgot to patch cannot reach the host (see ``_KILL_PRIMITIVES``).
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    for name in _KILL_PRIMITIVES:
        guard = _refuse_unpinned_signal_async if name.endswith("_async") else _refuse_unpinned_signal
        monkeypatch.setattr(platform_compat, name, guard)


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    sessions._sessions = {}
    # No teardown in flight: the manager's torn-down table is empty, so a live-map
    # miss is a key with no process (``SessionManager.tearing_down``).
    sessions.tearing_down = MagicMock(return_value=[])
    # A real scope, so a fake reset can report which session it pops the way the
    # real one does (``scope.note_pop``); a fake reset that ignores it pops nothing.
    sessions.teardown_scope = lambda on_pop=None: _TeardownScope({}, on_pop)
    return sessions


def _live_task() -> MagicMock:
    """A tracked task that has not finished (the reaper's deadline path)."""
    return MagicMock(done=MagicMock(return_value=False))


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
        claim = svc._claims["expired1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 120,
            task=_live_task(),
        )

        with patch("kiro_crew.sel.sel") as mock_sel, patch.object(svc, "_save"):
            await svc._force_reap("expired1", _JOB_TIMEOUT_SECS + 120, claim=claim)

        assert job.last_status == "error"
        assert "Reaped" in (job.last_error or "")
        assert svc._reaped_jobs.has("expired1", claim)
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

        svc._claims["ok1"] = _RunClaim(trigger="scheduled", claimed_at=time.time() - 60)  # 60s old

        with patch("kiro_crew.sel.sel"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        # Should not have been reaped
        assert not svc._reaped_jobs._marks  # nothing reaped
        sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaper_skips_done_tasks(self) -> None:
        """Reaper skips jobs whose asyncio task already completed (race guard)."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        done_task = MagicMock()
        done_task.done.return_value = True
        svc._claims["done1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=done_task
        )

        with patch("kiro_crew.sel.sel"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not svc._reaped_jobs._marks  # nothing reaped
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

        with patch("kiro_crew.sel.sel"), patch(
            "kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.05
        ), patch.object(
            svc, "_sigkill_session", new_callable=lambda: AsyncMock(return_value=None)
        ) as mock_kill, patch.object(svc, "_save"):
            await svc._force_reap("hang1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert job.last_status == "error"
        mock_kill.assert_awaited_once_with("cron:hang1", None, who="Reaper")

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

        with patch("kiro_crew.sel.sel"), patch.object(
            svc, "_sigkill_session", new_callable=lambda: AsyncMock(return_value=None)
        ) as mock_kill, patch.object(svc, "_save"):
            await svc._force_reap("exc1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert job.last_status == "error"
        mock_kill.assert_awaited_once_with("cron:exc1", None, who="Reaper")

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

        mock_task.cancel.assert_called_once()

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

    @pytest.mark.asyncio
    async def test_reaped_flag_prevents_merge(self, tmp_path: object) -> None:
        """When reaper kills a job, _run_job_isolated skips _merge_job_result."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("reaped1")
        svc._jobs = [job]
        claim = svc._claim_run("reaped1", "scheduled")
        svc._reaped_jobs.mark("reaped1", claim)

        with patch.object(svc, "_execute_with_timeout", new_callable=AsyncMock), patch.object(
            svc, "_merge_job_result"
        ) as mock_merge:
            await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        assert not svc._reaped_jobs.has("reaped1", claim)  # cleaned up

    @pytest.mark.asyncio
    async def test_reaped_flag_prevents_merge_on_cancel(self) -> None:
        """Reaped job skips merge even when CancelledError propagates."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("reaped2")
        svc._jobs = [job]
        claim = svc._claim_run("reaped2", "scheduled")
        svc._reaped_jobs.mark("reaped2", claim)

        with patch.object(
            svc, "_execute_with_timeout", side_effect=asyncio.CancelledError
        ), patch.object(svc, "_merge_job_result") as mock_merge:
            with pytest.raises(asyncio.CancelledError):
                await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        assert not svc._reaped_jobs.has("reaped2", claim)

    @pytest.mark.asyncio
    async def test_non_reaped_job_merges_normally(self) -> None:
        """Normal (non-reaped) job still merges results."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("normal1")

        with patch.object(svc, "_execute_with_timeout", new_callable=AsyncMock), patch.object(
            svc, "_merge_job_result"
        ) as mock_merge:
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
        claim = svc._claims["nosess1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("nosess1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert job.last_status == "error"
        assert svc._reaped_jobs.has("nosess1", claim)

    @pytest.mark.asyncio
    async def test_job_start_time_tracked(self) -> None:
        """_run_job_isolated records and cleans up start time."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        job = _make_job("track1")

        start_captured: list[bool] = []

        async def capture_start(j: CronJob, claim: object = None) -> None:
            start_captured.append(svc.running_since("track1") is not None)

        with patch.object(svc, "_execute_with_timeout", side_effect=capture_start), patch.object(
            svc, "_merge_job_result"
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
        svc._claims["custom1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 2000, task=_live_task()
        )

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not svc._reaped_jobs._marks  # nothing reaped

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

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert svc._reaped_jobs.has("custom2", claim)
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
        svc._claims["floor1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 1000, task=_live_task()
        )

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not svc._reaped_jobs._marks  # nothing reaped

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

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert svc._reaped_jobs.has("cap1", claim)
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

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert svc._reaped_jobs.has("ghost1", claim)


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
        svc._claims["slept1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 600,
            started_monotonic=time.monotonic() - 60,
            task=_live_task(),
        )

        with patch.object(svc, "_force_reap", new_callable=AsyncMock) as mock_reap, _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        mock_reap.assert_not_awaited()
        assert not svc._reaped_jobs._marks  # nothing reaped

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

        assert svc._reaped_jobs.has("legacy1", claim)


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
            svc._claims[job.id] = _RunClaim(
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
            assert not svc._reaped_jobs._marks
            svc._sessions.reset.assert_not_awaited()

            # The next tick fires the job again.
            await svc._on_timer()
            fresh = svc._claims[job.id].task
            assert fresh is not None and fresh is not stale
            await fresh
            assert ran == [job.id]
        finally:
            await svc.stop()


# ── A refused or failed SIGKILL is a kill failure, not a reap ──

# The start id the fake client records at spawn (``platform_compat.get_process_start_id``
# reads ``/proc/<pid>/stat`` field 22 on Linux); the kill re-reads it before signalling.
_START_ID = "4821903"


def _session_with_pid(svc: CronService, session_key: str, pid: int | None) -> MagicMock:
    """Register a session under ``session_key`` whose ACP client reports ``pid``.

    The client carries the start id the reaper's recycled-pid check compares, so
    a test whose start-id read answers the same value drives ``_sigkill_session``
    all the way to the group kill.
    """
    client = MagicMock()
    client._pid = pid
    client._child_pids = {}
    client._start_time = _START_ID
    session = MagicMock()
    session.provider._client = client
    svc._sessions._sessions[session_key] = session
    return client


def _handle_of(svc: CronService, session_key: str) -> ProcessHandle:
    """The kill handle ``_force_reap`` / ``cancel`` take before the reset and hand to the kill."""
    handles = svc._session_process_handles(session_key)
    assert handles, f"no session registered under {session_key}"
    return handles[0]


def _torn_down_by_the_run(svc: CronService, session_key: str) -> MagicMock:
    """Move the session under ``session_key`` from the live map into the torn-down table.

    The shape of a run whose OWN teardown reset ran first: the session is out of
    the live map (the pop happens under the registry lock, before the awaits that
    can hang), and the session manager retains it -- with the process handle read
    at the pop, as the real table does -- for exactly the life of that teardown,
    where ``_session_process_handles`` reads it on a live-map miss.
    """
    session = svc._sessions._sessions.pop(session_key)
    _retain_torn_down(svc, session_key, session)
    return session


def _retain_torn_down(svc: CronService, session_key: str, *sessions: Any) -> None:
    """Have the manager double's ``tearing_down`` answer *sessions* under ``session_key``, in order.

    Each entry is the real :class:`TornDown` record -- the session and the handle
    read off it NOW, at the moment the table would have captured it -- so a test
    that clears the session's pid afterwards models exactly the hung teardown
    whose own awaits cleared it. Captured under :func:`_root_reads_back`, as the
    pop would have read it with the leader alive and leading its own group.
    """
    with _root_reads_back():
        entries = [TornDown(session, process_handle_of(session)) for session in sessions]
    svc._sessions.tearing_down = MagicMock(
        side_effect=lambda key: list(entries) if key == session_key else []
    )


def _group_gone() -> Any:
    """The leader's process group is empty: the signal-0 probe of it finds no member.

    Pinned wherever a test walks a gone or recycled leader: the probe is real
    ``os.killpg(number, 0)`` otherwise, and a made-up pid may name a live group
    on the host running the suite.
    """
    return patch("kiro_crew.platform_compat.pgroup_exists", return_value=False)


@contextmanager
def _isolated_leader() -> Iterator[None]:
    """The fabricated root leads its own POSIX group: ``getpgid(pid) == pid``, and our own group is 1.

    Both names are CREATED on a runner that lacks them (Windows has neither), so
    the capture site ``isolated_group_of`` -- which reads them through ``getattr``
    -- captures the group on every platform the POSIX-shaped tests run on.
    """
    with (
        patch("os.getpgid", side_effect=lambda pid: pid, create=True),
        patch("os.getpgrp", return_value=1, create=True),
    ):
        yield


@contextmanager
def _root_reads_back(start_id: str = _START_ID) -> Iterator[None]:
    """The root's start id reads back as ``start_id``, the platform reports the pid alive, it leads its own group, its group probe is empty.

    All four reads are pinned: ``process_survived`` asks the platform for
    liveness after the identity check (Windows reads a creation time back for an
    exited child whose handle is still held) and asks a gone leader's group
    before calling it gone, and an unpinned ``pid_exists`` / ``pgroup_exists`` on
    a made-up pid answers whatever the host happens to run -- not the same on
    every runner. ``os.getpgid`` answers the pid itself (:func:`_isolated_leader`),
    so the snapshot captures the isolated group a ``start_new_session`` root leads
    (``ProcessHandle.pgid == pid``): the kill then addresses THAT captured id
    through ``kill_process_group``, which the test pins, and never a group
    resolved from the pid at signal time. Restored on exit, so a test wanting no
    captured group patches ``os.getpgid`` itself inside the block.
    """
    with (
        patch("kiro_crew.platform_compat.get_process_start_id", return_value=start_id),
        patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        _isolated_leader(),
        _group_gone(),
    ):
        yield


def _kill_path_stubs() -> Any:
    """The child-tree probe, the root reads and the sweep stubbed so only the kill decides."""
    return (
        patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        _root_reads_back(),
        patch("kiro_crew.acp.client._kill_escaped_children"),
    )


def _overdue_reap_fixture(tmp_path: object, job_id: str) -> tuple[CronService, CronJob, _RunClaim]:
    """A service whose session reset fails, so ``_force_reap`` reaches the SIGKILL."""
    svc = CronService(base_dir=None, on_job=AsyncMock())
    svc._history = CronHistoryStore(base_dir=tmp_path)
    svc._sessions = _mock_sessions()
    svc._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
    job = _make_job(job_id)
    svc._jobs = [job]
    claim = svc._claims[job_id] = _RunClaim(
        trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
    )
    return svc, job, claim


def _audited_outcome(mock_sel: MagicMock) -> str:
    return mock_sel().log_tool_invocation.call_args.kwargs["outcome"]


class TestReaperRecordsAFailedSigkill:
    """The audit never says ``reaped`` for a run whose process group was not killed.

    ``_sigkill_session`` raises nothing -- the reap must still finish the claim
    it took -- but it REPORTS what stopped the kill: the broadcast guard's
    refusal of the pid, or the error the kill raised. ``_force_reap`` carries
    that into the run's terminal record (``…; kill failed: <reason>``) and
    audits ``reaper_force_kill`` as ``failed``, the same outcome a kill await
    that raised gets. Before this, both were a log line and the audit read
    ``reaped`` while the process group kept running.
    """

    @pytest.mark.asyncio
    async def test_a_refused_pid_is_audited_as_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "refused1")
        _session_with_pid(svc, "cron:refused1", 4242)
        refusal = ValueError("kill_process_group: refusing broadcast/self process group 4242")
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=refusal,
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("refused1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        # The guard refused the pid outright: nothing was safe to signal, and
        # nothing was.
        pid_kill.assert_not_awaited()
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the SEL audit says the process group was reaped while it is still alive"
        assert (job.last_error or "").startswith("Reaped after")
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")
        assert "4242" in (job.last_error or ""), "the record does not name the refused pid"
        # The run still ended for the record: a terminal row, the claim
        # released, the reap marked -- the failure is added, not substituted.
        runs, total = await svc._history.get_job_history("refused1")
        assert total == 1 and runs[0]["status"] == "timeout"
        assert "; kill failed: " in runs[0]["error"]
        assert "refused1" not in svc._claims
        assert svc._reaped_jobs.has("refused1", claim)
        assert job.last_status == "error"

    @pytest.mark.asyncio
    async def test_the_kill_failure_suffix_is_bounded_at_retention(self, tmp_path: object) -> None:
        """A kill reason the size of a tree's stderr cannot push ``last_error`` past the record bound.

        A Windows tree drain that fails carries one line per process the run
        spawned; joined onto the run's own text it is held to
        ``MAX_ERROR_DETAIL_LEN`` by the shared ``with_kill_failure`` (the same
        bound the sub-agent record applies), the reason keeping at least
        ``KILL_FAILURE_RESERVE_LEN`` of it and the run's text trimmed to make room.
        """
        from kiro_crew.process_identity import (
            KILL_FAILURE_RESERVE_LEN,
            MAX_ERROR_DETAIL_LEN,
            with_kill_failure,
        )

        svc, job, claim = _overdue_reap_fixture(tmp_path, "bounded1")
        _session_with_pid(svc, "cron:bounded1", 4343)
        blob = "\n".join(f"ERROR: The process with PID {4343 + n} could not be terminated." for n in range(200))
        assert len(blob) > MAX_ERROR_DETAIL_LEN
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError(blob),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(side_effect=PermissionError(blob))),
        ):
            await svc._force_reap("bounded1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        last_error = job.last_error or ""
        assert len(last_error) <= MAX_ERROR_DETAIL_LEN, (
            f"last_error is {len(last_error)} chars: the kill failure was appended unbounded"
        )
        assert last_error.startswith("Reaped after")
        assert "; kill failed: PermissionError: ERROR: The process with PID 4343" in last_error
        runs, _ = await svc._history.get_job_history("bounded1")
        assert len(runs[0]["error"]) <= MAX_ERROR_DETAIL_LEN
        # The helper's rule: the reason keeps its reserve even when the run's error fills the bound.
        bounded = with_kill_failure("x" * MAX_ERROR_DETAIL_LEN, blob)
        assert len(bounded) == MAX_ERROR_DETAIL_LEN
        assert bounded.endswith(blob[:KILL_FAILURE_RESERVE_LEN])
        assert bounded.startswith("x" * (MAX_ERROR_DETAIL_LEN - KILL_FAILURE_RESERVE_LEN - len("; kill failed: ")))
        assert with_kill_failure("", "gone") == "kill failed: gone"

    @pytest.mark.asyncio
    async def test_a_group_kill_that_raises_with_no_pid_fallback_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """EPERM on the group and on the pid: the process is there and unsignalled."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "eperm1")
        _session_with_pid(svc, "cron:eperm1", 4343)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
        ):
            await svc._force_reap("eperm1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: PermissionError: " in (job.last_error or "")
        assert "eperm1" not in svc._claims

    @pytest.mark.asyncio
    async def test_a_kill_path_error_before_the_signal_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """The catch-all that only logged: a probe that raises left the group unsignalled."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "probe1")
        _session_with_pid(svc, "cron:probe1", 4444)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.acp.client._get_child_pids",
                side_effect=RuntimeError("cannot schedule new futures after shutdown"),
            ),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("probe1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: RuntimeError: cannot schedule" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_delivered_sigkill_is_still_audited_as_reaped(self, tmp_path: object) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "killed1")
        _session_with_pid(svc, "cron:killed1", 4545)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("killed1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_group_that_is_already_gone_is_nothing_to_kill(self, tmp_path: object) -> None:
        """ProcessLookupError on the group AND the pid: the run's process exited on its own."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "gone1")
        _session_with_pid(svc, "cron:gone1", 4646)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=ProcessLookupError("[Errno 3] No such process"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
        ):
            await svc._force_reap("gone1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_group_error_followed_by_a_gone_pid_keeps_the_group_error(self) -> None:
        """EPERM on the group names members it could not signal; a gone pid does not clear it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:mixed", 4747)
        children, start_id, sweep = _kill_path_stubs()

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
        ):
            failure = await svc._sigkill_session("cron:mixed", _handle_of(svc, "cron:mixed"))

        assert failure is not None and failure.startswith("PermissionError: ")

    @pytest.mark.asyncio
    async def test_a_pid_scoped_fallback_that_lands_is_a_delivered_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX: the escaped-children sweep covers what the group signal missed, so the kill counts.

        The platform seam is pinned to POSIX: this rule is the one the two Windows
        tests below invert (there the root-only fallback cannot stand in for the
        tree walk), so left to the runner's own platform the same fixture reads
        as a failed kill on a Windows shard.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:fallback", 4848)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            assert await svc._sigkill_session("cron:fallback", _handle_of(svc, "cron:fallback")) is None

    @pytest.mark.asyncio
    async def test_on_windows_a_drain_that_raises_is_a_failure_and_no_root_only_fallback_stands_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing sweeps on Windows, so a pinned drain that raised is the failure -- no ``taskkill /PID`` is tried.

        A root-only kill that landed would leave the descendants standing; the
        drain either terminates the tree through its verified handles or reports.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:win-fallback", 4949)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned",
                side_effect=PermissionError("Access is denied."),
            ),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)) as pid_kill,
        ):
            failure = await svc._sigkill_session("cron:win-fallback", _handle_of(svc, "cron:win-fallback"))

        assert failure == (
            "Windows tree cleanup incomplete for pid 4949: PermissionError: Access is denied."
        )
        tree_kill.assert_not_awaited()
        pid_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_windows_a_tree_that_is_already_gone_is_nothing_to_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows rule keeps only real errors: a root the pin cannot find is a gone tree.

        The no-pin variant of the Windows gone-root rule: the object with the
        recorded creation time cannot be opened at the pin (the root exited
        between the last identity read and the pin), and with no exact-tree
        cleanup pending for its identity there is nothing this code can
        enumerate, verify or signal (the pinned drain walked the tree while it
        was alive), and Windows has no process groups to probe -- a gone root
        stays "nothing to kill", the documented platform limitation. A pending
        pin is the other variant (``TestAGoneWindowsRootDrainsItsPendingTreeCleanup``).
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:win-gone", 5050)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True) as group,
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False) as drain,
        ):
            assert await svc._sigkill_session("cron:win-gone", _handle_of(svc, "cron:win-gone")) is None

        drain.assert_called_once_with(5050, _START_ID, platform_compat.SIGKILL)
        tree_kill.assert_not_awaited()
        group.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_handle_and_no_usable_pid_are_nothing_to_kill(self) -> None:
        """The early returns are not failures: there is no process group to answer for.

        No pre-reset handle (no session was live under the key before the
        reset), or a handle with no usable pid -- nothing names a process, so
        nothing is signalled. A live session whose client has no usable pid
        (never spawned, or already reset) is not even a candidate: the key names
        no process through it.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:nopid", None)
        _session_with_pid(svc, "cron:pid1", 1)
        no_pid = ProcessHandle(pid=None, start_id=_START_ID, pgid=None, child_pids={})

        assert svc._session_process_handles("cron:nopid") == []
        assert svc._session_process_handles("cron:pid1") == []
        with patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill:
            assert await svc._sigkill_session("cron:absent", None) is None
            assert await svc._sigkill_session("cron:absent", no_pid) is None
            assert await svc._sigkill_sessions("cron:nopid", []) is None

        tree_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_successor_under_the_key_is_not_the_run_s_process(self) -> None:
        """Only the pre-reset handle names the process; a session in the map now is a successor.

        The reset pops the run's session and awaits; a cold start (a sub-agent
        completion delivering into the key) can register a NEW session under the
        same key in that window. Reading the map at kill time would signal that
        successor and leave the run's own, hung process alive -- recorded reaped.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:succ", 5151)
        children, start_id, sweep = _kill_path_stubs()

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            handle = _handle_of(svc, "cron:succ")
            # The reset popped the run's session; a successor process now holds the key.
            svc._sessions._sessions.pop("cron:succ")
            _session_with_pid(svc, "cron:succ", 8080)
            assert await svc._sigkill_session("cron:succ", handle) is None

        group_kill.assert_called_once_with(5151, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_a_reset_that_hangs_after_popping_the_session_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """The reset pops the session from the map before it can hang; the kill must not need it.

        ``SessionLifecycle.reset`` removes the map entry under its lock and only
        then awaits the shutdown that can hang. Without a handle taken before the
        reset, the fallback looked the key up, found nothing, and the run was
        audited ``reaped`` while its process kept running.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "popped1")
        _session_with_pid(svc, "cron:popped1", 5151)

        async def _pop_then_hang(session_key: str, **_: Any) -> bool:
            svc._sessions._sessions.pop(session_key, None)
            raise asyncio.TimeoutError

        svc._sessions.reset = AsyncMock(side_effect=_pop_then_hang)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("popped1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "cron:popped1" not in svc._sessions._sessions, "the fixture did not pop the session"
        group_kill.assert_called_once_with(5151, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_root_is_verified_by_its_recorded_start_id(self) -> None:
        """A root whose live start id matches the one the client recorded is ours: killed.

        The shared child verifier denies a pid with no recorded basename, and the
        root has none, so validating the root through it never let a real kill
        through. The root is compared by start id, the recycling detector itself.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:root", 5252)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            _isolated_leader(),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            assert await svc._sigkill_session("cron:root", _handle_of(svc, "cron:root")) is None

        group_kill.assert_called_once_with(5252, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_a_recycled_pid_is_nothing_to_kill_and_only_recorded_children_are_swept(
        self,
    ) -> None:
        """A live start id that differs from the recorded one means another process owns the pid now."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:recycled", 5353)
        client._child_pids = {6161: ("111", b"node")}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7171]) as probe,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:recycled", _handle_of(svc, "cron:recycled")) is None

        tree_kill.assert_not_awaited()
        # Never read through a pid that is not ours: no fresh child probe, and
        # the sweep gets only the children the client had recorded.
        probe.assert_not_called()
        sweep.assert_called_once_with({6161: ("111", b"node")})

    @pytest.mark.asyncio
    async def test_a_live_pid_whose_identity_cannot_be_confirmed_is_a_failed_kill(self) -> None:
        """Alive but unverifiable is not gone: not signalled, and recorded as a kill failure."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:unread", 5454)
        client = _session_with_pid(svc, "cron:norecord", 5555)
        client._start_time = None

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            unread = await svc._sigkill_session("cron:unread", _handle_of(svc, "cron:unread"))
            with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
                no_record = await svc._sigkill_session("cron:norecord", _handle_of(svc, "cron:norecord"))

        tree_kill.assert_not_awaited()
        assert unread == "pid 5454 is alive but could not be verified as this run's; not signalled"
        assert no_record == "pid 5555 is alive but could not be verified as this run's; not signalled"

    @pytest.mark.asyncio
    async def test_a_pid_that_has_exited_is_nothing_to_kill(self) -> None:
        """No start id AND no process behind the pid: it exited; only the children are swept."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:exited", 5656)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]) as probe,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:exited", _handle_of(svc, "cron:exited")) is None

        tree_kill.assert_not_awaited()
        probe.assert_not_called()
        sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_root_that_exits_during_the_child_walk_is_not_signalled_and_its_fresh_child_is_named(
        self,
    ) -> None:
        """The start id is read again right before the signal; a changed one is a gone root, and a child the walk found is named.

        The child walk awaits, and the root can exit -- and its pid be handed to
        another process -- while it does. A ``killpg`` through the pid then would
        signal that process's group. A child the walk FOUND (7272, in its own
        group, so the retained group's probe never sees it) was read through a
        pid that may already have been recycled when the walk ran, so it is not
        swept -- and it is not dropped either: the record names it as a kill
        failure, unattributed and not signalled, never ``reaped`` over it. Only
        the children recorded before the reset (6262) are swept.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:walk", 5757)
        client._child_pids = {6262: ("222", b"node")}
        # Snapshotted before the mocked reads: the kill's own two identity reads
        # are the two values below, so the SECOND is the one that differs.
        handle = _handle_of(svc, "cron:walk")

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7272]) as probe,
            patch("kiro_crew.acp.client._capture_child_records", return_value={7272: ("333", b"sh")}),
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            # The root's two reads, then the post-sweep read of the recorded
            # child: gone (its start id does not read back).
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({5757: [_START_ID, "9999999"], 6262: None}),
            ),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            failure = await svc._sigkill_session("cron:walk", handle)

        probe.assert_called_once_with(5757)  # the walk was entered; the second read stopped the signal
        tree_kill.assert_not_awaited()
        sweep.assert_called_once_with({6262: ("222", b"node")})
        assert failure is not None and "7272" in failure, (
            "the child the walk found under the exiting leader was dropped and the kill reported "
            f"success over it: {failure!r}"
        )
        assert failure == (
            "1 child(ren) found during the walk (pid 7272) could not be attributed to the run "
            "after its leader exited; not signalled"
        )

    @pytest.mark.asyncio
    async def test_a_root_that_exits_during_a_walk_that_found_nothing_new_is_nothing_to_kill(
        self,
    ) -> None:
        """The control: no fresh child, the recorded child gone, an empty group -- nothing to name."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:walk2", 5858)
        client._child_pids = {6363: ("222", b"node")}
        handle = _handle_of(svc, "cron:walk2")

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[6363]),
            patch("kiro_crew.acp.client._capture_child_records") as capture,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({5858: [_START_ID, "9999999"], 6363: None}),
            ),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:walk2", handle) is None

        capture.assert_not_called()
        tree_kill.assert_not_awaited()
        sweep.assert_called_once_with({6363: ("222", b"node")})

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_still_kills_through_the_handle(
        self, tmp_path: object
    ) -> None:
        """``reset`` answers False when the key is already gone: it stopped nothing.

        A concurrent reset popped the entry between the reap's snapshot and the
        reset's lock. Whether that reset's shutdown lands is not this reap's to
        assume: the handle says the process is standing (its recorded start id
        reads back), so the kill goes through the handle and the record says
        reaped only once it has been signalled.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "unmapped1")
        _session_with_pid(svc, "cron:unmapped1", 5858)

        async def _already_popped(session_key: str, **_: Any) -> bool:
            svc._sessions._sessions.pop(session_key, None)
            return False

        svc._sessions.reset = AsyncMock(side_effect=_already_popped)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("unmapped1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(5858, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_session_the_run_s_own_teardown_popped_before_the_snapshot_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """The run's own finally reset pops the session BEFORE the reap looks; the kill still lands.

        The ordinary shape of a run that hangs in its teardown: the run body's
        ``finally`` resets its session, the reset pops the map entry under the
        registry lock and then hangs in the provider shutdown, and only later does
        the reaper measure the run over its deadline. Its live-map lookup misses,
        its own reset answers False for the already-popped key, and without the
        torn-down table there was no handle -- nothing to verify, ``reaped``
        recorded, while the hung reset (which this reap's cancel of the run task
        is about to interrupt) still held the process. The handle is read from
        the session the manager retains for the life of that teardown, and the
        kill goes to the pre-pop pid.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "torn1")
        _session_with_pid(svc, "cron:torn1", 6363)
        _torn_down_by_the_run(svc, "cron:torn1")
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("torn1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "cron:torn1" not in svc._sessions._sessions, "the fixture left the session live"
        group_kill.assert_called_once_with(6363, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_torn_down_session_whose_pid_the_hung_teardown_cleared_is_killed_on_the_pop_time_handle(
        self, tmp_path: object
    ) -> None:
        """The retained handle is the one captured at the pop, not a re-read of the session.

        The run's own teardown pops the session, its ACP kill fails, the client's
        reset clears the recorded pid anyway, and the teardown hangs on the
        transport -- the process still standing. A reap that re-read the retained
        session found no pid, dropped the only process under the key as "names
        nothing", and recorded ``reaped``. The table captures the handle in the
        pop's own lock hold, and the kill goes to that identity.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "torn3")
        client = _session_with_pid(svc, "cron:torn3", 6565)
        _torn_down_by_the_run(svc, "cron:torn3")
        # What the client's reset does after a kill it could not confirm.
        client._pid = None
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("torn3", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert group_kill.call_args_list == [call(6565, platform_compat.SIGKILL)], (
            "the reap discarded the live process whose pid the hung teardown had cleared "
            f"and recorded {_audited_outcome(mock_sel)} over it"
        )
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_refused_kill_of_a_session_the_run_s_own_teardown_popped_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """Same pop-before-the-snapshot shape, kill refused: ``failed``, never ``reaped``.

        Before the torn-down table this run was audited ``reaped`` with a clean
        ``last_error`` and no kill attempted at all -- the audit this PR corrects,
        on the path a hung teardown takes every time.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "torn2")
        _session_with_pid(svc, "cron:torn2", 6464)
        _torn_down_by_the_run(svc, "cron:torn2")
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=ValueError(
                    "kill_process_group: refusing broadcast/self process group 6464"
                ),
            ),
        ):
            await svc._force_reap("torn2", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")
        assert "6464" in (job.last_error or "")
        assert "torn2" not in svc._claims

    @pytest.mark.asyncio
    async def test_the_reap_reaches_the_process_of_a_teardown_the_real_manager_holds(
        self, tmp_path: object
    ) -> None:
        """End to end through ``SessionManager``: the run's reset hangs, the reap kills, the table empties.

        The run task resets its own session and hangs in the provider shutdown;
        the reaper arrives after that pop. Through the real manager the handle
        comes from the torn-down table, the kill goes to the pre-pop pid, the
        audit says ``reaped`` for a delivered kill -- and the reap's cancel of the
        run task ends the hung teardown, whose scope releases the entry: the
        table is empty once the teardown is over.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        provider, _, _ = await mgr.get_or_create("cron:real1")
        mgr.release("cron:real1")
        # Above the kernel's pid ceiling: even an unpatched probe cannot meet a
        # real process under it.
        pid = 2**22 + 6565
        client = MagicMock()
        client._pid = pid
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        hang = asyncio.Event()

        async def _hung_shutdown() -> None:
            await hang.wait()

        provider.shutdown = AsyncMock(side_effect=_hung_shutdown)

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("real1")
        svc._jobs = [job]
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
            # The run's own teardown (``SessionLifecycle.reset``), resumed by the
            # reap's cancel, signals the pid it holds through its own seams.
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as teardown_tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            # The run's own finally reset: pops the session, then hangs in the
            # provider shutdown. Reap only once it is IN the shutdown: the reap's
            # cancel of the run task must land there (deferred by ``reset`` past
            # its kill-and-sweep, then re-raised); a cancel landing one await
            # earlier, at the end-record crumb hop, is absorbed by design and the
            # teardown runs on into the hang.
            run_teardown = asyncio.create_task(mgr.reset("cron:real1"))
            try:
                for _ in range(400):
                    if not mgr.has_session("cron:real1") and provider.shutdown.await_count:
                        break
                    await asyncio.sleep(0.005)
                assert not mgr.has_session("cron:real1"), "the run's reset did not pop the session"
                assert provider.shutdown.await_count == 1, "the teardown never reached the shutdown"
                assert mgr.tearing_down("cron:real1") != []
                # The client's own reset, after a kill it could not confirm,
                # clears the recorded pid with the process still standing: the
                # reap must kill on the identity the table captured at the pop.
                client._pid = None
                claim = svc._claims["real1"] = _RunClaim(
                    trigger="scheduled",
                    claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
                    task=run_teardown,
                )

                await svc._force_reap("real1", _JOB_TIMEOUT_SECS + 60, claim=claim)

                # The reap's kill went through the torn-down handle to the pre-pop
                # pid's CAPTURED group. Its cancel of the run task
                # (``_finish_taken_claim``) lands while the reap persists its
                # record: ``reset`` defers the cancellation past its own
                # kill-and-sweep, so the resumed teardown may signal the same pid
                # once more through its own seams before re-raising -- every kill
                # here names the run's own process, none a successor's.
                assert group_kill.call_args_list == [call(pid, platform_compat.SIGKILL)]
                assert {c.args[0] for c in teardown_tree_kill.await_args_list} <= {pid}
                assert _audited_outcome(mock_sel) == "reaped"
                assert "kill failed" not in (job.last_error or "")
                assert "real1" not in svc._claims
                # The cancel ends the hung teardown, and the scope the facade
                # opened around it releases the entry.
                with pytest.raises(asyncio.CancelledError):
                    await run_teardown
            finally:
                # A failed assertion must not leave the hung teardown pending
                # past the patches (its deferred kill-and-sweep would then run
                # unstubbed): release the hang, cancel, and let it finish here.
                hang.set()
                run_teardown.cancel()
                await asyncio.gather(run_teardown, return_exceptions=True)

        assert mgr.tearing_down("cron:real1") == [], "the torn-down entry outlived its teardown"

    @pytest.mark.asyncio
    async def test_the_reap_reaches_every_teardown_the_real_manager_holds_under_the_key(
        self, tmp_path: object
    ) -> None:
        """Two hung teardowns under one key -- the run's own and a successor's -- and both processes are answered.

        The run's reset pops its session and hangs; a late completion cold-starts
        a successor under the key; the successor's own reset pops it and hangs as
        well. A table that retained the first popper alone let the reap kill the
        run's process, record ``reaped``, and leave the successor's hung process
        standing with nothing left to find it by. Every teardown in flight is
        retained, so the reap's snapshot names both handles and kills both.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        hang = asyncio.Event()

        async def _hung_shutdown() -> None:
            await hang.wait()

        async def _torn_down_and_hung(pid: int) -> tuple[Any, asyncio.Task[Any]]:
            provider, _, _ = await mgr.get_or_create("cron:real2")
            mgr.release("cron:real2")
            client = MagicMock()
            client._pid = pid
            client._child_pids = {}
            client._start_time = _START_ID
            provider._client = client
            provider.shutdown = AsyncMock(side_effect=_hung_shutdown)
            teardown = asyncio.create_task(mgr.reset("cron:real2"))
            for _ in range(400):
                if not mgr.has_session("cron:real2") and provider.shutdown.await_count:
                    break
                await asyncio.sleep(0.005)
            assert not mgr.has_session("cron:real2"), "the reset did not pop the session"
            assert provider.shutdown.await_count == 1, "the teardown never reached the shutdown"
            return provider, teardown

        run_pid, successor_pid = 2**22 + 6666, 2**22 + 6767
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("real2")
        svc._jobs = [job]
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            # The run's own finally reset pops its session and hangs; then a late
            # completion's successor is popped by ITS reset and hangs too.
            run_provider, run_teardown = await _torn_down_and_hung(run_pid)
            successor_provider, successor_teardown = await _torn_down_and_hung(successor_pid)
            try:
                claim = svc._claims["real2"] = _RunClaim(
                    trigger="scheduled",
                    claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
                    task=run_teardown,
                )

                await svc._force_reap("real2", _JOB_TIMEOUT_SECS + 60, claim=claim)

                signalled = sorted(c.args[0] for c in group_kill.call_args_list)
                assert signalled == [run_pid, successor_pid], (
                    "the successor's hung teardown was invisible to the reap: only "
                    f"{signalled} signalled"
                )
                assert _audited_outcome(mock_sel) == "reaped"
                assert "kill failed" not in (job.last_error or "")
                # The reap's cancel ends the run's teardown; the successor's is not
                # the run task and keeps its entry until it ends.
                with pytest.raises(asyncio.CancelledError):
                    await run_teardown
                assert [entry.session.provider for entry in mgr.tearing_down("cron:real2")] == [
                    successor_provider
                ]
            finally:
                hang.set()
                for teardown in (run_teardown, successor_teardown):
                    teardown.cancel()
                await asyncio.gather(run_teardown, successor_teardown, return_exceptions=True)

        assert mgr.tearing_down("cron:real2") == []

    @pytest.mark.asyncio
    async def test_a_process_that_survives_a_completed_reset_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """A reset that returned True is not proof the process is gone.

        The reset's own shutdown can fail without raising out of it. After every
        completed reset the handle is asked -- pid plus recorded start id -- and
        a process that still stands gets the fallback; its outcome, not the
        reset's boolean, is what the record says.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "survived1")
        _session_with_pid(svc, "cron:survived1", 5959)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("survived1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(5959, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_survivor_the_kill_cannot_signal_is_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        """The survivor's fallback is audited on its own outcome: refused here, so ``failed``."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "survived2")
        _session_with_pid(svc, "cron:survived2", 6060)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=ValueError(
                    "kill_process_group: refusing broadcast/self process group 6060"
                ),
            ),
        ):
            await svc._force_reap("survived2", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_process_gone_after_a_completed_reset_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: the reset did its job -- no start id and no process behind the pid, no kill."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "gone1")
        _session_with_pid(svc, "cron:gone1", 6161)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("gone1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_an_exited_pid_whose_start_id_still_reads_back_is_nothing_to_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: identity alone is not liveness -- an exited pid the platform confirms is no survivor.

        On Windows the creation time reads back through a query handle for as long
        as any handle to the exited process object is held (the transport's, until
        GC), so a just-exited child answers the recorded start id while
        ``pid_exists`` (which confirms the exit code) says gone. Without the
        liveness read every completed reset there logged a survivor and spawned
        two ``taskkill`` runs against a dead pid before recording ``reaped``.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "exited1")
        _session_with_pid(svc, "cron:exited1", 6767)
        svc._sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("exited1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_pid_recycled_after_a_completed_reset_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: a different start id behind the pid means the run's process is gone."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "recycled1")
        _session_with_pid(svc, "cron:recycled1", 6262)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("recycled1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_and_no_handle_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: no session before the reset either -- the run had no process to answer for."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "nosess1")
        svc._sessions.reset = AsyncMock(return_value=False)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("nosess1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"

    @pytest.mark.asyncio
    async def test_a_reset_that_succeeds_never_reaches_the_kill(self, tmp_path: object) -> None:
        """Control: the SIGKILL is the fallback, so a reset that returns keeps ``reaped``."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "reset1")
        svc._sessions.reset = AsyncMock()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("reset1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"


def _start_id_reads(table: dict[int, Any]) -> Any:
    """A per-pid ``get_process_start_id`` side effect.

    A list is consumed in order and its last value then repeats; anything else
    is that pid's constant answer; an unlisted pid reads ``None`` (gone).
    """
    state = {pid: (list(value) if isinstance(value, list) else value) for pid, value in table.items()}

    def _read(pid: int) -> str | None:
        value = state.get(pid)
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    return _read


def _vouching_child(client: MagicMock, cpid: int, start: str = "5150001") -> int:
    """Record ``cpid`` on the client as a descendant whose identity a test can make read back."""
    client._child_pids = {cpid: (start, b"node")}
    return cpid


def _child_helpers() -> Any:
    """The client's child helpers, resolved the way cron resolves them (so patches apply)."""
    from kiro_crew.session import child_process_helpers

    return child_process_helpers()


class TestALeaderlessGroupIsNeverReaped:
    """A dead group leader is not an empty process group.

    A late child spawn followed by the leader's exit leaves members under the
    group id while every pid-addressed lookup of the leader fails, and a kill
    that resolved the group from the pid -- or read the leader's absence as the
    group's -- audited ``reaped`` over processes still running. The handle keeps
    the group id captured while the leader was alive and identity-checked
    (``ProcessHandle.pgid``), and that captured id is the ONLY group the kill
    ever signals (``platform_compat.kill_process_group``, alive leader or gone):
    once the leader is gone the group decides -- members are signalled by the
    captured id once a member verified as this run's vouches for it,
    ``ProcessLookupError`` from that signal means "empty" only for a verified
    group, and a live group that cannot be verified as this run's is a kill
    failure. The POSIX branch is exercised on EVERY platform the suite runs on:
    ``IS_WINDOWS`` is False (the module's autouse kill seam), and the two group
    primitives the capture site reads through ``getattr`` -- ``os.getpgid``,
    supplied per test, and ``os.getpgrp`` -- are patched into existence where the
    runner lacks them, so no shard skips these assertions. The Windows kill
    branch has its own expectations in ``TestReaperRecordsAFailedSigkill`` --
    nothing sweeps descendants there.
    """

    @pytest.fixture(autouse=True)
    def _posix_group_primitives(self) -> Iterator[None]:
        """``os.getpgrp`` exists (our own group is 1) on every runner; each test supplies ``os.getpgid``."""
        with patch("os.getpgrp", return_value=1, create=True):
            yield

    @pytest.mark.asyncio
    async def test_a_leader_that_exits_after_a_late_child_spawn_is_a_failed_kill_not_reaped(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leader gone, group alive and vouched, group signal refused (EPERM): a failure, not ``reaped`` -- and no pid fallback for a gone leader."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "leaderless1")
        client = _session_with_pid(svc, "cron:leaderless1", 7373)
        child = _vouching_child(client, 8373)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # Alive at the snapshot (the bracketed group read), exited by the kill;
            # the late-spawned recorded child still reads back in the group.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7373: [_START_ID, _START_ID, None], child: "5150001"}),
            ),
            patch("os.getpgid", return_value=7373, create=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=7373),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ) as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("leaderless1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(7373, platform_compat.SIGKILL)
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says the run was reaped while its leaderless process group is still running"
        assert "; kill failed: PermissionError: " in (job.last_error or "")
        pid_kill.assert_not_awaited()  # a gone leader's pid may be another process's by now
        assert "leaderless1" not in svc._claims

    @pytest.mark.asyncio
    async def test_a_live_group_is_signalled_by_the_group_id_retained_while_the_leader_lived(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leader's pid was recycled before the kill; the recorded child still in the group vouches for it; the CAPTURED id is signalled -- nothing is resolved from the recycled pid."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "leaderless2")
        client = _session_with_pid(svc, "cron:leaderless2", 7474)
        child = _vouching_child(client, 8474)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # The leader's start id reads back at the snapshot (the bracketed group
            # read), then another process owns the pid; the recorded child's reads
            # back throughout.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads(
                    {7474: [_START_ID, _START_ID, "9999999"], child: "5150001"}
                ),
            ),
            # ``getpgid`` answers the pid at the snapshot; by the kill the number
            # belongs to a stranger whose group is a different one -- a kill that
            # resolved the group from the pid now would signal THAT group.
            patch("os.getpgid", side_effect=[7474, 7474, 31337, 31337, 31337], create=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=7474),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            # After the delivered group kill the vouching child sits in the zombie
            # state (its start id still reads back): a finished process, not a
            # survivor of the sweep -- pinned, since the post-sweep recheck reads
            # the child's liveness and a made-up pid answers whatever the host runs.
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("leaderless2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert group_kill.call_args_list == [call(7474, platform_compat.SIGKILL)], (
            "the group kill did not address the group id captured with the handle "
            f"(signalled {[c.args[0] for c in group_kill.call_args_list]}; a group resolved "
            "from the recycled pid would be 31337, a stranger's)"
        )
        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_live_group_whose_id_was_not_verified_is_a_failed_kill_not_reaped(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No pgid captured while the leader lived, leader gone: the group cannot be told from a stranger's -- not signalled."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "leaderless3")
        _session_with_pid(svc, "cron:leaderless3", 7575)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # Unreadable at snapshot time: no verified group id on the handle. By
            # the kill the leader has exited.
            patch("os.getpgid", side_effect=ProcessLookupError("[Errno 3] No such process"), create=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("leaderless3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_not_called()
        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert (
            "; kill failed: pid 7575 exited but its process group 7575 still has members and "
            "could not be verified as this run's; not signalled"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_live_group_no_verified_member_vouches_for_is_not_signalled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a late, unrecorded spawn keeps the group alive: nothing verified is in it -- reported, not signalled.

        Once every verified member is gone the number is free for another tree
        (a stranger that took the leader's old pid, ``setsid``-ed, spawned and
        exited leaves a leaderless group under the same number), so a group the
        run cannot prove is its own is never signalled.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:held", 7676)
        # The one recorded child has exited (its start id does not read back).
        child = _vouching_child(client, 8676)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("os.getpgid", return_value=7676, create=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=7676),
            # Snapshot bracket (2), then the leader is gone; the child gone too.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7676: [_START_ID, _START_ID, None], child: None}),
            ),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            handle = _handle_of(svc, "cron:held")
            assert handle.pgid == 7676, "the snapshot did not verify the group id"
            failure = await svc._sigkill_session("cron:held", handle)

        group_kill.assert_not_called()
        assert failure == (
            "pid 7676 exited and its process group 7676 still has members, but none could be "
            "verified as this run's; not signalled"
        )

    def test_on_macos_the_root_s_zombie_vouches_through_the_kernel_group_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``getpgid`` refuses a darwin zombie; the group listing matched on (pid, start id) stands in."""
        from types import SimpleNamespace

        from kiro_crew.process_identity import ProcessHandle, _group_member_vouches

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(sys, "platform", "darwin")
        pid = _UNALLOCATABLE_PID
        handle = ProcessHandle(pid=pid, start_id=_START_ID, pgid=pid, child_pids={})

        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=None),
            patch(
                "kiro_crew.platform_compat.darwin_pgroup_members",
                return_value=[SimpleNamespace(pid=pid, start_id=_START_ID)],
            ) as listing,
        ):
            assert _group_member_vouches(handle) is True
        listing.assert_called_once_with(pid)

        # A listed member under a recycled number does not pass: the start id differs.
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=None),
            patch(
                "kiro_crew.platform_compat.darwin_pgroup_members",
                return_value=[SimpleNamespace(pid=pid, start_id="9999999")],
            ),
        ):
            assert _group_member_vouches(handle) is False

    @pytest.mark.asyncio
    async def test_an_empty_group_after_the_leader_exit_is_nothing_to_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the leader took its whole group with it -- ``reaped``, nothing signalled."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "emptied1")
        _session_with_pid(svc, "cron:emptied1", 7777)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # Alive at the snapshot, gone by the kill; the captured group probes empty.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7777: [_START_ID, _START_ID, None]}),
            ),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("os.getpgid", return_value=7777, create=True),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await svc._force_reap("emptied1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_leader_gone_after_a_completed_reset_with_a_live_group_still_gets_the_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``process_survived`` asks a gone leader's group before calling it gone."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "survivors1")
        client = _session_with_pid(svc, "cron:survivors1", 7878)
        child = _vouching_child(client, 8878)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # Alive at the snapshot (the bracketed group read), gone ever after;
            # the recorded child reads back throughout.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7878: [_START_ID, _START_ID, None], child: "5150001"}),
            ),
            patch("os.getpgid", return_value=7878, create=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=7878),
            # The leader is gone; the recorded child is a zombie still in the group
            # -- it vouches for the group (a zombie reads its group) and is not a
            # running survivor. Both liveness reads pinned per pid.
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=True),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await svc._force_reap("survivors1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        pid_kill.assert_not_awaited()
        group_kill.assert_called_once_with(7878, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"


class TestARecordedChildTheSweepLeftStandingIsNamed:
    """A recorded child the escaped-children sweep left running is a kill failure, not a reap.

    The client's sweep signals a recorded descendant only once it is verified by
    start id AND basename, and reports nothing: a child that kept its pid and
    start id but ``exec``'d into another name is refused by that guard and stays
    alive with no return path, so a kill that returned the group signal's outcome
    alone recorded ``reaped`` over it. After the sweep every recorded child whose
    start id reads back and that is still running -- not a zombie -- is named
    (``process_identity._sweep_children``), reported, not signalled: the guard
    refused it, and this record does not overrule the guard. The liveness reads
    (``pid_exists`` / ``pid_is_zombie``) are pinned everywhere: a made-up pid
    answers whatever the host runs otherwise.
    """

    @staticmethod
    def _fixture(tmp_path: object, job_id: str, pid: int, child: int) -> tuple[CronService, CronJob, _RunClaim]:
        svc, job, claim = _overdue_reap_fixture(tmp_path, job_id)
        client = _session_with_pid(svc, f"cron:{job_id}", pid)
        _vouching_child(client, child)
        return svc, job, claim

    @staticmethod
    @contextmanager
    def _reads(pid: int, child: int, child_start: str | None = "5150001") -> Iterator[None]:
        """The root reads back throughout and leads its own group; the child's start id as given."""
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({pid: _START_ID, child: child_start}),
            ),
            _isolated_leader(),
        ):
            yield

    @pytest.mark.asyncio
    async def test_a_child_that_exec_d_into_another_name_survives_the_sweep_as_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """Group kill delivered, sweep ran and refused the child (basename), child still running: ``failed``."""
        svc, job, claim = self._fixture(tmp_path, "execd1", 7171, 8171)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            self._reads(7171, 8171),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.process_identity._SWEEP_SETTLE_SECS", 0, create=True),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("execd1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(7171, platform_compat.SIGKILL)
        sweep.assert_called_once_with({8171: ("5150001", b"node")})
        pid_kill.assert_not_awaited()  # reported, not signalled: the client's guard refused it
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says the run was reaped while a recorded child it verified is still running"
        assert (
            "; kill failed: recorded child pid 8171 survived the escaped-children sweep, identity "
            "verified; not signalled"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_child_the_sweep_signalled_is_a_zombie_not_a_survivor(
        self, tmp_path: object
    ) -> None:
        """Control: the child's start id still reads back but it sits in the zombie state -- finished, ``reaped``."""
        svc, job, claim = self._fixture(tmp_path, "execd2", 7272, 8272)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            self._reads(7272, 8272),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=True),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True),
        ):
            await svc._force_reap("execd2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_child_that_settles_after_the_signal_is_not_a_survivor(
        self, tmp_path: object
    ) -> None:
        """SIGKILL lands asynchronously: a child still running on the first read and a zombie on the next is settled."""
        svc, job, claim = self._fixture(tmp_path, "execd3", 7373, 8373)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            self._reads(7373, 8373),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", side_effect=[False, True]) as zombie,
            _group_gone(),
            patch("kiro_crew.process_identity._SWEEP_SETTLE_SECS", 0, create=True),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True),
        ):
            await svc._force_reap("execd3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert zombie.call_count == 2, "the post-sweep read was not retried"
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_child_whose_start_id_does_not_read_back_is_not_the_run_s(
        self, tmp_path: object
    ) -> None:
        """Control: a recorded child gone or recycled (start id differs) is another process's now -- ``reaped``."""
        svc, job, claim = self._fixture(tmp_path, "execd4", 7474, 8474)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            self._reads(7474, 8474, child_start="9999999"),
            # Alive and running under the recorded pid -- but not the process the
            # client recorded, so nothing this run answers for.
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True),
        ):
            await svc._force_reap("execd4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_surviving_child_is_named_beside_the_group_kill_s_own_failure(self) -> None:
        """Both failures ride one record: the refused group kill first, the surviving child after it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:both", 7575)
        _vouching_child(client, 8575)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            self._reads(7575, 8575),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.process_identity._SWEEP_SETTLE_SECS", 0, create=True),
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
        ):
            failure = await svc._sigkill_session("cron:both", _handle_of(svc, "cron:both"))

        assert failure == (
            "PermissionError: [Errno 1] Operation not permitted; recorded child pid 8575 survived "
            "the escaped-children sweep, identity verified; not signalled"
        )


class TestEveryProcessUnderTheKeyIsAnswered:
    """A live successor under the key does not hide the run's torn-down process, or the reverse.

    The run's own teardown reset pops session A and stalls in an await; a
    completion cold-starts session B under the same key. The reap reads BOTH
    handles (the torn-down one first), resets the key -- which is B's teardown
    -- and then verifies and kills every candidate on its own handle. Before,
    the live session won the snapshot, so the reap killed B and its cancel
    abandoned A mid-hung-shutdown while the audit recorded ``reaped``.
    """

    def _two_sessions(self, svc: CronService, key: str, run_pid: int, successor_pid: int) -> None:
        _session_with_pid(svc, key, run_pid)
        _torn_down_by_the_run(svc, key)
        _session_with_pid(svc, key, successor_pid)  # the live successor

    def test_both_are_candidates_the_torn_down_one_first(self) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        self._two_sessions(svc, "cron:both", 7979, 8979)

        handles = svc._session_process_handles("cron:both")

        assert [h.pid for h in handles] == [7979, 8979], "the torn-down run process is not first"

    def test_a_successor_under_a_recycled_pid_is_its_own_handle(self) -> None:
        """The dedup key is the handle -- ``(pid, start id)`` -- never the pid.

        The run's torn-down process exited and the kernel handed its number to
        the successor a late cold start registered under the key. A snapshot that
        dropped the successor as "the same pid" would kill nothing (the dead
        predecessor) and record ``reaped`` over the successor's tree.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:recycled-succ", 8282)
        _torn_down_by_the_run(svc, "cron:recycled-succ")
        successor = _session_with_pid(svc, "cron:recycled-succ", 8282)
        successor._start_time = "7777777"

        handles = svc._session_process_handles("cron:recycled-succ")

        assert [(h.pid, h.start_id) for h in handles] == [(8282, _START_ID), (8282, "7777777")], (
            "the successor under the recycled pid was dropped as a duplicate of the dead "
            "predecessor's handle"
        )
        # And the same incarnation named twice -- the torn-down table and the live
        # map holding the one session -- is one handle.
        same_svc = CronService(base_dir=None, on_job=AsyncMock())
        same_svc._sessions = _mock_sessions()
        _session_with_pid(same_svc, "cron:same", 8383)
        same_session = same_svc._sessions._sessions["cron:same"]
        _retain_torn_down(same_svc, "cron:same", same_session)
        assert len(same_svc._session_process_handles("cron:same")) == 1

    @pytest.mark.asyncio
    async def test_a_successor_under_a_recycled_pid_is_killed_on_its_own_handle(
        self, tmp_path: object
    ) -> None:
        """Red-first for the pid-keyed dedup: the successor's group is signalled; the predecessor's recycled pid is not."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "recycled-both")
        _session_with_pid(svc, "cron:recycled-both", 8484)
        _torn_down_by_the_run(svc, "cron:recycled-both")
        successor = _session_with_pid(svc, "cron:recycled-both", 8484)
        successor._start_time = "7777777"
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # The pid now reads the successor's start id: the predecessor is
            # recycled (nothing to kill, its group probes empty), the successor is
            # alive and verified.
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="7777777"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            _isolated_leader(),
            _group_gone(),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("recycled-both", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert group_kill.call_args_list == [call(8484, platform_compat.SIGKILL)], (
            "the successor under the recycled pid was dropped as a duplicate of the dead "
            "predecessor's handle: no kill reached it"
        )
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_run_s_torn_down_process_and_the_live_successor_are_both_killed(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "both1")
        self._two_sessions(svc, "cron:both1", 8080, 9080)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("both1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [8080, 9080], (
            "the run's own (torn-down) process must be killed, and the successor beside it"
        )
        assert _audited_outcome(mock_sel) == "reaped"

    @pytest.mark.asyncio
    async def test_a_refused_kill_of_the_torn_down_process_is_a_failed_kill_whatever_the_successor(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "both2")
        self._two_sessions(svc, "cron:both2", 8181, 9181)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        def _refuse_the_run_s_process(pgid: int, _sig: int) -> bool:
            if pgid == 8181:
                raise ValueError("kill_process_group: refusing broadcast/self process group 8181")
            return True

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=_refuse_the_run_s_process,
            ),
        ):
            await svc._force_reap("both2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")
        assert "8181" in (job.last_error or "")


class TestTheSessionTheResetPopsIsCaptured:
    """A registration that lands after the snapshot is what the reset pops -- and what the fallback must kill.

    The pre-reset snapshot is keyed by name. A cold start can register a new
    session under the key between that snapshot and the reset's pop; the reset
    pops THAT session and hangs on it, and a fallback fed the snapshot alone finds
    nothing to kill and records ``reaped`` over a live process. The reset now runs
    under a scope whose ``on_pop`` hook takes the popped session's handle in the
    same lock hold as the pop, and the fallback kills it; a popped session with
    no recorded pid is a named kill failure, never ``reaped``.
    """

    @staticmethod
    def _registering_reset(svc: CronService, key: str, pid: int | None) -> Any:
        """A reset that cold-starts a session under ``key`` after the snapshot, pops it, then hangs."""

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            assert session_key == key
            client = _session_with_pid(svc, key, pid)
            session = svc._sessions._sessions.pop(key)
            scope = kwargs.get("scope")
            if scope is not None:
                scope.note_pop(key, session)
            del client
            raise asyncio.TimeoutError

        return _reset

    @pytest.mark.asyncio
    async def test_a_session_registered_after_the_snapshot_is_killed_through_the_pop(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late1")
        assert svc._session_process_handles("cron:late1") == [], "the snapshot must see nothing"
        svc._sessions.reset = AsyncMock(side_effect=self._registering_reset(svc, "cron:late1", 8787))
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("late1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(8787, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_popped_session_with_no_process_handle_yet_is_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        """The cold start had not spawned yet: nothing names its process, so nothing can verify it."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late2")
        svc._sessions.reset = AsyncMock(side_effect=self._registering_reset(svc, "cron:late2", None))

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("late2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says the run was reaped while the session the reset popped names no process"
        assert (
            "; kill failed: the session the reset popped had no process handle yet; not signalled"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_popped_handle_joins_the_snapshot_not_replaces_it(self, tmp_path: object) -> None:
        """A torn-down run process seen at the snapshot is still killed beside the popped successor."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late3")
        _session_with_pid(svc, "cron:late3", 8888)
        _torn_down_by_the_run(svc, "cron:late3")
        svc._sessions.reset = AsyncMock(side_effect=self._registering_reset(svc, "cron:late3", 9888))
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("late3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [8888, 9888]
        assert _audited_outcome(mock_sel) == "reaped"

    def test_the_kill_set_is_keyed_by_the_handle_not_the_pid(self) -> None:
        """A popped session under a snapshot handle's pid but another start id is a second process; the same incarnation is one."""
        snapshot = ProcessHandle(pid=8989, start_id=_START_ID, pgid=8989, child_pids={})
        recycled = ProcessHandle(pid=8989, start_id="7777777", pgid=8989, child_pids={})
        same = ProcessHandle(pid=8989, start_id=_START_ID, pgid=None, child_pids={1: ("x", b"n")})

        targets, missing = CronService._kill_set([snapshot], [(object(), recycled), (object(), same)])

        assert missing is None
        assert targets == [snapshot, recycled], (
            "the popped session under the recycled pid was dropped as a duplicate of the "
            "snapshot handle"
        )
        assert snapshot == same and hash(snapshot) == hash(same), (
            "the handle's identity is (pid, start id); group id and child records are not part of it"
        )
        assert snapshot != recycled

    @pytest.mark.asyncio
    async def test_through_the_real_manager_the_popped_session_s_process_is_killed(
        self, tmp_path: object
    ) -> None:
        """End to end: a cold start attempted inside the reap's reset call is held at the door -- the fence, not the pop capture, answers it -- and lands after the record.

        Through the real manager the pop capture is the net under the fence: a
        cold start under the key between the reap's snapshot and the reset's pop
        (the late completion's ``get_or_create``) never registers under the run,
        because ``_end_run_processes`` raised the key's ending fence before the
        snapshot. Nothing lands during the passes, nothing is killed, the record
        is ``reaped`` over an empty key -- and the held call is not dropped: it
        lands once the fence lifts, a new life under the recorded key.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            provider.shutdown = AsyncMock()
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        real_reset = mgr.reset
        racing: list[asyncio.Task[Any]] = []
        held_during_reset: list[bool] = []

        async def _cold_start_then_reset(key: str, **kwargs: Any) -> bool:
            # The late completion's cold start, attempted after the reap's
            # snapshot and before the pop: held at the door while the fence is up.
            racing.append(asyncio.create_task(mgr.get_or_create(key)))
            for _ in range(20):
                await asyncio.sleep(0)
            held_during_reset.append(not racing[0].done())
            return await real_reset(key, **kwargs)

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("late4")
        svc._jobs = [job]
        claim = svc._claims["late4"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(mgr, "reset", side_effect=_cold_start_then_reset),
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            assert svc._session_process_handles("cron:late4") == []
            await svc._force_reap("late4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert held_during_reset == [True], (
            "the cold start attempted between the snapshot and the pop was answered under the "
            f"fenced key: {racing[0].exception() if racing[0].done() and racing[0].exception() else 'it registered'}"
        )
        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")
        # The fence lifted with the passes: the held call lands -- the completion
        # racing the reap is delivered after the record, not dropped.
        _, is_new, _ = await asyncio.wait_for(racing[0], timeout=5)
        assert is_new and mgr.has_session("cron:late4")
        mgr.release("cron:late4")
        await real_reset("cron:late4")


class TestARegistrationAfterThePopIsNamedNotChased:
    """A session registered under the key AFTER the reset's pop is a named kill failure; there is no second pass.

    The snapshot and the pop capture together name every session the key held up
    to the pop, and the fence is why nothing lands under it afterwards: every
    door into the live map either meets the fence -- ``get_or_create`` at its
    front door, after its busy-turn wait and at registration, cold start and
    warm-pool claim alike -- or never publishes under a cron key
    (``open_task_session`` publishes ``taskrunner:`` keys and refuses a key being
    ended; the background session publishes its own key). So a reset-and-kill
    pass is run ONCE per key, and the post-pass read is a net that names, not a
    chase: a session under the key the pass did not handle -- a manager without
    the fence, or a door this reasoning missed -- is reported as a kill failure,
    audited ``failed``, never reset, never signalled, and a session the pass
    already answered is never reported.
    """

    @staticmethod
    def _late_registering_reset(svc: CronService, key: str, late_pids: list[int]) -> Any:
        """A reset that pops the session under ``key`` and then cold-starts the next of ``late_pids`` under it.

        The registration lands AFTER the pop -- a door without the fence -- so
        neither the snapshot nor the pop capture of the pass names it. Completes
        (``True``) every time.
        """
        remaining = list(late_pids)

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            assert session_key == key
            session = svc._sessions._sessions.pop(key, None)
            scope = kwargs.get("scope")
            if session is not None and scope is not None:
                scope.note_pop(key, session)
            if remaining:
                _session_with_pid(svc, key, remaining.pop(0))
            return True

        return _reset

    @pytest.mark.asyncio
    async def test_a_session_registered_after_the_pop_is_a_failed_kill_not_reaped_and_not_chased(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "chase1")
        _session_with_pid(svc, "cron:chase1", 8787)
        svc._sessions.reset = AsyncMock(
            side_effect=self._late_registering_reset(svc, "cron:chase1", [9797])
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("chase1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [
            8787
        ], "a session that landed under the key past the fence was reset and signalled: a second pass ran"
        assert svc._sessions.reset.await_count == 1, "the key was reset more than once"
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says the run was reaped while a session registered after the pop still runs"
        assert (
            "; kill failed: a session registered under the key after the reset's pop (pid 9797); "
            "not reset, not signalled"
        ) in (job.last_error or "")
        assert 9797 in {s.provider._client._pid for s in svc._sessions._sessions.values()}

    @pytest.mark.asyncio
    async def test_a_session_the_pass_already_answered_is_not_reported(self, tmp_path: object) -> None:
        """The run's own torn-down session still sits in the table after the pass: handled, so one reset and no failure."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "chase3")
        _session_with_pid(svc, "cron:chase3", 8888)
        _torn_down_by_the_run(svc, "cron:chase3")
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("chase3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(8888, platform_compat.SIGKILL)
        assert svc._sessions.reset.await_count == 1
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_cold_start_after_the_pop_is_held_by_the_fence(
        self, tmp_path: object
    ) -> None:
        """End to end: the first reset completes and the late cold start after its pop is held by the fence -- nothing lands, nothing is dropped.

        Through the real manager the fence is why one pass is enough: the
        completion's ``get_or_create`` after the pop meets the key's ending fence
        at the door and waits there, so nothing lands after the pop for a
        post-pass read to name; it lands once the fence lifts, under a key whose
        run is recorded.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            provider.shutdown = AsyncMock()
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        first_pid = 2**22 + 9090

        provider, _, _ = await mgr.get_or_create("cron:chase4")
        mgr.release("cron:chase4")
        client = MagicMock()
        client._pid = first_pid
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        real_reset = mgr.reset
        resets = 0
        racing: list[asyncio.Task[Any]] = []
        held_after_pop: list[bool] = []

        async def _reset_then_late_cold_start(key: str, **kwargs: Any) -> bool:
            nonlocal resets
            resets += 1
            result = await real_reset(key, **kwargs)
            if resets == 1:
                # The late completion's cold start after the pop: held at the door.
                racing.append(asyncio.create_task(mgr.get_or_create(key)))
                for _ in range(20):
                    await asyncio.sleep(0)
                held_after_pop.append(not racing[0].done())
            return result

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("chase4")
        svc._jobs = [job]
        claim = svc._claims["chase4"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(mgr, "reset", side_effect=_reset_then_late_cold_start),
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            # The first process is gone once its reset completed: no survivor.
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("chase4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert held_after_pop == [True], (
            "the late cold start after the pop was answered under the fenced key: "
            f"{racing[0].exception() if racing[0].done() and racing[0].exception() else 'it registered'}"
        )
        assert resets == 1, "the key was reset more than once although nothing landed after the pop"
        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")
        # The held call lands after the record: the completion is delivered, not dropped.
        _, is_new, _ = await asyncio.wait_for(racing[0], timeout=5)
        assert is_new and mgr.has_session("cron:chase4")
        mgr.release("cron:chase4")
        await real_reset("cron:chase4")


class TestEveryKeyOfTheRunIsEnded:
    """A reap or cancel ends EVERY key the run registered, not the newest one.

    A sequential job registers one key per agent inside one run, and an earlier
    agent's session is kept alive -- its reset deferred -- while its sub-agents
    are pending and a later agent runs. A reap that ended only the newest key
    reset the hung later agent, recorded ``reaped``, and left the earlier agent's
    session and its sub-agents running behind that record. Each registered key
    is attributed to the run that registered it (``register_active_session_key``
    reads the job's stored claim); the reap and cancel end every key of the run
    they took (``_run_session_keys``, newest first), each fenced through the
    record, and leave an older run's key -- a finished run's session retained
    for its pending sub-agents -- alone. A key the run registers while its
    others are being ended (its task is cancelled only after the passes) is
    ended by a bounded second round; one that lands after the last round is a
    named kill failure, so the audit says ``failed``.
    """

    @staticmethod
    def _registers_next_key_on_reset(
        svc: CronService, job_id: str, next_keys: list[tuple[str, int]]
    ) -> Any:
        """A reset that pops the session under the key and then registers the run's next agent key with a live session.

        The shape of a sequential run whose hung agent's stream ends when its
        session is reset: the run's task, not yet cancelled, moves on to the next
        agent and registers that key while the reap is still ending the others.
        """
        remaining = list(next_keys)

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            session = svc._sessions._sessions.pop(session_key, None)
            scope = kwargs.get("scope")
            if session is not None and scope is not None:
                scope.note_pop(session_key, session)
            if remaining:
                key, pid = remaining.pop(0)
                svc.register_active_session_key(job_id, key)
                _session_with_pid(svc, key, pid)
            return True

        return _reset

    @pytest.mark.asyncio
    async def test_a_sequential_run_s_deferred_earlier_agent_is_ended_beside_the_hung_newest(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "seq1")
        svc.register_active_session_key("seq1", "cron:seq1:agentA")
        _session_with_pid(svc, "cron:seq1:agentA", 7001)
        svc.register_active_session_key("seq1", "cron:seq1:agentB")
        _session_with_pid(svc, "cron:seq1:agentB", 7002)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("seq1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [7002, 7001], (
            "the reap ended only the newest agent's key and recorded reaped while the earlier "
            "agent's session, kept alive for its pending sub-agents, still runs"
        )
        assert [called.args[0] for called in svc._sessions.reset.await_args_list] == [
            "cron:seq1:agentB",
            "cron:seq1:agentA",
        ]
        assert _audited_outcome(mock_sel) == "reaped"
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["session_key"] == "cron:seq1:agentB"
        assert audit["metadata"]["session_keys"] == ["cron:seq1:agentB", "cron:seq1:agentA"]
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_an_older_run_s_key_kept_alive_for_its_subagents_is_not_ended(
        self, tmp_path: object
    ) -> None:
        """The run's reap is not the job's: a finished run's session lives on for its pending sub-agents."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "older1")
        finished = _RunClaim(trigger="scheduled", claimed_at=time.time() - 3600)
        svc._claims["older1"] = finished
        svc.register_active_session_key("older1", "cron:older1:finished")
        _session_with_pid(svc, "cron:older1:finished", 7010)
        svc._claims["older1"] = claim
        svc.register_active_session_key("older1", "cron:older1:current")
        _session_with_pid(svc, "cron:older1:current", 7011)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("older1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [7011]
        svc._sessions.reset.assert_awaited_once()
        assert svc._sessions.reset.await_args.args[0] == "cron:older1:current"
        assert "cron:older1:finished" in svc._sessions._sessions, (
            "the reap of the current run ended the finished run's session"
        )
        assert _audited_outcome(mock_sel) == "reaped"
        assert svc.active_session_keys() == frozenset({"cron:older1:finished"}), (
            "the key the reap answered was not retired, or the finished run's key was"
        )

    @pytest.mark.asyncio
    async def test_a_key_the_run_registers_while_its_others_are_ended_is_ended_by_a_second_round(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late1")
        svc.register_active_session_key("late1", "cron:late1:agentA")
        _session_with_pid(svc, "cron:late1:agentA", 7020)
        svc._sessions.reset = AsyncMock(
            side_effect=self._registers_next_key_on_reset(
                svc, "late1", [("cron:late1:agentB", 7021)]
            )
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("late1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [7020, 7021], (
            "the key the run registered while its first key was being ended was not ended"
        )
        assert _audited_outcome(mock_sel) == "reaped"
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_keys"] == ["cron:late1:agentA", "cron:late1:agentB"]
        assert svc._sessions._sessions == {}
        assert svc.active_session_keys() == frozenset()

    @pytest.mark.asyncio
    async def test_a_key_registered_after_the_last_round_is_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late2")
        svc.register_active_session_key("late2", "cron:late2:agentA")
        _session_with_pid(svc, "cron:late2:agentA", 7030)
        svc._sessions.reset = AsyncMock(
            side_effect=self._registers_next_key_on_reset(
                svc, "late2", [("cron:late2:agentB", 7031), ("cron:late2:agentC", 7032)]
            )
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("late2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [7030, 7031]
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says reaped while a key the run registered after the last round still runs"
        assert (
            "; kill failed: the run registered cron:late2:agentC after 2 rounds of resets; "
            "not reset, not signalled"
        ) in (job.last_error or "")
        assert "cron:late2:agentC" in svc._sessions._sessions
        assert svc.active_session_keys() == frozenset({"cron:late2:agentC"}), (
            "a key the reap did not end must stay registered as live"
        )

    @pytest.mark.asyncio
    async def test_the_run_s_keys_are_ended_together_not_one_after_another(
        self, tmp_path: object
    ) -> None:
        """The fences are up for one key's bound, not the sum of every key's.

        Each key's ending is bounded on its own (the reset timeout, the verified
        kill); ended one after another, a run of many agent keys holds its fences
        for the SUM of those bounds -- past ``ENDING_FENCE_WAIT_SECS``, so a
        completion held at the door is refused for a teardown that was merely
        long. Pinned by a handshake: the newest key's reset (ended first in a
        serial order) waits until the OLDER key's reset has started; a serial
        ending never starts it and hangs, a concurrent one completes.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "par1")
        svc.register_active_session_key("par1", "cron:par1:agentA")
        _session_with_pid(svc, "cron:par1:agentA", 7050)
        svc.register_active_session_key("par1", "cron:par1:agentB")
        _session_with_pid(svc, "cron:par1:agentB", 7051)
        older_started = asyncio.Event()

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            if session_key == "cron:par1:agentA":
                older_started.set()
            else:
                # The newest key's reset outlasts the older key's start.
                await older_started.wait()
            session = svc._sessions._sessions.pop(session_key, None)
            scope = kwargs.get("scope")
            if session is not None and scope is not None:
                scope.note_pop(session_key, session)
            return True

        svc._sessions.reset = AsyncMock(side_effect=_reset)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            try:
                await asyncio.wait_for(
                    svc._force_reap("par1", _JOB_TIMEOUT_SECS + 60, claim=claim), timeout=5
                )
            except asyncio.TimeoutError:
                pytest.fail(
                    "the run's keys were ended one after another: the older key's reset never "
                    "started while the newest key's was in flight, so the fences are up for the "
                    "sum of every key's bound"
                )

        assert sorted(called.args[0] for called in group_kill.call_args_list) == [7050, 7051]
        assert _audited_outcome(mock_sel) == "reaped"
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_keys"] == ["cron:par1:agentB", "cron:par1:agentA"]

    @pytest.mark.asyncio
    async def test_a_key_whose_kill_failed_stays_registered(self, tmp_path: object) -> None:
        """Only a key every session of which was answered is retired from the registry."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "keep1")
        svc.register_active_session_key("keep1", "cron:keep1:agentA")
        _session_with_pid(svc, "cron:keep1:agentA", 7040)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=ValueError(
                    "kill_process_group: refusing broadcast/self process group 7040"
                ),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            await svc._force_reap("keep1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert svc.active_session_keys() == frozenset({"cron:keep1:agentA"})
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_keys"] == [], (
            "the audit lists a key as ended whose kill was refused and which is still registered: "
            f"{audit['metadata']['session_keys']}"
        )

    @pytest.mark.asyncio
    async def test_a_refused_key_is_not_listed_as_ended_beside_a_key_that_was(
        self, tmp_path: object
    ) -> None:
        """Two keys, one kill refused: ``session_keys`` names the ended one only, the refused one stays registered and is not chased as a late registration."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "mixed1")
        svc.register_active_session_key("mixed1", "cron:mixed1:agentA")
        _session_with_pid(svc, "cron:mixed1:agentA", 7051)
        svc.register_active_session_key("mixed1", "cron:mixed1:agentB")
        _session_with_pid(svc, "cron:mixed1:agentB", 7052)
        children, start_id, sweep = _kill_path_stubs()

        def _refuse_agent_a(pgid: int, sig: int) -> bool:
            if pgid == 7051:
                raise ValueError("kill_process_group: refusing broadcast/self process group 7051")
            return True

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", side_effect=_refuse_agent_a),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            await svc._force_reap("mixed1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_keys"] == ["cron:mixed1:agentB"], (
            "the audit's ended keys do not match what was ended: "
            f"{audit['metadata']['session_keys']}"
        )
        assert svc.active_session_keys() == frozenset({"cron:mixed1:agentA"})
        assert [called.args[0] for called in svc._sessions.reset.await_args_list] == [
            "cron:mixed1:agentB",
            "cron:mixed1:agentA",
        ], "the refused key was ended a second time as a late registration"
        assert "registered cron:mixed1:agentA after" not in (job.last_error or ""), (
            "the still-registered refused key was reported as a registration the run made "
            f"while it was being ended: {job.last_error}"
        )


class TestAColdStartInFlightWhenTheRunEndsIsFenced:
    """The case no pass can see: a cold start inside ``provider.start()`` when the run is ended.

    ``get_or_create`` takes its allocation reservation, then awaits
    ``provider.start()`` BEFORE publishing into the map. A reap or cancel that
    runs then finds nothing under the key -- no snapshot, no pop, no post-pass
    read can name it -- and without the fence recorded ``reaped`` while that
    provider published afterwards, its process and the completion's injection
    running on. ``_end_run_processes`` now raises the key's ending fence
    (``SessionManager.ending_key``) before its first snapshot: the in-flight
    reservation is invalidated, so the registration is refused when the start
    returns (fence up or lifted) and the started provider is hard-killed by the
    closing manager's own path; the passes report the allocation still in flight
    as a named kill failure, so the audit says ``failed``. The completion's call
    is not dropped: refused there, it waits for the lift and allocates again, a
    new life under the recorded key.
    """

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_start_in_flight_is_named_refused_hard_killed_and_lands_after(
        self, tmp_path: object
    ) -> None:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        inside_start = asyncio.Event()
        gate = asyncio.Event()
        started: list[Any] = []

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            provider.shutdown = AsyncMock()

            async def _gated_start(*_args: Any, **_kwargs: Any) -> None:
                started.append(provider)
                inside_start.set()
                await gate.wait()

            provider.start = AsyncMock(side_effect=_gated_start)
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        cold_start = asyncio.create_task(mgr.get_or_create("cron:fence1"))
        await asyncio.wait_for(inside_start.wait(), timeout=2)
        assert not mgr.has_session("cron:fence1"), "nothing is published while start() runs"

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence1")
        svc._jobs = [job]
        claim = svc._claims["fence1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(mgr, "_dispatch_hard_kill") as hard_kill,
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            assert svc._session_process_handles("cron:fence1") == []
            await svc._force_reap("fence1", _JOB_TIMEOUT_SECS + 60, claim=claim)
            assert _audited_outcome(mock_sel) == "failed", (
                "the audit says the run was reaped while a cold start under the key was still "
                "inside provider.start()"
            )
            # The record is written and the fence has lifted; only now does the
            # start return: refused at registration, its provider hard-killed,
            # and the call allocates again -- the completion lands after the record.
            gate.set()
            provider, is_new, _ = await asyncio.wait_for(cold_start, timeout=5)

        group_kill.assert_not_called()  # nothing was published for the passes to kill
        assert (
            "kill failed: a cold start under the key was past its spawn door when the run was ended"
            in (job.last_error or "")
        )
        assert len(started) == 2, "the refused call did not allocate again after the lift"
        hard_kill.assert_called_once_with(started[0])
        assert is_new and provider is started[1] and mgr.has_session("cron:fence1"), (
            "the completion whose start the fence caught was dropped instead of landing after "
            "the record"
        )
        assert not mgr._has_allocation_reservation("cron:fence1")
        mgr.release("cron:fence1")
        await mgr.reset("cron:fence1")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_start_refused_during_the_passes_is_still_named(
        self, tmp_path: object
    ) -> None:
        """The start returns WHILE the passes run: refused, hard-killed by dispatch, its reservation gone -- and still named.

        Without the receipt the reservation cleanup erased every trace of that
        start before the post-pass read, so the record said ``reaped`` on the
        strength of a hard kill dispatched off the loop whose outcome nothing
        read back -- a kill the dispatch could not land left the process alive
        behind that record. The receipt survives until the fence lifts, so the
        audit says ``failed`` and the record names the refusal.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        inside_start = asyncio.Event()
        gate = asyncio.Event()
        started: list[Any] = []

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            provider.shutdown = AsyncMock()

            async def _gated_start(*_args: Any, **_kwargs: Any) -> None:
                started.append(provider)
                inside_start.set()
                if len(started) == 1:
                    await gate.wait()

            provider.start = AsyncMock(side_effect=_gated_start)
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        cold_start = asyncio.create_task(mgr.get_or_create("cron:fence7"))
        await asyncio.wait_for(inside_start.wait(), timeout=2)
        real_reset = mgr.reset
        refused_during_passes: list[bool] = []

        async def _reset_then_let_the_start_return(key: str, **kwargs: Any) -> bool:
            result = await real_reset(key, **kwargs)
            # The start returns while the fence is up: refused at registration,
            # its provider hard-killed, its reservation removed -- all before the
            # post-pass read.
            gate.set()
            for _ in range(50):
                await asyncio.sleep(0)
            refused_during_passes.append(not mgr._has_allocation_reservation(key))
            return result

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence7")
        svc._jobs = [job]
        claim = svc._claims["fence7"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(mgr, "_dispatch_hard_kill") as hard_kill,
            patch.object(mgr, "reset", side_effect=_reset_then_let_the_start_return),
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("fence7", _JOB_TIMEOUT_SECS + 60, claim=claim)
            assert refused_during_passes == [True], (
                "the test did not get the start refused and its reservation removed before the "
                "post-pass read"
            )
            assert _audited_outcome(mock_sel) == "failed", (
                "the audit says the run was reaped while a cold start under the key had just "
                "been refused at registration with its hard kill unconfirmed"
            )
            provider, is_new, _ = await asyncio.wait_for(cold_start, timeout=5)

        group_kill.assert_not_called()
        assert (
            "kill failed: a cold start under the key was past its spawn door when the run was "
            "ended (fenced: 1 refused at registration during the ending, the provider "
            "hard-killed there by the allocation path -- an outcome this record does not "
            "confirm); not signalled"
        ) in (job.last_error or "")
        hard_kill.assert_called_once_with(started[0])
        assert len(started) == 2 and is_new and provider is started[1]
        assert mgr.has_session("cron:fence7")
        # The receipt went with the fence: the recorded key carries nothing forward.
        assert mgr._spawn_in_flight("cron:fence7") is None
        mgr.release("cron:fence7")
        await real_reset("cron:fence7")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_caller_held_at_the_door_lands_only_once_the_run_is_recorded(
        self, tmp_path: object
    ) -> None:
        """The fence outlives the passes: a completion held at the door wakes to a RECORDED key.

        The run's terminal record -- the locked store merge, the history row --
        and its audit are written while the key is still fenced; only then does
        the fence lift and the held caller land. A fence that lifted when the
        passes ended let the caller allocate against a key that was neither
        being ended nor recorded: runtime and durable state disagreeing for the
        span of the write.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            provider.shutdown = AsyncMock()
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        provider, _, _ = await mgr.get_or_create("cron:fence5")
        mgr.release("cron:fence5")
        client = MagicMock()
        client._pid = 2**22 + 9191
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        real_reset = mgr.reset
        events: list[str] = []
        racing: list[asyncio.Task[Any]] = []

        async def _land() -> tuple[Any, bool, bool]:
            result = await mgr.get_or_create("cron:fence5")
            events.append("landed")
            return result

        async def _reset_then_race(key: str, **kwargs: Any) -> bool:
            result = await real_reset(key, **kwargs)
            if not racing:
                # The completion's cold start after the pop: held at the door.
                racing.append(asyncio.create_task(_land()))
                for _ in range(20):
                    await asyncio.sleep(0)
                events.append("held" if not racing[0].done() else "landed before the passes ended")
            return result

        boundary = mgr._allocation_boundary()
        real_end_ending = boundary.end_ending

        def _end_ending(key: str) -> None:
            events.append("fence down")
            real_end_ending(key)

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence5")
        svc._jobs = [job]
        claim = svc._claims["fence5"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()
        real_append = svc._history.append

        async def _append(record: Any) -> None:
            events.append("record")
            await real_append(record)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(svc._history, "append", AsyncMock(side_effect=_append)),
            patch.object(boundary, "end_ending", side_effect=_end_ending),
            patch.object(mgr, "reset", side_effect=_reset_then_race),
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            mock_sel().log_tool_invocation.side_effect = lambda **_: events.append("audit")
            await svc._force_reap("fence5", _JOB_TIMEOUT_SECS + 60, claim=claim)
            _, is_new, _ = await asyncio.wait_for(racing[0], timeout=5)

        assert events == ["held", "record", "audit", "fence down", "landed"], (
            "the caller held at the door was let in against a key that was neither being ended "
            f"nor recorded: {events}"
        )
        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")
        assert is_new and mgr.has_session("cron:fence5")
        mgr.release("cron:fence5")
        await real_reset("cron:fence5")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_claim_waiting_on_the_busy_parent_is_not_named_and_lands_after_the_record(
        self, tmp_path: object
    ) -> None:
        """A reservation is not a process: a completion's claim blocked on the busy parent's turn is held, not named.

        The sub-agent completion's ``get_or_create`` of the parent key has taken
        its reservation and waits on the live session's turn semaphore -- it
        started no provider, and the passes tore the run's process down. Naming
        that reservation as a start refused at registration recorded a kill
        failure that never happened: ``failed`` over a clean reap. Only a
        reservation past its spawn door is named; the waiter is woken by the
        reset, meets the fence at the door and lands once the run is recorded.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            provider.shutdown = AsyncMock()
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        # The run's own turn: the permit stays HELD -- the busy parent.
        provider, _, _ = await mgr.get_or_create("cron:fence6")
        client = MagicMock()
        client._pid = 2**22 + 9292
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client

        waiter = asyncio.create_task(mgr.get_or_create("cron:fence6"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not waiter.done(), "the claim did not wait on the busy parent's turn"
        assert mgr._has_allocation_reservation("cron:fence6"), "the waiting claim holds a reservation"
        assert not mgr._spawn_in_flight("cron:fence6"), "a claim waiting on the turn is not a start"

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence6")
        svc._jobs = [job]
        claim = svc._claims["fence6"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            await svc._force_reap("fence6", _JOB_TIMEOUT_SECS + 60, claim=claim)
            assert _audited_outcome(mock_sel) == "reaped", (
                "the audit says the run's kill failed over a claim that started nothing: "
                f"{job.last_error!r}"
            )
            # Woken by the reset, held at the door, landed after the record.
            _, is_new, _ = await asyncio.wait_for(waiter, timeout=5)

        group_kill.assert_not_called()
        assert "kill failed" not in (job.last_error or "")
        assert is_new and mgr.has_session("cron:fence6"), "the waiting claim was dropped"
        assert not mgr._has_allocation_reservation("cron:fence6")
        await mgr.reset("cron:fence6")

    @pytest.mark.asyncio
    async def test_the_fence_is_raised_before_the_snapshot_and_lifted_only_after_the_record_and_audit(
        self, tmp_path: object
    ) -> None:
        """Order and report, on a manager double: fence up before the first read, the start past its spawn door named, the fence down only after the record and the audit."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        events: list[str] = []

        @contextmanager
        def _fence(key: str) -> Iterator[None]:
            events.append(f"fence up {key}")
            try:
                yield
            finally:
                events.append(f"fence down {key}")

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            events.append("reset")
            return False

        real_append = svc._history.append

        async def _append(record: Any) -> None:
            events.append("record")
            await real_append(record)

        svc._sessions.ending_key = _fence
        svc._sessions.reset = AsyncMock(side_effect=_reset)
        svc._sessions._spawn_in_flight = MagicMock(
            side_effect=lambda key: events.append(f"spawn in flight? {key}") or "refused at registration, its provider hard-killed there"
        )
        job = _make_job("fence2")
        svc._jobs = [job]
        claim = svc._claims["fence2"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(svc._history, "append", AsyncMock(side_effect=_append)),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            mock_sel().log_tool_invocation.side_effect = lambda **_: events.append("audit")
            await svc._force_reap("fence2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert events == [
            "fence up cron:fence2",
            "reset",
            "spawn in flight? cron:fence2",
            "record",
            "audit",
            "fence down cron:fence2",
        ], "the ending fence lifted before the run's terminal record and audit were written"
        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert (
            "kill failed: a cold start under the key was past its spawn door when the run was ended "
            "(fenced: refused at registration, its provider hard-killed there); not signalled"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_manager_without_the_fence_is_not_fenced_and_reports_nothing(
        self, tmp_path: object
    ) -> None:
        """A double with neither ``ending_key`` nor a boolean ``_spawn_in_flight``: the passes are the whole answer."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        del svc._sessions.ending_key
        del svc._sessions._spawn_in_flight
        svc._sessions.reset = AsyncMock(return_value=False)
        job = _make_job("fence3")
        svc._jobs = [job]
        claim = svc._claims["fence3"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
        ):
            await svc._force_reap("fence3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_without_the_fence_a_claim_after_the_post_pass_read_is_recorded_reaped_over(
        self, tmp_path: object
    ) -> None:
        """Why the fence, given the pre-reset handles, the pop capture and the post-pass read: the interval they cannot see.

        The three name every process under the key up to the post-pass read.
        A completion's claim that begins AFTER that read -- while the terminal
        record is being written, an interval of awaits -- is outside all three:
        no snapshot, no pop and no spawn read is taken again, and without the
        fence its cold start publishes under the key before the record lands,
        so the record says ``reaped`` over a live process nothing answered.
        The fence holds that claim at the door until the record and the audit
        are written, so it lands only under a recorded key. Red with the fence
        removed (``_ending_fence`` a no-op): the claim lands before the record.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            provider.shutdown = AsyncMock()
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        provider, _, _ = await mgr.get_or_create("cron:fence7")
        mgr.release("cron:fence7")
        client = MagicMock()
        client._pid = 2**22 + 9393
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        events: list[str] = []
        racing: list[asyncio.Task[Any]] = []

        async def _land() -> tuple[Any, bool, bool]:
            result = await mgr.get_or_create("cron:fence7")
            events.append("landed")
            return result

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence7")
        svc._jobs = [job]
        claim = svc._claims["fence7"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()
        real_append = svc._history.append

        async def _append(record: Any) -> None:
            # The passes and the post-pass read are over; the record is being
            # written. A completion's claim begins now and is given time enough
            # for a cold start (a thread hop and an instant provider start) to
            # publish: unfenced, it lands here; fenced, it is held at the door.
            racing.append(asyncio.create_task(_land()))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if racing[0].done():
                    break
            events.append("landed before the record" if racing[0].done() else "held")
            events.append("record")
            await real_append(record)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(svc._history, "append", AsyncMock(side_effect=_append)),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            mock_sel().log_tool_invocation.side_effect = lambda **_: events.append("audit")
            await svc._force_reap("fence7", _JOB_TIMEOUT_SECS + 60, claim=claim)
            _, is_new, _ = await asyncio.wait_for(racing[0], timeout=5)

        assert events == ["held", "record", "audit", "landed"], (
            "the run was recorded reaped over a session that landed under the key after the "
            "post-pass read and before the record: no pass, no pop capture and no spawn read "
            f"named it, and only the fence orders it after the record: {events}"
        )
        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert is_new and mgr.has_session("cron:fence7")
        mgr.release("cron:fence7")
        await mgr.reset("cron:fence7")


class TestAGoneWindowsRootDrainsItsPendingTreeCleanup:
    """Windows: a root that exited is not a tree that finished.

    The spawn reserves an exact-handle cleanup for the tree; when the root exits
    but descendants (or cleanup debt) outlive it, the pin for the root's identity
    stays pending. A gone root with a pin pending is drained through
    ``kill_process_tree_pinned`` (off the loop), and only a completed drain is a
    kill; no pin pending is nothing this code can reach. ``IS_WINDOWS`` pinned;
    the platform primitives are stubbed, so this runs on every runner.
    """

    def _windows_gone_root(
        self, svc: CronService, key: str, pid: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        _session_with_pid(svc, key, pid)

    @pytest.mark.asyncio
    async def test_a_pending_cleanup_is_drained_and_a_completed_drain_is_the_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root reads back, exits before the pin (``False``), its pending pin is drained: the kill."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-pin1")
        self._windows_gone_root(svc, "cron:win-pin1", 8282, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned", side_effect=[False, True]
            ) as drain,
        ):
            await svc._force_reap("win-pin1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert drain.call_args_list == [call(8282, _START_ID, platform_compat.SIGKILL)] * 2
        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_drain_that_does_not_complete_is_a_failed_kill_not_reaped(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-pin2")
        self._windows_gone_root(svc, "cron:win-pin2", 8383, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned",
                side_effect=[False, OSError("Windows tree cleanup incomplete: 2 handles retained")],
            ),
        ):
            await svc._force_reap("win-pin2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: Windows tree cleanup incomplete for exited pid 8383: OSError: " in (
            job.last_error or ""
        )

    @pytest.mark.asyncio
    async def test_an_identity_that_cannot_be_pinned_is_a_failed_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        self._windows_gone_root(svc, "cron:win-pin3", 8484, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False),
        ):
            failure = await svc._sigkill_session("cron:win-pin3", _handle_of(svc, "cron:win-pin3"))

        assert failure == (
            "pid 8484 exited with Windows tree cleanup pending and its identity could not be "
            "pinned; not drained"
        )

    @pytest.mark.asyncio
    async def test_a_pending_cleanup_with_no_recorded_identity_is_a_failed_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin answers by root pid alone; without a creation time nothing can be drained safely."""
        from kiro_crew.process_identity import (
            ProcessHandle,
            kill_verified_process,
            process_survived,
        )

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        handle = ProcessHandle(pid=8686, start_id=None, pgid=None, child_pids={})

        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True) as pin,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned") as drain,
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            assert process_survived(handle) is True
            failure = await kill_verified_process(
                handle, who="Reaper", key="cron:win-pin5", child_helpers=_child_helpers()
            )

        pin.assert_called_with(8686, None)
        drain.assert_not_called()
        assert failure == (
            "pid 8686 exited with Windows tree cleanup pending and no recorded identity to pin "
            "it by; not drained"
        )

    @pytest.mark.asyncio
    async def test_a_gone_root_with_a_pending_cleanup_survived_a_completed_reset(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``process_survived`` reads the pin: a completed reset that left cleanup pending is not a reap."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-pin4")
        self._windows_gone_root(svc, "cron:win-pin4", 8585, monkeypatch)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # The creation time still reads back through the held handle; the exit
            # code says gone; the pin says the tree's cleanup is still owed.
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=True) as drain,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("win-pin4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        # The survivor's identity still reads back, so the live-root path pins
        # and drains it -- never ``taskkill /T`` by number.
        tree_kill.assert_not_awaited()
        drain.assert_called_once_with(8585, _START_ID, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"


class TestALiveWindowsRootIsKilledThroughItsPinnedHandle:
    """Windows: a live root's tree is terminated through a handle pinned on its creation time, never by pid.

    ``taskkill /T /PID`` resolves the pid by number when ``taskkill.exe`` runs --
    an executor hop and a process spawn after the identity read that found the
    root alive -- so a pid recycled in that window hands a stranger's tree to
    the kill. The live root goes through ``kill_process_tree_pinned``: the
    process object is opened only when its creation time matches the recorded
    start id and held across the terminate, the descendants are terminated
    through their own verified handles, and only a completed drain is the kill.
    A root whose identity cannot be pinned any more is gone (its pending cleanup
    pin decides, as on POSIX the retained group does), and a drain that raises
    is a failure with no root-only fallback. ``IS_WINDOWS`` pinned, the platform
    primitives stubbed, so this runs on every runner.
    """

    def _windows_live_root(
        self, svc: CronService, key: str, pid: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        _session_with_pid(svc, key, pid)

    @pytest.mark.asyncio
    async def test_a_pid_recycled_after_the_last_identity_read_is_never_signalled_by_number(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both identity reads pass, the pid is recycled before the signal: the pin refuses and ``taskkill /T`` is never spawned."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-live1")
        self._windows_live_root(svc, "cron:win-live1", 8787, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)) as pid_kill,
            # The recycled pid: no process object with the recorded creation time
            # can be opened any more, and the old identity owes no cleanup.
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False) as drain,
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
        ):
            await svc._force_reap("win-live1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert group_kill.call_count == 0, (
            "the Windows kill signalled pid 8787 by number after its identity read: a pid "
            "recycled in between hands a stranger's process tree to taskkill /T"
        )
        pid_kill.assert_not_awaited()
        drain.assert_called_once_with(8787, _START_ID, platform_compat.SIGKILL)
        # The run's root is gone and its tree owes nothing: nothing to kill.
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_recycled_pid_whose_old_tree_still_owes_cleanup_is_a_failed_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-live2")
        self._windows_live_root(svc, "cron:win-live2", 8888, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
        ):
            await svc._force_reap("win-live2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert (
            "; kill failed: pid 8888 exited with Windows tree cleanup pending and its identity "
            "could not be pinned; not drained"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_completed_pinned_drain_is_the_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-live3")
        self._windows_live_root(svc, "cron:win-live3", 8989, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=True) as drain,
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending") as pin,
        ):
            await svc._force_reap("win-live3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        drain.assert_called_once_with(8989, _START_ID, platform_compat.SIGKILL)
        tree_kill.assert_not_awaited()
        pin.assert_not_called()  # a drained tree asks nothing more
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_pinned_drain_that_raises_is_a_failed_kill_with_no_root_only_fallback(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-live4")
        self._windows_live_root(svc, "cron:win-live4", 9090, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)) as pid_kill,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned",
                side_effect=OSError("Windows process tree did not drain before the deadline"),
            ),
        ):
            await svc._force_reap("win-live4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert (
            "; kill failed: Windows tree cleanup incomplete for pid 9090: OSError: Windows process "
            "tree did not drain before the deadline"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_pinned_drain_runs_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drain waits on the tree's handles, so it must not run on the caller's loop thread."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        self._windows_live_root(svc, "cron:win-live5", 9191, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()
        loop_thread = threading.get_ident()
        drained_on: list[int] = []

        def _drain(pid: int, start: str, sig: int) -> bool:
            drained_on.append(threading.get_ident())
            return True

        with (
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", side_effect=_drain),
        ):
            assert await svc._sigkill_session("cron:win-live5", _handle_of(svc, "cron:win-live5")) is None

        assert drained_on and all(ident != loop_thread for ident in drained_on), (
            "the pinned drain ran on the event loop"
        )


class TestProcessSurvived:
    """``process_survived`` says False only on evidence the process is gone.

    Liveness comes from ``pid_exists`` (the exit-code-confirmed predicate); the
    recorded start id guards only a LIVE pid against reuse. The platform seam is
    pinned both ways: the primitives' shapes are stubbed, so the verdict must be
    the same rule on win32 -- where the identity read alone lies about a
    just-exited child -- and on linux, where nothing changes.
    """

    def _handle(
        self,
        pid: int | None = 7070,
        start_id: str | None = _START_ID,
        pgid: int | None = None,
    ) -> ProcessHandle:
        return ProcessHandle(pid=pid, start_id=start_id, pgid=pgid, child_pids={})

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_a_live_pid_whose_start_id_reads_back_survived(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        ):
            assert process_survived(self._handle()) is True

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_an_exited_pid_whose_start_id_still_reads_back_did_not_survive(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        """Identity is not liveness.

        On win32 the creation FILETIME reads back through a query handle for as
        long as any handle to the exited process object is still held (the
        Proactor transport's, until GC), so a just-exited child answers the
        recorded start id while ``pid_exists`` -- which confirms the exit code --
        says gone: not a survivor. Same rule on linux, where a dead pid has no
        readable start id to begin with.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            _group_gone(),
        ):
            assert process_survived(self._handle()) is False

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_a_live_pid_with_a_different_start_id_is_not_our_process(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        """A live pid is guarded by identity: a different start id is another process's pid now."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            _group_gone(),
        ):
            assert process_survived(self._handle()) is False

    def test_a_recycled_pid_is_decided_by_identity_before_the_platform_is_asked(self) -> None:
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.pid_exists") as exists,
            _group_gone(),
        ):
            assert process_survived(self._handle()) is False
        exists.assert_not_called()

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_a_gone_leader_whose_group_still_has_members_survived_on_posix_only(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        """The leader's absence is not the group's: members a late child spawn left keep the run alive.

        On linux the retained group id is probed and a live group is a survivor
        the kill has to answer for. On win32 there are no process groups and
        nothing this code can reach once the root is gone, so the gone root
        stays "nothing to kill" -- the documented platform limitation.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True) as group,
        ):
            assert process_survived(self._handle(pgid=7070)) is (not windows)
        if windows:
            group.assert_not_called()
        else:
            group.assert_called_once_with(7070)

    def test_a_gone_leader_with_no_verified_group_id_is_probed_by_its_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX: a ``start_new_session`` leader's pid IS its group id; with no verified id it is probed, never signalled."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True) as group,
        ):
            assert process_survived(self._handle(pgid=None)) is True
        group.assert_called_once_with(7070)

    def test_a_gone_windows_root_whose_tree_cleanup_is_pending_survived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows asks the exact-tree cleanup pin: pending means descendants or debt outlived the root."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True) as pin,
            patch("kiro_crew.platform_compat.pgroup_exists") as group,
        ):
            assert process_survived(self._handle()) is True
        pin.assert_called_once_with(7070, _START_ID)
        group.assert_not_called()

    def test_a_live_pid_with_no_readable_start_id_survived(self) -> None:
        """Alive but unverifiable is the kill's to decide (a named failure), not a gone process."""
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        ):
            assert process_survived(self._handle()) is True

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_a_verified_running_recorded_child_means_the_run_s_process_is_standing(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        """Leader gone, tree gone, but a recorded child whose start id reads back is still running: a survivor.

        The escaped-children sweep refuses a child that ``exec``'d into another
        name (basename check) and reports nothing, so unless the recorded children
        are asked here such a child survives every teardown unreported. A zombie
        child -- signalled, waiting to be reaped -- is a finished process, not a
        survivor; a child whose start id does not read back is another process's.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        child = 8070
        handle = ProcessHandle(
            pid=7070, start_id=_START_ID, pgid=None, child_pids={child: ("5150001", b"node")}
        )
        reads = _start_id_reads({7070: None, child: "5150001"})
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=reads),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
        ):
            assert process_survived(handle) is True
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=reads),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=True),
            _group_gone(),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
        ):
            assert process_survived(handle) is False, "a zombie child is finished, not standing"
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7070: None, child: "9999999"}),
            ),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
        ):
            assert process_survived(handle) is False, "a recycled child pid is another process's"

    def test_no_usable_pid_did_not_survive(self) -> None:
        with patch("kiro_crew.platform_compat.get_process_start_id") as start_id:
            assert process_survived(self._handle(pid=None)) is False
        start_id.assert_not_called()


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

        return patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id), threads

    @pytest.mark.asyncio
    async def test_process_survived_async_runs_the_scan_on_a_worker_thread(self) -> None:
        handle = ProcessHandle(
            pid=_UNALLOCATABLE_PID, start_id=_START_ID, pgid=None, child_pids={_UNALLOCATABLE_PID + 1: (_START_ID, "kiro-cli")}
        )
        reads, threads = self._reads_off_the_loop()
        with (
            reads,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
        ):
            assert await process_survived_async(handle) is True

        assert threads and all(ident != threading.get_ident() for ident in threads), (
            "the per-child scan ran on the event loop"
        )

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
        assert threads and all(ident != threading.get_ident() for ident in threads), (
            "the post-sweep standing read ran on the event loop"
        )

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
        assert threads and all(ident != threading.get_ident() for ident in threads), (
            "the group-member vouch ran on the event loop"
        )


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
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="7770001") as reads,
            patch(
                "os.getpgid", side_effect=ProcessLookupError("[Errno 3] No such process"), create=True
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
