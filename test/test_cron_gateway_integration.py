"""Integration tests for script/command cron execution in the gateway.

Tests the actual _cron_callback dispatch for script and command jobs,
including delivery, concurrency guard, timeout handling, and Report().
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.executors as ex
from kiro_crew.cron import CronJob, CronSchedule


async def _stalled_gate(*_args, **_kwargs):
    """Stand in for a fire-time gate that outlives the wake deadline.

    Models a recoverable event-loop stall rather than a slow gate: it is the
    whole ``run_in_cron_gate_pool`` call that fails to complete, so neither of
    the gate's OWN bounds is what stops it. That is the only way to reach the
    state where ``asyncio.wait_for`` in ``_execute_with_timeout`` cancels the
    coroutine AT the await -- no internal deadline sizing can prevent it.
    """
    await asyncio.sleep(30)


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
    gw._cron_session_binding = {}
    gw._cron_session_binding_overflow_count = 0
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
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


async def _run_script_callback(gw, job, script_result=None, vet_reason=None, side_effect=None):
    """Run the cron callback with a mocked run_script_sandboxed result.

    ``vet_reason`` feeds the fire-time governance gate (None = job may run);
    patching vet_job_at_fire_time also stands in for the script-path
    resolution it performs, which the removed gateway-level
    resolve_script_path call also covered.

    Pass ``side_effect`` to make the mocked call raise instead of returning.
    """
    captured_cb = None
    mock_kw = (
        {"side_effect": side_effect} if side_effect is not None else {"return_value": script_result}
    )

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_script_sandboxed", **mock_kw) as mock_run,
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=vet_reason),
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

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return await _init_and_run(), mock_run


async def _run_command_callback(gw, job, cmd_result=None, side_effect=None, vet_reason=None):
    """Run the cron callback with a mocked run_command_sandboxed result.

    Pass ``side_effect`` to make the mocked call raise instead of returning.
    ``vet_reason`` feeds the fire-time governance gate (None = job may run),
    mirroring _run_script_callback.
    """
    captured_cb = None
    mock_kw = (
        {"side_effect": side_effect} if side_effect is not None else {"return_value": cmd_result}
    )
    # Only stand in for the gate when simulating a denial: other tests here drive
    # the REAL gate, and patching it unconditionally would silence them.
    gate = (
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=vet_reason)
        if vet_reason is not None
        else nullcontext()
    )

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_command_sandboxed", **mock_kw) as mock_run,
        gate,
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
    async def test_skip_is_success_not_failure(self):
        # A completed Skip is a SUCCESS outcome (mirrors the ok/done/report
        # siblings): the branch returns None and never marks the run
        # last_status="error", so CronScheduler._execute treats the tick as
        # healthy and resets the strike counter for it one frame up. Long-lived
        # pollers end EVERY tick with Skip, so mis-classifying Skip as a failure
        # would trip the 5-strike auto-pause on a >99% healthy job.
        gw = _make_gw()
        job = _make_script_job()
        job.consecutive_failures = 3
        result, _ = await _run_script_callback(gw, job, {"status": "skip"})
        assert result is None
        assert job.last_status != "error"

    @pytest.mark.asyncio
    async def test_skip_defers_strike_reset_to_execute(self):
        # The Skip branch must NOT reset the counter or lift auto-pause itself:
        # that is record_success's job, reached only through
        # CronScheduler._execute, whose reset is guarded by the _cancelled_jobs
        # cancel-race check. An unguarded reset in this branch would clear the
        # pause and re-enable a job cancelled mid-tick, so the callback layer
        # leaves the bookkeeping untouched and defers to _execute. (The guarded
        # _execute reset — and the cancel guard — are covered by
        # TestExecuteSuccessResetsCounter in test_cron_autopause_persist.)
        gw = _make_gw()
        job = _make_script_job()
        job.consecutive_failures = 5
        job.auto_paused = True
        job.user_paused = True
        job.enabled = False
        result, _ = await _run_script_callback(gw, job, {"status": "skip"})
        assert result is None
        assert job.last_status != "error"
        assert job.consecutive_failures == 5
        assert job.auto_paused is True
        assert job.user_paused is True
        assert job.enabled is False

    @pytest.mark.asyncio
    async def test_done_removes_job(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "done", "message": "CR merged"})
        assert "CR merged" in (result or "")
        assert job.last_result == "CR merged"
        gw.cron_svc.remove_job_async.assert_called_once_with(
            "sj1", actor="cron", source="cron", one_shot_path="cron_gateway"
        )

    @pytest.mark.asyncio
    async def test_done_busy_store_defers_removal_not_dropped(self):
        """A busy store on the Done removal must hand off to defer_removal, not
        silently drop it (Arbiter BLOCK item 1). Otherwise the completed job
        lingers enabled and re-fires."""
        from kiro_crew.cron import CronStoreBusy

        gw = _make_gw()
        job = _make_script_job()
        captured_cb = None

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch(
                "kiro_crew.slack.gateway.run_script_sandboxed",
                return_value={"status": "done", "message": "CR merged"},
            ),
            patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=None),
            patch("kiro_crew.slack.gateway.sel"),
        ):

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                # First removal attempt hits a contended store.
                svc.remove_job_async = AsyncMock(side_effect=CronStoreBusy("busy"))
                svc.defer_removal = MagicMock()
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

            await gw._init_cron()
            assert captured_cb is not None
            await captured_cb(job)

        gw.cron_svc.remove_job_async.assert_called_once_with(
            "sj1", actor="cron", source="cron", one_shot_path="cron_gateway"
        )
        # The removal was queued for deferred drain, not dropped.
        gw.cron_svc.defer_removal.assert_called_once_with("sj1")

    @pytest.mark.asyncio
    async def test_report_does_not_remove_job(self):
        gw = _make_gw()
        job = _make_script_job(session_key="dashboard:chat-1")
        result, _ = await _run_script_callback(
            gw, job, {"status": "report", "message": "DRB passed"}
        )
        assert "DRB passed" in (result or "")
        assert job.last_result == "DRB passed"
        gw.cron_svc.remove_job_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_increments_failures(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(
            gw, job, {"status": "error", "error": "something broke"}
        )
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

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.slack.gateway.run_script_sandboxed") as mock_run,
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
        result, _ = await _run_command_callback(
            gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
        )
        assert job.last_status == "ok"
        assert "hello" in job.last_result

    @pytest.mark.asyncio
    async def test_error_command_increments_failures(self):
        gw = _make_gw()
        job = _make_command_job()
        result, _ = await _run_command_callback(
            gw, job, {"status": "error", "output": "Exit code 1\n", "exit_code": 1}
        )
        assert job.last_status == "error"
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_empty_output_no_delivery(self):
        gw = _make_gw()
        job = _make_command_job()
        result, _ = await _run_command_callback(
            gw, job, {"status": "ok", "output": "", "exit_code": 0}
        )
        assert result is None  # no output = no delivery

    @pytest.mark.asyncio
    async def test_silent_success_overwrites_stale_result(self):
        """A silent success must not leave the previous run's failure in last_result.

        The dashboard and the cron_list renderers read last_result to show "what
        this job last produced", so a stale value is presented as this run's
        output on a job the same view reports as OK. Cleared rather than marked
        with a literal: last_status already carries the verdict, and any literal
        stored here is also legal job output, so no reader could tell the two
        apart.
        """
        gw = _make_gw()
        job = _make_command_job(last_result="⚠️ Exit code 1\n\nstderr:\nboom")
        result, _ = await _run_command_callback(
            gw, job, {"status": "ok", "output": "", "exit_code": 0}
        )
        assert result is None
        assert job.last_status == "ok"
        assert job.last_result == ""
        assert "Exit code 1" not in job.last_result

    @pytest.mark.asyncio
    async def test_report_of_exactly_ok_is_kept_as_a_result(self):
        """A job whose reported text happens to be "ok" still has a result.

        Pinned alongside the clearing tests because the two are only one string
        apart: clearing on silence is correct, and dropping this value is not.
        """
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "report", "message": "ok"})
        assert result == "ok"
        assert job.last_status == "ok"
        assert job.last_result == "ok"
        assert job.result_produced is True

    @pytest.mark.asyncio
    async def test_silent_failure_clears_stale_result(self):
        """A silent failure must not present the PREVIOUS failure's text as this run's.

        Cleared rather than sentinel-marked: an empty result lets a reader fall
        back to last_error, which carries this run's actual reason.
        """
        gw = _make_gw()
        job = _make_command_job(last_result="⚠️ Exit code 1\n\nstderr:\nold failure")
        result, _ = await _run_command_callback(
            gw, job, {"status": "timeout", "output": "", "exit_code": None}
        )
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "old failure" not in job.last_result
        assert "no output" in job.last_error

    @pytest.mark.asyncio
    async def test_timeout_clears_stale_result(self):
        """A timed-out run must not present the previous run's output as its own."""
        gw = _make_gw()
        job = _make_command_job(last_result="42 widgets")
        result, _ = await _run_command_callback(gw, job, side_effect=asyncio.TimeoutError())
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "42 widgets" not in job.last_result
        assert "timeout" in job.last_error

    @pytest.mark.asyncio
    async def test_raising_command_clears_stale_result(self):
        """Same for a raising run: last_error must be the only text left to read."""
        gw = _make_gw()
        job = _make_command_job(last_result="42 widgets")
        result, _ = await _run_command_callback(gw, job, side_effect=RuntimeError("boom"))
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "boom" in job.last_error

    @pytest.mark.asyncio
    async def test_script_timeout_clears_stale_result(self):
        """The script branch's timeout path carries the same invariant."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        result, _ = await _run_script_callback(gw, job, side_effect=asyncio.TimeoutError())
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "42 widgets" not in job.last_result
        assert "timeout" in job.last_error

    @pytest.mark.asyncio
    async def test_raising_script_clears_stale_result(self):
        """Same for a raising script run: last_error must be the only text left."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        result, _ = await _run_script_callback(gw, job, side_effect=RuntimeError("boom"))
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "boom" in job.last_error

    @pytest.mark.asyncio
    async def test_command_fire_time_deny_clears_stale_result(self):
        """A governance denial is result-less, so it must not wear the last run's output."""
        gw = _make_gw()
        job = _make_command_job(last_result="42 widgets")
        result, mock_run = await _run_command_callback(
            gw,
            job,
            {"status": "ok", "output": "unused", "exit_code": 0},
            vet_reason="command not permitted at fire time",
        )
        assert result is None
        mock_run.assert_not_called()
        assert job.last_status == "error"
        assert job.last_result == "", "a denied run must not display the previous run's output"
        assert "not permitted" in job.last_error

    @pytest.mark.asyncio
    async def test_script_fire_time_deny_clears_stale_result(self):
        """Same denial path on the script side."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        result, mock_run = await _run_script_callback(
            gw,
            job,
            {"status": "ok"},
            vet_reason="script changed on disk",
        )
        assert result is None
        mock_run.assert_not_called()
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "changed on disk" in job.last_error

    @pytest.mark.asyncio
    async def test_script_skip_clears_stale_result(self):
        """A Skip is a result-less success -- carrying prior output reads as produced."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        await _run_script_callback(gw, job, {"status": "skip"})
        assert job.last_result == "", "a Skip must not present the previous run's output"

    @pytest.mark.asyncio
    async def test_silent_script_ok_leaves_no_sentinel(self):
        """A silent ok clears rather than writing the literal ok sentinel.

        The sentinel existed only to be non-empty, and two mcp_cron readers had to
        filter it back out; clearing removes the writer and both filters.
        """
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        await _run_script_callback(gw, job, {"status": "ok"})
        assert job.last_status == "ok"
        assert job.last_result == "", "no sentinel, and no stale carry either"

    @pytest.mark.asyncio
    async def test_nonempty_output_still_stored(self):
        """Negative control: the change must not suppress a real result."""
        gw = _make_gw()
        job = _make_command_job(last_result="stale")
        await _run_command_callback(
            gw, job, {"status": "ok", "output": "42 widgets\n", "exit_code": 0}
        )
        assert "42 widgets" in job.last_result
        assert job.last_result != ""

    @pytest.mark.asyncio
    async def test_timeout_passed_to_subprocess(self):
        gw = _make_gw()
        job = _make_command_job(timeout=120)
        _, mock_run = await _run_command_callback(
            gw, job, {"status": "ok", "output": "done\n", "exit_code": 0}
        )
        # Verify cmd_timeout was passed
        mock_run.assert_called_once()
        args = mock_run.call_args[0]
        assert args[0] == "echo hello"
        assert args[1] == 120  # the timeout value

    @pytest.mark.asyncio
    async def test_fire_time_command_deny_blocks_execution(self):
        # A policy tightened after this job was scheduled must still block it at
        # fire time, not just at cron_add authoring time.
        gw = _make_gw()
        job = _make_command_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch(
                "kiro_crew.mcp_cron._vet_command_governance",
                return_value="Error: cron command blocked by governance policy: denied",
            ),
        ):
            result, mock_run = await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )
        assert result is None
        assert job.last_status == "error"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_time_capability_deny_blocks_execution(self):
        # capabilities.cron can be disabled after the job was scheduled too.
        gw = _make_gw()
        job = _make_command_job()
        with patch(
            "kiro_crew.mcp_cron._vet_cron_capability_governance",
            return_value="Error: cron scheduling blocked by governance policy: disabled",
        ):
            result, mock_run = await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )
        assert result is None
        assert job.last_status == "error"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_time_governance_allow_still_executes(self):
        gw = _make_gw()
        job = _make_command_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch("kiro_crew.mcp_cron._vet_command_governance", return_value=None),
        ):
            result, mock_run = await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )
        assert job.last_status == "ok"
        mock_run.assert_called_once()


