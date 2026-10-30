"""JSON-RPC over an ``AcpClient`` child's stdio.

Request ids, bounded request and response writes, frame reads, waiting for a response
while deferring other frames, draining notifications, and the idle reader that watches
stdout between turns on a backend whose agent starts turns of its own.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_activated_model``, ``_drain_mode_reports``, ``_last_activity``,
``_last_substitution_model``, ``_mcp_notifications``, ``_mcp_report_frame_floor``,
``_mode_frame_superseded``, ``_next_id``, ``_oauth_emitted_servers``,
``_pending_oauth_requests``, ``_stdin_lock``, ``_stdin_stalled``.

Instance fields read: ``_buffer``, ``_cancel_grace_secs``, ``_cancel_ts``,
``_cancelled``, ``_kill_sent``, ``_mcp_report``, ``_mode_frame_awaited``,
``_native_skill_projection``, ``_process``, ``_stderr_lines``, ``_stderr_task``,
``_turn_done``, ``memory_mode``.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable, TypeVar

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
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_AVAILABLE_COMMANDS,
    UPDATE_CONFIG_OPTION,
    UPDATE_CURRENT_MODE,
    UPDATE_PLAN,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    UPDATE_USAGE,
    JsonRpcMessage,
    JsonRpcRequest,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_FORBIDDEN_MODES
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")

_R = TypeVar("_R")


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


# The request id an agent-started turn reads under. This client's ids start at 1
# and only grow, so no response can match it.
_AGENT_STARTED_REQUEST_ID = -1
# `_meta["_claude/origin"].kind` values claude-agent-acp puts on the closing
# usage_update of a cycle Claude Code ran on its own (its AUTONOMOUS_RESULT_ORIGINS).
# A user turn's closing usage_update carries {"kind": "human"}, outside this set.
_CLAUDE_AUTONOMOUS_ORIGIN_KINDS = frozenset(
    {"task-notification", "peer", "coordinator", "observer", "observer-activity"}
)
# session/update kinds that mean the agent is acting. Others (usage, the session
# title, available commands) arrive between turns with no turn behind them.
_AGENT_ACTIVITY_UPDATE_KINDS = frozenset(
    {
        UPDATE_AGENT_MESSAGE_CHUNK,
        UPDATE_AGENT_THOUGHT_CHUNK,
        UPDATE_TOOL_CALL,
        UPDATE_TOOL_CALL_UPDATE,
        UPDATE_PLAN,
    }
)
# session/update kinds whose listed fields are full snapshots: every reader of two
# frames that differ only in those fields ends where the later frame alone leaves it.
# A kind a reader starts to treat as incremental must leave this table.
_SNAPSHOT_UPDATE_FIELDS: dict[str, frozenset[str]] = {
    UPDATE_USAGE: frozenset({"used", "size"}),
    UPDATE_AVAILABLE_COMMANDS: frozenset({"availableCommands"}),
}


def _session_update_kind(msg: JsonRpcMessage) -> tuple[str, dict]:
    """The ``sessionUpdate`` kind of a ``session/update`` frame and its ``update`` body."""
    if not msg.is_method(METHOD_SESSION_UPDATE) or not isinstance(msg.params, dict):
        return "", {}
    update = msg.params.get("update")
    if not isinstance(update, dict):
        return "", {}
    kind = update.get("sessionUpdate")
    return (kind if isinstance(kind, str) else ""), update


def _starts_agent_turn(msg: JsonRpcMessage) -> bool:
    """True when a frame read between turns means the agent is running a turn of its own."""
    if msg.method is not None and msg.id is not None:
        # A request (a permission, a file read) the agent waits on until answered.
        return True
    return _session_update_kind(msg)[0] in _AGENT_ACTIVITY_UPDATE_KINDS


def _ends_agent_started_turn(msg: JsonRpcMessage) -> bool:
    """True for a frame that closes the cycle an agent-started turn is reading.

    The usage_update that closes a cycle Claude Code ran on its own, or a
    response. No request is sent under that turn's id, so a response to a
    request sent before the turn began answers a wait that already ended,
    usually a prompt whose turn ended here, and the frames read were that
    prompt's tail. ``_read_turn`` leaves a response to a later request unread,
    and reads the answer to a steering request as a stray response.
    """
    if msg.method is None and msg.id is not None:
        return True
    kind, update = _session_update_kind(msg)
    if kind != UPDATE_USAGE:
        return False
    meta = update.get("_meta")
    origin = meta.get("_claude/origin") if isinstance(meta, dict) else None
    return isinstance(origin, dict) and origin.get("kind") in _CLAUDE_AUTONOMOUS_ORIGIN_KINDS


def _supersedes(newer: JsonRpcMessage, older: JsonRpcMessage) -> bool:
    """True when *newer* differs from *older* only in its kind's snapshot fields.

    Both are ``session/update`` frames of one ``_SNAPSHOT_UPDATE_FIELDS`` kind with
    the same keys. Everything else must match: the session, ``_meta``, and a
    ``usage_update``'s cost, which is billed as the change between readings, so
    each reading counts.
    """
    kind, update = _session_update_kind(newer)
    fields = _SNAPSHOT_UPDATE_FIELDS.get(kind)
    older_kind, older_update = _session_update_kind(older)
    if fields is None or older_kind != kind or update.keys() != older_update.keys():
        return False
    return {**newer.params, "update": None} == {**older.params, "update": None} and all(
        update[key] == older_update[key] for key in update.keys() - fields
    )


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
    data = transport_framing.encode_frame(req.to_dict())
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
        await self._write_request_bounded(data, req_id, method)
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
    data = transport_framing.encode_frame(msg)
    try:
        if before_write is None:
            await self._write_response_bounded(data, request_id)
        else:
            await self._write_response_bounded(data, request_id, before_write=before_write)
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
    data = transport_framing.encode_frame(msg)
    try:
        await self._write_response_bounded(data, request_id)
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
    self._last_activity = time.monotonic()


# ── Idle reader ──
#
# A StreamReader allows one waiting readline() at a time, so on a client
# that runs the idle reader every method that reads stdout claims it first,
# which stops the idle reader; the last release restarts it.


def _install_idle_reader(self: AcpClient) -> None:
    """Bind the idle reader's steps over this client's own methods, once.

    Each stdout reader claims stdout, a turn read under
    ``_AGENT_STARTED_REQUEST_ID`` ends on the frame that closes it, a prompt
    waits until no such turn reads, and the two paths that end the process drop
    the reader. The claude adapter calls this
    from its spawn step (``ClaudeLaunch.resolve_spawn``), which runs again on
    every respawn, and a client of any other backend never reaches it, so its
    construction and read paths behave as on main (harness-parity H13).
    """
    if self._idle_reader_installed:
        return
    self._idle_reader_installed = True
    gate = self._agent_turn_gate = asyncio.Lock()
    prompt_loop, kill_process, reset_state = (
        self._prompt_loop,
        self._kill_process,
        self._reset_state,
    )

    def read_turn(req_id: int, timeout: float) -> AsyncGenerator[tuple[str, JsonRpcMessage], None]:
        return self._read_turn(prompt_loop, req_id, timeout)

    async def kill(*, force: bool = False) -> None:
        self._cancel_idle_reader()
        await kill_process(force=force)

    def reset() -> None:
        self._cancel_idle_reader()
        reset_state()

    # A prompt entry point resets _cancelled and _turn_done and writes its
    # prompt before _prompt_loop takes _turn_lock, so it passes the gate
    # stream_unsolicited holds while it reads, and counts itself in flight.
    def queued(start: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.isasyncgenfunction(start):

            @functools.wraps(start)
            async def queued_stream(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
                async with gate:
                    self._prompts_in_flight += 1
                try:
                    async with aclosing(start(*args, **kwargs)) as events:
                        async for event in events:
                            yield event
                finally:
                    self._prompts_in_flight -= 1

            return queued_stream

        @functools.wraps(start)
        async def queued_call(*args: Any, **kwargs: Any) -> Any:
            async with gate:
                self._prompts_in_flight += 1
            try:
                return await start(*args, **kwargs)
            finally:
                self._prompts_in_flight -= 1

        return queued_call

    # Both: this first runs inside the first ensure_ready, so only the
    # _initialize_session claim covers that handshake, and the ensure_ready
    # claim stops the reader when a later prompt starts.
    bound: dict[str, Callable[..., Any]] = {
        name: self._claiming_stdout(getattr(self, name))
        for name in (
            "ensure_ready",
            "_initialize_session",
            "_wait_for_response",
            "_drain_notifications",
            "wait_for_compaction",
            "_drain_post_compaction_metadata",
            "_read_steer_answers",
        )
    }
    bound.update(_prompt_loop=read_turn, _kill_process=kill, _reset_state=reset)
    for name in ("send_message", "send_message_stream", "stream_events", "stream_command"):
        bound[name] = queued(getattr(self, name))
    for name, method in bound.items():
        setattr(self, name, method)


def _claiming_stdout(
    self: AcpClient, read: Callable[..., Awaitable[_R]]
) -> Callable[..., Awaitable[_R]]:
    @functools.wraps(read)
    async def claimed(*args: Any, **kwargs: Any) -> _R:
        await self._claim_stdout()
        try:
            return await read(*args, **kwargs)
        finally:
            self._release_stdout()

    return claimed


async def _read_turn(
    self: AcpClient,
    prompt_loop: Callable[[int, float], AsyncGenerator[tuple[str, JsonRpcMessage], None]],
    req_id: int,
    timeout: float,
) -> AsyncGenerator[tuple[str, JsonRpcMessage], None]:
    """``_prompt_loop`` with stdout claimed.

    Under ``_AGENT_STARTED_REQUEST_ID`` (``stream_unsolicited``) no response
    arrives, so this yields a ``complete`` of its own on the frame that closes
    the cycle (``_ends_agent_started_turn``), or before the response to a
    request sent while this turn reads, which it leaves at the front of
    ``_buffer`` for that request's reader. The answer to a steering request
    ends no turn: it is passed on as a stray response, as in a user turn.
    """
    first_unsent_id = self._next_id
    unanswered_steering = self._unanswered_steering_ids()
    await self._claim_stdout()
    try:
        # An end marker read before this turn's first activity belongs to an
        # earlier cycle, so it does not end this one.
        saw_activity = False
        async with aclosing(prompt_loop(req_id, timeout)) as frames:
            async for action, msg in frames:
                # _prompt_loop hands a steer that still waits its answer first,
                # so a steering answer that reaches here came too late for it.
                late_steering_answer = msg.method is None and msg.id in unanswered_steering
                if late_steering_answer:
                    unanswered_steering.discard(msg.id)
                if req_id != _AGENT_STARTED_REQUEST_ID or late_steering_answer:
                    yield action, msg
                    continue
                result: dict = {}
                if msg.method is None and isinstance(msg.id, int) and msg.id >= first_unsent_id:
                    self._buffer.appendleft(msg)
                else:
                    yield action, msg
                    saw_activity = saw_activity or _starts_agent_turn(msg)
                    if not (saw_activity and _ends_agent_started_turn(msg)):
                        continue
                    # A prompt's late response keeps its own stop reason and usage.
                    if isinstance(msg.result, dict):
                        result = msg.result
                if not result.get("stopReason"):
                    reason = STOP_REASON_CANCELLED if self._cancelled else STOP_REASON_END_TURN
                    result = {"stopReason": reason}
                yield "complete", JsonRpcMessage(id=req_id, result=result)
                return
    finally:
        self._release_stdout()


async def _claim_stdout(self: AcpClient) -> None:
    self._stdout_claims += 1
    try:
        await self._stop_idle_reader()
    except BaseException:
        # The reader may already be stopped, so undo the claim the way a release does.
        self._release_stdout()
        raise


def _release_stdout(self: AcpClient) -> None:
    self._stdout_claims -= 1
    self._maybe_start_idle_reader()


async def _stop_idle_reader(self: AcpClient) -> None:
    # Cancelling inside readline() loses nothing: StreamReader consumes a
    # line only once its newline is buffered, and _read_message does not
    # suspend between taking the line and returning the frame.
    task = self._idle_reader
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.wait({task})


def _cancel_idle_reader(self: AcpClient) -> None:
    """Stop the idle reader without waiting, for paths that end the process."""
    task = self._idle_reader
    if task is not None and not task.done():
        task.cancel()
    self._idle_reader = None
    self._agent_turn_start = None


def _agent_turn_pending(self: AcpClient) -> bool:
    """True while the frame an agent-started turn began with is still unread.

    Whichever reader takes it from _buffer ends the hand-off: a turn reads it
    as its own, and a request wait files it the way it files any notification.
    """
    start = self._agent_turn_start
    return start is not None and any(frame is start for frame in self._buffer)


def _maybe_start_idle_reader(self: AcpClient) -> None:
    """Read stdout while no turn or request does, so a turn the agent starts is seen."""
    if (
        self.on_agent_turn is None
        or self._stdout_claims
        or self._agent_turn_pending()
        or not self._turn_done.is_set()
        or (self._idle_reader is not None and not self._idle_reader.done())
        or not self._is_process_alive()
    ):
        return
    self._idle_reader = asyncio.get_running_loop().create_task(self._idle_read_loop())


async def _idle_read_loop(self: AcpClient) -> None:
    # The frames go back to the front of _buffer in arrival order however this
    # ends, a cancel included, so the next reader sees what it would have read,
    # less the snapshot frames a later one superseded.
    # Held locally until then because _read_message pops _buffer first.
    # Not after _cancel_idle_reader dropped this reader: the process those
    # frames came from is gone, and _buffer may already serve its successor.
    held: list[JsonRpcMessage] = []
    start: JsonRpcMessage | None = None
    # Stops at the replay buffer's capacity, so putting the frames back drops none.
    room = self._buffer.maxlen or 0
    try:
        while len(held) + len(self._buffer) < room:
            try:
                # The cancel flag belongs to the turn that ended before this
                # reader started, so its grace window does not apply here.
                msg = await self._read_message(timeout=_READ_TIMEOUT, check_cancel=False)
            except AcpError:
                return  # the process is gone; the next caller reports it
            if msg is None:
                if not self._is_process_alive():
                    return
                continue
            held = [frame for frame in held if not _supersedes(msg, frame)]
            held.append(msg)
            if _starts_agent_turn(msg):
                start = msg
                break
    finally:
        if self._idle_reader is asyncio.current_task():
            self._buffer.extendleft(reversed(held))
    if start is None:
        return
    self._agent_turn_start = start
    callback = self.on_agent_turn
    if callback is not None:
        try:
            callback(self)
        except Exception:
            logger.exception("on_agent_turn callback failed")


async def _read_message(
    self: AcpClient, timeout: float = _READ_TIMEOUT, *, check_cancel: bool = True
) -> JsonRpcMessage | None:
    if check_cancel and self._cancelled:
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
            # Both spellings of the tail go past the redactor before any of it
            # is published: a secret wrapped across two stderr lines hides
            # behind the "; " separator otherwise.
            from kiro_crew.acp.stderr_tail import safe_stderr_tail

            stderr_tail = (
                safe_stderr_tail(self._stderr_lines, "; ")
                if self.memory_mode == "persistent"
                else ""
            )
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
    *,
    until_declared: bool = False,
) -> None:
    """Drain init notifications (buffered + live) and log MCP servers.

    ``until_declared`` is the unattended-run wait (see
    ``wait_for_declared_mcp``): quiet does not end the drain, it ends as soon as
    every server the agent spec declares has reported, or at ``duration``.

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
        elif (
            isinstance(update, dict)
            and update.get("sessionUpdate") == UPDATE_CURRENT_MODE
            and self.backend in ACP_BACKEND_FORBIDDEN_MODES
        ):
            # The drain applies no mode report, and clears the buffer it read; a
            # harness whose routing a mode can undo keeps them for the drift check
            # that follows (``_refuse_session_config_drift``). A membership test, so
            # the Kiro path gains no work (harness-parity H13).
            self._drain_mode_reports.append(update)

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
        if until_declared:
            if not self._mcp_report.declared_unreported():
                break
            # No idle exit: a slow server is quiet while it boots.
            idle_remaining = remaining
        else:
            # Early-exit once servers have been quiet for the idle window. Poll
            # in idle-sized slices (capped by remaining) so we notice quiet
            # promptly.
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
