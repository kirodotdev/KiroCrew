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
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

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
    async def test_done_removes_job(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "done", "message": "CR merged"})
        assert "CR merged" in (result or "")
        assert job.last_result == "CR merged"
        gw.cron_svc.remove_job_async.assert_called_once_with("sj1")

    @pytest.mark.asyncio
    async def test_done_busy_store_defers_removal_not_dropped(self):
        """A busy store on the Done removal must hand off to defer_removal, not
        silently drop it (Arbiter BLOCK item 1). Otherwise the completed job
        lingers enabled and re-fires."""
        from kiro_crew.cron import CronStoreBusy

        gw = _make_gw()
        job = _make_script_job()
        captured_cb = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls, \
             patch("kiro_crew.slack.gateway.run_script_sandboxed",
                   return_value={"status": "done", "message": "CR merged"}), \
             patch("kiro_crew.slack.gateway.resolve_script_path"), \
             patch("kiro_crew.slack.gateway.sel"):

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

        gw.cron_svc.remove_job_async.assert_called_once_with("sj1")
        # The removal was queued for deferred drain, not dropped.
        gw.cron_svc.defer_removal.assert_called_once_with("sj1")

    @pytest.mark.asyncio
    async def test_report_does_not_remove_job(self):
        gw = _make_gw()
        job = _make_script_job(session_key="dashboard:chat-1")
        result, _ = await _run_script_callback(gw, job, {"status": "report", "message": "DRB passed"})
        assert "DRB passed" in (result or "")
        assert job.last_result == "DRB passed"
        gw.cron_svc.remove_job_async.assert_not_called()

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

    @pytest.mark.asyncio
    async def test_fire_time_command_deny_blocks_execution(self):
        # A policy tightened after this job was scheduled must still block it at
        # fire time, not just at cron_add authoring time.
        gw = _make_gw()
        job = _make_command_job()
        with patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None), \
             patch(
                 "kiro_crew.mcp_cron._vet_command_governance",
                 return_value="Error: cron command blocked by governance policy: denied",
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
        with patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None), \
             patch("kiro_crew.mcp_cron._vet_command_governance", return_value=None):
            result, mock_run = await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )
        assert job.last_status == "ok"
        mock_run.assert_called_once()


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
            await _run_command_callback(gw, job, {"status": "error", "output": f"err {i}", "exit_code": 1})
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


async def _run_llm_callback(gw, job, *, get_or_create_side_effect=None):
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

    with patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls, \
         patch("kiro_crew.slack.gateway.run_in_embed_pool", _embed_mock), \
         patch("kiro_crew.slack.gateway.stream_and_collect", _stream_mock), \
         patch("kiro_crew.slack.gateway.sel"), \
         patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx:

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
        result = await captured_cb(job)
        return result, _stream_mock


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
    """Issue #647: every model-spending cron turn appends exactly one usage row
    tagged surface='cron'; the zero-token script/command modes append none."""

    @pytest.mark.asyncio
    async def test_llm_cron_persists_usage_row_with_surface(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        persist = AsyncMock()
        # Patch gateway's own bindings: the imports are at module scope there,
        # so patching the source module would not be seen by the call site.
        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", persist
        ), patch(
            "kiro_crew.slack.gateway.read_context_tokens",
            MagicMock(return_value=(1234, 200000)),
            create=True,
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
        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", persist
        ), patch(
            "kiro_crew.slack.gateway.read_context_tokens",
            MagicMock(return_value=(10, 100)),
            create=True,
        ), patch(
            "kiro_crew.slack.gateway.read_effective_agent",
            MagicMock(return_value="kirocrew"),
            create=True,
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
        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", persist
        ), patch(
            "kiro_crew.slack.gateway.read_context_tokens",
            MagicMock(return_value=(10, 100)),
            create=True,
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
        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", persist
        ):
            await _run_script_callback(gw, job, {"status": "ok"})

        persist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_command_cron_writes_no_usage_row(self):
        gw = _make_gw()
        job = _make_command_job()

        persist = AsyncMock()
        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", persist
        ):
            await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )

        persist.assert_not_awaited()
