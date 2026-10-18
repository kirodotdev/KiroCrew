"""One gateway connection's bridge between kiro-cli's stdio and the daemon's socket.

Owner module of :mod:`kiro_crew.mcp_gateway.stub`, which re-exports every name here;
read and patch them there. This module owns:

* the socket's framing (:func:`_write_frame`, :func:`_read_frame`, :func:`_safe_close`)
  and the control frames the daemon and the stub exchange beside MCP traffic (ping,
  pong, keepalive, ``queued``);
* :class:`StubSession`, the per-stub state that outlives any one connection: the stdin
  reader and its queue, the captured ``initialize``, and why the last bridge ended;
* :func:`run_bridge`, the pump loop over one connection with its ping-while-outstanding
  liveness check, and :func:`_emit_error_frames`, which answers kiro-cli's abandoned
  requests;
* the stub's logger, which the facade and the other owners import from here.

It never imports the facade.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import sys
import threading
from typing import Any, Optional

from kiro_crew.json_line import parse_json_object_line

#: The stub's one logger, shared by every owner module and by the
#: :mod:`kiro_crew.mcp_gateway.stub` facade, which imports it from here. The stub runs
#: as ``python -m kiro_crew.mcp_gateway.stub``, where the facade's ``__name__`` is
#: ``"__main__"``, so the name follows how the facade was loaded: the stub's stderr
#: names one logger, the one the facade's ``__name__`` names.
logger = logging.getLogger(
    "__main__"
    if getattr(getattr(sys.modules.get("__main__"), "__spec__", None), "name", None)
    == "kiro_crew.mcp_gateway.stub"
    else "kiro_crew.mcp_gateway.stub"
)


# --- Bridge liveness (ping-while-outstanding) constants ---------------------
# Interval between successive stub→gateway liveness pings while at least one
# JSON-RPC request is outstanding. A peer that is merely slow (but alive)
# responds to pings even while processing a long tool call.
_BRIDGE_PING_INTERVAL_SECS = 10.0
# How many CONSECUTIVE pings must go unanswered before declaring the peer dead.
# Total grace period is _BRIDGE_PING_INTERVAL_SECS × _BRIDGE_PING_MAX_MISSES.
_BRIDGE_PING_MAX_MISSES = 3
# Reserved type field for the stub→gateway liveness ping control frame and its
# response. The gateway echoes {"type": "pong"} for any {"type": "ping"} it
# receives from a registered stub.
_BRIDGE_PING_TYPE = "ping"
# Cap on emitting the liveness error frames. Bounded because the whole point of
# that path is to stop a caller hanging — an unbounded write to a wedged reader
# would reproduce the defect it exists to fix.
_ERROR_EMIT_TIMEOUT_SECS = 5.0
_BRIDGE_PONG_TYPE = "pong"
# Reserved type for the gateway->stub keepalive control frame. The gateway
# writes one to every live stub each heartbeat sweep so that a half-open
# transport — which a parked reader cannot observe — fails an actual write and
# becomes detectable. It carries no payload and expects no reply: the write
# succeeding or failing IS the signal, and it is consumed here rather than
# forwarded, exactly like the pong frame above.
_BRIDGE_KEEPALIVE_TYPE = "keepalive"


# Lines the stdin reader thread may hold for a consumer that is not reading. The
# reader BLOCKS on a full queue, so kiro-cli's pipe applies backpressure and a
# slow consumer cannot balloon RSS. ``_drain_while_disconnected`` holds itself to
# the same COUNT for the same reason: it moves lines out of the queue, so a bound
# of its own is what keeps that guarantee from being defeated.
_STDIN_QUEUE_MAXSIZE = 256


# Daemon->stub control frame while queued behind the spawn gate. Consumed by
# the pre-flight, the reconnect replay and the bridge; never forwarded.
_SPAWN_QUEUED_TYPE = "queued"


async def _write_frame(writer: asyncio.StreamWriter, obj: dict) -> None:
    # stdin_pump and _recaller_loop both write this shared stub->gateway socket
    # and both await drain(). Each write() here lands a WHOLE frame in one
    # synchronous call, so frame bytes cannot interleave — the hazard is the
    # concurrent drain(): under backpressure, FlowControlMixin._drain_helper on
    # many deployed interpreter patch releases holds a single drain waiter
    # (`assert waiter is None or waiter.cancelled()`), so a second concurrent
    # drain() trips the assert, kills that pump task, and tears the bridge down
    # (newer patch releases allow multiple waiters; older ones are common).
    # Serialize the write+drain pair through the writer's lock — the same
    # _mc_write_lock idiom gatewayd/backend use on their writers — which also
    # keeps the invariant robust if a future edit splits a frame across writes.
    lock = getattr(writer, "_mc_write_lock", None)
    guard = lock if lock is not None else contextlib.nullcontext()
    async with guard:
        writer.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")
        await writer.drain()


class _NonObjectFrame(json.JSONDecodeError):
    """A gateway frame that is valid JSON but neither an object nor ``null``.

    A ``JSONDecodeError``, so every catch set that degrades on a bad frame
    still does; the admission wait catches it first and reads the next frame.
    """


async def _read_frame(reader: asyncio.StreamReader) -> Optional[dict]:
    try:
        line = await reader.readuntil(b"\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    if not line:
        return None
    # errors="replace": an invalid-UTF-8 byte must NOT raise UnicodeDecodeError
    # (a ValueError, not json.JSONDecodeError) out through the handshake /
    # ensure_backend catch sets — that would kill the stub before fallback_exec.
    # Every unusable frame (not JSON, not an object, nested past the decoder's
    # ceiling, which raises RecursionError) becomes the one JSONDecodeError
    # those catch sets handle, and degrades cleanly to a per-session exec. A
    # non-object value raises the _NonObjectFrame subclass, which only the
    # admission wait tells apart.
    msg = parse_json_object_line(line, errors="replace")
    if msg is not None:
        return msg
    if _is_json_non_object(line):
        raise _NonObjectFrame("gateway frame is not a JSON object", "", 0)
    raise json.JSONDecodeError("gateway frame is not JSON", "", 0)


def _is_json_non_object(line: bytes) -> bool:
    """True when *line* is JSON other than an object or ``null``.

    Run only for a frame the shared parser already refused, so the second
    parse costs nothing on the frames that matter.
    """
    try:
        value = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return False
    return value is not None


async def _safe_close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


class StubSession:
    """Per-STUB state that must outlive any single gateway connection.

    A connection dies with the daemon; the stub does not. Everything a reconnect
    needs therefore lives here rather than inside :func:`run_bridge`'s frame,
    which is scoped to one socket:

    * **the stdin reader thread and its queue.** This is why a reconnect cannot
      simply call ``run_bridge`` again on a fresh socket. Creating the reader
      per call would put two threads on fd 0 -- splitting kiro-cli's lines
      between two consumers -- while any line the first one had already
      dequeued dies with the old frame. The head buffer
      (:meth:`push_front`) is the same argument one level up: the drain that
      runs while the bridge is down dequeues lines to judge them, and a frame it
      chose not to answer belongs to the stream, not to that task.
    * **the ``initialize`` frame.** The stdin pump consumes and forwards it
      once and kiro-cli never re-sends it, so without a copy here a fresh daemon
      would hold a never-initialized backend that rejects every later call --
      the reason the liveness path deliberately does not exec a replacement.
    * **the ``initialize`` result**, so a reconnect can refuse a daemon
      generation that would answer this session differently from the toolset it
      froze at ``session/new``.
    * **why the last bridge ended**, which is what decides reconnect vs exit.
    """

    #: Bridge endings a reconnect may follow. Everything else is a real
    #: shutdown -- kiro-cli closed stdin, a signal arrived, or kiro-cli's own
    #: reader died -- where re-attaching would serve nobody.
    RECONNECTABLE = frozenset({"peer_dead", "socket_eof", "socket_write_failed"})

    def __init__(self) -> None:  # noqa: D107
        #: Why the most recent bridge ended; see :attr:`RECONNECTABLE`.
        self.reason: str = ""
        #: Request IDs still unanswered when that bridge ended.
        self.outstanding_ids: list = []
        #: Verbatim ``initialize`` line as kiro-cli sent it.
        self.captured_init: Optional[bytes] = None
        #: The ``result`` object this session was told at handshake time.
        self.init_result: Optional[dict] = None
        #: Successful reconnects so far, for logging and the audit record.
        self.reconnects: int = 0
        self._subscribed: bool = False
        self._line_q: Optional["asyncio.Queue[bytes]"] = None
        #: Lines already taken off the queue that no pump has forwarded yet.
        #: :func:`_drain_while_disconnected` fills this while the bridge is down;
        #: :meth:`next_line` empties it before touching the queue, so the next
        #: bridge forwards them ahead of anything that arrives later.
        self._head: list[bytes] = []

    def note_outbound(self, line: bytes, msg: dict) -> None:
        """Record what a forwarded frame means for a future reconnect."""
        method = msg.get("method")
        if self.captured_init is None and method == "initialize":
            # Bounded by construction: one frame, replaced never, and only the
            # handshake matches -- so this cannot grow with session traffic.
            self.captured_init = line if line.endswith(b"\n") else line + b"\n"
        elif method == "resources/subscribe":
            # Latched on the REQUEST and never released. Counting on the request
            # is deliberate: a subscribe this session believes it holds is what
            # must not be dropped silently, and over-counting only costs a
            # reconnect that would have been safe.
            #
            # Never released even on ``resources/unsubscribe``, which is the
            # subtler half: that too is only a request, so an unsubscribe the
            # server REJECTED would clear the flag while the subscription stayed
            # live upstream -- and the reconnect would then be allowed for a
            # session whose resource updates are about to stop with nothing
            # said. Confirming it would mean tracking responses per id, which is
            # more machinery than the recovery is worth while replaying
            # subscriptions is still a follow-up.
            self._subscribed = True

    def has_live_subscriptions(self) -> bool:
        """Has this session asked for resource subscriptions a restart loses?

        The daemon's subscription table lives in its process and dies with it, so
        a reconnect that replayed only ``initialize`` would come back looking
        healthy while resource updates never arrived again. That is a silent
        degradation this fix must not introduce, so a subscribed session is
        refused the reconnect and takes the pre-existing terminal exit instead --
        the same outcome it had before, rather than a new quiet one.
        """
        return self._subscribed

    def note_init_result(self, msg: dict) -> None:
        """Remember the handshake result the FIRST daemon gave this session."""
        if self.init_result is None and isinstance(msg.get("result"), dict):
            self.init_result = msg["result"]

    def captured_init_id(self) -> Any:
        """The request id of the captured handshake, or ``None``."""
        if self.captured_init is None:
            return None
        try:
            msg = json.loads(self.captured_init)
        except (ValueError, TypeError):
            return None
        return msg.get("id") if isinstance(msg, dict) else None

    async def next_line(self) -> bytes:
        """One line from the real ``sys.stdin``, via a single reader thread.

        The read runs on a DEDICATED DAEMON thread and hands lines over through
        a bounded queue. A daemon thread is never joined at interpreter or
        asyncio shutdown, so a ``readline`` still blocked because kiro-cli holds
        stdin open cannot hang graceful SIGTERM -- the older
        ``run_in_executor(readline)`` used the default executor, which
        ``asyncio.run()`` joins via ``shutdown_default_executor()``.

        Lines put back by :meth:`push_front` come out first, in the order
        kiro-cli sent them. That is what lets the disconnected drain inspect a
        frame without consuming it: whatever it did not answer is handed to the
        next bridge ahead of anything newer, so a reconnect never reorders the
        stream.
        """
        if self._head:
            return self._head.pop(0)
        if self._line_q is None:
            self._line_q = self._start_reader()
        return await self._line_q.get()

    def push_front(self, lines: list[bytes]) -> None:
        """Return dequeued lines to the head of the inbound stream, in order.

        Called from the drain's ``finally`` so a cancellation cannot strand the
        frames it was holding: they belong to kiro-cli's stream, not to the task
        that happened to be looking at them.
        """
        if lines:
            self._head = list(lines) + self._head

    def take_pending_request_ids(self) -> list:
        """Ids of every request nobody will forward, dropping the frames.

        For the terminal path only. These frames are normally forwarded by the
        next bridge, but when the reconnect gives up there is no next bridge, so
        leaving them would mean those calls are never answered for the rest of a
        session whose stdout is about to close. The ids join
        :attr:`outstanding_ids` and are answered by the terminal
        :func:`_emit_error_frames` -- the same single-response rule as everywhere
        else, which is why the frames are dropped as the ids are taken.

        BOTH places a line can be waiting are read, and reading only one is a hole
        rather than an omission: the head holds what the drain handed back, while
        the queue holds whatever the reader thread has put there since -- a
        pipelined burst, or anything that arrived in the window between the drain
        being awaited and this call. A line left in the queue is a request that no
        longer has any reader at all.

        A buffered ``initialize`` is included rather than deferred: deferral
        exists so the handshake replay can answer it for real, and by the time
        this is called no replay is coming.

        The reader thread may still put another line in after this returns. That
        one is genuinely unanswerable -- the process is on its way out and stdout
        is about to close -- and no bookkeeping here can change it.
        """
        lines = list(self._head)
        self._head = []
        if self._line_q is not None:
            while True:
                try:
                    lines.append(self._line_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
        ids = []
        for line in lines:
            msg = parse_json_object_line(line)
            if msg is not None and "method" in msg and "id" in msg:
                ids.append(msg["id"])
        return ids

    def _start_reader(self) -> "asyncio.Queue[bytes]":
        loop = asyncio.get_running_loop()
        # Bounded + backpressured: the reader thread blocks on put() (via
        # run_coroutine_threadsafe) when the queue is full, so a stalled
        # writer.drain() (gatewayd slow to accept) cannot let the reader keep
        # draining stdin into an unbounded queue and balloon RSS.
        line_q: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=_STDIN_QUEUE_MAXSIZE)

        def _blocking_reader() -> None:
            fh = sys.stdin.buffer
            try:
                while True:
                    chunk = fh.readline()
                    # Block this thread until the queue has room. .result()
                    # waits on the loop; if the loop has stopped (shutdown) it
                    # raises and the daemon thread simply exits.
                    asyncio.run_coroutine_threadsafe(line_q.put(chunk), loop).result()
                    if not chunk:
                        return
            except Exception:  # pragma: no cover — defensive
                try:
                    asyncio.run_coroutine_threadsafe(line_q.put(b""), loop).result(timeout=1.0)
                except Exception:
                    pass

        threading.Thread(target=_blocking_reader, name="stub-stdin", daemon=True).start()
        return line_q


async def run_bridge(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stop_event: asyncio.Event,
    *,
    stdin: Optional[asyncio.StreamReader] = None,
    stdout_writer: Optional[asyncio.StreamWriter] = None,
    ping_interval: float = _BRIDGE_PING_INTERVAL_SECS,
    ping_max_misses: int = _BRIDGE_PING_MAX_MISSES,
    peer_supports_ping: bool = False,
    session: StubSession,
) -> None:
    """Pump stdin ↔ socket until either side closes or ``stop_event`` fires.

    ``stdin``/``stdout_writer`` are dependency-injected so tests can drive
    the bridge through in-process pipes; ``None`` falls back to real
    ``sys.stdin``/``sys.stdout``. On clean stdin EOF an ``Unregister``
    frame is sent so gatewayd detaches without waiting on refcount.

    ``session`` is the SINGLE channel this reports through, and it carries the
    state that must survive THIS connection so the caller can re-attach to a
    restarted gateway: pass the same :class:`StubSession` across successive calls
    and the stdin reader, the captured handshake and the ending all persist.
    ``session.reason`` names the ending precisely -- which is what distinguishes
    a transport loss worth reconnecting from a real shutdown -- and
    ``session.outstanding_ids`` lists what was left unanswered. It is required
    rather than defaulted because there is deliberately no return value: a
    caller without a session would learn nothing, and a second spelling of "why
    the bridge ended" would have to be kept in step with the first forever.
    """

    # Writer-thread liveness signals. The real bridge hands stdout frames to a
    # daemon writer thread (see stdout_pump); if that thread dies (broken pipe
    # to the kiro-cli reader) the bridge must tear down even when NO further
    # upstream line ever arrives to trip the producer-side check — otherwise
    # stdout_pump parks in reader.readuntil() forever and the bridge hangs, the
    # very leak this guards against. The threading.Event is polled by the
    # producer (_emit) as a fast path; the asyncio.Event, set via
    # call_soon_threadsafe from the writer thread, wakes a dedicated bridge task
    # so teardown never depends on upstream traffic.
    bridge_loop = asyncio.get_running_loop()
    # Per-CONNECTION fields, cleared so a reconnect is not judged on the last
    # connection's ending. The captured handshake and the stdin reader are
    # deliberately NOT cleared: those are what a reconnect exists to reuse.
    session.reason = ""
    session.outstanding_ids = []
    # stdin_pump and _recaller_loop both write this shared stub->gateway socket;
    # serialize their write+drain through one lock (mirrors gatewayd/backend).
    if getattr(writer, "_mc_write_lock", None) is None:
        setattr(writer, "_mc_write_lock", asyncio.Lock())
    writer_failed = threading.Event()
    writer_failed_evt = asyncio.Event()

    def _flag_writer_failed() -> None:
        writer_failed.set()
        bridge_loop.call_soon_threadsafe(writer_failed_evt.set)

    # --- Liveness state (ping-while-outstanding) ----------------------------
    # Track JSON-RPC request IDs forwarded to the gateway that have not yet
    # received a response. The liveness monitor fires ONLY while this set is
    # non-empty, so idle/slow-but-alive sessions are never timed out.
    _outstanding_ids: set = set()
    # Pong receipt flag: set by the stdout pump when a pong arrives, cleared
    # by the monitor each tick. Lightweight alternative to a counter/queue.
    _pong_received = asyncio.Event()
    # Peer-dead event: set by the monitor when consecutive pings go unanswered.
    _peer_dead_evt = asyncio.Event()

    async def stdin_pump() -> None:
        # Inbound line source. Tests inject an in-process StreamReader; the real
        # stub reads sys.stdin through the SESSION, which owns exactly one
        # reader thread per process. Owning it here instead would put a second
        # thread on fd 0 the moment a reconnect ran this pump again, splitting
        # kiro-cli's lines between two consumers (see StubSession).
        async def _next_line() -> bytes:
            if stdin is not None:
                try:
                    return await stdin.readuntil(b"\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    return b""
            return await session.next_line()

        while True:
            line = await _next_line()
            if not line:
                session.reason = session.reason or "stdin_eof"
                try:
                    await _write_frame(writer, {"type": "unregister"})
                except Exception:
                    pass
                return
            # Track outbound JSON-RPC request IDs (have "method" + "id"), and
            # let the session keep whatever a reconnect will need.
            # Best-effort: a line that does not parse as an object (nested
            # past the decoder's ceiling included) is still forwarded verbatim.
            msg = parse_json_object_line(line)
            if msg is not None:
                try:
                    session.note_outbound(line, msg)
                    if "method" in msg and "id" in msg:
                        _outstanding_ids.add(msg["id"])
                except (ValueError, TypeError):
                    pass
            try:
                # Serialize with _recaller_loop's _write_frame writes on the
                # same socket: the write itself is whole-frame atomic, but a
                # second concurrent drain() under backpressure trips the
                # single-waiter assert in FlowControlMixin._drain_helper on
                # pre-3.12-fix interpreters, killing this pump task and tearing
                # the bridge down (see _write_frame).
                _lock = getattr(writer, "_mc_write_lock", None)
                _guard = _lock if _lock is not None else contextlib.nullcontext()
                async with _guard:
                    writer.write(line if line.endswith(b"\n") else line + b"\n")
                    await writer.drain()
            except (ConnectionError, BrokenPipeError):
                session.reason = session.reason or "socket_write_failed"
                return

    async def stdout_pump() -> None:
        stdout_fh = sys.stdout.buffer
        # Real path: hand frames to a DEDICATED daemon writer thread rather than
        # asyncio.to_thread (the default executor). asyncio.run() joins the
        # default executor via shutdown_default_executor(), so a write blocked
        # on a stalled kiro-cli reader (its stdout pipe buffer full) would hang
        # graceful SIGTERM. A daemon thread is never joined — mirroring the
        # stdin reader decoupling above. The queue is bounded so a stalled
        # reader can't balloon RSS; the pump applies backpressure with a
        # cancellable sleep, never the default executor.
        write_q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=256)
        writer_thread: Optional[threading.Thread] = None
        # writer_failed (threading.Event) + writer_failed_evt (asyncio.Event)
        # are defined at run_bridge scope; _flag_writer_failed() sets both so a
        # dead writer surfaces both to the producer (_emit fast path) and to a
        # dedicated bridge task, tearing the bridge down even if upstream goes
        # silent.
        if stdout_writer is None:

            def _blocking_writer() -> None:
                try:
                    while True:
                        item = write_q.get()
                        if item is None:
                            return
                        try:
                            stdout_fh.write(item)
                            stdout_fh.flush()
                        except Exception:
                            # Real write failure (reader gone / pipe broken):
                            # flag it so _emit raises and the bridge tears down
                            # even with no more upstream lines, then exit.
                            _flag_writer_failed()
                            return
                except Exception:  # pragma: no cover — defensive
                    _flag_writer_failed()

            writer_thread = threading.Thread(
                target=_blocking_writer, name="stub-stdout", daemon=True
            )
            writer_thread.start()

        async def _emit(line: bytes) -> None:
            if stdout_writer is not None:
                stdout_writer.write(line)
                await stdout_writer.drain()
                return
            # Bounded put with cancellable backpressure — never the default
            # executor, so a stalled writer thread cannot hang SIGTERM.
            while True:
                if writer_failed.is_set():
                    # Writer thread died — propagate instead of parking on a
                    # full queue forever so the bridge tears down.
                    raise BrokenPipeError("stub stdout writer thread died")
                try:
                    write_q.put_nowait(line)
                    return
                except queue.Full:
                    await asyncio.sleep(0.05)

        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    session.reason = session.reason or "socket_eof"
                    return
                if not line:
                    session.reason = session.reason or "socket_eof"
                    return
                # Intercept control frames (gateway liveness reply, gateway
                # keepalive) and track response IDs to clear outstanding
                # requests.
                # Best-effort: parse failures pass the line through verbatim.
                _is_control = False
                msg = parse_json_object_line(line)
                try:
                    if msg is not None:
                        _mtype = msg.get("type")
                        if _mtype == _BRIDGE_PONG_TYPE:
                            _pong_received.set()
                            _is_control = True
                        elif _mtype == _BRIDGE_KEEPALIVE_TYPE:
                            # Gateway-side transport probe. Nothing to do: the
                            # gateway learns what it needs from whether the
                            # write succeeded. Swallow it.
                            _is_control = True
                        elif _mtype == _SPAWN_QUEUED_TYPE:
                            # Our backend is being respawned behind the spawn
                            # gate. The daemon is alive and still holds our
                            # place, which is exactly what a pong proves, so it
                            # satisfies the liveness monitor too.
                            _pong_received.set()
                            _is_control = True
                        elif "id" in msg and "method" not in msg:
                            # A response (has id, no method) — clear from
                            # outstanding set.
                            _outstanding_ids.discard(msg["id"])
                            if msg["id"] == session.captured_init_id():
                                # What this session was told at handshake time.
                                # A later daemon generation answering the
                                # replayed handshake differently must not be
                                # passed off as the same server.
                                session.note_init_result(msg)
                except (ValueError, TypeError):
                    pass
                # Control frames are gateway<->stub only; never forward to
                # kiro-cli stdout.
                if _is_control:
                    continue
                try:
                    await _emit(line)
                except (ConnectionError, BrokenPipeError):
                    session.reason = session.reason or "writer_failed"
                    return
        finally:
            # Best-effort stop signal; the daemon thread is never joined, so
            # shutdown never blocks on it regardless.
            if writer_thread is not None:
                try:
                    write_q.put_nowait(None)
                except queue.Full:
                    pass

    async def _liveness_monitor() -> None:
        """Ping the gateway ONLY while requests are outstanding, and declare the
        peer dead after ``ping_max_misses`` consecutive unanswered pings.

        Every cycle consumes exactly ONE ``ping_interval``, whichever way it
        ends: a MISSED pong consumes it as the wait itself, and an ANSWERED pong
        consumes what is left of it as a sleep. So the advertised grace is
        ``ping_interval × ping_max_misses`` rather than twice that, and the ping
        RATE is one per interval rather than one per round-trip. That remainder
        sleep is load-bearing: the gateway answers a ping inline in its
        connection handler, so a healthy pong is back in microseconds and a loop
        that returned straight to the next ping would ping at socket speed for
        the whole life of an outstanding request -- burning a core on this stub
        and on the single-loop daemon that has to answer every one of them.
        An idle bridge also resets the miss count, so an earlier partial streak
        cannot carry across an idle gap.

        Never fires on an idle bridge, nor on a peer that answers while still
        working — that is the distinction between "slow" and "wedged", and the
        reason a blanket bridge timeout is the wrong mechanism here.
        """
        consecutive_misses = 0
        while not stop_event.is_set() and not _peer_dead_evt.is_set():
            if not _outstanding_ids:
                # Idle: burn one interval, then re-check. A stop during the wait
                # exits cleanly.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=ping_interval)
                    return
                except asyncio.TimeoutError:
                    pass
                consecutive_misses = 0
                continue
            # Clear the pong flag before sending so a reply to THIS ping is what
            # satisfies the wait below, not a stale one.
            _pong_received.clear()
            try:
                await _write_frame(writer, {"type": _BRIDGE_PING_TYPE})
            except (OSError, ConnectionError, BrokenPipeError):
                # Socket already broken — bridge will tear down on its own.
                return
            # Stamped AFTER the write, not before: _write_frame awaits the
            # shared writer lock and drain, so a stamp taken first would count
            # that backpressure against the interval and let the next ping
            # follow the pong immediately.
            sent_at = bridge_loop.time()
            # We check immediately after sending: the pong may have arrived
            # between our clear and the send (or during the write).
            # Give the peer the full next interval to reply.
            try:
                await asyncio.wait_for(_pong_received.wait(), timeout=ping_interval)
            except asyncio.TimeoutError:
                pass
            if _pong_received.is_set():
                consecutive_misses = 0
                # Answered: the wait above was NOT the interval, so sleep out
                # the rest of it before the next ping. Measured from the ping on
                # the wire, so a slow-but-answered pong shortens this sleep
                # instead of adding to it, keeping the rate at one ping per
                # interval no matter how fast the peer replies. A stop wakes us
                # at once, so teardown never waits out a gap. ``_peer_dead_evt``
                # needs no waiter here: this task is its only setter and it
                # returns immediately after setting it, so it cannot change
                # under us.
                remaining = ping_interval - (bridge_loop.time() - sent_at)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=remaining)
                        return
                    except asyncio.TimeoutError:
                        pass
            else:
                consecutive_misses += 1
                logger.warning(
                    "stub liveness: ping unanswered (%d/%d), " "outstanding_ids=%d",
                    consecutive_misses,
                    ping_max_misses,
                    len(_outstanding_ids),
                )
                if consecutive_misses >= ping_max_misses:
                    logger.error(
                        "stub liveness: peer dead after %d missed pings; " "triggering fallback",
                        consecutive_misses,
                    )
                    _peer_dead_evt.set()
                    return

    tasks = {
        asyncio.create_task(stdin_pump(), name="kirocrew-mcp-stub-stdin"),
        asyncio.create_task(stdout_pump(), name="kirocrew-mcp-stub-stdout"),
        asyncio.create_task(stop_event.wait(), name="kirocrew-mcp-stub-stop"),
        # Wakes when the stdout writer thread dies, so the bridge tears down
        # even if no further upstream line arrives to trip _emit's fast-path
        # check.
        asyncio.create_task(writer_failed_evt.wait(), name="kirocrew-mcp-stub-writer-failed"),
    }
    # Gated on negotiation, not assumed: an older gatewayd has no ping handler,
    # so pinging it would guarantee a miss streak and force-degrade a healthy
    # session. No capability, no monitor — the bridge behaves exactly as before.
    if peer_supports_ping:
        tasks.add(asyncio.create_task(_liveness_monitor(), name="kirocrew-mcp-stub-liveness"))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _safe_close(writer)
        # Ending reason, most authoritative cause first. A dead peer also breaks
        # the stdin pump's next write, so the pump-set reason must not mask it;
        # and a deliberate stop outranks every transport symptom it produces.
        if stop_event.is_set():
            session.reason = "stop"
        elif _peer_dead_evt.is_set():
            session.reason = "peer_dead"
        elif writer_failed.is_set():
            session.reason = "writer_failed"
        elif not session.reason:
            # No pump claimed an ending. Unknown is deliberately NOT
            # reconnectable: re-attaching on a cause nobody can name is how a
            # recoverable outage turns into a session that answers wrongly.
            session.reason = "unknown"
        session.outstanding_ids = list(_outstanding_ids)


async def _emit_error_frames(req_ids: list, message: str, *, pool_label: str) -> None:
    """Answer abandoned requests so the caller is never left parked.

    The write runs on a worker thread and is bounded by
    ``_ERROR_EMIT_TIMEOUT_SECS``: a blocked kiro-cli reader (full stdout pipe)
    must not wedge the very path whose job is to unwedge the caller, which is
    the defect this path exists to prevent.
    """
    if not req_ids:
        return

    def _write() -> None:
        for req_id in req_ids:
            err_frame = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": message},
            }
            sys.stdout.buffer.write(
                json.dumps(err_frame, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        sys.stdout.buffer.flush()

    try:
        await asyncio.wait_for(asyncio.to_thread(_write), timeout=_ERROR_EMIT_TIMEOUT_SECS)
    except (asyncio.TimeoutError, OSError, ValueError):
        # Nothing better to do: the reader is gone or wedged. Exiting still
        # closes stdout, which is what tells kiro-cli this server is done.
        logger.warning("could not emit error frames pool=%s", pool_label)
