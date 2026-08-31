"""Agent-role JSON-RPC 2.0 framing over newline-delimited streams.

``kiro_crew.acp.runtime`` owns the *client* half of ACP: it spawns kiro-cli and
talks to it. This module is the mirror image — the framing an ACP **agent** needs
when an editor (VS Code, Zed) spawns Kiro Crew and drives it over stdio.

Only framing and id correlation live here; method semantics belong to
``kiro_crew.acp_server.server``. The reader/writer are injected so tests can
drive a full conversation over in-memory pipes without a subprocess.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Protocol

from kiro_crew.acp.types import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    JsonRpcMessage,
)

logger = logging.getLogger(__name__)


def _safe_id(value: Any) -> Any:
    """Return *value* if it is a usable JSON-RPC id, else ``None``.

    A JSON-RPC id is a string or a number (never a bool, which is an ``int``
    subclass). An error whose triggering frame carried no usable id is answered
    with ``id: null`` per the spec.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


# A peer that never answers a request blocks us forever, so outbound requests
# (the only ones we await) are bounded.
DEFAULT_REQUEST_TIMEOUT = 120.0

# Editors can place screenshots and embedded resources in one JSON-RPC line.
# Raise asyncio's 64 KiB default while retaining a finite per-frame ceiling.
ACP_FRAME_LIMIT_BYTES = 10 * 1024 * 1024
_OVERSIZE_DRAIN_MAX_BYTES = 4 * ACP_FRAME_LIMIT_BYTES

# Grace period for in-flight handlers after the peer closes the pipe. A handler
# mid-tool should get a chance to finish; one that ignores cancellation must not
# hold the process open forever.
DRAIN_TIMEOUT = 5.0


async def _drain_oversize_frame(
    reader: asyncio.StreamReader, exc: asyncio.LimitOverrunError
) -> bool:
    """Discard one oversized line and stop exactly at its newline boundary."""
    discarded = 0
    while True:
        if exc.consumed <= 0:
            return False
        try:
            discarded += len(await reader.readexactly(exc.consumed))
        except asyncio.IncompleteReadError:
            return False
        if discarded > _OVERSIZE_DRAIN_MAX_BYTES:
            return False
        try:
            await reader.readuntil(b"\n")
            return True
        except asyncio.LimitOverrunError as again:
            exc = again
        except asyncio.IncompleteReadError:
            return False


