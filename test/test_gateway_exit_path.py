"""The shutdown signal handlers and the bounded exit in ``gateway_runtime/exit_path.py``.

``_install_shutdown_signal_handlers`` puts one handler on SIGINT and SIGTERM. The
first signal cancels any wheel apply in flight and sets the shared shutdown event;
the second claims the exit and hands it to one non-daemon ``kc-force-exit``
thread, which drains the SEL audit queue, reaps orphaned sessions, drains the
member event log, drains SEL again and then the ``gateway.log`` queue, none of
which may stop the exit by raising. These tests capture the installed handler and
drive each path with every effect replaced, joining that thread. ``_shutdown_and_exit``
runs every drain and exits once even when the graceful teardown and the drains
fail, and parks instead once a force exit is claimed; ``os._exit`` is replaced,
so the test process never exits.
"""

from __future__ import annotations

import asyncio
import signal
import sys
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
def seams(monkeypatch: pytest.MonkeyPatch):
    found = SimpleNamespace(
        compat=MagicMock(name="platform_compat"),
        event=MagicMock(name="shutdown_event"),
        cleanup=MagicMock(name="cleanup_orphaned_sessions"),
        drain=MagicMock(name="drain_for_shutdown"),
        stop_log=MagicMock(name="_stop_log_queue_listener"),
        flush=MagicMock(name="flush_audit_queue"),
    )
    monkeypatch.setattr(gw, "platform_compat", found.compat)
    monkeypatch.setattr(gw, "shutdown_event", found.event)
    monkeypatch.setattr(gw, "flush_audit_queue", found.flush)
    monkeypatch.setattr(session_mod, "cleanup_orphaned_sessions", found.cleanup)
    monkeypatch.setattr(eventlog_hooks, "drain_for_shutdown", found.drain)
    monkeypatch.setattr(cli_mod, "_stop_log_queue_listener", found.stop_log)
    # The real threading module, with every thread and timer the handler starts
    # recorded: the force-exit thread can finish before a test could find it by
    # enumerating live threads.
    found.threads = []
    found.timers = []

    def _thread(*args: Any, **kwargs: Any) -> threading.Thread:
        thread = threading.Thread(*args, **kwargs)
        found.threads.append(thread)
        return thread

    def _timer(*args: Any, **kwargs: Any) -> threading.Timer:
        timer = threading.Timer(*args, **kwargs)
        found.timers.append(timer)
        return timer

    monkeypatch.setattr(
        gw, "threading", SimpleNamespace(Event=threading.Event, Thread=_thread, Timer=_timer)
    )
    yield found
    # Retire whatever a test started, so no deadline timer fires into a later
    # test and no drain thread outlives this one.
    for timer in found.timers:
        timer.cancel()
        if timer.is_alive():
            timer.join(5.0)
    for thread in found.threads:
        if thread.is_alive():
            thread.join(5.0)


def _install(monkeypatch: pytest.MonkeyPatch, orch: Any = None) -> dict[Any, Any]:
    """Install the handlers on the running loop and return them by signal."""
    handlers: dict[Any, Any] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))
    if orch is None:
        orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    orch._install_shutdown_signal_handlers()
    return handlers


def _join_force_exit(seams: SimpleNamespace) -> threading.Thread:
    """Wait for the one force-exit thread the second signal started to finish."""
    assert len(seams.threads) == 1, f"expected one force-exit thread, got {seams.threads}"
    (thread,) = seams.threads
    assert thread.name == "kc-force-exit"
    thread.join(10.0)
    assert not thread.is_alive(), "the force-exit thread did not finish"
    return thread


@pytest.mark.asyncio
async def test_both_signals_share_one_handler(monkeypatch, seams) -> None:
    handlers = _install(monkeypatch)
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    assert handlers[signal.SIGINT] is handlers[signal.SIGTERM]


@pytest.mark.asyncio
async def test_the_first_signal_stops_the_apply_and_sets_the_event(monkeypatch, seams) -> None:
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    handlers = _install(monkeypatch, orch)

    handlers[signal.SIGTERM]()

    seams.compat.cancel_wheel_applies_in_flight.assert_called_once_with("shutdown")
    seams.event.set.assert_called_once_with()
    seams.compat.hard_exit.assert_not_called()
    seams.cleanup.assert_not_called()
    seams.flush.assert_not_called()
    assert not orch._force_exit_claimed.is_set()
    assert seams.threads == [] and seams.timers == []