class TestFireTimeGatesScriptAndMessage:
    """Fire-time re-vetting for script and message (LLM) cron jobs.

    Command jobs gained this gate first (TestCommandExecution above); these
    tests lock in the same policy-tightened-after-scheduling protection for
    the other two job kinds, routed through mcp_cron.vet_job_at_fire_time.
    The mcp_cron privates are patched (not the helper itself) so the
    helper's real dispatch logic is exercised.
    """

    async def _run_script_real_vet(self, gw, job, script_result, cap=None, script_vet=None):
        """Run the script callback with the REAL vet helper, privates patched."""
        captured_cb = None

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch(
                "kiro_crew.slack.gateway.run_script_sandboxed", return_value=script_result
            ) as mock_run,
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=cap),
            patch("kiro_crew.mcp_cron.resolve_script_path", return_value=("/tmp/x.py", "run")),
            patch("kiro_crew.mcp_cron._vet_script_file", return_value=script_vet) as mock_vet_file,
            patch("kiro_crew.slack.gateway.sel") as mock_sel,
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
            result = await captured_cb(job)
            return result, mock_run, mock_vet_file, mock_sel

    @pytest.mark.asyncio
    async def test_script_fire_time_capability_deny_blocks_execution(self):
        # capabilities.cron disabled AFTER the script job was scheduled must
        # deny the run at fire time, not merely re-resolve the path.
        gw = _make_gw()
        job = _make_script_job()
        result, mock_run, _, mock_sel = await self._run_script_real_vet(
            gw,
            job,
            {"status": "ok"},
            cap="Error: cron scheduling blocked by governance policy: disabled",
        )
        assert result is None
        assert job.last_status == "error"
        assert "governance" in job.last_error
        mock_run.assert_not_called()
        # Job KEPT (a later policy loosening lets it resume on its own):
        # not removed, and the denial did NOT feed the auto-pause counter.
        gw.cron_svc.remove_job_async.assert_not_called()
        assert job.consecutive_failures == 0
        assert job.enabled is True
        outcomes = [
            c.kwargs.get("outcome")
            for c in mock_sel.return_value.log_tool_invocation.call_args_list
        ]
        assert "denied" in outcomes

    @pytest.mark.asyncio
    async def test_script_fire_time_body_rescan_denies_edited_script(self):
        # A script file edited on disk after authoring (e.g. to read a
        # credential path) is re-scanned at fire time and denied.
        gw = _make_gw()
        job = _make_script_job()
        result, mock_run, mock_vet_file, mock_sel = await self._run_script_real_vet(
            gw,
            job,
            {"status": "ok"},
            script_vet="Error: cron script blocked: references a credential path",
        )
        assert result is None
        assert job.last_status == "error"
        assert "credential" in job.last_error
        mock_run.assert_not_called()
        gw.cron_svc.remove_job_async.assert_not_called()
        assert job.consecutive_failures == 0
        assert job.enabled is True
        # The re-scan ran against the freshly re-resolved path.
        mock_vet_file.assert_called_once_with("/tmp/x.py")
        outcomes = [
            c.kwargs.get("outcome")
            for c in mock_sel.return_value.log_tool_invocation.call_args_list
        ]
        assert "denied" in outcomes

    @pytest.mark.asyncio
    async def test_script_fire_time_allow_still_executes(self):
        gw = _make_gw()
        job = _make_script_job()
        result, mock_run, _, _ = await self._run_script_real_vet(gw, job, {"status": "ok"})
        assert result == "ok"
        assert job.last_status == "ok"
        mock_run.assert_called_once()

    async def _run_message_callback(self, gw, job, cap=None):
        """Run the cron callback for a message (LLM) job up to the gate."""
        captured_cb = None

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=cap),
            patch("kiro_crew.slack.gateway.sel") as mock_sel,
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
            result = await captured_cb(job)
            return result, mock_sel

    @pytest.mark.asyncio
    async def test_message_fire_time_capability_deny_blocks_dispatch(self):
        # Message (LLM) jobs need a fire-time capabilities.cron
        # check: without it, disabling the capability after scheduling has no
        # effect. The gate must block the session dispatch entirely.
        gw = _make_gw()
        job = CronJob(
            id="mj1",
            name="msg-job",
            message="summarize the day",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        result, mock_sel = await self._run_message_callback(
            gw,
            job,
            cap="Error: cron scheduling blocked by governance policy: disabled",
        )
        assert result is None
        assert job.last_status == "error"
        assert "governance" in job.last_error
        # No LLM session was acquired.
        gw.sessions.get_or_create.assert_not_called()
        gw.cron_svc.remove_job_async.assert_not_called()
        assert job.consecutive_failures == 0
        assert job.enabled is True
        outcomes = [
            c.kwargs.get("outcome")
            for c in mock_sel.return_value.log_tool_invocation.call_args_list
        ]
        assert "denied" in outcomes


class TestFireTimeDenyOneShotRetention:
    """A fire-time denial is a policy refusal, not a completed run: one-shot
    delete_after_run jobs must be RETAINED and at-jobs stay armed."""

    @pytest.mark.asyncio
    async def test_deny_sets_fire_time_denied_flag(self):
        gw = _make_gw()
        job = _make_script_job(delete_after_run=True)
        result, mock_run = await _run_script_callback(
            gw,
            job,
            {"status": "ok"},
            vet_reason="Error: cron scheduling blocked by governance policy: x",
        )
        assert result is None
        assert job.fire_time_denied is True
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_run_resets_flag_via_execute(self):
        # CronService._execute resets the marker at the start of every run.
        from kiro_crew.cron import CronService

        job = _make_script_job()
        job.fire_time_denied = True  # stale from a prior denied run

        async def _ok(j):
            return "ok"

        svc = CronService.__new__(CronService)
        svc._on_job = _ok
        await svc._execute(job)
        assert job.fire_time_denied is False

    def test_merge_retains_denied_delete_after_run_job(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="one-shot", message="go", at_ts=9999999999.0)
        job.delete_after_run = True
        svc._save()
        job.fire_time_denied = True
        job.last_status = "error"
        job.enabled = False  # _execute parks a denied at-job disabled
        svc._merge_job_result(job)
        # include_disabled: the denied one-shot is parked DISABLED, so the
        # default (enabled-only) listing hides it — retention is what matters.
        stored = next((j for j in svc.list_jobs(include_disabled=True) if j.id == job.id), None)
        assert stored is not None, "denied one-shot was deleted"
        # Parked DISABLED so a past-due at-job cannot refire every tick.
        assert stored.enabled is False
        # A COMPLETED run (no denial) still deletes it.
        job.fire_time_denied = False
        job.last_status = "ok"
        svc._merge_job_result(job)
        assert not any(j.id == job.id for j in svc.list_jobs(include_disabled=True))

    @pytest.mark.asyncio
    async def test_denied_past_due_at_job_does_not_stay_due(self):
        """A past-due at-job denied at fire time must be parked disabled —
        leaving it enabled would make it due again on every timer tick."""
        from kiro_crew.cron import CronService

        job = _make_script_job(schedule=CronSchedule(kind="at", at_ts=1.0), delete_after_run=True)

        async def _deny(j):
            # Mirrors the gateway deny branch: mark refusal, return normally.
            j.last_status = "error"
            j.fire_time_denied = True
            return None

        svc = CronService.__new__(CronService)
        svc._on_job = _deny
        await svc._execute(job)
        assert job.enabled is False
        assert job.fire_time_denied is True


class TestFireTimeAuditTrail:
    """Every fire-time decision — allowed and denied — leaves a SEL
    governance_decision event keyed cron:<job.id>."""

    def test_allowed_fire_emits_governance_decision(self):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        job = _make_command_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch("kiro_crew.mcp_cron._vet_command_governance", return_value=None),
            patch("kiro_crew.mcp_cron.sel") as mock_sel,
        ):
            assert vet_job_at_fire_time(job) is None
        calls = mock_sel.return_value.log_governance_decision.call_args_list
        assert any(
            c.kwargs.get("outcome") == "allowed" and c.kwargs.get("session_key") == f"cron:{job.id}"
            for c in calls
        )

    def test_denied_fire_emits_governance_decision(self):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        job = _make_command_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch(
                "kiro_crew.mcp_cron._vet_command_governance",
                return_value="Error: cron command blocked by governance policy: x",
            ),
            patch("kiro_crew.mcp_cron.sel") as mock_sel,
        ):
            assert vet_job_at_fire_time(job) is not None
        calls = mock_sel.return_value.log_governance_decision.call_args_list
        assert any(
            c.kwargs.get("outcome") == "denied" and c.kwargs.get("scope") == "commands"
            for c in calls
        )

    def test_script_body_deny_emits_scoped_decision(self):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        job = _make_script_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch("kiro_crew.mcp_cron.resolve_script_path", return_value=("/tmp/x.py", "run")),
            patch(
                "kiro_crew.mcp_cron._vet_script_file", return_value="Error: cron script blocked: x"
            ),
            patch("kiro_crew.mcp_cron.sel") as mock_sel,
        ):
            assert vet_job_at_fire_time(job) is not None
        calls = mock_sel.return_value.log_governance_decision.call_args_list
        assert any(
            c.kwargs.get("outcome") == "denied" and c.kwargs.get("scope") == "cron_script_body"
            for c in calls
        )


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


