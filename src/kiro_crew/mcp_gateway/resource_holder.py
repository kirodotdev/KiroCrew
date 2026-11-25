"""The resource-subscription holder: a gateway connection the Kiro Crew server keeps for a session.

``rfc-mcp-resource-subscriptions.md`` §4.1-§4.3. A session's MCP resource
subscriptions must outlive the session's kiro-cli, whose stub detaches when the
chat goes idle. The holder is a second stub connection for one (session,
server) pair, opened from this process, that registers with the session stub's
Register (from ``describe-session``) under a ``stub_uuid`` of its own, sends the
stub's ``initialize``, subscribes each URI and reads updates on its own socket.

Identity. The holder is attributed by an inline caller block -- the ``caller``
of the claim frame the session itself is claimed with
(:func:`~kiro_crew.mcp_gateway.claim.build_claim_frame`) -- and registers with no
session token and no ancestor pids. The daemon builds an identical
``CallerContext`` from that block, so the holder shares the session's grant on a
server that accepts caller identity (§4.4), and nothing pid-indexed can reach the
connection afterwards. A claim would not do: a claim is applied to every
connection under the pid it names, and the only pid naming this process names
every kiro-cli stub it spawned too.

Liveness (§4.3). An MCP ``ping`` is forwarded every ``ping_interval_secs``.
The daemon can answer any in-flight holder request when the backend dies,
restarts, or is replaced with a changed tool set. Those answers describe the
transport, not the server's verdict: the holder ends that connection,
reconnects, registers again under a fresh ``stub_uuid``, initializes,
re-subscribes every tracked URI and reports a gap. End of file on the socket
means the daemon went away and follows the same reconnect path. A refused
register, backend or server-authored ``initialize`` pauses the holder, except a
``capacity`` refusal of the backend, which is about the host or the moment: the
holder reconnects later, or, while it awaits the subscribe that started it,
stops so that subscribe is refused. A server-authored refused re-subscribe, or
one answered without a verdict, pauses that URI; the second also ends the
connection. A re-subscribe accepted for a URI unsubscribed meanwhile is
released before the holder goes live. A subscribe with an unknown outcome, and
an unsubscribe not answered with an explicit result, end the connection too:
the holder reconnects and subscribes what it still tracks.

Update and gap events leave through synchronous listener methods that return at
once. Pause transitions are awaited because their durable write must land before
the holder publishes or acts on them.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.backend_wire import _log_safe_identifier, _tool_call_error_text
from kiro_crew.mcp_gateway.claim import build_claim_frame
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES

logger = logging.getLogger(__name__)

#: Register field a holder sets on its own Register. A holder's connection is
#: never the answer to ``describe-session`` (``daemon.session_describe``): it
#: describes the session's stub, and a holder that described itself would carry
#: a copy forward forever.
RESOURCE_HOLDER_FIELD = "resource_holder"

#: Bound on one control or request round trip through the daemon.
REQUEST_TIMEOUT_SECS = 30.0
#: Bound on a register or ``ensure_backend`` answer. A cold backend spawn sits
#: inside ``ensure_backend``. The holder sends it bare, so the daemon waits for
#: spawn capacity only its short legacy wait and then answers a ``capacity``
#: refusal, which the holder treats as a lost connection: it reconnects no sooner
#: than the refusal's ``retry_after_secs``. This bound covers a daemon that never
#: answers at all; the holder then reconnects too. A subscribe that starts
#: a holder waits for it while it holds the service's lock, but for at most what
#: is left of that subscribe's own budget
#: (``resource_subscriptions.SUBSCRIBE_BUDGET_SECS``), which also has to cover
#: its ``describe-session`` (a connect and a read, :data:`REQUEST_TIMEOUT_SECS`
#: each), its subscribe request (:data:`REQUEST_TIMEOUT_SECS`) and the store
#: I/O: that budget bounds the lock hold, not the sum of these bounds.
HANDSHAKE_TIMEOUT_SECS = 120.0
#: Reconnect backoff after the daemon went away, doubling to the ceiling.
RECONNECT_DELAY_SECS = 1.0
RECONNECT_DELAY_MAX_SECS = 30.0
#: Longest wait a ``capacity`` refusal's own ``retry_after_secs`` may set. That
#: one wait may exceed :data:`RECONNECT_DELAY_MAX_SECS`, because the daemon names
#: it (60 s for an unavailable backend).
CAPACITY_RETRY_MAX_SECS = 300.0
#: How often a live holder forwards an MCP ``ping`` (§4.3).
PING_INTERVAL_SECS = 60.0

#: Most characters of a pause or refusal reason the holder reports. The text is
#: partly server-authored, and the record and every event keep it.
REASON_MAX_CHARS = 1024
#: Ends a reason cut at :data:`REASON_MAX_CHARS`; counted inside that bound.
REASON_CUT_MARKER = " [cut]"

#: The prefix of the error message the daemon answers an in-flight request with
#: after it respawned the backend under it (``daemon.connection``).
_RESTARTED_PREFIX = "backend restarted mid-call, retry: "
#: The prefix of the error message a dying backend answers its in-flight
#: requests with (``backend_liveness``), before anything has respawned it.
_GONE_PREFIX = "backend gone: "
#: The prefix used when a replacement backend failed tool-surface validation.
_REPLACED_PREFIX = "the MCP server was replaced and its tool set changed ("
#: Every daemon-authored answer that means the request lost its backend.
_LOST_BACKEND_PREFIXES = (_GONE_PREFIX, _RESTARTED_PREFIX, _REPLACED_PREFIX)
#: The JSON-RPC error code of the daemon-authored answers above.
_DAEMON_ERROR_CODE = -32000
#: ``admission_protocol.REJECT_CLASS_CAPACITY``: a refusal about the host or the
#: moment, never the target. Restated rather than imported, so the holder, which
#: runs in the Kiro Crew server, does not load the daemon's admission modules; a
#: test pins the two equal.
_REJECT_CLASS_CAPACITY = "capacity"
#: Daemon frames that are never an answer to a register or ``ensure_backend``:
#: ``keepalive`` (the sweeper's transport probe, written to every registered
#: connection, the holder's included), ``pong`` and ``queued``. The session stub
#: skips the same three (``stub_link._admission_control_frame``). Restated for the
#: reason above; a test pins them.
_DAEMON_CONTROL_FRAME_TYPES = frozenset({"keepalive", "pong", "queued"})


#: :attr:`HolderPaused.code` for a server whose ``initialize`` result does not
#: declare ``resources.subscribe`` (RFC §4.9.3 ``not_subscribable``).
PAUSED_NOT_SUBSCRIBABLE = "not_subscribable"


class HolderPaused(Exception):
    """The holder cannot keep its subscriptions up; the message is the reason.

    ``code`` names the cause where a caller must tell causes apart; empty
    otherwise.
    """

    def __init__(self, reason: str, code: str = "") -> None:
        super().__init__(reason)
        self.code = code


class GatewayAtCapacity(ConnectionError):
    """The daemon could not start the backend at this moment; reconnect later."""

    def __init__(self, reason: str, retry_after_secs: float) -> None:
        super().__init__(reason)
        self.retry_after_secs = retry_after_secs


class HolderNotLive(Exception):
    """A request was made while the holder had no live connection."""


class HolderListener(Protocol):
    """Where a holder reports events and requests durable state transitions."""

    def on_update(self, uri: str) -> None: ...

    def on_gap(self, uris: list[str]) -> None: ...

    async def on_paused(self, reason: str, uris: list[str]) -> bool:
        """Persist a whole-holder pause; return whether it was published."""
        ...

    async def on_refused(self, uri: str, reason: str) -> bool:
        """Persist a refused URI; return whether it was published."""
        ...


@dataclass(frozen=True)
class HolderSpec:
    """What a holder needs to register as the session's caller again, at any time."""

    session_key: str
    channel_id: str
    server: str
    register: dict[str, Any]
    initialize: dict[str, Any]


