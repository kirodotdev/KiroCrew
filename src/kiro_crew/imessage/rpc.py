"""Newline-framed JSON-RPC 2.0 over a child process's stdio.

The ``imsg`` bridge is spoken to the way a language server is: one long-lived
child, one compact JSON object per line on stdin, one per line on stdout. This
module owns only that framing — request/response correlation, notification
routing, and child lifecycle — and knows nothing about iMessage semantics.

Two framing details are load-bearing:

* **The stream limit is raised well above asyncio's 64 KiB default.** A single
  inbound line can carry a large message payload, and the default limit turns
  an oversized line into a reader-killing ``LimitOverrunError`` — i.e. a
  channel that goes silent rather than dropping one message.
* **A line that cannot be parsed is dropped, not fatal.** The child reserves
  stderr for diagnostics, but a truncated or non-JSON stdout line must not take
  the reader task down and strand every pending call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

#: Max bytes in one stdout line. The bridge folds a whole message (and, for
#: other methods, whole chat listings) into a single line, so the asyncio
#: default of 64 KiB is far too small: exceeding it raises inside the reader
#: and kills inbound delivery entirely.
STDOUT_LINE_LIMIT = 8 * 1024 * 1024

#: How long a single call waits for its response before giving up. The bridge
#: serializes mutations on one FIFO worker, so a send can queue behind another.
DEFAULT_CALL_TIMEOUT_S = 30.0

#: How long ``close`` waits for the child to honour the stdin-EOF exit contract
#: before escalating to a kill, and again after killing. Module-level so a test
#: can exercise the escalation path without a real wall-clock wait.
EXIT_GRACE_S = 5.0

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class RpcError(Exception):
    """A JSON-RPC error response from the bridge."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class RpcTransportError(Exception):
    """The child is gone, never started, or stopped answering."""


