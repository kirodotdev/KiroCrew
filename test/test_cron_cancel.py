"""Tests for user-initiated cancellation of running cron executions.

Covers CronService.cancel() (agent + script/command paths), the
subprocess registry in cron_script, real mid-run cancellation of
run_command_sandboxed, and the POST /api/crons/{id}/cancel handler.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.cron_script import (
    _CANCELLED_PROC_JOBS,
    _RUNNING_PROCS,
    kill_running_process,
    run_command_sandboxed,
)
from kiro_crew.dashboard.handlers.cron import api_cron_cancel


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    sessions._sessions = {}
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
        svc._executing.add("run1")
        svc._job_start_times["run1"] = time.time() - 42
        svc._job_run_meta["run1"] = (time.time() - 42, "manual")
        task = MagicMock(done=MagicMock(return_value=False))
        svc._running_tasks["run1"] = task
        refresh_calls: list[str] = []
        svc._push_refresh = refresh_calls.append

        with patch("kiro_crew.sel.sel") as mock_sel, patch.object(svc, "_save"):
            assert await svc.cancel("run1") is True

        assert job.last_status == "error"
        assert "Cancelled by user" in (job.last_error or "")
        assert "run1" in svc._cancelled_jobs
        assert "run1" not in svc._executing
        assert "run1" not in svc._job_start_times
        assert "run1" not in svc._running_tasks
        task.cancel.assert_called_once()
        sessions.reset.assert_awaited_once_with("cron:run1")
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
        """Script crons: subprocess is killed; no kiro-cli session reset."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        sessions = _mock_sessions()
        svc._sessions = sessions

        job = _make_job("script1", script="~/.kirocrew/crons/x.py:run")
        svc._jobs = [job]
        svc._executing.add("script1")
        svc._job_start_times["script1"] = time.time() - 10
        svc._running_tasks["script1"] = MagicMock(done=MagicMock(return_value=False))

        with patch(
            "kiro_crew.cron_script.kill_running_process", return_value=True
        ) as mock_kill, patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            assert await svc.cancel("script1") is True

        mock_kill.assert_called_once_with("script1")
        sessions.reset.assert_not_awaited()

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
        svc._executing.add("run2")
        svc._job_start_times["run2"] = time.time() - 5
        svc._running_tasks["run2"] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc.cancel("run2")

        assert job.consecutive_failures == 3
        assert job.enabled is True

    @pytest.mark.asyncio
    async def test_run_job_isolated_skips_history_when_cancelled(
        self, tmp_path: object
    ) -> None:
        """The normal completion path must not double-record after cancel()."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        job = _make_job("run3")
        svc._jobs = [job]
        svc._cancelled_jobs.add("run3")

        with patch.object(svc, "_merge_job_result") as mock_merge:
            await svc._run_job_isolated(job)

        mock_merge.assert_not_called()
        _, total = await svc._history.get_job_history("run3")
        assert total == 0
        assert "run3" not in svc._cancelled_jobs  # flag consumed


class TestSubprocessRegistry:
    """cron_script running-subprocess registry + kill."""

    @pytest.mark.asyncio
    async def test_sigkill_session_guard_refusal_still_kills_pid(self):
        """Reaper: when the broadcast guard refuses the pgid, the runaway
        process must still be reaped via a scoped os.kill (never killpg)."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        client = MagicMock()
        client._pid = 2**22 + 777  # valid int pid
        client._child_pids = {}
        client._start_time = 12345.0
        session = MagicMock()
        session.provider._client = client
        sessions = MagicMock()
        sessions._sessions = {"cron:guard": session}
        svc._sessions = sessions

        with patch("kiro_crew.acp.client._get_child_pids", return_value=[]), \
             patch("kiro_crew.acp.client._is_our_child", return_value=True), \
             patch("kiro_crew.acp.client._kill_escaped_children"), \
             patch("os.getpgid", return_value=1), \
             patch("os.killpg") as mock_killpg, \
             patch("os.kill") as mock_kill:
            await svc._sigkill_session("cron:guard")

        mock_killpg.assert_not_called()
        mock_kill.assert_called_once()
        assert mock_kill.call_args.args[0] == 2**22 + 777

    def test_kill_unknown_job_returns_false(self) -> None:
        assert kill_running_process("no-such-job") is False

    def test_run_command_sandboxed_can_be_cancelled_mid_run(self) -> None:
        """Real end-to-end: a sleeping command is SIGTERMed mid-run.

        Sandbox wrapping is patched to identity: builder-fleet hosts don't
        reliably support the namespace/cgroup sandbox (the wrapped child can
        fail instantly or spawn slowly, racing the registry check — this
        flaked the Dry Run Build on Py3.10). The registry/kill mechanics are
        what's under test here; the real sandboxed path is covered by pod e2e.
        """
        result: dict = {}

        def _run() -> None:
            result.update(run_command_sandboxed("sleep 30", timeout=60, job_id="cancelme"))

        with patch(
            "kiro_crew.cron_script.wrap_argv", side_effect=lambda argv, mode: (argv, None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: argv
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
            t.join(timeout=10)
        assert not t.is_alive()
        assert time.time() - started < 10  # died well before the 30s sleep
        assert result["status"] == "cancelled"
        assert "cancelme" not in _RUNNING_PROCS
        assert "cancelme" not in _CANCELLED_PROC_JOBS  # flag consumed

    def test_run_command_without_job_id_not_registered(self) -> None:
        # Patch the sandbox wrap to identity for the same reason as the mid-run
        # test above: GH Actions blocks the namespace sandbox (unshare NEWNS),
        # so the real launcher aborts with status "error". What's under test is
        # that a job_id-less run is NOT added to the registry — mechanics that
        # don't need the sandbox.
        with patch(
            "kiro_crew.cron_script.wrap_argv", side_effect=lambda argv, mode: (argv, None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: argv
        ), patch(
            # Bypass the runtime shell probe (which itself spawns a child) — the
            # test is about the registry, not shell fingerprinting.
            "kiro_crew.cron_script._resolve_command_shell", return_value="sh"
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
