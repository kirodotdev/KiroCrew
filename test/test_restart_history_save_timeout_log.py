"""A history save cut by its 5 s deadline before a restart is logged as a WARNING.

Both restarts (the dashboard's ``_restart_gateway`` and Slack's ``/kirocrew
restart``) and the gateway shutdown bound ``save_all_slots_to_history`` with
``asyncio.wait_for(..., timeout=5.0)``. An abandoned save must be visible at the
default log level; any other failure of the save stays at DEBUG.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard import chat as dashboard_chat
from kiro_crew.dashboard.handlers import updates
from kiro_crew.slack import events


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        _slots={},
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=AsyncMock()),
        _mcp_gateway_stop=AsyncMock(),
    )


async def _dashboard_restart(monkeypatch) -> None:
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(platform_compat, "execv_target_available", lambda _exe: True)
    monkeypatch.setattr(updates, "flush_breadcrumb_writes", lambda _timeout: None)
    monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: None)
    await asyncio.wait_for(
        updates._restart_gateway(_state(), resolver=lambda: "/python"), timeout=30
    )


async def _slack_restart(monkeypatch) -> None:
    monkeypatch.setattr(events, "is_owner", lambda _caller: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())
    monkeypatch.setenv("INVOCATION_ID", "restart-test")
    monkeypatch.setattr("kiro_crew.cli.drain_log_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(platform_compat, "hard_exit", lambda *_a, **_k: None)
    state = _state()
    orch = SimpleNamespace(sessions=state.sessions, dashboard_state=state)
    await asyncio.wait_for(events._handle_restart(orch, "U1", "", AsyncMock()), timeout=30)


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "within 5.0s" in r.getMessage()
    ]


@pytest.mark.parametrize("run", [_dashboard_restart, _slack_restart], ids=["dashboard", "slack"])
@pytest.mark.parametrize(
    "outcome, warned",
    [(asyncio.TimeoutError(), True), (RuntimeError("history lock"), False), (None, False)],
    ids=["deadline", "other-failure", "saved"],
)
@pytest.mark.asyncio
async def test_a_restart_warns_only_when_the_save_hits_its_deadline(
    run, outcome, warned, monkeypatch, caplog
):
    def _save(_state):
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(dashboard_chat, "save_all_slots_to_history", _save)
    caplog.set_level(logging.DEBUG)
    await run(monkeypatch)
    assert bool(_warnings(caplog)) is warned, _warnings(caplog)


@pytest.mark.parametrize(
    "outcome, warned",
    [(asyncio.TimeoutError(), True), (RuntimeError("history lock"), False)],
    ids=["deadline", "other-failure"],
)
@pytest.mark.asyncio
async def test_the_shutdown_warns_only_when_the_save_hits_its_deadline(outcome, warned, caplog):
    from test_slack_gateway_more_coverage import _make_orchestrator, _mock_dashboard_state

    orch = _make_orchestrator()
    orch.cron_svc = None
    orch.heartbeat_svc = None
    orch.subagent_mgr = None
    orch.sessions = None
    orch._dashboard_runner = None
    ds = _mock_dashboard_state()
    ds._loop_watchdog = None
    ds._loop_heartbeat = None
    orch.dashboard_state = ds
    orch._stop_mcp_broker = AsyncMock()
    caplog.set_level(logging.DEBUG)
    with patch("kiro_crew.dashboard.chat.save_all_slots_to_history", side_effect=outcome):
        await orch._shutdown()
    assert bool(_warnings(caplog)) is warned, _warnings(caplog)
    ds.file_indexes.stop_all.assert_called_once_with()
