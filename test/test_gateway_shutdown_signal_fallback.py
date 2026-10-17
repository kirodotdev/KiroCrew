"""Ctrl-C still stops the gateway when its event loop cannot install signal handlers.

``GatewayOrchestrator._install_shutdown_signal_handlers`` asks the running loop for
SIGINT and SIGTERM handlers. Windows' ProactorEventLoop answers
``NotImplementedError``, a ``RuntimeError`` subclass, and then SIGINT is installed
through ``signal.signal`` instead, with a handler that hands the stop back to the loop
thread. A loop that refuses for another reason answers ``RuntimeError`` (off the main
thread, or closed) or ``ValueError`` (a signal it does not know), and then nothing is
installed.

Every case drives the method with a stub loop and a recording ``signal.signal``: no
real signal is raised or installed, and nothing waits.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from kiro_crew import platform_compat, session
from kiro_crew.slack.gateway import GatewayOrchestrator


class _StubLoop:
    """A loop whose ``add_signal_handler`` refuses with *refusal*.

    ``call_soon_threadsafe`` records the callback instead of running it, so a test
    runs it when it chooses. With *closed*, it raises the ``RuntimeError`` a closed
    loop raises.
    """

    def __init__(self, refusal: type[Exception], *, closed: bool = False) -> None:
        self.refusal = refusal
        self.closed = closed
        self.asked: list[signal.Signals] = []
        self.scheduled: list[Callable[..., object]] = []

    def add_signal_handler(self, sig: signal.Signals, callback: Callable[..., object]) -> None:
        self.asked.append(sig)
        raise self.refusal("this loop cannot install signal handlers")

    def call_soon_threadsafe(self, callback: Callable[..., object], *args: object) -> None:
        if self.closed:
            raise RuntimeError("Event loop is closed")
        self.scheduled.append(callback)


@dataclass
class _Installed:
    """What one call of the method left behind."""

    loop: _StubLoop
    stopping: asyncio.Event
    attempts: list[int] = field(default_factory=list)
    registrations: list[tuple[int, Any]] = field(default_factory=list)
    cancelled_applies: list[str] = field(default_factory=list)

    def handler_for(self, sig: signal.Signals) -> Callable[..., object]:
        handlers = [handler for signum, handler in self.registrations if signum == sig]
        assert len(handlers) == 1, f"expected one {sig.name} registration, got {self.registrations}"
        return handlers[0]


def _refuse_force_exit(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a single signal must never reach the force-exit path")


def _install(
    monkeypatch: pytest.MonkeyPatch,
    loop: _StubLoop,
    *,
    signal_refusal: type[Exception] | None = None,
) -> _Installed:
    """Run the method against *loop*, recording what it registers."""
    installed = _Installed(loop=loop, stopping=asyncio.Event())

    def _record_signal(signum: int, handler: Any) -> None:
        installed.attempts.append(signum)
        if signal_refusal is not None:
            raise signal_refusal("signal.signal refused the handler")
        installed.registrations.append((signum, handler))

    monkeypatch.setattr("kiro_crew.slack.gateway.shutdown_event", installed.stopping)
    monkeypatch.setattr(
        platform_compat, "cancel_wheel_applies_in_flight", installed.cancelled_applies.append
    )
    # A second signal force-exits the process (reaping orphaned sessions first);
    # these cases send one signal at most, so reaching that path fails the test.
    monkeypatch.setattr(platform_compat, "hard_exit", _refuse_force_exit)
    monkeypatch.setattr(session, "cleanup_orphaned_sessions", _refuse_force_exit)

    orchestrator = GatewayOrchestrator.__new__(GatewayOrchestrator)
    # Both stdlib patches are process-wide, and only the install reads them, so they
    # end with it: pytest-timeout calls ``signal.signal`` itself when a test fails,
    # before the test's own monkeypatch is undone.
    with pytest.MonkeyPatch.context() as during_install:
        during_install.setattr(asyncio, "get_running_loop", lambda: loop)
        during_install.setattr(signal, "signal", _record_signal)
        orchestrator._install_shutdown_signal_handlers()
    return installed


class TestLoopWithoutSignalSupport:
    """``NotImplementedError`` installs SIGINT through ``signal.signal``."""

    def test_sigint_is_registered_with_signal_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        installed = _install(monkeypatch, _StubLoop(NotImplementedError))

        assert installed.loop.asked == [signal.SIGINT, signal.SIGTERM]
        assert [signum for signum, _handler in installed.registrations] == [signal.SIGINT]

    def test_the_handler_sets_the_shutdown_event_through_the_loop_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed = _install(monkeypatch, _StubLoop(NotImplementedError))

        installed.handler_for(signal.SIGINT)(signal.SIGINT, None)

        # The handler only schedules the stop: the loop thread runs it.
        assert len(installed.loop.scheduled) == 1
        assert not installed.stopping.is_set()
        assert installed.cancelled_applies == []

        installed.loop.scheduled[0]()

        assert installed.stopping.is_set()
        assert installed.cancelled_applies == ["shutdown"]

    def test_a_closed_loop_sets_the_shutdown_event_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed = _install(monkeypatch, _StubLoop(NotImplementedError, closed=True))

        installed.handler_for(signal.SIGINT)(signal.SIGINT, None)

        assert installed.loop.scheduled == []
        assert installed.stopping.is_set()
        assert installed.cancelled_applies == ["shutdown"]

    @pytest.mark.parametrize("refusal", [ValueError, OSError])
    def test_a_refused_signal_signal_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, refusal: type[Exception]
    ) -> None:
        installed = _install(monkeypatch, _StubLoop(NotImplementedError), signal_refusal=refusal)

        assert installed.loop.asked == [signal.SIGINT, signal.SIGTERM]
        assert installed.attempts == [signal.SIGINT]
        assert installed.registrations == []
        assert not installed.stopping.is_set()


class TestLoopThatRefusesTheSignal:
    """``RuntimeError`` (off the main thread, or closed) and ``ValueError`` (a signal the
    loop rejects) install nothing and raise nothing."""

    @pytest.mark.parametrize("refusal", [RuntimeError, ValueError])
    def test_nothing_is_registered(
        self, monkeypatch: pytest.MonkeyPatch, refusal: type[Exception]
    ) -> None:
        installed = _install(monkeypatch, _StubLoop(refusal))

        assert installed.loop.asked == [signal.SIGINT, signal.SIGTERM]
        assert installed.attempts == []
        assert installed.loop.scheduled == []
        assert not installed.stopping.is_set()
        assert installed.cancelled_applies == []
