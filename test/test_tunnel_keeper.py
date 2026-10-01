"""The desktop client-only tunnel keeper rebuilds its forward and stops cleanly."""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.instances import tunnel_keeper
from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus


class _FakeTunnel:
    """Stands in for _SshTunnel: scripted start() results, exit on demand."""

    instances: list["_FakeTunnel"] = []
    script: list[bool] = []

    def __init__(self, instance_id, ssh_host, local_port, remote_port, *, on_exit):
        self.args = (ssh_host, local_port, remote_port)
        self.on_exit = on_exit
        self.status = TunnelStatus(instance_id=instance_id)
        self.stopped = False
        _FakeTunnel.instances.append(self)

    async def start(self) -> bool:
        ok = _FakeTunnel.script.pop(0) if _FakeTunnel.script else True
        self.status.state = TunnelState.CONNECTED if ok else TunnelState.ERROR
        self.status.error = "" if ok else "connection refused"
        return ok

    def drop(self) -> None:
        self.status.state = TunnelState.ERROR
        self.status.error = "exited"
        self.on_exit("x")

    async def stop(self) -> None:
        self.stopped = True
        self.status.state = TunnelState.STOPPED


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _FakeTunnel.instances = []
    _FakeTunnel.script = []
    # Real backoff is seconds long; these tests only care that it is applied.
    monkeypatch.setattr(tunnel_keeper, "_recover_backoff_secs", lambda attempt: 0.0)


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.01)


def test_a_dropped_forward_is_rebuilt_then_stopped_cleanly():
    async def scenario():
        stop = asyncio.Event()
        lines: list[str] = []
        task = asyncio.create_task(
            tunnel_keeper.keep_tunnel(
                "devbox", 5477, 5476, stop=stop, make_tunnel=_FakeTunnel, emit=lines.append
            )
        )
        await _until(lambda: len(_FakeTunnel.instances) == 1)
        _FakeTunnel.instances[0].drop()  # the laptop slept; ssh died
        # Stop only once the rebuilt forward has reported in: stop now wins a
        # race against a start() still in flight, by design.
        await _until(lambda: lines.count("tunnel: connected") == 2)
        stop.set()
        await asyncio.wait_for(task, 2)
        return lines

    lines = asyncio.run(scenario())
    first, second = _FakeTunnel.instances
    assert first.args == ("devbox", 5477, 5476)
    assert second.args == first.args
    assert second.stopped, "the live forward must be torn down on stop"
    assert lines[0] == "tunnel: connected"
    assert lines[1].startswith("tunnel: down (exited)")
    assert lines[2] == "tunnel: connected"


def test_a_failed_start_backs_off_and_retries(monkeypatch):
    attempts: list[int] = []
    monkeypatch.setattr(
        tunnel_keeper, "_recover_backoff_secs", lambda attempt: attempts.append(attempt) or 0.0
    )
    _FakeTunnel.script = [False, False, True]

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            tunnel_keeper.keep_tunnel(
                "devbox", 5477, 5476, stop=stop, make_tunnel=_FakeTunnel, emit=lambda _l: None
            )
        )
        await _until(lambda: len(_FakeTunnel.instances) == 3)
        await _until(lambda: _FakeTunnel.instances[-1].status.state == TunnelState.CONNECTED)
        _FakeTunnel.instances[-1].drop()
        await _until(lambda: len(_FakeTunnel.instances) == 4)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    # Two failures escalate the backoff; a connect resets it to the first step.
    assert attempts == [1, 2, 1]


def test_stop_during_backoff_returns_without_another_attempt(monkeypatch):
    monkeypatch.setattr(tunnel_keeper, "_recover_backoff_secs", lambda attempt: 60.0)
    _FakeTunnel.script = [False]

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            tunnel_keeper.keep_tunnel(
                "devbox", 5477, 5476, stop=stop, make_tunnel=_FakeTunnel, emit=lambda _l: None
            )
        )
        await _until(lambda: len(_FakeTunnel.instances) == 1)
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert len(_FakeTunnel.instances) == 1


def test_stop_while_connecting_tears_down_without_waiting_for_the_connect(monkeypatch):
    """App exit mid-connect must not wait out ssh's budget or orphan its child."""
    started = asyncio.Event()

    class _SlowTunnel(_FakeTunnel):
        async def start(self) -> bool:
            started.set()
            await asyncio.sleep(60)  # an ssh connect that has not answered yet
            return True

    lines: list[str] = []

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            tunnel_keeper.keep_tunnel(
                "devbox", 5477, 5476, stop=stop, make_tunnel=_SlowTunnel, emit=lines.append
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    (only,) = _FakeTunnel.instances
    assert only.stopped, "a forward stopped mid-connect must be torn down"
    assert lines == [], "nothing may report a connection or a retry after stop"


@pytest.mark.parametrize(
    "host,local,remote",
    [("-oProxyCommand=evil", 5477, 5476), ("devbox", 0, 5476), ("devbox", 5477, 70000)],
)
def test_run_refuses_unsafe_coordinates(host, local, remote, capsys):
    assert tunnel_keeper.run(host, local, remote) == 2
    assert "Refusing tunnel" in capsys.readouterr().err


def test_run_passes_validated_coordinates_to_the_loop(monkeypatch):
    seen: list[tuple] = []

    async def fake_main(host, local, remote):
        seen.append((host, local, remote))

    monkeypatch.setattr(tunnel_keeper, "_main", fake_main)
    assert tunnel_keeper.run("devbox", 5477, 5476) == 0
    assert seen == [("devbox", 5477, 5476)]


class _ClosedStdin:
    """A stdin whose parent has already gone: the first read is EOF."""

    class buffer:  # noqa: N801 - mirrors sys.stdin.buffer
        @staticmethod
        def read(_size):
            return b""


def test_the_stdin_lifeline_stops_the_keeper_when_the_parent_goes(monkeypatch):
    """EOF on stdin must end the loop: that is how the app's exit reaches it."""
    monkeypatch.setattr(tunnel_keeper.sys, "stdin", _ClosedStdin)
    stopped: list[bool] = []

    async def fake_keep(host, local, remote, *, stop, emit):
        emit("tunnel: connected")
        await asyncio.wait_for(stop.wait(), 2)
        stopped.append(stop.is_set())

    monkeypatch.setattr(tunnel_keeper, "keep_tunnel", fake_keep)
    asyncio.run(tunnel_keeper._main("devbox", 5477, 5476))
    assert stopped == [True]