@pytest.mark.asyncio
async def test_a_second_signal_drains_the_audit_tail_and_force_exits(
    monkeypatch, seams, capsys
) -> None:
    """The queued SEL tail is drained before the exit -- once before the orphan
    sweep and once more before the gateway.log tail -- and every drain runs on
    the dedicated non-daemon thread, not on the loop thread that runs the
    signal handler."""
    order: list[tuple[str, int]] = []

    def _record(name: str) -> Any:
        return lambda *_a, **_k: order.append((name, threading.get_ident()))

    seams.flush.side_effect = _record("sel")
    seams.cleanup.side_effect = _record("orphans")
    seams.drain.side_effect = _record("eventlog")
    seams.stop_log.side_effect = _record("gateway.log")
    seams.compat.hard_exit.side_effect = _record("exit")
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    handlers = _install(monkeypatch, orch)

    handlers[signal.SIGINT]()
    handlers[signal.SIGINT]()
    assert orch._force_exit_claimed.is_set()
    thread = _join_force_exit(seams)
    assert not thread.daemon, "interpreter shutdown would kill a daemon drain mid-write"

    assert [name for name, _ in order] == [
        "sel",
        "orphans",
        "eventlog",
        "sel",
        "gateway.log",
        "exit",
    ]
    assert {ident for _, ident in order} == {thread.ident}
    assert threading.get_ident() != thread.ident
    assert seams.compat.cancel_wheel_applies_in_flight.call_count == 2
    seams.cleanup.assert_called_once_with(narrow_with_leaders=False)
    seams.stop_log.assert_called_once_with(timeout=2.0)
    seams.compat.hard_exit.assert_called_once_with(0)
    assert "Force exit!" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_signal_handler_does_not_wait_for_the_drain(monkeypatch, seams) -> None:
    """The handler is a loop callback: it hands the drain off and returns while
    the drain is still running, so the loop is never parked on a blocking flush."""
    entered = threading.Event()
    release = threading.Event()

    def _wedged_flush(*_a: Any, **_k: Any) -> None:
        entered.set()
        release.wait(10.0)

    seams.flush.side_effect = _wedged_flush
    handlers = _install(monkeypatch)
    try:
        handlers[signal.SIGINT]()
        handlers[signal.SIGINT]()  # returns although the flush below is wedged
        assert entered.wait(10.0), "the force-exit thread never reached the SEL drain"
        seams.compat.hard_exit.assert_not_called()
    finally:
        release.set()
    _join_force_exit(seams)
    seams.compat.hard_exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_a_repeat_signal_mid_drain_starts_no_second_drain(monkeypatch, seams) -> None:
    entered = threading.Event()
    release = threading.Event()
    first_flush = True

    def _held_flush(*_a: Any, **_k: Any) -> None:
        nonlocal first_flush
        if first_flush:
            first_flush = False
            entered.set()
            release.wait(10.0)

    seams.flush.side_effect = _held_flush
    handlers = _install(monkeypatch)
    try:
        handlers[signal.SIGINT]()
        handlers[signal.SIGINT]()
        assert entered.wait(10.0), "the force-exit thread never reached the SEL drain"
        handlers[signal.SIGINT]()  # mid-drain
        handlers[signal.SIGTERM]()
        assert len(seams.threads) == 1
        seams.compat.hard_exit.assert_not_called()
    finally:
        release.set()
    _join_force_exit(seams)
    assert seams.flush.call_count == 2
    seams.cleanup.assert_called_once_with(narrow_with_leaders=False)
    seams.compat.hard_exit.assert_called_once_with(0)
    # A stop still owns any apply, on every signal.
    assert seams.compat.cancel_wheel_applies_in_flight.call_count == 4