def holder_register_frame(spec: HolderSpec) -> dict[str, Any]:
    """The Register a holder sends: the session stub's, as the session's caller.

    A fresh ``stub_uuid`` every time, because the daemon refuses to attach one
    that is already attached, and a stale one could still be on the backend
    after a reconnect.
    """
    frame = copy.deepcopy(spec.register)
    frame.pop("stub_session_token", None)
    caller = build_claim_frame(os.getpid(), spec.session_key, spec.channel_id)["caller"]
    frame.update(
        type="register",
        stub_uuid=str(uuid.uuid4()),
        caller=caller,
        session_key=caller["session_key"],
        session_type=caller["session_type"],
        principal_id=caller["principal_id"],
        channel_id=caller["channel_id"],
        ancestor_pids=[],
    )
    frame.pop("parent_pid", None)
    frame[RESOURCE_HOLDER_FIELD] = True
    return frame


async def _send_line(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
    writer.write(json.dumps(frame).encode("utf-8") + b"\n")
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader, timeout: float) -> dict[str, Any]:
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    if not line:
        raise ConnectionError("gateway closed the connection")
    msg = json.loads(line.decode("utf-8"))
    if not isinstance(msg, dict):
        raise ConnectionError("gateway sent a frame that is not an object")
    return msg


