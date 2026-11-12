"""The post-stop port probe waits out a bounded release grace before it reports.

``stop_app_backend`` signals the backend's whole process group but waits only on the
root it spawned. For a sandboxed backend that root is the sandbox wrapper, which exits
before the server inside it has closed its listening socket, so a single probe right
after the stop caught the port still bound. Disable, trust withdrawal and uninstall
then reported "backend still running ... not ours to stop" for a backend that was
already on its way out.

These tests pin both halves: a port released inside the grace is not reported, and a
listener that outlives it still is, on every caller of the shared sequence, including
the withdrawn-trust teardown that must refuse. A probe that stops seeing the listener
ends the wait only when :func:`port_released` confirms the port is free, because the
probe also answers "not listening" on a connect timeout.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest

import kiro_crew.apps.teardown as appteardown
from kiro_crew.platform_compat import PortListener
from kiro_crew.testing.clock import ManualClock

APP = "port-grace-app"
PORT = 9137


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    """A virtual clock on the teardown module, so the grace never sleeps for real.

    Set directly rather than through ``ManualClock.install``, which refuses a module
    with no ``time`` binding: the regression tests below must FAIL on an assertion,
    not error in setup, against a teardown that never re-probes. A clock the module
    stopped reading is still caught, by the slept-total assertion.
    """
    manual = ManualClock()
    monkeypatch.setattr(appteardown, "time", manual.as_time_module(), raising=False)
    return manual


class _Release:
    """Stands in for :func:`port_released`: records each port it is asked about."""

    def __init__(self) -> None:
        self.confirms = True
        self.asked: list[int] = []

    def __call__(self, port: int) -> bool:
        self.asked.append(port)
        return self.confirms


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> _Release:
    fake = _Release()
    monkeypatch.setattr(appteardown, "port_released", fake)
    return fake


def _probe_releasing_after(listening_probes: int) -> tuple[Callable[..., int | None], list[Any]]:
    """A probe that sees *PORT* listening for *listening_probes* calls, then free."""
    hints: list[Any] = []

    def _probe(name: str, *, port_hint: int | None = None) -> int | None:
        assert name == APP
        hints.append(port_hint)
        return PORT if len(hints) <= listening_probes else None

    return _probe, hints


def _never_releasing_probe() -> tuple[Callable[..., int | None], list[Any]]:
    hints: list[Any] = []

    def _probe(name: str, *, port_hint: int | None = None) -> int | None:
        hints.append(port_hint)
        return PORT

    return _probe, hints


async def _observe(probe: Callable[..., int | None], hint: int | None = PORT) -> tuple[Any, bool]:
    return await appteardown.stop_backend_and_observe(
        APP, recorded=lambda name: hint, stop=lambda name: True, probe=probe
    )


@pytest.mark.asyncio
async def test_a_port_released_during_the_grace_is_not_reported(
    clock: ManualClock, released: _Release
) -> None:
    # REGRESSION: one probe right after the stop saw the sandboxed server's socket
    # before the server had closed it, and the backend was reported as still running.
    probe, hints = _probe_releasing_after(2)

    live_port, had_hint = await _observe(probe)

    assert (live_port, had_hint) == (None, True)
    assert hints == [PORT, PORT, PORT], "every re-probe keeps the gateway-recorded hint"
    assert clock.sleeps == (appteardown.PORT_RELEASE_POLL_SECONDS,) * 2
    assert released.asked == [PORT], "the release is confirmed once, on the live port"


@pytest.mark.asyncio
async def test_a_probe_losing_sight_of_an_unconfirmed_listener_still_reports_it(
    clock: ManualClock, released: _Release
) -> None:
    # REGRESSION: the probe reads a connect timeout as "not listening", and a listener
    # that stops accepting turns the re-probes into timeouts once its backlog fills.
    # The first such None ended the wait as a release while the socket stayed bound.
    released.confirms = False
    probe, _ = _probe_releasing_after(1)

    live_port, _ = await _observe(probe)

    assert live_port == PORT
    assert sum(clock.sleeps) >= appteardown.PORT_RELEASE_GRACE_SECONDS
    assert released.asked and set(released.asked) == {PORT}


@pytest.mark.asyncio
async def test_a_listener_that_outlives_the_grace_is_still_reported(
    clock: ManualClock, released: _Release
) -> None:
    probe, hints = _never_releasing_probe()

    live_port, _ = await _observe(probe)

    assert live_port == PORT
    assert sum(clock.sleeps) >= appteardown.PORT_RELEASE_GRACE_SECONDS
    assert set(hints) == {PORT}
    assert released.asked == [], "a probe that still sees the port needs no confirmation"


@pytest.mark.asyncio
async def test_a_free_port_is_probed_once_with_no_wait(
    clock: ManualClock, released: _Release
) -> None:
    probe, hints = _probe_releasing_after(0)

    assert await _observe(probe) == (None, True)
    assert hints == [PORT]
    assert clock.sleeps == ()
    assert released.asked == []


@pytest.mark.asyncio
async def test_the_manifest_fallback_gets_the_same_grace(
    clock: ManualClock, released: _Release
) -> None:
    # With no recorded port the probe falls back to the declared one; the grace is
    # about how the stop waits, not about where the port came from.
    probe, hints = _probe_releasing_after(1)

    assert await _observe(probe, hint=None) == (None, False)
    assert hints == [None, None]


@pytest.fixture
def _quiet_teardown_steps() -> Iterator[None]:
    """Every teardown step except the backend observation, made inert."""

    async def _hooks(name: str, record: Any, **kw: Any) -> dict[str, Any]:
        return {}

    with (
        patch.object(appteardown, "on_app_disable", _hooks),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "recorded_backend_port", lambda name: PORT),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawing_trust", [False, True])
@pytest.mark.usefixtures("_quiet_teardown_steps")
async def test_teardown_does_not_fail_on_a_port_released_during_the_grace(
    clock: ManualClock, released: _Release, withdrawing_trust: bool
) -> None:
    probe, _ = _probe_releasing_after(3)
    with patch.object(appteardown, "unstopped_backend_port", probe):
        result = await appteardown.teardown_app_runtime(
            APP, {"resources": "gateway"}, withdrawing_trust=withdrawing_trust
        )

    assert result.ok, result.failures
    assert not any("still running" in w for w in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawing_trust", [False, True])
@pytest.mark.usefixtures("_quiet_teardown_steps")
@pytest.mark.parametrize("probe_loses_sight", [False, True])
async def test_teardown_still_fails_on_a_listener_that_outlives_the_grace(
    clock: ManualClock, released: _Release, withdrawing_trust: bool, probe_loses_sight: bool
) -> None:
    # The withdrawn-trust caller refuses on `failures`, so the surviving listener
    # must still land there, also when the re-probes stop seeing it without the
    # release being confirmed.
    released.confirms = False
    probe, _ = _probe_releasing_after(1) if probe_loses_sight else _never_releasing_probe()
    with patch.object(appteardown, "unstopped_backend_port", probe):
        result = await appteardown.teardown_app_runtime(
            APP, {"resources": "gateway"}, withdrawing_trust=withdrawing_trust
        )

    assert not result.ok
    assert any(f"backend still running on port {PORT}" in f for f in result.failures)


class TestPortReleased:
    """Only a refusal, or a completed lookup with no LISTEN socket, counts as free."""

    def test_an_accepting_listener_is_not_released(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            assert appteardown.port_released(server.getsockname()[1]) is False

    def test_a_bound_port_nobody_listens_on_is_released(self) -> None:
        # Bound but never listening: the port stays reserved for the test, and a
        # connect to it is refused (or, on Windows, ends in the PID lookup, which
        # finds no LISTEN socket on it).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
            reserved.bind(("127.0.0.1", 0))
            assert appteardown.port_released(reserved.getsockname()[1]) is True

    @pytest.mark.parametrize(
        ("lookup", "expected"),
        [
            pytest.param(([PortListener(4242, "127.0.0.1", "4")], True), False, id="bound"),
            pytest.param(([], True), True, id="lookup-found-none"),
            pytest.param(([], False), False, id="lookup-failed"),
        ],
    )
    def test_a_connect_timeout_is_settled_by_the_listener_lookup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        lookup: tuple[list[PortListener], bool],
        expected: bool,
    ) -> None:
        # A full backlog drops the handshake, so the connect times out while the
        # socket is still bound; that timeout alone must never read as a release.
        def _timeout(*args: Any, **kwargs: Any) -> socket.socket:
            raise TimeoutError("timed out")

        looked_up: list[int] = []

        def _lookup(port: int) -> tuple[list[PortListener], bool]:
            looked_up.append(port)
            return lookup

        monkeypatch.setattr(appteardown.socket, "create_connection", _timeout)
        monkeypatch.setattr(appteardown.platform_compat, "probe_port_listeners", _lookup)

        assert appteardown.port_released(PORT) is expected
        assert looked_up == [PORT]