@pytest.mark.asyncio
async def test_a_failing_drain_never_blocks_the_force_exit(monkeypatch, seams) -> None:
    seams.cleanup.side_effect = OSError("pid file unreadable")
    seams.drain.side_effect = OSError("disk wedged")
    seams.stop_log.side_effect = RuntimeError("listener gone")
    handlers = _install(monkeypatch)

    handlers[signal.SIGINT]()
    handlers[signal.SIGINT]()
    _join_force_exit(seams)

    assert seams.flush.call_count == 2
    seams.compat.hard_exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_a_closed_stdout_cannot_strand_the_claimed_force_exit(monkeypatch, seams) -> None:
    """The claim turns every later signal away, so the status line failing
    (a closed stdout pipe) must not stop the exit from being started."""

    class _ClosedPipe:
        def write(self, _text: str) -> int:
            raise BrokenPipeError("stdout reader went away")

        def flush(self) -> None:
            raise BrokenPipeError("stdout reader went away")

    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    handlers = _install(monkeypatch)

    handlers[signal.SIGINT]()
    handlers[signal.SIGINT]()  # does not raise
    _join_force_exit(seams)

    assert len(seams.timers) == 1
    assert seams.flush.call_count == 2
    seams.compat.hard_exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_the_force_exit_is_backstopped_by_a_deadline(monkeypatch, seams) -> None:
    """A step that overruns its own bound (the orphan sweep has none) cannot
    hold the process: a daemon timer hard-exits at the deadline."""
    release = threading.Event()
    seams.cleanup.side_effect = lambda **_k: release.wait(10.0)
    handlers = _install(monkeypatch)
    try:
        handlers[signal.SIGINT]()
        handlers[signal.SIGINT]()
        (timer,) = seams.timers
        assert timer.name == "kc-force-exit-deadline"
        assert timer.daemon
        assert timer.is_alive()
        assert timer.interval == gw._FORCE_EXIT_DEADLINE_SECS  # type: ignore[attr-defined]
        timer.function(*timer.args)  # type: ignore[attr-defined]  # what it does at expiry
        seams.compat.hard_exit.assert_called_once_with(0)
    finally:
        release.set()
    _join_force_exit(seams)


def test_the_deadline_never_cuts_a_drain_keeping_to_its_bound() -> None:
    from kiro_crew import sel as sel_mod

    bounds = 2 * sel_mod._HARD_EXIT_FLUSH_TIMEOUT_SECS + eventlog_hooks.SHUTDOWN_DRAIN_SECONDS + 2.0
    assert gw._FORCE_EXIT_DEADLINE_SECS > bounds


