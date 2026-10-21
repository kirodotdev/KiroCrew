"""JSON-RPC over the child's stdin.

Requests, notifications, responses and errors under the one write lock and the no-progress
bound, the answer futures, and the awaited control-plane request with its one-at-a-time
bookkeeping.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_last_activity``, ``_next_id``, ``_pending_requests``,
``_routed_requests``, ``_stdin_lock``.

Instance fields read: ``_acp_backend``, ``_dead``, ``_native_skill_projection``,
``_one_at_a_time_sent``, ``_process``, ``_session_queues``, ``_stderr_lines``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.acp.client import _loggable_request_id
from kiro_crew.acp.runtime_start import _cold_start_counts
from kiro_crew.acp.session_handle import AcpRequestTimeout, AcpRuntimeDead
from kiro_crew.acp.transport_framing import (
    _RESPONSE_WRITE_BOUND_SECS,
    RequestWriteResult,
    _stall_window_phrase,
    encode_frame,
    response_write_window_secs,
    write_notification_best_effort,
    write_request_frame_bounded,
    write_response_frame_bounded,
)
from kiro_crew.acp.types import (
    ACP_BACKENDS_SERIAL_SESSION_STARTS,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SET_MODE,
    JsonRpcRequest,
)

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


_REQUEST_TIMEOUT = 30.0


class _PendingRequests(dict):
    """``{req_id: Future}`` for awaited control-plane requests, with ownership transfer.

    A request that timed out is normally popped: nobody will read its answer.
    ``adopt(req_id)`` is the other choice -- keep the entry so the late answer
    still resolves the future, and record who owns it now. The reader loop
    keeps popping on response exactly as before; only the timeout path changes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.adopted: set[int] = set()

    def adopt(self, req_id: int) -> "asyncio.Future[dict[str, Any]] | None":
        future = self.get(req_id)
        if future is None:
            return None
        self.adopted.add(req_id)
        return future

    def pop(self, key, default=None):  # type: ignore[override]
        self.adopted.discard(key)
        return super().pop(key, default)

    def clear(self) -> None:
        self.adopted.clear()
        super().clear()


# Requests a backend in ACP_BACKENDS_SERIAL_SESSION_STARTS answers one at a time.
# kiro-cli (2.26 and earlier) awaits each of these inside its ACP connection's
# dispatch loop, so while one waits on a slow MCP server or the model list, every
# message behind it waits too, including the next session start. A start sent
# behind one therefore spends its own budget on the earlier request.
_ONE_AT_A_TIME_METHODS = frozenset({METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SET_MODE})


class AcpRuntimeStdinStalled(AcpRuntimeDead):
    """The shared stdin made no progress for the response-write bound.

    A subclass so the off-loop auto-answer can keep its ``send_stalled`` audit
    reason (the bound fires inside ``send_response``, ahead of that path's own
    outer timeout) while every ``except AcpRuntimeDead`` handler keeps working.
    """


# Named by the path callers import them from: an error chain renders each raised class
# as ``module.qualname`` (``kiro_crew.subagent._describe_exception``), and that text
# reaches the Subagents panel and tracebacks, so this module's name must not leak into it.
AcpRuntimeStdinStalled.__module__ = "kiro_crew.acp.runtime"


async def send_request(
    self: AcpRuntime,
    method: str,
    params: dict[str, Any],
    *,
    on_reserved: "Callable[[int], None] | None" = None,
) -> int:
    """Send a JSON-RPC request and return the request id.

    The response will be routed to the session's queue (via _routed_requests)
    so AcpSessionHandle can detect turn completion. For requests that need
    an immediate response (init, session/new), use _send_and_await instead.

    ``on_reserved`` is called with the id BEFORE the write, whose ``drain()``
    can suspend: the response can reach the session queue during that
    suspension, so a caller that must claim it registers the id by then.
    """
    if not self._process or not self._process.stdin:
        raise AcpRuntimeDead("process not running")
    if self._dead:
        raise AcpRuntimeDead("runtime is dead")

    projection = getattr(self, "_native_skill_projection", None)
    if projection is not None:
        params = projection.request(method, params)
    req_id = self._next_id
    self._next_id += 1

    # Register for session routing so the response goes to the right queue
    session_id = params.get("sessionId")
    if session_id and session_id in self._session_queues:
        self._routed_requests[req_id] = session_id
    if on_reserved is not None:
        on_reserved(req_id)

    req = JsonRpcRequest(method=method, params=params, id=req_id)
    data = encode_frame(req.to_dict())

    try:
        # Bounded on the reader's PROGRESS, not held across a raw drain: on
        # the shared stdin a request write that parks while kiro-cli is
        # flow-control-paused must not hold the write lock and wedge every
        # other session. On a stall _write_request_bounded marks the runtime
        # dead and raises AcpRuntimeStdinStalled (an AcpRuntimeDead) -- caught
        # below so the routing registration is dropped like any other death.
        await self._write_request_bounded(data, req_id, method)
    except AcpRuntimeDead:
        self._routed_requests.pop(req_id, None)
        raise
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._routed_requests.pop(req_id, None)
        self._mark_dead(f"pipe broken: {exc}")
        raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    self._last_activity = time.monotonic()
    return req_id


