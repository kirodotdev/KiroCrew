"""Per-connection event-log subscriptions for app-token WebSockets.

The contribution protocol's §3 delta channel. One hub for the whole gateway,
holding a bounded queue and one pump task per SUBSCRIBED SOCKET -- not per
subscription, so a socket watching thirty units still has one writer and one
place where backpressure is decided.

The three properties the contract names, and where each lives:

``eventlog_subscribed`` precedes every ``eventlog_event``
    :meth:`EventLogHub.subscribe` registers the socket for fan-out FIRST, then
    returns the ``lastSeq`` for the caller to send inline, and only then does the
    pump start. Events appended during that window sit in the queue rather than
    being lost, so the common case has no gap at all -- and the frame the client
    reads first is still ``eventlog_subscribed``, because the caller writes it to
    the socket before the pump is allowed to write anything.

The channel is a DELTA channel
    The hub never re-sends and never reorders: a frame it could not enqueue is
    dropped, and the socket is closed rather than left folding across a gap. The
    consumer's ``seq === last + 1`` check plus ``GET .../events?after=`` is the
    recovery path, and closing is what forces it.

A slow subscriber is closed, not buffered
    ``_QUEUE_LIMIT`` frames per socket. Over that, the socket is closed with a
    policy-violation code. Growing the queue instead would let one wedged
    contributor hold the gateway's memory hostage, and the contract explicitly
    reserves the right to close a slow subscriber.

Appends arrive on whatever thread wrote the log (the members handler offloads to
a worker), so :meth:`publish` is thread-safe and hops to the serving loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import Callable

from aiohttp import WSCloseCode, web

from kiro_crew.eventlog.types import Event

logger = logging.getLogger(__name__)

#: Close reason for a frame that could not be made safe to send.
_NOT_REDACTABLE = b"eventlog frame not redactable"

#: Frames one socket may have queued before it is closed as too slow.
_QUEUE_LIMIT = 256

#: Subscriptions one socket may hold. A subscription is cheap, but unbounded
#: growth on an authenticated socket is still a memory grant nobody declared.
_MAX_SUBSCRIPTIONS_PER_SOCKET = 64

WS_SUBSCRIBE = "eventlog_subscribe"
WS_SUBSCRIBED = "eventlog_subscribed"
WS_EVENT = "eventlog_event"
WS_UNSUBSCRIBE = "eventlog_unsubscribe"


class SubscriptionLimit(Exception):
    """The socket already holds :data:`_MAX_SUBSCRIPTIONS_PER_SOCKET`."""


class _SocketState:
    """One socket's queue, pump task and subscription set."""

    __slots__ = ("queue", "pump", "units", "closing")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self.pump: asyncio.Task[None] | None = None
        self.units: set[tuple[str, str]] = set()
        self.closing = False