# ── Per-job model override: _acquire_with_model_fallback / _annotate_model_downgrade ──


def _make_llm_job(**overrides):
    """LLM-based cron job (no script, no command) with optional model override."""
    defaults = dict(
        id="lj1",
        name="llm-job",
        message="Run daily check",
        schedule=CronSchedule(kind="every", every_secs=3600),
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _make_gw_for_llm():
    """Extended _make_gw with attributes the LLM single-agent path needs."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = None  # suppress Slack delivery
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw.dashboard_state.get_slot = MagicMock(return_value=None)
    gw.dashboard_state.has_slot = MagicMock(return_value=False)
    gw.dashboard_state.notify = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._cron_session_binding = {}
    gw._cron_session_binding_overflow_count = 0
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
    gw._cfg = MagicMock()
    gw._cfg.agent.provider = "acp"
    gw._cfg.hooks = {}
    gw._approval_mode = None
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.sessions.get_channel = MagicMock(return_value=None)
    gw.ctx_builder.build_message = MagicMock(return_value=("full prompt", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


async def _run_llm_callback(gw, job, *, get_or_create_side_effect=None, sel_factory=None):
    """Run the cron callback for an LLM-based job through _init_cron.

    get_or_create_side_effect: if provided, set as the side_effect on
    sessions.get_or_create (for simulating model errors / fallback).
    """
    captured_cb = None

    if get_or_create_side_effect is not None:
        gw.sessions.get_or_create = AsyncMock(side_effect=get_or_create_side_effect)
    else:
        provider_mock = MagicMock()
        gw.sessions.get_or_create = AsyncMock(return_value=(provider_mock, True, False))

    _embed_mock = AsyncMock(return_value=("full prompt", None))
    _stream_mock = AsyncMock(return_value="Agent response here")
    sel_patch = (
        patch("kiro_crew.slack.gateway.sel", new=sel_factory)
        if sel_factory is not None
        else patch("kiro_crew.slack.gateway.sel")
    )

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_in_embed_pool", _embed_mock),
        patch("kiro_crew.slack.gateway.stream_and_collect", _stream_mock),
        sel_patch,
        patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
    ):

        mock_ctx.return_value = (f"cron:{job.id}", job.message)

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.update_job_async = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        assert captured_cb is not None
        result = await captured_cb(job)
        return result, _stream_mock


def _recording_cron_provider_factory(created: list[tuple[object, str | None, str | None]]):
    """Build live providers while recording the binding used for each allocation."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = MagicMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.memory_mode = kwargs.get("memory_mode", "persistent")
        provider.is_process_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.context_window_tokens = lambda: 0
        provider.has_active_turn = lambda: False
        provider.runtime_info = lambda: (None, None)
        created.append((provider, agent, kwargs.get("cwd")))
        return provider

    return factory


class TestCronBindingMetadataEviction:
    @pytest.mark.asyncio
    async def test_removed_job_id_reuse_cold_starts_single_agent_under_the_new_binding(
        self, tmp_path
    ):
        """Removal eviction cannot let a same-name import reuse the old live session."""
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        created: list[tuple[object, str | None, str | None]] = []
        manager = SessionManager(
            KiroCrewConfig(),
            provider_factory=_recording_cron_provider_factory(created),
        )
        gateway = _make_gw_for_llm()
        gateway.sessions = manager
        job_id = "sameid01"
        key = f"cron:{job_id}"
        old_dir = tmp_path / "old-project"
        new_dir = tmp_path / "new-project"
        old_dir.mkdir()
        new_dir.mkdir()

        try:
            old_provider, _, _ = await manager.get_or_create(
                key,
                agent="old-agent",
                cwd=str(old_dir),
            )
            manager.release(key)
            gateway._cron_session_binding[key] = (
                str(old_dir),
                "old-agent",
                None,
                "project",
            )
            gateway._evict_removed_cron_session_bindings(frozenset({job_id}))
            assert key not in gateway._cron_session_binding

            real_get_or_create = manager.get_or_create
            job = _make_llm_job(
                id=job_id,
                name="same imported name",
                project_path=str(new_dir),
                agent_id="new-agent",
            )
            resolved = MagicMock(
                requested_resolved=True,
                kiro_agent="new-agent",
                model="",
                memory_store_name="",
                execution_context=None,
                resolved_alias="",
                resolved_source="project",
            )
            with (
                patch("kiro_crew.slack.gateway.resolve_agent_bindings", return_value=resolved),
                patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
                patch("kiro_crew.slack.gateway.publish_turn_identity", AsyncMock()),
            ):
                result, stream = await _run_llm_callback(
                    gateway,
                    job,
                    get_or_create_side_effect=real_get_or_create,
                )

            assert result == "Agent response here"
            assert stream.await_args.args[0] is not old_provider
            assert created[1][1:] == ("new-agent", str(new_dir))
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_pruned_sequence_key_reuse_cold_starts_under_the_new_binding(self, tmp_path):
        """A sequence key reintroduced after pruning cannot inherit its old provider."""
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        created: list[tuple[object, str | None, str | None]] = []
        manager = SessionManager(
            KiroCrewConfig(),
            provider_factory=_recording_cron_provider_factory(created),
        )
        gateway = _make_gw_for_llm()
        gateway.sessions = manager
        job_id = "sequence"
        key = f"cron:{job_id}:planner"
        old_dir = tmp_path / "old-sequence-project"
        new_dir = tmp_path / "new-sequence-project"
        old_dir.mkdir()
        new_dir.mkdir()

        try:
            old_provider, _, _ = await manager.get_or_create(
                key,
                agent="old-planner",
                cwd=str(old_dir),
            )
            manager.release(key)
            gateway._cron_session_binding[key] = (
                str(old_dir),
                "old-planner",
                None,
                "project",
            )
            await gateway._prune_cron_session_bindings(job_id, set())
            assert key not in gateway._cron_session_binding

            real_get_or_create = manager.get_or_create
            job = _make_llm_job(
                id=job_id,
                project_path=str(new_dir),
                agent_sequence=["planner", "worker"],
            )

            def resolved(_cfg, agent: str, *_args, **_kwargs):
                return MagicMock(
                    requested_resolved=True,
                    kiro_agent=f"new-{agent}",
                    model="",
                    memory_store_name="",
                    execution_context=None,
                    resolved_alias="",
                    resolved_source="project",
                )

            with (
                patch("kiro_crew.slack.gateway.resolve_agent_bindings", side_effect=resolved),
                patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
                patch("kiro_crew.slack.gateway.publish_turn_identity", AsyncMock()),
            ):
                result, stream = await _run_llm_callback(
                    gateway,
                    job,
                    get_or_create_side_effect=real_get_or_create,
                )

            assert result == "Agent response here"
            assert stream.await_args_list[0].args[0] is not old_provider
            assert created[1][1:] == ("new-planner", str(new_dir))
        finally:
            await manager.close_all()


class TestLlmCronAdmission:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_modes", [False, True])
    async def test_execution_binding_runs_off_loop(self, monkeypatch, with_modes):
        from kiro_crew import execution_context

        gw = _make_gw_for_llm()
        gw.ctx_builder._session_memory_modes = {} if with_modes else None
        job = _make_llm_job()
        loop_thread = threading.get_ident()
        bindings = []
        original = execution_context.bind_session_execution

        def bind(key, execution):
            bindings.append((threading.get_ident(), key, execution))
            return original(key, execution)

        monkeypatch.setattr(execution_context, "bind_session_execution", bind)
        result, _ = await asyncio.wait_for(_run_llm_callback(gw, job), 10)
        assert result == "Agent response here"
        # Mode publication also tightens the same canonical record.
        assert len(bindings) == (2 if with_modes else 1)
        thread, key, captured = bindings[0]
        assert all(thread != loop_thread for thread, _, _ in bindings)
        assert key == f"cron:{job.id}"
        assert captured == execution_context.read_session_execution(key, required=True)

    @pytest.mark.asyncio
    async def test_session_closing_retains_undispatched_one_shot(self):
        from kiro_crew.session import SessionClosingError

        gw = _make_gw_for_llm()
        job = _make_llm_job(
            delete_after_run=True,
            last_result="stale result",
            consecutive_failures=2,
        )

        async def _side_effect(*_args, **_kwargs):
            raise SessionClosingError("automatic update owns admission")

        result, stream = await _run_llm_callback(
            gw,
            job,
            get_or_create_side_effect=_side_effect,
        )

        assert result is None
        assert job.run_never_started is True
        assert job.last_status == "error"
        assert job.last_error == "gateway admission is closed"
        assert job.last_result == ""
        assert job.consecutive_failures == 2
        stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unbound_unresolved_one_shot_survives(self, tmp_path):
        """A vanished global agent skips; it never consumes scheduled work."""
        from kiro_crew.cron import CronService

        gw = _make_gw_for_llm()
        job = _make_llm_job(
            agent_id="ghost-agent",
            delete_after_run=True,
            schedule=CronSchedule(kind="at", at_ts=9999999999.0),
        )
        svc = CronService(base_dir=tmp_path)
        svc._jobs = [job]
        svc._save()

        with patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve:
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.resolved_source = "default"
            result, stream = await _run_llm_callback(gw, job)

        svc._merge_job_result(job)
        stored = next(
            (
                candidate
                for candidate in svc.list_jobs(include_disabled=True)
                if candidate.id == job.id
            ),
            None,
        )
        assert stored is not None, "unresolved one-shot was deleted"
        assert result is None
        assert stored.last_status == "error"
        assert stored.last_error == (
            "Agent 'ghost-agent' is no longer configured. Pick another agent for "
            "this job, or restore that one in your agent settings."
        )
        assert job.run_never_started is True
        gw.sessions.get_or_create.assert_not_called()
        stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_skipped_past_due_one_shot_is_parked_not_refired(self, tmp_path):
        """A skipped ``at`` job survives AND is parked -- it must not spin.

        Every ``delete_after_run`` job is an ``at`` job. When its agent stops
        resolving, the named skip sets ``run_never_started`` so the one-shot is
        retained (the sibling test above) -- but retention alone left it ENABLED:
        ``_execute`` parks an at-job only on ``fire_time_denied``, and
        ``_merge_job_result`` propagated the park only on that same flag.
        ``_is_due`` answers True for a past-due ``at`` job on every scan and
        ``_next_wake_secs`` yields ``0.0`` for it, so the timer re-entered the
        fire path with no delay -- the zero-delay refire loop the denial park
        exists to prevent, here for a condition that is just as permanent (the
        agent does not come back on its own). The skip must park the job like
        the denial does, and the park must reach DISK: ``enabled`` is re-derived
        from ``user_paused`` on reload, so an in-memory park alone comes back
        live. Read back through a FRESH service so that half is what is pinned.
        """
        import time

        from kiro_crew.cron import CronService

        gw = _make_gw_for_llm()
        job = _make_llm_job(
            agent_id="ghost-agent",
            delete_after_run=True,
            schedule=CronSchedule(kind="at", at_ts=time.time() - 60.0),
        )
        svc = CronService(base_dir=tmp_path)
        svc._jobs = [job]
        svc._save()

        with patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve:
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.resolved_source = "default"
            result, stream = await _run_llm_callback(gw, job)

        assert result is None
        assert job.run_never_started is True, "the skip must still retain the one-shot"
        assert job.enabled is False, "a skipped past-due one-shot was left enabled in memory"
        svc._merge_job_result(job)

        reloaded = CronService(base_dir=tmp_path)
        stored = next(
            (j for j in reloaded.list_jobs(include_disabled=True) if j.id == job.id), None
        )
        assert stored is not None, "unresolved one-shot was deleted"
        assert stored.enabled is False, (
            "a skipped past-due one-shot came back ENABLED from disk -- due again on "
            "every scan, a zero-delay refire loop"
        )
        assert stored.user_paused is True, "the park must be persisted as user_paused"
        assert stored.last_status == "error"
        assert "ghost-agent" in (stored.last_error or "")
        # The scheduler's own reading: nothing is due, nothing arms a zero wake.
        assert reloaded._next_wake_secs() is None, "the parked one-shot still arms the timer"
        gw.sessions.get_or_create.assert_not_called()
        stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_skipped_recurring_job_is_not_parked(self, tmp_path):
        """Control: the park is one-shot-only. A recurring job waits for its
        next slot and resumes on its own once the name resolves again, so the
        skip must leave ``enabled`` alone for it -- the same asymmetry the
        fire-time denial keeps. Project-bound, because that is the recurring
        shape the fire-time guard validates at all (an unbound recurring job
        dispatches from its frozen capture and reads no config)."""
        gw = _make_gw_for_llm()
        gw.cron_svc.update_job_async = AsyncMock()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ghost-agent")
        assert job.schedule.kind == "every"

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.kiro_agent = "default-agent"
            mock_resolve.return_value.resolved_source = "default"
            result, _stream = await _run_llm_callback(gw, job)

        assert result is None
        assert job.run_never_started is True
        assert job.enabled is True, "a skipped RECURRING job must not be parked"


class TestModelFallback:
    """Test _acquire_with_model_fallback and _annotate_model_downgrade paths."""

    @pytest.mark.asyncio
    async def test_model_override_passed_to_session(self):
        """When job.model is set, get_or_create receives it."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        result, _ = await _run_llm_callback(gw, job)
        # The first get_or_create call should have model=job.model
        call_kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        assert call_kwargs["model"] == "claude-opus-4-8"
        assert result == "Agent response here"

    @pytest.mark.asyncio
    async def test_no_model_passes_none(self):
        """When job.model is empty, get_or_create receives model=None."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")

        result, _ = await _run_llm_callback(gw, job)
        call_kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        assert call_kwargs["model"] is None

    @pytest.mark.asyncio
    async def test_model_unavailable_falls_back(self):
        """When pinned model fails with model-related error, retries without model."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-nonexistent-9")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-nonexistent-9' not found")
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert call_count[0] == 2
        assert "unavailable" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_model_fallback_annotates_response(self):
        """Downgraded result is prefixed with a warning annotation."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-fancy-model")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-fancy-model' is not available")
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert result.startswith("⚠️")
        assert "claude-fancy-model" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_non_model_error_propagates(self):
        """Errors unrelated to model are not caught by the fallback."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        async def _side_effect(*args, **kwargs):
            raise RuntimeError("connection refused to provider host")

        with pytest.raises(RuntimeError, match="connection refused"):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)

    @pytest.mark.asyncio
    async def test_no_fallback_when_model_empty(self):
        """When job.model is empty, any error propagates (no fallback needed)."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")

        async def _side_effect(*args, **kwargs):
            raise RuntimeError("model spawn failed")

        with pytest.raises(RuntimeError, match="model spawn failed"):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)


