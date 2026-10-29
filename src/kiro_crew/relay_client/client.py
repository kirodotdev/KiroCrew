"""The relay client: keeps one live host connection to the relay, and moves it when asked.

:class:`RelayClient` dials with the token source's host token, serves the connection
until it ends, classifies the end (:func:`~kiro_crew.relay_client.backoff.classify`)
and either redials, backs off, renews the host token, or stops with a reason a
person can read in ``/api/tunnel/status``. When the relay sends ``reconnect`` it opens
the next connection before it lets go of the current one (make before break): the
old connection finishes its open HTTP requests and then closes with 1000.

The client reports through the two callbacks the tunnel wrapper registers:
``on_connect(url)`` once the Crew's relay URL is live, so the wrapper adds it to the
allowed origins, and ``on_disconnect()`` when the client stops or gives up.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from collections import deque
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import aiohttp

from kiro_crew.relay_client.backoff import Backoff, CloseAction, ConnectionEnd, classify
from kiro_crew.relay_client.connection import HostConnection, RelayTimings
from kiro_crew.relay_client.ingress import CrewIngress
from kiro_crew.relay_client.protocol import (
    BACKOFF_HISTORY_MAX_ENTRIES,
    BACKOFF_RESET_AFTER_SECONDS,
    CLOSE_GOING_AWAY,
    crew_origin,
)
from kiro_crew.relay_client.token_source import (
    HostCredential,
    HostTokenRetryLater,
    HostTokenSource,
    HostTokenUnavailable,
    host_binding,
)

logger = logging.getLogger(__name__)

# Edition-neutral state strings: the values of ``kiro_crew.tunnel.TunnelState``.
# Spelled here because the platform layer composes this client and must not import
# the tunnel package.
STATE_STARTING = "starting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"

OnConnect = Callable[[str], Awaitable[None]]
OnDisconnect = Callable[[], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


class _Renewal(enum.Enum):
    """How one ask for a fresh host token went."""

    RENEWED = "renewed"
    #: The source cannot renew right now; keep the current token and back off.
    LATER = "later"
    #: The source cannot renew at all; the client stops.
    STOP = "stop"


_STOP_MESSAGES = {
    CloseAction.STOP_UPGRADE: "the relay needs a newer protocol version: update Kiro Crew",
    CloseAction.STOP_PROTOCOL: "the relay refused this client's hello",
    CloseAction.STOP_REPLACED: "another instance of this Crew holds its relay connection",
}


class RelayClient:
    """Hold the host connection for as long as the client runs."""

    def __init__(
        self,
        *,
        host_url: str,
        crew_url_for: Callable[[str], str],
        token_source: HostTokenSource,
        ingress: CrewIngress,
        client_version: str,
        timings: RelayTimings = RelayTimings(),
        rng: Optional[random.Random] = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        backoff_reset_after: float = BACKOFF_RESET_AFTER_SECONDS,
    ) -> None:
        self._host_url = host_url
        self._crew_url_for = crew_url_for
        self._tokens = token_source
        self._ingress = ingress
        self._client_version = client_version
        self._timings = timings
        self._sleep = sleep
        self._monotonic = monotonic
        self._backoff = Backoff(rng=rng)
        self._backoff_reset_after = backoff_reset_after
        self._on_connect: Optional[OnConnect] = None
        self._on_disconnect: Optional[OnDisconnect] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._stopping = asyncio.Event()
        self._live: Set[HostConnection] = set()
        self._draining: Optional[HostConnection] = None
        self._background: Set[asyncio.Task[Any]] = set()
        self._credential: Optional[HostCredential] = None
        self._state = STATE_STOPPED
        self._error = ""
        self._url = ""
        self._started_at = 0.0
        self._connected_at = 0.0
        self._delays: deque[float] = deque(maxlen=BACKOFF_HISTORY_MAX_ENTRIES)

    @property
    def delays(self) -> list[float]:
        """A snapshot of the bounded recent backoff history, oldest first."""
        return list(self._delays)

    # ── the provider-facing surface ──

    def register_callbacks(
        self, *, on_connect: Optional[OnConnect], on_disconnect: Optional[OnDisconnect]
    ) -> None:
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def state(self) -> str:
        return self._state

    @property
    def public_url(self) -> str:
        return self._url if self._state == STATE_CONNECTED else ""

    def relay_connected(self) -> bool:
        """Whether a host connection is up: past ``ready`` and not yet closing.

        A lease renewal reports this as ``relay.connected``, which the contract
        (section 4.2) makes true only from ``ready`` until that connection ends.
        During a move the old and the new connection overlap, and either counts.
        """
        return any(conn.serving for conn in self._live)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "url": self.public_url,
            "error": self._error,
            "started_at": self._started_at,
            "connected_at": self._connected_at,
            "reconnect_attempt": self._backoff.attempt,
        }

    async def start(self) -> None:
        if self.running:
            return
        self._stopping = asyncio.Event()
        self._state = STATE_STARTING
        self._error = ""
        self._started_at = time.time()
        self._tokens.on_new_token(self._forward_token)
        self._tokens.track_relay(self.relay_connected)
        self._task = asyncio.create_task(self._run(), name="relay-client")

    async def stop(self, code: int = CLOSE_GOING_AWAY) -> None:
        """Close every connection with *code* and stop redialling."""
        self._stopping.set()
        self._tokens.on_new_token(None)
        self._tokens.track_relay(None)
        for conn in list(self._live):
            await conn.close(code)
        if self._task is not None:
            done, _ = await asyncio.wait({self._task}, timeout=self._timings.close_handshake * 2)
            if not done:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        # A drain cancelled before it ran never reached its own discard; every
        # connection is closed by now, so none of them is live.
        self._live.clear()
        self._unbind()
        await self._lose_url()
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._state = STATE_STOPPED

    # ── the loop ──

    def _client_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env=False: the dial carries the host token, so the session never
            # reads netrc or any other credential from the environment, which could
            # swap the token for an unrelated service's password. A proxy set in the
            # environment is still used: each dial resolves it and passes it itself.
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    def _connection(self, credential: HostCredential) -> HostConnection:
        conn = HostConnection(
            session=self._client_session(),
            url=credential.host_url or self._host_url,
            credential=credential,
            ingress=self._ingress,
            binding=host_binding(credential) or ("", ""),
            client_version=self._client_version,
            timings=self._timings,
            monotonic=self._monotonic,
        )
        self._live.add(conn)
        return conn

    async def _wait(self, delay: float) -> None:
        """Sleep for *delay*, waking early when the client stops."""
        self._delays.append(delay)
        sleeper = asyncio.ensure_future(self._sleep(delay))
        stopper = asyncio.ensure_future(self._stopping.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            stopper.cancel()

    def _fail(self, message: str) -> None:
        logger.warning("relay client stopped: %s", message)
        self._state = STATE_ERROR
        self._error = message

    async def _run(self) -> None:
        renewed_after_refusal = False
        try:
            while not self._stopping.is_set():
                try:
                    credential = self._credential or await self._tokens.current()
                except HostTokenUnavailable as exc:
                    self._fail(str(exc))
                    return
                except HostTokenRetryLater as exc:
                    self._state, self._error = STATE_RECONNECTING, str(exc)
                    await self._wait(self._backoff.next_delay())
                    continue
                if host_binding(credential) is None:
                    self._fail("the host token names no Crew kind, or a personal Crew no owner")
                    return
                self._credential = credential
                try:
                    end, served = await self._serve(credential)
                finally:
                    self._unbind()
                if self._stopping.is_set():
                    return
                if end.reached_ready:
                    renewed_after_refusal = False
                if served >= self._backoff_reset_after:
                    self._backoff.reset()
                action = classify(end)
                logger.info("relay connection ended (%s); next: %s", _describe(end), action.value)
                if action is CloseAction.STOP_LOCAL:
                    self._fail(end.stop_reason)
                    await self._lose_url()
                    return
                if action in _STOP_MESSAGES:
                    self._fail(_STOP_MESSAGES[action])
                    await self._lose_url()
                    return
                self._state = STATE_RECONNECTING
                if action is CloseAction.RECONNECT_NOW:
                    continue
                if action is CloseAction.RENEW_ONCE:
                    if renewed_after_refusal:
                        self._fail("the relay refused the host token twice")
                        await self._lose_url()
                        return
                    renewal = await self._renew()
                    if renewal is _Renewal.STOP:
                        return
                    if renewal is _Renewal.RENEWED:
                        renewed_after_refusal = True
                        continue
                    # The source could not renew yet: the one renewal is not
                    # spent, so the next refusal asks again after the backoff.
                    await self._wait(self._backoff.next_delay())
                    continue
                if action is CloseAction.RENEW_THEN_BACKOFF:
                    if await self._renew() is _Renewal.STOP:
                        return
                await self._wait(self._backoff.next_delay())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("relay client loop failed")
            self._fail(f"relay client failed: {type(exc).__name__}")
            await self._lose_url()

    def _unbind(self) -> None:
        """Stop the dashboard honouring hand-offs once no connection serves them."""
        try:
            self._ingress.unbind_host()
        except Exception:
            logger.warning("the relay ingress refused the unbind", exc_info=True)

    async def _renew(self) -> "_Renewal":
        """Ask the source for a fresh host token."""
        try:
            self._credential = await self._tokens.renew(host_token_requested=True)
        except HostTokenUnavailable as exc:
            self._fail(str(exc))
            await self._lose_url()
            return _Renewal.STOP
        except HostTokenRetryLater:
            # Keep the token the client holds; the backoff paces the next try.
            return _Renewal.LATER
        return _Renewal.RENEWED

    async def _serve(self, credential: HostCredential) -> tuple[ConnectionEnd, float]:
        """Serve connections until the current one ends; return how, and for how long."""
        conn = self._connection(credential)
        run: "asyncio.Future[ConnectionEnd]" = asyncio.ensure_future(conn.run())
        if not await self._until(run, conn.ready):
            return await self._finish(conn, run), 0.0
        ready_at = self._monotonic()
        await self._now_connected(conn)
        while True:
            if not await self._until(run, conn.move_requested):
                return await self._finish(conn, run), self._monotonic() - ready_at
            moved = await self._move(conn, run)
            if moved is None:
                conn.move_requested.clear()
                continue
            conn, run = moved
            ready_at = self._monotonic()

    async def _until(self, run: "asyncio.Future[ConnectionEnd]", event: asyncio.Event) -> bool:
        """Wait for *event*; ``False`` when the connection ended or the client stopped first."""
        waiter = asyncio.ensure_future(event.wait())
        stopper = asyncio.ensure_future(self._stopping.wait())
        pending: "set[asyncio.Future[Any]]" = {run, waiter, stopper}
        try:
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            stopper.cancel()
        return event.is_set() and not run.done() and not self._stopping.is_set()

    async def _finish(
        self, conn: HostConnection, run: "asyncio.Future[ConnectionEnd]"
    ) -> ConnectionEnd:
        if not run.done():
            # Only a stop gets here with the connection still open.
            await conn.close(CLOSE_GOING_AWAY)
        try:
            return await run
        finally:
            self._live.discard(conn)

    async def _move(
        self, old: HostConnection, old_run: "asyncio.Future[ConnectionEnd]"
    ) -> Optional[tuple[HostConnection, "asyncio.Future[ConnectionEnd]"]]:
        """Open the next connection before the old one closes (make before break).

        Returns the new connection once it is ready, after handing the old one to
        a background drain. Returns ``None`` when the new one did not get ready
        within the relay's window; the old one then ends with the relay's 1012,
        which redials at once.

        The window (``closeInSeconds``) bounds only the wait for the new connection.
        The old one keeps its HTTP requests until they finish, then closes with
        1000, or until the relay closes it with 1012, which ends the drain without a
        redial: the new connection already serves the endpoint.
        """
        window = old.move_close_in
        credential = self._credential or old.credential
        new = self._connection(credential)
        new_run: "asyncio.Future[ConnectionEnd]" = asyncio.ensure_future(new.run())
        ready = asyncio.ensure_future(new.ready.wait())
        pending: "set[asyncio.Future[Any]]" = {ready, new_run, old_run}
        try:
            await asyncio.wait(pending, timeout=window, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
        if not new.ready.is_set() or new_run.done():
            await new.close(1000)
            await asyncio.gather(new_run, return_exceptions=True)
            self._live.discard(new)
            return None
        # Only a ready replacement may cut the earlier move's drain short.
        # Otherwise a failed dial would discard work with no hand-off to replace it.
        if self._draining is not None:
            await self._draining.close(1000)
        self._draining = old
        self._spawn(self._drain(old, old_run))
        await self._now_connected(new)
        return new, new_run

    async def _drain(self, old: HostConnection, old_run: "asyncio.Future[ConnectionEnd]") -> None:
        try:
            await old.drain_and_close(quiet=self._timings.move_grace)
            await asyncio.gather(old_run, return_exceptions=True)
        finally:
            self._live.discard(old)
            if self._draining is old:
                self._draining = None

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _now_connected(self, conn: HostConnection) -> None:
        url = (self._credential.crew_url if self._credential else None) or self._crew_url_for(
            conn.endpoint_id
        )
        # The URL joins the allowed origins, which an Origin header matches
        # exactly; a lease answer names the URL with a trailing slash.
        url = crew_origin(url)
        if self._credential is not None and conn.credential is not self._credential:
            # A new host token arrived while this connection was opening.
            try:
                await conn.send_token(self._credential)
            except ConnectionError:
                return
        self._state = STATE_CONNECTED
        self._error = ""
        self._connected_at = time.time()
        if url != self._url:
            await self._lose_url()
            self._url = url
            await self._call(self._on_connect, url)

    async def _lose_url(self) -> None:
        if self._url:
            self._url = ""
            await self._call(self._on_disconnect)

    async def _call(self, callback: Optional[Callable[..., Awaitable[None]]], *args: Any) -> None:
        if callback is None:
            return
        try:
            await callback(*args)
        except Exception:
            logger.warning("relay connect/disconnect callback failed", exc_info=True)

    async def _forward_token(self, credential: HostCredential) -> None:
        """Send a new host token down every live connection, as the relay expects."""
        self._credential = credential
        for conn in list(self._live):
            if conn.ready.is_set():
                try:
                    await conn.send_token(credential)
                except ConnectionError:
                    logger.debug("a relay connection closed before its new token went out")


def _describe(end: ConnectionEnd) -> str:
    if end.http_status is not None:
        return f"upgrade answered {end.http_status}"
    if end.code is not None:
        return f"close {end.code} {end.reason}".strip()
    return end.detail or "socket ended"
