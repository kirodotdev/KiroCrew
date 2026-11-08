"""Every restart reaches its exec or exit when closing the sessions never finishes.

The dashboard restart (``_restart_gateway``), the update restart
(``_restart_after_update_claimed``) and the Slack ``/kirocrew restart``
(``_handle_restart``) close every session with ``close_all()`` and then exec or
exit. ``close_all`` cancels each owned background task and then gathers it, with
no deadline of its own. Here one such task outlives every cancel, as one whose
cancel handler is blocked on I/O does, so the close never finishes: each restart
must still reach its exec or exit, name the step it cut in a warning, and leave
that task to the exec rather than wait on it.
"""

from __future__ import annotations

import asyncio
import logging
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

#: How long a restart past its session-close cap gets to reach its end. A restart
#: that waits on the held close never gets there.
_REACHES_END_SECS = 10.0


class _HeldClose:
    """A ``close_all`` held by an owned background task that outlives every cancel.

    It does with that task what ``SessionLifecycleService.close_all`` does with
    each of its owned tasks: cancel it, then gather it.
    """

    def __init__(self) -> None:
        self.order: list[str] = []
        self.released = asyncio.Event()
        self.background = asyncio.ensure_future(self._outlive_every_cancel())
        self.closing: asyncio.Future | None = None
        self.background_running_at_end: bool | None = None

    async def _outlive_every_cancel(self) -> None:
        while not self.released.is_set():
            try:
                await self.released.wait()
            except asyncio.CancelledError:
                continue

    async def close_all(self, *_args, **_kwargs) -> None:
        self.closing = asyncio.current_task()
        self.order.append("close_all")
        self.background.cancel()
        await asyncio.gather(self.background, return_exceptions=True)

    def end(self, label: str) -> Callable[..., None]:
        def _end(*_args, **_kwargs) -> None:
            self.order.append(label)
            self.background_running_at_end = not self.background.done()

        return _end

    def state(self) -> SimpleNamespace:
        return SimpleNamespace(
            _slots={},
            _gateway_restart_in_progress=False,
            push_update_progress=Mock(),
            sessions=SimpleNamespace(close_all=self.close_all),
        )


def _update_restart(close: _HeldClose, monkeypatch) -> Awaitable[object]:
    monkeypatch.setattr(slack_gateway, "flush_breadcrumb_writes", Mock())
    monkeypatch.setattr(platform_compat, "reexec_python_module", close.end("exec"))
    orch = SimpleNamespace(
        _pending_update_respawn=None,
        _update_apply_deferred=False,
        # Handed only to the mocked drain below, which returns at once.
        _UPDATE_DRAIN_TIMEOUT_SECS=None,
        dashboard_state=close.state(),
        sessions=SimpleNamespace(
            close_all=close.close_all, fence_update_restart=Mock(return_value=True)
        ),
        _drain_update_callback_work=AsyncMock(return_value=True),
        _stop_mcp_broker=AsyncMock(),
    )
    return GatewayOrchestrator._restart_after_update_claimed(orch, None, "/python")


def _dashboard_restart(close: _HeldClose, monkeypatch) -> Awaitable[object]:
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(platform_compat, "execv_target_available", lambda _exe: True)
    monkeypatch.setattr(updates, "flush_breadcrumb_writes", lambda _timeout: None)
    monkeypatch.setattr(updates, "reexec_python_module", close.end("exec"))
    return updates._restart_gateway(close.state(), resolver=lambda: "/python")


def _slack_restart(close: _HeldClose, monkeypatch) -> Awaitable[object]:
    monkeypatch.setattr(events, "is_owner", lambda _caller: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())
    monkeypatch.setenv("INVOCATION_ID", "restart-test")
    monkeypatch.setattr("kiro_crew.cli.drain_log_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(platform_compat, "hard_exit", close.end("exit"))
    state = close.state()
    orch = SimpleNamespace(sessions=state.sessions, dashboard_state=state)
    return events._handle_restart(orch, "U1", "", AsyncMock())


@pytest.fixture
def _first_save_skipped(monkeypatch):
    """The restarts' own save before close_all() is not what these tests watch."""
    monkeypatch.setattr(dashboard_chat, "save_all_slots_to_history", lambda _state: None)


@pytest.mark.parametrize(
    "run, ends_with",
    [
        pytest.param(_update_restart, "exec", id="update"),
        pytest.param(_dashboard_restart, "exec", id="dashboard"),
        pytest.param(_slack_restart, "exit", id="slack"),
    ],
)
@pytest.mark.asyncio
async def test_a_restart_whose_session_close_never_finishes_still_ends(
    run, ends_with, monkeypatch, caplog, _first_save_skipped
):
    # The held close never finishes, so any cap expires; a short one keeps it quick.
    monkeypatch.setattr(
        "kiro_crew.gateway_shutdown_budget.RESTART_SESSION_CLOSE_SECS", 0.2, raising=False
    )
    monkeypatch.setattr("kiro_crew.slack.events._RESTART_SESSION_CLOSE_SECS", 0.2, raising=False)
    caplog.set_level(logging.INFO)
    close = _HeldClose()
    restarting = asyncio.ensure_future(run(close, monkeypatch))
    try:
        done, _pending = await asyncio.wait({restarting}, timeout=_REACHES_END_SECS)
        assert restarting in done, f"the restart is still waiting on the close: {close.order}"
        restarting.result()
        assert close.order == ["close_all", ends_with], close.order
        assert close.background_running_at_end, "the restart waited on the held task"
        cut = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "closing sessions did not finish" in record.getMessage()
        ]
        assert cut, "no warning names the step the restart cut"
    finally:
        close.released.set()
        held = {restarting, close.background}
        if close.closing is not None:
            held.add(close.closing)
        await asyncio.wait(held, timeout=_REACHES_END_SECS)


@pytest.mark.asyncio
async def test_the_dashboard_restart_logs_each_step_as_it_starts(
    monkeypatch, caplog, _first_save_skipped
):
    """A restart that stalls names its step: the last step logged is the one it is in."""
    execs: list[str] = []
    state = SimpleNamespace(
        _slots={},
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=AsyncMock()),
        _mcp_gateway_stop=AsyncMock(),
    )
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(platform_compat, "execv_target_available", lambda _exe: True)
    monkeypatch.setattr(updates, "flush_breadcrumb_writes", lambda _timeout: None)
    monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: execs.append("exec"))
    caplog.set_level(logging.INFO, logger="kiro_crew.dashboard.handlers.updates")

    restarted = await asyncio.wait_for(
        updates._restart_gateway(state, resolver=lambda: "/python"), timeout=30
    )

    assert restarted is True and execs == ["exec"]
    prefix = "Gateway restart: "
    steps = [
        record.getMessage()[len(prefix) :].rsplit(" (+", 1)[0]
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]
    assert steps == [
        "saving chat history",
        "recording running chats",
        "closing sessions",
        "waiting for cut turns",
        "stopping the MCP broker",
        "running pre-exec stops",
        "flushing safety-override records",
        "exec",
    ]
