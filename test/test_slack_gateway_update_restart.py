"""The in-flight-work census a user-clicked restart reads.

Covers ``_count_in_flight_work`` and the live update handler tasks the callback
drain waits for. There is no update restart any more: only a person's restart cuts
the process (the decision entry "updates never restart a running gateway").
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

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
        # Each stand-in parks on an event nothing sets, so it stays pending until
        # the finally cancels it. No wall-clock sleep decides when it ends.
        gate = asyncio.Event()
        normal = asyncio.create_task(gate.wait(), name="dashboard-turn")
        remote = asyncio.create_task(gate.wait(), name="dashboard-relay")
        stage = asyncio.create_task(gate.wait(), name="dashboard-stage:slot-a")
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
            # A cancelled stand-in finishes on its next turn. The ceiling only
            # names a hang; it does not decide the outcome.
            _done, pending = await asyncio.wait({normal, remote, stage}, timeout=30)
            assert not pending, "cancelled stand-in tasks did not finish"
