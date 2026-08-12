"""Coverage for the Kiro Crew gateway's deterministic cron-execution paths.

``test_slack_gateway.py`` and ``test_cron_gateway_integration.py`` drive the
LLM (``message``) arm of ``_cron_callback`` thoroughly, but the two
deterministic arms — ``job.command`` (shell) and ``job.script`` (Python
callable) — only had their happy paths exercised. Everything reached here was
uncovered by the whole suite before this file:

* the shared concurrent-execution guard (``_running_script_ids``);
* command mode: fire-time governance denial, ``cancelled`` status, the
  empty-output ok/non-ok split, non-ok-with-output, timeout and generic-error
  arms, plus the best-effort SEL-audit ``except`` around each;
* script mode: governance denial, ``cancelled`` / ``ok`` / ``skip`` / ``done``
  / ``report`` / unknown-status dispositions, the auto-paused warning arms of
  timeout and generic error, and their SEL-audit ``except`` twins;
* the ``message``-arm fire-time denial audit ``except``;
* ``_deliver_script_result``'s queued-slot, rehydrate-miss, no-session-key,
  delivery-failure and ``CronStoreBusy`` deferred-removal branches;
* the "cron reaper not started" arm of ``_init_cron``;
* ``_channel_reply_link`` / ``_deliver_channel_reply`` resolution refusals;
* ``_persist_turn_row``'s best-effort ``except`` and ``_is_read_only_tool``'s
  no-token guard.

Everything is driven through mocked collaborators: the sandbox runners, the
governance gate, SEL and the cron service are all patched, so no subprocess, no
socket and no write outside the per-test ``KIROCREW_HOME`` (pinned by
``test/conftest.py``) happens. Style and patch seams mirror
``test_slack_gateway.py`` / ``test_cron_gateway_integration.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.cron import CronJob, CronStoreBusy
from kiro_crew.slack import gateway as gw

# ─── Helpers ─────────────────────────────────────────────────────────────


def _make_orchestrator(**kwargs: Any) -> Any:
    """Build a GatewayOrchestrator with mocked credentials (no Slack tokens).

    Returned as ``Any`` on purpose: every test below swaps real collaborators
    for mocks, which do not satisfy the declared attribute types.
    """
    cfg = KiroCrewConfig()
    creds = {"KIROCREW_OWNER_ID": "U_OWNER"}
    with patch.object(cfg, "load_credentials", return_value=creds):
        return gw.GatewayOrchestrator(
            cfg,
            no_dashboard=kwargs.pop("no_dashboard", True),
            no_crons=kwargs.pop("no_crons", True),
            no_open=True,
        )


def _mock_dashboard_state() -> MagicMock:
    ds = MagicMock()
    ds._slots = {}
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.get_slot = MagicMock(return_value=None)
    ds.has_slot = MagicMock(return_value=False)
    ds.conversation_log = None
    return ds


def _mock_slot(*, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.running = running
    slot.queue_append = MagicMock(return_value="q-1")
    slot.append = MagicMock()
    slot.task = None
    return slot


def _job(**kwargs: Any) -> CronJob:
    """A real CronJob, so record_success / record_failure semantics are real."""
    job = CronJob(
        id=kwargs.pop("id", "j1"),
        name=kwargs.pop("name", "nightly probe"),
        message=kwargs.pop("message", "go"),
    )
    for key, value in kwargs.items():
        setattr(job, key, value)
    return job


def _cron_service_double() -> MagicMock:
    svc = MagicMock()
    svc.start = AsyncMock()
    svc.start_reaper = MagicMock()
    svc.remove_job_async = AsyncMock(return_value=True)
    svc.defer_removal = MagicMock()
    svc.set_refresh_callback = MagicMock()
    svc.register_active_session_key = MagicMock()
    svc.clear_active_session_key = MagicMock()
    svc.get_job = MagicMock(return_value=None)
    return svc


def _blind_sel() -> MagicMock:
    """A SEL double whose audit write always fails (drives the best-effort arms)."""
    sel_obj = MagicMock()
    sel_obj.log_tool_invocation = MagicMock(side_effect=RuntimeError("sel down"))
    return sel_obj


def _sandboxed(result: Any) -> Any:
    """A sandbox-runner double: returns *result*, or raises it when it is an error.

    The real runners execute inside ``run_in_executor``, so raising here
    propagates through ``asyncio.wait_for`` exactly as a real failure does —
    which is what lets the timeout arm be driven without a real wall-clock wait.
    """

    def _run(*_args: Any, **_kwargs: Any) -> Any:
        if isinstance(result, BaseException):
            raise result
        return result

    return _run


@asynccontextmanager
async def _cron_cb(
    orch: Any,
    *,
    svc: MagicMock | None = None,
    gate_reason: str = "",
    sel_obj: MagicMock | None = None,
    command_result: Any = None,
    script_result: Any = None,
) -> AsyncIterator[Any]:
    """Run ``_init_cron`` with every collaborator patched and yield ``on_job``.

    The callback resolves ``sel`` / ``vet_job_at_fire_time`` / the sandbox
    runners from module globals at CALL time, so it must be invoked while these
    patches are still active — hence a context manager rather than a plain
    factory.
    """
    service = svc if svc is not None else _cron_service_double()
    captured: dict[str, Any] = {}

    async def _create(**kw: Any) -> MagicMock:
        captured["on_job"] = kw["on_job"]
        return service

    _sel = sel_obj if sel_obj is not None else MagicMock()
    with (
        patch.object(gw.CronService, "create", AsyncMock(side_effect=_create)),
        patch.object(gw, "cron_executor", lambda: None),
        patch.object(gw, "vet_job_at_fire_time", lambda job: gate_reason),
        patch.object(gw, "sel", lambda: _sel),
        patch.object(gw, "run_command_sandboxed", _sandboxed(command_result)),
        patch.object(gw, "run_script_sandboxed", _sandboxed(script_result)),
        patch.object(
            gw, "build_cron_session_context", lambda job: (f"cron:{job.id}", job.message)
        ),
        patch("kiro_crew.apps.bridges.reconcile_app_crons_for_execution", AsyncMock()),
    ):
        await orch._init_cron()
        assert "on_job" in captured
        yield captured["on_job"]


def _channel_sessions(
    *,
    origin: Any = None,
    mirror: Any = None,
    stored: Any = "",
    origin_raises: bool = False,
    stored_raises: bool = False,
) -> MagicMock:
    """A SessionManager double exposing only the link-resolution surface."""
    sessions = MagicMock()
    if origin_raises:
        sessions.get_origin_link = MagicMock(side_effect=RuntimeError("map wedged"))
    else:
        sessions.get_origin_link = MagicMock(return_value=origin)
    sessions.get_mirror_link = MagicMock(return_value=mirror)
    if stored_raises:
        sessions.get_channel = MagicMock(side_effect=RuntimeError("map wedged"))
    else:
        sessions.get_channel = MagicMock(return_value=stored)
    return sessions


# ═══════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestModuleHelpers:
    """``_persist_turn_row`` and ``_is_read_only_tool`` guard arms."""

    @pytest.mark.asyncio
    async def test_persist_turn_row_swallows_failure(self):
        """A usage-row persistence failure never propagates into the caller."""
        with patch.object(gw, "read_context_tokens", side_effect=RuntimeError("no provider")):
            await gw._persist_turn_row(
                object(),
                "_hb",
                provider="anthropic",
                surface="heartbeat",
                agent_fallback=lambda: "kirocrew",
                t0=0.0,
            )

    def test_read_only_tool_rejects_punctuation_only_title(self):
        """A title that tokenizes to nothing fails closed rather than approving."""
        assert gw._is_read_only_tool("___") is False
        assert gw._is_read_only_tool("read") is True


# ═══════════════════════════════════════════════════════════════════════════
# Command-mode cron execution
# ═══════════════════════════════════════════════════════════════════════════


class TestCronCommandMode:
    """``_cron_callback``'s ``job.command`` arm."""

    @pytest.mark.asyncio
    async def test_concurrent_run_is_skipped(self):
        """A job still running is skipped rather than started twice."""
        orch = _make_orchestrator()
        job = _job(command="echo hi")
        orch._running_script_ids.add(job.id)
        async with _cron_cb(orch, command_result={"status": "ok", "output": "hi"}) as cb:
            assert await cb(job) is None
        # The guard must not consume the marker — the in-flight run owns it.
        assert job.id in orch._running_script_ids

    @pytest.mark.asyncio
    async def test_fire_time_denial_keeps_job_and_audits(self):
        """Governance denial marks the run failed without counting a failure."""
        orch = _make_orchestrator()
        job = _job(command="printf hi")
        async with _cron_cb(
            orch, gate_reason="capabilities.cron denied", sel_obj=_blind_sel()
        ) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert job.fire_time_denied is True
        # A policy denial must never feed the auto-pause counter.
        assert job.consecutive_failures == 0
        assert job.id not in orch._running_script_ids

    @pytest.mark.asyncio
    async def test_cancelled_status_is_not_a_failure(self):
        """A user cancel leaves the bookkeeping to CronService.cancel()."""
        orch = _make_orchestrator()
        job = _job(command="sleep 1")
        async with _cron_cb(orch, command_result={"status": "cancelled"}) as cb:
            assert await cb(job) is None
        assert job.consecutive_failures == 0
        assert job.last_status is None

    @pytest.mark.asyncio
    async def test_empty_output_ok_records_success(self):
        """No output on an ok status is a success with nothing to deliver."""
        orch = _make_orchestrator()
        job = _job(command="true", consecutive_failures=2)
        async with _cron_cb(orch, command_result={"status": "ok", "output": "   "}) as cb:
            assert await cb(job) is None
        assert job.last_status == "ok"
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_empty_output_non_ok_records_failure(self):
        """No output on a non-ok status is a failure, and it says why."""
        orch = _make_orchestrator()
        job = _job(command="false")
        async with _cron_cb(orch, command_result={"status": "error", "output": ""}) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert "non-ok status with no output" in (job.last_error or "")
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_non_ok_with_output_delivers_and_counts_failure(self):
        """Output is still delivered on a non-ok exit, but the run is a failure."""
        orch = _make_orchestrator()
        job = _job(command="false")
        result = {"status": "error", "output": "partial output", "exit_code": 3}
        async with _cron_cb(orch, sel_obj=_blind_sel(), command_result=result) as cb:
            assert await cb(job) == "partial output"
        assert job.last_status == "error"
        assert "exit_code=3" in (job.last_error or "")
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_timeout_records_failure(self):
        """A sandbox timeout is recorded against the job with its own audit arm."""
        orch = _make_orchestrator()
        job = _job(command="sleep 9999", timeout=7)
        async with _cron_cb(
            orch, sel_obj=_blind_sel(), command_result=asyncio.TimeoutError()
        ) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert job.last_error == "timeout (12s)"
        assert job.consecutive_failures == 1
        assert job.id not in orch._running_script_ids

    @pytest.mark.asyncio
    async def test_unexpected_error_records_failure(self):
        """An unexpected runner error is redacted, truncated and counted."""
        orch = _make_orchestrator()
        job = _job(command="printf hi")
        async with _cron_cb(
            orch, sel_obj=_blind_sel(), command_result=RuntimeError("x" * 400)
        ) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert len(job.last_error or "") <= 200
        assert job.consecutive_failures == 1


