"""The JSON-RPC wire of an MCP stdio server: frames read from stdin, frames written to stdout.

Holds the framing the first frame decides (``Content-Length`` headers or bare JSON lines),
the private stdout fd ``run_mcp_stdio_loop`` snapshots so a tool's own prints cannot
corrupt the stream, ``respond`` and ``notify``, the size limits that keep one oversized
or unparseable frame from desyncing the stream, and the Windows stdin pump.

Split out of ``kiro_crew.mcp_shared``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.mcp_shared.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New wire work goes here.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import sys
import threading
from typing import Any, Literal, Optional

from kiro_crew.json_line import (
    ID_PROBE_BYTES,
    parse_json_object_line,
    recover_line_id,
    recover_top_level_id,
)
from kiro_crew.validation import (
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    ValidationError,
    validate_jsonrpc_response,
)

#: The family logs as ``kiro_crew.mcp_shared``, the name callers and log filters use.
logger = logging.getLogger("kiro_crew.mcp_shared")


# Max frames the Windows stdin pump (``_start_stdin_pump``) reads ahead of the
# loop. Past it the pump blocks and the rest stays in the pipe, so the client
# feels backpressure exactly as it does when the POSIX loop is slow to read.
STDIN_PUMP_MAX_FRAMES = 64

#: Largest frame body :func:`_read_message` reads whole, Content-Length body
#: or bare-JSON line alike: the gateway's default per-line read limit (``mcp_gateway.pool._DEFAULT_READ_BUFFER_LIMIT``,
#: pinned equal by a test so this server need not import the gateway). A larger
#: body is drained in bounded chunks and answered as too large, so neither a
#: corrupt header nor an endless line can allocate it or desync the framing.
MAX_CONTENT_LENGTH_BYTES = 64 * 1024 * 1024


#: A declared length past this is no body any sender drains to: the header is
#: corrupt, and draining to it would swallow every later request.
_MAX_DRAINABLE_LENGTH_BYTES = 16 * MAX_CONTENT_LENGTH_BYTES


_DRAIN_CHUNK_BYTES = 1024 * 1024


# The framing the client's first accepted message used, and so the framing of
# every response. ``None`` until then. On a bare stream a ``Content-Length:``
# line is noise: honouring it would read the requests behind it as a body.
_framing: Literal["bare", "content-length"] | None = None


class _Skipped:
    """What :func:`_read_message` returns for one frame it dropped. Not EOF."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "SKIP"


#: One dropped frame. Distinct from ``None`` (EOF) so a ``null`` line cannot end
#: the loop, and returned per frame so a busy loop gets back to delivering a
#: finished tool's result instead of blocking on the next line.
SKIP = _Skipped()

# ── Private stdout descriptor for JSON-RPC responses ───────────────────────
# The vendored llama-cpp runtime wraps its multi-second GGUF model load in
# ``suppress_stdout_stderr``, which does a PROCESS-WIDE ``dup2(devnull, 1)``
# for the duration of the load (``_vendor/llama_cpp/_utils.py``). The first
# ``local_knowledge_search`` kicks that load on a background thread and returns
# a keyword-only result in milliseconds, so the JSON-RPC response for that very
# call races the load window. Written through fd 1, the bytes land in
# /dev/null: no exception, no short write, the SEL audit still records
# ``success`` -- and the client waits forever until the ACP tool-stall watchdog
# (``acp/client_timeouts.py::_TOOL_STALL_TIMEOUT``, 600s) kills the turn.
#
# ``snapshot_stdout_fd()`` takes an ``os.dup(1)`` at server startup, BEFORE any
# tool can run. A dup'd descriptor keeps pointing at the original pipe no
# matter what a later ``dup2`` does to fd 1, so responses always reach the
# client. Guarded by a lock because it is a raw unbuffered fd: ``os.write`` is
# not atomic across interleaved callers, and a torn frame desyncs the stream
# for every subsequent message.
#
# NOTE: the suppressor also rebinds the ``sys.stdout`` OBJECT to a devnull
# file, so "has sys.stdout been swapped?" is NOT a usable liveness check -- it
# is false exactly inside the window we must survive. The snapshot is the only
# reliable route.
_stdout_fd: Optional[int] = None


_stdout_fd_lock = threading.Lock()


