"""Tests for user-initiated cancellation of running cron executions.

Covers CronService.cancel() (agent + script/command paths), the
subprocess registry in cron_script, real mid-run cancellation of
run_command_sandboxed, and the POST /api/crons/{id}/cancel handler.
"""

from __future__ import annotations

import asyncio
import threading
import time
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

from kiro_crew import platform_compat
from kiro_crew.cron import CronJob, CronSchedule, CronService, _RunClaim
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.cron_script import (
    _CANCELLED_PROC_JOBS,
    _RUNNING_PROCS,
    kill_running_process,
    run_command_sandboxed,
)
from kiro_crew.dashboard.handlers.cron import api_cron_cancel


@pytest.fixture(autouse=True)
def _kill_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX-shaped kill path unless a test pins ``IS_WINDOWS``; every kill primitive refuses unless the test pins it (see test_cron_reaper.py)."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    for name in _KILL_PRIMITIVES:
        guard = _refuse_unpinned_signal_async if name.endswith("_async") else _refuse_unpinned_signal
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
        assert svc._cancelled_jobs.has("run1", claim)
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
        assert (
            mock_sel().log_tool_invocation.call_args.kwargs["tool_name"] == "cron_cancel"
        )

    @pytest.mark.asyncio
    async def test_cancel_script_job_kills_subprocess_not_session(
        self, tmp_path: object
    ) -> None:
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

        with patch(
            "kiro_crew.cron_script.kill_running_process", return_value=True
        ) as mock_kill, patch("kiro_crew.sel.sel") as mock_sel, patch.object(svc, "_save"):
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
    async def test_cancel_does_not_touch_consecutive_failures(
        self, tmp_path: object
    ) -> None:
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
        assert svc._cancelled_jobs.has("refused", claim)
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
            trigger="manual", claimed_at=time.time() - 42, task=MagicMock(done=MagicMock(return_value=False))
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="4821903"),
            _isolated_leader(),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
        ):
            assert await svc.cancel("popped") is True

        assert "cron:popped" not in svc._sessions._sessions, "the fixture did not pop the session"
        group_kill.assert_called_once_with(5252, platform_compat.SIGKILL)
        assert svc._cancelled_jobs.has("popped", claim)
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
            trigger="manual", claimed_at=time.time() - 42, task=MagicMock(done=MagicMock(return_value=False))
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
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
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
            trigger="manual", claimed_at=time.time() - 42, task=MagicMock(done=MagicMock(return_value=False))
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="4821903"),
            _isolated_leader(),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
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
            side_effect=lambda key: events.append(f"spawn in flight? {key}") or "refused at registration, its provider hard-killed there"
        )
        job = _make_job("fenced")
        svc._jobs = [job]
        svc._claims["fenced"] = _RunClaim(
            trigger="manual", claimed_at=time.time() - 42, task=MagicMock(done=MagicMock(return_value=False))
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
            trigger="manual", claimed_at=time.time() - 42, task=MagicMock(done=MagicMock(return_value=False))
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
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
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
        assert mock_sel().log_tool_invocation.call_args.kwargs["outcome"] == "failed", (
            "cancel recorded cancelled over a process it never answered"
        )
        assert 5656 in {s.provider._client._pid for s in svc._sessions._sessions.values()}

    @pytest.mark.asyncio
    async def test_run_job_isolated_skips_history_when_cancelled(
        self, tmp_path: object
    ) -> None:
        """The normal completion path must not double-record after cancel()."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        job = _make_job("run3")
        svc._jobs = [job]
        claim = svc._claim_run("run3", "manual")
        svc._cancelled_jobs.mark("run3", claim)

        with patch.object(svc, "_merge_job_result") as mock_merge:
            await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        _, total = await svc._history.get_job_history("run3")
        assert total == 0
        assert not svc._cancelled_jobs.has("run3", claim)  # flag consumed

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
            patch(
                "kiro_crew.platform_compat.kill_process_group", return_value=True
            ) as group_kill,
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

        with patch("kiro_crew.acp.client._get_child_pids", return_value=[]), \
             patch("kiro_crew.platform_compat.get_process_start_id", return_value="12345"), \
             patch("kiro_crew.acp.client._kill_escaped_children"), \
             patch("os.getpgid", return_value=1, create=True), \
             patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)) as pid_kill:
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

        with patch(
            "kiro_crew.cron_script.wrap_argv", side_effect=lambda argv, mode: (argv, None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: argv
        ), patch(
            # The shell probe (_resolve_command_shell) also calls wrap_argv to
            # sandbox-route its POSIX-strict test. On macOS where /bin/sh is bash
            # the probe fails (SandboxUnavailableError or brace-expansion detected)
            # and returns None, aborting before the subprocess is spawned. Patch
            # the resolver to return a known-good shell so the registry/cancel
            # mechanics under test can actually run.
            "kiro_crew.cron_script._resolve_command_shell", return_value="/bin/sh"
        ):
            t = threading.Thread(target=_run)
            t.start()
            # Wait for the subprocess to register.
            deadline = time.time() + 10
            while time.time() < deadline and "cancelme" not in _RUNNING_PROCS:
                time.sleep(0.05)
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
        with patch(
            "kiro_crew.cron_script.wrap_argv", side_effect=lambda argv, mode: (argv, None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: argv
        ), patch(
            # Bypass the runtime shell probe (which itself spawns a child) — the
            # test is about the registry, not shell fingerprinting.
            "kiro_crew.cron_script._resolve_command_shell", return_value=posix_test_shell
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
