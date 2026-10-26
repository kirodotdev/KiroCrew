"""The restart after an applied update.

Covers ``_count_in_flight_work``, the stale-asset drain that decides when the
restart may run, and the callback-safe restart: the session fence, the deferral
and its deadline, the bundle gap the restart owns, and a stop signalled while it
runs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
)

from kiro_crew.slack import gateway as gw


def _provider(active: bool):
    """A provider mock whose has_active_turn() returns *active*."""
    p = MagicMock()
    p.has_active_turn = MagicMock(return_value=active)
    return p


class TestCountInFlightWork:
    """Cover GatewayOrchestrator._count_in_flight_work (stale-asset drain)."""

    def test_zero_when_idle(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch._session_tasks = {}
        assert orch._count_in_flight_work() == 0

    def test_counts_active_provider_turns_only(self):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.return_value = [
            _provider(True),
            _provider(False),
            _provider(True),
        ]
        orch.dashboard_state = state
        orch._session_tasks = {}
        assert orch._count_in_flight_work() == 2

    def test_missing_accessor_and_predicate_errors_fail_closed(self):
        orch = _make_orchestrator()
        no_attr = MagicMock(spec=[])  # no has_active_turn attribute
        raising = MagicMock()
        raising.has_active_turn = MagicMock(side_effect=RuntimeError("boom"))
        state = MagicMock()
        state.sessions.active_providers.return_value = [no_attr, raising, _provider(True)]
        orch.dashboard_state = state
        orch._session_tasks = {}
        # Unknown provider state is unsafe for an automatic restart.
        assert orch._count_in_flight_work() == 3

    def test_counts_undone_session_tasks(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        undone1, undone2, done = MagicMock(), MagicMock(), MagicMock()
        undone1.done.return_value = False
        undone2.done.return_value = False
        done.done.return_value = True
        orch._session_tasks = {"a": undone1, "b": done, "c": undone2}
        assert orch._count_in_flight_work() == 2

    def test_active_providers_failure_fails_closed(self):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.side_effect = RuntimeError("nope")
        orch.dashboard_state = state
        orch._session_tasks = {}
        assert orch._count_in_flight_work() == 1

    def test_provider_turns_and_session_tasks_sum(self):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.return_value = [_provider(True)]
        state.workflow_service = None
        orch.dashboard_state = state
        undone = MagicMock()
        undone.done.return_value = False
        orch._session_tasks = {"x": undone}
        assert orch._count_in_flight_work() == 2

    def test_counts_background_work_that_restart_would_interrupt(self):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.return_value = []
        state.workflow_service.list_runs.return_value = [
            {"status": "running"},
            {"status": "finished"},
            {"status": "running"},
        ]
        orch.dashboard_state = state
        orch._session_tasks = {}
        orch.subagent_mgr = SimpleNamespace(pending_work_count=5)
        orch._running_script_ids = {"script-a", "script-b"}
        orch.task_runner = SimpleNamespace(running=True)

        assert orch._in_flight_work_counts() == (0, 10)
        assert orch._count_in_flight_work() == 10

    def test_counts_callback_reservations_and_channel_handler_tasks(self, monkeypatch):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.return_value = []
        state.workflow_service = None
        orch.dashboard_state = state
        orch._session_tasks = {}
        orch.sessions = SimpleNamespace(inbound_callback_count=2)
        monkeypatch.setattr(gw.inbound_spool, "pending_refusal_write_count", lambda: 3)
        direct = MagicMock()
        direct.done.return_value = False
        nested = MagicMock()
        nested.done.return_value = False
        finished = MagicMock()
        finished.done.return_value = True
        orch._handler_tasks = {direct}
        orch._channel_handles = {
            "teams": SimpleNamespace(_handler_tasks={direct, finished}),
            "imessage": SimpleNamespace(_peer=SimpleNamespace(_handler_tasks={nested})),
        }
        orch.subagent_mgr = None
        orch._running_script_ids = set()
        orch.task_runner = None

        assert orch._in_flight_work_counts() == (0, 7)

    def test_counts_dashboard_tasks_before_provider_acquisition(self):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.return_value = []
        state.workflow_service = None
        running = MagicMock()
        running.done.return_value = False
        remote = MagicMock()
        remote.done.return_value = False
        finished = MagicMock()
        finished.done.return_value = True
        state._slots = {
            "normal": SimpleNamespace(task=running),
            "remote": SimpleNamespace(task=remote),
            "finished": SimpleNamespace(task=finished),
        }
        orch.dashboard_state = state
        orch._session_tasks = {}
        orch.sessions = None
        orch.subagent_mgr = None
        orch._running_script_ids = set()
        orch.task_runner = None

        assert orch._in_flight_work_counts() == (2, 0)

    @pytest.mark.asyncio
    async def test_final_drain_collects_dashboard_and_stage_tasks(self):
        orch = _make_orchestrator()
        # flake-ok: pre-existing parked stand-in tasks the finally cancels, moved verbatim; tracked for a de-flake pass
        normal = asyncio.create_task(asyncio.sleep(60), name="dashboard-turn")
        # flake-ok: pre-existing parked stand-in task the finally cancels, moved verbatim; tracked for a de-flake pass
        remote = asyncio.create_task(asyncio.sleep(60), name="dashboard-relay")
        # flake-ok: pre-existing parked stand-in task the finally cancels, moved verbatim; tracked for a de-flake pass
        stage = asyncio.create_task(asyncio.sleep(60), name="dashboard-stage:slot-a")
        orch.dashboard_state = SimpleNamespace(
            _slots={
                "normal": SimpleNamespace(task=normal),
                "remote": SimpleNamespace(task=remote),
                "stage-gap": SimpleNamespace(task=None),
            },
            _background_tasks={stage},
        )
        try:
            assert set(orch._live_update_handler_tasks()) >= {normal, remote, stage}
        finally:
            for task in (normal, remote, stage):
                task.cancel()
            # flake-ok: pre-existing unbounded gather of the cancelled stand-ins, moved verbatim; tracked for a de-flake pass
            await asyncio.gather(normal, remote, stage, return_exceptions=True)


class TestCallbackSafeUpdateRestart:
    @pytest.mark.asyncio
    async def test_pre_fence_timeout_defers_without_closing_sessions(self, monkeypatch, tmp_path):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        sessions = SimpleNamespace(
            inbound_callback_count=0,
            fence_update_restart=MagicMock(return_value=True),
            close_all=AsyncMock(),
        )
        orch.sessions = sessions
        orch._drain_update_callback_work = AsyncMock(return_value=False)
        monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda _timeout: None)
        reexec = MagicMock()
        monkeypatch.setattr(gw.platform_compat, "reexec_python_module", reexec)
        # A real file: the restart establishes that the interpreter exists before it
        # tears anything down, so a synthetic path would defer for the WRONG reason
        # and this test would pass without ever reaching the drain it is about.
        interpreter = tmp_path / "python.exe"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gw.platform_compat.chmod_safe(interpreter, 0o700)
        respawn = MagicMock(return_value=str(interpreter))

        await orch._restart_after_update(respawn)

        # The deferral under test is the one the pre-fence drain causes, so assert it
        # was actually reached; without this the early no-target deferral satisfies
        # every assertion below and the test measures nothing.
        orch._drain_update_callback_work.assert_awaited()
        assert orch._update_apply_deferred is True
        assert orch._pending_update_respawn is respawn
        sessions.fence_update_restart.assert_not_called()
        sessions.close_all.assert_not_awaited()
        reexec.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_fences_then_closes_and_final_drains(self, monkeypatch, tmp_path):
        order: list[str] = []
        orch = _make_orchestrator()
        orch.dashboard_state = None
        sessions = SimpleNamespace(inbound_callback_count=0)
        sessions.fence_update_restart = MagicMock(side_effect=lambda: order.append("fence") or True)
        sessions.close_all = AsyncMock(side_effect=lambda: order.append("close"))
        orch.sessions = sessions

        async def drain(*, timeout):
            order.append(f"drain:{timeout}")
            return True

        orch._drain_update_callback_work = AsyncMock(side_effect=drain)
        monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda _timeout: None)
        monkeypatch.setattr(
            gw.platform_compat,
            "reexec_python_module",
            lambda *_args, **_kwargs: order.append("exec"),
        )
        # A real file, for the same reason as the test above: with a synthetic path
        # the restart establishes no target and returns before the first drain, so
        # ``order`` stays empty and this ordering is never exercised.
        interpreter = tmp_path / "python.exe"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gw.platform_compat.chmod_safe(interpreter, 0o700)

        await orch._restart_after_update(lambda: str(interpreter))

        assert order == ["drain:30.0", "fence", "close", "drain:None", "exec"]
        assert orch._pending_update_respawn is None

    @pytest.mark.asyncio
    async def test_a_stop_signalled_during_the_restart_is_not_swallowed(
        self, monkeypatch, tmp_path, caplog
    ):
        # The installer finished before the stop reached ``_shutdown``. Exec'ing
        # now would bring the gateway straight back up on the new version.
        stopping = {"set": False}
        monkeypatch.setattr(gw, "shutdown_event", SimpleNamespace(is_set=lambda: stopping["set"]))
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.sessions = SimpleNamespace(
            inbound_callback_count=0,
            fence_update_restart=MagicMock(return_value=True),
            close_all=AsyncMock(),
        )

        async def drain(*, timeout):
            if timeout is None:
                stopping["set"] = True
            return True

        orch._drain_update_callback_work = AsyncMock(side_effect=drain)
        monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda _timeout: None)
        reexec = MagicMock()
        monkeypatch.setattr(gw.platform_compat, "reexec_python_module", reexec)
        interpreter = tmp_path / "python.exe"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gw.platform_compat.chmod_safe(interpreter, 0o700)

        caplog.set_level("INFO", logger=gw.logger.name)
        await orch._restart_after_update(lambda: str(interpreter))

        reexec.assert_not_called()
        msgs = [r.getMessage() for r in caplog.records if r.name == gw.logger.name]
        assert "Update applied; restart skipped because the gateway is shutting down" in msgs
        orch.sessions.close_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_restart_owns_the_bundle_gap_while_it_runs(self, monkeypatch, tmp_path):
        """Its teardown must not be raced by the stale-asset watchdog."""
        from kiro_crew import update_ownership

        seen: list[object] = []
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.sessions = SimpleNamespace(
            inbound_callback_count=0,
            fence_update_restart=MagicMock(return_value=True),
            close_all=AsyncMock(side_effect=lambda: seen.append(update_ownership.current_owner())),
        )
        orch._drain_update_callback_work = AsyncMock(return_value=True)
        monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda _timeout: None)
        monkeypatch.setattr(
            gw.platform_compat,
            "reexec_python_module",
            lambda *_a, **_k: seen.append(update_ownership.current_owner()),
        )
        interpreter = tmp_path / "python.exe"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gw.platform_compat.chmod_safe(interpreter, 0o700)

        await orch._restart_after_update(lambda: str(interpreter))

        assert seen == ["the restart into an applied update"] * 2
        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_a_deferred_restart_keeps_owning_the_gap(self, monkeypatch, tmp_path):
        """The watchdog must not force the restart the update just put off."""
        from kiro_crew import update_ownership

        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.sessions = SimpleNamespace(inbound_callback_count=0)
        orch._drain_update_callback_work = AsyncMock(return_value=False)
        monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda _timeout: None)
        interpreter = tmp_path / "python.exe"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gw.platform_compat.chmod_safe(interpreter, 0o700)

        await orch._restart_after_update(lambda: str(interpreter))

        assert orch._update_apply_deferred is True
        assert update_ownership.current_owner() == "a deferred restart into an applied update"

    @pytest.mark.asyncio
    async def test_a_retry_that_defers_again_keeps_the_first_deadline(self, monkeypatch, tmp_path):
        """The coordinator retries every few minutes; that must not hold the watchdog off for good."""
        from kiro_crew import update_ownership

        clock = {"t": 1000.0}
        monkeypatch.setattr(update_ownership, "_now", lambda: clock["t"])
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.sessions = SimpleNamespace(inbound_callback_count=0)
        orch._drain_update_callback_work = AsyncMock(return_value=False)
        monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda _timeout: None)
        interpreter = tmp_path / "python.exe"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gw.platform_compat.chmod_safe(interpreter, 0o700)

        await orch._restart_after_update(lambda: str(interpreter))
        clock["t"] += update_ownership.DEFERRED_RESTART_MAX_SECS - 1
        await orch._restart_after_update(lambda: str(interpreter))
        assert update_ownership.current_owner() == "a deferred restart into an applied update"
        clock["t"] += 1

        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_no_usable_interpreter_leaves_the_gap_to_the_supervisor(self, tmp_path):
        """The pruned tree took the bundle too; the watchdog's exit is what relaunches."""
        from kiro_crew import update_ownership

        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.sessions = SimpleNamespace(inbound_callback_count=0)

        await orch._restart_after_update(lambda: str(tmp_path / "pruned" / "python"))

        assert orch._update_apply_deferred is True
        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_a_retry_with_no_usable_interpreter_ends_an_earlier_drain_deferral(
        self, tmp_path
    ):
        """A drain deferral must not keep owning the gap once the interpreter is pruned."""
        from kiro_crew import update_ownership

        update_ownership.note_restart_deferred()  # the earlier attempt's drain deferral
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.sessions = SimpleNamespace(inbound_callback_count=0)

        await orch._restart_after_update(lambda: str(tmp_path / "pruned" / "python"))

        assert orch._update_apply_deferred is True
        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_a_committed_restart_ends_the_deferral_it_retried(self, monkeypatch, tmp_path):
        from kiro_crew import update_ownership

        update_ownership.note_restart_deferred()
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.sessions = SimpleNamespace(
            inbound_callback_count=0,
            fence_update_restart=MagicMock(return_value=True),
            close_all=AsyncMock(),
        )
        orch._drain_update_callback_work = AsyncMock(return_value=True)
        monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda _timeout: None)
        monkeypatch.setattr(gw.platform_compat, "reexec_python_module", lambda *_a, **_k: None)
        interpreter = tmp_path / "python.exe"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gw.platform_compat.chmod_safe(interpreter, 0o700)

        await orch._restart_after_update(lambda: str(interpreter))

        assert update_ownership.current_owner() is None
