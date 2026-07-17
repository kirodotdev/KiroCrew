"""Integration tests for script/command cron execution in the gateway.

Tests the actual _cron_callback dispatch for script and command jobs,
including delivery, concurrency guard, timeout handling, and Report().
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule


def _make_gw():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw.dashboard_state.get_slot = MagicMock(return_value=None)
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


def _make_script_job(**overrides):
    defaults = dict(
        id="sj1",
        name="script-job",
        message="CR-123",
        schedule=CronSchedule(kind="every", every_secs=60),
        script="~/.kirocrew/crons/monitor.py:run",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _make_command_job(**overrides):
    defaults = dict(
        id="cj1",
        name="cmd-job",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        command="echo hello",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


async def _run_script_callback(gw, job, script_result):
    """Run the cron callback with a mocked run_script_sandboxed result."""
    captured_cb = None

    with patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls, \
         patch("kiro_crew.slack.gateway.run_script_sandboxed", return_value=script_result) as mock_run, \
         patch("kiro_crew.slack.gateway.resolve_script_path"), \
         patch("kiro_crew.slack.gateway.sel"):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.side_effect = capture_cron

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return await _init_and_run(), mock_run


async def _run_command_callback(gw, job, cmd_result):
    """Run the cron callback with a mocked run_command_sandboxed result."""
    captured_cb = None

    with patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls, \
         patch("kiro_crew.slack.gateway.run_command_sandboxed", return_value=cmd_result) as mock_run, \
         patch("kiro_crew.slack.gateway.sel"):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.side_effect = capture_cron

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return await _init_and_run(), mock_run


class TestScriptExecution:
    """Test script cron dispatch through the gateway callback."""

    @pytest.mark.asyncio
    async def test_ok_status_returns_ok(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "ok"})
        assert result == "ok"
        assert job.last_status == "ok"
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_skip_returns_none(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "skip"})
        assert result is None

    @pytest.mark.asyncio
    async def test_done_removes_job(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "done", "message": "CR merged"})
        assert "CR merged" in (result or "")
        assert job.last_result == "CR merged"
        gw.cron_svc.remove_job.assert_called_once_with("sj1")

    @pytest.mark.asyncio
    async def test_report_does_not_remove_job(self):
        gw = _make_gw()
        job = _make_script_job(session_key="dashboard:chat-1")
        result, _ = await _run_script_callback(gw, job, {"status": "report", "message": "DRB passed"})
        assert "DRB passed" in (result or "")
        assert job.last_result == "DRB passed"
        gw.cron_svc.remove_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_increments_failures(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "error", "error": "something broke"})
        # Error is handled internally (logged, not re-raised)
        assert result is None
        assert job.last_status == "error"
        assert job.consecutive_failures == 1
        assert "something broke" in job.last_error

    @pytest.mark.asyncio
    async def test_concurrent_guard_skips(self):
        gw = _make_gw()
        gw._running_script_ids.add("sj1")
        job = _make_script_job()
        # Should skip without calling run_script_sandboxed
        captured_cb = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls, \
             patch("kiro_crew.slack.gateway.run_script_sandboxed") as mock_run, \
             patch("kiro_crew.slack.gateway.sel"):

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                return svc

            mock_cron_cls.side_effect = capture_cron

            async def _init_and_run():
                await gw._init_cron()
                return await captured_cb(job)

            result = await _init_and_run()
        assert result is None
        mock_run.assert_not_called()


class TestCommandExecution:
    """Test command cron dispatch through the gateway callback."""

    @pytest.mark.asyncio
    async def test_ok_command_stores_output(self):
        gw = _make_gw()
        job = _make_command_job()
        result, _ = await _run_command_callback(gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0})
        assert job.last_status == "ok"
        assert "hello" in job.last_result

    @pytest.mark.asyncio
    async def test_error_command_increments_failures(self):
        gw = _make_gw()
        job = _make_command_job()
        result, _ = await _run_command_callback(gw, job, {"status": "error", "output": "Exit code 1\n", "exit_code": 1})
        assert job.last_status == "error"
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_empty_output_no_delivery(self):
        gw = _make_gw()
        job = _make_command_job()
        result, _ = await _run_command_callback(gw, job, {"status": "ok", "output": "", "exit_code": 0})
        assert result is None  # no output = no delivery

    @pytest.mark.asyncio
    async def test_timeout_passed_to_subprocess(self):
        gw = _make_gw()
        job = _make_command_job(timeout=120)
        _, mock_run = await _run_command_callback(gw, job, {"status": "ok", "output": "done\n", "exit_code": 0})
        # Verify cmd_timeout was passed
        mock_run.assert_called_once()
        args = mock_run.call_args[0]
        assert args[0] == "echo hello"
        assert args[1] == 120  # the timeout value


class TestTimeoutPersistence:
    """Test that timeout field survives save/load cycle."""

    def test_timeout_round_trips(self, tmp_path):
        from kiro_crew.cron import CronService
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            name="timeout-test",
            message="test",
            every_secs=60,
        )
        job.timeout = 180
        svc._save()

        svc2 = CronService(base_dir=tmp_path)
        jobs = svc2.list_jobs()
        loaded = next((j for j in jobs if j.id == job.id), None)
        assert loaded is not None
        assert loaded.timeout == 180


class TestAutoPause:
    """Test that jobs auto-pause after 5 consecutive failures."""

    @pytest.mark.asyncio
    async def test_script_auto_pauses_after_5_errors(self):
        gw = _make_gw()
        job = _make_script_job()
        for i in range(4):
            await _run_script_callback(gw, job, {"status": "error", "error": f"fail {i}"})
            assert job.enabled is True, f"Should not pause after {i+1} failures"
        await _run_script_callback(gw, job, {"status": "error", "error": "fail 4"})
        assert job.enabled is False
        assert job.consecutive_failures == 5

    @pytest.mark.asyncio
    async def test_command_auto_pauses_after_5_errors(self):
        gw = _make_gw()
        job = _make_command_job()
        for i in range(4):
            await _run_command_callback(gw, job, {"status": "error", "output": f"err {i}", "exit_code": 1})
            assert job.enabled is True
        await _run_command_callback(gw, job, {"status": "error", "output": "err 4", "exit_code": 1})
        assert job.enabled is False
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