# ═══════════════════════════════════════════════════════════════════════════
# Script-mode cron execution
# ═══════════════════════════════════════════════════════════════════════════


class TestCronScriptMode:
    """``_cron_callback``'s ``job.script`` arm and its dispositions."""

    @pytest.mark.asyncio
    async def test_fire_time_denial_keeps_job(self):
        orch = _make_orchestrator()
        job = _job(script="probes.py:check")
        async with _cron_cb(
            orch, gate_reason="script body rescan denied", sel_obj=_blind_sel()
        ) as cb:
            assert await cb(job) is None
        assert job.fire_time_denied is True
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_cancelled_status_is_not_a_failure(self):
        orch = _make_orchestrator()
        job = _job(script="probes.py:check")
        async with _cron_cb(orch, script_result={"status": "cancelled"}) as cb:
            assert await cb(job) is None
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_ok_status_records_success(self):
        orch = _make_orchestrator()
        job = _job(script="probes.py:check", consecutive_failures=1)
        async with _cron_cb(orch, sel_obj=_blind_sel(), script_result={"status": "ok"}) as cb:
            assert await cb(job) == "ok"
        assert job.last_status == "ok"
        assert job.last_result == "ok"
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_skip_status_delivers_nothing(self):
        """A Skip is a deliberate no-op: no result, no status change."""
        orch = _make_orchestrator()
        job = _job(script="probes.py:check")
        async with _cron_cb(orch, sel_obj=_blind_sel(), script_result={"status": "skip"}) as cb:
            assert await cb(job) is None
        assert job.last_result is None

    @pytest.mark.asyncio
    async def test_unknown_status_is_an_error(self):
        """An unrecognized status raises and lands in the generic error arm."""
        orch = _make_orchestrator()
        job = _job(script="probes.py:check")
        result = {"status": "weird", "error": "bad disposition"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert job.last_error == "bad disposition"
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_timeout_on_already_paused_job_warns(self):
        """An auto-paused job's timeout logs the pause without re-auditing it."""
        orch = _make_orchestrator()
        job = _job(
            script="probes.py:check", timeout=3, auto_paused=True, consecutive_failures=5
        )
        async with _cron_cb(
            orch, sel_obj=_blind_sel(), script_result=asyncio.TimeoutError()
        ) as cb:
            assert await cb(job) is None
        assert job.last_error == "timeout (8s)"
        assert job.auto_paused is True
        assert job.id not in orch._running_script_ids

    @pytest.mark.asyncio
    async def test_error_on_already_paused_job_warns(self):
        orch = _make_orchestrator()
        job = _job(script="probes.py:check", auto_paused=True, consecutive_failures=5)
        async with _cron_cb(
            orch, sel_obj=_blind_sel(), script_result=RuntimeError("callable exploded")
        ) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert "callable exploded" in (job.last_error or "")


# ═══════════════════════════════════════════════════════════════════════════
# _deliver_script_result
# ═══════════════════════════════════════════════════════════════════════════


class TestDeliverScriptResult:
    """Delivery of a Report / Done message back into the originating session."""

    @pytest.mark.asyncio
    async def test_report_queues_into_a_running_slot(self):
        """A busy slot gets the notification queued, not dispatched."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        slot = _mock_slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2")
        result = {"status": "report", "message": "still red"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) == "still red"
        slot.queue_append.assert_called_once()
        assert slot.append.call_args[0][0] == "queued"
        orch.dashboard_state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_report_injects_into_an_idle_slot(self):
        """An idle slot takes the notification as a real turn."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        slot = _mock_slot(running=False)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2")
        turn = MagicMock()
        result = {"status": "report", "message": "green again"}
        spawn = MagicMock(return_value=turn)
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch.object(gw, "_run_chat", MagicMock(return_value=MagicMock())),
        ):
            async with _cron_cb(orch, script_result=result) as cb:
                assert await cb(job) == "green again"
        spawn.assert_called_once()
        assert slot.append.call_args[0][0] == "inject"
        assert slot.task is turn

    @pytest.mark.asyncio
    async def test_report_falls_back_to_notification_when_no_slot(self):
        """No live and no rehydratable slot degrades to a bell notification."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(script="probes.py:check", session_key="dashboard:chat-gone")
        result = {"status": "report", "message": "orphaned"}
        with patch.object(gw, "_rehydrate_slot_from_history", MagicMock(return_value=None)):
            async with _cron_cb(orch, script_result=result) as cb:
                assert await cb(job) == "orphaned"
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args[0][2] == "orphaned"

    @pytest.mark.asyncio
    async def test_report_without_session_key_notifies(self):
        """A job with no originating session still reaches the bell feed."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(script="probes.py:check", session_key="")
        result = {"status": "report", "message": "no session"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) == "no session"
        orch.dashboard_state.notify.assert_called_once()
        orch.dashboard_state.get_slot.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_failure_does_not_break_the_run(self):
        """A delivery exception is logged; the run's own verdict still stands."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state.get_slot = MagicMock(side_effect=RuntimeError("slots wedged"))
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2")
        result = {"status": "report", "message": "delivered nowhere"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) == "delivered nowhere"
        assert job.last_status == "ok"

    @pytest.mark.asyncio
    async def test_done_removes_the_job(self):
        """A Done disposition delivers and then removes the one-shot job."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        svc = _cron_service_double()
        job = _job(script="probes.py:check", session_key="")
        result = {"status": "done", "message": "all clear"}
        async with _cron_cb(orch, svc=svc, sel_obj=_blind_sel(), script_result=result) as cb:
            assert await cb(job) == "all clear"
        svc.remove_job_async.assert_awaited_once_with(job.id)

    @pytest.mark.asyncio
    async def test_done_defers_removal_when_store_is_busy(self):
        """A busy store hands the removal to the deferred queue, not a retry."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        svc = _cron_service_double()
        svc.remove_job_async = AsyncMock(side_effect=CronStoreBusy("locked"))
        job = _job(script="probes.py:check", session_key="")
        result = {"status": "done", "message": "finished"}
        async with _cron_cb(orch, svc=svc, script_result=result) as cb:
            assert await cb(job) == "finished"
        svc.defer_removal.assert_called_once_with(job.id)

    @pytest.mark.asyncio
    async def test_silent_job_delivers_nothing(self):
        """A silent job's Report reaches neither a slot nor the bell."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2", silent=True)
        result = {"status": "report", "message": "quiet"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) == "quiet"
        orch.dashboard_state.notify.assert_not_called()
        orch.dashboard_state.get_slot.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# message-mode denial + _init_cron wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestCronMessageDenialAndWiring:
    @pytest.mark.asyncio
    async def test_message_job_fire_time_denial(self):
        """An LLM cron denied at fire time never dispatches a turn."""
        orch = _make_orchestrator()
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        job = _job()
        async with _cron_cb(
            orch, gate_reason="capabilities.cron off", sel_obj=_blind_sel()
        ) as cb:
            assert await cb(job) is None
        assert job.fire_time_denied is True
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_reaper_not_started_without_sessions(self):
        """Arming the scheduler without a session manager skips the reaper."""
        orch = _make_orchestrator(no_crons=False)
        orch.sessions = None
        svc = _cron_service_double()
        async with _cron_cb(orch, svc=svc):
            pass
        svc.start.assert_awaited_once()
        svc.start_reaper.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Non-Slack channel reply resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestChannelReplyLink:
    """``_channel_reply_link``'s resolution ladder and its refusals."""

    def test_slack_and_local_keys_return_none(self):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions()
        assert orch._channel_reply_link("slack:C1") is None
        assert orch._channel_reply_link("dashboard:chat-1-2") is None

    def test_no_session_manager_returns_none(self):
        orch = _make_orchestrator()
        orch.sessions = None
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    def test_link_getter_failure_falls_through_to_stored_channel(self):
        """A raising link getter degrades to the next rung, it does not propagate."""
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(origin_raises=True, stored="discord:U9")
        resolved = orch._channel_reply_link("discord:kirocrew:direct:U9")
        assert resolved is not None
        link, needs_dm = resolved
        assert (link.channel_type, link.channel_id, needs_dm) == ("discord", "U9", True)

    def test_stored_channel_lookup_failure_returns_none(self):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored_raises=True)
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    def test_no_stored_channel_returns_none(self):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored="")
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    @pytest.mark.parametrize(
        "stored",
        ["nocolon", "slack:U9", ":U9", "discord:"],
        ids=["no-separator", "slack-typed", "empty-type", "empty-peer"],
    )
    def test_unusable_stored_value_returns_none(self, stored):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored=stored)
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    @pytest.mark.parametrize(
        "stored",
        ["bogus:U9", "unified:U9"],
        ids=["unregistered-namespace", "unified-namespace"],
    )
    def test_unified_bucket_validates_stored_namespace(self, stored):
        """A unified DM bucket only accepts a registered non-unified namespace."""
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored=stored)
        assert orch._channel_reply_link("unified:kirocrew:direct:U9") is None

    def test_group_session_never_takes_the_stored_rung(self):
        """A group key's stored value is the sender, so it must not become a DM."""
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored="discord:U9")
        assert orch._channel_reply_link("discord:kirocrew:group:C1") is None

    def test_origin_link_wins_and_needs_no_dm_resolution(self):
        orch = _make_orchestrator()
        origin = gw.ChannelLink("discord", channel_id="C77", thread_id="T1")
        orch.sessions = _channel_sessions(origin=origin, stored="discord:U9")
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") == (origin, False)


class TestDeliverChannelReply:
    """``_deliver_channel_reply``'s early refusals."""

    @pytest.mark.asyncio
    async def test_blank_text_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "   ") is False

    @pytest.mark.asyncio
    async def test_no_dashboard_state_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "hi") is False

    @pytest.mark.asyncio
    async def test_unresolvable_key_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _channel_sessions(stored="")
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "hi") is False

    @pytest.mark.asyncio
    async def test_target_resolution_failure_degrades_to_false(self):
        """A raising governance ladder degrades the caller, it never propagates."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        link = gw.ChannelLink("discord", channel_id="C77")

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("profile dir unreadable")

        with patch.object(gw, "_resolve_channel_target", _boom):
            delivered = await orch._deliver_channel_reply(
                "discord:kirocrew:direct:U9", "hi", resolved_link=(link, False)
            )
        assert delivered is False

    @pytest.mark.asyncio
    async def test_no_governed_target_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        link = gw.ChannelLink("discord", channel_id="C77")
        with patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)):
            delivered = await orch._deliver_channel_reply(
                "discord:kirocrew:direct:U9", "hi", resolved_link=(link, False)
            )
        assert delivered is False