RequestHandler = Callable[[str, dict[str, Any], Any], Awaitable[None]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class FrameWriter(Protocol):
    """The subset of ``asyncio.StreamWriter`` this transport needs."""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


class AgentTransport:
    """Newline-delimited JSON-RPC 2.0 framing for the agent side of ACP.

    Two independent id namespaces meet on this pipe: the peer's client->agent
    request ids, and our own agent->client request ids (``session/request_permission``).
    They collide on small integers, so response correlation requires ``id`` match
    **and** ``method is None`` — the same discipline ``JsonRpcMessage.is_response_for``
    enforces on the client side. Without it, an inbound request whose id happens to
    equal an in-flight outbound request is misread as that request's response.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: FrameWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False
        # Retained strong refs to in-flight request handlers. asyncio only
        # holds a weak reference to a running task, so a fire-and-forget task
        # can be garbage-collected mid-flight and silently never finish.
        self._tasks: set[asyncio.Task[None]] = set()

    # ── outbound ──

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._closed:
            logger.debug("transport closed, dropping outbound %s", payload.get("method"))
            return
        line = json.dumps(payload) + "\n"
        # Serialised so two concurrent senders cannot interleave partial frames.
        async with self._write_lock:
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()

    async def send_result(self, req_id: Any, result: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def send_error(self, req_id: Any, code: int, message: str) -> None:
        await self._send(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        )

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def send_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        """Send an agent->client request and await its response.

        Raises ``asyncio.TimeoutError`` if the peer never answers, and
        ``AcpServerError`` if it answers with a JSON-RPC error.
        """
        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    # ── inbound ──

    async def run(
        self,
        on_request: RequestHandler,
        on_notification: NotificationHandler,
    ) -> None:
        """Read frames until EOF, dispatching to the supplied handlers.

        Returns when the peer closes stdin. Handler exceptions are contained: a
        request that raises is answered with an internal-error response rather
        than killing the read loop and stranding the peer.
        """
        while True:
            eof = False
            try:
                line = await self._reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                recovered = await _drain_oversize_frame(self._reader, exc)
                logger.warning(
                    "oversized JSON-RPC frame; replying -32600%s",
                    "" if recovered else " and closing the pipe",
                )
                await self.send_error(None, JSONRPC_INVALID_REQUEST, "Frame too large")
                if not recovered:
                    break
                continue
            except asyncio.IncompleteReadError as exc:
                line = exc.partial
                eof = True
            except ConnectionResetError:
                break
            if not line:  # EOF — the editor closed the pipe.
                break
            text = line.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                # Parse error. No id can be recovered from unparseable bytes, so
                # answer with id=null per JSON-RPC 2.0. Not fatal: one bad line
                # from a noisy peer must not end the session.
                logger.warning("malformed JSON-RPC frame (%d bytes); replying -32700", len(text))
                await self.send_error(None, JSONRPC_PARSE_ERROR, "Parse error")
                continue
            if not isinstance(data, dict):
                # Valid JSON, wrong shape (array/scalar). An Invalid Request with
                # no determinable id.
                logger.warning("non-object JSON-RPC frame; replying -32600")
                await self.send_error(None, JSONRPC_INVALID_REQUEST, "Invalid Request")
                continue
            if data.get("jsonrpc") != "2.0":
                logger.warning("frame with missing/invalid 'jsonrpc'; replying -32600")
                await self.send_error(
                    _safe_id(data.get("id")),
                    JSONRPC_INVALID_REQUEST,
                    "Invalid Request: 'jsonrpc' must be '2.0'",
                )
                continue
            await self._dispatch(JsonRpcMessage.from_dict(data), on_request, on_notification)
            if eof:
                break

        self._fail_pending(ConnectionError("peer closed the ACP pipe"))
        await self._drain_tasks()

    async def _dispatch(
        self,
        msg: JsonRpcMessage,
        on_request: RequestHandler,
        on_notification: NotificationHandler,
    ) -> None:
        params = msg.params if isinstance(msg.params, dict) else {}

        # Response to one of OUR outbound requests: id set, method absent.
        # A JSON-RPC id MUST be a string or number; a bool is an ``int``
        # subclass with ``hash(True) == hash(1)`` and ``True == 1``, so a
        # malformed ``{"id": true, ...}`` response would otherwise resolve an
        # in-flight outbound request whose id is 1. Reject a non-string/number id
        # as unusable rather than mis-correlating it.
        if msg.method is None and msg.id is not None:
            if _safe_id(msg.id) is None:
                logger.warning("response frame with non-string/number id %r; dropping", msg.id)
                return
            self._resolve(msg)
            return

        if msg.method is None:
            # Neither a method (so not a request or notification) nor a usable id
            # (so not a response): an Invalid Request per JSON-RPC 2.0.
            logger.warning("frame with neither method nor id; replying -32600")
            await self.send_error(None, JSONRPC_INVALID_REQUEST, "Invalid Request")
            return

        if not isinstance(msg.method, str):
            await self.send_error(
                _safe_id(msg.id),
                JSONRPC_INVALID_REQUEST,
                "Invalid Request: 'method' must be a string",
            )
            return

        # Notification: method, no id. Nothing to answer.
        if msg.id is None:
            await on_notification(msg.method, params)
            return

        # A request carries an id that MUST be a string or number.
        if _safe_id(msg.id) is None:
            await self.send_error(
                None, JSONRPC_INVALID_REQUEST, "Invalid Request: 'id' must be a string or number"
            )
            return

        # Dispatched as a task, never awaited inline. A handler may itself await
        # inbound data — session/request_permission waits on the editor's
        # answer — so awaiting it here would stop draining the pipe and
        # deadlock on the very frame the handler needs. It would also make
        # session/cancel unobservable until the turn it cancels had ended.
        task = asyncio.create_task(self._run_request(msg.method, params, msg.id, on_request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_request(
        self,
        method: str,
        params: dict[str, Any],
        req_id: Any,
        on_request: RequestHandler,
    ) -> None:
        try:
            await on_request(method, params, req_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never leave a request unanswered — the peer would block forever.
            logger.exception("handler for %s failed", method)
            await self.send_error(req_id, JSONRPC_INTERNAL_ERROR, "Internal error")

    async def _drain_tasks(self) -> None:
        """Let in-flight handlers finish once the pipe is gone, then drop them."""
        if not self._tasks:
            return
        _done, still = await asyncio.wait(list(self._tasks), timeout=DRAIN_TIMEOUT)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    def _resolve(self, msg: JsonRpcMessage) -> None:
        if _safe_id(msg.id) is None:
            # A bool/None/other unusable id: never correlate it to a pending
            # request (True would collide with the integer id 1). Belt-and-braces
            # alongside the _dispatch guard.
            logger.debug("ignoring response with unusable id %r", msg.id)
            return
        fut = self._pending.get(msg.id)
        if fut is None or fut.done():
            # A response for an id we are no longer awaiting (timed out, or the
            # peer echoed something unsolicited). Dropping is correct.
            logger.debug("ignoring response for unknown id %r", msg.id)
            return
        if msg.error is not None:
            fut.set_exception(AcpServerError(str(msg.error)))
        else:
            fut.set_result(msg.result)

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def close(self) -> None:
        self._closed = True
        self._fail_pending(ConnectionError("transport closed"))


class AcpServerError(RuntimeError):
    """The peer answered an agent->client request with a JSON-RPC error."""
