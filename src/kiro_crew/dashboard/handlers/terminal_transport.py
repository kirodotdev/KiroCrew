"""Which WebSocket owns a terminal session, and the bytes between that socket and the PTY.

A reconnect takes a session over by replaying its scrollback and publishing the new
socket while output keeps flowing to the old one; the displaced socket is told why and
closed with a bounded wait. Output is recorded into the scrollback and forwarded to the
current owner only, byte for byte; the bash readiness marker is matched on the way, not
removed, and turns into a ``ready`` frame. Input and resize frames go to the PTY.

An owner of :mod:`kiro_crew.dashboard.handlers.terminal`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat

if platform_compat.IS_POSIX:
    import fcntl
    import termios
else:  # pragma: no cover — Windows fallback
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.dashboard.handlers.terminal_session import _TerminalSession


_SCROLLBACK_MAX = 50 * 1024  # 50KB ring buffer per session for reconnect replay


# Unlocked catch-up rounds let a candidate converge while PTY recording keeps
# flowing to the current owner. The last round runs its send under output_lock,
# so takeover always converges even against a continuously streaming PTY; the
# earlier rounds only shorten how long that final pause has to be.
_TAKEOVER_CATCH_UP_ROUNDS = 8


# A candidate owns no session state while replay is in progress. Bound each
# replay frame so one backpressured socket cannot monopolize replace_lock and
# prevent a later healthy reconnect.
_TAKEOVER_REPLAY_SEND_TIMEOUT_S = 5.0


# The final ready frame runs while output publication is paused. Bound that
# pause so a candidate with a wedged transport cannot stall PTY draining.
_TAKEOVER_READY_TIMEOUT_S = 5.0


# Reconnect cleanup is advisory after ownership has either failed or moved.
# Bound each transport operation so a wedged peer cannot retain its handler or
# hold replace_lock indefinitely.
_TERMINAL_WS_CLEANUP_TIMEOUT_S = 1.0


# Advisory control frames (title, cwd, pong) ride the owner's transport lock.
# Bound lock acquisition plus the send so the singleton title poller can never
# park behind a displaced socket's blocked write or a wedged owner transport.
_OWNER_CONTROL_SEND_TIMEOUT_S = 2.0


# Sent once to each displaced socket when a newer connection is published, so
# the displaced window learns why its socket ended and not only the audit log.
# The machine-readable ``code`` lets the frontend stop its automatic redial for
# that socket: two live windows must not displace each other in a loop.
_STALE_OWNER_ERROR_MESSAGE = "Another connection owns this terminal session"


_STALE_OWNER_ERROR_CODE = "displaced"


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data`` to ``fd``, tolerating short writes.

    ``os.write`` on a blocking PTY controller fd may accept fewer bytes than requested
    when the tty input buffer is full: it blocks until *some* space frees, writes
    what fits, and returns a short count. A single ``os.write`` that discards its
    return therefore silently truncates a large paste under a backpressured
    reader. Loop over a ``memoryview`` so partial writes advance without
    reslicing, until the buffer is fully consumed.

    The loop writes through a private ``os.dup`` of ``fd``, taken before the
    first write. A concurrent ``_kill_session`` closes the session's descriptor
    without waiting for in-flight writes, and the kernel may hand that NUMBER
    to an unrelated ``open()`` — a loop still holding the raw number would then
    write the paste's remaining bytes into whatever reused it. The dup pins the
    PTY's file description for the loop's lifetime, so the original can close
    and be reused freely; once the shell side is gone, writes to the dup raise
    (``EIO``) instead of landing elsewhere. The race window shrinks back to the
    single ``dup`` call, no wider than the single-``os.write`` shape this
    replaced. Teardown still never waits on writers — semantics unchanged.

    Runs inside a single executor call so a multi-KB write does not bounce
    per-chunk through the event loop. ``OSError`` (a closed fd at the ``dup``,
    or the PTY torn down mid-loop) propagates to the caller unchanged.
    """
    if not data:
        return
    dup_fd = os.dup(fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(dup_fd, view)
            view = view[written:]
    finally:
        os.close(dup_fd)


def _release_terminal_ws_identity(
    sess: _TerminalSession,
    ws: web.WebSocketResponse,
) -> None:
    """Detach ``ws`` as owner if it still is one, arming the orphan reaper.

    Identity-guarded: a later reconnect may already have replaced ``sess.ws``,
    and clearing it unconditionally would silence PTY output to that owner.
    """
    if sess.ws is ws:
        sess.ws = None
        sess.last_ws_disconnect = time.monotonic()


async def _send_owner_control_frame(
    sess: _TerminalSession,
    ws: web.WebSocketResponse,
    payload: dict[str, Any],
) -> bool:
    """Send one advisory control frame to ``ws`` only while it owns ``sess``.

    Captures the transport lock ``ws`` was published with, re-confirms
    ownership after acquiring it (takeover rotates ``sess.send_lock`` and may
    have displaced ``ws`` while this caller waited), and bounds the whole
    acquire-plus-send so a blocked displaced transport cannot park the caller.
    Returns ``True`` only when the frame was handed to the owner's transport.
    """
    if sess.ws is not ws or ws.closed:
        return False
    send_lock = sess.send_lock

    async def send_under_lock() -> bool:
        async with send_lock:
            if sess.ws is not ws or ws.closed:
                return False
            await ws.send_str(json.dumps(payload))
            return True

    try:
        return await asyncio.wait_for(send_under_lock(), timeout=_OWNER_CONTROL_SEND_TIMEOUT_S)
    except (ConnectionResetError, RuntimeError, OSError, asyncio.TimeoutError):
        return False


async def _close_terminal_ws_bounded(
    ws: web.WebSocketResponse,
    *,
    error_message: str | None = None,
    error_code: str | None = None,
    close_code: int | None = None,
) -> None:
    """Best-effort terminal WebSocket cleanup with bounded transport waits."""
    if ws.closed:
        return
    if error_message is not None:
        frame: dict[str, Any] = {"type": "error", "message": error_message}
        if error_code is not None:
            frame["code"] = error_code
        try:
            await asyncio.wait_for(
                ws.send_str(json.dumps(frame)),
                timeout=_TERMINAL_WS_CLEANUP_TIMEOUT_S,
            )
        except (ConnectionResetError, RuntimeError, OSError, asyncio.TimeoutError):
            pass
    if ws.closed:
        return
    try:
        close = ws.close() if close_code is None else ws.close(code=close_code)
        await asyncio.wait_for(
            close,
            timeout=_TERMINAL_WS_CLEANUP_TIMEOUT_S,
        )
    except (ConnectionResetError, RuntimeError, OSError, asyncio.TimeoutError):
        pass


async def _replace_terminal_ws(
    sess: _TerminalSession,
    ws: web.WebSocketResponse,
) -> bool:
    """Replay retained output, then publish ``ws`` as the sole session owner."""
    async with sess.replace_lock:
        async with sess.output_lock:
            sent_output_bytes = sess.output_bytes
            retained_output = bytes(sess.scrollback)

        def catch_up_span() -> int | None:
            """Bytes this candidate still lacks, or None when a live owner would
            see a gap because the bounded ring already lost some of them."""
            missing = sess.output_bytes - sent_output_bytes
            if missing > len(sess.scrollback):
                if sess.ws is not None:
                    # The ring overtook this candidate during replay. Keep
                    # the current owner rather than publish with a gap.
                    return None
                # Nobody is attached: deliver what the ring still holds
                # rather than refuse the owner's only window.
                return len(sess.scrollback)
            return missing

        async def catch_up_once() -> bool:
            nonlocal sent_output_bytes
            current_output_bytes = sess.output_bytes
            if current_output_bytes == sent_output_bytes:
                return True
            missing = catch_up_span()
            if missing is None:
                return False
            if missing:
                await asyncio.wait_for(
                    ws.send_bytes(bytes(sess.scrollback[-missing:])),
                    timeout=_TAKEOVER_REPLAY_SEND_TIMEOUT_S,
                )
            sent_output_bytes = current_output_bytes
            return True

        # PTY output continues to the current owner while output_bytes records
        # what this unpublished candidate still needs.
        try:
            if retained_output:
                await asyncio.wait_for(
                    ws.send_bytes(retained_output),
                    timeout=_TAKEOVER_REPLAY_SEND_TIMEOUT_S,
                )
            for round_index in range(_TAKEOVER_CATCH_UP_ROUNDS):
                final_round = round_index == _TAKEOVER_CATCH_UP_ROUNDS - 1
                if not final_round:
                    # Unlocked rounds keep PTY recording flowing to the current
                    # owner while this candidate catches up.
                    if not await catch_up_once():
                        return False
                async with sess.output_lock:
                    if final_round:
                        # Output cannot advance while output_lock is held, so
                        # this one bounded send converges even against a
                        # continuously streaming PTY (`tail -f`, a verbose
                        # build). Without it a reload with no live owner could
                        # never reattach until the stream paused.
                        missing = catch_up_span()
                        if missing is None:
                            return False
                        if missing:
                            await asyncio.wait_for(
                                ws.send_bytes(bytes(sess.scrollback[-missing:])),
                                timeout=_TAKEOVER_REPLAY_SEND_TIMEOUT_S,
                            )
                        sent_output_bytes = sess.output_bytes
                    elif sess.output_bytes != sent_output_bytes:
                        continue
                    next_send_lock = asyncio.Lock()
                    if sess.shell_ready:
                        # The handler cannot read candidate input until this
                        # function returns. Send ready before publication so a
                        # failed transport leaves the healthy owner in place;
                        # output cannot advance while output_lock is held.
                        await asyncio.wait_for(
                            ws.send_str(
                                json.dumps(
                                    {
                                        "type": "ready",
                                        "shell": sess.shell,
                                        "fence_shells": sess.fence_shells,
                                    }
                                )
                            ),
                            timeout=_TAKEOVER_READY_TIMEOUT_S,
                        )
                    displaced_ws = sess.ws
                    stale_output_cancel = sess.output_send_cancel
                    sess.output_send_cancel = None
                    if stale_output_cancel is not None:
                        stale_output_cancel.set()
                    # Rotate the transport lock so an in-flight send to the
                    # displaced socket cannot block the new owner.
                    sess.send_lock = next_send_lock
                    sess.ws = ws
                    sess.last_ws_disconnect = None
                    sess.last_title = None
                    sess.last_cwd = None
                    sess.frames_dirty = True
                if displaced_ws is not None and displaced_ws is not ws:
                    # Closing outside output_lock wakes an idle displaced
                    # handler without pausing PTY recording or publication.
                    # The one coarse error frame tells that window why its
                    # socket ended; later stale frames add only an audit.
                    try:
                        await _close_terminal_ws_bounded(
                            displaced_ws,
                            error_message=_STALE_OWNER_ERROR_MESSAGE,
                            error_code=_STALE_OWNER_ERROR_CODE,
                        )
                    except BaseException:
                        # Only cancellation escapes the bounded close. The
                        # candidate's handler is ending before it reaches its
                        # own identity-guarded teardown, so detach it here or
                        # the session would keep a dead owner the orphan
                        # reaper never sees.
                        _release_terminal_ws_identity(sess, ws)
                        raise
                return True
            raise AssertionError(
                "unreachable: the final catch-up round always publishes or returns"
            )
        except (ConnectionResetError, RuntimeError, OSError, asyncio.TimeoutError):
            # The candidate disappeared before takeover completed. Keep the
            # previous owner and disconnect state authoritative.
            return False


async def _record_and_forward_terminal_output(
    sess: _TerminalSession,
    data: bytes,
) -> None:
    """Record one PTY read and forward it to the current owner."""
    send_task: asyncio.Task[None] | None = None
    cancel_event: asyncio.Event | None = None
    async with sess.output_lock:
        became_ready = _consume_ready_marker(sess, data)
        if sess.winpty is not None and not sess.shell_ready:
            sess.shell_ready = True
            became_ready = True
        sess.scrollback.extend(data)
        sess.output_bytes += len(data)
        sess.frames_dirty = True
        if len(sess.scrollback) > _SCROLLBACK_MAX:
            sess.scrollback = sess.scrollback[-_SCROLLBACK_MAX:]
        live = sess.ws
        send_lock = sess.send_lock
        if live is not None and not live.closed:

            async def forward_to_owner() -> None:
                async with send_lock:
                    if sess.ws is not live or live.closed:
                        return
                    await live.send_bytes(data)
                    if became_ready and sess.ws is live:
                        try:
                            await live.send_str(
                                json.dumps(
                                    {
                                        "type": "ready",
                                        "shell": sess.shell,
                                        "fence_shells": sess.fence_shells,
                                    }
                                )
                            )
                        except (ConnectionResetError, RuntimeError, OSError):
                            pass

            send_task = asyncio.create_task(forward_to_owner())
            cancel_event = asyncio.Event()
            sess.output_send_cancel = cancel_event

    if send_task is not None and cancel_event is not None:
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                (send_task, cancel_wait),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_wait in done and not send_task.done():
                send_task.cancel()
            result = (await asyncio.gather(send_task, return_exceptions=True))[0]
            if isinstance(result, asyncio.CancelledError):
                return
            if isinstance(result, (ConnectionResetError, RuntimeError, OSError)):
                return
            if isinstance(result, BaseException):
                raise result
        finally:
            cancel_wait.cancel()
            if not send_task.done():
                send_task.cancel()
            await asyncio.gather(send_task, cancel_wait, return_exceptions=True)
            async with sess.output_lock:
                if sess.output_send_cancel is cancel_event:
                    sess.output_send_cancel = None


async def _write_terminal_input(
    sess: _TerminalSession,
    ws: web.WebSocketResponse,
    data: bytes,
) -> bool:
    """Write one owner frame to the PTY; reject frames from displaced sockets."""
    async with sess.write_lock:
        if sess.ws is not ws:
            return False
        if sess.winpty is not None:
            # ConPTY's write buffers the full payload and returns len(data)
            # unconditionally (see conpty.WindowsPty.write).
            await asyncio.get_running_loop().run_in_executor(
                None,
                sess.winpty.write,
                data,
            )
        else:
            # Read the fd once before the offload: a concurrent kill sets the
            # fd to -1 before close, so os.write raises OSError.
            await asyncio.get_running_loop().run_in_executor(
                None,
                _write_all,
                sess.master_fd,  # wokeignore:rule=master
                data,
            )
        if b"\r" in data or b"\n" in data:
            # A submitted line may be a `cd`, so invalidate state derived from
            # the previous working directory only after an accepted write.
            sess.cwd_probe = None
            sess.frames_dirty = True
        return True


async def _resize_terminal(
    sess: _TerminalSession,
    ws: web.WebSocketResponse,
    cols: int,
    rows: int,
) -> bool:
    """Resize the PTY only when ``ws`` still owns the session."""
    async with sess.write_lock:
        if sess.ws is not ws:
            return False
        sess.cols = cols
        sess.rows = rows
        if sess.winpty is not None:
            try:
                sess.winpty.resize(cols, rows)
            except OSError:
                pass
        else:
            try:
                fcntl.ioctl(
                    sess.master_fd,  # wokeignore:rule=master
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
            except OSError:
                pass
        return True


def _consume_ready_marker(sess: "_TerminalSession", data: bytes) -> bool:
    """Advance the split-safe Bash marker matcher for one raw PTY read."""
    marker = sess.ready_marker
    if sess.shell_ready or marker is None:
        return False
    combined = bytes(sess.ready_probe) + data
    if marker in combined:
        sess.shell_ready = True
        sess.ready_marker = None
        sess.ready_probe.clear()
        return True
    keep = max(0, len(marker) - 1)
    sess.ready_probe = bytearray(combined[-keep:]) if keep else bytearray()
    return False
