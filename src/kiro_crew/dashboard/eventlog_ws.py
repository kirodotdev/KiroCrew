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

    __slots__ = ("queue", "pump", "units", "closing", "pending")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self.pump: asyncio.Task[None] | None = None
        self.units: set[tuple[str, str]] = set()
        self.closing = False
        # Units whose subscribe handshake has not finished. A live frame for a
        # PENDING (kind, unit) is staged here instead of going to `queue`, so the
        # pump cannot deliver it ahead of the subscription's own acknowledgement
        # and replay frames -- which would feed the subscriber's fold an event out
        # of order. `activate` flushes replay-then-buffer into the queue and drops
        # the key, after which live frames for that unit go straight to `queue`.
        self.pending: dict[tuple[str, str], list[str]] = {}


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
        was_new = key not in state.units
        state.units.add(key)
        with self._registry_lock:
            self._by_unit.setdefault(key, set()).add(ws)
            # Mark a FRESH subscription PENDING: a live frame arriving before
            # `activate` is staged in this buffer rather than the shared queue, so
            # the pump cannot deliver it ahead of the replay + acknowledgement. A
            # duplicate subscribe to an already-held unit is a no-op here and does
            # not re-open a pending buffer for an already-active stream.
            if was_new:
                state.pending[key] = []
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
        if state.closing:
            # The socket is already being torn down (a redaction fault or a
            # revoked `activate` marked it and scheduled `_close`). `ws.py` calls
            # this unconditionally right after `activate`, so it is reached on the
            # revoked path too -- starting a pump or flushing that socket's pending
            # buffer here would just race the close. Leave both to `_close`/`drop`.
            return
        # Safety net: flush any subscription still marked pending that was never
        # explicitly activated. The full handshake calls `activate` (replay +
        # buffered, in order) BEFORE this, so by here its key is already cleared;
        # this only fires for a caller that subscribes and starts the pump without
        # the replay handshake, and it must not strand that unit's buffered live
        # frames in the pending buffer forever. No replay to prepend on this path,
        # so the buffered frames go straight to the queue in arrival order.
        if state.pending:
            with self._registry_lock:
                leftover: list[str] = []
                for buf in state.pending.values():
                    leftover.extend(buf)
                state.pending.clear()
            for msg in leftover:
                try:
                    state.queue.put_nowait(msg)
                except asyncio.QueueFull:
                    self._close_slow(ws, state)
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
            state.pending.pop(key, None)
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
        peers = self._live_peers(kind, unit_id)
        if not peers:
            return
        msg = self._event_frame(kind, unit_id, event)
        if msg is None:
            # Redaction or serialization failed: _event_frame has already logged,
            # and on a redaction fault it closed the unit's subscribers (each
            # resumes by catch-up, which redacts on the same path). Drop the frame.
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
            loop.call_soon_threadsafe(self._enqueue_many, targets, msg, (kind, unit_id))
            return
        self._enqueue_many(targets, msg, (kind, unit_id))

    def _event_frame(self, kind: str, unit_id: str, event: Event) -> str | None:
        """The redacted ``eventlog_event`` wire string for one event, or None.

        Shared by live :meth:`publish` and the subscribe-time replay so a
        replayed event is byte-identical to the live one and passes the SAME
        network-boundary redaction. Returns None when the event cannot be made
        safe to send: on a redaction fault it FAILS CLOSED (logs, closes the
        unit's subscribers so each resumes by catch-up), and on a serialization
        error it logs and drops. Pure string work, safe on the appending thread.
        """
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
            return None
        try:
            return json.dumps(
                {
                    "type": WS_EVENT,
                    "data": {"kind": kind, id_field: unit_id, "id": unit_id, "event": safe_event},
                }
            )
        except (TypeError, ValueError):
            logger.warning("eventlog: event for %s/%s is not serializable", kind, unit_id)
            return None

    def _peers(self, kind: str, unit_id: str) -> list[web.WebSocketResponse]:
        """A snapshot of one unit's subscribers, taken under the registry lock.

        The copy is what makes an off-loop `publish` safe: the loop can add or
        drop a subscriber at any moment, and iterating the live set from the
        appending thread raises instead of fanning out. Returns EVERY subscriber
        (including a revoked app's) -- the fan-out path filters revoked apps via
        :meth:`_live_peers`, while the close path needs the unfiltered set so a
        revoked socket is still torn down on a redaction fault.
        """
        with self._registry_lock:
            return list(self._by_unit.get((kind, unit_id), ()))

    def _live_peers(self, kind: str, unit_id: str) -> list[web.WebSocketResponse]:
        """Fan-out peers with revoked apps removed. See :meth:`_peers`.

        Re-checks authorization per app at DELIVERY. Two gates, both closing a
        window between a subscription and a disable:

        * The in-process revocation tombstone (`grants.is_revoked`): closes the
          register-during-revoke races -- a subscription can slip in between a
          grant fence check and its registration, or during an app replacement,
          and the tombstone denies it regardless of when its socket registered.
        * The per-app grant FENCE captured on the socket at setup
          (`ws["_app_fence"]`) vs the current `grants.grant_fence(app)`: closes a
          disable that lands AFTER the socket subscribed, including a FILE-ONLY
          disable in another process. That disable moves only the durable epoch,
          which the in-process tombstone cannot see but the fence folds in -- so
          without this a warm subscription would keep receiving frames for the
          disabled app until the reconciler poll. A socket with no captured fence
          (a dashboard user, `_app == ""`) is unaffected: it takes neither gate.

        `grants` reads a DIFFERENT lock, so it cannot deadlock against the registry
        lock and does not block the appending thread; `grant_fence` is an
        mtime-memoized epoch read (one `os.stat` in the steady state).
        """
        from kiro_crew.eventlog import grants

        live: list[web.WebSocketResponse] = []
        for ws in self._peers(kind, unit_id):
            app = ws.get("_app", "")
            if not app:
                live.append(ws)
                continue
            if grants.is_revoked(app):
                continue
            captured = ws.get("_app_fence")
            if captured is not None and not grants.fence_admits(captured, grants.grant_fence(app)):
                # A disable (in this process or another) landed after this socket
                # subscribed -- deny delivery. The subscriber recovers by
                # reconnecting and re-authorizing.
                continue
            live.append(ws)
        return live

    def _close_subscribers(self, kind: str, unit_id: str) -> None:
        """Close every subscriber of one unit, from any thread.

        The remedy when a frame cannot be made safe to send: the consumer's own
        recovery path is `GET .../events?after=<last folded seq>`, and closing is
        what forces it. Marking `closing` first stops any FURTHER frame from being
        enqueued on this socket in the meantime (`_enqueue_many` and `activate`
        skip a `closing` socket); the scheduled `_close` is what ends delivery of
        whatever is already queued, by closing the socket out from under the pump.
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

    def _enqueue_many(
        self,
        targets: list[web.WebSocketResponse],
        msg: str,
        key: tuple[str, str] | None = None,
    ) -> None:
        for ws in targets:
            state = self._sockets.get(ws)
            if state is None or state.closing:
                continue
            # A frame for a unit still mid-handshake on this socket is STAGED in
            # the subscription's pending buffer, not the shared queue: the pump
            # would otherwise deliver it ahead of the replay + acknowledgement and
            # feed the subscriber's fold an out-of-order event. `activate` flushes
            # the buffer (after replay) once the handshake completes. Checked under
            # the registry lock so it cannot race `activate` clearing the key.
            if key is not None:
                with self._registry_lock:
                    buf = state.pending.get(key)
                    if buf is not None:
                        if len(buf) >= _QUEUE_LIMIT:
                            # The staging buffer is bounded exactly like the queue:
                            # a subscription whose handshake stalls while appends
                            # pour in must be CLOSED (its recovery is a fresh
                            # catch-up read), not buffered without limit -- an
                            # unbounded buffer would be the very "hide a gap" the
                            # queue's overflow-close exists to prevent.
                            over = True
                        else:
                            buf.append(msg)
                            continue
                    else:
                        over = False
                if over:
                    logger.info(
                        "eventlog: closing a subscriber whose pending handshake "
                        "buffer exceeded %d frames",
                        _QUEUE_LIMIT,
                    )
                    self._close_slow(ws, state)
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

    def _schedule_revoked_close(self, ws: web.WebSocketResponse, state: _SocketState) -> None:
        """Tear down a subscription whose app lost its grant mid-handshake.

        The revocation twin of :meth:`_close_slow`: marks ``closing`` so no further
        frame is enqueued on this socket, then schedules :meth:`_close` (which
        ``drop``s the registration and closes the socket, ending delivery). Used by
        :meth:`activate` when the captured replay would otherwise reach an app
        revoked -- in this process or a cross-process file-only disable -- after it
        subscribed.
        """
        state.closing = True
        loop = self._loop_provider() or self._serving_loop()
        if loop is None or loop.is_closed():
            return
        loop.create_task(self._close(ws, b"eventlog grant revoked"))

    def activate(
        self,
        ws: web.WebSocketResponse,
        kind: str,
        unit_id: str,
        events: list[Event],
    ) -> None:
        """Finish a subscription's handshake: flush replay + buffered live frames.

        Called AFTER :meth:`subscribe` and the ``eventlog_subscribed`` ack, and
        BEFORE :meth:`start_pump`. It closes the ordering race the pending buffer
        opened: while the subscription was pending, any live frame for this unit
        was staged in ``state.pending[key]`` instead of the shared queue (so the
        pump could not deliver it ahead of the ack). Here we enqueue, in order:
        the catch-up REPLAY frames first (bridging the client's folded cursor to
        live), then the BUFFERED live frames that arrived during the handshake,
        then drop the pending key so subsequent live frames go straight to the
        queue. The drain-and-clear happens under the registry lock, so a
        concurrent :meth:`publish` either appended to the buffer before we drained
        it (and is flushed here) or finds the key already gone and enqueues
        directly -- never a frame lost between the two.

        Each replay event goes through the SAME :meth:`_event_frame` path live
        delivery uses, so a replayed frame is byte-identical and redacted
        identically. A frame that cannot be built is skipped (``_event_frame``
        fails closed and, on a redaction fault, closes the unit's subscribers).
        Loop-side and I/O-free: the caller reads the events off-loop and hands
        them in.
        """
        state = self._sockets.get(ws)
        if state is None or state.closing:
            return
        # Final authorization re-check before this subscription delivers anything.
        # The replay list was captured in `subscribe` BEFORE several awaits (the
        # off-loop `may_use_kind`/`resolve_unit`/`last_seq`/`events_after` reads and
        # the ack send), so a grant revoked -- in this process OR a cross-process
        # file-only disable -- during that handshake would otherwise be handed the
        # replay here with no gate, while live fan-out IS gated per frame in
        # `_live_peers`. Re-check the SAME two gates (`is_revoked` + captured fence)
        # that live delivery uses; on a mismatch deliver nothing and close the
        # socket, whose recovery is reconnect + re-authorize. A dashboard socket
        # (`_app == ""`) takes neither gate, exactly as in `_live_peers`.
        app = ws.get("_app", "")
        if app:
            from kiro_crew.eventlog import grants

            captured = ws.get("_app_fence")
            if grants.is_revoked(app) or (
                captured is not None and not grants.fence_admits(captured, grants.grant_fence(app))
            ):
                # Deny delivery and tear the socket down. `_schedule_revoked_close`
                # marks `closing` (so no further frame is enqueued) and schedules
                # `_close`, which `drop`s the registration and closes the socket --
                # the "unsubscribe on mismatch" the subscriber recovers from by
                # reconnecting and re-authorizing. Returning here leaves this unit's
                # `state.pending[key]` buffer un-popped, but `drop` clears the whole
                # socket's pending on teardown, so nothing is stranded.
                self._schedule_revoked_close(ws, state)
                return
        key = (kind, unit_id)
        replay_msgs: list[str] = []
        for event in events:
            msg = self._event_frame(kind, unit_id, event)
            if msg is not None:
                replay_msgs.append(msg)
        with self._registry_lock:
            buffered = state.pending.pop(key, [])
            ordered = replay_msgs + buffered
        for msg in ordered:
            try:
                state.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # The catch-up span plus buffered live frames overran the queue:
                # the consumer is too far behind to bridge live. Close it -- its
                # own recovery is a fresh catch-up read from its last folded seq.
                logger.info(
                    "eventlog: closing a subscriber whose replay exceeded %d frames",
                    _QUEUE_LIMIT,
                )
                self._close_slow(ws, state)
                return

    async def _close(self, ws: web.WebSocketResponse, reason: bytes) -> None:
        self.drop(ws)
        with contextlib.suppress(Exception):
            await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=reason)

    async def _pump(self, ws: web.WebSocketResponse, state: _SocketState) -> None:
        """Write this socket's queued frames, in order, until it closes."""
        from kiro_crew.eventlog import grants

        try:
            while not ws.closed:
                msg = await state.queue.get()
                # Re-check authorization BEFORE delivering this already-queued frame
                # (GPT 6.1 F3). The fence/revocation gates in `_live_peers` and
                # `activate` are checked only at ENQUEUE time; a file-only disable
                # that races an active stream leaves frames already on the queue,
                # and without this recheck `_pump` would deliver them to a
                # now-revoked subscriber. Same two gates and same cheap
                # mtime-memoized `grant_fence` stat as `_live_peers`; a dashboard
                # socket (`_app == ""`) takes neither gate. On a mismatch, stop
                # delivering and tear the socket down (its recovery is reconnect +
                # re-auth), rather than draining the rest of the queue.
                app = ws.get("_app", "")
                if app:
                    captured = ws.get("_app_fence")
                    if grants.is_revoked(app) or (
                        captured is not None
                        and not grants.fence_admits(captured, grants.grant_fence(app))
                    ):
                        logger.info(
                            "eventlog: stopping delivery to a revoked/fenced subscriber mid-stream"
                        )
                        self._schedule_revoked_close(ws, state)
                        return
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