class TestProjectPathMissingSkipsRun:
    """A job's project directory existing at save time but gone by fire time
    must SKIP the run entirely (no agent invoked, no session acquired) and
    record it as a normal failed run — not silently fall back to a global
    agent, which ran the wrong agent with no visible sign beyond a list-page
    badge (the original, since-reverted behavior).
    """

    @pytest.mark.asyncio
    async def test_single_agent_job_skips_and_marks_error(self, tmp_path):
        gw = _make_gw_for_llm()
        gw.cron_svc.update_job_async = AsyncMock()
        vanished = str(tmp_path / "does-not-exist")
        job = _make_llm_job(project_path=vanished, agent_id="ea-dev")

        result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "no longer exists" in job.last_error
        # The reason is persisted and read past the owner boundary; the folder
        # itself is owner-only and stays in the log line.
        assert vanished not in job.last_error
        assert job.run_never_started is True
        # No agent turn ran: neither the session acquire nor the prompt
        # stream was ever reached.
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_folder_runs_normally(self, tmp_path):
        # Control: a REAL directory must not trip the skip at all — the
        # normal single-agent turn still runs and returns its result.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value.requested_resolved = True
            mock_resolve.return_value.kiro_agent = "ea-dev"
            mock_resolve.return_value.model = ""
            mock_resolve.return_value.memory_store_name = ""
            mock_resolve.return_value.execution_context = None
            mock_resolve.return_value.resolved_alias = None
            mock_resolve.return_value.resolved_source = "project"
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result == "Agent response here"
        assert job.last_status != "error"
        assert job.run_never_started is False
        gw.sessions.get_or_create.assert_called()

    @pytest.mark.asyncio
    async def test_sequential_job_skips_before_any_sequence_member_runs(self, tmp_path):
        gw = _make_gw_for_llm()
        gw.cron_svc.update_job_async = AsyncMock()
        vanished = str(tmp_path / "does-not-exist")
        job = _make_llm_job(project_path=vanished, agent_sequence=["agent-a", "agent-b"])

        result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "no longer exists" in job.last_error
        assert vanished not in job.last_error
        assert job.run_never_started is True
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()


