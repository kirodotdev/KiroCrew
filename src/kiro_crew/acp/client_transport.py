"""JSON-RPC over an ``AcpClient`` child's stdio.

Request ids, bounded request and response writes, frame reads, waiting for a response
while deferring other frames, and draining notifications.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_activated_model``, ``_last_activity``,
``_last_substitution_model``, ``_mcp_notifications``, ``_mcp_report_frame_floor``,
``_mode_frame_superseded``, ``_next_id``, ``_oauth_emitted_servers``,
``_pending_oauth_requests``, ``_stdin_lock``, ``_stdin_stalled``.

Instance fields read: ``_buffer``, ``_cancel_grace_secs``, ``_cancel_ts``,
``_cancelled``, ``_kill_sent``, ``_mcp_report``, ``_mode_frame_awaited``,
``_native_skill_projection``, ``_process``, ``_stderr_lines``, ``_stderr_task``,
``memory_mode``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.acp import transport_framing
from kiro_crew.acp._dispatch import _loggable_request_id
from kiro_crew.acp._frame_record import record_frame
from kiro_crew.acp.client_spawn_env import _exit_origin
from kiro_crew.acp.client_timeouts import (
    _DRAIN_DURATION,
    _DRAIN_IDLE_EXIT,
    _READ_TIMEOUT,
    _WAIT_RESPONSE_MAX_TIMEOUT,
)
from kiro_crew.acp.runtime_models import (
    _extract_advisory_detail,
    _is_model_substitution_advisory,
    _substitute_model_from_advisory,
)
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied, AcpTimeoutError
from kiro_crew.acp.transport_framing import (
    RequestWriteResult,
    _stall_window_phrase,
    response_write_window_secs,
    settle_drain,
    write_request_frame_bounded,
    write_response_frame_bounded,
)
from kiro_crew.acp.types import (
    METHOD_AGENT_SWITCHED,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_RESUME,
    METHOD_SESSION_UPDATE,
    METHOD_SET_MODEL,
    UPDATE_CONFIG_OPTION,
    JsonRpcMessage,
    JsonRpcRequest,
)
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


def _is_safe_oauth_url(url: str) -> bool:
    """Reject anything that isn't http(s) — `<a href>` will execute javascript:/data:."""
    if not url:
        return False
    lower = url.lower()
    return lower.startswith("https://") or lower.startswith("http://")


def _jsonrpc_error_code(error: object) -> int | None:
    """The integer ``code`` of a JSON-RPC error frame, or ``None``.

    Tolerant of every shape the wire has produced: a missing ``code``, a
    non-dict frame, or a code spelled as a numeric string all answer ``None``
    (a bool is refused too — ``True == 1`` would otherwise read as a code).
    """
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    return None


def _next_req_id(self: AcpClient) -> int:
    rid = self._next_id
    self._next_id += 1
    return rid


# ── JSON-RPC Transport ──


async def _send_request(
    self: AcpClient,
    method: str,
    params: dict,
    *,
    on_id: Callable[[int], None] | None = None,
) -> int:
    """Write one request frame and return its id.

    ``on_id`` is called with the id before the frame is written, so a caller
    that waits for the answer can register for it before the write's drain
    can suspend and the turn's read loop can read that answer.
    """
    if not self._process or not self._process.stdin:
        raise AcpError("ACP process not running")

    projection = getattr(self, "_native_skill_projection", None)
    if projection is not None:
        params = projection.request(method, params)
    req_id = self._next_req_id()
    if on_id is not None:
        on_id(req_id)
    req = JsonRpcRequest(method=method, params=params, id=req_id)
    data = json.dumps(req.to_dict()) + "\n"
    if method == METHOD_SET_MODEL:
        # The session runs this model from here on, whatever an earlier
        # agent activation put it on (see _note_agent_switched). Marked
        # before the write, whose drain can suspend while a read runs.
        self._activated_model = ""
        if getattr(self, "_mode_frame_awaited", False):
            self._mode_frame_superseded = True
    try:
        # Bounded on the reader's PROGRESS, not held across a raw drain: a
        # request write that parks while the backend is flow-control-paused
        # must not hold the write lock (acute on the shared runtime, where a
        # co-tenant waits behind the lock, but a single session can self-wedge
        # too). A stall raises AcpProcessDied, the same recovery a closed pipe
        # already gets.
        await self._write_request_bounded(data.encode(), req_id, method)
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
    self._last_activity = time.monotonic()
    return req_id