class JsonRpcPeer:
    """A child process speaking newline-framed JSON-RPC 2.0 on stdio.

    Calls are correlated by a monotonically increasing integer id. Anything
    arriving with a ``method`` and no ``id`` is a notification and is handed to
    ``on_notification``; the peer never replies to one.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        on_notification: NotificationHandler | None = None,
        on_disconnect: "Callable[[str], None] | None" = None,
        cwd: str | None = None,
    ) -> None:
        self._argv = list(argv)
        self._on_notification = on_notification
        # Called once when the reader ends for a reason that is NOT our own
        # close(). Without it a bridge that exits after startup leaves the reader
        # simply gone: outstanding calls fail, but nothing marks the channel down
        # and nothing retries, so inbound stops while the dashboard still says
        # connected -- silent for the rest of the process's life.
        self._on_disconnect = on_disconnect
        self._cwd = cwd
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        # Notifications are dispatched OFF the reader, as one task each.
        #
        # The reader owns stdout, so it is the only thing that can resolve a
        # pending call's future. Awaiting a notification handler inline therefore
        # deadlocks the moment that handler issues a call of its own -- which the
        # inbound path always does, because answering a message sends one.
        #
        # A task per notification with a tracked set is the pattern every other
        # channel in this repo uses for exactly this (see webex/client.py's
        # `_handler_tasks`). The set only pins the tasks against garbage
        # collection; it does not serialize them. Ordering is NOT a property this
        # layer provides, and the channel above does not need it: two messages
        # for the same conversation are serialized by the per-session semaphore
        # in `sessions.get_or_create`, and messages for different conversations
        # have no ordering relationship worth preserving.
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._closing = False
        self.last_stderr = ""

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        """Spawn the child and begin reading its stdout."""
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                limit=STDOUT_LINE_LIMIT,
            )
        except (OSError, ValueError) as exc:
            raise RpcTransportError(f"cannot spawn {self._argv[0]!r}: {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def close(self) -> None:
        """Close stdin (the child's documented clean exit) and reap it."""
        self._closing = True
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except (OSError, RuntimeError):
                logger.debug("imessage rpc: stdin close failed", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # The child ignored the EOF contract; escalate rather than leak it.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            with_suppressed = asyncio.shield(proc.wait())
            try:
                await asyncio.wait_for(with_suppressed, timeout=EXIT_GRACE_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("imessage rpc: child did not exit after kill")
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        # In-flight handlers are cancelled too: the peer is going away, so a turn
        # still mid-delivery cannot finish, and leaving the tasks running would
        # let them send on a closed bridge. Cancelling loses those messages, which
        # is the at-most-once contract this channel documents.
        for handler_task in list(self._handler_tasks):
            handler_task.cancel()
        self._handler_tasks.clear()
        self._fail_pending(RpcTransportError("bridge closed"))
        self._reader_task = None
        self._stderr_task = None
        self._proc = None

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send a request and await its result.

        Raises :class:`RpcError` for an error response and
        :class:`RpcTransportError` when the child is unusable or silent.
        """
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise RpcTransportError("bridge is not running")
        self._next_id += 1
        req_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        # The bridge rejects a null/array params; omit the key instead.
        if params:
            payload["params"] = params
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        try:
            async with self._write_lock:
                proc.stdin.write(line)
                await proc.stdin.drain()
        except (OSError, RuntimeError, ConnectionResetError) as exc:
            self._pending.pop(req_id, None)
            raise RpcTransportError(f"write to bridge failed: {exc}") from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise RpcTransportError(f"{method} timed out after {timeout:.0f}s") from exc
        finally:
            self._pending.pop(req_id, None)

    # -- internals ----------------------------------------------------------

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        while True:
            try:
                raw = await stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                # A line past the (already generous) limit. Drop it and keep
                # reading: the alternative is a permanently silent channel.
                logger.warning("imessage rpc: oversized stdout line dropped (%s)", exc)
                continue
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            if not raw:
                break
            try:
                frame = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                logger.warning("imessage rpc: unparseable stdout line dropped")
                continue
            if not isinstance(frame, dict):
                continue
            try:
                await self._route(frame)
            except Exception:
                # Routing itself is now cheap (resolve a future, or enqueue a
                # notification), so this guards a malformed frame rather than a
                # handler -- handler failures are caught in `_notify_loop`.
                logger.exception("imessage rpc: routing a frame failed")
        if not self._closing:
            reason = "bridge exited"
            rc = proc.returncode
            if rc is not None:
                reason = f"bridge exited (code {rc})"
            if self.last_stderr:
                reason = f"{reason}: {self.last_stderr}"
            self._fail_pending(RpcTransportError("bridge exited"))
            if self._on_disconnect is not None:
                try:
                    self._on_disconnect(reason)
                except Exception:
                    # The notification is best-effort; failing it must not leave
                    # the reader's own teardown half-done.
                    logger.exception("imessage rpc: disconnect handler failed")

    async def _route(self, frame: dict[str, Any]) -> None:
        frame_id = frame.get("id")
        if frame_id is not None and ("result" in frame or "error" in frame):
            future = self._pending.pop(_as_int(frame_id), None)
            if future is None or future.done():
                return
            error = frame.get("error")
            if isinstance(error, dict):
                future.set_exception(
                    RpcError(
                        _as_int(error.get("code")) or -32603,
                        str(error.get("message") or "unknown error"),
                        error.get("data"),
                    )
                )
                return
            result = frame.get("result")
            future.set_result(result if isinstance(result, dict) else {})
            return
        method = frame.get("method")
        if isinstance(method, str) and method and self._on_notification is not None:
            params = frame.get("params")
            # Hand off, never await: awaiting here deadlocks the reader against
            # its own pending calls (see `_handler_tasks`).
            task = asyncio.create_task(
                self._invoke_notification(method, params if isinstance(params, dict) else {})
            )
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _invoke_notification(self, method: str, params: dict[str, Any]) -> None:
        handler = self._on_notification
        if handler is None:
            return
        try:
            await handler(method, params)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Swallowed, matching every other channel: the message is dropped and
            # logged. Delivery is at-most-once by design -- see the client's
            # cursor docstring -- so there is nothing to hold back or retry here.
            logger.exception("imessage rpc: notification handler failed")

    async def _drain_stderr(self) -> None:
        """Keep the child's diagnostic pipe from filling and blocking it."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            try:
                raw = await proc.stderr.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            if not raw:
                break
            text = raw.decode("utf-8", "replace").strip()
            if text:
                self.last_stderr = text[:400]
                logger.debug("imsg stderr: %s", self.last_stderr)

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def _as_int(value: Any) -> int:
    """Coerce a JSON-RPC id/code to int, or 0 when it is not numeric."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