class TestUnboundSequenceJobIsNotFireTimeValidated:
    """The sequence path's fire-time agent check has the single-agent path's scope.

    The single-agent path validates an agent name at fire time only for a
    PROJECT-BOUND job (a folder is the one place a project definition can shadow
    the name) plus the narrow unbound ``delete_after_run`` one-shot (a vanished
    explicit agent must not consume scheduled work that never ran). An unbound
    recurring job dispatches from its captured execution without being refused,
    which is what lets a sequence member that is not a ``config.agents`` alias --
    an app-registered agent, a crew name the host never aliased -- keep firing.
    The sequence guard is scoped the same way, so the two paths cannot disagree
    about which jobs a stale roster is allowed to stop.
    """

    @pytest.mark.asyncio
    async def test_an_unbound_recurring_sequence_fires_on_an_unresolved_name(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(agent_sequence=["ghost-a", "ghost-b"])
        assert not job.project_path and not job.member_id and not job.delete_after_run

        with patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve:
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.resolved_source = "default"
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert job.run_never_started is False, (
            "an unbound recurring sequence job was refused at fire time for a member "
            "name that is not a config alias -- the shape the single-agent path exempts"
        )
        assert job.last_status != "error"
        assert result == "Agent response here"
        gw.sessions.get_or_create.assert_called()
        assert gw.sessions.get_or_create.call_args_list[0].kwargs["agent"] == "ghost-a"

    @pytest.mark.asyncio
    async def test_an_unbound_one_shot_sequence_is_still_validated(self, tmp_path):
        # Control: the one-shot exception applies to the sequence path as it does
        # to the single-agent path -- a vanished member must not consume the
        # scheduled work, so the fire is the named never-started skip.
        from kiro_crew.cron import CronService

        gw = _make_gw_for_llm()
        job = _make_llm_job(
            agent_sequence=["ghost-a", "ghost-b"],
            delete_after_run=True,
            schedule=CronSchedule(kind="at", at_ts=9999999999.0),
        )
        svc = CronService(base_dir=tmp_path)
        svc._jobs = [job]
        svc._save()

        with patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve:
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.resolved_source = "default"
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.run_never_started is True
        assert job.last_status == "error"
        assert "ghost-a" in job.last_error
        gw.sessions.get_or_create.assert_not_called()
        svc._merge_job_result(job)
        assert any(
            j.id == job.id for j in svc.list_jobs(include_disabled=True)
        ), "the skipped one-shot was consumed"


class TestUnresolvedProjectAgentSkipsRun:
    """A job whose ``agent_id``/sequence member names a specific agent that
    ``resolve_agent_bindings`` cannot find inside ``project_path`` (the
    folder exists, but the agent's own JSON does not, or never did) must
    SKIP the run — not silently execute under the default agent's tools and
    permissions with no visible sign anything was substituted. Distinct from
    ``TestProjectPathMissingSkipsRun``, which covers the folder itself being
    gone; this covers the folder existing but not declaring the requested
    agent.
    """

    @pytest.mark.asyncio
    async def test_single_agent_job_skips_and_marks_error(self, tmp_path):
        gw = _make_gw_for_llm()
        gw.cron_svc.update_job_async = AsyncMock()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ghost-agent")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.kiro_agent = "default-agent"
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "ghost-agent" in job.last_error
        assert job.run_never_started is True
        # No agent turn ran: neither the session acquire nor the prompt
        # stream was ever reached — the resolved default binding must never
        # be handed to a session.
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_sequential_job_skips_before_any_sequence_member_runs(self, tmp_path):
        gw = _make_gw_for_llm()
        gw.cron_svc.update_job_async = AsyncMock()
        job = _make_llm_job(project_path=str(tmp_path), agent_sequence=["ghost-a", "ghost-b"])

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.kiro_agent = "default-agent"
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "ghost-a" in job.last_error
        assert job.run_never_started is True
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sequence", [False, True], ids=["single-agent", "sequence"])
    async def test_skip_log_redacts_job_and_agent_secrets(self, tmp_path, caplog, sequence):
        """Both unresolved-agent log sites scrub the job name and reason."""
        credential_name = "AKIAIOSFODNN7EXAMPLE"
        exfil_agent = (
            "https://collect.attacker.example/?token="
            + "aB3" * 70
            + "&path=/home/alice/.aws/credentials"
        )
        gw = _make_gw_for_llm()
        kwargs = {
            "name": credential_name,
            "project_path": str(tmp_path),
            "agent_sequence" if sequence else "agent_id": (
                [exfil_agent, "second-agent"] if sequence else exfil_agent
            ),
        }
        job = _make_llm_job(**kwargs)

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"),
        ):
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.kiro_agent = "default-agent"
            mock_resolve.return_value.resolved_source = "default"
            result, stream = await _run_llm_callback(gw, job)

        assert result is None
        stream.assert_not_awaited()
        assert exfil_agent in job.last_error, "the test did not reach the named skip branch"
        emitted = "\n".join(caplog.messages)
        assert exfil_agent not in emitted, (
            f"the {'sequence' if sequence else 'single-agent'} unresolved-agent log "
            "leaked the exfiltration-shaped agent name"
        )
        assert credential_name not in emitted, (
            f"the {'sequence' if sequence else 'single-agent'} unresolved-agent log "
            "leaked the credential-shaped job name"
        )

    @pytest.mark.asyncio
    async def test_a_job_naming_no_agent_is_probed_as_the_default_not_as_a_name(self):
        """A job that selected NO agent must be probed as the default.

        ``cron_agent`` is the execution's ``template_id``, and that falls back to
        the literal ``"kirocrew"`` when the job named nothing. Handing that
        fallback to the resolver asks "is 'kirocrew' a configured alias?", which is
        False on any host that does not happen to define one -- so the guard below
        refused every job left on "Leave default for the primary agent", replacing
        the silent substitution this feature exists to stop with a silent refusal.
        ``requested_resolved`` is True for a ``None`` name by construction, which is
        the invariant the save-time check states as "an empty agent means the
        default, which always resolves"; this pins that the fire path asks the same
        question. No ``project_path`` is set, so the guard's own folder condition is
        not what carries the test.
        """
        gw = _make_gw_for_llm()
        gw.cron_svc.update_job_async = AsyncMock()
        job = _make_llm_job()
        assert not job.agent_id and not job.member_id

        def _fake_resolve(_cfg, agent_name, _project, **_kw):
            # Mirrors the loader's own rule, `(not agent_name) or alias_hit or
            # passthrough`, with no alias configured -- so a NAME reads as
            # unresolved and None reads as resolved. A flat `requested_resolved =
            # False` stub would refuse the run no matter what the fire path asked,
            # and a flat True would pass even if it asked the wrong question.
            bindings = MagicMock()
            bindings.requested_resolved = agent_name is None
            bindings.kiro_agent = "default-agent"
            bindings.resolved_source = "global"
            return bindings

        with patch("kiro_crew.slack.gateway.resolve_agent_bindings", _fake_resolve) as _:
            result, _stream_mock = await _run_llm_callback(gw, job)

        # The run is not refused: the default always resolves, and it only does so
        # here because the fire path asked about the default rather than about the
        # synthesized "kirocrew" template id.
        assert result is not None
        assert job.last_status != "error"
        _stream_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolved_project_agent_still_runs_normally(self, tmp_path):
        # Control: requested_resolved=True (the agent WAS found in the
        # project) must not trip the skip — the normal single-agent turn
        # still runs and returns its result.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value.requested_resolved = True
            mock_resolve.return_value.kiro_agent = "ea-dev"
            mock_resolve.return_value.model = ""
            mock_resolve.return_value.memory_store_name = ""
            mock_resolve.return_value.execution_context = None
            mock_resolve.return_value.resolved_alias = None
            mock_resolve.return_value.resolved_source = "project"
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result == "Agent response here"
        assert job.last_status != "error"
        assert job.run_never_started is False
        gw.sessions.get_or_create.assert_called()

    @pytest.mark.asyncio
    async def test_sequential_run_never_starts_if_a_later_member_is_unresolvable(self, tmp_path):
        # Resolving every sequence member in one pre-pass, before any session
        # is acquired, means an unresolvable later member is caught before the
        # FIRST member ever runs -- resolving inside the per-agent execution
        # loop instead would let an early member run a REAL turn before a
        # later member's unresolvable agent aborts the whole run and marks it
        # run_never_started=True, mislabeling a run that already executed.
        # The pre-pass keeps run_never_started honest.
        gw = _make_gw_for_llm()
        gw.cron_svc.update_job_async = AsyncMock()
        job = _make_llm_job(
            project_path=str(tmp_path), agent_sequence=["resolves-fine", "ghost-agent"]
        )

        def _resolve_side_effect(_cfg, agent, _project_path, **_kwargs):
            b = MagicMock()
            b.requested_resolved = agent != "ghost-agent"
            b.kiro_agent = agent
            b.model = ""
            b.memory_store_name = ""
            b.resolved_source = "project"
            return b

        with (
            patch(
                "kiro_crew.slack.gateway.resolve_agent_bindings",
                side_effect=_resolve_side_effect,
            ),
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "ghost-agent" in job.last_error
        assert job.run_never_started is True
        # The whole point of the fix: NO session was ever acquired, even for
        # the first (resolvable) member -- proving the abort happened in the
        # pre-pass, before any turn ran.
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolved_alias_model_is_not_dropped(self, tmp_path):
        # Substituting job.agent_id with _bindings.kiro_agent (the raw
        # kiro-cli agent name) so the resolved agent actually dispatches must
        # not silently drop that alias's OWN configured model tier
        # (ResolvedBindings.model) -- get_or_create must not see only the bare
        # kiro_agent name and fall through to whatever THAT agent defaults to.
        # job.model (a job-level pin) is empty here, so the alias's model must
        # be the one that reaches get_or_create.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev", model="")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value.requested_resolved = True
            mock_resolve.return_value.kiro_agent = "raw-kiro-agent-name"
            mock_resolve.return_value.model = "alias-pinned-model"
            mock_resolve.return_value.memory_store_name = ""
            mock_resolve.return_value.execution_context = None
            mock_resolve.return_value.resolved_alias = "ea-dev"
            mock_resolve.return_value.resolved_source = "alias"
            await _run_llm_callback(gw, job)

        gw.sessions.get_or_create.assert_called()
        _, kwargs = gw.sessions.get_or_create.call_args
        assert kwargs.get("model") == "alias-pinned-model"
        assert kwargs.get("agent") == "raw-kiro-agent-name"

    @pytest.mark.asyncio
    async def test_job_level_model_pin_still_outranks_the_alias_model(self, tmp_path):
        # A job-level job.model pin outranks the resolved alias's own model --
        # confirming the fix's precedence (job.model or alias_model) rather
        # than accidentally swapping which one wins.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev", model="job-pinned-model")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value.requested_resolved = True
            mock_resolve.return_value.kiro_agent = "raw-kiro-agent-name"
            mock_resolve.return_value.model = "alias-pinned-model"
            mock_resolve.return_value.memory_store_name = ""
            mock_resolve.return_value.execution_context = None
            mock_resolve.return_value.resolved_alias = "ea-dev"
            mock_resolve.return_value.resolved_source = "alias"
            await _run_llm_callback(gw, job)

        _, kwargs = gw.sessions.get_or_create.call_args
        assert kwargs.get("model") == "job-pinned-model"


class TestMemberShadowRefusalAudit:
    @staticmethod
    def _shadowed_bindings():
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED

        bindings = MagicMock()
        bindings.requested_resolved = False
        bindings.kiro_agent = "member-template"
        bindings.resolved_source = RESOLVED_SOURCE_MEMBER_SHADOWED
        return bindings

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sequence", [False, True], ids=["single-agent", "sequence"])
    async def test_refusal_emits_sel_denial(self, tmp_path, sequence):
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED

        gw = _make_gw_for_llm()
        kwargs = {
            "project_path": str(tmp_path),
            "member_id": "dev",
            "agent_sequence" if sequence else "agent_id": (
                ["member-template", "second-member"] if sequence else "member-template"
            ),
        }
        job = _make_llm_job(**kwargs)
        sel_factory = MagicMock()

        with (
            patch(
                "kiro_crew.slack.gateway.resolve_agent_bindings",
                return_value=self._shadowed_bindings(),
            ),
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch(
                "kiro_crew.cron.resolve_cron_memory",
                return_value=("dev-private", "member-template"),
            ),
        ):
            result, stream = await _run_llm_callback(gw, job, sel_factory=sel_factory)

        assert result is None
        assert job.run_never_started is True
        gw.sessions.get_or_create.assert_not_called()
        stream.assert_not_awaited()
        calls = sel_factory.return_value.log_api_access.call_args_list
        assert len(calls) == 1, "member-shadow refusal emitted no SEL denial"
        assert calls[0].kwargs == {
            "caller": f"cron:{job.id}",
            "operation": "cron.fire.project_bound_job",
            "outcome": "denied",
            "source": "cron",
            "resources": job.id,
            "error": RESOLVED_SOURCE_MEMBER_SHADOWED,
        }

    @pytest.mark.asyncio
    async def test_sel_failure_preserves_the_single_agent_refusal(self, tmp_path):
        gw = _make_gw_for_llm()
        job = _make_llm_job(
            project_path=str(tmp_path),
            member_id="dev",
            agent_id="member-template",
        )
        sel_factory = MagicMock()
        sel_factory.return_value.log_api_access.side_effect = RuntimeError("SEL unavailable")

        with (
            patch(
                "kiro_crew.slack.gateway.resolve_agent_bindings",
                return_value=self._shadowed_bindings(),
            ),
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch(
                "kiro_crew.cron.resolve_cron_memory",
                return_value=("dev-private", "member-template"),
            ),
        ):
            result, stream = await _run_llm_callback(gw, job, sel_factory=sel_factory)

        assert (
            sel_factory.return_value.log_api_access.call_count == 1
        ), "the member-shadow refusal never attempted its best-effort SEL denial"
        assert result is None, "an SEL failure escaped the clean refusal"
        assert job.run_never_started is True
        assert job.last_status == "error"
        assert "This job is bound to a Crew Member" in job.last_error
        gw.sessions.get_or_create.assert_not_called()
        stream.assert_not_awaited()


class TestMemberBoundJobWithAnOperatingFolder:
    """A member-bound job may be given a project directory, and the two concerns
    stay separate: the folder supplies the run's CWD, while the member keeps its
    own template, lineage and memory.

    Reachable in the product: the folder field renders in the crew editor's
    schedule section, which is the host that sets ``member_id``.
    Project precedence must NOT apply here: substituting a same-named project
    definition would re-base the member onto a different parent template and trip
    the ``parent_identity_changed`` guard, stopping the job from starting at all.
    """

    @pytest.mark.asyncio
    async def test_a_member_bound_job_opts_out_of_project_precedence(self, tmp_path):
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="member-template", member_id="dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch("kiro_crew.cron.resolve_cron_memory", return_value=("", "member-template")),
        ):
            mock_resolve.return_value.requested_resolved = True
            mock_resolve.return_value.kiro_agent = "member-template"
            mock_resolve.return_value.model = ""
            mock_resolve.return_value.resolved_alias = "someone-else"
            mock_resolve.return_value.resolved_source = "alias"
            await _run_llm_callback(gw, job)

        # The member keeps its lineage: precedence is explicitly disabled.
        _, resolve_kwargs = mock_resolve.call_args
        assert resolve_kwargs.get("allow_project_override") is False

    @pytest.mark.asyncio
    async def test_the_folder_still_supplies_the_cwd(self, tmp_path):
        # The other half of the split rule: opting out of project precedence must
        # not also discard the folder, which is what the operator actually asked
        # for by setting it.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="member-template", member_id="dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch("kiro_crew.cron.resolve_cron_memory", return_value=("", "member-template")),
        ):
            mock_resolve.return_value.requested_resolved = True
            mock_resolve.return_value.kiro_agent = "member-template"
            mock_resolve.return_value.model = ""
            mock_resolve.return_value.resolved_alias = "someone-else"
            mock_resolve.return_value.resolved_source = "alias"
            await _run_llm_callback(gw, job)

        _, kwargs = gw.sessions.get_or_create.call_args
        assert kwargs.get("cwd") == str(tmp_path)

    @pytest.mark.asyncio
    async def test_the_members_own_name_wins_over_the_resolved_one(self, tmp_path):
        # Map job.member_id back to its config.agents NAME: the resolver is
        # called with the member's kiro_agent, so a same-named project agent
        # can make resolved_alias fall back to a DIFFERENT member and apply
        # that member's crew-scoped effort/watchdog settings and memory namespace.
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.config.loader import KiroCrewAgentConfig

        cfg = KiroCrewConfig(
            agents={
                "Development Member": KiroCrewAgentConfig(
                    kiro_agent="member-template", member_id="dev"
                )
            }
        )
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="member-template", member_id="dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch("kiro_crew.cron.resolve_cron_memory", return_value=("", "member-template")),
        ):
            mock_resolve.return_value.requested_resolved = True
            mock_resolve.return_value.kiro_agent = "member-template"
            mock_resolve.return_value.model = ""
            mock_resolve.return_value.resolved_alias = "someone-else"
            mock_resolve.return_value.resolved_source = "alias"
            await _run_llm_callback(gw, job)

        assert gw._cron_session_binding[f"cron:{job.id}"][2] == "Development Member"


