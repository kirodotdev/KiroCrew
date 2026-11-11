"""The desktop client-only tunnel keeper rebuilds its forward and stops cleanly."""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.instances import tunnel_keeper
from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus
from kiro_crew.testing.wait import async_wait_until, await_until


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


async def _until(predicate) -> None:
    await async_wait_until(predicate)


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
        await await_until(task)
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
        await await_until(task)

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
        await await_until(task)

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
        await await_until(started)
        stop.set()
        await await_until(task)

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
        def readline(_size=-1):
            return b""


class _ScriptedStdin:
    """A stdin carrying the parent's control lines, then EOF as the parent exits."""

    def __init__(self, *lines: bytes) -> None:
        self.buffer = self
        self._lines = list(lines)

    def readline(self, _size: int = -1) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def read(self, _size: int = -1) -> bytes:
        chunk, self._lines = b"".join(self._lines), []
        return chunk


def test_the_stdin_lifeline_stops_the_keeper_when_the_parent_goes(monkeypatch):
    """EOF on stdin must end the loop: that is how the app's exit reaches it."""
    monkeypatch.setattr(tunnel_keeper.sys, "stdin", _ClosedStdin)
    stopped: list[bool] = []

    async def fake_keep(host, local, remote, *, stop, emit):
        emit("tunnel: connected")
        await await_until(stop)
        stopped.append(stop.is_set())

    monkeypatch.setattr(tunnel_keeper, "keep_tunnel", fake_keep)
    asyncio.run(tunnel_keeper._main("devbox", 5477, 5476))
    assert stopped == [True]


def test_a_pane_port_asked_for_on_stdin_is_forwarded_beside_the_dashboard(monkeypatch):
    """A Remote Crew pane loads ``localhost:<port>``: the keeper must carry that port too.

    The port is the remote gateway's own forward to the crew, so from the
    laptop nothing answers on it unless the keeper forwards it, with the same
    number on both ends. The parent exits right after asking, and the pane
    forward must go with it.
    """
    monkeypatch.setattr(tunnel_keeper.sys, "stdin", _ScriptedStdin(b"forward 7779\n"))
    calls: list[tuple] = []

    async def fake_keep(host, local, remote, *, stop, emit, **kwargs):
        calls.append((host, local, remote, kwargs.get("label", "tunnel:")))
        await await_until(stop)
        calls.append(("stopped", local))

    monkeypatch.setattr(tunnel_keeper, "keep_tunnel", fake_keep)
    asyncio.run(tunnel_keeper._main("devbox", 5477, 5476))
    assert ("devbox", 5477, 5476, "tunnel:") in calls
    assert ("devbox", 7779, 7779, "tunnel: pane 7779") in calls
    assert ("stopped", 7779) in calls, "a pane forward must not outlive the parent"


def test_pane_forwards_hold_each_port_until_it_is_dropped():
    async def scenario():
        lines: list[str] = []
        panes = tunnel_keeper.PaneForwards(
            "devbox", 5477, make_tunnel=_FakeTunnel, emit=lines.append
        )
        panes.command("forward 7779\n")
        panes.command("forward 7780\n")
        panes.command("forward 7779\n")  # already held: no second forward
        await _until(lambda: len(lines) == 2)
        assert panes.ports == [7779, 7780]
        panes.command("drop 7779\n")
        (first,) = [t for t in _FakeTunnel.instances if t.args[1] == 7779]
        await _until(lambda: first.stopped)
        assert panes.ports == [7780]
        await panes.close()
        return lines

    lines = asyncio.run(scenario())
    assert sorted(t.args for t in _FakeTunnel.instances) == [
        ("devbox", 7779, 7779),
        ("devbox", 7780, 7780),
    ]
    assert all(t.stopped for t in _FakeTunnel.instances)
    assert sorted(lines) == ["tunnel: pane 7779 connected", "tunnel: pane 7780 connected"]