async def send_notification(self: AcpRuntime, method: str, params: dict[str, Any]) -> None:
    """Send a JSON-RPC notification (no id, no response expected).

    Unlike send_request, this does NOT allocate an id or register routing,
    so it leaves no _routed_requests entry to leak when the server (per the
    ACP spec) sends no response back (e.g. session/cancel).
    """
    if not self._process or not self._process.stdin:
        raise AcpRuntimeDead("process not running")
    if self._dead:
        raise AcpRuntimeDead("runtime is dead")

    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    data = encode_frame(msg)

    try:
        # A notification (session/cancel) is fire-and-forget and is the one
        # signal that can end a wedged turn: wait for the lock under the
        # no-progress bound, append unlocked if it does not come, and never
        # park on a drain behind a reader that stopped.
        outcome = await write_notification_best_effort(
            self._process.stdin,
            self._stdin_write_lock(),
            data,
            bound_secs=_RESPONSE_WRITE_BOUND_SECS,
            before_write=self._refuse_write_if_dead,
        )
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._mark_dead(f"pipe broken: {exc}")
        raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    # Only a drained frame is evidence the backend moved: an unlocked append
    # or a stall must not refresh the activity clock the idle/wedged-turn
    # probes read -- that would defer the probe at the very moment a failed
    # write showed the backend is not reading.
    if outcome == "drained":
        self._last_activity = time.monotonic()
    else:
        logger.debug(
            "send_notification method=%s: %s; activity clock not refreshed",
            _loggable_request_id(method),
            outcome,
        )


def _refuse_write_if_dead(self: AcpRuntime) -> None:
    """Re-check under the write lock, right before a frame goes out.

    A writer that queued for the lock while a sibling's stall marked the
    runtime dead must not write into the pipe afterwards: ``_mark_dead``
    has already failed every pending request and poisoned every session
    queue, and the recovery path requeues the turn -- a prompt written now
    would run a second time. Every runtime writer calls this immediately
    before writing: under the lock when it holds one, and on the unlocked
    best-effort notification append as well.
    """
    if self._dead:
        raise AcpRuntimeDead("runtime is dead")