class TestAProjectFileNamedLikeTheDispatchedTemplate:
    """A bound checkout cannot borrow the job's private store by shipping a file
    named after the TEMPLATE the fire dispatches.

    The resolver's own collision check judges the requested NAME (and, under the
    member opt-out, that name's template). Neither name is the one at risk here:
    a job whose identity comes from its captured carrier dispatches
    ``execution_context.template_id`` (``resolve_agent_bindings``'s ``kiro_agent``
    is read straight off the carrier), and a job that names no agent at all is
    probed as the DEFAULT -- ``agent_name`` is ``None``, so the resolver's probe
    is not even reached. Either way the dispatched template can be a name the
    resolver never compared, the fire's cwd is the bound folder, and the backend
    resolves that template project-first -- so the project's own file answers the
    turn while the carrier still names the crew's private silo. A project's
    ``.kiro/agents`` file is writable by anyone who can land a branch in that
    checkout, which is the same "land a branch, read a crew's private memory"
    hazard ``_project_dispatch_execution`` already states for the alias-name
    collision, reached through the template name instead.
    """

    @staticmethod
    def _cfg():
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.config.loader import (
            KiroCrewAgentConfig,
            MemoryStoreConfig,
            WorkspaceConfig,
        )

        return KiroCrewConfig(
            agents={
                "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
                # The alias is spelled differently from the template it points
                # at, and the template is what a fire actually dispatches.
                "dev": KiroCrewAgentConfig(kiro_agent="dev-template", memory_store="dev-private"),
            },
            default_agent="default",
            workspaces={"default": WorkspaceConfig(dir="/tmp/ws")},
            default_workspace="default",
            memory_stores={"default": MemoryStoreConfig(), "dev-private": MemoryStoreConfig()},
            default_memory_store="default",
        )

    @staticmethod
    def _project(tmp_path, declared: str):
        d = tmp_path / ".kiro" / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{declared}.json").write_text(f'{{"name": "{declared}"}}', encoding="utf-8")
        return str(tmp_path)

    @staticmethod
    def _carrier():
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        # The shape a cron created from a chat session on alias "dev" persists:
        # the crew's private store, and the crew's TEMPLATE as the dispatched id.
        return ExecutionContext(
            member_id=None,
            store=MemoryStoreRef("dev-private"),
            selection_kind="member",
            template_id="dev-template",
            selection_name="dev",
        )

    async def _dispatched_execution(self, gw, job, monkeypatch):
        from kiro_crew import execution_context
        from kiro_crew.agent_discovery import clear_project_agent_cache

        published: list = []
        original = execution_context.bind_session_execution

        def _bind(key, execution, **kw):
            published.append(execution)
            return original(key, execution, **kw)

        monkeypatch.setattr(execution_context, "bind_session_execution", _bind)
        clear_project_agent_cache()
        with patch("kiro_crew.slack.gateway.KiroCrewConfig.load", return_value=self._cfg()):
            await _run_llm_callback(gw, job)
        assert published, "the fire never bound a session execution"
        return published[0]

    @pytest.mark.asyncio
    async def test_the_projects_file_does_not_inherit_the_private_store(
        self, tmp_path, monkeypatch
    ):
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            execution_context=self._carrier().to_record(),
        )
        execution = await self._dispatched_execution(_make_gw_for_llm(), job, monkeypatch)

        assert execution.store.store_id == "default", (
            "the bound checkout ships a file named after the dispatched template, "
            "so its own definition answers the turn -- under the crew's private "
            "memory store"
        )
        assert execution.member_id is None
        # Still dispatched: the project file is what runs, so re-pointing the
        # STORE must not also make the fire skip.
        assert execution.template_id == "dev-template"

    @pytest.mark.asyncio
    async def test_a_checkout_declaring_another_name_keeps_the_private_store(
        self, tmp_path, monkeypatch
    ):
        # Control: the re-point is driven by the COLLISION, not by binding a
        # folder. A checkout that declares some other agent cannot reach the
        # dispatched template, so the job keeps its own silo.
        job = _make_llm_job(
            project_path=self._project(tmp_path, "unrelated"),
            execution_context=self._carrier().to_record(),
        )
        execution = await self._dispatched_execution(_make_gw_for_llm(), job, monkeypatch)

        assert execution.store.store_id == "dev-private"
        assert execution.template_id == "dev-template"

    @pytest.mark.asyncio
    async def test_a_sequence_member_does_not_inherit_the_private_store(
        self, tmp_path, monkeypatch
    ):
        # The sequence path resolves each member WITHOUT the carrier, so its
        # dispatched template comes from the alias record instead -- and the
        # job-level carrier (which can name a private store) is what a member
        # falls through to. Same collision, second path.
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            agent_sequence=["dev"],
            execution_context=self._carrier().to_record(),
        )
        execution = await self._dispatched_execution(_make_gw_for_llm(), job, monkeypatch)

        assert execution.store.store_id == "default"
        assert execution.template_id == "dev-template"

    @pytest.mark.asyncio
    async def test_the_collision_check_reads_no_project_filesystem_on_the_loop(
        self, tmp_path, monkeypatch
    ):
        # The check runs on the event loop, so it must consult the WARMED name
        # cache and nothing else -- the same rule _project_declares_agent states
        # for its own on-loop read, and for the same reason: a scan here is an
        # unbounded stall on chat, WebSocket and heartbeat processing. Widening
        # this into a fresh scan would make every project-bound fire pay for a
        # checkout walk.
        import kiro_crew.agent_discovery as ad

        loop_thread = threading.get_ident()
        scanned: list[int] = []
        real_scan = ad.project_agent_names

        def _scan(*args, **kwargs):
            scanned.append(threading.get_ident())
            return real_scan(*args, **kwargs)

        monkeypatch.setattr(ad, "project_agent_names", _scan)
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            execution_context=self._carrier().to_record(),
        )
        execution = await self._dispatched_execution(_make_gw_for_llm(), job, monkeypatch)

        assert execution.store.store_id == "default"
        assert scanned, "nothing scanned the checkout at all -- the warm is the hit's source"
        assert all(thread != loop_thread for thread in scanned), (
            "the dispatched-template collision check scanned the project ON the "
            "event loop; it must read the cache the warm populated"
        )

    @pytest.mark.asyncio
    async def test_a_cold_name_cache_still_fires_the_job(self, tmp_path, monkeypatch):
        # The documented residual, pinned rather than left to be rediscovered: with
        # nothing warmed there is no answer available without touching the
        # filesystem on the loop, so the run proceeds on its captured store exactly
        # as it did before this check existed. Stated here so a reader knows the
        # degrade is a choice, and so "make it fail closed" is a decision with a
        # test to change rather than a silent one.
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            execution_context=self._carrier().to_record(),
        )
        gw = _make_gw_for_llm()
        with patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()):
            execution = await self._dispatched_execution(gw, job, monkeypatch)

        assert execution.store.store_id == "dev-private"


