"""The shutdown signal handlers and the bounded exit in ``gateway_runtime/exit_path.py``.

``_install_shutdown_signal_handlers`` puts one handler on SIGINT and SIGTERM. The
first signal cancels any wheel apply in flight and sets the shared shutdown event;
the second force-exits after reaping orphaned sessions and draining the member
event log and the ``gateway.log`` queue, none of which may stop the exit by
raising. These tests capture the installed handler and drive each path with
every effect replaced. ``_shutdown_and_exit`` runs every drain and exits once even
when the graceful teardown and the drains fail; ``os._exit`` is replaced, so the
test process never exits.
"""

from __future__ import annotations

import asyncio
import signal
import threading
import time
import weakref
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli as cli_mod
from kiro_crew import eventlog_hooks
from kiro_crew import session as session_mod
from kiro_crew import skill_usage
from kiro_crew.slack import gateway as gw


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    found = SimpleNamespace(
        compat=MagicMock(name="platform_compat"),
        event=MagicMock(name="shutdown_event"),
        cleanup=MagicMock(name="cleanup_orphaned_sessions"),
        drain=MagicMock(name="drain_for_shutdown"),
        stop_log=MagicMock(name="_stop_log_queue_listener"),
    )
    monkeypatch.setattr(gw, "platform_compat", found.compat)
    monkeypatch.setattr(gw, "shutdown_event", found.event)
    monkeypatch.setattr(session_mod, "cleanup_orphaned_sessions", found.cleanup)
    monkeypatch.setattr(eventlog_hooks, "drain_for_shutdown", found.drain)
    monkeypatch.setattr(cli_mod, "_stop_log_queue_listener", found.stop_log)
    return found


def _install(monkeypatch: pytest.MonkeyPatch) -> dict[Any, Any]:
    """Install the handlers on the running loop and return them by signal."""
    handlers: dict[Any, Any] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    orch._install_shutdown_signal_handlers()
    return handlers


@pytest.mark.asyncio
async def test_both_signals_share_one_handler(monkeypatch, seams) -> None:
    handlers = _install(monkeypatch)
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    assert handlers[signal.SIGINT] is handlers[signal.SIGTERM]


@pytest.mark.asyncio
async def test_the_first_signal_stops_the_apply_and_sets_the_event(monkeypatch, seams) -> None:
    handlers = _install(monkeypatch)

    handlers[signal.SIGTERM]()

    seams.compat.cancel_wheel_applies_in_flight.assert_called_once_with("shutdown")
    seams.event.set.assert_called_once_with()
    seams.compat.hard_exit.assert_not_called()
    seams.cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_a_second_signal_drains_and_force_exits(monkeypatch, seams, capsys) -> None:
    handlers = _install(monkeypatch)

    handlers[signal.SIGINT]()
    handlers[signal.SIGINT]()

    assert seams.compat.cancel_wheel_applies_in_flight.call_count == 2
    seams.cleanup.assert_called_once_with(narrow_with_leaders=False)
    seams.drain.assert_called_once_with()
    seams.stop_log.assert_called_once_with(timeout=2.0)
    seams.compat.hard_exit.assert_called_once_with(0)
    assert "Force exit!" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_failing_drain_never_blocks_the_force_exit(monkeypatch, seams) -> None:
    seams.drain.side_effect = OSError("disk wedged")
    seams.stop_log.side_effect = RuntimeError("listener gone")
    handlers = _install(monkeypatch)

    handlers[signal.SIGINT]()
    handlers[signal.SIGINT]()

    seams.compat.hard_exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_off_the_main_thread_no_handler_is_installed(monkeypatch, seams) -> None:
    def _not_main_thread(_sig: Any, _cb: Any) -> None:
        raise RuntimeError("set_wakeup_fd only works in main thread")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", _not_main_thread)
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)

    orch._install_shutdown_signal_handlers()  # skipped quietly, as under an xdist worker

    seams.event.set.assert_not_called()


# ── the bounded exit ──────────────────────────────────────────────────────────


class _NoExitOs:
    """The facade's ``os`` with ``_exit`` recorded instead of ending the process."""

    def __init__(self) -> None:
        self._exit = MagicMock(name="os._exit")

    def __getattr__(self, name: str) -> Any:
        import os

        return getattr(os, name)