def snapshot_stdout_fd() -> Optional[int]:
    """Capture a private dup of the real stdout descriptor. Idempotent.

    Called once at ``run_mcp_stdio_loop`` entry. Returns the dup'd fd, or
    ``None`` when stdout is not fd-backed (pytest's captured stdout, an
    embedded host handing us a StringIO) -- in that case ``respond()`` falls
    back to ``sys.stdout`` exactly as before.
    """
    global _stdout_fd
    with _stdout_fd_lock:
        if _stdout_fd is not None:
            return _stdout_fd
        try:
            _stdout_fd = os.dup(sys.stdout.fileno())
        except (AttributeError, OSError, ValueError):
            # No usable fileno (StringIO / captured / closed stdout).
            _stdout_fd = None
        return _stdout_fd


def release_stdout_fd() -> None:
    """Close the private stdout dup, if one was captured. Idempotent.

    Keeps the descriptor from leaking when a loop is run repeatedly in one
    process (the test suite drives ``run_mcp_stdio_loop`` many times); a real
    server process exits after its single loop returns.
    """
    global _stdout_fd
    with _stdout_fd_lock:
        fd = _stdout_fd
        _stdout_fd = None
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def _write_all(fd: int, payload: bytes) -> int:
    """Write every byte of ``payload`` to ``fd``; return the count written.

    ``os.write`` on a pipe may accept fewer bytes than offered; a silently
    truncated frame desyncs the JSON-RPC stream for every later message, so
    loop until the payload is fully handed over. Mirrors the short-read loop
    in :func:`_read_message`.

    On failure the ``OSError`` propagates, but the bytes written so far are
    attached as ``bytes_written`` so the caller can tell a clean failure (zero
    bytes — safe to retry on another stream) from a partial one (retrying would
    duplicate the prefix and tear the frame).
    """
    view = memoryview(payload)
    written = 0
    while view:
        try:
            n = os.write(fd, view)
        except OSError as exc:
            exc.bytes_written = written  # type: ignore[attr-defined]
            raise
        view = view[n:]
        written += n
    return written


def respond(req_id: Any, result: Any, error: dict | None = None) -> None:
    """Write a validated JSON-RPC response to stdout."""
    if req_id is None:
        return
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error:
        resp["error"] = error
    else:
        resp["result"] = result
    try:
        resp = validate_jsonrpc_response(resp)
    except ValidationError:
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": "Internal error"},
        }
    _write_frame(resp, label=f"request {req_id}")


def notify(method: str, params: dict[str, Any]) -> None:
    """Write a server->client JSON-RPC notification (no ``id``) to stdout."""
    _write_frame({"jsonrpc": "2.0", "method": method, "params": params}, label=method)


def _write_frame(msg: dict[str, Any], *, label: str) -> None:
    """Frame ``msg`` per the negotiated framing and write it to the client.

    ``label`` names the frame in the torn-write log line (a request id, or a
    notification method).
    """
    body = json.dumps(msg)
    if _framing == "content-length":
        payload = body.encode("utf-8")
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload
    else:
        frame = (body + "\n").encode("utf-8")

    # Preferred path: the private descriptor captured before any tool ran, so a
    # library's process-wide dup2 on fd 1 (see _stdout_fd above) cannot swallow
    # this response. Serialized -- os.write is unbuffered and a partial
    # interleave would tear the frame.
    with _stdout_fd_lock:
        # Re-read INSIDE the lock: a concurrent release_stdout_fd() between an
        # outside-the-lock read and the write could otherwise hand os.write a
        # closed descriptor whose number has already been recycled by another
        # open() -- sending JSON-RPC bytes into an unrelated file. Not reachable
        # from today's single-threaded dispatch, but the lock is already held
        # here, so pay nothing to make it structurally safe.
        fd = _stdout_fd
        if fd is not None:
            try:
                _write_all(fd, frame)
                return
            except OSError as exc:
                # The dup'd fd is unusable (client pipe closed). Fall back to
                # sys.stdout ONLY if nothing was written, so a genuinely broken
                # pipe surfaces the way a direct sys.stdout write would. After a
                # PARTIAL write, re-emitting the whole frame would duplicate the
                # prefix and desync the stream for every later message -- drop
                # it instead and let the client's own timeout handle the turn.
                if getattr(exc, "bytes_written", 0):
                    logger.error(
                        "Torn JSON-RPC frame for %s: wrote %d of %d bytes "
                        "before %s; dropping rather than duplicating the prefix",
                        label,
                        exc.bytes_written,  # type: ignore[attr-defined]
                        len(frame),
                        exc.__class__.__name__,
                    )
                    return
    if _framing == "content-length":
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(frame.decode("utf-8"))
        sys.stdout.flush()