class TestSessionReuseNoticesWhichDefinitionWon:
    """The session-reuse key carries the resolved SOURCE, so project-agent
    precedence holds in steady state rather than only at cold start.

    A live persistent session is reused regardless of the cwd/agent passed to
    ``get_or_create``. Dropping a ``<project>/.kiro/agents/<name>.json`` beside a
    same-named alias changes WHICH definition answers while the cwd and the
    requested name both stay put -- and where the crew's template is spelled like
    the crew on a host whose ``default_agent`` is that crew, the resolved agent and
    alias are identical too. Without the source tag a job firing every few minutes
    would keep running the losing definition until an idle eviction.
    """

    @staticmethod
    def _binding_mock(source: str):
        b = MagicMock()
        b.requested_resolved = True
        # Deliberately IDENTICAL across both fires -- only the source differs.
        b.kiro_agent = "dev"
        b.model = ""
        b.memory_store_name = ""
        b.execution_context = None
        b.resolved_alias = "dev"
        b.resolved_source = source
        return b

    @staticmethod
    def _gw_with_pending_subagents():
        gw = _make_gw_for_llm()
        gw.subagent_mgr = MagicMock()
        gw.subagent_mgr.has_pending_work_for = MagicMock(
            side_effect=AssertionError("sync pending probe called")
        )
        gw.subagent_mgr.has_pending_work_for_async = AsyncMock(return_value=True)
        return gw

    @pytest.mark.asyncio
    async def test_a_source_change_alone_defers_the_fire(self, tmp_path):
        # The deferral is the unambiguous observable: a binding mismatch with
        # subagents pending returns without running the turn, and NOTHING else in
        # this path produces that. (A plain reset is not a usable signal here --
        # the run's own finally block releases and resets every fire.)
        gw = self._gw_with_pending_subagents()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value = self._binding_mock("alias")
            await _run_llm_callback(gw, job)
            assert gw._cron_session_binding[f"cron:{job.id}"][3] == "alias"

            # A project file now shadows the alias. cwd, requested name, resolved
            # agent and resolved crew alias are all unchanged.
            mock_resolve.return_value = self._binding_mock("project")
            result, stream_mock = await _run_llm_callback(gw, job)

        assert result is None, "the fire should have been deferred"
        stream_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unchanged_source_does_not_defer(self, tmp_path):
        # The control: identical bindings across fires must NOT defer, or every
        # persistent job with a live subagent would stall forever.
        gw = self._gw_with_pending_subagents()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            mock_resolve.return_value = self._binding_mock("alias")
            await _run_llm_callback(gw, job)
            mock_resolve.return_value = self._binding_mock("alias")
            result, stream_mock = await _run_llm_callback(gw, job)

        assert result is not None
        stream_mock.assert_awaited()


class TestThrottleFallbackCronWiring:
    """agent.fallback_model reaches the cron turn, and a fallback-served run
    is visibly annotated (never silent)."""

    @pytest.mark.asyncio
    async def test_chain_is_threaded_into_stream_and_collect(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        with patch(
            "kiro_crew.slack.gateway.configured_fallback_chain",
            return_value=("fb-1", "fb-2"),
        ):
            _result, stream_mock = await _run_llm_callback(gw, job)
        assert stream_mock.await_args.kwargs["fallback_models"] == ("fb-1", "fb-2")

    @pytest.mark.asyncio
    async def test_fallback_served_run_is_annotated(self):
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        provider_mock = MagicMock()
        setattr(provider_mock, TURN_FALLBACK_ATTR, ("primary-m", "fb-m"))

        async def _side_effect(*args, **kwargs):
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert result.startswith("⚠️ Model 'primary-m' throttled")
        assert "fb-m" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_fallback_served_run_blanks_pinned_model_in_usage_row(self):
        """A pinned job.model must NOT be billed while a fallback served the
        turn — the usage row blanks it so model_source reports what ran."""
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="pinned-model")
        provider_mock = MagicMock()
        setattr(provider_mock, TURN_FALLBACK_ATTR, ("pinned-model", "fb-m"))

        async def _side_effect(*args, **kwargs):
            return (provider_mock, True, False)

        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", new_callable=AsyncMock
        ) as persist_mock:
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert persist_mock.await_count >= 1
        # Positional arg 1 is the model; blank = defer to model_source.
        assert persist_mock.await_args.args[1] == ""

    @pytest.mark.asyncio
    async def test_unswapped_run_still_bills_pinned_model(self):
        """No fallback marker -> the explicit pin is recorded unchanged."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="pinned-model")
        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", new_callable=AsyncMock
        ) as persist_mock:
            await _run_llm_callback(gw, job)
        assert persist_mock.await_count >= 1
        assert persist_mock.await_args.args[1] == "pinned-model"

    def test_annotate_noop_without_marker(self):
        from types import SimpleNamespace

        from kiro_crew.slack.gateway import _annotate_model_fallback

        provider = SimpleNamespace()
        assert _annotate_model_fallback("text", provider) == "text"

    def test_annotate_malformed_marker_is_noop(self):
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR
        from kiro_crew.slack.gateway import _annotate_model_fallback

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("only-one",))
        assert _annotate_model_fallback("text", provider) == "text"

    def test_gateway_annotator_is_the_shared_body(self):
        """DRIFT PIN: the gateway name must BE the shared
        helper next to TURN_FALLBACK_ATTR — not a re-spelled copy."""
        from kiro_crew.llm_helpers import annotate_model_fallback
        from kiro_crew.slack.gateway import _annotate_model_fallback

        assert _annotate_model_fallback is annotate_model_fallback

    @pytest.mark.asyncio
    async def test_chain_exhaustion_story_reaches_the_failure_alert(self):
        """A cron turn failing after the chain exhausted must
        alert with the WHOLE walk (the story the walk attached to the
        exception), not just the last candidate's error — on BOTH the
        dashboard notify and the Slack DM legs, and even when the backend
        error is verbose enough to hit the detail cap (a real ACP throttle
        payload routinely exceeds it)."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        # The Slack DM leg: slack client present, no channel delivery.
        gw.slack = MagicMock()
        gw.slack.post_message = AsyncMock()
        gw._open_dm_with_retry = AsyncMock(return_value="D123")
        gw._deliver_cron_to_channel = AsyncMock(return_value=False)

        # Verbose error: longer than _CRON_FAILURE_DETAIL_CAP so the story
        # would be sliced away if it were appended after the cap.
        exc = AcpError("Internal error: API Error: " + "x" * 700)
        setattr(
            exc, FALLBACK_STORY_ATTR, "primary-m throttled; fallbacks fb-1, fb-2 also unavailable"
        )

        captured_cb = None
        provider_mock = MagicMock()
        gw.sessions.get_or_create = AsyncMock(return_value=(provider_mock, True, False))
        _embed_mock = AsyncMock(return_value=("full prompt", None))
        _stream_mock = AsyncMock(side_effect=exc)

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.slack.gateway.run_in_embed_pool", _embed_mock),
            patch("kiro_crew.slack.gateway.stream_and_collect", _stream_mock),
            patch("kiro_crew.slack.gateway.sel"),
            patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
        ):
            mock_ctx.return_value = (f"cron:{job.id}", job.message)

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
            with pytest.raises(AcpError):
                await captured_cb(job)

            # Same backend error, DIFFERENT walk (a candidate momentarily
            # unadvertised changes the story): the dedup hash keys on the
            # story-free error text, so the repeat is suppressed instead of
            # re-paging the user.
            exc2 = AcpError("Internal error: API Error: " + "x" * 700)
            setattr(
                exc2, FALLBACK_STORY_ATTR, "primary-m throttled; fallbacks fb-2 also unavailable"
            )
            _stream_mock.side_effect = exc2
            with pytest.raises(AcpError):
                await captured_cb(job)

        bodies = [
            str(c.args[2]) for c in gw.dashboard_state.notify.call_args_list if len(c.args) >= 3
        ]
        assert any(
            "primary-m throttled" in b and "fb-1, fb-2 also unavailable" in b for b in bodies
        ), f"expected the chain story in a failure alert, got {bodies!r}"
        # The delivered detail stays bounded by the cap even with the story
        # appended (the budget trims the error part). +60 absorbs the fixed
        # "❌ Job failed:\n" / "❌ Job failed (suppressed — same error):\n"
        # prefixes around exc_detail — the bound under test is exc_detail's.
        from kiro_crew.slack.gateway import _CRON_FAILURE_DETAIL_CAP

        for b in bodies:
            assert len(b) <= _CRON_FAILURE_DETAIL_CAP + 60, f"unbounded alert body: {len(b)} chars"
        # The Slack DM — the surface a user actually gets paged on — carries
        # the story too.
        slack_texts = [str(c.args[1]) for c in gw.slack.post_message.call_args_list]
        assert any(
            "primary-m throttled" in t and "also unavailable" in t for t in slack_texts
        ), f"expected the chain story in the Slack alert, got {slack_texts!r}"
        # The second run took the dup-suppress path (story-free hash), with a
        # bounded body that still carries its own walk.
        assert any("suppressed — same error" in b for b in bodies), bodies
        assert any("fb-2 also unavailable" in b for b in bodies), bodies


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


class TestCronUsageRow:
    """Every model-spending cron turn appends exactly one usage row
    tagged surface='cron'; the zero-token script/command modes append none."""

    @pytest.mark.asyncio
    async def test_llm_cron_persists_usage_row_with_surface(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        persist = AsyncMock()
        # Patch gateway's own bindings: the imports are at module scope there,
        # so patching the source module would not be seen by the call site.
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(1234, 200000)),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job)

        persist.assert_awaited_once()
        kwargs = persist.await_args.kwargs
        assert kwargs["surface"] == "cron"
        assert kwargs["provider"] == "acp"
        assert kwargs["context_used"] == 1234
        assert kwargs["context_window"] == 200000

    @pytest.mark.asyncio
    async def test_cron_row_records_resolved_agent_not_requested(self):
        """The agent that served the turn wins over the configured alias."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        persist = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(10, 100)),
                create=True,
            ),
            patch(
                "kiro_crew.slack.gateway.read_effective_agent",
                MagicMock(return_value="kirocrew"),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job)

        persist.assert_awaited_once()
        assert persist.await_args.kwargs["agent"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_downgraded_cron_does_not_record_rejected_model(self):
        """A model that was refused never ran, so it must not be attributed."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-nonexistent-9")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-nonexistent-9' not found")
            return (provider_mock, True, False)

        persist = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(10, 100)),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)

        persist.assert_awaited_once()
        # Positional arg 1 is the model; blank defers to model_source, which
        # reports the model that actually served the turn.
        assert persist.await_args.args[1] == ""

    @pytest.mark.asyncio
    async def test_script_cron_writes_no_usage_row(self):
        gw = _make_gw()
        job = _make_script_job()

        persist = AsyncMock()
        with patch("kiro_crew.slack.gateway.persist_token_record_async", persist):
            await _run_script_callback(gw, job, {"status": "ok"})

        persist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_command_cron_writes_no_usage_row(self):
        gw = _make_gw()
        job = _make_command_job()

        persist = AsyncMock()
        with patch("kiro_crew.slack.gateway.persist_token_record_async", persist):
            await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )

        persist.assert_not_awaited()


