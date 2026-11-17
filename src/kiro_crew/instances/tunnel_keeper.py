"""Keep the desktop app's client-only SSH forwards alive.

In client-only mode the desktop app runs no local gateway: it loads a remote
crew through a local port that an SSH forward carries to the crew's machine.
The forward dies whenever the laptop sleeps, and nothing in the app rebuilt it,
so every wake ended at the "no gateway is answering" dialog.

Remote Crew already supervises exactly this kind of forward inside the gateway.
This module runs that same supervisor -- :class:`_SshTunnel`, with its readiness
wait, zombie probe and exit classification -- and the same backoff, without a
gateway around it. The instance registry, token minting and hop leases the
manager layers on top are deliberately absent: the desktop app fetches its own
token over SSH.

The dashboard forward is one fixed port pair. The Remote Crew panes that
dashboard shows are a second kind: each pane loads ``localhost:<port>``, where
``<port>`` is a forward the remote gateway opened on ITS machine, so the app
asks for each one over stdin (:class:`PaneForwards`) and it is carried with the
same number on both ends.

Exits 0 when asked to stop (SIGTERM, SIGINT, or EOF on stdin, which the parent
holds open as a lifeline), and 2 when the arguments are refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import threading
from typing import Callable

from kiro_crew.instances.ssh_tunnel_manager import _recover_backoff_secs, _SshTunnel
from kiro_crew.instances.validation import SshValidationError, validate_ssh_host

logger = logging.getLogger(__name__)

#: The id the dashboard forward's supervisor logs under.
TUNNEL_ID = "desktop-client"

#: How many pane forwards the keeper holds at once. Each is its own ssh child;
#: a ``forward`` past this is refused rather than spawning another.
MAX_PANE_FORWARDS = 32

#: Emitted on stdout, one line per state change, so the parent can log them.
#: The dashboard forward's lines start ``tunnel:``; a pane forward's start
#: ``tunnel: pane <port>``, which the app reads to learn the pane can load.
_DASHBOARD_LABEL = "tunnel:"
_CONNECTED = "{label} connected"
_RETRYING = "{label} down ({error}); retrying in {delay:.0f}s"
_PANE_REFUSED = "{label} refused ({reason})"

#: A control line on stdin is ``<verb> <port>``, one space apart.
_CONTROL_VERBS = frozenset({"forward", "drop"})
#: ASCII only: ``str.isdigit`` also accepts superscripts and other scripts' digits.
_ASCII_DIGITS = frozenset("0123456789")
_PORT_MAX_DIGITS = 5
#: A control line is a few bytes; a longer read is split and matches nothing.
_CONTROL_LINE_MAX_BYTES = 64


def _valid_port(value: int) -> bool:
    return 1 <= value <= 65535


def _parse_control_line(line: str) -> tuple[str, int] | None:
    """``(verb, port)`` for a ``forward <port>`` or ``drop <port>`` line, else None."""
    verb, space, digits = line.strip().partition(" ")
    if not space or verb not in _CONTROL_VERBS:
        return None
    if not 1 <= len(digits) <= _PORT_MAX_DIGITS or not _ASCII_DIGITS.issuperset(digits):
        return None
    return verb, int(digits)


def _pane_label(port: int) -> str:
    return f"tunnel: pane {port}"


async def _start_unless_stopped(tunnel: _SshTunnel, stop: asyncio.Event) -> bool:
    """Run ``tunnel.start()`` but give up the moment *stop* is set.

    A connect can take the whole ssh connect budget; the app quitting in that
    window must not wait it out, nor leave the ssh child it spawned running.
    Returns False when stop won; the caller then tears the tunnel down.
    """
    starting = asyncio.create_task(tunnel.start())
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait([starting, stopping], return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()
    if starting.done() and not stop.is_set():
        return bool(starting.result())
    starting.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await starting
    return False


async def keep_tunnel(
    ssh_host: str,
    local_port: int,
    remote_port: int,
    *,
    stop: asyncio.Event,
    make_tunnel: Callable[..., _SshTunnel] = _SshTunnel,
    emit: Callable[[str], None] = print,
    tunnel_id: str = TUNNEL_ID,
    label: str = _DASHBOARD_LABEL,
) -> None:
    """Hold ``127.0.0.1:local_port -> ssh_host:remote_port`` open until *stop* is set.

    A forward that fails to come up, or exits after it did, is rebuilt after the
    same capped exponential backoff Remote Crew uses. A forward that connects
    resets that backoff, so a laptop waking from sleep reconnects within seconds
    rather than inheriting the delay of an earlier outage. *label* starts every
    line emitted, so the dashboard forward and each pane forward report apart.
    """
    attempt = 0
    while not stop.is_set():
        exited = asyncio.Event()
        tunnel = make_tunnel(
            tunnel_id,
            ssh_host,
            local_port,
            remote_port,
            on_exit=lambda _id: exited.set(),
        )
        if await _start_unless_stopped(tunnel, stop):
            attempt = 0
            emit(_CONNECTED.format(label=label))
            waiters = [asyncio.create_task(exited.wait()), asyncio.create_task(stop.wait())]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
            if stop.is_set():
                await tunnel.stop()
                return
        if stop.is_set():
            # Asked to stop while the forward was still connecting: whatever
            # start() had already spawned is torn down, never left behind.
            await tunnel.stop()
            return
        attempt += 1
        delay = _recover_backoff_secs(attempt)
        error = tunnel.status.error or tunnel.status.state.value
        emit(_RETRYING.format(label=label, error=error, delay=delay))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
    # Every other way out of the loop leaves no child behind: a failed start()
    # has already terminated its own, and the backoff runs with none.


class PaneForwards:
    """The Remote Crew pane ports forwarded beside the dashboard's own.

    The remote gateway reaches each crew through a forward on a loopback port
    of its own machine, and the pane the dashboard frames loads that port on
    ``localhost``. From the laptop nothing answers there unless it is forwarded
    too, so each requested port gets its own :func:`keep_tunnel`, with the same
    number on both ends and the same supervision and backoff as the dashboard
    forward. Driven by control lines on stdin (:meth:`command`); a port already
    held is left as it is, and :meth:`drop` tears one down.
    """

    def __init__(
        self,
        ssh_host: str,
        dashboard_port: int,
        *,
        make_tunnel: Callable[..., _SshTunnel] = _SshTunnel,
        emit: Callable[[str], None] = print,
        limit: int = MAX_PANE_FORWARDS,
    ) -> None:
        self._host = ssh_host
        self._dashboard_port = dashboard_port
        self._make_tunnel = make_tunnel
        self._emit = emit
        self._limit = limit
        self._held: dict[int, tuple[asyncio.Task[None], asyncio.Event]] = {}
        # Dropped forwards still tearing their ssh child down: close() waits
        # for these too, since the app drops a window's ports just before it quits.
        self._draining: set[asyncio.Task[None]] = set()
        # Set by close(): a line still in flight from stdin must not start a
        # forward that nothing would tear down.
        self._closed = False

    @property
    def ports(self) -> list[int]:
        """The pane ports held now, in ascending order."""
        return sorted(self._held)

    def command(self, line: str) -> None:
        """Apply one stdin control line; anything else is reported and ignored."""
        parsed = _parse_control_line(line)
        if parsed is None:
            self._emit("tunnel: ignored a control line that is not forward/drop <port>")
            return
        verb, port = parsed
        if verb == "forward":
            self.forward(port)
        else:
            self.drop(port)

    def forward(self, port: int) -> None:
        """Start holding *port* open, unless it is held, refused, or over the limit."""
        if self._closed or port in self._held:
            return
        label = _pane_label(port)
        # The dashboard's own port is already bound by its forward, so a pane
        # forward there could only fail its bind; refuse it up front.
        if not _valid_port(port) or port == self._dashboard_port:
            self._emit(_PANE_REFUSED.format(label=label, reason="not a pane port"))
            return
        if len(self._held) >= self._limit:
            reason = f"already holding {self._limit} pane forwards"
            self._emit(_PANE_REFUSED.format(label=label, reason=reason))
            return
        stop = asyncio.Event()
        task = asyncio.create_task(
            keep_tunnel(
                self._host,
                port,
                port,
                stop=stop,
                make_tunnel=self._make_tunnel,
                emit=self._emit,
                tunnel_id=f"pane-{port}",
                label=label,
            )
        )
        self._held[port] = (task, stop)

    def drop(self, port: int) -> None:
        """Stop holding *port*; its forward is torn down by its own loop."""
        held = self._held.pop(port, None)
        if held is None:
            return
        task, stop = held
        stop.set()
        self._draining.add(task)
        task.add_done_callback(self._draining.discard)

    async def close(self) -> None:
        """Tear every pane forward down and wait until each ssh child is gone."""
        self._closed = True
        held = list(self._held.values())
        self._held.clear()
        for _task, stop in held:
            stop.set()
        tasks = [task for task, _stop in held] + list(self._draining)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _watch_stdin(
    loop: asyncio.AbstractEventLoop,
    stop: asyncio.Event,
    on_line: Callable[[str], None] | None = None,
) -> None:
    """Hand each stdin line to *on_line*; set *stop* when stdin closes.

    EOF means the parent exited, however it exited.
    """

    def _read() -> None:
        with contextlib.suppress(Exception):
            while raw := sys.stdin.buffer.readline(_CONTROL_LINE_MAX_BYTES):
                if on_line is not None:
                    loop.call_soon_threadsafe(on_line, raw.decode("ascii", "replace"))
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=_read, name="tunnel-keeper-stdin", daemon=True).start()


async def _main(ssh_host: str, local_port: int, remote_port: int) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows has no loop signal handlers; there the stdin lifeline and a
        # process kill are the only ways to stop, which is why the app holds it.
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, stop.set)

    def emit(line: str) -> None:
        print(line, flush=True)

    panes = PaneForwards(ssh_host, local_port, emit=emit)
    # Always on: a forward outliving the process that wanted it is the harm
    # here, whoever the parent is.
    _watch_stdin(loop, stop, panes.command)
    try:
        await keep_tunnel(ssh_host, local_port, remote_port, stop=stop, emit=emit)
    finally:
        await panes.close()


def run(ssh_host: str, local_port: int, remote_port: int) -> int:
    """Validate the forward's coordinates and keep it alive; the CLI entry point."""
    try:
        host = validate_ssh_host(ssh_host)
    except SshValidationError as exc:
        print(f"Refusing tunnel: {exc}", file=sys.stderr)
        return 2
    if not (_valid_port(local_port) and _valid_port(remote_port)):
        print("Refusing tunnel: ports must be between 1 and 65535", file=sys.stderr)
        return 2
    asyncio.run(_main(host, local_port, remote_port))
    return 0
