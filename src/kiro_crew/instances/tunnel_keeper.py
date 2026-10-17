"""Keep one SSH port-forward alive for the desktop app's client-only mode.

In client-only mode the desktop app runs no local gateway: it loads a remote
crew through a local port that an SSH forward carries to the crew's machine.
The forward dies whenever the laptop sleeps, and nothing in the app rebuilt it,
so every wake ended at the "no gateway is answering" dialog.

Remote Crew already supervises exactly this kind of forward inside the gateway.
This module runs that same supervisor -- :class:`_SshTunnel`, with its readiness
wait, zombie probe and exit classification -- and the same backoff, without a
gateway around it. The instance registry, token minting and hop leases the
manager layers on top are deliberately absent: the desktop app fetches its own
token over SSH, and the forward it needs is a single fixed port pair.

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

#: The id the supervisor logs under; there is only ever one forward here.
TUNNEL_ID = "desktop-client"

#: Emitted on stdout, one line per state change, so the parent can log them.
_CONNECTED = "tunnel: connected"
_RETRYING = "tunnel: down ({error}); retrying in {delay:.0f}s"


def _valid_port(value: int) -> bool:
    return 1 <= value <= 65535


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
) -> None:
    """Hold ``127.0.0.1:local_port -> ssh_host:remote_port`` open until *stop* is set.

    A forward that fails to come up, or exits after it did, is rebuilt after the
    same capped exponential backoff Remote Crew uses. A forward that connects
    resets that backoff, so a laptop waking from sleep reconnects within seconds
    rather than inheriting the delay of an earlier outage.
    """
    attempt = 0
    while not stop.is_set():
        exited = asyncio.Event()
        tunnel = make_tunnel(
            TUNNEL_ID,
            ssh_host,
            local_port,
            remote_port,
            on_exit=lambda _id: exited.set(),
        )
        if await _start_unless_stopped(tunnel, stop):
            attempt = 0
            emit(_CONNECTED)
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
        emit(_RETRYING.format(error=error, delay=delay))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
    # Every other way out of the loop leaves no child behind: a failed start()
    # has already terminated its own, and the backoff runs with none.


def _watch_stdin(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Set *stop* when stdin closes -- the parent exited, however it exited."""

    def _read() -> None:
        with contextlib.suppress(Exception):
            while sys.stdin.buffer.read(4096):
                pass
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
    # Always on: a forward outliving the process that wanted it is the harm
    # here, whoever the parent is.
    _watch_stdin(loop, stop)
    await keep_tunnel(
        ssh_host,
        local_port,
        remote_port,
        stop=stop,
        emit=lambda line: print(line, flush=True),
    )


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