def _read_message(stdin) -> dict[str, Any] | _Skipped | None:
    """Read one JSON-RPC frame, auto-detecting Content-Length vs bare JSON framing.

    Returns the message, ``None`` at end of stream, or :data:`SKIP` for one
    frame that was dropped: a line or body that is not valid UTF-8, not JSON,
    not a JSON object, or nested past the decoder's ceiling (see
    :func:`kiro_crew.json_line.parse_json_object_line`), which is answered as
    a parse error when it is a request whose top-level id is recoverable
    (:func:`_answer_unparseable`), a header-shaped line on a bare-JSON stream,
    and a body over :data:`MAX_CONTENT_LENGTH_BYTES`, which is drained and
    answered as too large when its id is recoverable. A Content-Length header
    whose length cannot be read, or one past any drainable size, ends the
    stream (``None``, logged at ERROR): the body behind an unreadable one
    cannot be delimited, so every later frame would be read joined to it, and
    draining to an absurd one would swallow every later request.

    Uses stdin.buffer (binary mode) for all reads, and keeps the frame as
    bytes until it is parsed, so Content-Length byte counts are honoured
    correctly for multi-byte UTF-8 content.
    """
    global _framing
    raw = stdin.buffer
    # Room for the cap plus a CRLF terminator; the cap counts the body only.
    line = raw.readline(MAX_CONTENT_LENGTH_BYTES + 2)
    if not line:
        return None  # EOF
    terminated = line.endswith(b"\n")
    body_len = len(line) - (2 if line.endswith(b"\r\n") else 1 if terminated else 0)
    if body_len > MAX_CONTENT_LENGTH_BYTES:
        head = line[:ID_PROBE_BYTES]
        tail = line[-ID_PROBE_BYTES:]
        while not terminated:
            chunk = raw.readline(_DRAIN_CHUNK_BYTES)
            if not chunk:
                break
            tail = (tail + chunk[-ID_PROBE_BYTES:])[-ID_PROBE_BYTES:]
            terminated = chunk.endswith(b"\n")
        _refuse_oversized(f"A line of more than {MAX_CONTENT_LENGTH_BYTES} bytes", head, tail)
        return SKIP
    stripped = line.strip()
    if stripped[:15].lower() != b"content-length:":
        # Bare JSON line (backwards compat).
        msg = parse_json_object_line(line)
        if msg is None:
            _answer_unparseable(line)
            return SKIP
        _framing = _framing or "bare"
        return msg
    if _framing == "bare":
        return SKIP
    try:
        length = int(stripped[15:])
    except ValueError:
        length = -1
    if length < 0:
        logger.error(
            "unreadable Content-Length header %r; the framing is lost, ending the stream",
            stripped[:80],
        )
        return None
    if length > _MAX_DRAINABLE_LENGTH_BYTES:
        logger.error(
            "Content-Length %d is past any drainable size; the framing is lost, "
            "ending the stream",
            length,
        )
        return None
    _framing = "content-length"
    # Consume the blank line separator
    while True:
        sep = raw.readline()
        if sep.strip() == b"":
            break
    # Read exactly `length` bytes, in bounded reads: a single raw.read(length)
    # may return fewer bytes than requested on a partial read (the
    # RawIOBase/socket contract permits short reads), which would truncate the
    # body and desync the stream for every subsequent message. A body over the
    # cap is drained the same way, keeping only its two ends for the id.
    keep = length <= MAX_CONTENT_LENGTH_BYTES
    chunks: list[bytes] = []
    head = tail = b""
    remaining = length
    while remaining > 0:
        chunk = raw.read(min(remaining, _DRAIN_CHUNK_BYTES))
        if not chunk:
            # EOF before the declared body fully arrived — the message is
            # incomplete. Discard it explicitly rather than parsing a
            # truncated body, which could otherwise return a message the
            # sender never finished transmitting if the partial bytes happen
            # to be valid JSON (e.g. a well-formed prefix).
            return None
        remaining -= len(chunk)
        if keep:
            chunks.append(chunk)
            continue
        if len(head) < ID_PROBE_BYTES:
            head += chunk[: ID_PROBE_BYTES - len(head)]
        tail = (tail + chunk[-ID_PROBE_BYTES:])[-ID_PROBE_BYTES:]
    if not keep:
        _refuse_oversized(f"Content-Length {length}", head, tail)
        return SKIP
    body = b"".join(chunks)
    msg = parse_json_object_line(body)
    if msg is None:
        _answer_unparseable(body)
        return SKIP
    return msg


