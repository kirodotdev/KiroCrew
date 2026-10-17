"""Every restart that closes the sessions waits for the dashboard turns it cut.

The update restart (``_restart_after_update_claimed``), the dashboard restart
(``_restart_gateway``) and the Slack ``/kirocrew restart`` (``_handle_restart``)
save every chat slot, then ``close_all()`` the sessions, then exec or exit. A
dashboard turn that was mid-reply when its session closed saves the text its user
watched stream as it unwinds, after that first save. Each test here parks such a
turn mid-unwind and lets it save only once the restart waits on it, or has already
reached its exec or exit; the save must land first.
``test_slack_gateway.py::TestShutdown`` pins the same order for the graceful
shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard import chat as dashboard_chat
from kiro_crew.dashboard.handlers import updates
from kiro_crew.slack import events
from kiro_crew.slack import gateway as slack_gateway
from kiro_crew.slack.gateway import GatewayOrchestrator


class _CutTurn(asyncio.Future):
    """The task of a dashboard turn ``close_all()`` cut, still unwinding.

    Waiting on a future means registering a done callback on it, whether through
    ``asyncio.wait``, ``wait_for`` or a plain ``await``, so that registration is
    the moment the restart starts waiting for this turn.
    """

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order
        self.waited_on = asyncio.Event()

    def add_done_callback(self, fn, *, context=None) -> None:
        if not self.waited_on.is_set():
            self.order.append("waits")
            self.waited_on.set()
        if context is None:
            super().add_done_callback(fn)
        else:
            super().add_done_callback(fn, context=context)


class _Restart:
    """One restart, the turn it cuts, and the order of what happens."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.turn = _CutTurn(self.order)
        self.state = SimpleNamespace(
            _slots={"s1": SimpleNamespace(task=self.turn)},
            _gateway_restart_in_progress=False,
            push_update_progress=Mock(),
            sessions=SimpleNamespace(close_all=self.close_all),
        )

    async def close_all(self, *_args, **_kwargs) -> None:
        self.order.append("close_all")

    def end(self, label: str) -> Callable[..., None]:
        return lambda *_a, **_k: self.order.append(label)


def _update_restart(restart: _Restart, monkeypatch) -> Awaitable[object]:
    monkeypatch.setattr(slack_gateway, "flush_breadcrumb_writes", Mock())
    monkeypatch.setattr(platform_compat, "reexec_python_module", restart.end("exec"))
    orch = SimpleNamespace(
        _pending_update_respawn=None,
        _update_apply_deferred=False,
        _UPDATE_DRAIN_TIMEOUT_SECS=GatewayOrchestrator._UPDATE_DRAIN_TIMEOUT_SECS,
        dashboard_state=restart.state,
        sessions=SimpleNamespace(
            close_all=restart.close_all, fence_update_restart=Mock(return_value=True)
        ),
        _drain_update_callback_work=AsyncMock(return_value=True),
        _stop_mcp_broker=AsyncMock(),
    )
    return GatewayOrchestrator._restart_after_update_claimed(orch, None, "/python")


def _dashboard_restart(restart: _Restart, monkeypatch) -> Awaitable[object]:
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(platform_compat, "execv_target_available", lambda _exe: True)
    monkeypatch.setattr(updates, "flush_breadcrumb_writes", lambda _timeout: None)
    monkeypatch.setattr(updates, "reexec_python_module", restart.end("exec"))
    return updates._restart_gateway(restart.state, resolver=lambda: "/python")


def _slack_restart(restart: _Restart, monkeypatch) -> Awaitable[object]:
    monkeypatch.setattr(events, "is_owner", lambda _caller: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())
    monkeypatch.setenv("INVOCATION_ID", "restart-test")
    monkeypatch.setattr("kiro_crew.cli.drain_log_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(platform_compat, "hard_exit", restart.end("exit"))
    orch = SimpleNamespace(sessions=restart.state.sessions, dashboard_state=restart.state)
    return events._handle_restart(orch, "U1", "", AsyncMock())


_RESTARTS = pytest.mark.parametrize(
    "run, ends_with",
    [
        pytest.param(_update_restart, "exec", id="update"),
        pytest.param(_dashboard_restart, "exec", id="dashboard"),
        pytest.param(_slack_restart, "exit", id="slack"),
    ],
)


@pytest.fixture
def _first_save_skipped(monkeypatch):
    """The restarts' own save before close_all() is not what these tests watch."""
    monkeypatch.setattr(dashboard_chat, "save_all_slots_to_history", lambda _state: None)


@_RESTARTS
@pytest.mark.asyncio
async def test_a_restart_waits_for_the_turn_it_cut_before_it_ends(
    run, ends_with, monkeypatch, _first_save_skipped
):
    monkeypatch.setattr(
        "kiro_crew.gateway_shutdown_budget.DASHBOARD_TURN_UNWIND_SECS", 5.0, raising=False
    )
    restart = _Restart()
    restarting = asyncio.ensure_future(run(restart, monkeypatch))
    waited_on = asyncio.ensure_future(restart.turn.waited_on.wait())
    try:
        await asyncio.wait_for(
            asyncio.wait({waited_on, restarting}, return_when=asyncio.FIRST_COMPLETED), 30
        )
        # The turn's partial-reply save, then its end.
        restart.order.append("saved")
        restart.turn.set_result(None)
        await asyncio.wait_for(restarting, timeout=30)
        assert restart.order == [
            "close_all",
            "waits",
            "saved",
            ends_with,
        ], f"the restart did not wait for the turn it cut: {restart.order}"
    finally:
        waited_on.cancel()
        restart.turn.cancel()
        await asyncio.wait_for(
            asyncio.gather(waited_on, restarting, return_exceptions=True), timeout=30
        )


@_RESTARTS
@pytest.mark.asyncio
async def test_a_restart_never_cancels_or_outwaits_a_hung_turn(
    run, ends_with, monkeypatch, _first_save_skipped
):
    monkeypatch.setattr(
        "kiro_crew.gateway_shutdown_budget.DASHBOARD_TURN_UNWIND_SECS", 0.2, raising=False
    )
    restart = _Restart()
    try:
        await asyncio.wait_for(run(restart, monkeypatch), timeout=30)
        assert restart.order[0] == "close_all", restart.order
        assert restart.order[-1] == ends_with, restart.order
        assert not restart.turn.done(), "a hung turn must be left to the exit, not cancelled"
    finally:
        restart.turn.cancel()