def test_a_dropped_pane_forward_is_rebuilt_and_reports_under_its_port():
    async def scenario():
        lines: list[str] = []
        panes = tunnel_keeper.PaneForwards(
            "devbox", 5477, make_tunnel=_FakeTunnel, emit=lines.append
        )
        panes.forward(7779)
        await _until(lambda: lines == ["tunnel: pane 7779 connected"])
        _FakeTunnel.instances[0].drop()  # the crew's far end went away
        await _until(lambda: lines.count("tunnel: pane 7779 connected") == 2)
        await panes.close()
        return lines

    lines = asyncio.run(scenario())
    assert lines[1].startswith("tunnel: pane 7779 down (exited)")
    assert len(_FakeTunnel.instances) == 2


@pytest.mark.parametrize(
    "line,reason",
    [
        ("forward 5477", "not a pane port"),  # the dashboard forward's own port
        ("forward 0", "not a pane port"),
        ("forward 70000", "not a pane port"),
    ],
)
def test_a_pane_port_the_keeper_cannot_hold_is_refused(line, reason):
    async def scenario():
        lines: list[str] = []
        panes = tunnel_keeper.PaneForwards(
            "devbox", 5477, make_tunnel=_FakeTunnel, emit=lines.append
        )
        panes.command(line)
        await panes.close()
        return lines

    port = line.split()[1]
    assert asyncio.run(scenario()) == [f"tunnel: pane {port} refused ({reason})"]
    assert _FakeTunnel.instances == []


def test_pane_forwards_past_the_limit_are_refused_and_junk_is_ignored():
    async def scenario():
        lines: list[str] = []
        panes = tunnel_keeper.PaneForwards(
            "devbox", 5477, make_tunnel=_FakeTunnel, emit=lines.append, limit=1
        )
        panes.command("forward 7779")
        panes.command("forward 7780")
        junk_lines = (
            "",
            "forward",
            "forward 7781 7782",
            "listen 7783",
            "forward \u00b2",
            "forward 123456",
            "forward  7784",
        )
        for junk in junk_lines:
            panes.command(junk)
        await _until(lambda: "tunnel: pane 7779 connected" in lines)
        await panes.close()
        return lines

    lines = asyncio.run(scenario())
    assert "tunnel: pane 7780 refused (already holding 1 pane forwards)" in lines
    assert [t.args for t in _FakeTunnel.instances] == [("devbox", 7779, 7779)]
    assert lines.count("tunnel: ignored a control line that is not forward/drop <port>") == 7


def test_close_waits_for_a_pane_forward_dropped_just_before_it():
    """App quit closes a window (``drop``) and then the keeper (``close``) at once.

    The dropped forward is still tearing its ssh child down when close() runs,
    and asyncio.run cancels whatever close() did not wait for.
    """

    async def scenario():
        lines: list[str] = []
        panes = tunnel_keeper.PaneForwards(
            "devbox", 5477, make_tunnel=_FakeTunnel, emit=lines.append
        )
        panes.forward(7779)
        await _until(lambda: lines == ["tunnel: pane 7779 connected"])
        panes.drop(7779)
        await panes.close()

    asyncio.run(scenario())
    (only,) = _FakeTunnel.instances
    assert only.stopped, "close() must wait for a forward dropped just before it"


def test_close_tears_down_every_pane_forward_and_starts_no_more():
    async def scenario():
        panes = tunnel_keeper.PaneForwards(
            "devbox", 5477, make_tunnel=_FakeTunnel, emit=lambda _l: None
        )
        panes.forward(7779)
        panes.forward(7780)
        await _until(lambda: len(_FakeTunnel.instances) == 2)
        await panes.close()
        panes.forward(7781)  # a control line still in flight from stdin
        return panes.ports

    assert asyncio.run(scenario()) == []
    assert len(_FakeTunnel.instances) == 2
    assert all(t.stopped for t in _FakeTunnel.instances)