async def _write_request_bounded(
    self: AcpClient, data: bytes, request_id: int, method: str
) -> None:
    """Write a REQUEST frame under the write lock and the same no-progress bound.

    The request twin of :meth:`_write_response_bounded`. A request frame is
    caller-sized (a prompt may carry any number of image blocks), which is
    why the bound is on the reader's PROGRESS rather than on elapsed time: a
    reader still consuming keeps the wait alive, and only a writer whose
    buffer has not shrunk for ``_RESPONSE_WRITE_BOUND_SECS`` is the
    reader-gone stall. Holding the write lock across an unbounded ``drain()``
    here would let a flow-control-paused backend park it forever WITH the
    lock held; on the shared runtime that wedges every co-tenant session
    behind the lock at 0 CPU. A stall is mapped to
    ``AcpProcessDied`` so the caller takes the existing session-reset +
    bounded-requeue recovery.

    A DRAIN_STALL can leave the frame buffered in a backend that resumes
    reading, but the kill-and-reap that would try to make delivery impossible
    is NOT done here: it cannot be guaranteed (a close/EOF keeps flushing the
    buffered bytes, and a wedged child is exactly the one that fails to exit
    within the grace, so death is not confirmed before the raise), and on the
    shared runtime it would bypass the ownership authorization and terminate
    sibling sessions. Instead a DRAIN_STALL raises ``AcpProcessDied`` with
    ``ambiguous_delivery`` set, which ``build_recovery_requeue`` reads to
    resume from restored state rather than replay a prompt the backend may
    have consumed; a LOCK_STALL wrote no byte, so it is not ambiguous and its
    replay is safe. The phase appears only in the log. Either phase marks
    the client stalled, so nothing reuses it: the next
    ``ensure_ready`` kills the child before spawning its replacement.
    """
    assert self._process is not None and self._process.stdin is not None
    result = await write_request_frame_bounded(
        self._process.stdin,
        self._stdin_write_lock(),
        data,
        bound_secs=transport_framing._RESPONSE_WRITE_BOUND_SECS,
    )
    if result is RequestWriteResult.OK:
        return
    safe_id = _loggable_request_id(request_id)
    window = response_write_window_secs(
        self._process.stdin, transport_framing._RESPONSE_WRITE_BOUND_SECS
    )
    logger.warning(
        "ACP stdin stalled: %s while sending request method=%s req=%s; "
        "treating the backend as dead (%s)",
        _stall_window_phrase(self._process.stdin, window),
        _loggable_request_id(method),
        safe_id,
        (
            "frame never written"
            if result is RequestWriteResult.LOCK_STALL
            else "frame already buffered"
        ),
    )
    self._stdin_stalled = True
    raise AcpProcessDied(
        f"ACP stdin stalled: no write progress for {window:g}s while "
        f"sending request req={safe_id}",
        ambiguous_delivery=result is RequestWriteResult.DRAIN_STALL,
    )


