"""Keep the running relay in step with the Remote Control setting.

``tunnel.relay_enabled`` picks the relay client at boot, so the field is marked
restart-only. Turning Remote Control OFF must not wait for a restart.
:class:`RemoteControlGuard` runs in the gateway beside the tunnel manager and,
every :data:`POLL_SECONDS`, stops the relay through :meth:`TunnelManager.stop`
whenever it is running but the setting is off (level-triggered, so a stop that
failed is retried).

It never starts the relay. Turning Remote Control on takes a gateway restart, as
``restart=True`` on the field says. The guard does nothing when the relay was not
selected at boot. It reads only the setting.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0

#: The longest a one-click off waits for the relay's open HTTP requests to finish
#: before it stops the relay anyway. The off request itself may have come through the
#: relay, and its answer must reach the browser before the relay closes.
DRAIN_SECONDS = 5.0
_DRAIN_POLL_SECONDS = 0.05


#: ``ensure_available()`` answers this when no relay client is running.
_NOT_RUNNING = "disabled"
#: ...and this only while the client is connected to the relay.
_CONNECTED = "connected"


def _relay_http_idle(provider: Any) -> bool:
    """Whether no relayed HTTP request is still open on the relay client.

    Reads the relay client's connections through their ``http_streams_idle`` event,
    which a connection clears when a request arrives and sets once every request's
    answer has gone back to the relay. A provider without a running client is idle.
    """
    client = getattr(provider, "_client", None)
    for conn in list(getattr(client, "_live", None) or ()):
        idle = getattr(conn, "http_streams_idle", None)
        if idle is not None and not idle.is_set():
            return False
    return True


class RemoteControlGuard:
    """Stops the relay whenever the setting is off; never starts it."""

    def __init__(
        self,
        manager: Any,
        provider: Any,
        *,
        requested: bool = True,
        poll_seconds: float = POLL_SECONDS,
        idle: Optional[Callable[[], bool]] = None,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self._manager = manager
        self._provider = provider
        #: ``tunnel.relay_enabled`` as the live config watcher last applied it
        #: (:meth:`on_config`). True at install: the guard exists only when the
        #: relay was selected at boot.
        self._requested = requested
        self._poll = poll_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._idle = idle or (lambda: _relay_http_idle(provider))
        self._drain = drain_seconds
        #: The pending :meth:`stop_after_drain` task. While it runs, the poll leaves
        #: the relay alone, so it cannot close the connection under an open answer.
        self._deferred: Optional[asyncio.Task[None]] = None
        #: True from :meth:`stop_after_drain` until its drain ends (the apply after
        #: it is still :attr:`_deferred`, so :meth:`stop` can cancel it).
        self._draining = False
        self._stop_reservations = 0
        self._subscription: Any = None

    def on_config(self, change: Any) -> None:
        """The ``tunnel`` applier on the live config watcher: adopt ``relay_enabled``.

        The off edge of ``tunnel.relay_enabled`` is deliberately live; the on edge
        is boot-only. This applier only records the value, and the guard acts on it
        only to stop the relay (:meth:`_act`), never to start it, so ``restart=True``
        on the field stays true for turning it on.
        """
        self._requested = change.new.tunnel.relay_enabled is True

    async def running(self) -> bool:
        """Whether a relay client exists now, connected, starting or failed.

        The stop decision reads this, so a client that ended in error is still
        cleaned up. Whether other devices can reach this Crew is :meth:`connected`.
        """
        state = await self._provider.ensure_available(install=False)
        return state != _NOT_RUNNING

    async def connected(self) -> bool:
        """Whether the relay client is connected now, so other devices can reach Crew."""
        state = await self._provider.ensure_available(install=False)
        return state == _CONNECTED

    async def tick(self) -> None:
        """Read the setting once and stop the relay if it is off."""
        async with self._lock:
            await self._act(self._requested)

    async def _act(self, requested: bool) -> None:
        """Stop the relay unless *requested*. Holds the lock.

        Never starts it: turning it on takes a restart.
        """
        if requested or not await self.running():
            return
        if self.stop_pending:
            return  # stop_after_drain stops it once the open answers are out
        logger.info("Remote Control stopping the relay (turned off)")
        await self._manager.stop()

    async def apply(self) -> None:
        """Act on a write this process just made, without waiting for the poll.

        The caller has the live config watcher reload first (``refresh_now``), so
        :meth:`on_config` has adopted whichever write landed last.
        """
        await self.tick()

    @contextmanager
    def reserve_stop(self) -> Iterator[None]:
        """Keep polling from closing an off request while its write is in flight.

        Reserve before persistence can publish false. On success the caller starts
        the drain before releasing; on failure polling resumes. Count overlapping
        writes so one request cannot release another request's reservation.
        """
        self._stop_reservations += 1
        try:
            yield
        finally:
            self._stop_reservations -= 1

    @property
    def stop_pending(self) -> bool:
        """Whether an off write or its response drain is still pending."""
        return self._stop_reservations > 0 or self._draining

    def stop_after_drain(self) -> None:
        """Act on a turn-off this process just wrote, after its answer is sent.

        A one-click off can arrive through the relay itself, and stopping the relay
        closes the connection that carries the answer. So this returns at once and
        a background task waits until the relay has no open HTTP request (the off
        request's own answer included), for at most :data:`DRAIN_SECONDS`, then runs
        :meth:`apply`. A request that did not come through the relay leaves nothing
        open, so the stop follows straight away.
        """
        if self._deferred is not None and not self._deferred.done():
            return
        self._draining = True
        self._deferred = asyncio.get_running_loop().create_task(
            self._drain_then_apply(), name="remote-control-off"
        )

    async def _drain_then_apply(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self._drain, 0.0)
        # Yield once first, so the handler that scheduled this returns its answer
        # before the idle check can see the relay idle.
        await asyncio.sleep(0)
        while loop.time() < deadline:
            try:
                # On the loop, not a thread: it reads asyncio events, and is cheap.
                if self._idle():
                    break
            except Exception:  # noqa: BLE001 - an unreadable idle state ends the wait
                logger.debug("Remote Control drain check failed", exc_info=True)
                break
            await asyncio.sleep(_DRAIN_POLL_SECONDS)
        else:
            logger.info("Remote Control off: open requests outlived the drain; stopping anyway")
        # The drain ends before apply(), so its tick is not skipped as "stop pending".
        # The task handle is kept until apply() returns, so stop() still cancels a
        # start that apply() is waiting on.
        self._draining = False
        try:
            await self.apply()
        except Exception:  # noqa: BLE001 - the poll retries a stop that failed
            logger.warning("Remote Control off: stopping the relay failed", exc_info=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end the guard
                logger.warning("Remote Control guard tick failed", exc_info=True)
            await asyncio.sleep(self._poll)

    def subscribe(self) -> None:
        """Register :meth:`on_config` on the live config watcher, once.

        Section-wide (``"tunnel"``): ``tunnel.relay_enabled`` is restart-marked, and
        ``config/live.py`` refuses a leaf applier there. The off edge is deliberately
        live and the on edge stays boot-only: this applier is read only to stop.
        """
        if self._subscription is None:
            from kiro_crew.config import live

            self._subscription = live.subscribe(
                "tunnel", callback=self.on_config, name="remote-control-guard"
            )
            # An off the watcher adopted before this guard was installed (while the
            # boot start ran) reaches it now, not only on the next change.
            live.watch().replay(self._subscription)

    def start(self) -> None:
        self.subscribe()
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="remote-control-guard"
            )

    async def stop(self) -> None:
        for task in (self._task, self._deferred):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._deferred = None
        self._draining = False
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None


#: The gateway's guard, set when the relay was selected at boot. One per process.
_guard: Optional[RemoteControlGuard] = None


def install(manager: Any, provider: Any) -> RemoteControlGuard:
    """Create, start and publish the gateway's guard. Called once, on the loop."""
    global _guard
    guard = RemoteControlGuard(manager, provider)
    guard.start()
    _guard = guard
    return guard


def install_for_relay(manager: Any, provider: Any) -> Optional[RemoteControlGuard]:
    """:func:`install`, but only when *provider* is the relay client's provider.

    An edition's own tunnel provider is not Remote Control and is left alone.
    """
    from kiro_crew.relay_client.provider import RelayTunnelProvider

    if not isinstance(provider, RelayTunnelProvider):
        return None
    return install(manager, provider)


def current_guard() -> Optional[RemoteControlGuard]:
    return _guard


async def uninstall() -> None:
    """Stop the guard first at shutdown, so it cannot restart what shutdown stops."""
    global _guard
    guard, _guard = _guard, None
    if guard is not None:
        await guard.stop()