def _refuse_oversized(what: str, head: bytes, tail: bytes) -> None:
    """Log a frame drained for being over :data:`MAX_CONTENT_LENGTH_BYTES`, and
    answer it as too large under the top-level id found at either end."""
    req_id = recover_top_level_id(head, tail, requests_only=True)
    logger.warning(
        "%s is over the %d-byte cap; body discarded (id %r)",
        what,
        MAX_CONTENT_LENGTH_BYTES,
        req_id,
    )
    if req_id is not None:
        respond(
            req_id,
            None,
            error={
                "code": JSONRPC_INVALID_REQUEST,
                "message": f"Request over the {MAX_CONTENT_LENGTH_BYTES}-byte limit",
            },
        )


def _answer_unparseable(frame: bytes) -> None:
    """Answer a request frame that does not parse, when its id is recoverable.

    Unanswered, its caller would wait for its own timeout while pings are
    still answered, so nothing would see the server as wedged.
    Only a request (a top-level ``method``) is answered, under the top-level
    object's own id found at either end
    (:func:`kiro_crew.json_line.recover_line_id`); a log line, a response or a
    frame with no recoverable id is just dropped.
    """
    if not frame.lstrip().startswith(b"{"):
        return
    req_id = recover_line_id(frame, requests_only=True)
    logger.warning("unparseable JSON-RPC frame dropped (id %r)", req_id)
    if req_id is not None:
        respond(req_id, None, error={"code": JSONRPC_PARSE_ERROR, "message": "Parse error"})


def _stdin_holds_a_line(stdin) -> bool:
    """True when *stdin*'s buffer already holds a complete line.

    ``select`` polls the descriptor, which cannot see bytes the buffered reader
    has already pulled off it: a request that arrived in the same read as the
    line just handled is invisible to it until the client writes again. The
    peek runs with the descriptor non-blocking, so an empty buffer costs one
    refused read rather than a wait. ``False`` for a stream with no peek or
    descriptor (nothing it holds is hidden from ``select``).
    """
    raw: Any = getattr(stdin, "buffer", None)
    peek = getattr(raw, "peek", None)
    if peek is None:
        return False
    try:
        fd = raw.fileno()
        was_blocking = os.get_blocking(fd)
    except (AttributeError, OSError, ValueError):
        return False
    try:
        os.set_blocking(fd, False)
        try:
            held = peek(1)
        except (BlockingIOError, OSError, ValueError):
            return False
    finally:
        with contextlib.suppress(OSError, ValueError):
            os.set_blocking(fd, was_blocking)
    return b"\n" in held


class _NoFrame:
    """What the loop's frame read returns when no frame arrived in time."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NO_FRAME"


NO_FRAME = _NoFrame()


def _start_stdin_pump(stdin) -> "queue.Queue[Any]":
    """Read *stdin* frames on a daemon thread, in order, into a queue.

    Windows ``select.select`` accepts only sockets, so a loop there cannot poll
    stdin between ticks of a running call. A thread blocked in ``readline``
    waits on it instead, which lets the loop answer pings, cancels and queued
    calls while a tool runs exactly as it does on POSIX. Each item is what
    :func:`_read_message` returned; ``None`` (end of stream) is the last one.
    An exception raised reading a frame is queued as the last item, for the
    loop to raise where a direct read would have. At most
    :data:`STDIN_PUMP_MAX_FRAMES` frames wait in the queue; the pump blocks
    until the loop takes one.
    """
    frames: "queue.Queue[Any]" = queue.Queue(maxsize=STDIN_PUMP_MAX_FRAMES)

    def _pump() -> None:
        while True:
            try:
                msg = _read_message(stdin)
            except BaseException as exc:  # noqa: BLE001 - re-raised by the loop
                frames.put(exc)
                return
            frames.put(msg)
            if msg is None:
                return

    threading.Thread(target=_pump, name="mcp-stdin-pump", daemon=True).start()
    return frames