async def _read_answer(reader: asyncio.StreamReader, timeout: float) -> dict[str, Any]:
    """The next frame that is not a daemon control frame, within *timeout* of this call.

    The deadline is checked before every read, so a daemon that sends only
    control frames still times out, even when each one is already buffered.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError("the gateway sent only control frames")
        msg = await _read_frame(reader, remaining)
        if msg.get("type") not in _DAEMON_CONTROL_FRAME_TYPES:
            return msg


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, ConnectionError):
        pass


async def _connect(socket_path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a daemon connection that reads frames as large as the daemon sends."""
    return await asyncio.wait_for(
        transport.connect(socket_path, limit=READ_BUFFER_LIMIT_BYTES), REQUEST_TIMEOUT_SECS
    )


async def describe_session(socket_path: Path, session_key: str, server: str) -> dict[str, Any]:
    """The daemon's ``describe-session`` answer for (*session_key*, *server*)."""
    reader, writer = await _connect(socket_path)
    try:
        await _send_line(
            writer, {"type": "describe-session", "session_key": session_key, "server": server}
        )
        return await _read_frame(reader, REQUEST_TIMEOUT_SECS)
    finally:
        await _close(writer)


def _error_text(reply: dict[str, Any]) -> str:
    error = reply.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def bound_reason(reason: str) -> str:
    """*reason* cut to :data:`REASON_MAX_CHARS`, the cut marked."""
    if len(reason) <= REASON_MAX_CHARS:
        return reason
    return reason[: REASON_MAX_CHARS - len(REASON_CUT_MARKER)] + REASON_CUT_MARKER


def _capacity_wait(retry_after_secs: float) -> float:
    """The wait a ``capacity`` refusal asks for, at most :data:`CAPACITY_RETRY_MAX_SECS`.

    0 for a value that is not finite and positive, so the normal backoff applies.
    """
    if not math.isfinite(retry_after_secs) or retry_after_secs <= 0:
        return 0.0
    return min(retry_after_secs, CAPACITY_RETRY_MAX_SECS)


