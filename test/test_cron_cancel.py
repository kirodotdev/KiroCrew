"""Tests for user-initiated cancellation of running cron executions.

Covers CronService.cancel() (agent + script/command paths), the
subprocess registry in cron_script, real mid-run cancellation of
run_command_sandboxed, and the POST /api/crons/{id}/cancel handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_cron_reaper import (
    _KILL_PRIMITIVES,
    _isolated_leader,
    _refuse_unpinned_signal,
    _refuse_unpinned_signal_async,
    _retain_torn_down,
)

from kiro_crew import cron as cron_module
from kiro_crew import platform_compat
from kiro_crew.cron import CronJob, CronSchedule, CronService, CronStoreBusy, _RunClaim
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.cron_script import (
    _CANCELLED_PROC_JOBS,
    _RUNNING_PROCS,
    kill_running_process,
    run_command_sandboxed,
)
from kiro_crew.dashboard.handlers.cron import api_cron_cancel
from kiro_crew.testing.wait import LoopHop, await_until, finished, wait_until


@pytest.fixture(autouse=True)
def _kill_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX-shaped kill path unless a test pins ``IS_WINDOWS``; every kill primitive refuses unless the test pins it (see cron_reaper_helpers.py)."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    for name in _KILL_PRIMITIVES:
        guard = (
            _refuse_unpinned_signal_async if name.endswith("_async") else _refuse_unpinned_signal
        )
        monkeypatch.setattr(platform_compat, name, guard)


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    sessions._sessions = {}
    # No teardown in flight (``SessionManager.tearing_down``): a live-map miss is
    # a key with no process.
    sessions.tearing_down = MagicMock(return_value=[])
    return sessions


def _make_job(job_id: str = "job1", name: str = "test job", **kwargs) -> CronJob:
    return CronJob(
        id=job_id,
        name=name,
        message="do something",
        schedule=CronSchedule(kind="every", every_secs=300),
        created_ts=time.time(),
        **kwargs,
    )


class TestCronServiceCancel:
    """CronService.cancel() semantics."""

    @pytest.mark.asyncio
    async def test_cancel_not_running_returns_false(self) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._jobs = [_make_job("idle1")]
        assert await svc.cancel("idle1") is False
        assert await svc.cancel("nonexistent") is False

    @pytest.mark.asyncio
    async def test_cancel_agent_job_resets_session_and_records_history(
        self, tmp_path: object
    ) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        sessions = _mock_sessions()
        svc._sessions = sessions

        job = _make_job("run1")
        svc._jobs = [job]
        task = MagicMock(done=MagicMock(return_value=False))
        claim = svc._claims["run1"] = _RunClaim(
            trigger="manual", claimed_at=time.time() - 42, task=task
        )
        refresh_calls: list[str] = []
        svc._push_refresh = refresh_calls.append

        with patch("kiro_crew.sel.sel") as mock_sel, patch.object(svc, "_save"):
            assert await svc.cancel("run1") is True

        assert job.last_status == "error"
        assert "Cancelled by user" in (job.last_error or "")
        assert claim.cancelled
        assert "run1" not in svc._claims
        task.cancel.assert_called_once()
        # ``ends_conversation``: cancelling the job ends its conversation, so its
        # sub-agent runs go with it. Asserting the whole call keeps a later edit from
        # dropping that and leaving the children of a cancelled cron running.
        sessions.reset.assert_awaited_once_with("cron:run1", ends_conversation=True, scope=ANY)
        assert "cron_history" in refresh_calls and "crons" in refresh_calls
        runs, total = await svc._history.get_job_history("run1")
        assert total == 1
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["trigger"] == "manual"
        mock_sel().log_tool_invocation.assert_called_once()
        assert mock_sel().log_tool_invocation.call_args.kwargs["tool_name"] == "cron_cancel"

    @pytest.mark.asyncio
    async def test_cancel_script_job_kills_subprocess_not_session(self, tmp_path: object) -> None:
        """Script crons: subprocess is killed; no kiro-cli session reset -- and the audit lists no session as ended."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        sessions = _mock_sessions()
        svc._sessions = sessions

        job = _make_job("script1", script="~/.kirocrew/crons/x.py:run")
        svc._jobs = [job]
        svc._claims["script1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - 10,
            task=MagicMock(done=MagicMock(return_value=False)),
        )

        with (
            patch("kiro_crew.cron_script.kill_running_process", return_value=True) as mock_kill,
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
        ):
            assert await svc.cancel("script1") is True

        mock_kill.assert_called_once_with("script1")
        sessions.reset.assert_not_awaited()
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_key"] == "cron:script1"
        assert audit["metadata"]["session_keys"] == [], (
            "the cron_cancel audit lists a session as ended that step 2 never touched: "
            f"{audit['metadata']['session_keys']}"
        )

    @pytest.mark.asyncio
    async def test_cancel_does_not_touch_consecutive_failures(self, tmp_path: object) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("run2")
        job.consecutive_failures = 3
        svc._jobs = [job]
        svc._claims["run2"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - 5,
            task=MagicMock(done=MagicMock(return_value=False)),
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc.cancel("run2")

        assert job.consecutive_failures == 3
        assert job.enabled is True

    @pytest.mark.asyncio
    async def test_cancel_records_a_refused_sigkill_as_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """Same helper as the reaper: a kill it reports as refused is a failure here too.

        The cancel still finishes -- claim released, task cancelled, terminal
        row written, ``True`` answered -- but its record carries the failure
        and its audit says ``failed``, not ``cancelled``, because the run's
        process group is still alive.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        svc._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
        client = MagicMock()
        client._pid = 5151
        client._child_pids = {}
        client._start_time = "4821903"
        session = MagicMock()
        session.provider._client = client
        svc._sessions._sessions["cron:refused"] = session

        job = _make_job("refused")
        svc._jobs = [job]
        task = MagicMock(done=MagicMock(return_value=False))
        claim = svc._claims["refused"] = _RunClaim(
            trigger="manual", claimed_at=time.time() - 42, task=task
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="4821903"),
            _isolated_leader(),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=ValueError(
                    "kill_process_group: refusing broadcast/self process group 5151"
                ),
            ),
        ):
            assert await svc.cancel("refused") is True

        assert "refused" not in svc._claims
        task.cancel.assert_called_once()
        assert claim.cancelled
        assert (job.last_error or "").startswith("Cancelled by user after")
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")
        runs, total = await svc._history.get_job_history("refused")
        assert total == 1 and runs[0]["status"] == "cancelled"
        assert "; kill failed: " in runs[0]["error"]
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["tool_name"] == "cron_cancel"
        assert (
            audit["outcome"] == "failed"
        ), "the SEL audit says the run was cancelled while its process group is still alive"

    @pytest.mark.asyncio
    async def test_cancel_kills_the_process_a_hung_reset_had_already_unmapped(
        self, tmp_path: object
    ) -> None:
        """The reset pops the session before it can hang; cancel takes its kill handle first."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        async def _pop_then_hang(session_key: str, **_: object) -> bool:
            svc._sessions._sessions.pop(session_key, None)
            raise asyncio.TimeoutError

        svc._sessions.reset = AsyncMock(side_effect=_pop_then_hang)
        client = MagicMock()
        client._pid = 5252
        client._child_pids = {}
        client._start_time = "4821903"
        session = MagicMock()
        session.provider._client = client
        svc._sessions._sessions["cron:popped"] = session
        job = _make_job("popped")
        svc._jobs = [job]
        claim = svc._claims["popped"] = _RunClaim(
            trigger="manual",
            claimed_at=time.time() - 42,
            task=MagicMock(done=MagicMock(return_value=False)),
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="4821903"),
            _isolated_leader(),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            assert await svc.cancel("popped") is True

        assert "cron:popped" not in svc._sessions._sessions, "the fixture did not pop the session"
        group_kill.assert_called_once_with(5252, platform_compat.SIGKILL)
        assert claim.cancelled
        assert "kill failed" not in (job.last_error or "")
        assert mock_sel().log_tool_invocation.call_args.kwargs["outcome"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_kills_a_process_that_survived_a_completed_reset(
        self, tmp_path: object
    ) -> None:
        """A reset that returned (True, or False for an already-popped key) is not proof of death.

        Mirrors ``_force_reap``: after every completed reset the pre-reset handle
        is asked -- pid plus recorded start id -- and a process still standing
        gets the fallback, so cancel never records a cancellation it did not
        deliver on the strength of the reset's boolean.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        svc._sessions.reset = AsyncMock(return_value=True)
        client = MagicMock()
        client._pid = 5353
        client._child_pids = {}
        client._start_time = "4821903"
        session = MagicMock()
        session.provider._client = client
        svc._sessions._sessions["cron:survived"] = session
        job = _make_job("survived")
        svc._jobs = [job]
        svc._claims["survived"] = _RunClaim(
            trigger="manual",
            claimed_at=time.time() - 42,
            task=MagicMock(done=MagicMock(return_value=False)),
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="4821903"),
            _isolated_leader(),
            # Liveness is read after identity (see ``process_survived``); pinned so
            # the verdict does not depend on what the host runs under a made-up pid.
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            assert await svc.cancel("survived") is True

        group_kill.assert_called_once_with(5353, platform_compat.SIGKILL)
        assert "kill failed" not in (job.last_error or "")
        assert mock_sel().log_tool_invocation.call_args.kwargs["outcome"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_kills_the_process_of_a_session_the_run_s_own_teardown_popped(
        self, tmp_path: object
    ) -> None:
        """The run's own finally reset popped the session BEFORE cancel looked; the kill still lands.

        Same lookup as ``_force_reap``: the live map misses, the session manager
        still holds the popped session for the life of its (hung) teardown, and
        the handle read from there names the process. Without it cancel's own
        reset answered False for the gone key, nothing was verified, and the run
        was recorded ``cancelled`` with its process untouched.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        svc._sessions.reset = AsyncMock(return_value=False)
        client = MagicMock()
        client._pid = 5454
        client._child_pids = {}
        client._start_time = "4821903"
        torn = MagicMock()
        torn.provider._client = client
        # Out of the live map, in the torn-down table (with the handle read at the pop).
        _retain_torn_down(svc, "cron:torn", torn)
        job = _make_job("torn")
        svc._jobs = [job]
        svc._claims["torn"] = _RunClaim(
            trigger="manual",
            claimed_at=time.time() - 42,
            task=MagicMock(done=MagicMock(return_value=False)),
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="4821903"),
            _isolated_leader(),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            assert await svc.cancel("torn") is True

        group_kill.assert_called_once_with(5454, platform_compat.SIGKILL)
        assert "kill failed" not in (job.last_error or "")
        assert mock_sel().log_tool_invocation.call_args.kwargs["outcome"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_s_kill_path_logs_under_its_own_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The shared kill path names its caller: cancel's lines read ``Cancel:``, not ``Reaper:``."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        with caplog.at_level("WARNING", logger="kiro_crew.cron"):
            assert await svc._sigkill_sessions("cron:quiet", [], who="Cancel") is None

        assert "Cancel: no session found for cron:quiet" in caplog.text
        assert "Reaper:" not in caplog.text

    @pytest.mark.asyncio
    async def test_cancel_holds_the_key_s_ending_fence_through_the_record_and_names_a_spawn_in_flight(
        self, tmp_path: object
    ) -> None:
        """Same fence as ``_force_reap``: up before the first read, lifted only after the record and the audit, a start past its spawn door named.

        A cold start caught inside ``provider.start()`` when cancel runs has
        published nothing a pass could see; the fence invalidates it (refused at
        registration, its provider hard-killed there) and cancel reports it
        instead of recording ``cancelled`` over it. And the fence outlives the
        passes: a caller held at the door wakes to a key whose run is RECORDED,
        never to one that is neither being ended nor recorded.
        """
        from collections.abc import Iterator
        from contextlib import contextmanager

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
            side_effect=lambda key: events.append(f"spawn in flight? {key}")
            or "refused at registration, its provider hard-killed there"
        )
        job = _make_job("fenced")
        svc._jobs = [job]
        svc._claims["fenced"] = _RunClaim(
            trigger="manual",
            claimed_at=time.time() - 42,
            task=MagicMock(done=MagicMock(return_value=False)),
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(svc._history, "append", AsyncMock(side_effect=_append)),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            mock_sel().log_tool_invocation.side_effect = lambda **_: events.append("audit")
            assert await svc.cancel("fenced") is True

        assert events == [
            "fence up cron:fenced",
            "reset",
            "spawn in flight? cron:fenced",
            "record",
            "audit",
            "fence down cron:fenced",
        ], "the ending fence lifted before the run's terminal record and audit were written"
        tree_kill.assert_not_awaited()
        assert mock_sel().log_tool_invocation.call_args.kwargs["outcome"] == "failed"
        assert (
            "kill failed: a cold start under the key was past its spawn door when the run was ended"
            in (job.last_error or "")
        )

    @pytest.mark.asyncio
    async def test_cancel_names_a_session_registered_under_the_key_after_the_pop_as_a_failed_kill(
        self, tmp_path: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Same one pass as ``_force_reap``: a session that lands after the reset's pop is named, not reset, not signalled.

        Through the real manager nothing lands under a fenced key after the pop
        (every door meets the fence or never publishes a cron key); a manager
        without the fence that does register one is reported, and cancel records
        ``failed`` rather than ``cancelled`` over the process it did not answer.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        def _register(pid: int) -> None:
            client = MagicMock()
            client._pid = pid
            client._child_pids = {}
            client._start_time = "4821903"
            session = MagicMock()
            session.provider._client = client
            svc._sessions._sessions["cron:late"] = session

        _register(5555)
        late = [5656]

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            session = svc._sessions._sessions.pop(session_key, None)
            scope = kwargs.get("scope")
            if session is not None and scope is not None:
                scope.note_pop(session_key, session)
            if late:
                _register(late.pop(0))  # lands after the pop
            return True

        svc._sessions.reset = AsyncMock(side_effect=_reset)
        job = _make_job("late")
        svc._jobs = [job]
        svc._claims["late"] = _RunClaim(
            trigger="manual",
            claimed_at=time.time() - 42,
            task=MagicMock(done=MagicMock(return_value=False)),
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="4821903"),
            _isolated_leader(),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=False),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
            caplog.at_level("WARNING", logger="kiro_crew.cron"),
        ):
            assert await svc.cancel("late") is True

        assert [called.args[0] for called in group_kill.call_args_list] == [
            5555
        ], "a session that landed after the pop was signalled: a second pass ran"
        assert svc._sessions.reset.await_count == 1, "the key was reset more than once"
        assert (
            "Cancel: a session registered under cron:late after the reset's pop for cron late "
            "(pid 5656); not reset, not signalled"
        ) in caplog.text
        assert (
            "; kill failed: a session registered under the key after the reset's pop (pid 5656); "
            "not reset, not signalled"
        ) in (job.last_error or "")
        assert (
            mock_sel().log_tool_invocation.call_args.kwargs["outcome"] == "failed"
        ), "cancel recorded cancelled over a process it never answered"
        assert 5656 in {s.provider._client._pid for s in svc._sessions._sessions.values()}

    @pytest.mark.asyncio
    async def test_run_job_isolated_skips_history_when_cancelled(self, tmp_path: object) -> None:
        """The normal completion path must not double-record after cancel()."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        job = _make_job("run3")
        svc._jobs = [job]
        claim = svc._claim_run("run3", "manual")
        claim.cancelled = True

        with patch.object(svc, "_merge_job_result") as mock_merge:
            await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        _, total = await svc._history.get_job_history("run3")
        assert total == 0

    @pytest.mark.asyncio
    async def test_a_run_stop_cancels_mid_callback_records_its_own_interruption(
        self, tmp_path: object
    ) -> None:
        """stop() cancels the run task and sets no cancel flag, so the run still
        files a history row. The row must not quote the previous run's error or
        call the run a failure, and the job keeps the last completed run's
        outcome rather than the blank status ``_execute`` reset it to.
        """
        prior_error = "Reaped after 3808s (exceeded 1800s deadline)"
        svc = CronService(base_dir=None, on_job=AsyncMock(side_effect=asyncio.CancelledError))
        svc._history = CronHistoryStore(base_dir=tmp_path)
        job = _make_job("stopped", last_status="error", last_error=prior_error)
        svc._jobs = [job]
        claim = svc._claim_run("stopped", "scheduled")

        with (
            patch.object(svc, "_merge_job_result") as mock_merge,
            patch.object(svc, "_compute_jitter", return_value=0),
            pytest.raises(asyncio.CancelledError),
        ):
            await svc._run_job_isolated(job, claim)

        runs, total = await svc._history.get_job_history("stopped")
        assert total == 1
        row = runs[0]
        assert row["status"] == "cancelled"
        assert row["error"] == "Interrupted: the cron service stopped before this run finished"
        assert row["summary"] == row["error"]
        assert prior_error not in json.dumps(row)
        merged = mock_merge.call_args.args[0]
        assert (merged.last_status, merged.last_error) == ("error", prior_error)
        assert (job.last_status, job.last_error) == ("error", prior_error)

    @pytest.mark.asyncio
    async def test_a_run_stop_cancels_in_its_jitter_wait_is_not_the_previous_success(
        self, tmp_path: object
    ) -> None:
        """A run stopped during its jitter wait never entered ``_execute``, so the
        job still holds the previous run's "ok" when the row is written. The
        row must say the run was stopped before it started, not "success".
        """
        on_job = AsyncMock()
        svc = CronService(base_dir=None, on_job=on_job)
        svc._history = CronHistoryStore(base_dir=tmp_path)
        job = _make_job("jittered", last_status="ok", last_error=None)
        svc._jobs = [job]
        claim = svc._claim_run("jittered", "scheduled")

        async def _stopped_in_jitter(_seconds: float) -> None:
            raise asyncio.CancelledError

        with (
            patch.object(svc, "_merge_job_result") as mock_merge,
            patch.object(svc, "_compute_jitter", return_value=42.0),
            patch.object(cron_module, "_sleep_out_jitter", _stopped_in_jitter),
            pytest.raises(asyncio.CancelledError),
        ):
            await svc._run_job_isolated(job, claim)

        on_job.assert_not_awaited()
        runs, total = await svc._history.get_job_history("jittered")
        assert total == 1
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["error"] == "Interrupted: the cron service stopped before this run started"
        assert mock_merge.call_args.args[0].last_status == "ok"

    @pytest.mark.asyncio
    async def test_a_run_stopped_after_its_marker_before_execute_keeps_the_previous_outcome(
        self, tmp_path: object
    ) -> None:
        """Past the jitter wait but before ``_execute`` resets the outcome, the
        job still holds the previous run's fields. A stop there (here, inside
        the timeout wrapper) has no outcome of its own either."""
        on_job = AsyncMock()
        svc = CronService(base_dir=None, on_job=on_job)
        svc._history = CronHistoryStore(base_dir=tmp_path)
        job = _make_job("premarker", last_status="ok", last_error=None)
        svc._jobs = [job]
        claim = svc._claim_run("premarker", "scheduled")

        with (
            patch.object(svc, "_merge_job_result") as mock_merge,
            patch.object(svc, "_compute_jitter", return_value=0),
            patch.object(
                svc, "_execute_with_timeout", AsyncMock(side_effect=asyncio.CancelledError)
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await svc._run_job_isolated(job, claim)

        on_job.assert_not_awaited()
        runs, _ = await svc._history.get_job_history("premarker")
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["error"] == "Interrupted: the cron service stopped before this run started"
        assert mock_merge.call_args.args[0].last_status == "ok"

    @pytest.mark.asyncio
    async def test_a_stopped_run_that_already_failed_keeps_its_own_failure(
        self, tmp_path: object
    ) -> None:
        """A command that marked its run failed and is then stopped while its
        failure alert goes out has an outcome of its own: the row and the job
        keep that failure, not the previous run's success."""

        async def _fail_then_stopped(j: CronJob) -> None:
            j.last_status = "error"
            j.last_error = "exit 1"
            raise asyncio.CancelledError

        svc = CronService(base_dir=None, on_job=_fail_then_stopped)
        svc._history = CronHistoryStore(base_dir=tmp_path)
        job = _make_job("failed", last_status="ok", last_error=None)
        svc._jobs = [job]
        claim = svc._claim_run("failed", "scheduled")

        with (
            patch.object(svc, "_merge_job_result") as mock_merge,
            patch.object(svc, "_compute_jitter", return_value=0),
            pytest.raises(asyncio.CancelledError),
        ):
            await svc._run_job_isolated(job, claim)

        runs, _ = await svc._history.get_job_history("failed")
        assert (runs[0]["status"], runs[0]["error"]) == ("failure", "exit 1")
        merged = mock_merge.call_args.args[0]
        assert (merged.last_status, merged.last_error) == ("error", "exit 1")

    @pytest.mark.asyncio
    async def test_a_started_one_shot_stopped_mid_callback_is_not_handed_an_old_error(
        self, tmp_path: object
    ) -> None:
        """A ``delete_after_run`` one-shot whose earlier run failed (then was
        resumed) is consumed or parked from THIS run's ``last_status``. A
        restored "error" would leave a started one-shot neither consumed nor
        parked, so the merge must see this run's blank status, as on main."""
        svc = CronService(base_dir=None, on_job=AsyncMock(side_effect=asyncio.CancelledError))
        svc._history = CronHistoryStore(base_dir=tmp_path)
        job = _make_job(
            "oneshot",
            last_status="error",
            last_error="exit 1",
            delete_after_run=True,
        )
        job.schedule = CronSchedule(kind="at", at_ts=time.time() - 60)
        svc._jobs = [job]
        claim = svc._claim_run("oneshot", "scheduled")

        with (
            patch.object(svc, "_merge_job_result") as mock_merge,
            patch.object(svc, "_compute_jitter", return_value=0),
            pytest.raises(asyncio.CancelledError),
        ):
            await svc._run_job_isolated(job, claim)

        runs, _ = await svc._history.get_job_history("oneshot")
        assert runs[0]["status"] == "cancelled"
        assert mock_merge.call_args.args[0].last_status is None

    @pytest.mark.asyncio
    async def test_execute_does_not_carry_the_previous_error_into_a_new_run(self) -> None:
        """``_execute`` resets ``last_error`` with ``last_status``: a callback that
        reports an error status without text must not inherit the old text."""

        async def _fails_without_text(j: CronJob) -> None:
            j.last_status = "error"

        svc = CronService(base_dir=None, on_job=_fails_without_text)
        job = _make_job("silent", last_status="error", last_error="last week's failure")
        await svc._execute(job)
        assert job.last_status == "error"
        assert job.last_error is None

    @pytest.mark.asyncio
    async def test_cancel_ends_every_key_of_the_run_not_only_the_newest(
        self, tmp_path: object
    ) -> None:
        """A sequential run's earlier agent, kept alive for its sub-agents, is ended beside the hung newest.

        Mirrors the reaper (``TestEveryKeyOfTheRunIsEnded``): both keys the run
        registered are reset and their processes killed, newest first, and the
        audit lists every key ended. A cancel that ended the newest key alone
        recorded ``cancelled`` while the earlier agent's session still ran.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        svc._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
        job = _make_job("seq1")
        svc._jobs = [job]
        task = MagicMock(done=MagicMock(return_value=False))
        claim = svc._claims["seq1"] = _RunClaim(
            trigger="manual", claimed_at=time.time() - 42, task=task
        )
        for key, pid in (("cron:seq1:agentA", 6001), ("cron:seq1:agentB", 6002)):
            svc.register_active_session_key("seq1", key)
            client = MagicMock()
            client._pid = pid
            client._child_pids = {}
            client._start_time = "4821903"
            session = MagicMock()
            session.provider._client = client
            svc._sessions._sessions[key] = session

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="4821903"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=False),
            _isolated_leader(),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            assert await svc.cancel("seq1") is True

        assert [called.args[0] for called in group_kill.call_args_list] == [6002, 6001], (
            "cancel ended only the newest agent's key and recorded cancelled while the earlier "
            "agent's session, kept alive for its pending sub-agents, still runs"
        )
        assert [called.args[0] for called in svc._sessions.reset.await_args_list] == [
            "cron:seq1:agentB",
            "cron:seq1:agentA",
        ]
        assert claim.taken and "seq1" not in svc._claims
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["tool_name"] == "cron_cancel" and audit["outcome"] == "cancelled"
        assert audit["session_key"] == "cron:seq1:agentB"
        assert audit["metadata"]["session_keys"] == ["cron:seq1:agentB", "cron:seq1:agentA"]


class TestSubprocessRegistry:
    """cron_script running-subprocess registry + kill."""

    @pytest.mark.asyncio
    async def test_sigkill_session_guard_refusal_still_kills_pid(self):
        """Reaper: when no isolated group could be captured for the leader (its
        group read is not its own pid -- init's, a foreign group), the runaway
        process must still be reaped via a pid-scoped kill of the identity-verified
        pid (never a group signal, and never a group resolved from the pid at
        signal time), and that scoped kill counts as delivered (no failure)."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        client = MagicMock()
        client._pid = 2**22 + 777  # valid int pid
        client._child_pids = {}
        client._start_time = "12345"
        session = MagicMock()
        session.provider._client = client
        sessions = MagicMock()
        sessions._sessions = {"cron:guard": session}
        svc._sessions = sessions

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="12345"),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("os.getpgid", return_value=1, create=True),
            patch(
                "kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)
            ) as pid_kill,
        ):
            handles = svc._session_process_handles("cron:guard")
            assert handles and handles[0].pgid is None, "a foreign group was captured"
            handle = handles[0]
            assert await svc._sigkill_session("cron:guard", handle) is None

        # ``kill_process_group`` is pinned to a refusal for the module: had a group
        # signal been attempted it would have surfaced as a failure above.
        pid_kill.assert_awaited_once_with(2**22 + 777, platform_compat.SIGKILL)

    def test_kill_unknown_job_returns_false(self) -> None:
        assert kill_running_process("no-such-job") is False

    def test_run_command_sandboxed_can_be_cancelled_mid_run(self, tmp_path, monkeypatch) -> None:
        """Real end-to-end: a sleeping command is SIGTERMed mid-run.

        Sandbox wrapping is patched to identity: builder-fleet hosts don't
        reliably support the namespace/cgroup sandbox (the wrapped child can
        fail instantly or spawn slowly, racing the registry check — this
        flaked the Dry Run Build on Py3.10). The registry/kill mechanics are
        what's under test here; the real sandboxed path is covered by pod e2e.
        """
        # ``run_command_sandboxed`` has no cwd parameter -- the command runs
        # where the gateway runs -- so the child inherits this process's CWD.
        # Under pytest that is the checkout; pin it to the test's own directory
        # for the spawn (restored by the fixture after the thread is joined).
        monkeypatch.chdir(tmp_path)
        result: dict = {}

        def _run() -> None:
            result.update(run_command_sandboxed("sleep 30", timeout=60, job_id="cancelme"))

        with (
            patch(
                "kiro_crew.cron_script.wrap_argv",
                # ``**k``, not a fixed ``mode``: this passthrough stands in for whatever
                # keyword set the caller uses, so it pins the identity behaviour under test
                # and not the wrap's signature.
                side_effect=lambda argv, **k: (argv, None),
            ),
            patch("kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: argv),
            patch(
                # The shell probe (_resolve_command_shell) also calls wrap_argv to
                # sandbox-route its POSIX-strict test. On macOS where /bin/sh is bash
                # the probe fails (SandboxUnavailableError or brace-expansion detected)
                # and returns None, aborting before the subprocess is spawned. Patch
                # the resolver to return a known-good shell so the registry/cancel
                # mechanics under test can actually run.
                "kiro_crew.cron_script._resolve_command_shell",
                return_value="/bin/sh",
            ),
        ):
            t = threading.Thread(target=_run)
            t.start()
            # Wait for the subprocess to register.
            wait_until(
                lambda: "cancelme" in _RUNNING_PROCS,
                describe=lambda: f"running={list(_RUNNING_PROCS)}",
            )
            assert "cancelme" in _RUNNING_PROCS
            started = time.time()
            assert kill_running_process("cancelme") is True
            # Poll for thread death (same pattern as the registration wait
            # above) instead of one fixed-budget join: an instantaneous
            # is_alive() read behind a single join can report a still-dying
            # thread on a loaded runner even when SIGTERM worked. The 20s
            # deadline stays comfortably below the child's 30s sleep, so
            # passing still proves death-by-cancellation, not natural expiry.
            deadline = started + 20
            while time.time() < deadline and t.is_alive():
                t.join(timeout=0.1)
        assert not t.is_alive(), "thread still alive 20s after SIGTERM"
        # 25, not 20: the final join may return ~0.1s past the poll deadline
        # with the thread already dead; the headroom keeps that success from
        # failing here while staying well below the 30s natural expiry.
        assert time.time() - started < 25  # died well before the 30s sleep
        assert result["status"] == "cancelled"
        assert "cancelme" not in _RUNNING_PROCS
        assert "cancelme" not in _CANCELLED_PROC_JOBS  # flag consumed

    def test_run_command_without_job_id_not_registered(
        self, posix_test_shell, tmp_path, monkeypatch
    ) -> None:
        # Patch the sandbox wrap to identity for the same reason as the mid-run
        # test above: GH Actions blocks the namespace sandbox (unshare NEWNS),
        # so the real launcher aborts with status "error". What's under test is
        # that a job_id-less run is NOT added to the registry — mechanics that
        # don't need the sandbox.
        monkeypatch.chdir(tmp_path)  # the spawn inherits CWD; see the test above
        with (
            patch(
                "kiro_crew.cron_script.wrap_argv",
                # ``**k`` for the reason the test above gives: a passthrough pins the
                # identity behaviour, not the keyword set the caller passes.
                side_effect=lambda argv, **k: (argv, None),
            ),
            patch("kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: argv),
            patch(
                # Bypass the runtime shell probe (which itself spawns a child) — the
                # test is about the registry, not shell fingerprinting.
                "kiro_crew.cron_script._resolve_command_shell",
                return_value=posix_test_shell,
            ),
        ):
            result = run_command_sandboxed("echo hi", timeout=10)
        assert result["status"] == "ok"
        assert not _RUNNING_PROCS


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/crons/{job_id}/cancel", api_cron_cancel)
    return app


def _make_state(job: CronJob | None, cancel_result: bool = True) -> MagicMock:
    state = MagicMock()
    state.crons = MagicMock()
    state.crons.list_jobs.return_value = [job] if job else []
    state.crons.cancel = AsyncMock(return_value=cancel_result)
    state.push_refresh = MagicMock()
    return state


class TestApiCronCancel:
    """POST /api/crons/{id}/cancel handler."""

    @pytest.mark.asyncio
    async def test_cancel_running_job_ok(self) -> None:
        state = _make_state(_make_job("j1", name="etl job"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/j1/cancel")
            assert resp.status == 200
            data = await resp.json()
        assert data["ok"] is True
        state.crons.cancel.assert_awaited_once_with("j1")
        state.push_refresh.assert_called_with("crons")

    @pytest.mark.asyncio
    async def test_cancel_unknown_job_404(self) -> None:
        state = _make_state(None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/ghost/cancel")
            assert resp.status == 404
        state.crons.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_idle_job_409(self) -> None:
        state = _make_state(_make_job("j2"), cancel_result=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/j2/cancel")
            assert resp.status == 409
            data = await resp.json()
        assert "not running" in data["error"]

    @pytest.mark.asyncio
    async def test_cancel_whose_pause_was_not_saved_409(self) -> None:
        state = _make_state(_make_job("j3"))
        state.crons.cancel = AsyncMock(
            side_effect=cron_module.CronPauseNotSaved("this one-shot may run again")
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/j3/cancel")
            assert resp.status == 409
            data = await resp.json()
        assert data == {
            "error": "this one-shot may run again",
            "code": "cron_pause_not_saved",
            "retryable": False,
        }
        state.push_refresh.assert_called_with("crons")


_ADMITTED = SimpleNamespace(admitted=True, reason="")


async def _start_without_timer(svc: CronService) -> None:
    """Start ``svc`` with its real timer retired, so the test's own ticks are the only ones."""
    await svc.start()
    svc._arm_timer = lambda: None  # type: ignore[method-assign]
    task, svc._timer_task = svc._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await await_until(task, what="timer task")


async def _cancel_mid_run_then_tick(svc: CronService, job_id: str, started: asyncio.Event) -> None:
    """Tick once, cancel the run while its callback is blocked, then tick again.

    The tick takes a run's claim before it returns, so the claim table is the
    dispatch record: a second claim after the cancel is the job running again.
    """
    with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
        await svc._on_timer()
        await await_until(started, what="started")
        assert await svc.cancel(job_id) is True
        assert job_id not in svc._claims, "cancel left the first run's claim in place"
        await svc._on_timer()  # the next tick
        _assert_not_redispatched(svc, job_id)


def _assert_not_redispatched(svc: CronService, job_id: str) -> None:
    """The tick takes a run's claim before it returns, so a claim here is a re-run."""
    assert job_id not in svc._claims, "the tick after the cancel dispatched the one-shot again"


_HELD_MESSAGE = (
    "The run was cancelled, but its pause could not be saved, so this one-shot may run again"
    " once the cron store is reloaded or the gateway restarts. Delete it if it must not run"
    " again."
)
_UNREAD_NOTE = (
    "; the pause could not be saved (the cron store could not be read),"
    " so this one-shot may run again"
)
_NEWER_RUN = "the cron store already holds a newer run of the job"
_NOT_ONE_SHOT = "the job is no longer a one-shot in the cron store"
_ENOSPC = "OSError: [Errno 28] No space left on device"
_ON_SCHEDULE_MESSAGE = (
    "The run was cancelled, but its pause could not be saved: a newer run of the job was"
    " recorded meanwhile, so the job is back on its schedule now. Pause or delete it if it"
    " must not run again."
)
_RECURRING_MESSAGE = (
    "The run was cancelled, but its pause could not be saved: the job was changed to a"
    " recurring schedule meanwhile and runs on that schedule now. Pause or delete it if it"
    " must not run again."
)


def _on_schedule(reason: str) -> tuple[str, str, tuple[str, bool, bool]]:
    note = f"; the pause could not be saved ({reason}), so the job is back on its schedule now"
    return note, _ON_SCHEDULE_MESSAGE, ("at", True, False)


def _recurring(reason: str) -> tuple[str, str, tuple[str, bool, bool]]:
    note = (
        f"; the pause could not be saved ({reason}), so the job runs on its recurring schedule now"
    )
    return note, _RECURRING_MESSAGE, ("every", True, False)


def _paused(reason: str, kind: str) -> tuple[str, None, tuple[str, bool, bool]]:
    return (
        f"; the pause could not be saved ({reason}), and the job is paused",
        None,
        (kind, False, True),
    )


def _held(reason: str) -> tuple[str, str, tuple[str, bool, bool]]:
    note = f"; the pause could not be saved ({reason}), so this one-shot may run again"
    return note, _HELD_MESSAGE, ("at", False, False)


_BUSY = "CronStoreBusy: cron store busy"
_REMOVED = ("; the job was removed from the cron store meanwhile", None, None)


# For each end of the cancel's save: the history entry after "Cancelled by user after
# Ns", the ``CronPauseNotSaved`` message (None: ``cancel`` returns True), and the live
# job's (kind, enabled, user_paused) after the cancel (None: gone from memory). The
# texts follow the live job, whichever process wrote the store meanwhile.
_CANCEL_REPORTS: dict[str | None, tuple[str, str | None, tuple[str, bool, bool] | None]] = {
    None: ("", None, ("at", False, True)),
    "malformed": (_UNREAD_NOTE, _HELD_MESSAGE, None),
    "unreadable": (_UNREAD_NOTE, _HELD_MESSAGE, ("at", False, False)),
    "removed": _REMOVED,
    "newer_run": _on_schedule(_NEWER_RUN),
    "recurring": _recurring(_NOT_ONE_SHOT),
    "write_error": _held(_ENOSPC),
    "manual_run": _paused(_NEWER_RUN, "at"),
    "newer_run_here": _on_schedule(_NEWER_RUN),
    "recurring_here": _recurring(_NOT_ONE_SHOT),
    "recurring_paused": _paused(_NOT_ONE_SHOT, "every"),
    "paused_then_write_error": _paused(_ENOSPC, "at"),
    "recurring_then_write_error": _recurring(_ENOSPC),
    "recurring_here_then_write_error": _recurring(_ENOSPC),
    # A busy store raises before the merge runs, so ``cancel`` applies its rules.
    "recurring_here_then_busy": _recurring(_BUSY),
    "removed_here_then_busy": _REMOVED,
    "resumed_here_then_busy": _held(_BUSY),
    "newer_run_here_then_busy": _on_schedule(_BUSY),
    "reloaded_then_busy": _held(_BUSY),
    # A failed park answers success only when the saved store shows the job cannot
    # run again: a pause or a removal this process could not save is not proof.
    "pause_unsaved_then_write_error": (_held(_ENOSPC)[0], _HELD_MESSAGE, ("at", False, True)),
    "paused_here_then_write_error": _paused(_ENOSPC, "at"),
    "removed_unsaved_then_busy": (_held(_BUSY)[0], _HELD_MESSAGE, None),
    "paused_here_then_unreadable": (_UNREAD_NOTE, _HELD_MESSAGE, ("at", False, True)),
}
# Ends where another writer paused the job meanwhile: ``user_paused`` on disk is theirs.
_PAUSED_MEANWHILE = (
    "manual_run",
    "recurring_paused",
    "paused_then_write_error",
    "paused_here_then_write_error",
    "paused_here_then_unreadable",
)
# Ends where the store changes in the gap and the cancel's save then fails.
_THEN_WRITE_ERROR = (
    "paused_then_write_error",
    "recurring_then_write_error",
    "recurring_here_then_write_error",
)
# Ends where this process changes the job, or reloads the store, in the gap, and
# the cancel's merge then meets a busy store.
_THEN_BUSY = (
    "recurring_here_then_busy",
    "removed_here_then_busy",
    "resumed_here_then_busy",
    "newer_run_here_then_busy",
    "reloaded_then_busy",
)


class TestCancelledOneShotIsParked:
    """A cancelled one-shot ``at`` job is parked disabled, so the next tick does not run it again."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("delete_after_run", "pause"),
        # (enabled, auto_paused, user_paused) on disk after the cancel. A
        # delete_after_run one-shot is left to main's failed-run auto-pause,
        # which races the cancel's store write, so it is not a case here.
        [(False, (False, False, True))],
    )
    async def test_cancelled_one_shot_does_not_run_again(
        self, tmp_path, delete_after_run: bool, pause: tuple[bool, bool, bool]
    ) -> None:
        runs: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_job(job: CronJob) -> None:
            runs.append(job.id)
            started.set()
            await await_until(release, what="release")

        svc = CronService(base_dir=tmp_path, on_job=on_job)
        await _start_without_timer(svc)
        job = await svc.add_job_async(
            "remind-once", "msg", at_ts=time.time() - 1, delete_after_run=delete_after_run
        )
        try:
            await _cancel_mid_run_then_tick(svc, job.id, started)
        finally:
            release.set()
            await svc.stop()
        assert runs == [job.id], f"cancelled one-shot ran {len(runs)} times"

        # Parked on disk, not deleted: a fresh service finds it disabled and paused
        # by the user, and its tick does not run it.
        reloaded = CronService(base_dir=tmp_path, on_job=on_job)
        await _start_without_timer(reloaded)
        try:
            parked = reloaded.get_job(job.id)
            assert parked is not None
            assert (parked.enabled, parked.auto_paused, parked.user_paused) == pause
            assert parked.last_error.startswith("Cancelled by user")
            with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
                await reloaded._on_timer()
            assert job.id not in reloaded._claims
        finally:
            await reloaded.stop()
        assert runs == [job.id]

    @staticmethod
    def _busy_lock(svc: CronService, busy_calls: list[int], timeouts: list[float]) -> None:
        """Make ``svc``'s store lock raise the real ``CronStoreBusy`` while ``busy_calls[0]`` > 0.

        Each refused entry uses up one call; ``-1`` keeps the store busy until the
        test sets it to 0. Every entry's lock wait is recorded in ``timeouts``.
        """
        real_lock = svc._file_lock

        @contextlib.contextmanager
        def _lock(*args: Any, **kwargs: Any):
            timeouts.append(kwargs.get("timeout", cron_module._FILE_LOCK_TIMEOUT_SECS))
            if busy_calls[0]:
                busy_calls[0] -= busy_calls[0] > 0
                raise CronStoreBusy("cron store busy")
            with real_lock(*args, **kwargs):
                yield

        svc._file_lock = _lock  # type: ignore[method-assign]

    @staticmethod
    def _on_disk(tmp_path, job_id: str) -> tuple[bool, bool]:
        """(enabled, user_paused) as a fresh service reads the store file."""
        fresh = CronService(base_dir=tmp_path)
        fresh._load()
        stored = fresh.get_job(job_id)
        assert stored is not None
        return stored.enabled, stored.user_paused

    @pytest.mark.asyncio
    async def test_a_busy_store_reports_the_pause_not_saved(self, tmp_path) -> None:
        """A busy store: one save at the store's standard lock wait, with no retry.
        ``user_paused`` stays off, the caller and the history say the pause was not
        saved, the hold keeps the next tick off, and no later tick writes the park."""
        runs: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_job(job: CronJob) -> None:
            runs.append(job.id)
            started.set()
            await await_until(release, what="release")

        svc = CronService(base_dir=tmp_path, on_job=on_job)
        await _start_without_timer(svc)
        job = await svc.add_job_async("remind-once", "msg", at_ts=time.time() - 1)
        busy_calls, timeouts = [0], []
        self._busy_lock(svc, busy_calls, timeouts)
        try:
            with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
                await svc._on_timer()
                await await_until(started, what="started")
                busy_calls[0], timeouts[:] = -1, []
                with pytest.raises(cron_module.CronPauseNotSaved, match="may run again"):
                    await finished(svc.cancel(job.id), "svc.cancel")
                one_try = [cron_module._FILE_LOCK_TIMEOUT_SECS]
                assert timeouts == one_try, f"one save at the standard lock wait, not {timeouts}"
                assert job.id not in svc._claims, "cancel left the first run's claim in place"
                live = svc.get_job(job.id)
                assert live is not None
                assert live.user_paused is False, "a pause the store did not save is reported"
                assert self._on_disk(tmp_path, job.id) == (True, False)
                history, _total = await svc._history.get_job_history(job.id)
                error = history[0]["error"]
                assert error.startswith("Cancelled by user"), error
                assert "the pause could not be saved (CronStoreBusy" in error, error
                assert "may run again" in error, error
                await svc._on_timer()  # the store is still busy: the hold keeps the job off
                _assert_not_redispatched(svc, job.id)
                busy_calls[0] = 0
                await svc._on_timer()  # this tick reaches the store, and writes no park
                _assert_not_redispatched(svc, job.id)
        finally:
            release.set()
            await svc.stop()
        assert runs == [job.id], f"cancelled one-shot ran {len(runs)} times"
        assert self._on_disk(tmp_path, job.id) == (True, False), "a later write parked the job"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("change", "reason"),
        # What happens to the store just before the cancel's save, and why the
        # pause is then reported not saved (None: a write holding it returned).
        [
            (None, None),
            ("malformed", "the cron store could not be read"),
            ("unreadable", "the cron store could not be read"),
            ("removed", "the job is no longer in the cron store"),
            ("newer_run", _NEWER_RUN),
            ("recurring", _NOT_ONE_SHOT),
            ("write_error", _ENOSPC),
            ("manual_run", _NEWER_RUN),
            ("newer_run_here", _NEWER_RUN),
            ("recurring_here", _NOT_ONE_SHOT),
            ("recurring_paused", _NOT_ONE_SHOT),
            ("paused_then_write_error", _ENOSPC),
            ("recurring_then_write_error", _ENOSPC),
            ("recurring_here_then_write_error", _ENOSPC),
            ("recurring_here_then_busy", _BUSY),
            ("removed_here_then_busy", _BUSY),
            ("resumed_here_then_busy", _BUSY),
            ("newer_run_here_then_busy", _BUSY),
            ("reloaded_then_busy", _BUSY),
            ("pause_unsaved_then_write_error", _ENOSPC),
            ("paused_here_then_write_error", _ENOSPC),
            ("removed_unsaved_then_busy", _BUSY),
            ("paused_here_then_unreadable", "the cron store could not be read"),
        ],
    )
    async def test_the_pause_is_reported_saved_only_once_a_write_holding_it_returned(
        self, tmp_path, change: str | None, reason: str | None
    ) -> None:
        """Every way the cancel's save can end. Only a store write that holds the park
        and returned counts as a saved pause. Every other end is worded from the live
        job after the merge: a job removed or paused meanwhile has nothing left to
        pause, so that cancel is not reported as a failed pause."""
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def on_job(job: CronJob) -> None:
            calls.append(job.id)
            if len(calls) == 1:  # the run the test cancels; a manual Run returns at once
                started.set()
                await await_until(release, what="release")

        svc = CronService(base_dir=tmp_path, on_job=on_job)
        await _start_without_timer(svc)
        job = await svc.add_job_async("remind-once", "msg", at_ts=time.time() - 1)
        store = tmp_path / "crons.json"
        real_merge = svc._merge_terminal_state_locked
        real_read = Path.read_bytes

        def unreadable(path: Path) -> bytes:
            if path == store:
                raise OSError(errno.EIO, "Input/output error")
            return real_read(path)

        real_read_text = Path.read_text
        faults = contextlib.ExitStack()

        def unreadable_text(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == store:
                raise OSError(errno.EIO, "Input/output error")
            return real_read_text(path, *args, **kwargs)

        def merge_after_the_change(*args: Any, **kwargs: Any) -> Any:
            # On the cancel's worker thread, just before its own save.
            if change == "malformed":
                store.write_text("{not json", encoding="utf-8")
            elif change == "removed":
                other = CronService(base_dir=tmp_path)
                assert other.remove_job(job.id, actor="test", source="test")
            elif change == "newer_run":
                CronService(base_dir=tmp_path)._merge_terminal_state_locked(
                    job.id, last_status="ok", last_error="", last_run_ts=1.0, run_generation=10**6
                )
            elif change == "recurring":
                assert CronService(base_dir=tmp_path).update_job(job.id, every_secs=3600)
            elif change == "unreadable":
                with patch.object(Path, "read_bytes", unreadable):
                    return real_merge(*args, **kwargs)
            elif change == "write_error":
                with patch.object(svc, "_save", side_effect=OSError(28, "No space left on device")):
                    return real_merge(*args, **kwargs)
            elif change == "manual_run":
                # A manual Run in this process, accepted once the cancel released
                # the claim, completes and merges its record first.
                assert LoopHop(loop).run(manual(), "manual run")
            elif change == "newer_run_here":
                # A newer terminal record merged by this process, as its reaper writes.
                real_merge(
                    job.id,
                    last_status="error",
                    last_error="",
                    last_run_ts=1.0,
                    run_generation=10**6,
                )
            elif change == "recurring_here":
                assert svc.update_job(job.id, every_secs=3600)
            elif change == "recurring_paused":
                other = CronService(base_dir=tmp_path)
                assert other.update_job(job.id, every_secs=3600)
                assert other.enable_job(job.id, False)
            elif change in _THEN_WRITE_ERROR:
                # The store changes in the gap, the merge reloads it (or, for this
                # process's own change, already holds it), and then its save fails.
                if change == "paused_then_write_error":
                    assert CronService(base_dir=tmp_path).enable_job(job.id, False)
                elif change == "recurring_then_write_error":
                    assert CronService(base_dir=tmp_path).update_job(job.id, every_secs=3600)
                else:
                    assert svc.update_job(job.id, every_secs=3600)
                with patch.object(svc, "_save", side_effect=OSError(28, "No space left on device")):
                    return real_merge(*args, **kwargs)
            elif change in _THEN_BUSY:
                # This process changes the job, or reloads a store another process
                # wrote, in the gap; then the lock is busy, so the merge never runs.
                if change == "recurring_here_then_busy":
                    assert svc.update_job(job.id, every_secs=3600)
                elif change == "removed_here_then_busy":
                    assert svc.remove_job(job.id, actor="test", source="test")
                elif change == "resumed_here_then_busy":
                    assert svc.enable_job(job.id, True)
                elif change == "newer_run_here_then_busy":
                    real_merge(
                        job.id,
                        last_status="error",
                        last_error="",
                        last_run_ts=1.0,
                        run_generation=10**6,
                    )
                else:
                    CronService(base_dir=tmp_path).add_job("unrelated", "msg", every_secs=3600)
                    svc._tick_scan_locked()  # the tick's reload drops the in-memory hold
                with patch.object(svc, "_file_lock", side_effect=CronStoreBusy("cron store busy")):
                    return real_merge(*args, **kwargs)
            elif change == "pause_unsaved_then_write_error":
                # This process's pause fails to save, and so does the cancel's park.
                with patch.object(svc, "_save", side_effect=OSError(28, "No space left on device")):
                    with pytest.raises(OSError):
                        svc.enable_job(job.id, False)
                    return real_merge(*args, **kwargs)
            elif change == "paused_here_then_write_error":
                assert svc.enable_job(job.id, False)
                with patch.object(svc, "_save", side_effect=OSError(28, "No space left on device")):
                    return real_merge(*args, **kwargs)
            elif change == "removed_unsaved_then_busy":
                with patch.object(svc, "_save", side_effect=OSError(28, "No space left on device")):
                    with pytest.raises(OSError):
                        svc.remove_job(job.id, actor="test", source="test")
                with patch.object(svc, "_file_lock", side_effect=CronStoreBusy("cron store busy")):
                    return real_merge(*args, **kwargs)
            elif change == "paused_here_then_unreadable":
                # A saved pause, then the store cannot be read, through the cancel's end.
                assert svc.enable_job(job.id, False)
                faults.enter_context(patch.object(Path, "read_bytes", unreadable))
                faults.enter_context(patch.object(Path, "read_text", unreadable_text))
            return real_merge(*args, **kwargs)

        async def manual() -> bool:
            return await svc.run_job(job.id)

        loop = asyncio.get_running_loop()
        svc._merge_terminal_state_locked = merge_after_the_change  # type: ignore[method-assign]
        note, message, live_after = _CANCEL_REPORTS[change]
        try:
            with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
                await svc._on_timer()
                await await_until(started, what="started")
                if message is None:
                    assert await finished(svc.cancel(job.id), "svc.cancel") is True
                else:
                    with pytest.raises(cron_module.CronPauseNotSaved) as raised:
                        await finished(svc.cancel(job.id), "svc.cancel")
                    assert str(raised.value) == message
            live = svc.get_job(job.id)
            history, _total = await svc._history.get_job_history(job.id)
        finally:
            faults.close()
            release.set()
            await svc.stop()
        error = history[0]["error"]
        assert re.fullmatch(r"Cancelled by user after \d+s" + re.escape(note), error), error
        if reason is None:
            assert self._on_disk(tmp_path, job.id) == (False, True)
        elif change == "malformed":
            assert store.read_text(encoding="utf-8") == "{not json", "a write replaced the store"
        elif change in ("removed", "removed_here_then_busy"):
            assert CronService(base_dir=tmp_path).get_job(job.id) is None
        else:
            paused_meanwhile = change in _PAUSED_MEANWHILE
            assert self._on_disk(tmp_path, job.id)[1] is paused_meanwhile, "the park is on disk"
        live_state = None if live is None else (live.schedule.kind, live.enabled, live.user_paused)
        assert live_state == live_after, f"{change}: the report does not match the live job"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_another_process_s_choice_after_a_failed_cancel_is_kept(
        self, tmp_path, enabled: bool
    ) -> None:
        """A CLI-style service resumes or pauses the job after a cancel whose park did not
        land; the gateway's next store pass and its next save keep that choice."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_job(job: CronJob) -> None:
            started.set()
            await await_until(release, what="release")

        svc = CronService(base_dir=tmp_path, on_job=on_job)
        await _start_without_timer(svc)
        job = await svc.add_job_async("remind-once", "msg", at_ts=time.time() - 1)
        busy_calls: list[int] = [0]
        self._busy_lock(svc, busy_calls, [])
        try:
            with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
                await svc._on_timer()
                await await_until(started, what="started")
                busy_calls[0] = -1
                # Test B checks the report; this one checks only what the store keeps.
                with contextlib.suppress(RuntimeError):
                    await finished(svc.cancel(job.id), "svc.cancel")
                busy_calls[0] = 0
                cli = CronService(base_dir=tmp_path)
                cli._load()
                assert await asyncio.to_thread(cli.enable_job, job.id, enabled) is True
                await asyncio.to_thread(svc._tick_scan_locked)
                await svc.add_job_async("unrelated", "msg", every_secs=3600)
        finally:
            release.set()
            await svc.stop()
        assert self._on_disk(tmp_path, job.id) == (enabled, not enabled)

    @pytest.mark.asyncio
    async def test_cancelled_recurring_job_keeps_its_schedule(self, tmp_path) -> None:
        """Only a one-shot is parked: a cancelled ``every`` job stays enabled for its next slot."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_job(job: CronJob) -> None:
            started.set()
            await await_until(release, what="release")

        svc = CronService(base_dir=tmp_path, on_job=on_job)
        await _start_without_timer(svc)
        job = await svc.add_job_async("every-hour", "msg", every_secs=3600, strict_schedule=True)
        live = svc.get_job(job.id)
        assert live is not None
        live.created_ts = live.last_run_ts = time.time() - 7200  # due on the first tick
        try:
            with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
                await svc._on_timer()
                await await_until(started, what="started")
                assert await svc.cancel(job.id) is True
        finally:
            release.set()
            await svc.stop()

        reloaded = CronService(base_dir=tmp_path, on_job=on_job)
        reloaded._load()
        kept = reloaded.get_job(job.id)
        assert kept is not None
        assert (kept.enabled, kept.user_paused) == (True, False)