def _stdin_write_lock(self: AcpClient) -> asyncio.Lock:
    """The one lock every stdin write on this client takes (see
    ``await_under_no_progress_bound`` for why the bound needs it). Created on
    first use so a client built without ``__init__`` (test doubles) has one."""
    lock = getattr(self, "_stdin_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        self._stdin_lock = lock
    return lock


async def _write_response_bounded(
    self: AcpClient,
    data: bytes,
    request_id: str | int,
    *,
    before_write: Callable[[], None] | None = None,
) -> None:
    """Write a response/error frame under the write lock and a no-progress bound.

    Left alone, a deny path answering a permission request would sit on a
    flow-control-paused writer until the turn deadline. A writer whose
    buffer stops shrinking for ``_RESPONSE_WRITE_BOUND_SECS`` -- while
    waiting for the lock or while draining -- is the same undeliverable-
    response condition a closed pipe reports as an error, so it is mapped to
    the same exception: ``AcpProcessDied`` sends the caller down the existing
    session-reset + bounded-requeue recovery instead of hanging. The request
    id appears only through ``_loggable_request_id``. *before_write* runs
    under the lock right before the frame is written and may raise to abort
    the write: no byte of the frame is written then.
    """
    assert self._process is not None and self._process.stdin is not None
    if await write_response_frame_bounded(
        self._process.stdin,
        self._stdin_write_lock(),
        data,
        bound_secs=transport_framing._RESPONSE_WRITE_BOUND_SECS,
        before_write=before_write,
    ):
        return
    safe_id = _loggable_request_id(request_id)
    window = response_write_window_secs(
        self._process.stdin, transport_framing._RESPONSE_WRITE_BOUND_SECS
    )
    logger.warning(
        "ACP stdin stalled: %s while "
        "delivering response to req=%s; treating the backend as dead",
        _stall_window_phrase(self._process.stdin, window),
        safe_id,
    )
    raise AcpProcessDied(
        f"ACP stdin stalled: no write progress for {window:g}s while "
        f"delivering response to req={safe_id}"
    )


async def _send_response(
    self: AcpClient,
    request_id: str | int,
    result: dict,
    *,
    before_write: Callable[[], None] | None = None,
) -> None:
    if not self._process or not self._process.stdin:
        raise AcpError("ACP process not running")

    msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
    data = json.dumps(msg) + "\n"
    try:
        if before_write is None:
            await self._write_response_bounded(data.encode(), request_id)
        else:
            await self._write_response_bounded(data.encode(), request_id, before_write=before_write)
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
    self._last_activity = time.monotonic()


async def _send_error(self: AcpClient, request_id: str | int, code: int, message: str) -> None:
    """Send a JSON-RPC 2.0 error response for a server→client request.

    It answers an unrecognized inbound request (e.g. ``fs/read_text_file``,
    ``terminal/create``) with ``-32601 Method not found`` so the agent fails
    fast instead of blocking forever waiting for a response we'd never send.
    """
    if not self._process or not self._process.stdin:
        raise AcpError("ACP process not running")

    msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    data = json.dumps(msg) + "\n"
    try:
        await self._write_response_bounded(data.encode(), request_id)
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
    self._last_activity = time.monotonic()


async def _read_message(self: AcpClient, timeout: float = _READ_TIMEOUT) -> JsonRpcMessage | None:
    if self._cancelled:
        if time.monotonic() - self._cancel_ts > self._cancel_grace_secs:
            raise AcpError("Cancel grace window exceeded; agent unresponsive")

    if self._buffer:
        return self._buffer.popleft()

    if not self._process or not self._process.stdout:
        raise AcpError("ACP process not running")

    try:
        line = await asyncio.wait_for(self._process.stdout.readline(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    except (ValueError, asyncio.LimitOverrunError) as exc:
        # A single JSON-RPC line exceeded the stdout StreamReader buffer
        # (_STDOUT_BUFFER_LIMIT). This does NOT corrupt the stream: before
        # raising ValueError, readline() deletes the oversize line through
        # its terminating newline when one is already buffered, else clears
        # the buffer, then resumes the transport (CPython
        # asyncio.streams.StreamReader.readline — its docstring states this).
        # So drop the frame and let the caller read the next one, exactly
        # like the blank-line and non-JSON paths below; raising
        # AcpProcessDied here would end a healthy live turn over one
        # unreadably large frame.
        #
        # NOTE the deliberate asymmetry with AcpRuntime._reader_loop, which
        # additionally enforces a drain budget: that reader is a standalone
        # task with no deadline, so an endlessly unterminated stream needs an
        # explicit terminal state there. HERE every call is bounded by the
        # caller's `timeout` and the callers run their own deadlines, so the
        # worst case is one turn ending on its deadline instead of a frame —
        # no unbounded state, and still strictly better than killing the turn
        # on the first oversize frame. Computing a byte budget would require
        # readuntil (readline reports neither the branch taken nor the bytes
        # dropped), i.e. hand-rolling readline's buffer repair on the path
        # that is NOT the reported failure.
        logger.warning("Dropped an oversize ACP stdout frame: %s", exc)
        return None
    if not line:
        # EOF — process likely died or closing. Check and avoid busy-loop.
        if self._process and self._process.returncode is not None:
            # Unshielded: the child has exited, so a drain still running past
            # the budget is held open only by a descendant that inherited the
            # pipe, and every later settle on this failure would wait on it
            # again.
            await settle_drain(self._stderr_task, 0.5, shield=False)
            stderr_tail = "; ".join(self._stderr_lines) if self.memory_mode == "persistent" else ""
            if stderr_tail:
                from kiro_crew.security import redact_credentials, redact_exfiltration_urls

                stderr_tail, _ = redact_exfiltration_urls(stderr_tail)
                stderr_tail, _ = redact_credentials(stderr_tail)
            detail = f" — {stderr_tail}" if stderr_tail else ""
            code = self._process.returncode
            origin = _exit_origin(code, self._kill_sent)
            raise AcpError(f"ACP process exited (code={code}{origin}){detail}")
        await asyncio.sleep(0.1)
        return None

    text = line.decode(errors="replace").strip()
    if not text:
        return None

    self._last_activity = time.monotonic()

    # Only a JSON object is a frame. Every other line costs that line, not
    # the caller's handshake or turn: text that is not JSON, a bare scalar
    # or array (`123`, `[...]`, `null`), nesting past the decoder's ceiling
    # (RecursionError) and an integer past the int-string digit limit.
    parsed = parse_json_object_line(text)
    if parsed is None:
        if self.memory_mode == "persistent":
            logger.debug("Skipping a line from ACP that is not a JSON object: %.100s", text)
        return None
    data: dict[str, Any] = parsed

    # Opt-in raw-frame recording for the replay corpus. A no-op unless
    # KIROCREW_ACP_RECORD_FRAMES names a directory, and the write is
    # offloaded off this loop when it is set. It never raises -- see
    # kiro_crew.acp._frame_record. Placed after the buffer early-return
    # above so a frame is recorded once, when it comes off the wire, not
    # again when a turn loop replays it out of _buffer.
    if self.memory_mode == "persistent":
        await record_frame(self.backend, data, len(line))

    projection = getattr(self, "_native_skill_projection", None)
    if projection is not None:
        data = projection.frame(data)
    return JsonRpcMessage(
        id=data.get("id"),
        method=data.get("method"),
        result=data.get("result"),
        error=data.get("error"),
        params=data.get("params"),
    )


async def _wait_for_response(
    self: AcpClient,
    req_id: int,
    timeout: float = 50.0,
    *,
    method: str = "",
    expected_mcp: object = None,
) -> dict:
    """Block until a JSON-RPC response matching *req_id* arrives.

    Explicitly classifies JSON-RPC 2.0 messages with the same method-aware
    discipline as ``JsonRpcMessage.is_response_for`` (a *response* has an
    id + no method; a *request* has both id AND method):

    - Notifications (method + no id): buffered in ``_mcp_notifications`` for
      ``_drain_notifications`` to process.
    - Matching response (id == req_id + no method): returned.
    - Server→client request (method + id) and foreign-id responses
      (id != req_id + no method): collected into a LOCAL ``deferred`` list
      and re-injected into ``self._buffer`` IN ORDER once the matching
      response arrives or on timeout.

    Server requests and foreign responses must NOT be re-appended to
    ``self._buffer`` mid-loop and ``continue``-d: ``_read_message`` pops
    ``self._buffer`` first, so it would immediately re-read the same frame,
    re-defer it, and spin until the deadline (the original bug). Holding
    them in a local list until exit guarantees forward progress while
    preserving the frame so a later ``_prompt_loop`` / ``_process_message``
    can answer the deferred ``session/request_permission`` request.

    The deadline is **activity-based**: any received message (notification,
    deferred frame, or the matching response) resets it to ``now + timeout``,
    bounded by an absolute ``_WAIT_RESPONSE_MAX_TIMEOUT`` safety cap. This
    keeps a long ``session/load`` replay (which streams the entire prior
    transcript as ``session/update`` notifications before resolving) alive
    instead of being killed by a fixed wall-clock and silently falling back
    to ``session/new``.
    """
    from kiro_crew import shutdown_event

    start = time.monotonic()
    deadline = start + timeout
    hard_deadline = start + max(timeout, _WAIT_RESPONSE_MAX_TIMEOUT)
    # Frames that are not the awaited response but must survive this call.
    deferred: list[JsonRpcMessage] = []

    def _reinject() -> None:
        # Re-inject in order at the FRONT of the buffer so a later
        # _prompt_loop / _process_message sees them before newer frames.
        for d in reversed(deferred):
            self._buffer.appendleft(d)
        deferred.clear()

    while time.monotonic() < deadline and time.monotonic() < hard_deadline:
        if shutdown_event.is_set():
            _reinject()
            raise AcpError("Shutdown in progress")
        remaining = min(deadline, hard_deadline) - time.monotonic()
        if remaining <= 0:
            break
        msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
        if msg is None:
            continue
        # Activity-based deadline: extend on any received frame, capped by
        # the hard safety deadline. Safe for init/handshake callers — only
        # extends while the agent is actively sending us data.
        deadline = min(time.monotonic() + timeout, hard_deadline)
        if msg.is_response_for(req_id):
            _reinject()
            if msg.error:
                if _is_model_substitution_advisory(msg.error):
                    # Admin-tier / headless-tier policy substituted the
                    # requested model. The session is already live on the
                    # substitute -- keep going; log loudly so operators see it.
                    detail = _extract_advisory_detail(msg.error)
                    self._last_substitution_model = _substitute_model_from_advisory(msg.error)
                    # Redact before logging. The advisory payload originates
                    # from the ACP backend and flows to the dashboard activity
                    # feed and Slack via the gateway log. Match the existing
                    # _format_acp_error pattern (acp/transport_errors.py).
                    _payload_log = detail if detail else str(msg.error)
                    _payload_log, _ = redact_exfiltration_urls(_payload_log)
                    _payload_log, _ = redact_credentials(_payload_log)
                    logger.warning(
                        "ACP backend substituted model (policy): %s",
                        _payload_log,
                    )
                    return msg.result or {}
                # Dual-redact msg.error before interpolating into the AcpError.
                # msg.error is the wire-derived JSON-RPC error frame from the ACP
                # backend; AcpError propagates to the dashboard activity feed and
                # Slack via the same path as the logger sinks. Same redaction
                # discipline as the substitution-advisory log site above.
                _err_log, _ = redact_exfiltration_urls(str(msg.error))
                _err_log, _ = redact_credentials(_err_log)
                raise AcpError(f"JSON-RPC error: {_err_log}", code=_jsonrpc_error_code(msg.error))
            return msg.result or {}
        # Notification (has method, no id) — buffer for drain.
        if msg.method and msg.id is None:
            self._mcp_notifications.append(msg)
            logger.debug("Buffered notification: %s (req=%d)", msg.method, req_id)
            continue
        # Server→client request (method AND id) or a foreign-id response
        # (id != req_id, no method). Defer locally — do NOT re-append to
        # self._buffer here, that would spin (see docstring). Re-injected
        # in order on return/raise so the permission request survives.
        if msg.method:
            logger.debug(
                "Deferring inbound server request: method=%s id=%s (waiting for %d)",
                _loggable_request_id(msg.method),
                _loggable_request_id(msg.id),
                req_id,
            )
        else:
            logger.debug(
                "Deferring non-matching response: id=%s (waiting for %d)",
                _loggable_request_id(msg.id),
                req_id,
            )
        deferred.append(msg)

    _reinject()
    label = method or f"request {req_id}"
    message = f"ACP {label} timed out after {timeout:.0f}s"
    # Both restore verbs, not just one: a harness in
    # ``ACP_BACKENDS_RESUME_WITHOUT_LOAD`` reaches this with
    # ``method=session/resume`` and the same ``expected_mcp`` detail, and an
    # omission here silently drops that detail from the only message an operator
    # sees when a restore stalls.
    if method in {METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SESSION_RESUME}:
        progress = self._mcp_timeout_progress(expected_mcp)
        if progress:
            message += f" ({progress})"
        err = AcpTimeoutError(message=message)
        # A start that never answered, tagged for the self-driving callers
        # that count consecutive start failures (see
        # ``AcpError.session_start_failed``). Set only in this branch: the
        # other awaited requests are not session starts.
        err.session_start_failed = True
        raise err
    raise AcpTimeoutError(message=message)


async def _drain_notifications(
    self: AcpClient,
    duration: float = _DRAIN_DURATION,
    idle_exit: float = _DRAIN_IDLE_EXIT,
) -> None:
    """Drain init notifications (buffered + live) and log MCP servers.

    Captures `_kiro.dev/mcp/oauth_request` into `_pending_oauth_requests` so
    callers can surface an Authorize prompt after `ensure_ready()` returns.

    Exits early once no notification has arrived for ``idle_exit`` seconds
    (MCP servers have gone quiet), bounded by the ``duration`` hard cap. This
    avoids always waiting the full cap on the common fast path while still
    giving genuinely slow servers up to ``duration`` to report in.
    """
    deadline = time.monotonic() + duration
    last_activity = time.monotonic()
    drained = 0
    mcp_servers: list[str] = []

    def _capture_oauth(msg: JsonRpcMessage) -> None:
        if not msg.is_method(METHOD_MCP_OAUTH_REQUEST):
            return
        params = msg.params if isinstance(msg.params, dict) else {}
        server_name = str(params.get("serverName") or params.get("name") or "")
        oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
        # Drop unsafe-scheme URLs *before* recording dedupe so a later safe
        # retry for the same server still gets through.
        if not _is_safe_oauth_url(oauth_url):
            if oauth_url:
                logger.warning(
                    "ACP: refusing unsafe MCP OAuth URL for %s", server_name or "(unknown)"
                )
            return
        # Without a server_name we can't reliably correlate this banner with
        # the matching server_initialized/server_init_failure notification —
        # the discard path keys on server_name only.  Drop rather than risk
        # a permanently-stuck dedupe entry.
        if not server_name:
            logger.warning("ACP: dropping MCP OAuth request with empty serverName")
            return
        if server_name in self._oauth_emitted_servers:
            logger.debug("ACP: dropping duplicate MCP OAuth request for %s", server_name)
            return
        self._oauth_emitted_servers.add(server_name)
        self._pending_oauth_requests.append({"serverName": server_name, "oauthUrl": oauth_url})
        logger.info("ACP: MCP OAuth request for %s", server_name)

    def _capture_config_update(msg: JsonRpcMessage) -> None:
        if not msg.is_method(METHOD_SESSION_UPDATE):
            return
        params = msg.params or {}
        update = params.get("update", {})
        if isinstance(update, dict) and update.get("sessionUpdate") == UPDATE_CONFIG_OPTION:
            self._handle_config_option_update(msg)

    def _capture_agent_switched(msg: JsonRpcMessage) -> None:
        if msg.is_method(METHOD_AGENT_SWITCHED):
            self._note_agent_switched(msg.params if isinstance(msg.params, dict) else None)

    # Process notifications buffered during _wait_for_response
    for idx, msg in enumerate(self._mcp_notifications):
        drained += 1
        _capture_oauth(msg)
        _capture_config_update(msg)
        _capture_agent_switched(msg)
        # Frames below the floor were buffered by an EARLIER session attempt
        # (a session/load that failed before the fallback session/new). The
        # OAuth and config captures above still want them — the user must
        # answer that authorization either way — but the report must not
        # credit this session with a server that reported to another attempt.
        if idx >= self._mcp_report_frame_floor:
            # Owned by construction: this transport runs one session per
            # process, so there is no co-tenant whose frame could arrive
            # here. Stated rather than defaulted so a transport that gains
            # tenants has to answer the question instead of inheriting a
            # yes.
            self._mcp_report.record_frame(msg, owned=True)
        name = ""
        if isinstance(msg.params, dict):
            name = msg.params.get("name") or msg.params.get("serverName") or ""
        if name or "mcp" in (msg.method or ""):
            mcp_servers.append(name or msg.method or "unknown")
    self._mcp_notifications.clear()
    # The floor indexes INTO that buffer, so it has to fall with it —
    # otherwise the next attempt's frames land below a stale floor and are
    # dropped from the report instead of the previous attempt's.
    self._mcp_report_frame_floor = 0

    while True:
        # Single time snapshot per iteration so the deadline and idle checks
        # can't diverge on a loaded host (CR feedback).
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        # Early-exit once servers have been quiet for the idle window. Poll in
        # idle-sized slices (capped by remaining) so we notice quiet promptly.
        idle_remaining = idle_exit - (now - last_activity)
        if idle_remaining <= 0:
            break
        try:
            read_msg = await self._read_message(timeout=min(remaining, idle_remaining, 2.0))
            if not read_msg:
                continue
            # Any received message counts as activity (servers still talking),
            # resetting the idle window even if it carries no method.
            last_activity = time.monotonic()
            if not read_msg.method:
                continue
            drained += 1
            _capture_oauth(read_msg)
            _capture_config_update(read_msg)
            _capture_agent_switched(read_msg)
            self._mcp_report.record_frame(read_msg, owned=True)
            if "mcp" in (read_msg.method or ""):
                name = ""
                if isinstance(read_msg.params, dict):
                    name = read_msg.params.get("name") or read_msg.params.get("serverName") or ""
                mcp_servers.append(name or read_msg.method)
        except AcpError:
            break
    if mcp_servers:
        logger.info("ACP: MCP servers loaded: %s", ", ".join(mcp_servers))