@pytest.mark.parametrize("drain_fails", ["returns-false", "raises"])
@pytest.mark.asyncio
async def test_the_exit_path_runs_every_drain_and_exits_once(
    monkeypatch, seams, caplog, drain_fails: str
) -> None:
    from kiro_crew.crew_log import emit as crew_log_emit

    no_exit = _NoExitOs()
    monkeypatch.setattr(gw, "os", no_exit)
    monkeypatch.setattr(gw, "drain_sign_in_refresher", AsyncMock())
    monkeypatch.setattr(gw, "flush_audit_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(cli_mod, "drain_log_queue_before_hard_exit", AsyncMock())
    drain_kwargs: dict[str, Any] = (
        {"return_value": False}
        if drain_fails == "returns-false"
        else {"side_effect": OSError("disk wedged")}
    )
    monkeypatch.setattr(crew_log_emit, "drain_for_shutdown", MagicMock(**drain_kwargs))
    monkeypatch.setattr(skill_usage, "flush_all_for_shutdown", MagicMock(**drain_kwargs))
    seams.drain.configure_mock(**drain_kwargs)
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    orch.dashboard_state = None
    orch._dashboard_port = 0
    orch._sign_in_refresher_task = None
    orch._shutdown = AsyncMock(side_effect=RuntimeError("teardown wedged"))

    with caplog.at_level("DEBUG", logger="kiro_crew.slack.gateway"):
        await orch._shutdown_and_exit()

    no_exit._exit.assert_called_once_with(0)
    seams.cleanup.assert_called_once_with()
    gw.flush_audit_queue_before_hard_exit.assert_awaited_once_with()
    cli_mod.drain_log_queue_before_hard_exit.assert_awaited_once_with()
    assert "Graceful shutdown timed out — force exiting" in caplog.text
    if drain_fails == "returns-false":
        assert "the session's log did not fully drain before exit" in caplog.text
        assert "the member event log did not fully drain before exit" in caplog.text
    else:
        assert "the session's log drain failed during shutdown" in caplog.text
        assert "the member event log drain failed during shutdown" in caplog.text
    if drain_fails == "returns-false":
        assert "the skill-usage ledger did not fully flush before exit" in caplog.text
    else:
        assert "the skill-usage ledger flush failed during shutdown" in caplog.text


def _bare_orchestrator() -> Any:
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    orch.dashboard_state = None
    orch._dashboard_port = 0
    orch._sign_in_refresher_task = None
    orch._shutdown = AsyncMock()
    return orch


@pytest.mark.asyncio
async def test_the_exit_path_persists_skill_hits_accepted_inside_the_debounce_window(
    monkeypatch, seams, tmp_path
) -> None:
    # The gateway leaves through os._exit, which skips atexit and kills the
    # daemon flush thread. A hit record() accepted inside the 60 s window must
    # still be on disk once _shutdown_and_exit has run.
    from kiro_crew.skill_usage import SkillUsageLedger

    monkeypatch.setattr(skill_usage, "_live_ledgers", weakref.WeakSet())
    no_exit = _NoExitOs()
    monkeypatch.setattr(gw, "os", no_exit)
    monkeypatch.setattr(gw, "drain_sign_in_refresher", AsyncMock())
    monkeypatch.setattr(gw, "flush_audit_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(cli_mod, "drain_log_queue_before_hard_exit", AsyncMock())
    path = tmp_path / "skill-usage.json"
    ledger = SkillUsageLedger(path)
    ledger._last_flush = time.time() + 3600  # inside the window: record() spawns no write
    ledger.record("alpha")
    ledger.record("alpha")
    assert not path.exists(), "precondition: the hits are debounced, only in memory"

    await _bare_orchestrator()._shutdown_and_exit()

    no_exit._exit.assert_called_once_with(0)
    assert SkillUsageLedger(path).score("alpha")[0] == 2.0
    assert not ledger.dirty


@pytest.mark.asyncio
async def test_a_wedged_skill_usage_flush_cannot_hold_the_exit(monkeypatch, seams) -> None:
    release = threading.Event()

    def _wedged() -> bool:
        release.wait(10)
        return True

    monkeypatch.setattr(skill_usage, "flush_all_for_shutdown", _wedged)
    monkeypatch.setattr(skill_usage, "SHUTDOWN_FLUSH_SECS", 0.05)
    no_exit = _NoExitOs()
    monkeypatch.setattr(gw, "os", no_exit)
    monkeypatch.setattr(gw, "drain_sign_in_refresher", AsyncMock())
    monkeypatch.setattr(gw, "flush_audit_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(cli_mod, "drain_log_queue_before_hard_exit", AsyncMock())
    try:
        await _bare_orchestrator()._shutdown_and_exit()
        no_exit._exit.assert_called_once_with(0)
    finally:
        release.set()
