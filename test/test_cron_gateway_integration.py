"""Integration tests for script/command cron execution in the gateway.

Tests the actual _cron_callback dispatch for script and command jobs,
including delivery, concurrency guard, timeout handling, and Report().
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import (
    _make_command_job,
    _make_gw,
    _make_gw_for_llm,
    _make_llm_job,
    _make_script_job,
    _run_command_callback,
    _run_llm_callback,
    _run_script_callback,
)

from kiro_crew.cron import CronJob, CronSchedule


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
        # CronScheduler._execute, whose reset is guarded by the cancel markers
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

        Cleared rather than marked with a literal: last_status carries the verdict, and
        any literal stored here is also legal job output.
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

    Same gate as command jobs, through mcp_cron.vet_job_at_fire_time. The mcp_cron
    privates are patched, not the helper, so its real dispatch logic runs.
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


class TestCronDefaultAgent:
    """The configured default agent must reach dispatch for an agent-less cron.

    ``_cron_callback`` captures its selectors up front, so a default resolved below
    that capture reaches nothing and the cron silently runs as the ``CLIENT_NAME``
    floor. The ``agent`` kwarg ``get_or_create`` receives is the only place to see it.
    """

    @staticmethod
    def _dispatched_agent(gw):
        return gw.sessions.get_or_create.call_args_list[0].kwargs["agent"]

    @pytest.mark.asyncio
    async def test_agentless_job_runs_the_configured_default(self):
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="")

        result, _ = await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "configured-default"
        assert result == "Agent response here"

    @pytest.mark.asyncio
    async def test_explicit_agent_id_is_not_overridden(self):
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="pinned-agent")

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "pinned-agent"

    @pytest.mark.asyncio
    async def test_whitespace_agent_id_is_treated_as_absent(self):
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="   ")

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "configured-default"

    @pytest.mark.asyncio
    async def test_no_configured_default_keeps_the_client_name_floor(self):
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = ""
        job = _make_llm_job(agent_id="")

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "kirocrew"

    @pytest.mark.asyncio
    async def test_non_str_configured_default_degrades_to_the_floor(self):
        """A non-str configured default must not reach ``template_id``.

        ``ExecutionContext.__post_init__`` requires a str template_id, so an
        unset (or, here, mocked) config attribute that is merely truthy and
        ``.strip()``-able would abort the whole dispatch with "missing
        execution template" instead of degrading to the floor.
        """
        gw = _make_gw_for_llm()
        # _make_gw_for_llm leaves _cfg a MagicMock, so .default_agent is a
        # MagicMock: truthy and .strip()-able, but not a str.
        assert not isinstance(gw._cfg.agent.default_agent, str)
        job = _make_llm_job(agent_id="")

        result, _ = await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "kirocrew"
        assert result == "Agent response here"

    @pytest.mark.asyncio
    async def test_a_retained_session_under_another_agent_defers_the_fire(self):
        """A live session built for the PREVIOUS default must not be reused.

        The reset is deferred while sub-agents are pending, and ``get_or_create`` claims
        the live ``cron:{job.id}`` session without comparing the ``agent`` kwarg. Deferring
        is the only safe branch: resetting would destroy the pending sub-agent work.
        """
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        gw.sessions._get_session_agent = MagicMock(return_value="previous-default")
        job = _make_llm_job(agent_id="")

        result, stream = await _run_llm_callback(gw, job)

        assert gw.sessions.get_or_create.call_args_list == []
        stream.assert_not_awaited()
        assert result is None
        # The retention marker, not a failure: a deferred fire must not count
        # toward auto-pause, and must not consume a delete_after_run job.
        assert job.run_never_started is True
        assert job.last_status == "error"
        assert "previous-default" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_retained_session_under_the_same_agent_still_fires(self):
        """Same agent both fires: nothing to protect, so nothing is deferred."""
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        gw.sessions._get_session_agent = MagicMock(return_value="configured-default")
        job = _make_llm_job(agent_id="")

        result, _ = await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "configured-default"
        assert result == "Agent response here"
        assert job.run_never_started is False

    @pytest.mark.asyncio
    async def test_an_unreadable_live_agent_does_not_defer_the_fire(self):
        """No readable agent is NOT evidence of a mismatch.

        The guard fires on positive evidence only: deferring on an answer that is not a
        non-empty ``str`` would stall every agent-less cron on such a host forever.
        """
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        # A bare MagicMock attribute: truthy, != the dispatched name, not a str.
        assert not isinstance(gw.sessions._get_session_agent("cron:x"), str)
        job = _make_llm_job(agent_id="")

        result, _ = await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "configured-default"
        assert result == "Agent response here"

    @pytest.mark.asyncio
    async def test_a_pinned_agent_defers_past_a_session_retained_under_another(self):
        """A job pinning its own agent can change it through ``cron update``.

        The update re-points the captured template, so the next fire may
        dispatch a different name while the previous fire's session is still
        retained for pending sub-agents. That fire is deferred, exactly as for a
        changed default, rather than run on the previous agent's process.
        """
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        gw.sessions._get_session_agent = MagicMock(return_value="previous-agent")
        job = _make_llm_job(agent_id="pinned-agent")

        result, stream = await _run_llm_callback(gw, job)

        assert gw.sessions.get_or_create.call_args_list == []
        stream.assert_not_awaited()
        assert result is None
        assert job.run_never_started is True
        assert "previous-agent" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_pinned_agent_fires_past_a_session_retained_under_the_same(self):
        gw = _make_gw_for_llm()
        gw.sessions._get_session_agent = MagicMock(return_value="pinned-agent")
        job = _make_llm_job(agent_id="pinned-agent")

        result, _ = await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "pinned-agent"
        assert result == "Agent response here"
        assert job.run_never_started is False

    @pytest.mark.asyncio
    async def test_a_dispatching_sequence_keeps_its_own_agents(self):
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="", agent_sequence=["seq-a", "seq-b"])

        await _run_llm_callback(gw, job)

        dispatched = [c.kwargs.get("agent") for c in gw.sessions.get_or_create.call_args_list]
        assert "configured-default" not in dispatched

    @pytest.mark.asyncio
    async def test_the_shared_job_record_is_never_mutated(self):
        """The scheduler hands the callback its LIVE stored CronJob.

        ``_save()`` reserialises ``self._jobs``, so writing the default onto
        ``job.agent_id`` would pin it in ``crons.json`` and stop tracking
        ``agent.default_agent``.
        """
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="")

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "configured-default"
        assert job.agent_id == "", "the stored CronJob must not carry the resolved default"

    @pytest.mark.asyncio
    async def test_a_dormant_lone_sequence_still_gets_the_default(self):
        """A one-entry sequence is dormant (agent_sequence_dispatches is False).

        Dispatch falls through to agent_id, so the default still applies --
        gating on a bare ``job.agent_sequence`` would deny it here.
        """
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="", agent_sequence=["lonely"])

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "configured-default"

    @pytest.mark.asyncio
    async def test_default_agent_does_not_enter_the_captured_execution(self, monkeypatch):
        """The resolved default must reach dispatch WITHOUT entering the bind.

        Every fire re-binds the stable ``cron:{job.id}`` key with
        ``replace_existing`` False, which raises when the candidate differs from the
        recorded ``template_id="kirocrew"``; the job would fail every fire and auto-pause.
        """
        from kiro_crew import execution_context

        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="")

        bound = []
        original = execution_context.bind_session_execution

        def bind(key, execution):
            bound.append(execution)
            return original(key, execution)

        monkeypatch.setattr(execution_context, "bind_session_execution", bind)
        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "configured-default"
        assert bound, "bind_session_execution was never called"
        assert bound[0].template_id == "kirocrew", (
            "the configured default leaked into the captured execution identity; "
            "a second fire on the stable cron: key would raise "
            "'session already belongs to another execution'"
        )

    @pytest.mark.asyncio
    async def test_a_captured_non_floor_template_keeps_its_own_agent(self):
        """A record carrying a REAL captured template must not be re-pointed.

        A schedule made from a template chat names its template only in
        ``execution_context.template_id`` with ``agent_id`` empty, so the override is
        gated on the resolved agent still being the floor.
        """
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        captured = ExecutionContext(None, MemoryStoreRef("default"), "template", "researcher")
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="", execution_context=captured.to_record())

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "researcher"

    @pytest.mark.asyncio
    async def test_a_captured_floor_template_still_gets_the_default(self):
        """An agent-less job whose captured ``template_id`` is the ``"kirocrew"`` floor.

        Post-migration records all carry an ``execution_context``, so a gate inside the
        legacy arm alone would reach only pre-migration jobs.
        """
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        captured = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="", execution_context=captured.to_record())

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "configured-default"

    @pytest.mark.asyncio
    async def test_a_member_capture_spelling_the_floor_keeps_its_own_agent(self):
        """A member execution is a real selection even when it spells the floor.

        ``resolve_member_execution`` sets ``template_id`` to
        ``agent.kiro_agent or "kirocrew"``. Substituting would inject another template's
        prompt into the member's envelope, so the override needs ``member_id`` unset.
        """
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        captured = ExecutionContext(
            "alice", MemoryStoreRef("alice-store", "alice"), "member", "kirocrew"
        )
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="", execution_context=captured.to_record())

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) != "configured-default"

    @pytest.mark.asyncio
    async def test_a_legacy_member_with_no_member_id_keeps_its_own_agent(self):
        """``member_id`` alone does not identify a member capture.

        A member predating the identity migration persists no ``member_id`` and is named
        by ``selection_kind == 'member'`` alone, so the gate tests ``selection_kind``.
        """
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        captured = ExecutionContext(
            None, MemoryStoreRef("legacy-global-store"), "member", "kirocrew"
        )
        assert captured.member_id is None, "the legacy shape under test"
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="", execution_context=captured.to_record())

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) != "configured-default"

    @pytest.mark.asyncio
    async def test_an_unmigrated_member_schedule_keeps_its_own_agent(self):
        """A pre-migration member cron carries its membership on the JOB, not the capture.

        With ``job.execution_context`` ``None`` the capture is rebuilt by
        ``execution_for_store`` with a null ``member_id`` and the ``"template"``
        namespace, so it passes both capture gates; only ``job.member_id`` names the member.
        """
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="", member_id="alice", memory_store="default")
        assert job.execution_context is None, "the unmigrated shape under test"

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) != "configured-default"

    @pytest.mark.asyncio
    async def test_a_default_declaring_a_spawn_allowlist_keeps_the_floor(self, monkeypatch):
        """A restricted default cannot be dispatched while the capture names the floor.

        The spawn gate reads the parent's ``availableAgents`` from the bound capture,
        which names the floor, so the substituted template's declaration would never be
        consulted and a child it forbids would start.
        """
        from kiro_crew import subagent as _subagent

        monkeypatch.setattr(
            _subagent, "parent_spawn_allowlists", lambda template: (("only-this-child",),)
        )
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="")

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) != "configured-default"

    @pytest.mark.asyncio
    async def test_an_unknown_spawn_allowlist_keeps_the_floor(self, monkeypatch):
        """``None`` is the reader's UNKNOWN and must DECLINE -- deny by default.

        The spawn gate does not catch it: for the floor name it gets ``()`` and admits.
        ``None`` means a spec file in the user-writable agents dir was refused, where a
        restrictive declaration could hide.
        """
        from kiro_crew import subagent as _subagent

        monkeypatch.setattr(_subagent, "parent_spawn_allowlists", lambda template: None)
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "configured-default"
        job = _make_llm_job(agent_id="")

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) != "configured-default"

    @pytest.mark.asyncio
    async def test_a_cleared_default_still_defers_a_session_held_by_the_old_one(self):
        """The window is an AGENT-LESS JOB, not "this fire substituted".

        Fire N substitutes ``researcher`` and defers its reset; the operator then clears
        the default, so fire N+1 substitutes nothing. A guard keyed on
        ``_default_substituted`` would skip and run the floor on ``researcher``'s process.
        """
        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = ""  # the operator cleared it between fires
        gw.sessions._get_session_agent = MagicMock(return_value="researcher")
        job = _make_llm_job(agent_id="")

        result, stream = await _run_llm_callback(gw, job)

        assert gw.sessions.get_or_create.call_args_list == []
        stream.assert_not_awaited()
        assert result is None
        assert job.run_never_started is True
        assert "researcher" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_live_config_default_outranks_the_boot_snapshot(self, monkeypatch):
        """``agent.default_agent`` is adopted live, so a boot-only read goes stale.

        The field has no ``restart=True`` mark, while ``self._cfg`` is rebuilt only by
        unrelated MCP handlers.
        """
        import copy

        from kiro_crew.config import live

        gw = _make_gw_for_llm()
        gw._cfg.agent.default_agent = "stale-boot-default"
        live_cfg = copy.copy(gw._cfg)
        live_cfg.agent = copy.copy(gw._cfg.agent)
        live_cfg.agent.default_agent = "live-default"
        monkeypatch.setattr(live, "snapshot", lambda: live_cfg)
        job = _make_llm_job(agent_id="")

        await _run_llm_callback(gw, job)

        assert self._dispatched_agent(gw) == "live-default"

    @pytest.mark.asyncio
    async def test_a_default_naming_a_crew_alias_does_not_inherit_that_crew(self, monkeypatch):
        """The configured default is a TEMPLATE name, so the alias collapse must not claim it.

        ``_resolve_cron_agent`` collapses any name keying ``cfg.agents`` into a crew alias,
        binding that crew's workspace, model and MCP servers. ``agent.default_agent`` has
        no alias validation, so collapsing would silently widen the cron's capabilities.
        """
        import copy

        from kiro_crew.config import live, loader

        gw = _make_gw_for_llm()
        live_cfg = copy.copy(gw._cfg)
        live_cfg.agent = copy.copy(gw._cfg.agent)
        live_cfg.agent.default_agent = "shared-name"
        # Real containers, so nothing short-circuits the collapse before the
        # template-namespace gate: a MagicMock ``agents`` answers ``in`` with
        # False and would make this test pass with the gate removed.
        crew_entry = MagicMock()
        crew_entry.workspace = "crew-ws"
        live_cfg.agents = {"shared-name": crew_entry}
        live_cfg.workspaces = {"crew-ws": MagicMock()}
        monkeypatch.setattr(live, "snapshot", lambda: live_cfg)
        crew_bindings = MagicMock()
        crew_bindings.kiro_agent = "kirocrew"
        monkeypatch.setattr(loader, "resolve_agent_bindings", lambda *a, **k: crew_bindings)
        monkeypatch.setattr(loader, "workspace_dir_from_entry", lambda *a, **k: "/tmp/crew-ws")
        job = _make_llm_job(agent_id="")

        await _run_llm_callback(gw, job)

        kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        # Dispatched as the template it was configured as, not as the crew's
        # collapsed ``kiro_agent`` ...
        assert kwargs["agent"] == "shared-name"
        # ... and the EXPLICIT no-crew spelling, not merely a falsy one.
        # ``resolve_crew_identity`` returns an explicit ``crew_agent`` verbatim
        # including ``""``, but treats ``None`` as "apply the crew-namespace
        # fallback" and reselects any ``agent`` that keys ``config.agents`` -- so
        # asserting only falsiness would pass on a ``None`` that still inherits
        # the colliding crew's identity, workspace and capability MCP servers.
        assert kwargs["crew_agent"] == ""
        # ... with no crew workspace bound off the collision.
        assert kwargs["cwd"] is None


class TestFailedOneShotIsParkedNotDeleted:
    """A one-shot whose run FAILED (error or timeout) is kept, parked disabled.

    Only a run that ended "ok" consumes a ``delete_after_run`` at-job. Each
    callback double below implements the real contract of its path: raise
    (LLM error), sleep past the deadline (timeout), or mark the job and return
    normally (fire-time deny, never-started).
    """

    @staticmethod
    def _svc(tmp_path, on_job):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=on_job)
        svc._load()
        job = svc.add_job(name="one-shot", message="go", at_ts=9999999999.0)
        job.delete_after_run = True
        svc._save()
        return svc, job

    @staticmethod
    def _on_disk(tmp_path, job_id):
        from kiro_crew.cron import CronService

        fresh = CronService(base_dir=tmp_path)
        fresh._load()
        return next((j for j in fresh.list_jobs(include_disabled=True) if j.id == job_id), None)

    async def _run(self, svc, job):
        from kiro_crew.cron_service.execution import close_run

        await svc._execute_with_timeout(job)
        # The production shape: merge the run's frozen COPY against a store
        # the merge must reload, so the live job's fields cannot leak through.
        terminal, _status, _result = close_run(
            job, started_at=0.0, generation=1, being_cancelled=False
        )
        svc._reset_fingerprint()
        await asyncio.to_thread(svc._merge_job_result, terminal)

    @pytest.mark.asyncio
    async def test_errored_one_shot_is_kept_and_disabled(self, tmp_path):
        async def _boom(j):
            raise RuntimeError("agent failed")

        svc, job = self._svc(tmp_path, _boom)
        await self._run(svc, job)
        stored = self._on_disk(tmp_path, job.id)
        assert stored is not None, "a one-shot whose run errored was deleted"
        assert stored.enabled is False and stored.auto_paused is True
        assert stored.user_paused is False, "a failure park must not read as a user pause"
        assert stored.last_status == "error"
        assert stored.last_error == "agent failed"

    @pytest.mark.asyncio
    async def test_timed_out_one_shot_is_kept_and_disabled(self, tmp_path, monkeypatch):
        import kiro_crew.cron as cron_mod

        monkeypatch.setattr(cron_mod, "effective_wake_budget", lambda j: 0.05)
        for name in ("_pool_queue_allowance", "_gate_budget_allowance", "_vet_allowance"):
            monkeypatch.setattr(cron_mod, name, lambda j: 0)

        async def _hang(j):
            await asyncio.Event().wait()  # never set: only the deadline ends it

        svc, job = self._svc(tmp_path, _hang)
        await self._run(svc, job)
        stored = self._on_disk(tmp_path, job.id)
        assert stored is not None, "a one-shot whose run timed out was deleted"
        assert stored.enabled is False and stored.auto_paused is True
        assert stored.user_paused is False, "a failure park must not read as a user pause"
        assert (stored.last_error or "").startswith("Timed out after")

    @pytest.mark.asyncio
    async def test_successful_one_shot_is_still_deleted(self, tmp_path):
        async def _ok(j):
            return "ok"

        svc, job = self._svc(tmp_path, _ok)
        await self._run(svc, job)
        assert job.last_status == "ok"
        assert self._on_disk(tmp_path, job.id) is None, "a successful one-shot was not consumed"

    @pytest.mark.asyncio
    async def test_denied_one_shot_is_still_parked_disabled(self, tmp_path):
        async def _deny(j):
            j.last_status = "error"
            j.fire_time_denied = True

        svc, job = self._svc(tmp_path, _deny)
        await self._run(svc, job)
        stored = self._on_disk(tmp_path, job.id)
        assert stored is not None, "a denied one-shot was deleted"
        assert stored.enabled is False
        assert stored.auto_paused is False, "a policy denial is neutral and must not auto-pause"

    @pytest.mark.asyncio
    async def test_never_started_one_shot_stays_enabled(self, tmp_path):
        async def _starved(j):
            j.last_status = "error"
            j.run_never_started = True

        svc, job = self._svc(tmp_path, _starved)
        await self._run(svc, job)
        stored = self._on_disk(tmp_path, job.id)
        assert stored is not None, "a never-started one-shot was deleted"
        assert stored.enabled is True, "a never-started one-shot was parked; it must retry"

    @pytest.mark.asyncio
    async def test_stale_failed_record_does_not_park_a_newer_run(self, tmp_path):
        from kiro_crew.cron_service.execution import close_run

        async def _boom(j):
            raise RuntimeError("old run failed")

        svc, job = self._svc(tmp_path, _boom)
        await svc._execute_with_timeout(job)
        terminal, _status, _result = close_run(
            job, started_at=0.0, generation=1, being_cancelled=False
        )
        # A newer run's record already landed: the store holds generation 2.
        stored = next(j for j in svc._jobs if j.id == job.id)
        stored.run_generation = 2
        # The operator re-enabled it, which clears the pause the old run set.
        stored.enabled, stored.user_paused, stored.auto_paused = True, False, False
        svc._save()
        svc._reset_fingerprint()
        await asyncio.to_thread(svc._merge_job_result, terminal)
        on_disk = self._on_disk(tmp_path, job.id)
        assert on_disk is not None
        assert on_disk.enabled is True, "an older failed record parked a newer run"

    @pytest.mark.asyncio
    async def test_save_fault_on_a_failed_one_shot_queues_no_consume(self, tmp_path):
        from kiro_crew.cron_service.execution import close_run
        from kiro_crew.cron_service.store import CronStoreUnreadable

        async def _boom(j):
            raise RuntimeError("agent failed")

        svc, job = self._svc(tmp_path, _boom)
        await svc._execute_with_timeout(job)
        terminal, _status, _result = close_run(
            job, started_at=0.0, generation=1, being_cancelled=False
        )

        def _unreadable():
            # The real contract: the store cannot be read, so nothing is written.
            raise CronStoreUnreadable("store unreadable")

        svc._save = _unreadable
        await asyncio.to_thread(svc._merge_job_result, terminal)
        # A failed one-shot owes a park, not a delete. The park rides in the run
        # record the finalizer holds, so the save fault must not queue a consume.
        assert job.id not in svc._pending_removals

    @pytest.mark.asyncio
    async def test_cancel_during_failure_alert_still_parks_the_one_shot(self, tmp_path):
        from kiro_crew.cron_service.execution import close_run

        async def _fail_then_cancelled_in_alert(j):
            # The command/script contract: mark the run failed on the shared job,
            # then await the failure alert -- which a shutdown cancels.
            j.last_status = "error"
            j.last_error = "exit 1"
            raise asyncio.CancelledError

        svc, job = self._svc(tmp_path, _fail_then_cancelled_in_alert)
        with pytest.raises(asyncio.CancelledError):
            await svc._execute_with_timeout(job)
        terminal, _status, _result = close_run(
            job, started_at=0.0, generation=1, being_cancelled=True
        )
        svc._reset_fingerprint()
        await asyncio.to_thread(svc._merge_job_result, terminal)
        stored = self._on_disk(tmp_path, job.id)
        assert stored is not None, "a cancelled failed one-shot was deleted"
        assert (
            stored.enabled is False and stored.auto_paused is True
        ), "a cancel during the failure alert left the failed one-shot enabled"