class _Connection:
    """One registered holder connection and its in-flight requests."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0

    async def request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: float = REQUEST_TIMEOUT_SECS,
    ) -> dict[str, Any]:
        self._next_id += 1
        req_id = f"kc-holder-{self._next_id}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[req_id] = future
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            await _send_line(self.writer, frame)
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(req_id, None)

    def fail_pending(self, exc: BaseException) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(exc)


class ResourceHolder:
    """Keeps one session's subscriptions on one server up across failures."""

    def __init__(
        self,
        spec: HolderSpec,
        socket_path: Callable[[], Optional[Path]],
        listener: HolderListener,
        *,
        uris: tuple[str, ...] = (),
        resumed: bool = False,
        awaiting_subscribe: bool = False,
        ping_interval_secs: float = PING_INTERVAL_SECS,
        reconnect_delay_secs: float = RECONNECT_DELAY_SECS,
        probe_timeout_secs: float = HANDSHAKE_TIMEOUT_SECS,
    ) -> None:
        self.spec = spec
        self._socket_path = socket_path
        self._listener = listener
        self._uris: list[str] = list(uris)
        # A server may place an update immediately after the subscribe response
        # in the same socket write. The pump can read both before subscribe()
        # resumes, so pending URIs hold one update until the verdict is applied.
        self._pending_uris: set[str] = set()
        self._held_updates: set[str] = set()
        # A holder built from stored subscriptions has already missed whatever
        # happened while nobody held them, so its first connection is a gap.
        self._gap_pending = resumed
        # Started for a subscribe that lands once it is live: holding nothing
        # until then is expected, so a refused carried URI does not pause it.
        self._awaiting_subscribe = awaiting_subscribe
        self._ping_interval = ping_interval_secs
        self._reconnect_delay = reconnect_delay_secs
        self._probe_timeout = max(HANDSHAKE_TIMEOUT_SECS, probe_timeout_secs)
        self._conn: Optional[_Connection] = None
        self._live = asyncio.Event()
        self._done = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self.paused_reason = ""
        self.paused_code = ""
        #: Why the latest connection attempt failed, cleared once live, so a
        #: caller that gives up waiting is told what the holder was waiting on.
        self._last_connect_failure = ""
        #: Set when a holder awaiting its first subscribe met a ``capacity``
        #: refusal and stopped, so its caller is refused at once.
        self._start_refusal = ""

    @property
    def uris(self) -> list[str]:
        return list(self._uris)

    @property
    def is_live(self) -> bool:
        return self._live.is_set()

    def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"mcp-resource-holder-{self.spec.server}"
        )

    async def wait_ready(self, timeout: float) -> None:
        """Return once live; raise :class:`HolderPaused` if it paused first."""
        live = asyncio.ensure_future(self._live.wait())
        done = asyncio.ensure_future(self._done.wait())
        try:
            await asyncio.wait({live, done}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            live.cancel()
            done.cancel()
        if self._live.is_set():
            return
        if self.paused_reason:
            raise HolderPaused(self.paused_reason, self.paused_code)
        if self._start_refusal:
            raise HolderPaused(self._start_refusal)
        waited = f"the holder did not connect within {timeout:g}s"
        failure = self._last_connect_failure
        raise HolderPaused(bound_reason(f"{waited}: {failure}" if failure else waited))

    async def subscribe(self, uri: str) -> dict[str, Any]:
        """Subscribe *uri* now; the server's reply, result or error."""
        conn = self._conn
        if conn is None or not self._live.is_set():
            raise HolderNotLive("the holder has no live gateway connection")
        self._pending_uris.add(uri)
        try:
            reply = await self._request(conn, "resources/subscribe", {"uri": uri})
            if "error" in reply:
                self._held_updates.discard(uri)
            elif "result" in reply:
                if uri not in self._uris:
                    self._uris.append(uri)
                if uri in self._held_updates:
                    self._held_updates.discard(uri)
                    self._listener.on_update(uri)
            else:
                self._held_updates.discard(uri)
                self._end_session(conn, "subscribe reply had no result")
                raise ConnectionError("resource subscribe reply did not contain a result")
            return reply
        except asyncio.CancelledError:
            self._held_updates.discard(uri)
            if uri not in self._uris:
                self._end_session(conn, "subscribe request was cancelled")
            raise
        except asyncio.TimeoutError:
            self._held_updates.discard(uri)
            self._end_session(conn, "subscribe request timed out")
            raise
        except BaseException:
            self._held_updates.discard(uri)
            raise
        finally:
            self._pending_uris.discard(uri)

    async def unsubscribe(self, uri: str) -> None:
        if uri not in self._uris:
            return
        self._uris.remove(uri)
        conn = self._conn
        if conn is not None and self._live.is_set():
            try:
                reply = await self._request(conn, "resources/unsubscribe", {"uri": uri})
                if "error" in reply:
                    self._end_session(conn, f"unsubscribe was refused: {_error_text(reply)}")
                elif "result" not in reply:
                    self._end_session(conn, "unsubscribe reply had no result")
            except asyncio.CancelledError:
                self._end_session(conn, "unsubscribe request was cancelled")
                raise
            except asyncio.TimeoutError:
                self._end_session(conn, "unsubscribe request timed out")
            except (OSError, ConnectionError) as exc:
                logger.info(
                    "resource holder: unsubscribe was not answered: %s",
                    _tool_call_error_text({"error": str(exc)}) or "?",
                )

    def _end_session(self, conn: _Connection, what: str) -> None:
        """End *conn* when what the server holds may differ from :attr:`uris`.

        The server may have applied a timed-out subscribe, or kept a refused,
        malformed or timed-out unsubscribe, and nothing here would track that
        upstream subscription. Closing the socket ends the read loop, so
        ``_run`` reconnects and subscribes from :attr:`uris` alone; the daemon
        releases what the detached connection held.
        """
        logger.warning(
            "resource holder for %s on %r ended its connection: %s; reconnecting",
            _log_safe_identifier(self.spec.session_key),
            _log_safe_identifier(self.spec.server),
            _tool_call_error_text({"error": what}) or "?",
        )
        self._live.clear()
        conn.fail_pending(ConnectionError("holder connection ended"))
        conn.writer.close()

    async def _request(
        self,
        conn: _Connection,
        method: str,
        params: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send one request, reconnecting on a daemon lost-backend answer."""
        reply = await conn.request(method, params, **kwargs)
        error = reply.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        if (
            isinstance(error, dict)
            and error.get("code") == _DAEMON_ERROR_CODE
            and isinstance(message, str)
            and message.startswith(_LOST_BACKEND_PREFIXES)
        ):
            what = f"{method} received a daemon lost-backend answer"
            self._end_session(conn, what)
            raise ConnectionError(what)
        return reply

    async def close(self) -> None:
        """Drop the connection. The daemon's detach releases what it held."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._live.clear()
        self._done.set()

    async def _run(self) -> None:
        delay = self._reconnect_delay
        try:
            while True:
                path = self._socket_path()
                if path is None:
                    self._last_connect_failure = "the MCP gateway is not running"
                else:
                    try:
                        await self._session(path)
                        delay = self._reconnect_delay
                    except HolderPaused as paused:
                        if await self._pause(str(paused), paused.code):
                            return
                        logger.warning(
                            "resource holder for %s on %r could not persist its pause; retrying",
                            _log_safe_identifier(self.spec.session_key),
                            _log_safe_identifier(self.spec.server),
                        )
                    except (OSError, ConnectionError, asyncio.TimeoutError, ValueError) as exc:
                        self._last_connect_failure = bound_reason(str(exc) or type(exc).__name__)
                        logger.info(
                            "resource holder for %s on %r lost its connection: %s",
                            _log_safe_identifier(self.spec.session_key),
                            _log_safe_identifier(self.spec.server),
                            _tool_call_error_text({"error": str(exc)}) or "?",
                        )
                        if isinstance(exc, GatewayAtCapacity):
                            if self._awaiting_subscribe:
                                # Nothing is up yet, and the subscribe that started
                                # this holder holds the service's lock while it waits:
                                # it hears the refusal now, and nothing is paused.
                                self._start_refusal = self._last_connect_failure
                                return
                            delay = max(delay, _capacity_wait(exc.retry_after_secs))
                    except Exception as exc:  # noqa: BLE001 - a fault must pause, not end silently
                        # Not CancelledError: that is a BaseException and ends the task.
                        if await self._pause(f"the holder failed: {exc!r}"):
                            return
                self._live.clear()
                self._gap_pending = True
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_DELAY_MAX_SECS)
        finally:
            self._live.clear()
            self._done.set()

    async def _pause(self, reason: str, code: str = "") -> bool:
        reason = bound_reason(reason)
        persisted = await self._listener.on_paused(reason, list(self._uris))
        if not persisted:
            return False
        self.paused_reason = reason
        self.paused_code = code
        logger.warning(
            "resource holder for %s on %r paused: %s",
            _log_safe_identifier(self.spec.session_key),
            _log_safe_identifier(self.spec.server),
            _tool_call_error_text({"error": reason}) or "?",
        )
        return True

    async def _session(self, path: Path) -> None:
        reader, writer = await _connect(path)
        conn = _Connection(reader, writer)
        pump: Optional[asyncio.Task[None]] = None
        pinger: Optional[asyncio.Task[None]] = None
        try:
            await self._handshake(conn)
            pump = asyncio.create_task(self._pump(conn))
            await self._initialize(conn)
            held = bool(self._uris)
            await self._resubscribe(conn)
            if held and not self._uris and not self._awaiting_subscribe:
                # Live with nothing subscribed would still pin its backend (§4.6).
                raise HolderPaused(f"{self.spec.server!r} refused every subscription")
            self._awaiting_subscribe = False
            self._conn = conn
            self._last_connect_failure = ""
            self._live.set()
            if self._gap_pending:
                self._gap_pending = False
                self._listener.on_gap(list(self._uris))
            pinger = asyncio.create_task(self._ping_loop(conn))
            done, _pending = await asyncio.wait((pump, pinger), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            self._live.clear()
            self._conn = None
            self._held_updates.difference_update(self._pending_uris)
            self._pending_uris.clear()
            child_tasks = [task for task in (pinger, pump) if task is not None]
            for child_task in child_tasks:
                if not child_task.done():
                    child_task.cancel()
            conn.fail_pending(ConnectionError("holder connection closed"))
            await _close(writer)
            if child_tasks:
                await asyncio.gather(*child_tasks, return_exceptions=True)

    async def _handshake(self, conn: _Connection) -> None:
        await _send_line(conn.writer, holder_register_frame(self.spec))
        registered = await _read_answer(conn.reader, HANDSHAKE_TIMEOUT_SECS)
        if registered.get("type") != "registered":
            raise HolderPaused(
                f"the gateway refused to register the holder: {registered.get('reason') or registered}"
            )
        await _send_line(conn.writer, {"type": "ensure_backend"})
        ready = await _read_answer(conn.reader, HANDSHAKE_TIMEOUT_SECS)
        if ready.get("type") == "rejected" and ready.get("class") == _REJECT_CLASS_CAPACITY:
            # The host or the moment, never the target: try again later.
            retry_after = ready.get("retry_after_secs")
            raise GatewayAtCapacity(
                f"the gateway is at capacity for {self.spec.server!r}: {ready.get('reason') or ready}",
                float(retry_after) if isinstance(retry_after, (int, float)) else 0.0,
            )
        if ready.get("type") != "ready":
            raise HolderPaused(
                f"the gateway could not start {self.spec.server!r}: {ready.get('reason') or ready}"
            )

    async def _initialize(self, conn: _Connection) -> None:
        params = self.spec.initialize.get("params")
        reply = await self._request(conn, "initialize", params if isinstance(params, dict) else {})
        if "error" in reply:
            raise HolderPaused(f"{self.spec.server!r} refused initialize: {_error_text(reply)}")
        result = reply.get("result")
        capabilities = result.get("capabilities") if isinstance(result, dict) else None
        resources = capabilities.get("resources") if isinstance(capabilities, dict) else None
        if not (isinstance(resources, dict) and resources.get("subscribe")):
            raise HolderPaused(
                f"{self.spec.server!r} does not declare resources.subscribe",
                PAUSED_NOT_SUBSCRIBABLE,
            )
        await _send_line(conn.writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _resubscribe(self, conn: _Connection) -> None:
        # ``unsubscribe`` sends nothing until the holder is live, so a URI it
        # drops during these awaits is skipped here or released below.
        accepted: list[str] = []
        for uri in list(self._uris):
            if uri not in self._uris:
                continue
            reply = await self._request(conn, "resources/subscribe", {"uri": uri})
            if "error" in reply:
                text = _error_text(reply)
                await self._refuse(
                    uri, bound_reason(f"{self.spec.server!r} refused the subscribe: {text}")
                )
            elif "result" not in reply:
                # The server's own answer, so retrying it would never end: the
                # URI pauses like a refusal. The server may still hold it, so
                # the connection ends and the daemon's detach releases it.
                await self._refuse(
                    uri,
                    bound_reason(f"{self.spec.server!r} answered the subscribe without a verdict"),
                )
                if not self._uris and not self._awaiting_subscribe:
                    raise HolderPaused(f"{self.spec.server!r} refused every subscription")
                self._end_session(conn, "re-subscribe reply had no result")
                raise ConnectionError("resource re-subscribe reply did not contain a result")
            else:
                accepted.append(uri)
        await self._release_untracked(conn, accepted)

    async def _release_untracked(self, conn: _Connection, accepted: list[str]) -> None:
        """Unsubscribe each *accepted* URI that :attr:`uris` dropped, before going live.

        Repeated until none is left, because an unsubscribe can land during
        these awaits too. A reply without an explicit result ends the
        connection, as :meth:`unsubscribe` does.
        """
        while stale := [uri for uri in accepted if uri not in self._uris]:
            for uri in stale:
                accepted.remove(uri)
                reply = await self._request(conn, "resources/unsubscribe", {"uri": uri})
                if "error" in reply:
                    what = f"unsubscribe was refused: {_error_text(reply)}"
                elif "result" not in reply:
                    what = "unsubscribe reply had no result"
                else:
                    continue
                self._end_session(conn, what)
                raise ConnectionError("a re-subscribe no longer tracked could not be released")

    async def _refuse(self, uri: str, reason: str) -> None:
        """Record that the server will not hold *uri*, and stop tracking it."""
        if not await self._listener.on_refused(uri, reason):
            raise ConnectionError("could not persist a refused resource subscription")
        # An unsubscribe that landed while the refusal was recorded took it already.
        if uri in self._uris:
            self._uris.remove(uri)

    async def _ping_loop(self, conn: _Connection) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await self._probe_backend(conn)

    async def _probe_backend(self, conn: _Connection) -> None:
        """Forward one MCP ``ping`` using the backend-start request bound."""
        await self._request(conn, "ping", timeout=self._probe_timeout)

    async def _pump(self, conn: _Connection) -> None:
        """Read until end of file, never waiting on anything but the socket."""
        while True:
            line = await conn.reader.readline()
            if not line:
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            await self._dispatch(conn, msg)

    async def _dispatch(self, conn: _Connection, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")
        if method is None and isinstance(msg_id, str) and msg_id in conn.pending:
            future = conn.pending[msg_id]
            if not future.done():
                future.set_result(msg)
            return
        if method == "notifications/resources/updated":
            params = msg.get("params")
            uri = params.get("uri") if isinstance(params, dict) else None
            if isinstance(uri, str):
                if uri in self._uris:
                    self._listener.on_update(uri)
                elif uri in self._pending_uris:
                    self._held_updates.add(uri)
            return
        if method == "ping" and msg_id is not None:
            # MCP requires an empty result for a ping, whoever sends it.
            await _send_line(conn.writer, {"jsonrpc": "2.0", "id": msg_id, "result": {}})
            return
        if method is not None and msg_id is not None:
            # A server-to-client request (roots, sampling, elicitation): the
            # holder has no user to ask, so it declines rather than leave the
            # server waiting.
            await _send_line(
                conn.writer,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": "not supported"},
                },
            )