def _stdin_write_lock(self: AcpRuntime) -> asyncio.Lock:
    """The one lock every stdin write on this runtime takes (see
    ``await_under_no_progress_bound`` in transport_framing for why the bound needs
    it). Created on first use so a runtime built without ``__init__`` (test
    doubles) has one."""
    lock = getattr(self, "_stdin_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        self._stdin_lock = lock
    return lock


async def _write_response_bounded(
    self: AcpRuntime,
    data: bytes,
    request_id: str | int,
    *,
    before_write: Callable[[], None] | None = None,
    withdrawal_check: Callable[[], None] | None = None,
) -> None:
    """Write a response/error frame under the write lock and a no-progress bound.

    Mirror of ``AcpClient._write_response_bounded`` for the shared runtime.
    Every multiplexed session shares this one stdin, so a response can wait
    behind another session's multi-MB prompt frame on a flow-control-paused
    writer while the backend is perfectly healthy; the shared helper only
    gives up when the write buffer has not shrunk for
    ``_RESPONSE_WRITE_BOUND_SECS`` -- nothing consumed, reader gone -- which
    on a shared pipe is a dead runtime, not a slow session. Marks the runtime
    dead (resolving every pending wait by teardown) and raises
    ``AcpRuntimeDead``, which the session provider translates to
    ``AcpProcessDied`` so the caller takes the same session-reset +
    bounded-requeue recovery the broken-pipe case uses. The request id
    appears only through ``_loggable_request_id``. A caller's
    ``before_write`` runs under the lock after the dead-runtime check, and
    its ``withdrawal_check`` ahead of it (see :meth:`send_response`).
    """
    assert self._process is not None and self._process.stdin is not None

    def checks() -> None:
        # A frame its caller withdrew is not written, whatever the pipe's
        # state; the caller's other check is read on a live runtime only, so
        # a runtime that died while this writer waited reports the death.
        if withdrawal_check is not None:
            withdrawal_check()
        self._refuse_write_if_dead()
        if before_write is not None:
            before_write()

    if await write_response_frame_bounded(
        self._process.stdin,
        self._stdin_write_lock(),
        data,
        bound_secs=_RESPONSE_WRITE_BOUND_SECS,
        before_write=checks,
    ):
        return
    safe_id = _loggable_request_id(request_id)
    window = response_write_window_secs(self._process.stdin, _RESPONSE_WRITE_BOUND_SECS)
    logger.warning(
        "ACP runtime stdin stalled: %s "
        "while delivering response to req=%s; marking runtime dead",
        _stall_window_phrase(self._process.stdin, window),
        safe_id,
    )
    self._mark_dead("response write stalled (backend not reading stdin)", stdin_stalled=True)
    raise AcpRuntimeStdinStalled(
        f"stdin stalled: no write progress for {window:g}s while "
        f"delivering response to req={safe_id}"
    )


async def _write_request_bounded(
    self: AcpRuntime, data: bytes, request_id: int, method: str
) -> None:
    """Write a REQUEST frame under the write lock and the same no-progress bound.

    The request twin of :meth:`_write_response_bounded`, for the same
    shared-runtime hazard: one stdin serves every multiplexed session, so a
    request write (``session/prompt``, ``session/new``, ``set_mode``,
    ``_session/steering``) that holds the lock across an unbounded ``drain()``
    parks forever WITH the lock held whenever kiro-cli is flow-control-paused
    -- busy generating on one lane and not reading stdin, the state right
    after ``spawn_run`` fans several session prompts onto the one pipe. Every
    other session's stdin write then queues behind that lock at 0 CPU while
    the busy lane keeps streaming stdout and looks healthy. The bound is on
    PROGRESS, not elapsed time, so a caller-sized frame (a prompt may carry
    any number of image blocks) stays legal: a reader still consuming keeps
    the wait alive, and only a writer whose buffer has not shrunk for
    ``_RESPONSE_WRITE_BOUND_SECS`` is the reader-gone stall.

    Both phases mark the runtime dead and raise
    :class:`AcpRuntimeStdinStalled`, which the session provider translates to
    ``AcpProcessDied`` so the caller takes the ownership-authorized session-
    reset recovery the broken-pipe case uses. This path does NOT kill the
    child: a ``DRAIN_STALL`` can leave the frame buffered in a kiro-cli that
    resumes, but killing cannot be guaranteed (a close/EOF keeps flushing the
    buffered bytes and the wedged child is the one that will not exit in a kill
    grace, so death is unconfirmed before the raise) and, on this shared
    runtime, signalling the child from a write path bypasses the
    ``authorize_runtime_kill`` ownership gate and teardown barrier the only
    sanctioned teardown takes, terminating sibling sessions' work unauthorized.
    Instead a ``DRAIN_STALL`` raises with ``ambiguous_delivery`` set, which
    rides through ``_translate_dead`` into ``build_recovery_requeue`` and makes
    the recovery resume from restored state rather than replay the prompt the
    paused backend may have consumed; a ``LOCK_STALL`` wrote no byte, so it is
    not ambiguous and its replay is safe. The method name and request id appear
    only through ``_loggable_request_id``.
    """
    assert self._process is not None and self._process.stdin is not None
    result = await write_request_frame_bounded(
        self._process.stdin,
        self._stdin_write_lock(),
        data,
        bound_secs=_RESPONSE_WRITE_BOUND_SECS,
        before_write=self._refuse_write_if_dead,
    )
    if result is RequestWriteResult.OK:
        return
    safe_id = _loggable_request_id(request_id)
    window = response_write_window_secs(self._process.stdin, _RESPONSE_WRITE_BOUND_SECS)
    logger.warning(
        "ACP runtime stdin stalled: %s while sending request method=%s req=%s; "
        "marking runtime dead (%s)",
        _stall_window_phrase(self._process.stdin, window),
        _loggable_request_id(method),
        safe_id,
        # Which phase stalled, for the log only -- recovery is the same either
        # way (see below).
        (
            "frame never written"
            if result is RequestWriteResult.LOCK_STALL
            else "frame already buffered"
        ),
    )
    # Both phases mark the runtime dead and raise -- no direct child kill from
    # here. On the shared runtime the child serves every multiplexed session,
    # and signalling it from a write path bypasses the ownership
    # authorization + teardown barrier (authorize_runtime_kill) that the only
    # sanctioned teardown takes, so it would terminate sibling sessions' work
    # unauthorized. Instead the two phases differ ONLY in the recovery hint
    # they carry: a LOCK_STALL wrote no byte, so the recovery's prompt replay
    # is the frame's first and only delivery (replay safe); a DRAIN_STALL left
    # the frame in the transport, which a kiro-cli that merely paused reading
    # could still consume -- so it is flagged ambiguous_delivery, and the
    # recovery resumes from restored state rather than replaying the prompt
    # verbatim (which would run its tools twice). The flag rides the stall
    # through _translate_dead onto AcpProcessDied; the authorized teardown
    # reaps the old child through the ownership path.
    drain_stall = result is RequestWriteResult.DRAIN_STALL
    self._mark_dead(
        (
            "request write stalled after the frame was buffered"
            if drain_stall
            else "request write stalled waiting for stdin lock"
        ),
        stdin_stalled=True,
    )
    raise AcpRuntimeStdinStalled(
        f"stdin stalled: no write progress for {window:g}s while " f"sending request req={safe_id}",
        ambiguous_delivery=drain_stall,
    )


async def send_request_for_answer(
    self: AcpRuntime,
    method: str,
    params: dict[str, Any],
    on_registered: "Callable[[asyncio.Future[dict[str, Any]]], None] | None" = None,
) -> "asyncio.Future[dict[str, Any]]":
    """Write a request and return a future for its answer, off the session queue.

    For a request a session sends WHILE its own turn is streaming, whose answer
    the sender needs. :meth:`send_request` routes the answer into the session's
    queue, where the turn's dispatch loop is the only reader, so waiting for it
    there would mean pulling the turn's frames out from under that loop.
    :meth:`_send_and_await` resolves off the queue but owns the wait and drops
    the registration when it times out. This registers the future the same way
    (``_pending_requests``, which the reader resolves before any routing) and
    hands it back, so the caller can bound the wait and still observe an answer
    that arrives after the bound.

    The future carries the answer's ``result`` object, or an
    :class:`AcpRuntimeError` for an error answer, or :class:`AcpRuntimeDead`
    if the process dies first -- the same three outcomes ``_send_and_await``
    gives.

    ``on_registered`` is called with the future BEFORE the write, whose
    ``drain()`` can suspend: the reader may resolve the future during that
    suspension, so a caller that must see the answer in order with the frames
    after it has to hold the future by then.
    """
    if not self._process or not self._process.stdin:
        raise AcpRuntimeDead("process not running")
    if self._dead:
        raise AcpRuntimeDead("runtime is dead")

    req_id = self._next_id
    self._next_id += 1
    projection = getattr(self, "_native_skill_projection", None)
    if projection is not None:
        params = projection.request(method, params)
    req = JsonRpcRequest(method=method, params=params, id=req_id)
    data = encode_frame(req.to_dict())

    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    self._pending_requests[req_id] = future
    if on_registered is not None:
        on_registered(future)
    try:
        # Bounded like send_request: a raw drain here would hold the
        # shared write lock against a flow-control-paused kiro-cli. A
        # stall marks the runtime dead and raises AcpRuntimeStdinStalled (an
        # AcpRuntimeDead), caught below so the pending future is retrieved or
        # cancelled rather than left unretrieved.
        await self._write_request_bounded(data, req_id, method)
    except AcpRuntimeDead:
        # Reached two ways, both of which have already resolved the future
        # before the raise, so retrieve or cancel it here only to stop
        # asyncio logging a handled error as unhandled: _refuse_write_if_dead
        # (a sibling stall marked the runtime dead while this caller held the
        # lock wait), or _write_request_bounded's own stall paths -- both of
        # which call _mark_dead and fail this pending future first.
        self._pending_requests.pop(req_id, None)
        if future.done():
            if not future.cancelled():
                future.exception()
        else:
            future.cancel()
        raise
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._pending_requests.pop(req_id, None)
        self._mark_dead(f"pipe broken: {exc}")
        raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    self._last_activity = time.monotonic()
    return future


def forget_request(self: AcpRuntime, future: "asyncio.Future[dict[str, Any]]") -> None:
    """Stop waiting for a :meth:`send_request_for_answer` answer.

    Drops the registration so an answer that never comes holds nothing, and
    cancels the future so any callback on it runs once. A no-op when the answer
    already arrived or the runtime has died (both already dropped it).
    """
    for req_id, pending in list(self._pending_requests.items()):
        if pending is future:
            self._pending_requests.pop(req_id, None)
            break
    if not future.done():
        future.cancel()


async def send_response(
    self: AcpRuntime,
    request_id: str | int,
    result: dict[str, Any],
    *,
    before_write: Callable[[], None] | None = None,
    withdrawal_check: Callable[[], None] | None = None,
) -> None:
    """Send a JSON-RPC response (for server→client requests like permission).

    ``before_write`` runs under the write lock right before the frame is
    written, after the dead-runtime check, and may raise to abort the write
    with nothing written. ``withdrawal_check`` runs under the same lock ahead
    of the dead-runtime check and may raise the same way: a frame its caller
    withdrew is not written, whatever the pipe's state.
    """
    if not self._process or not self._process.stdin:
        raise AcpRuntimeDead("process not running")
    if self._dead:
        raise AcpRuntimeDead("runtime is dead")

    msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
    data = encode_frame(msg)

    try:
        await self._write_response_bounded(
            data,
            request_id,
            before_write=before_write,
            withdrawal_check=withdrawal_check,
        )
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._mark_dead(f"pipe broken: {exc}")
        raise AcpRuntimeDead(f"pipe broken: {exc}") from exc


async def send_error(self: AcpRuntime, request_id: str | int, code: int, message: str) -> None:
    """Send a JSON-RPC error response."""
    if not self._process or not self._process.stdin:
        raise AcpRuntimeDead("process not running")
    if self._dead:
        raise AcpRuntimeDead("runtime is dead")

    msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    data = encode_frame(msg)

    try:
        await self._write_response_bounded(data, request_id)
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._mark_dead(f"pipe broken: {exc}")
        raise AcpRuntimeDead(f"pipe broken: {exc}") from exc


def _one_at_a_time_ahead(
    self: AcpRuntime, method: str, req_id: int
) -> list[tuple[int, str, float]]:
    """Record *req_id* if the backend serializes it; return what it was sent behind.

    Returns ``(id, method, seconds since sent)`` for every request in
    :data:`_ONE_AT_A_TIME_METHODS` still awaiting an answer, but only when
    *method* is a session start, the one caller that reports it, and only on a
    backend in ``ACP_BACKENDS_SERIAL_SESSION_STARTS``. Answered or abandoned
    ids are dropped here, so the map never outgrows ``_pending_requests``.
    """
    sent = self._one_at_a_time_sent
    for stale in [rid for rid in sent if rid not in self._pending_requests]:
        del sent[stale]
    # H6: only a backend measured to serialize these is described as having
    # made this start wait; an overlap on any other harness says nothing.
    if (
        method not in _ONE_AT_A_TIME_METHODS
        or self._acp_backend not in ACP_BACKENDS_SERIAL_SESSION_STARTS
    ):
        return []
    now = time.monotonic()
    ahead = (
        [(rid, m, now - t) for rid, (m, t) in sent.items()]
        if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD)
        else []
    )
    sent[req_id] = (method, now)
    return ahead