class EventLogHub:
    """Fan appended events out to the app sockets subscribed to their unit."""

    def __init__(
        self,
        *,
        loop_provider: Callable[[], asyncio.AbstractEventLoop | None] | None = None,
    ) -> None:
        self._sockets: dict[web.WebSocketResponse, _SocketState] = {}
        self._by_unit: dict[tuple[str, str], set[web.WebSocketResponse]] = {}
        self._loop_provider = loop_provider or self._running_loop
        # The serving loop, captured the first time a socket subscribes (which
        # runs on that loop). ``_serving_loop`` falls back to it, so an append
        # racing a FIRST subscribe -- before any pump exists to read a loop
        # from -- can still hop to the serving loop and enqueue, rather than
        # being dropped for want of a known loop.
        self._captured_loop: asyncio.AbstractEventLoop | None = None
        # `_by_unit` is MUTATED loop-side (subscribe / unsubscribe / drop) and
        # READ from the appending thread by `publish`, so it needs a real lock:
        # `list(peers)` on a set another thread is mutating raises "Set changed
        # size during iteration", and the log service swallows a sink failure —
        # so the committed event would silently never reach its subscribers.
        # Held for dict and set operations only, never across an await or I/O.
        self._registry_lock = threading.Lock()

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    # ---- subscription lifecycle ------------------------------------------
    def subscribe(self, ws: web.WebSocketResponse, kind: str, unit_id: str) -> None:
        """Register *ws* for a unit's events. Idempotent.

        Registers BEFORE the caller reads ``lastSeq`` and sends
        ``eventlog_subscribed``, so an append racing the subscribe is queued
        rather than dropped. The pump is not started here -- see
        :meth:`start_pump`.
        """
        state = self._sockets.get(ws)
        if state is None:
            state = _SocketState()
            self._sockets[ws] = state
        key = (kind, unit_id)
        if key not in state.units and len(state.units) >= _MAX_SUBSCRIPTIONS_PER_SOCKET:
            raise SubscriptionLimit(
                f"a socket may hold at most {_MAX_SUBSCRIPTIONS_PER_SOCKET} subscriptions"
            )
        state.units.add(key)
        with self._registry_lock:
            self._by_unit.setdefault(key, set()).add(ws)
        # Capture the serving loop now: subscribe runs on it, and publish's
        # off-loop path needs a known loop to hop to even before this socket's
        # pump is started.
        if self._captured_loop is None:
            running = self._running_loop()
            if running is not None:
                self._captured_loop = running

    def start_pump(self, ws: web.WebSocketResponse) -> None:
        """Start this socket's writer, once the subscribed frame has been sent."""
        state = self._sockets.get(ws)
        if state is None or state.pump is not None:
            return
        loop = self._loop_provider()
        if loop is None:
            logger.debug("eventlog: no loop to pump on; frames will queue")
            return
        state.pump = loop.create_task(self._pump(ws, state))

    def unsubscribe(self, ws: web.WebSocketResponse, kind: str, unit_id: str) -> bool:
        """Drop one subscription. Returns whether it was held."""
        state = self._sockets.get(ws)
        if state is None:
            return False
        key = (kind, unit_id)
        held = key in state.units
        state.units.discard(key)
        with self._registry_lock:
            peers = self._by_unit.get(key)
            if peers is not None:
                peers.discard(ws)
                if not peers:
                    del self._by_unit[key]
        return held

    def drop(self, ws: web.WebSocketResponse) -> None:
        """Forget a socket entirely: every subscription and its pump.

        Called from the WS handler's cleanup and from app teardown. Safe to call
        for a socket that never subscribed.
        """
        state = self._sockets.pop(ws, None)
        if state is None:
            return
        with self._registry_lock:
            for key in state.units:
                peers = self._by_unit.get(key)
                if peers is None:
                    continue
                peers.discard(ws)
                if not peers:
                    del self._by_unit[key]
        state.units.clear()
        if state.pump is not None:
            state.pump.cancel()
            state.pump = None

    def subscriptions(self, ws: web.WebSocketResponse) -> frozenset[tuple[str, str]]:
        state = self._sockets.get(ws)
        return frozenset(state.units) if state is not None else frozenset()

    def subscriber_count(self, kind: str, unit_id: str) -> int:
        with self._registry_lock:
            return len(self._by_unit.get((kind, unit_id), ()))

    def sockets_for_app(self, app: str) -> list[web.WebSocketResponse]:
        """Every subscribed socket belonging to *app* (teardown, §6)."""
        # Snapshot for the same reason as _serving_loop: teardown can reach this
        # off the serving loop, and a live iterator would raise on a concurrent
        # subscribe or drop.
        return [ws for ws in list(self._sockets) if ws.get("_app", "") == app]

    # ---- fan-out ---------------------------------------------------------
    def publish(self, kind: str, unit_id: str, event: Event) -> None:
        """Enqueue one appended event for every subscriber of its unit.

        The ``EventSink`` the log service calls. Runs on the appending thread,
        inside the log's per-unit lock, so it must not block and must not raise:
        it serializes once, then hands the string to the serving loop.
        """
        peers = self._peers(kind, unit_id)
        if not peers:
            return
        unit = None
        try:
            from kiro_crew.eventlog.contrib import get_unit

            unit = get_unit(kind)
        except Exception:  # pragma: no cover - defensive
            logger.debug("eventlog: unit lookup failed for %r", kind, exc_info=True)
        id_field = unit.id_field if unit is not None else "id"
        # Network-boundary redaction, matching the projection broadcast and the
        # catch-up read: an event's `data` carries agent-authored free-text that
        # can hold a credential or presigned URL, and this frame goes live to the
        # browser. Scrub `data` before serialization. The redactor is pure string
        # work (no I/O), so it is safe on the appending thread inside the lock.
        safe_event: Event = event
        try:
            from kiro_crew.eventlog.service import (
                _redact_projection_value,
                redact_projection_identifier,
            )

            data = event.get("data")
            if isinstance(data, dict):
                _safe_data = _redact_projection_value(data)
                if isinstance(_safe_data, dict):
                    # The event's TYPE is chosen by the contributor too, so it is
                    # an attacker-controlled string on the same wire as its data
                    # and passes the same chain. A normal `<app>/<action>` is
                    # unchanged by the redactor; a credential-shaped one is not
                    # handed to every co-subscriber verbatim.
                    # ``Event`` declares ``type`` as a required ``str``, so index it.
                    # ``.get`` widens the value to ``object``, and the redacted result
                    # then fails the type check on the very field it is assigned back
                    # to. A missing key raises inside this ``try``, which fails closed
                    # by dropping the frame -- the failure mode this arm already has.
                    _raw_type: str = event["type"]
                    safe_event = {
                        **event,
                        "type": redact_projection_identifier(_raw_type),
                        "data": _safe_data,
                    }
        except Exception:
            # FAIL CLOSED. The old fallback published the RAW event, which is the
            # one outcome redaction exists to prevent: a granted contributor
            # could then reach co-subscribers with an unredacted credential by
            # making the redactor fail. Depth is bounded at the door
            # (`contrib.MAX_VALUE_DEPTH`) so this should be unreachable; if it
            # happens anyway the frame is dropped and the subscribers are closed,
            # and each resumes by catch-up — whose read redacts on the same path.
            logger.warning(
                "eventlog: redaction failed for %s/%s; dropping the frame and "
                "closing its subscribers",
                kind,
                unit_id,
                exc_info=True,
            )
            self._close_subscribers(kind, unit_id)
            return
        try:
            msg = json.dumps(
                {
                    "type": WS_EVENT,
                    "data": {"kind": kind, id_field: unit_id, "id": unit_id, "event": safe_event},
                }
            )
        except (TypeError, ValueError):
            logger.warning("eventlog: event for %s/%s is not serializable", kind, unit_id)
            return

        targets = peers
        loop = self._loop_provider()
        if loop is None:
            # Off-loop append with no running loop on this thread: hand it to the
            # serving loop the pumps live on.
            loop = self._serving_loop()
            if loop is None or loop.is_closed():
                logger.debug("eventlog: no serving loop; dropping fan-out")
                return
            loop.call_soon_threadsafe(self._enqueue_many, targets, msg)
            return
        self._enqueue_many(targets, msg)

    def _peers(self, kind: str, unit_id: str) -> list[web.WebSocketResponse]:
        """A snapshot of one unit's subscribers, taken under the registry lock.

        The copy is what makes an off-loop `publish` safe: the loop can add or
        drop a subscriber at any moment, and iterating the live set from the
        appending thread raises instead of fanning out.
        """
        with self._registry_lock:
            return list(self._by_unit.get((kind, unit_id), ()))

    def _close_subscribers(self, kind: str, unit_id: str) -> None:
        """Close every subscriber of one unit, from any thread.

        The remedy when a frame cannot be made safe to send: the consumer's own
        recovery path is `GET .../events?after=<last folded seq>`, and closing is
        what forces it. Marking `closing` first stops the pump from writing
        anything else in the meantime.
        """
        for ws in self._peers(kind, unit_id):
            state = self._sockets.get(ws)
            if state is not None:
                state.closing = True
            loop = self._loop_provider() or self._serving_loop()
            if loop is None or loop.is_closed():
                continue
            if loop is self._running_loop():
                loop.create_task(self._close(ws, _NOT_REDACTABLE))
            else:
                loop.call_soon_threadsafe(self._schedule_close, loop, ws)

    def _schedule_close(self, loop: asyncio.AbstractEventLoop, ws: web.WebSocketResponse) -> None:
        """Start the close coroutine ON the serving loop.

        A named method rather than a closure: this is handed to
        ``call_soon_threadsafe`` from the appending thread, where a lambda with a
        captured default is both harder to read and untypeable.
        """
        loop.create_task(self._close(ws, _NOT_REDACTABLE))

    def _serving_loop(self) -> asyncio.AbstractEventLoop | None:
        """The loop the pumps run on.

        Prefers a live pump's loop; falls back to the loop captured at the
        first subscribe, so an append that races the very first subscription
        (no pump yet) still finds the serving loop instead of dropping the
        fan-out.
        """
        # The captured loop FIRST, and a snapshot for the fallback. This runs on
        # the appending thread (see publish's docstring) while the serving loop
        # is free to add a socket in subscribe or remove one in drop, and a
        # Python-level iterator over the live dict raises RuntimeError when it
        # observes that. The sink's caller swallows a raise, so the cost was a
        # committed delta that no subscriber ever received.
        #
        # Reordering is safe because the closed check below is what made the
        # captured loop second-choice, and it is still made: subscribe captures
        # the loop before any pump exists, so whenever a pump's loop is usable
        # the captured one is too.
        loop = self._captured_loop
        if loop is not None and not loop.is_closed():
            return loop
        for state in list(self._sockets.values()):
            if state.pump is not None:
                return state.pump.get_loop()
        return None

    def _enqueue_many(self, targets: list[web.WebSocketResponse], msg: str) -> None:
        for ws in targets:
            state = self._sockets.get(ws)
            if state is None or state.closing:
                continue
            try:
                state.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # A slow subscriber. Closing is the contract's own remedy: the
                # consumer resumes by catch-up from its last folded seq, which is
                # correct, where a silently dropped frame would leave it folding
                # across a gap it never saw.
                logger.info(
                    "eventlog: closing a subscriber that fell %d frames behind", _QUEUE_LIMIT
                )
                self._close_slow(ws, state)

    def _close_slow(self, ws: web.WebSocketResponse, state: _SocketState) -> None:
        state.closing = True
        loop = self._loop_provider() or self._serving_loop()
        if loop is None or loop.is_closed():
            return
        loop.create_task(self._close(ws, b"eventlog subscriber too slow"))

    async def _close(self, ws: web.WebSocketResponse, reason: bytes) -> None:
        self.drop(ws)
        with contextlib.suppress(Exception):
            await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=reason)

    async def _pump(self, ws: web.WebSocketResponse, state: _SocketState) -> None:
        """Write this socket's queued frames, in order, until it closes."""
        try:
            while not ws.closed:
                msg = await state.queue.get()
                try:
                    await ws.send_str(msg)
                except Exception:
                    # The peer is gone; the WS handler's own cleanup calls drop().
                    logger.debug("eventlog: send failed, subscriber likely gone")
                    return
        except asyncio.CancelledError:
            raise

    # ---- teardown (§6) ---------------------------------------------------
    async def close_app(self, app: str) -> int:
        """Close every subscription held by *app*'s sockets. Returns the count.

        The socket itself is closed rather than merely unsubscribed: the app's
        code is being stopped, so leaving an authenticated socket open with no
        subscriptions is a connection to a process that is being torn down.
        """
        targets = self.sockets_for_app(app)
        for ws in targets:
            await self._close(ws, b"app disabled")
        return len(targets)


_hub: EventLogHub | None = None


def get_hub() -> EventLogHub:
    """Process-wide hub, created on first use."""
    global _hub
    if _hub is None:
        _hub = EventLogHub()
    return _hub


def set_hub(hub: EventLogHub | None) -> None:
    """Test seam."""
    global _hub
    _hub = hub


def attach_to_service() -> None:
    """Wire the hub into the member log service as its append sink.

    Called once at dashboard startup, next to ``attach_broadcast``. Idempotent:
    re-attaching sets the same sink.
    """
    try:
        from kiro_crew.eventlog.service import get_service

        get_service().attach_event_sink(get_hub().publish)
    except Exception:
        logger.warning("eventlog: could not attach the subscription hub", exc_info=True)