@pytest.mark.asyncio
async def test_without_a_thread_the_force_exit_drains_inline(monkeypatch, seams) -> None:
    """When no thread can be started the handler drains inline: a blocked loop
    for the drains' bounded time beats exiting over the audit tail."""

    def _no_thread(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(
        gw,
        "threading",
        SimpleNamespace(Event=threading.Event, Timer=MagicMock(), Thread=_no_thread),
    )
    handlers = _install(monkeypatch)

    handlers[signal.SIGINT]()
    handlers[signal.SIGINT]()

    assert seams.flush.call_count == 2
    seams.stop_log.assert_called_once_with(timeout=2.0)
    seams.compat.hard_exit.assert_called_once_with(0)


# ── the graceful exit yields to a claimed force exit ─────────────────────────


@pytest.mark.asyncio
async def test_the_park_returns_when_no_force_exit_is_claimed() -> None:
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    await orch._park_if_force_exit_claimed()  # handlers never installed
    orch._force_exit_claimed = threading.Event()
    await orch._park_if_force_exit_claimed()


@pytest.mark.asyncio
async def test_the_park_never_returns_once_a_force_exit_is_claimed() -> None:
    """Returning would unwind run() and start interpreter shutdown under the
    force thread's drain; the park keeps the coroutine suspended instead."""
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    orch._force_exit_claimed = threading.Event()
    orch._force_exit_claimed.set()
    task = asyncio.create_task(orch._park_if_force_exit_claimed())
    await asyncio.sleep(0)  # the task's first step has run
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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


@pytest.mark.asyncio
async def test_the_exit_path_releases_shared_crew_slices_a_timed_out_shutdown_skipped(
    monkeypatch, seams
) -> None:
    from kiro_crew.shared_crew import admission_gate

    no_exit = _NoExitOs()
    monkeypatch.setattr(gw, "os", no_exit)
    monkeypatch.setattr(gw, "drain_sign_in_refresher", AsyncMock())
    monkeypatch.setattr(gw, "flush_audit_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(cli_mod, "drain_log_queue_before_hard_exit", AsyncMock())
    order: list[str] = []
    seams.cleanup.side_effect = lambda *a, **k: order.append("reap")

    async def release() -> int:
        order.append("release")
        return 1

    monkeypatch.setattr(admission_gate, "shutdown_cleanup", release)
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    orch.dashboard_state = None
    orch._dashboard_port = 0
    orch._sign_in_refresher_task = None
    orch._shutdown = AsyncMock(side_effect=asyncio.TimeoutError())

    await orch._shutdown_and_exit()

    # After the runtimes are reaped, before the hard exit.
    assert order == ["reap", "release"]
    no_exit._exit.assert_called_once_with(0)


async def _reached(barrier: asyncio.Event, task: asyncio.Task[Any]) -> None:
    """Wait for ``barrier``, bounded, and fail at once if ``task`` ends first.

    A producer that raises before it sets the barrier would otherwise leave
    the test waiting until the outer pytest timeout kills the worker.
    """
    waiter = asyncio.ensure_future(barrier.wait())
    done, _ = await asyncio.wait({waiter, task}, timeout=10.0, return_when=asyncio.FIRST_COMPLETED)
    if waiter in done:
        return
    waiter.cancel()
    if task in done:
        task.result()  # re-raises the producer's failure
        pytest.fail("the exit path returned before reaching the barrier")
    pytest.fail("the exit path never reached the barrier")


def _graceful_exit_seams(monkeypatch: pytest.MonkeyPatch) -> _NoExitOs:
    no_exit = _NoExitOs()
    monkeypatch.setattr(gw, "os", no_exit)
    monkeypatch.setattr(gw, "drain_sign_in_refresher", AsyncMock())
    monkeypatch.setattr(gw, "flush_audit_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(cli_mod, "drain_log_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(skill_usage, "flush_all_for_shutdown", MagicMock(return_value=True))
    from kiro_crew.shared_crew import admission_gate

    monkeypatch.setattr(admission_gate, "shutdown_cleanup", AsyncMock(return_value=0))
    return no_exit


@pytest.mark.asyncio
async def test_a_claimed_force_exit_parks_the_graceful_exit_before_its_drains(
    monkeypatch, seams
) -> None:
    """The force thread owns both drains and the exit once claimed, so the
    graceful exit neither drains under it nor ``os._exit``s over its tail."""
    no_exit = _graceful_exit_seams(monkeypatch)
    orch = _bare_orchestrator()
    orch._force_exit_claimed = threading.Event()
    orch._force_exit_claimed.set()
    parked = asyncio.Event()
    real_park = orch._park_if_force_exit_claimed

    async def _observed_park() -> None:
        parked.set()
        await real_park()

    orch._park_if_force_exit_claimed = _observed_park

    task = asyncio.create_task(orch._shutdown_and_exit())
    try:
        await _reached(parked, task)
        await asyncio.sleep(0)
        assert not task.done()
        gw.flush_audit_queue_before_hard_exit.assert_not_awaited()
        cli_mod.drain_log_queue_before_hard_exit.assert_not_awaited()
        no_exit._exit.assert_not_called()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_force_exit_claimed_during_the_sel_drain_parks_before_the_log_drain(
    monkeypatch, seams
) -> None:
    """A second signal landing while the graceful SEL drain runs: the graceful
    exit must not stop the gateway.log listener under the force thread's SEL
    drain, nor exit over it."""
    no_exit = _graceful_exit_seams(monkeypatch)
    orch = _bare_orchestrator()
    orch._force_exit_claimed = threading.Event()
    flushed = asyncio.Event()

    async def _flush_then_second_signal() -> None:
        orch._force_exit_claimed.set()
        flushed.set()

    gw.flush_audit_queue_before_hard_exit.side_effect = _flush_then_second_signal

    task = asyncio.create_task(orch._shutdown_and_exit())
    try:
        await _reached(flushed, task)
        await asyncio.sleep(0)
        assert not task.done()
        gw.flush_audit_queue_before_hard_exit.assert_awaited_once_with()
        cli_mod.drain_log_queue_before_hard_exit.assert_not_awaited()
        no_exit._exit.assert_not_called()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_an_unclaimed_force_exit_leaves_the_graceful_exit_as_it_was(
    monkeypatch, seams
) -> None:
    no_exit = _graceful_exit_seams(monkeypatch)
    orch = _bare_orchestrator()
    orch._force_exit_claimed = threading.Event()

    await orch._shutdown_and_exit()

    gw.flush_audit_queue_before_hard_exit.assert_awaited_once_with()
    cli_mod.drain_log_queue_before_hard_exit.assert_awaited_once_with()
    no_exit._exit.assert_called_once_with(0)