async def _send_and_await(
    self: AcpRuntime,
    method: str,
    params: dict[str, Any],
    timeout: float = _REQUEST_TIMEOUT,
    *,
    translate: bool = True,
) -> dict[str, Any]:
    """Send a JSON-RPC request and await the response via _pending_requests.

    Used for control-plane requests (initialize, session/new, set_mode)
    where we need the response immediately rather than routing it to a
    session queue. ``timeout`` bounds the wait — teardown paths pass a
    tighter value than the default so an unresponsive runtime can't stall
    session eviction. ``translate=False`` sends *params* as given, past the
    skill projection's name translation: the one caller is the ``set_mode``
    bracket, which passes params it translated itself and, between attempts,
    re-derives them only from the newest adopted projection
    (:meth:`_activate_mode_bracketed`).
    """
    if not self._process or not self._process.stdin:
        raise AcpRuntimeDead("process not running")
    if self._dead:
        raise AcpRuntimeDead("runtime is dead")

    req_id = self._next_id
    self._next_id += 1
    ahead = self._one_at_a_time_ahead(method, req_id)

    projection = getattr(self, "_native_skill_projection", None)
    if projection is not None and translate:
        params = projection.request(method, params)
    req = JsonRpcRequest(method=method, params=params, id=req_id)
    data = encode_frame(req.to_dict())

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    self._pending_requests[req_id] = future

    try:
        # Bounded like send_request: the control-plane
        # requests (initialize, session/new, set_mode) share the same
        # drain under the shared write lock and must not park it against
        # a flow-control-paused kiro-cli. A stall raises AcpRuntimeStdinStalled
        # (an AcpRuntimeDead), caught below to retrieve/cancel the future.
        await self._write_request_bounded(data, req_id, method)
    except AcpRuntimeDead:
        # Reached through _refuse_write_if_dead (a sibling stall marked the
        # runtime dead while this caller held the lock wait) OR through
        # _write_request_bounded's own stall paths -- both of which call
        # _mark_dead. They all fail this future before the raise, so retrieve
        # its exception (or cancel a still-pending one) rather than drop the
        # last reference unretrieved and have asyncio log a handled error as
        # unhandled.
        self._pending_requests.pop(req_id, None)
        if future.done():
            if not future.cancelled():
                future.exception()
        else:
            future.cancel()
        raise
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._pending_requests.pop(req_id, None)
        self._mark_dead(f"pipe broken: {exc}")
        raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    self._last_activity = time.monotonic()

    stage = {
        "initialize": "initialize",
        METHOD_SESSION_NEW: "session_new",
        METHOD_SESSION_LOAD: "session_load",
        METHOD_SET_MODE: "set_mode",
    }.get(method)
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        if method == METHOD_SESSION_NEW:
            # The answer may still come, and it names a session the runtime
            # has CREATED, so keep the future registered for the reader loop
            # and hand ownership to the caller's StartCollector via ``adopt``
            # (RFC §4.4) instead of leaving an unowned session in the shared
            # process. ``wait_for`` cancelled the future; register a fresh one
            # bound to the same id. Every other method is popped: a future
            # left registered with no reader is failed by the cleanup kill's
            # ``_mark_dead`` and logged as an exception nobody retrieved.
            fresh: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending_requests[req_id] = fresh
            adopt = getattr(self._pending_requests, "adopt", None)
            adopted = adopt(req_id) if adopt is not None else fresh
        else:
            self._pending_requests.pop(req_id, None)
            adopted = None
        active_starts, queued_starts = _cold_start_counts()
        if self._process is None:
            process_state = "absent"
        elif self._process.returncode is None:
            process_state = "running"
        else:
            process_state = "exited"
        logger.warning(
            "acp_startup_stage stage=%s outcome=timeout timeout_method=%s "
            "timeout_budget_s=%g duration_ms=%.1f active_starts=%d "
            "queued_starts=%d process_state=%s stderr_lines=%d",
            stage or "control",
            method,
            timeout,
            (time.monotonic() - started) * 1000.0,
            active_starts,
            queued_starts,
            process_state,
            min(len(self._stderr_lines), 20),
        )
        # Name the budget: a session-start timeout (90s) must be
        # distinguishable from a generic control-plane one (30s).
        timeout_exc = AcpRequestTimeout(f"Request {method} timed out after {timeout:g}s")
        # Only a request the process had still not answered at this deadline
        # is reported: on a backend that answers them one at a time, this
        # start had not begun while that one was outstanding. One answered
        # earlier may have cost a few seconds or nothing, so it is left out
        # rather than blamed for the whole budget.
        unanswered = [(m, age) for rid, m, age in ahead if rid in self._pending_requests]
        if unanswered:
            setattr(timeout_exc, "queued_behind", unanswered)
        if adopted is not None:
            # What create_session needs to build the collector.
            setattr(timeout_exc, "req_id", req_id)
            setattr(timeout_exc, "adopted_future", adopted)
        raise timeout_exc
    if stage is not None:
        active_starts, queued_starts = _cold_start_counts()
        logger.info(
            "acp_startup_stage stage=%s outcome=ready timeout_method=%s "
            "timeout_budget_s=%g duration_ms=%.1f active_starts=%d queued_starts=%d",
            stage,
            method,
            timeout,
            (time.monotonic() - started) * 1000.0,
            active_starts,
            queued_starts,
        )
    return result