def test_shutdown_cancel_keeps_the_last_completed_result(tmp_path) -> None:
    """A shutdown cancel must not wipe the previous run's result.

    stop() cancels the in-flight task but never adds the job to _cancelled_jobs,
    so the funnel's result-less clear would otherwise run on every gateway stop
    and persist an empty result over the last completed run's output.
    """
    import asyncio

    from kiro_crew.cron import CronJob, CronSchedule, CronService

    async def _hang(*args, **kwargs):
        await asyncio.sleep(9999)

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
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
        try:
            svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
            job = _make_command_job(timeout_secs=2)
            for _ in range(ex._MAX_CRON_WORKERS):
                ex.cron_executor().submit(release.wait, 3.0)
            await asyncio.sleep(0.2)

            ran = threading.Event()

            async def _execute(_job, _meta=None):
                return await ex.run_in_cron_pool(ran.set, timeout=30, queue_timeout=60)

            svc._execute = _execute  # type: ignore[method-assign]
            await svc._execute_with_timeout(job)

            assert ran.is_set(), "the job was killed by the wake deadline while still queued"
            assert job.last_error != "Timed out after 2s"
        finally:
            release.set()
            await asyncio.sleep(0.05)
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
            await asyncio.sleep(30)

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
        with patch("kiro_crew.cron._REAPER_INTERVAL", 0.01):
            loop_task = asyncio.ensure_future(svc._reaper_loop())
            await asyncio.sleep(0.25)
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
                for _ in range(ex._MAX_CRON_WORKERS):
                    ex.cron_executor().submit(release.wait, 5.0)
                await asyncio.sleep(0.2)

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
            await asyncio.sleep(0.05)
            ex.shutdown_maintenance_executor()

    @pytest.fixture
    def gate_pool(self):
        """Fresh pools, plus a release event so a starved gate never outlives the test."""
        ex.shutdown_maintenance_executor()
        release = threading.Event()
        try:
            yield release
        finally:
            release.set()
            ex.shutdown_maintenance_executor()

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
                for _ in range(ex._MAX_CRON_GATE_WORKERS):
                    ex.cron_gate_executor().submit(release.wait, 20.0)
                await asyncio.sleep(0.2)

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
    ``cron.py``'s delete site reads ``run_never_started`` to decide whether a
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
        still_present = any(j.id == created.id for j in svc.list_jobs())
        assert still_present is expect_retained, (
            "the one-shot was deleted by a run that never dispatched"
            if expect_retained
            else "the one-shot survived a run that did dispatch"
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
    ran". So a shutdown or wake deadline landing mid-vet reached ``cron.py``'s
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


class TestTheSkipMessageNamesTheRealReason:
    """The ``last_error`` an unresolved agent leaves on a skipped fire.

    This string is the ONLY signal the operator gets for these skips: they
    spend no auto-pause strike by design, so nothing else escalates and the
    text itself has to carry the remedy. Three shapes, and the third is why
    the wording is branched at all -- a member-bound job shadowed by its bound
    directory refuses BECAUSE the agent is present there, so the unbranched
    "not found in project directory" would state the opposite of the fact and
    send the operator to create a file that already exists.
    """

    def test_an_unbound_job_says_the_name_is_gone(self):
        from kiro_crew.slack.gateway import _agent_unresolved_message

        job = _make_script_job(project_path="")
        msg = _agent_unresolved_message(job, "ghost")
        assert "Agent 'ghost' is no longer configured" in msg
        # Every branch names a NEXT STEP, not just a diagnosis (UX Review).
        assert "Pick another agent" in msg, "the unbound skip named no remedy"

    def test_a_bound_job_names_the_directory_to_fix(self):
        from kiro_crew.slack.gateway import _agent_unresolved_message

        job = _make_script_job(project_path="/repo")
        msg = _agent_unresolved_message(job, "ghost")
        assert "not found in this job's project directory" in msg
        # The directory to CREATE the file in, not merely the one it is missing
        # from: an operator should not have to know the .kiro/agents/ convention
        # to act on this. The CONVENTION is named; the folder itself is not (see
        # the disclosure test below) -- the owner reads the folder off the job.
        assert ".kiro/agents/" in msg, "the skip did not say where to add the agent"
        assert "/repo" not in msg

    def test_the_member_shadow_refusal_does_not_claim_the_agent_is_missing(self):
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED
        from kiro_crew.slack.gateway import _agent_unresolved_message

        job = _make_script_job(project_path="/repo")
        msg = _agent_unresolved_message(job, "default", RESOLVED_SOURCE_MEMBER_SHADOWED)

        assert "not found" not in msg, (
            "the shadow refusal reported the agent as missing from the very "
            "directory that declares it -- the operator would go add a file "
            "that is already there, when the fix is to rename it or unbind "
            "the member"
        )
        assert "project directory" in msg, "the refusal did not say WHERE the shadowing file is"
        assert "/repo" not in msg
        assert "Crew Member" in msg, "the refusal did not say WHAT the project file collides with"
        # The remedies, not just the diagnosis: this text is the whole signal.
        assert "Rename" in msg and "unbind" in msg, (
            "the refusal named no remedy, leaving the operator with a skipped "
            "job and no stated way to clear it"
        )

    def test_the_member_shadow_refusal_leads_with_the_plain_fact(self):
        """UX Review: the state, in plain words, before any remedy or mechanism.

        Internal vocabulary in the opening clause -- shadowing, a member's
        private memory -- spends the one moment the operator most needs plain
        words, on a state rare enough that they are reading it for the first
        time. This string is their only signal, so it states WHAT IS TRUE first
        (the job is bound to a Crew Member, and the directory defines an agent
        of the same name), then the consequence, then the two remedies.
        """
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED
        from kiro_crew.slack.gateway import _agent_unresolved_message

        job = _make_script_job(project_path="/repo")
        msg = _agent_unresolved_message(job, "default", RESOLVED_SOURCE_MEMBER_SHADOWED)

        assert msg.startswith("This job is bound to a Crew Member"), (
            "the refusal did not open on the plain fact; the operator reads "
            f"mechanism before state: {msg!r}"
        )
        assert "the same name" in msg, "the refusal never plainly said the names collide"
        # The flagged vocabulary, gone: neither term survives anywhere in the
        # sentence, not merely moved later in it.
        for jargon in ("shadow", "private memory"):
            assert jargon not in msg, f"internal vocabulary {jargon!r} survived in {msg!r}"
        # The remedies keep their own clause and stay LAST -- a remedy read
        # before the state it repairs is a remedy for nothing.
        assert msg.index("Rename the project's agent") > msg.index("the same name")

    @pytest.mark.asyncio
    async def test_no_persisted_skip_reason_names_the_bound_folder(self, tmp_path):
        """``last_error`` is read past the owner boundary; the folder is not.

        ``GET /api/crons`` serializes ``last_error`` for every dashboard token and
        withholds ``project_path`` from a non-owner; the run-history routes
        serialize the same string as a run's ``summary``/``error`` with no owner
        gate at all, and ``cron_list`` shows it in chat. So the skip REASON is
        readable by a non-owner by design (it is the only diagnosis a skip
        leaves), and the folder must therefore not ride inside it -- neither the
        path itself nor its ``<path>/.kiro/agents/`` layout. Every persisted skip
        builder is covered here: the two ``_agent_unresolved_message`` shapes for
        a bound job and the vanished-directory skip. The path stays in the log
        line, which is where an operator-only diagnosis belongs.
        """
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED
        from kiro_crew.slack.gateway import (
            _agent_unresolved_message,
            _project_directory_vanished,
        )

        folder = str(tmp_path / "private-repo")
        job = _make_script_job(project_path=folder)
        for msg in (
            _agent_unresolved_message(job, "ghost"),
            _agent_unresolved_message(job, "ghost", RESOLVED_SOURCE_MEMBER_SHADOWED),
        ):
            assert folder not in msg, msg
            assert "private-repo" not in msg, msg
        assert await _project_directory_vanished(job) is True
        assert job.last_status == "error"
        assert folder not in job.last_error, job.last_error
        assert "private-repo" not in job.last_error, job.last_error
        assert "no longer exists" in job.last_error

    @pytest.mark.asyncio
    async def test_vanished_directory_log_line_does_not_carry_the_raw_folder(
        self, tmp_path, caplog
    ):
        """The vanished-directory skip's log line must not carry the raw path.

        The dashboard log subscription gate (``dashboard/ws.py``'s
        ``subscribe_logs`` handler) admits any ``_is_dashboard_user`` socket --
        that flag is set for every authenticated dashboard token holder,
        including a non-owner ``!dashboard`` Slack subject (``app == ""`` sets
        ``is_dashboard_user = not _app = True``), not only the configured
        ``owner_id``. ``is_owner_dashboard_request`` is a distinct, stricter
        predicate this gate never calls. So the log stream is a
        dashboard-*user* surface, not an owner-only one, and a folder path
        logged there reaches the same non-owner the cron owner gate exists to
        keep it from. The path stays useful to the operator as the folder's
        last path segment (``private-repo``) is still present; the fix is to
        strip it via :func:`~kiro_crew.security.redaction.redact_local_paths`
        the same way every other subprocess/OS-error surface that reaches a
        browser does, not to drop the diagnosis outright.
        """
        import logging

        from kiro_crew.slack.gateway import _project_directory_vanished

        folder = str(tmp_path / "private-repo")
        credential_name = "AKIAIOSFODNN7EXAMPLE"
        job = _make_script_job(project_path=folder, name=credential_name)

        with caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"):
            assert await _project_directory_vanished(job) is True

        full_text = "\n".join(caplog.messages)
        assert (
            credential_name not in full_text
        ), "the vanished-directory log leaked the credential-shaped job name"
        assert folder not in full_text, (
            "the vanished-directory log line carried the raw project folder "
            "path, which a non-owner dashboard-token holder (a `!dashboard` "
            "Slack subject) can read via the WS log subscription -- that gate "
            "checks only `_is_dashboard_user`, never `is_owner_dashboard_request`"
        )
