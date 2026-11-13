"""Turn-state parity between the two per-session ACP drivers.

``AcpClient`` and ``AcpSessionHandle`` implement one state machine (is a turn running,
has it finished), and the 409 gate, ``AcpProvider.cancel()`` and the shutdown drain
cannot tell them apart. Each invariant runs against both via ``_DRIVERS``; real
differences are asserted as such. No subprocess: the adapter flips a mock's liveness.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.testing.wait import finished

# ── Driver adapters ───────────────────────────────────────────────────────────
#
# The two drivers spell the same reads differently: AcpClient exposes methods
# over its own child process, AcpSessionHandle exposes properties over a shared
# runtime it does not own. The adapter is the whole translation layer; every
# test below is written against the adapter only.


@dataclass(frozen=True)
class _Driver:
    """One per-session ACP driver, normalized to the turn-state surface."""

    name: str
    make: Callable[[], Any]
    turn_active: Callable[[Any], bool]
    unfinished: Callable[[Any], bool]
    set_transport_alive: Callable[[Any, bool], None]
    cancel: Callable[[Any], Any]  # returns an awaitable

    def begin_turn(self, obj: Any) -> None:
        """Model what every prompt entry does: clear the turn-done Event."""
        obj._turn_done.clear()

    def turn_done_is_set(self, obj: Any) -> bool:
        return bool(obj._turn_done.is_set())


def _client_process(alive: bool = True) -> MagicMock:
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = None if alive else 1
    # cancel_session writes the notification to stdin; an awaitable drain keeps
    # the write off the swallowed-exception path so the test exercises the real
    # ordering of the state mutations around it.
    proc.stdin = AsyncMock()
    proc.stdin.write = MagicMock()
    # _reset_state() closes the pipes synchronously; an AsyncMock close would
    # hand it an un-awaited coroutine instead.
    proc.stdin.close = MagicMock()
    return proc


def _make_client() -> AcpClient:
    client = AcpClient()
    client._process = _client_process()
    # cancel_session early-returns without a session id, which would make the
    # cancel invariants vacuous on this driver.
    client._session_id = "sess-parity"
    return client


def _make_handle() -> AcpSessionHandle:
    runtime = MagicMock()
    runtime.is_alive.return_value = True
    runtime.send_notification = AsyncMock()
    return AcpSessionHandle(
        session_id="sess-parity",
        queue=asyncio.Queue(),
        runtime=runtime,
        # Explicit settings so construction never reads config from disk.
        watchdog=WatchdogSettings(),
    )


_DRIVERS = (
    _Driver(
        name="AcpClient",
        make=_make_client,
        turn_active=lambda c: c.has_active_turn(),
        unfinished=lambda c: c.has_unfinished_turn(),
        set_transport_alive=lambda c, alive: setattr(
            c._process, "returncode", None if alive else 1
        ),
        cancel=lambda c: c.cancel_session(),
    ),
    _Driver(
        name="AcpSessionHandle",
        make=_make_handle,
        turn_active=lambda h: h.is_turn_active,
        unfinished=lambda h: h.has_unfinished_turn,
        set_transport_alive=lambda h, alive: h._runtime.is_alive.configure_mock(return_value=alive),
        cancel=lambda h: h.cancel(),
    ),
)

_PARAM = pytest.mark.parametrize("drv", _DRIVERS, ids=[d.name for d in _DRIVERS])


# ── Idle ──────────────────────────────────────────────────────────────────────


@_PARAM
def test_fresh_session_is_idle(drv: _Driver) -> None:
    """A new chat cannot pick a model or an agent, with no way out.

    The transport is alive and no prompt has been sent. If this reads as a turn
    in flight, set-model and set-agent answer 409 "a turn is running" forever --
    there is no timeout, so it looks like a hang, and the only escape is sending
    a message the user did not want.
    """
    obj = drv.make()

    assert drv.turn_done_is_set(obj) is True
    assert drv.turn_active(obj) is False
    assert drv.unfinished(obj) is False


@_PARAM
def test_started_turn_reads_active(drv: _Driver) -> None:
    """A real turn is treated as idle, so its transport is swapped underneath it.

    Idle-means-done must not be so eager that a started turn also reads done:
    a turn that reads idle can be cancelled, killed, or have its model changed
    mid-stream, which loses the user's answer.
    """
    obj = drv.make()

    drv.begin_turn(obj)

    assert drv.turn_active(obj) is True
    assert drv.unfinished(obj) is True


@_PARAM
def test_turn_on_dead_transport_reads_neither_active_nor_unfinished(drv: _Driver) -> None:
    """Shutdown waits on a turn whose process is already gone, then times out.

    Both reads gate on transport liveness. Without that, a turn interrupted by a
    crashed backend stays "in flight" forever: cancel fires at a corpse instead
    of answering no_turn, and the shutdown drain blocks on a turn that can never
    reach its done boundary.
    """
    obj = drv.make()
    drv.begin_turn(obj)

    drv.set_transport_alive(obj, False)

    assert drv.turn_active(obj) is False
    assert drv.unfinished(obj) is False


# ── Cancel ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@_PARAM
async def test_cancel_does_not_complete_the_turn(drv: _Driver) -> None:
    """A cancelled turn's transport is killed while the backend still holds it.

    The turn stays open until the backend acks; marking it done would let the drain
    kill the transport with the session lock held (an empty response next start).
    """
    obj = drv.make()
    drv.begin_turn(obj)

    await drv.cancel(obj)

    assert drv.turn_done_is_set(obj) is False
    assert drv.unfinished(obj) is True
    # A cancelled turn does not read as "active": a caller that would re-send a
    # cancel, or gate a model switch on it, must see it as spoken for.
    assert drv.turn_active(obj) is False


@pytest.mark.asyncio
@_PARAM
async def test_cancel_with_no_turn_in_flight_is_a_no_op(drv: _Driver) -> None:
    """An idle session starts reporting a turn nobody will ever finish.

    Touching the turn-done Event on an idle session invents a turn no ack closes, so
    teardown hangs to its timeout and the 409 gate blocks model switches.
    """
    obj = drv.make()

    await drv.cancel(obj)

    assert drv.turn_done_is_set(obj) is True
    assert drv.turn_active(obj) is False
    assert drv.unfinished(obj) is False


@pytest.mark.asyncio
@_PARAM
async def test_second_cancel_sees_an_inactive_turn(drv: _Driver) -> None:
    """A held stop button re-cancels, and every extra cancel re-arms the grace.

    Both drivers factor cancel state into the active read so a repeat cancel
    early-returns instead of sending another notification and pushing the
    unresponsive-cancel deadline out, which is what makes a wedged turn
    un-recoverable.
    """
    obj = drv.make()
    drv.begin_turn(obj)

    await drv.cancel(obj)
    assert drv.turn_active(obj) is False

    await drv.cancel(obj)

    assert drv.turn_active(obj) is False
    assert drv.unfinished(obj) is True


# ── Draining ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@_PARAM
async def test_wait_turn_done_returns_at_once_when_idle(drv: _Driver) -> None:
    """Every idle teardown pays the full drain timeout before it can proceed.

    Teardown drains sessions one by one. The return shape differs on purpose and is
    not asserted: AcpClient returns the stop reason, AcpSessionHandle a bool.
    """
    obj = drv.make()

    await finished(obj.wait_turn_done(0.05), "the idle wait_turn_done")

    assert drv.turn_done_is_set(obj) is True


# ── Reset / respawn ───────────────────────────────────────────────────────────


def test_client_reset_preserves_an_in_flight_turn() -> None:
    """A respawn reports the killed turn as an answer, and kills its replacement.

    Rebuilding the turn-done Event on reset marks the respawned turn done and orphans
    anyone waiting on the old Event.
    """
    client = _make_client()
    client._process = _client_process(alive=False)
    client._turn_done.clear()  # prompt entry: the turn has begun
    event_before = client._turn_done

    client._reset_state()  # the dead-process respawn branch
    client._process = _client_process(alive=True)

    assert client._turn_done is event_before
    assert client._turn_done.is_set() is False
    assert client.has_active_turn() is True
    assert client.has_unfinished_turn() is True


def test_client_reset_preserves_idle() -> None:
    """A failed startup leaves the client looking busy, blocking model switches.

    The same rule in the other direction: a reset outside any turn must leave
    idle reading as idle, or a client that failed to start is stuck at the 409
    gate with no turn to finish and clear it.
    """
    client = _make_client()
    client._process = _client_process(alive=False)

    client._reset_state()
    client._process = _client_process(alive=True)

    assert client._turn_done.is_set() is True
    assert client.has_active_turn() is False


def test_handle_has_no_reset_path_to_guard() -> None:
    """A reset added to the shared-runtime driver silently skips this guard.

    A handle shares its runtime, so it is replaced rather than reset. If a reset is
    added, extend the preserve-the-turn invariant above to cover it.
    """
    assert not hasattr(AcpSessionHandle, "_reset_state")
