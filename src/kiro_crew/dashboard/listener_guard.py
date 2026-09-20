"""Keep the gateway's TCP listener alive after a failed ``accept()``.

The defect this guards against is Windows-only and lives in CPython, not in
Kiro Crew. ``asyncio.proactor_events.BaseProactorEventLoop._start_serving``
runs one ``loop()`` callback per completed ``AcceptEx``::

    except OSError as exc:
        if sock.fileno() != -1:
            self.call_exception_handler({
                'message': 'Accept failed on a socket', ...})
            sock.close()          # listener gone, accept never re-armed

One ``OSError`` from the accept completion -- observed as ``[WinError 64]``
(``ERROR_NETNAME_DELETED``) when a tunnelled peer aborts its half-open
connection before the accept completes -- closes the LISTEN
socket and never re-arms it. The event loop keeps running, every already
accepted connection keeps working, the process stays alive, and every new
connection is refused. The POSIX selector loop
(``selector_events._accept_connection``) treats the same error as a
per-connection event and keeps the listener registered, so macOS and Linux
never see this. :func:`kiro_crew.dashboard.server._arm_listener_guard`
therefore arms this guard on Windows only.

Switching Windows to ``WindowsSelectorEventLoopPolicy`` is NOT a fix: the
selector loop has no subprocess support on Windows and the gateway spawns its
runtimes with ``asyncio.create_subprocess_exec``. This module instead makes
the listener itself recoverable and, when it cannot be recovered, turns the
"alive but not listening" state into a non-zero exit so the supervisor
(systemd / launchd / a Windows scheduled task with restart-on-failure)
relaunches the gateway.

Two detectors feed one recovery path:

* **Loop exception hook.** The guard wraps the running loop's exception
  handler. When a context carries the proactor's ``Accept failed on a socket``
  message it schedules an immediate check; the wrapped handler (crash_guard's
  breadcrumb writer) still runs, so the traceback still lands in crash.log.
* **Periodic loopback HTTP self-probe.** Every ``interval`` seconds the guard
  opens a TCP connection to the address the listener actually bound and
  reads the status line of ``GET /api/live``. Any HTTP status line counts as
  serving; a refused connection, a timeout or a non-HTTP answer does not. A
  failed probe is re-confirmed after ``confirm_delay`` before acting. This is
  the health check that a process-alive or has-connections check cannot be:
  the failure mode leaves the process alive and its accepted connections
  ESTABLISHED.

Recovery stops the dead ``TCPSite`` (``server.close()`` + unregister -- the
accepted connections are untouched), creates a fresh ``TCPSite`` on the same
host and the port that was really bound, and starts it. Rebinding succeeds on
Windows with the old accepted connections still open (verified; a LISTEN
socket has no TIME_WAIT). Bind failures retry with exponential backoff up to
``max_attempts``; after that the guard records :data:`LISTENER_LOST_EXIT_CODE`
and sets the process-wide shutdown event so the gateway exits non-zero.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol

from aiohttp import web

logger = logging.getLogger(__name__)

#: The literal message CPython's proactor loop reports when an accept
#: completion raised ``OSError`` (``asyncio/proactor_events.py``,
#: ``BaseProactorEventLoop._start_serving.loop``). Matched verbatim: it is the
#: only signal the loop emits before it closes the listening socket.
ACCEPT_FAILED_MESSAGE = "Accept failed on a socket"

#: Process exit status when the listener is lost and cannot be rebound. Non-zero
#: on purpose: a supervisor with restart-on-failure semantics relaunches the
#: process only when it did NOT exit 0. 69 is ``EX_UNAVAILABLE`` from
#: ``sysexits.h`` ("service unavailable"), distinct from the stale-asset
#: watchdog's ``EX_TEMPFAIL`` (75) so a post-mortem can tell the two apart.
LISTENER_LOST_EXIT_CODE = 69

#: Seconds between periodic self-probes. The exception hook reacts instantly to
#: the CPython path above; the probe covers the one case that hook cannot see --
#: another component calling ``loop.set_exception_handler`` after the guard
#: armed displaces the hook, and then only a probe observes the closed
#: listener. So it can be slow.
DEFAULT_PROBE_INTERVAL_SECS = 60.0
#: Seconds before a failed probe is re-confirmed. A single probe can lose a race
#: against a momentarily saturated accept queue; a dead listener fails twice.
DEFAULT_CONFIRM_DELAY_SECS = 2.0
#: Per-connect / per-read budget of one probe.
DEFAULT_PROBE_TIMEOUT_SECS = 5.0
#: Rebind attempts per incident before giving up and exiting.
DEFAULT_MAX_REBIND_ATTEMPTS = 5
#: First backoff delay; doubles per failed attempt, capped at
#: :data:`DEFAULT_MAX_BACKOFF_SECS`.
DEFAULT_BACKOFF_BASE_SECS = 0.5
DEFAULT_MAX_BACKOFF_SECS = 8.0

_PROBE_PATH = "/api/live"


class _ShutdownSignal(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    async def wait(self) -> bool: ...


def probe_target(sockname: Any) -> tuple[str, int] | None:
    """Loopback-reachable ``(host, port)`` for a listener bound at *sockname*.

    A wildcard bind (``0.0.0.0`` / ``::``) is probed on the matching loopback
    address; a specific address is probed as bound. Returns ``None`` for a
    sockname that is not a TCP ``(host, port[, ...])`` tuple.
    """
    if not isinstance(sockname, (tuple, list)) or len(sockname) < 2:
        return None
    host, port = sockname[0], sockname[1]
    if not isinstance(host, str) or not isinstance(port, int):
        return None
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    return host, port


async def http_probe(host: str, port: int, *, timeout: float) -> bool:
    """True when something at ``host:port`` answers ``GET /api/live`` with an HTTP status line.

    The status code is irrelevant -- a 503 (not ready) or 401 still proves the
    HTTP server accepted the connection and is serving. Only a refused or timed
    out connect, or a non-HTTP first line, is a failure. Never raises.
    """
    writer: asyncio.StreamWriter | None = None
    try:
        reader, stream = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer = stream
        request = (
            f"GET {_PROBE_PATH} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        stream.write(request)
        await asyncio.wait_for(stream.drain(), timeout)
        line = await asyncio.wait_for(reader.readline(), timeout)
        return line.startswith(b"HTTP/1.")
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return False
    except Exception:  # a probe must never take the guard down with it
        logger.debug("listener self-probe failed unexpectedly", exc_info=True)
        return False
    finally:
        if writer is not None:
            writer.close()


def _listening_sockets(site: web.TCPSite) -> tuple[Any, ...]:
    """The LISTEN sockets behind *site*'s asyncio ``Server`` (``()`` when none).

    ``asyncio.Server.sockets`` is a tuple of ``TransportSocket`` wrappers; a
    closed listener stays in it with ``fileno() == -1``. Typed through
    ``AbstractServer`` by aiohttp, hence the ``getattr``.
    """
    server = site._server
    if server is None:
        return ()
    return tuple(getattr(server, "sockets", None) or ())


class ListenerGuard:
    """Watch one ``TCPSite`` and rebind it when its listener dies.

    Construct with the *runner* and the *site* that :func:`_start_site` just
    started, then :meth:`arm` on the running loop. :meth:`stop` on shutdown.
    """

    def __init__(
        self,
        runner: web.BaseRunner,
        site: web.TCPSite,
        shutdown_event: _ShutdownSignal,
        *,
        interval: float = DEFAULT_PROBE_INTERVAL_SECS,
        confirm_delay: float = DEFAULT_CONFIRM_DELAY_SECS,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT_SECS,
        max_attempts: int = DEFAULT_MAX_REBIND_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECS,
        max_backoff: float = DEFAULT_MAX_BACKOFF_SECS,
        probe: Callable[..., Any] | None = None,
    ) -> None:
        self._runner = runner
        self._site = site
        self._shutdown_event = shutdown_event
        self._interval = interval
        self._confirm_delay = confirm_delay
        self._probe_timeout = probe_timeout
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._probe = probe if probe is not None else http_probe
        # Bind parameters are captured from the live site so the rebind lands on
        # the SAME host and the port that was REALLY bound (``--port auto`` binds
        # 0 and reads the OS-assigned port back; rebinding 0 would move it).
        self._host = site._host
        self._port = self._bound_port(site) or site._port
        self._backlog = site._backlog
        self._reuse_address = site._reuse_address
        self._reuse_port = site._reuse_port
        self._ssl_context = site._ssl_context
        self._loop: asyncio.AbstractEventLoop | None = None
        self._previous_handler: Callable[..., Any] | None = None
        self._probe_task: asyncio.Task[None] | None = None
        self._recover_task: asyncio.Task[bool] | None = None
        self._stopped = False
        self._exit_code = 0
        self.recoveries = 0

    # ── public surface ───────────────────────────────────────────────────

    @property
    def site(self) -> web.TCPSite:
        """The site currently serving (replaced on every successful rebind)."""
        return self._site

    @property
    def port(self) -> int:
        return self._port

    @property
    def exit_code(self) -> int:
        """:data:`LISTENER_LOST_EXIT_CODE` once the guard gave up, else ``0``."""
        return self._exit_code

    def arm(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Hook the loop's exception handler and start the periodic self-probe."""
        loop = loop or asyncio.get_running_loop()
        self._loop = loop
        self._previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._on_loop_exception)
        self._probe_task = loop.create_task(self._probe_loop(), name="listener-guard-probe")

    def stop(self) -> None:
        """Detach from the loop; safe to call twice. Never rebinds afterwards."""
        self._stopped = True
        if self._probe_task is not None:
            self._probe_task.cancel()
            self._probe_task = None
        loop = self._loop
        if loop is not None and loop.get_exception_handler() == self._on_loop_exception:
            loop.set_exception_handler(self._previous_handler)
        self._loop = None

    def listener_open(self) -> bool:
        """Whether the current site still holds an open LISTEN socket.

        This is the state the CPython bug leaves behind: the asyncio ``Server``
        object survives but its socket's ``fileno()`` is ``-1``.
        """
        sockets = _listening_sockets(self._site)
        if not sockets:
            return False
        return all(sock.fileno() != -1 for sock in sockets)

    async def check_now(self, reason: str) -> bool:
        """Verify the listener and recover if it is dead. Returns True when serving."""
        if self._stopped or self._shutdown_event.is_set():
            return True
        if self.listener_open():
            return True
        return await self._recover(reason)

    # ── detectors ─────────────────────────────────────────────────────────

    def _on_loop_exception(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        try:
            if context.get("message") == ACCEPT_FAILED_MESSAGE and not self._stopped:
                logger.warning(
                    "asyncio reported %r (%s); verifying the gateway listener",
                    ACCEPT_FAILED_MESSAGE,
                    context.get("exception"),
                )
                # call_soon, not inline: the proactor closes the socket right
                # AFTER the handler returns, so the check must run one tick
                # later to see the closed fileno rather than a still-open one.
                loop.call_soon(self._schedule_check, "accept failed on the listening socket")
        finally:
            previous = self._previous_handler
            if previous is not None:
                previous(loop, context)
            else:
                loop.default_exception_handler(context)

    def _schedule_check(self, reason: str) -> None:
        if self._stopped or self._loop is None:
            return
        if self._recover_task is not None and not self._recover_task.done():
            return
        self._recover_task = self._loop.create_task(
            self.check_now(reason), name="listener-guard-recover"
        )

    async def _probe_loop(self) -> None:
        while not self._stopped and not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._interval)
                return
            except asyncio.TimeoutError:
                pass
            if self._stopped:
                return
            if self._recover_task is not None and not self._recover_task.done():
                continue  # a recovery is already in flight; do not race it
            if not self.listener_open():
                await self._recover("listening socket is closed")
                continue
            if await self._probe_once():
                continue
            # Re-confirm before acting: one probe can lose a race against a
            # saturated accept queue; a dead listener fails again.
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._confirm_delay)
                return
            except asyncio.TimeoutError:
                pass
            if self._stopped or await self._probe_once():
                continue
            await self._recover("loopback HTTP self-probe failed twice")

    async def _probe_once(self) -> bool:
        target = self._current_target()
        if target is None:
            return False
        host, port = target
        return bool(await self._probe(host, port, timeout=self._probe_timeout))

    def _current_target(self) -> tuple[str, int] | None:
        sockets = _listening_sockets(self._site)
        if not sockets:
            return None
        try:
            return probe_target(sockets[0].getsockname())
        except OSError:
            return None

    # ── recovery ──────────────────────────────────────────────────────────

    async def _recover(self, reason: str) -> bool:
        if self._stopped or self._shutdown_event.is_set():
            return False
        logger.critical(
            "Gateway listener on %s:%d is not accepting connections (%s); "
            "the process is alive but unreachable -- rebinding",
            self._host or "*",
            self._port,
            reason,
        )
        for attempt in range(1, self._max_attempts + 1):
            if self._stopped or self._shutdown_event.is_set():
                return False
            await self._stop_site(self._site)
            new_site = self._new_site()
            try:
                await new_site.start()
            except OSError as exc:
                await self._stop_site(new_site)
                delay = min(self._backoff_base * (2 ** (attempt - 1)), self._max_backoff)
                logger.error(
                    "Listener rebind attempt %d/%d failed: %s -- retrying in %.1fs",
                    attempt,
                    self._max_attempts,
                    exc,
                    delay,
                )
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=delay)
                    return False
                except asyncio.TimeoutError:
                    continue
            self._site = new_site
            self.recoveries += 1
            logger.warning(
                "Gateway listener rebound on %s:%d after %d attempt(s) "
                "(recovery #%d this process); existing connections were kept",
                self._host or "*",
                self._port,
                attempt,
                self.recoveries,
            )
            return True

        self._exit_code = LISTENER_LOST_EXIT_CODE
        logger.critical(
            "Gateway listener on %s:%d could not be rebound after %d attempts; "
            "exiting with status %d so the supervisor restarts the gateway "
            "instead of leaving it alive and unreachable",
            self._host or "*",
            self._port,
            self._max_attempts,
            LISTENER_LOST_EXIT_CODE,
        )
        self._shutdown_event.set()
        return False

    def _new_site(self) -> web.TCPSite:
        # shutdown_timeout is deliberately not forwarded: aiohttp owns it on the
        # runner, and the runner is shared with the site being replaced.
        return web.TCPSite(
            self._runner,
            self._host,
            self._port,
            ssl_context=self._ssl_context,
            backlog=self._backlog,
            reuse_address=self._reuse_address,
            reuse_port=self._reuse_port,
        )

    async def _stop_site(self, site: web.TCPSite) -> None:
        """Release *site*'s server and registration; tolerant of a half-started site.

        ``TCPSite.stop`` only closes the asyncio ``Server`` (the LISTEN socket;
        accepted connections stay up) and unregisters the site from the
        runner. A site whose ``start()`` raised is registered but has no
        server, and one already unregistered raises ``RuntimeError`` -- both
        are expected here and must not abort the rebind.
        """
        try:
            await site.stop()
        except RuntimeError:
            pass  # the site is absent from the runner
        except Exception:
            logger.debug("stopping the dead listener site raised", exc_info=True)

    @staticmethod
    def _bound_port(site: web.TCPSite) -> int:
        sockets = _listening_sockets(site)
        if not sockets:
            return 0
        try:
            target = probe_target(sockets[0].getsockname())
        except OSError:
            return 0
        return target[1] if target else 0


def listener_guard_exit_code(guard: ListenerGuard | None) -> int:
    """Exit status contributed by *guard*: its code once it gave up, else ``0``."""
    return guard.exit_code if guard is not None else 0
