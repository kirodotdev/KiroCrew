"""WebSocket PTY handler for the built-in CLI panel."""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import re
import shutil
import stat
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Protocol

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.config import live
from kiro_crew.config.loader import config_path
from kiro_crew.dashboard import terminal_commands
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.origin import check_origin, mark_audit_claimed
from kiro_crew.executors import discovery_executor, subprocess_executor
from kiro_crew.hooks import validate_file_path
from kiro_crew.notifications.bus import NotificationBus, NotificationPayload
from kiro_crew.sandbox import _PYTHON_ENV_PREFIXES, RLIMIT_PROFILE_NONE, spawn_shim_argv
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)

# PTY support is POSIX-only (openpty/fork/ioctl/termios). On Windows these
# modules do not exist; the web-terminal panel degrades to a clear error.
if platform_compat.IS_POSIX:
    import fcntl
    import pty as _pty
    import termios
else:  # pragma: no cover — Windows fallback
    fcntl = None  # type: ignore[assignment]
    _pty = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

# Global ceiling across ALL chats' terminal tabs. Each chat's activity bar caps
# its own terminals (frontend MAX_TERMINALS_PER_CHAT); this is the server-side
# backstop. Override via config.json dashboard.terminal.max_sessions.
_MAX_SESSIONS = 12
# Bound on a ``{session_id}`` URL path param, applied by BOTH routes that read
# one. Named rather than repeated because the two call sites drifted while the
# bound was a bare literal: WS-open enforced it and DELETE did not.
_MAX_SESSION_ID_LEN = 64
_ORPHAN_TIMEOUT_S = 900  # 15 min with no WS → reap PTY (grace window for reload/network drops; in-app nav keeps the WS alive)
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
# Private-use WebSocket close code: the shell is gone, so clients must dispose
# the terminal instead of treating the close as a transient drop and redialing.
_TERMINAL_WS_CLOSE_SHELL_EXITED = 4001
# Exits recorded for a dial that finds no registry entry (a popout whose shell
# died before the new window connected). Kept OUTSIDE the registry so no
# ``registry.get`` caller sees a second entry kind. The window is the orphan
# timeout: past it a dial is a new session anyway.
_EXITED_TTL_S = float(_ORPHAN_TIMEOUT_S)
# Oldest dropped first. Every retained field is bounded: the id by
# _MAX_SESSION_ID_LEN at the route, the status and stamp by their own types,
# the close flag by being a bool, and the replay bytes by the two ceilings
# below.
_EXITED_MAX = 64
# Records dropped by the cap rather than by expiry. A dial to a dropped id
# opens a fresh shell, so each drop is counted and logged.
_exited_evicted = 0
# A reaped session's ring buffer is gone, so the exit record keeps a copy for a
# window that was detached when the shell exited: without it that reconnect is
# told the exit but shows an empty pane. Per record this mirrors the live ring,
# and _EXITED_MAX alone would then admit 64 x 50KB = 3.2MB.
_EXITED_SCROLLBACK_MAX = _SCROLLBACK_MAX
# So a second ceiling bounds the store as a whole. Over it the OLDEST records
# give up their replay bytes rather than the record itself: the exit fact and
# its status are what stop a dial getting a fresh shell, and they cost a few
# dozen bytes, while the replay is the part a user can lose and still be told
# the truth.
_EXITED_SCROLLBACK_BUDGET = 512 * 1024
# Replay payloads shed by that budget. Counted separately from _exited_evicted
# because the consequence differs: a shed record still answers the dial.
_exited_scrollback_shed = 0


class _ExitedRecord(NamedTuple):
    """What is remembered about a shell that exited, for _EXITED_TTL_S."""

    at: float
    """``time.monotonic()`` when the exit was recorded."""
    status: int | None
    """The child's exit status; ``None`` when the probe could not resolve one."""
    close: bool
    """The close decision taken AT exit time, so a reconnect applies the value
    in force when the shell died rather than re-reading a since-toggled
    setting and disagreeing with the window that saw the exit live."""
    scrollback: bytes
    """Bounded tail of the session's output, replayed to a detached reconnect.
    Empty once the store's byte budget sheds it."""


# Ceiling on the wait for the child's status after reader EOF; an unresolved
# status reports as ``null``. One bound for both backends, set by ConPTY, which
# took 3.08s to report a clean ``cmd.exe`` exit on windows-latest. POSIX
# resolves in well under 20ms, so the bound only matters for a status that
# never arrives.
_CHILD_REAP_TIMEOUT_S = 10.0
# ConPTY has no child watcher, so the status is polled at this interval.
_CONPTY_STATUS_POLL_S = 0.05


def _sel():
    import kiro_crew.dashboard.handlers as _pkg  # circular import: __init__ imports terminal

    return _pkg.sel()


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


class _ConptyBackend(Protocol):
    """Structural type for the Windows ConPTY backend (:class:`kiro_crew.conpty.WindowsPty`).

    Declared as a Protocol rather than importing WindowsPty so this module stays
    importable on POSIX, where ``conpty`` pulls in Windows-only bindings. Typing
    the field as bare ``object`` made every ``sess.winpty.<method>()`` call an
    ``attr-defined`` error and pushed callers toward scattered ``type: ignore``
    comments; the Protocol keeps the call sites checked instead.
    """

    @property
    def pid(self) -> int: ...

    def read(self, size: int = 4096) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def resize(self, cols: int, rows: int) -> None: ...

    def isalive(self) -> bool: ...

    def exitstatus(self) -> int | None: ...

    def terminate(self, force: bool = True) -> None: ...


@dataclass
class _TerminalSession:
    """Server-side state for one PTY session."""

    session_id: str
    master_fd: int
    proc: "asyncio.subprocess.Process | None" = None
    winpty: _ConptyBackend | None = None  # WindowsPty (ConPTY) backend on Windows
    cols: int = 80
    rows: int = 24
    created_at: float = field(default_factory=time.monotonic)
    last_ws_disconnect: float | None = None  # set when WS drops, cleared on reconnect
    ws: web.WebSocketResponse | None = None
    reader_task: asyncio.Task | None = None
    scrollback: bytearray = field(default_factory=bytearray)
    last_title: str | None = None  # last title pushed to the client (dedup)
    last_cwd: str | None = None  # last cwd pushed to the client (dedup)
    # Absolute path of the shell this PTY actually launched, as _resolve_shell
    # pinned it. Reported to the client in the `ready` frame: the client mints
    # session ids and opens the socket without ever asking what got spawned, so
    # a caller that needs to know which shell will interpret the bytes it is
    # about to write (Run-in-terminal, which honors a code fence's language)
    # has no other way to find out. Reporting is deliberately one-way -- the
    # client is never given a say in WHICH shell is spawned.
    shell: str = ""
    # name -> absolute path for the shells a code fence can name, as resolved on
    # this host. Reported alongside `shell` so a caller handing a snippet to a
    # different shell can name an absolute path instead of a bare name a
    # project-local PATH entry could hijack.
    fence_shells: dict[str, str] = field(default_factory=dict)
    # (monotonic_ts, cwd) memo for the path-completion route. The title poller's
    # ``last_cwd`` is up to a second stale, which is long enough for a user to
    # `cd` and immediately request completions against the OLD directory — so
    # completion probes the shell itself and memoizes here instead (see
    # _session_cwd_cached). Cleared as soon as the client submits a line, since
    # that line may be the `cd` the memo would otherwise hide.
    cwd_probe: tuple[float, str | None] | None = None
    # Whether the title/cwd poller still has anything to recompute. A shell
    # cannot change directory, or start or finish a command, without writing to
    # the PTY — so a session that has produced no output since the last probe
    # cannot have gone stale, and probing it again would spend a process
    # introspection call (a forked `lsof` where nothing cheaper answers) to
    # re-derive a label it already has. Set on PTY output, on a submitted line,
    # and on (re)connect, where the dedup markers are cleared and both frames
    # must be pushed again.
    frames_dirty: bool = True
    # Set once the exit note is published, so the three exit paths publish it
    # at most once between them (see ``_announce_exit``).
    exit_announced: bool = False
    # A WebSocket upgrade completes before startup has yielded the terminal back
    # to a freshly spawned login shell. Run-in-terminal callers must not release
    # queued commands until this barrier has been crossed.
    shell_ready: bool = False
    # Bash inherits a PROMPT_COMMAND that emits this randomized OSC marker once,
    # after its login profiles return. ``ready_probe`` retains the small suffix
    # needed when the marker straddles two PTY reads; output itself is still
    # forwarded byte-for-byte.
    ready_marker: bytes | None = None
    ready_probe: bytearray = field(default_factory=bytearray)
    # Serializes concurrent WS writes (reader loop + title poller + pong);
    # aiohttp's WebSocket writer is not safe for concurrent sends.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The sole PTY reader races its transport child against this event. Takeover
    # signals it at publication, releasing the reader even when the displaced
    # socket is blocked on its old send lock or inside flow control.
    output_send_cancel: asyncio.Event | None = None
    # Counts PTY output bytes over the session lifetime. Reconnect uses it to
    # catch output produced while replay awaits the candidate socket.
    output_bytes: int = 0
    # Linearizes output recording and owner publication. Socket sends never hold
    # this lock except the bounded final ready frame, so a slow client cannot
    # normally stop the PTY reader from recording bytes for reconnect replay.
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes reconnect candidates without coupling takeover to the current
    # owner's socket writes or an in-flight PTY write.
    replace_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes client-to-PTY input and resize across handlers. A displaced
    # handler may keep draining its socket after reconnect, so each operation
    # must confirm ownership while holding this lock. It also keeps a frame
    # contiguous when ``_write_all`` needs several ``os.write`` calls.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
        return await asyncio.wait_for(
            send_under_lock(), timeout=_OWNER_CONTROL_SEND_TIMEOUT_S
        )
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
    *,
    can_publish: Callable[[], bool],
) -> bool:
    """Replay retained output, then publish ``ws`` as the sole session owner.

    ``can_publish`` is re-checked at each point the candidate could otherwise
    publish to a session that has lost registry identity or whose child exited.
    """
    async with sess.replace_lock:
        # A reconnect may have captured ``sess`` before an exit path queued it
        # on this lock, then resume after that path removed the registry entry.
        # Validate inside the lock so such a stale candidate cannot publish to
        # a detached session object.
        if not can_publish():
            return False
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
                    # Teardown paths do not all take replace_lock. Revalidate
                    # after replay and again after the ready-frame await so a
                    # candidate never publishes after losing registry identity
                    # or after its child has exited.
                    if not can_publish():
                        return False
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
                    if not can_publish():
                        return False
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
            raise AssertionError("unreachable: the final catch-up round always publishes or returns")
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
                None, sess.winpty.write, data,
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


def _get_registry(request: web.Request) -> dict[str, _TerminalSession | None]:
    state: DashboardState = request.app["state"]
    return state._terminal_sessions


def _get_exited(request: web.Request) -> dict[str, _ExitedRecord]:
    """Records of shells that exited on their own, keyed by session id."""
    return _exited_for_state(request.app["state"])


def _exited_for_state(state: object) -> dict[str, _ExitedRecord]:
    """``_get_exited`` for a caller with no request (the orphan sweep).

    Type-checked, not trusted: a ``MagicMock`` state's truthy auto-attribute
    would otherwise read as "every session has exited".
    """
    exited = getattr(state, "_terminal_exited", None)
    if not isinstance(exited, dict):
        exited = {}
        try:
            state._terminal_exited = exited  # type: ignore[attr-defined]
        except AttributeError:
            pass  # a slotted or frozen stub: fall back to a per-call throwaway
    return exited


async def _dead_session_status(sess: "_TerminalSession") -> int | None:
    """Exit status of a session already known dead; ``None`` if the probe raises."""
    try:
        return await _child_exit_status(sess)
    except Exception:  # noqa: BLE001 -- an unknown status is a valid answer
        return None


def _announce_exit(
    sess: "_TerminalSession",
    status: int | None,
    *,
    bus: "NotificationBus | None",
    exited: dict[str, _ExitedRecord] | None,
    close: bool,
) -> None:
    """Apply the exit disposition: record the exit, publish the note once.

    Called by every path that can find a shell dead (the reader's end, a
    reconnect that finds a dead but still-registered entry, the orphan sweep),
    so none of them answers a dead session with a fresh shell. Synchronous so
    callers can pop the entry immediately after with no await in between, which
    is also what makes the ring read below safe: the PTY appends to it under
    ``output_lock`` from a coroutine, and no await here yields to one.

    The bell note is published only when ``close`` is set: with the tab kept,
    the shell's output and exit line are still on screen, so the note would
    duplicate what the user can read.
    """
    if exited is not None:
        _record_exited(
            exited,
            sess.session_id,
            status,
            close=close,
            scrollback=bytes(sess.scrollback),
        )
    if sess.exit_announced:
        return
    sess.exit_announced = True
    if close and bus is not None and status != 0:
        _notify_abnormal_exit(bus, sess, status)


def _prune_exited(exited: dict[str, _ExitedRecord], *, now: float) -> None:
    """Drop records past _EXITED_TTL_S, then the oldest over the two ceilings.

    Insertion order is age order, so the oldest is the first key. ``now`` is a
    ``time.monotonic()`` reading, so a wall-clock step cannot expire a record.
    """
    global _exited_evicted, _exited_scrollback_shed
    cutoff = now - _EXITED_TTL_S
    for session_id in [sid for sid, rec in exited.items() if rec.at <= cutoff]:
        del exited[session_id]
    evicted = 0
    while len(exited) > _EXITED_MAX:
        del exited[next(iter(exited))]
        evicted += 1
    if evicted:
        _exited_evicted += evicted
        logger.warning(
            "terminal exit records over the %d cap: dropped the %d oldest "
            "(%d dropped since start); a reconnect to a dropped session "
            "opens a fresh shell",
            _EXITED_MAX, evicted, _exited_evicted,
        )
    # Then the byte budget, which sheds replay bytes and keeps the record.
    held = sum(len(rec.scrollback) for rec in exited.values())
    shed = 0
    for session_id, rec in list(exited.items()):  # oldest first
        if held <= _EXITED_SCROLLBACK_BUDGET:
            break
        if not rec.scrollback:
            continue
        held -= len(rec.scrollback)
        exited[session_id] = rec._replace(scrollback=b"")
        shed += 1
    if shed:
        _exited_scrollback_shed += shed
        logger.warning(
            "terminal exit replay over the %d-byte budget: dropped the output "
            "of the %d oldest record(s) (%d dropped since start); those "
            "sessions still report their exit, with an empty pane",
            _EXITED_SCROLLBACK_BUDGET, shed, _exited_scrollback_shed,
        )


def _record_exited(
    exited: dict[str, _ExitedRecord],
    session_id: str,
    status: int | None,
    *,
    close: bool,
    scrollback: bytes = b"",
    now: float | None = None,
) -> None:
    """Remember that ``session_id``'s shell exited, and with what status.

    Recorded even when an owner socket was told, because another window holding
    the same id (a popout) can still dial.

    The replay copy is kept only when the tab will be KEPT: with ``close`` set
    the client disposes of the tab on the exit frame, so nothing would ever
    render those bytes and holding them would spend the store's budget on
    output no one can read.
    """
    stamp = time.monotonic() if now is None else now
    exited.pop(session_id, None)  # re-insert so insertion order stays age order
    exited[session_id] = _ExitedRecord(
        at=stamp,
        status=status,
        close=close,
        scrollback=b"" if close else scrollback[-_EXITED_SCROLLBACK_MAX:],
    )
    _prune_exited(exited, now=stamp)


def _recorded_exit(
    exited: dict[str, _ExitedRecord],
    session_id: str,
    *,
    now: float | None = None,
) -> _ExitedRecord | None:
    """This id's record when its shell exited within the window, else ``None``.

    A peek, not a pop: two stale windows can hold the same id and both must be
    told. Expiry releases the record.
    """
    stamp = time.monotonic() if now is None else now
    _prune_exited(exited, now=stamp)
    return exited.get(session_id)


def _get_config(request: web.Request | None = None) -> dict:
    """The ``dashboard.terminal`` object, or ``{}`` for anything malformed.

    Every level is type-checked rather than chained, and the read fails CLOSED to
    the default. config.json is hand-edited, so a non-object at any level --
    ``"dashboard": false``, a number, a string, a list, or a document that is not
    an object at all -- would make a chained ``.get`` raise ``AttributeError``,
    which is NOT in the caught set below. That surfaced as an HTTP 500 on every
    terminal route, including the per-keystroke completion one, from a single typo.

    Returning the empty default (rather than propagating) is the convention the
    nested reads already follow: a malformed value means "nothing configured",
    which is also what an absent key means.
    """
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    dashboard = data.get("dashboard")
    if not isinstance(dashboard, dict):
        return {}
    cfg = dashboard.get("terminal")
    return cfg if isinstance(cfg, dict) else {}


def _is_enabled(request: web.Request) -> bool:
    """Terminal panel is enabled by default. Disable via config.json:
    {"dashboard": {"terminal": {"enabled": false}}}
    Cached for 30s to avoid disk I/O per request.
    """
    now = time.monotonic()
    if now - _enabled_cache[1] < 30:
        return _enabled_cache[0]
    result = bool(_get_config(request).get("enabled", True))
    _enabled_cache[0] = result
    _enabled_cache[1] = now
    return result


_enabled_cache: list = [True, 0.0]  # [value, timestamp]


def _completion_cfg(request: web.Request) -> dict:
    """The ``dashboard.terminal.completion`` object, or ``{}``.

    Type-checked at BOTH levels because config.json is hand-edited: `"terminal":
    false` would make `.get("completion")` raise on a boolean and `"completion":
    false` would make the next `.get` raise — each an HTTP 500 from a typo on a
    per-keystroke route. A non-object at either level means "nothing configured",
    which is also the default.

    Deliberately NOT memoised in ``_enabled_cache``: that slot belongs to
    ``_is_enabled``, and a second flag sharing it would cross-contaminate the two.
    Callers pay the executor-offloaded read the command tier already paid.
    """
    cfg = _get_config(request)
    if not isinstance(cfg, dict):
        return {}
    inner = cfg.get("completion")
    return inner if isinstance(inner, dict) else {}


def _close_on_exit() -> bool:
    """Whether an exited shell closes its tab (``dashboard.terminal.close_on_exit``).

    Off by default: the tab stays, showing the shell's output and its exit
    code. Read from the live config watcher's snapshot -- never from disk --
    because the exit paths run on the event loop. The gateway primes the
    watcher at boot, so a Settings toggle applies to the next exit; with no
    watcher armed the read fails closed to the default.
    """
    snap = live.snapshot()
    if snap is None:
        return False
    cfg = snap.dashboard.terminal
    return isinstance(cfg, dict) and cfg.get("close_on_exit") is True


def _completion_disabled(completion_cfg: dict) -> bool:
    """True only for a literal ``enabled: false``.

    Any other value — a JSON string, a number, null, or an absent key — degrades
    to the default (enabled). ``bool("false") is True``, so coercing would turn a
    hand-edited string into the opposite of what it reads like.
    """
    return completion_cfg.get("enabled", True) is False


def _resolve_cwd(cfg: dict, requested: str | None) -> str:
    """Resolve the PTY working directory.

    A valid client-requested dir (the chat's project dir, passed as ?cwd=) wins;
    otherwise the configured cwd, else $HOME. The requested dir must be an
    existing directory — this is the user's own interactive shell (auth is
    enforced at the WS handshake), so there is no root restriction beyond isdir.
    """
    default = cfg.get("cwd") or os.environ.get("HOME") or "/"
    if requested:
        candidate = os.path.abspath(os.path.expanduser(requested))
        if os.path.isdir(candidate):
            return candidate
        logger.warning("terminal: ignoring invalid cwd %r", requested)
    return default


def _resolve_shell(cfg: dict) -> tuple[str, str | None]:
    """Resolve the shell program the terminal launches.

    Resolution order is the configured ``dashboard.terminal.shell``, else
    ``$SHELL`` (POSIX only), else the platform default (``/bin/bash`` /
    ``powershell.exe``). Each candidate
    must now resolve to an executable (``shutil.which`` handles both absolute
    paths and bare names on ``PATH``). A configured value that does not resolve
    falls back rather than failing the open: a typo'd setting must never leave
    the user without a terminal.

    The value returned is the ABSOLUTE path ``shutil.which`` resolved, not the
    candidate as written: the spawn runs with the session's cwd (the chat's
    project directory), so a bare name would be resolved a second time there,
    and a relative ``PATH`` entry would let a project-planted executable win a
    race the validation here already decided. Pinning the resolved path makes
    the program validated the program spawned.

    Blocking note: ``shutil.which`` stats every ``PATH`` entry, so callers on
    the event loop must run this via an executor (they do — see the three call
    sites), never inline.

    Returns ``(shell, rejected)`` where ``rejected`` is the configured value
    when it was set but skipped, so callers can surface the fallback (session
    response, log) instead of silently launching a different shell.

    When no candidate resolves at all, the platform default is returned
    unvalidated so the spawn's own error — not a silent substitution — is what
    the user sees on such a host.
    """
    configured = str(cfg.get("shell") or "").strip()
    if configured:
        resolved = shutil.which(configured)
        if resolved:
            return os.path.abspath(resolved), None
    rejected = configured or None
    if platform_compat.IS_WINDOWS:
        candidates = ["powershell.exe"]
    else:
        env_shell = os.environ.get("SHELL", "")
        candidates = ([env_shell] if env_shell else []) + ["/bin/bash"]
    for cand in candidates:
        resolved = shutil.which(cand)
        if resolved:
            # abspath (both branches): `which` joins the matching PATH entry
            # verbatim, so a RELATIVE entry (PATH=bin:…) yields a relative
            # result the spawn's project cwd would re-resolve — exactly the
            # substitution pinning exists to prevent. Anchoring here binds the
            # path to the gateway cwd the validation ran in.
            return os.path.abspath(resolved), rejected
    return candidates[-1], rejected


def _proc_comm(pid: int) -> str | None:
    """Command name of a process (Linux /proc). None if unavailable."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


# Trusted absolute locations for the lsof binary used by the macOS/BSD cwd
# fallback. Resolving a bare "lsof" through inherited PATH would let anything
# that can prepend a PATH entry (e.g. an activated workspace virtualenv's bin/)
# hijack the spawn with gateway privileges, so we only ever execute these fixed
# system paths and fail closed (no cwd frame) when none exists.
_LSOF_PATHS = ("/usr/sbin/lsof", "/usr/bin/lsof")


def _proc_cwd(pid: int) -> str | None:
    """Current working directory of a process, or None if it cannot be read.

    Prefers the subprocess-free sources (``/proc`` on Linux, ``libproc`` on
    macOS) and only falls back to ``lsof -d cwd`` — whose ``-Fn`` output carries
    the path on an ``n``-prefixed line — on a host where neither answers. That
    fallback forks, so callers must still run this off the event loop.
    """
    cwd = platform_compat.process_cwd(pid)
    if cwd is not None:
        return cwd
    lsof = next((p for p in _LSOF_PATHS if os.path.isfile(p)), None)
    if not lsof:
        return None  # fail closed rather than resolve via PATH
    try:
        out = subprocess.run(
            [lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        for line in out.splitlines():
            if line.startswith("n") and len(line) > 1:
                return line[1:]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# How long a cwd probe may be reused. Short enough that `cd foo` followed
# immediately by a completion sees the new directory, long enough that one poll
# tick — which needs the cwd for both the title and the cwd frame — probes the
# shell once rather than twice, and that holding a key down does not re-probe
# per keystroke.
_CWD_PROBE_TTL_S = 0.4


def _session_cwd(sess: "_TerminalSession") -> str | None:
    """Full current working directory of the session's shell, or None.

    Memoized on the session for :data:`_CWD_PROBE_TTL_S`, because the answer has
    two consumers a fraction of a millisecond apart (the title label and the cwd
    frame) and on a host where ``_proc_cwd`` has to fork ``lsof`` the probe, not
    the polling, is the cost that matters.
    """
    if not platform_compat.IS_POSIX or sess.proc is None:
        return None
    now = time.monotonic()
    probe = sess.cwd_probe
    if probe is not None and now - probe[0] < _CWD_PROBE_TTL_S:
        return probe[1]
    cwd = _proc_cwd(sess.proc.pid)
    sess.cwd_probe = (now, cwd)
    return cwd


async def _session_cwd_cached(sess: "_TerminalSession") -> str | None:
    """``_session_cwd`` probed off the event loop, skipping the executor hop
    entirely on a memo hit.

    The title poller's ``sess.last_cwd`` is deliberately NOT reused here: it is
    refreshed on a 1 s cadence, so a completion issued right after a ``cd``
    would resolve against the previous directory. The TTL is not the only
    guard — the WebSocket write path drops the memo whenever the client submits
    a line, so a ``cd`` invalidates it immediately rather than after the TTL."""
    now = time.monotonic()
    probe = sess.cwd_probe
    if probe is not None and now - probe[0] < _CWD_PROBE_TTL_S:
        return probe[1]
    loop = asyncio.get_running_loop()
    cwd = await loop.run_in_executor(subprocess_executor(), _session_cwd, sess)
    # _session_cwd memoizes its own probes; this covers the branches where it
    # returns without probing (no shell, non-POSIX host) so those do not re-hop
    # to the executor on every keystroke.
    sess.cwd_probe = (now, cwd)
    return cwd


def _session_title(sess: "_TerminalSession") -> str | None:
    """Best-effort "what is this terminal doing" label: the foreground command
    name while one runs, else the shell's cwd basename. Returns None when it
    can't tell (client keeps its current title, so a host that can resolve
    neither source simply stays at the tab's cwd default).

    The cwd goes through :func:`_session_cwd` rather than :func:`_proc_cwd` so
    that deriving this label shares one probe with the poller's cwd frame."""
    if not platform_compat.IS_POSIX or sess.master_fd < 0 or sess.proc is None:  # wokeignore:rule=master
        return None
    try:
        fg = os.tcgetpgrp(sess.master_fd)  # wokeignore:rule=master
    except OSError:
        return None
    # setsid() makes the shell its own process-group leader (pgid == pid); a
    # foreground pgid different from that means a command is running.
    if fg > 0 and fg != sess.proc.pid:
        name = _proc_comm(fg)
        if name:
            return name
    cwd = _session_cwd(sess)
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return None


def _is_bash_shell(shell: str) -> bool:
    """Whether *shell* supports the injected post-profile readiness marker."""
    name = shell.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in {"bash", "bash.exe"}


# The shells a code fence can name. FIXED here, never derived from anything a
# client, a fence or an agent supplies: the client selects among the entries
# this list produced, so no caller-supplied string is ever resolved or run.
_FENCE_SHELL_NAMES = ("bash", "sh", "zsh", "fish")


def _agent_can_rewrite(path: str, uid: int) -> bool:
    """Whether *path* or any ancestor could be rewritten by uid.

    Current mode bits are not the question: the OWNER of a directory can chmod it
    writable whenever it likes, so a user-owned ``0555`` directory is mutable in
    one syscall. Ownership is the durable property, and it has to hold for every
    ancestor too -- being able to rename a parent is enough to substitute
    everything under it.

    A world- or group-writable directory counts as rewritable UNLESS it carries
    the sticky bit, which is what stops a non-owner removing or renaming someone
    else's entry (``/tmp`` is the ordinary case).

    Two tests are needed and they catch different things. Ownership is the durable
    property: an owner can chmod at will, so current permission proves nothing
    about it. Mode bits are the cheap one. Note what this does NOT see: a POSIX
    ACL grant on an ancestor is invisible to ``st_mode``, and probing it with
    ``os.access`` here would read the REAL uid rather than this check's subject.
    Such an ACL can only be placed by root or by the directory's owner, and either
    of those can equally re-point the shell ``_resolve_shell`` itself resolves, so
    the exposure it would add over the base is nil. The candidate FILE is probed
    with ``os.access`` in the caller, where the subject is unambiguous.

    Fails closed: a path that cannot be stat'ed is treated as rewritable.
    """
    current = os.path.abspath(path)
    while True:
        try:
            st = os.lstat(current)
        except OSError:
            return True
        if st.st_uid == uid:
            return True
        loose = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if loose and not st.st_mode & stat.S_ISVTX:
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _resolve_fence_shells(launched: str) -> dict[str, str]:
    """Fence-nameable shells that sit ALONGSIDE the shell *launched* came from.

    Reported to the client so a snippet handed to a different shell names an
    absolute path rather than a bare name -- the same reasoning as
    ``_resolve_shell``'s pinning: the terminal runs with the chat's project
    directory as its cwd, so a bare name would be resolved there, and a relative
    ``PATH`` entry would let a project-planted executable win.

    Discovery adds NO new trusted location and consults no ``PATH`` at all. Each
    name is probed directly inside the directory the session's own shell
    canonically came from, so the set reported here is exactly as trustworthy as
    the shell ``_resolve_shell`` already chose. A planted binary therefore cannot
    become a reported shell unless the attacker already controls that directory,
    in which case the spawn itself is compromised first and a fence tag adds
    nothing. Probing the directory rather than resolving and filtering also means
    an unrelated ``PATH`` entry earlier in the search order (a venv, an asdf shim)
    cannot shadow a co-located shell out of the map.

    COVERAGE, deliberately narrow: a shell is offered only when it is co-located
    with the session's own shell AND neither it nor any ancestor of that directory
    is OWNED by the gateway's user (an owner can chmod at will, so mode bits alone
    prove nothing). Nothing this process could rewrite is ever offered, which is
    what closes the swap-after-discovery window: a path is reported now and invoked
    later, when the user confirms. That leaves the ordinary distro layout
    (root-owned ``/usr/bin``) and excludes a user-owned prefix -- Homebrew's, a
    workspace, a project-local ``bin`` -- where the snippet then behaves exactly as
    it does today. Under a root gateway every path is owned by the caller, so
    nothing is offered at all; that is the safe direction.

    Blocking note: a bounded handful of stats, run in the same off-loop hop as
    ``_resolve_shell`` rather than inline.
    """
    if not launched:
        return {}
    trusted_dir = os.path.dirname(os.path.realpath(launched))
    if not trusted_dir:
        return {}
    if platform_compat.IS_WINDOWS:
        # POSIX-only by construction: there is no bash/sh/zsh/fish host here to
        # hand a snippet to, and the ownership test below has no Windows
        # equivalent -- report nothing rather than approximate it.
        return {}
    uid = os.geteuid()
    if _agent_can_rewrite(trusted_dir, uid):
        return {}
    found: dict[str, str] = {}
    for name in _FENCE_SHELL_NAMES:
        candidate = os.path.join(trusted_dir, name)
        if os.path.islink(candidate):
            continue  # the link target is outside what was vetted above
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        if st.st_uid == uid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            continue  # rewritable in place
        if os.access(candidate, os.W_OK):
            continue  # rewritable in place via an ACL the mode bits do not show
        found[name] = candidate
    return found


def _resolve_shell_with_fence_shells(cfg: dict) -> tuple[str, str | None, dict[str, str]]:
    """``_resolve_shell`` plus the fence-shell map, in one off-loop hop."""
    shell, rejected = _resolve_shell(cfg)
    return shell, rejected, _resolve_fence_shells(shell)


# Names the readiness hook reads. When the hook runs, both are consumed and unset
# at the first prompt, so nothing the user runs afterwards sees them. A profile
# that ASSIGNS PROMPT_COMMAND prevents the hook from running at all, and both
# then stay exported for that session -- inert, since the only thing that reads
# them is the hook that was replaced.
_READY_TOKEN_VAR = "KIROCREW_TERMINAL_READY_TOKEN"
_READY_HOOK_VAR = "KIROCREW_TERMINAL_READY_HOOK"
# Carries an inherited PROMPT_COMMAND across the hook's lifetime so the
# withdrawal can restore it instead of unsetting the variable. Absent when the
# gateway's own environment exported no PROMPT_COMMAND, which is the norm.
_READY_PREV_VAR = "KIROCREW_TERMINAL_READY_PREV"


def _pty_child_env(extra: dict[str, str]) -> dict[str, str]:
    """Build the interactive shell's environment: the gateway environment plus
    *extra*, minus Kiro Crew's own Python startup variables.

    ``PYTHONPATH``/``PYTHONHOME``/``PYTHONPYCACHEPREFIX`` are searched BEFORE a
    venv's own site-packages, so leaking the gateway's copies makes a user's
    Python 3.13 venv import Kiro Crew's 3.12 site-packages and its C extensions
    fail to load. The agent surface strips them via
    ``sandbox.scrub_agent_subprocess_env``; this brings the terminal into line.

    Only the Python prefixes are dropped. Unlike the agent spawn, this is the
    user's own unsandboxed shell (see the spawn comment below), so
    ``SSH_AUTH_SOCK``, the AWS vars and the rest of the credential-bearing
    environment must survive or git-over-SSH and the AWS CLI break in it.

    One variable is ADDED: ``BASH_SILENCE_DEPRECATION_WARNING=1``. macOS ships
    Bash 3.2, which prints a three-line "the default interactive shell is now
    zsh" notice on every interactive start. In this panel that banner is pure
    noise: it lands at the top of the transcript of a terminal the user opened
    inside Kiro Crew, it says nothing about Kiro Crew, and it repeats on every
    session. It also precedes the readiness marker, so it is the first thing any
    consumer of the stream has to skip. A user who wants the notice keeps it by
    exporting the variable themselves with an empty value, which this respects:
    an existing value of any kind wins, so this only fills a gap. The variable
    means nothing to other shells and to Bash on other platforms.
    """
    env = {**os.environ, **extra}
    env.setdefault("BASH_SILENCE_DEPRECATION_WARNING", "1")
    for key in list(env):
        if any(key.startswith(prefix) for prefix in _PYTHON_ENV_PREFIXES):
            del env[key]
    return env


def _bash_ready_env(token: str) -> dict[str, str]:
    """Environment that makes a real login Bash report readiness at its prompt.

    Bash reads an ``--init-file`` only when it is *not* a login shell, so the
    marker cannot ride an injected rc file without giving up ``-l`` — and giving
    up ``-l`` breaks the profile chain: ``shopt -q login_shell`` is then false, so
    every profile stanza guarded on login-ness silently no-ops and the user's
    environment never loads. Sourcing the same files from an rc file cannot
    substitute, because that option is read-only and stays off.

    A login shell does honour a ``PROMPT_COMMAND`` inherited from its
    environment, and runs it after the profile chain returns and before the first
    prompt — exactly where the readiness marker has to fire.

    The snippet is single-shot and self-removing: it emits only while the token
    variable is still set, unsets that token so neither a later prompt nor a
    child shell repeats the sequence, and withdraws itself from
    ``PROMPT_COMMAND`` only when that variable is still exactly the scalar that
    was exported. Both halves of that last test matter: a profile that APPENDED
    (``PROMPT_COMMAND="$PROMPT_COMMAND; history -a"``) no longer matches, and one
    that appended as an ARRAY (``PROMPT_COMMAND+=("history -a")``, Bash 5.1+)
    leaves the exported text as element zero — which the scalar expansion alone
    would match, so unsetting there would take the user's own element with it.
    ``${PROMPT_COMMAND[1]+x}`` distinguishes the two without Bash 5.1 syntax the
    ``/bin/bash`` on macOS (3.2) cannot parse.

    A profile that ASSIGNS ``PROMPT_COMMAND`` outright drops the hook, and with
    it the marker: that session's barrier never opens, and the client's own
    bounded timeout reports the failure without writing. That is the same
    fail-closed outcome the barrier already has for a profile that never returns,
    and it is deliberate — releasing on inferred progress instead risks handing a
    queued command to a profile that is still reading input, which corrupts it.

    One case is NOT fail-closed and is worth naming: a profile that installs its
    own hook only when the variable looks unset — ``[ -z "$PROMPT_COMMAND" ] &&
    PROMPT_COMMAND=…``, which is how the RHEL-family ``/etc/bashrc`` installs its
    terminal-title updater — sees the exported hook, skips its own install, and
    then this snippet withdraws itself, so that session ends with no prompt
    command at all. It cannot be fixed by choosing a better moment or a cleverer
    guard: every carrier a login shell inherits is visible to the profile chain,
    and the carriers that are invisible to it (``BASH_ENV``, non-interactive only;
    ``ENV``, POSIX mode only; ``INPUTRC``, cannot run commands) do not run at the
    post-profile point a readiness marker needs.
    An operator who EXPORTED ``PROMPT_COMMAND`` into the gateway's own
    environment keeps it: the exported value is the readiness hook followed by
    the inherited command, and the withdrawal restores the inherited command
    rather than unsetting the variable. Replacing it outright would be data loss,
    not just a lost nicety — ``PROMPT_COMMAND='history -a'`` is the standard way
    to make concurrent shells append to ``HISTFILE``, and without it a shell
    exiting OVERWRITES that file with its own in-memory list, dropping the
    commands every sibling shell had appended.
    """
    hook = (
        f'if [[ -n "${{{_READY_TOKEN_VAR}-}}" ]]; then '
        f"builtin printf '\\033]697;KiroCrewReady;%s\\007' "
        f'"${{{_READY_TOKEN_VAR}}}"; '
        f"builtin unset {_READY_TOKEN_VAR}; "
        f'if [[ "${{PROMPT_COMMAND-}}" == "${{{_READY_HOOK_VAR}-}}" '
        f'&& -z "${{PROMPT_COMMAND[1]+x}}" ]]; then '
        f'if [[ -n "${{{_READY_PREV_VAR}+x}}" ]]; then '
        f'PROMPT_COMMAND="${{{_READY_PREV_VAR}}}"; '
        f"else builtin unset PROMPT_COMMAND; fi; fi; "
        f"builtin unset {_READY_HOOK_VAR} {_READY_PREV_VAR}; fi"
    )
    inherited = os.environ.get("PROMPT_COMMAND") or ""
    # Blankness is tested on a stripped copy, but the value CARRIED is the raw
    # one: trailing whitespace can be escaped (`printf x\ `), and stripping that
    # turns the escape into a line continuation, which changes what the command
    # does rather than merely tidying it.
    has_inherited = bool(inherited.strip())
    # The exported value is executed as shell code, so the inherited command is
    # carried verbatim rather than quoted into an argument. It runs AFTER the
    # marker, which is the order the readiness signal needs: the marker must be
    # the first thing this prompt writes.
    exported = f"{hook}; {inherited}" if has_inherited else hook
    # _READY_HOOK_VAR mirrors the exported value (the WHOLE value, hook plus any
    # inherited tail) so the hook can tell "still mine" from "a profile has taken
    # this over" without pattern-matching its own text. Whenever the hook RUNS it
    # unsets the mirror on both branches, withdrawing or not; the third path is a
    # profile that replaced PROMPT_COMMAND, where the hook never runs and these
    # names stay exported for that session (see the docstring).
    env = {
        _READY_TOKEN_VAR: token,
        _READY_HOOK_VAR: exported,
        "PROMPT_COMMAND": exported,
    }
    if has_inherited:
        env[_READY_PREV_VAR] = inherited
    return env


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


def _sess_alive(sess: "_TerminalSession") -> bool:
    """Whether the session's child process is still running (either backend)."""
    if sess.winpty is not None:
        try:
            return bool(sess.winpty.isalive())
        except Exception:
            return False
    if sess.proc is not None:
        return sess.proc.returncode is None
    return False


def _sess_pid(sess: "_TerminalSession") -> int | None:
    """PID of the session's child process (either backend), or None."""
    if sess.winpty is not None:
        return sess.winpty.pid
    return sess.proc.pid if sess.proc is not None else None


async def _kill_session(sess: _TerminalSession) -> None:
    """Kill PTY process and close FDs for a session."""
    # Windows ConPTY backend: terminate the pseudo-console child and close its
    # handles. Offloaded to the subprocess pool so a wedged TerminateProcess /
    # ClosePseudoConsole can never stall the event loop (same rationale as the
    # POSIX os.close offload below).
    if sess.winpty is not None:
        wp = sess.winpty
        sess.winpty = None
        if sess.reader_task is not None:
            sess.reader_task.cancel()
            try:
                await sess.reader_task
            except (asyncio.CancelledError, Exception):
                pass
        loop = asyncio.get_running_loop()
        pid = getattr(wp, "pid", 0)
        # Reap the whole console tree (the shell + anything it spawned) via
        # taskkill /T so a background child can't outlive the closed terminal.
        if pid:
            try:
                await platform_compat.kill_process_tree_async(
                    pid, platform_compat.SIGTERM
                )
            except (OSError, ProcessLookupError):
                pass
        # Free the pseudo-console + handles (TerminateProcess is a backstop).
        try:
            await loop.run_in_executor(
                subprocess_executor(), wp.terminate,  # type: ignore[attr-defined]
            )
        except (OSError, RuntimeError):
            pass
        return
    # The child goes FIRST, then the PTY's controller descriptor, and the order
    # is the fix for a real deadlock. Closing the controller end while the reader
    # task is blocked in os.read() on it behaves differently per kernel: Linux
    # hangs up the terminal end and the read returns EIO, which is what let the
    # close come first; macOS (and the BSDs) make close() wait for that
    # outstanding read, so with an interactive bash still holding the terminal
    # end the close never returned, and four PTY tests timed out at 120 s on
    # every macOS run, each parking a pool thread forever. Ending the session's
    # process tree first releases the terminal end on both, so the read returns
    # EOF, the close completes. SIGHUP is what a vanished terminal
    # delivers and the one signal an interactive shell does not ignore (it
    # ignores SIGTERM, which alone would cost the 5 s escalation wait); SIGTERM
    # follows for everything else, SIGKILL after the wait as before.
    if sess.proc is not None and sess.proc.returncode is None:
        # Route through platform_compat.kill_process_tree so the whole terminal
        # handler stays platform-portable (killpg on POSIX, taskkill /T on
        # Windows). This PTY teardown is POSIX-only in practice — api_terminal_
        # ws returns an error on Windows before any session is created — but
        # keeping a single shim call site avoids a raw-os.killpg vs shim
        # inconsistency across the module, and the tests all patch the shim.
        for sig in (platform_compat.SIGHUP, platform_compat.SIGTERM):
            try:
                # Async variants offload Windows taskkill to subprocess_executor
                # so this PTY teardown path never blocks the event loop on
                # taskkill.exe. POSIX os.killpg stays inline.
                await platform_compat.kill_process_tree_async(sess.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                # PermissionError (EPERM): the child made the PTY its controlling
                # terminal (TIOCSCTTY) and leads a session/group we can't signal.
                # Fall through to wait()/kill the proc directly.
                pass
        try:
            await asyncio.wait_for(sess.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                await platform_compat.kill_process_tree_async(
                    sess.proc.pid, platform_compat.SIGKILL
                )
            except (ProcessLookupError, PermissionError):
                pass
            try:
                sess.proc.kill()
            except ProcessLookupError:
                pass
            # BOUNDED, because SIGKILL is not the end of the story on a PTY. A
            # shell blocked writing into a controller buffer nobody drains sits
            # in a tty write inside the kernel, and the pending SIGKILL only
            # tears it down once it leaves that sleep, which needs a READ on the
            # controller fd. The read that would free it is this session's
            # reader task, so a session whose reader has already ended (an
            # OSError on the fd, an EOF, a cancellation) hands an unbounded
            # wait() a child that cannot exit until the close further down runs.
            # That is a deadlock inside a request handler: the handler is lost
            # for the life of the process, and on the shutdown path it wedges
            # the whole teardown. Give up on the reap instead, name the pid, and
            # let the rest of teardown run: closing the controller fd is itself
            # what releases the wedged write, and the child watcher reaps the
            # exit afterwards.
            try:
                await asyncio.wait_for(
                    sess.proc.wait(), timeout=platform_compat.REAP_TIMEOUT_SECS
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "terminal: session %s pid %s did not exit %ss after SIGKILL; "
                    "continuing teardown without reaping it",
                    sess.session_id, sess.proc.pid, platform_compat.REAP_TIMEOUT_SECS,
                )
    # os.close() on a PTY controller fd can still BLOCK in the kernel when the far
    # end is wedged (uninterruptible sleep, or a child this process may not
    # signal). Run it on the dedicated subprocess pool, never the event loop, so a
    # wedged close then costs at most one pool thread instead of freezing the
    # whole gateway, and shares no workers with the orphan-reaping maintenance
    # sweep.
    if sess.master_fd >= 0:  # wokeignore:rule=master
        fd = sess.master_fd  # wokeignore:rule=master
        # Clear the handle BEFORE the await: if this coroutine is cancelled while
        # suspended on the executor (e.g. aiohttp cancels the request handler on
        # client disconnect), the fd must not be left referenced on the session.
        sess.master_fd = -1  # wokeignore:rule=master
        try:
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), os.close, fd,
            )
        except (OSError, RuntimeError):
            # OSError: close failed. RuntimeError: the subprocess pool was
            # already torn down (shutdown races interpreter exit): submit
            # raises rather than returning a future; the fd is reaped on exit.
            pass
    if sess.reader_task is not None:
        sess.reader_task.cancel()
        try:
            await sess.reader_task
        except (asyncio.CancelledError, Exception):
            pass


async def _child_exit_status(sess: _TerminalSession) -> int | None:
    """Exit status of the session's child, or ``None`` when it is unavailable.

    ``None`` means the status did not resolve within ``_CHILD_REAP_TIMEOUT_S``.
    Both backends report a real code; ConPTY's is available once the child has
    been reaped.
    """
    wp = sess.winpty
    if wp is not None:
        # Poll the non-blocking ``isalive()`` (which also reaps the child)
        # rather than pywinpty's ``wait()``, whose executor thread cannot be
        # cancelled at the deadline and would leak per reap.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CHILD_REAP_TIMEOUT_S
        while wp.isalive():
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(_CONPTY_STATUS_POLL_S)
        return wp.exitstatus()
    proc = sess.proc
    if proc is None:
        return None
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=_CHILD_REAP_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None
    return proc.returncode


async def _handle_pty_reader_end(
    registry: dict[str, _TerminalSession | None],
    sess: _TerminalSession,
    *,
    bus: NotificationBus | None = None,
    exited: dict[str, _ExitedRecord] | None = None,
) -> None:
    """Announce and reap a session whose shell exited on its own.

    The reader also ends on a deliberate teardown (``DELETE``, the orphan
    sweep), which raises the same ``OSError``. Those paths pop the registry
    entry before closing the fd, so the identity check below tells the two
    apart. An abnormal exit is also published on ``system.terminal``.
    """
    if registry.get(sess.session_id) is not sess:
        return  # deliberate teardown, already accounted for by its own path
    status = await _child_exit_status(sess)
    if registry.get(sess.session_id) is not sess:
        return  # teardown or replacement took the slot while status resolved
    if status is None and _sess_alive(sess):
        # The child is still running (an fd closed under us): not an exit.
        return
    close = _close_on_exit()
    if not sess.exit_announced:
        # Published before any transport await so owner churn cannot lose it.
        sess.exit_announced = True
        if close and bus is not None and status != 0:
            _notify_abnormal_exit(bus, sess, status)

    if not await _tell_owner_exited_and_pop(
        registry, sess, status=status, close=close, exited=exited
    ):
        return
    # We run inside reader_task; _kill_session cancels it, which would abort the
    # teardown part-way and leak the controller fd.
    sess.reader_task = None
    await _kill_session(sess)


async def _tell_owner_exited_and_pop(
    registry: dict[str, _TerminalSession | None],
    sess: _TerminalSession,
    *,
    status: int | None,
    close: bool,
    exited: dict[str, _ExitedRecord] | None,
) -> bool:
    """Send the attached owner the exit frame and the 4001 close, then pop.

    ``close`` tells the client what the tab does: close, or stay showing the
    exit. Decided server-side so every window of one dashboard agrees. Shared
    by the reader's end and the orphan sweep's death arm, so a window attached
    to a shell the sweep found dead is told the same way. Returns False when
    another path disposed of the entry meanwhile, in which case that path
    owns the teardown and the caller must not kill.
    """
    payload = {"type": "exit", "status": status, "close": close}
    # Serialized with reconnect publication: a reconnect that wins the lock
    # becomes the owner closed below; one queued behind it fails ``can_publish``.
    async with sess.replace_lock:
        if registry.get(sess.session_id) is not sess:
            return False
        ws = sess.ws
        if ws is not None:
            await _send_owner_control_frame(sess, ws, payload)
            if registry.get(sess.session_id) is not sess:
                return False
            # Detach may clear sess.ws without replace_lock; a real takeover
            # cannot publish while this lock is held.
            if sess.ws is ws:
                await _close_terminal_ws_bounded(
                    ws, close_code=_TERMINAL_WS_CLOSE_SHELL_EXITED
                )
                if registry.get(sess.session_id) is not sess:
                    return False
        if exited is not None:
            # Before the pop, under the lock: a queued reconnect can never
            # see the entry gone and the record absent. The ring is copied
            # here too, because the kill that follows this pop discards it and
            # a second window holding the same id may still dial.
            _record_exited(
                exited,
                sess.session_id,
                status,
                close=close,
                scrollback=bytes(sess.scrollback),
            )
        registry.pop(sess.session_id, None)
        return True


def _notify_abnormal_exit(bus: NotificationBus, sess: _TerminalSession, status: int | None) -> None:
    """Push one bell-feed note for a non-zero or unknown exit status.

    One ``group_key`` for all sessions, so a shell dying in a loop stacks.
    Best-effort: a refusing bus is logged and the reap goes on.

    Says "shell" throughout, the word the setting and the pane's exit line
    both use, so a reader can tell the three surfaces describe one event.
    """
    subject = f"The shell {sess.shell}" if sess.shell else "The shell"
    if status is None:
        title = "Terminal shell exited"
        body = f"{subject} exited; its exit code is unknown."
    else:
        title = f"Terminal shell exited with code {status}"
        body = f"{subject} exited with code {status}."
    try:
        bus.push(
            NotificationPayload(
                source="system",
                channel="system.terminal",
                title=title,
                body=body,
                group_key="terminal-exit",
                meta={"session_id": sess.session_id, "shell": sess.shell, "status": status},
            )
        )
    except Exception:  # noqa: BLE001 -- the reap must not depend on the feed
        logger.warning(
            "terminal %s: could not publish the exit notification", sess.session_id, exc_info=True
        )


async def api_terminal_ws(request: web.Request) -> web.WebSocketResponse | web.Response:
    """WebSocket PTY for the built-in CLI panel.

    Protocol:
      - Binary frames: raw terminal I/O (both directions)
      - Text frames (JSON): control messages
        - Client→Server: {"type":"resize","cols":N,"rows":N}
        - Client→Server: {"type":"ping"}
        - Server→Client: {"type":"pong"}
        - Server→Client: {"type":"exit","status":N|null} — the shell exited on
          its own; the session is reaped as this frame is sent, so the client
          closes the tab instead of redialing. ``status`` is null when the child's
          status could not be resolved within the reap bound, on either backend.
    """
    # A WebSocket upgrade is a GET, and `csrf_middleware` validates the origin
    # only for unsafe methods, so the handshake would otherwise arrive
    # unchecked. The session cookie is attached automatically and SameSite=Lax
    # does not distinguish ports, so any other loopback origin could open a PTY
    # under the operator's own session. The other two WebSocket routes check in
    # their own handlers for the same reason (`ws.py`, `stt_stream.py`).
    if not check_origin(request, require=True):
        _sel().log_api_access(
            caller=request.get("user") or "unknown",
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"origin_not_allowed={request.headers.get('Origin', '')[:80]!r}",
        )
        # This record is the specific one; claim the request so the deny-audit
        # boundary does not add a second, generic entry for the same refusal.
        mark_audit_claimed(request)
        raise web.HTTPForbidden(text="WebSocket origin not allowed")
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.ws.open")
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")

    session_id = request.match_info.get("session_id", "")
    if not session_id or len(session_id) > _MAX_SESSION_ID_LEN:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"invalid_session_id={session_id!r}",
        )
        return web.Response(status=400, text="Invalid session_id")

    registry = _get_registry(request)
    # Resolved here because the reader that reports the exit outlives the request.
    notification_bus: NotificationBus | None = getattr(
        request.app["state"], "notification_bus", None
    )
    exited_records = _get_exited(request)
    cfg = _get_config(request)
    max_sessions = cfg.get("max_sessions", _MAX_SESSIONS)
    # Resolve the shell HERE, before the reservation region below: the
    # resolution is a PATH scan (shutil.which stats every entry) that must run
    # off-loop, and the spawn branches sit between the placeholder reservation
    # and the session registration, where an added await would suspend the
    # handler with the registry still holding the None placeholder — a window
    # every concurrent reader of the registry would then observe. One hop per
    # WS open, now also carrying the fence-shell map the ready frame reports;
    # a reconnect keeps the values its original open resolved.
    shell, rejected_shell, fence_shells = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _resolve_shell_with_fence_shells, cfg,
    )

    # Check if reconnecting to existing session. A None VALUE under an
    # existing key is another handler's reservation placeholder (set below,
    # held across its awaits): treat it as "session already being opened" and
    # refuse, instead of reading it as absent — two tabs racing the same
    # unregistered session id would otherwise both pass the reservation check
    # and spawn two PTYs, leaking one. This guards every await in this
    # handler (the off-loop shell resolution above and ws.prepare below).
    if session_id in registry and registry[session_id] is None:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"session={session_id},reservation_in_flight=1",
        )
        return web.Response(
            status=409, text="Terminal session is already being opened"
        )
    existing = registry.get(session_id)
    if existing and not _sess_alive(existing):
        # Dead but not yet popped by its reader (seconds, with a backpressured
        # owner socket). Answer it as an exit, never with a fresh shell, and
        # tell the window still attached to it through the same
        # _tell_owner_exited_and_pop the reader's end and the sweep use: it
        # sends the frame, closes 4001, records and pops under replace_lock.
        # Recording and telling before the kill is what keeps the OTHER window
        # from being left with a live-looking tab over a dead shell, since the
        # kill closes fds and never touches sess.ws.
        status = await _dead_session_status(existing)
        close = _close_on_exit()
        # Note only: the tell below owns the record, so this must not write one.
        _announce_exit(existing, status, bus=notification_bus, exited=None, close=close)
        if await _tell_owner_exited_and_pop(
            registry, existing, status=status, close=close, exited=exited_records
        ):
            # The entry is popped, so claim the slot synchronously -- no await
            # in between -- to hold concurrent opens at 409 across the kill.
            registry[session_id] = None
            try:
                await _kill_session(existing)
            finally:
                registry.pop(session_id, None)
        # A False return means another path (the reader's end, the sweep) got
        # there first; that path owns the teardown, so this one must not kill.
        existing = None

    # A dial to an id whose shell already exited. Answered before the capacity
    # check, so a full registry cannot stop a client learning its shell is gone.
    if not existing:
        record = _recorded_exit(exited_records, session_id)
        if record is not None:
            ws = web.WebSocketResponse(heartbeat=30, timeout=300)
            await ws.prepare(request)
            # Output first, so the pane reads in the order it happened: the
            # shell's own tail, then the exit line the client writes from the
            # frame. The session's ring died with the reap, so this copy is the
            # only thing standing between a detached window and a blank pane.
            if record.scrollback:
                try:
                    await asyncio.wait_for(
                        ws.send_bytes(record.scrollback),
                        timeout=_TAKEOVER_REPLAY_SEND_TIMEOUT_S,
                    )
                except (ConnectionResetError, RuntimeError, OSError, asyncio.TimeoutError):
                    pass  # the frame and close below still tell the truth
            # Frame next: it carries the status, which a bare 4001 does not, and
            # the close decision taken AT exit time rather than a fresh read, so
            # a setting toggled inside the record's window cannot make this
            # window disagree with the one that watched the shell die.
            try:
                await asyncio.wait_for(
                    ws.send_str(
                        json.dumps(
                            {"type": "exit", "status": record.status, "close": record.close}
                        )
                    ),
                    timeout=_TERMINAL_WS_CLEANUP_TIMEOUT_S,
                )
            except (ConnectionResetError, RuntimeError, OSError, asyncio.TimeoutError):
                pass  # the close below is what the client acts on
            await _close_terminal_ws_bounded(
                ws, close_code=_TERMINAL_WS_CLOSE_SHELL_EXITED
            )
            _sel().log_api_access(
                caller=caller,
                operation="terminal.ws.reconnect",
                outcome="success",
                source="dashboard",
                resources=(
                    f"session={session_id},shell_exited=1,status={record.status},"
                    f"replayed={len(record.scrollback)}"
                ),
            )
            return ws

    # Reserve slot synchronously before any await to prevent race condition.
    if not existing and len(registry) >= max_sessions:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"max_sessions={max_sessions}",
        )
        return web.Response(status=429, text=f"Max {max_sessions} terminal sessions")

    # Reserve a placeholder so concurrent requests see the slot as taken
    placeholder = not existing
    if placeholder:
        registry[session_id] = None

    ws = web.WebSocketResponse(heartbeat=30, timeout=300)
    try:
        await ws.prepare(request)
    except Exception:
        if placeholder:
            registry.pop(session_id, None)  # type: ignore[arg-type]
        raise

    if existing:
        # Validated inside replace_lock: the exit path may remove ``existing``
        # while this handler waits on it.
        replaced = await _replace_terminal_ws(
            existing,
            ws,
            can_publish=lambda: (
                registry.get(session_id) is existing and _sess_alive(existing)
            ),
        )
        if not replaced:
            _sel().log_api_access(
                caller=caller,
                operation="terminal.ws.reconnect",
                outcome="error",
                source="dashboard",
                resources=f"session={session_id},takeover_failed=1",
            )
            current = registry.get(session_id)
            shell_exited = (
                not _sess_alive(existing)
                and (current is existing or current is None)
            )
            if shell_exited:
                # The shell exited while this reconnect waited; a generic close
                # would redial and spawn a fresh shell over it.
                await _close_terminal_ws_bounded(
                    ws, close_code=_TERMINAL_WS_CLOSE_SHELL_EXITED
                )
            else:
                await _close_terminal_ws_bounded(
                    ws,
                    error_message="Terminal reconnect failed",
                )
            return ws
        sess = existing
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.reconnect",
            outcome="ok",
            source="dashboard",
            resources=f"session={session_id},pid={_sess_pid(sess)}",
        )
    elif platform_compat.IS_WINDOWS:
        # Windows: spawn a ConPTY-backed shell (PowerShell by default). There is
        # no POSIX pty/fork; kiro_crew.conpty drives the Win32 pseudo-console via
        # ctypes (stdlib, no extra dependency).
        from kiro_crew.conpty import WindowsPty

        if rejected_shell:
            logger.warning(
                "terminal: configured shell %r not executable; falling back to %r",
                rejected_shell, shell,
            )
        cwd = _resolve_cwd(cfg, request.query.get("cwd"))
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        env = _pty_child_env({"KIROCREW_TERMINAL": "1"})
        argv = [shell, "-NoLogo"] if "powershell" in shell.lower() else [shell]
        try:
            wp = WindowsPty(argv, cwd=cwd, env=env, cols=80, rows=24)
        except Exception as exc:
            registry.pop(session_id, None)  # type: ignore[arg-type]
            _sel().log_api_access(
                caller=caller, operation="terminal.ws.open",
                outcome="error", source="dashboard",
                resources=f"conpty_spawn_failed={exc}",
            )
            if not ws.closed:
                await ws.send_str(json.dumps(
                    {"type": "error", "message": f"Failed to start terminal: {exc}"}
                ))
                await ws.close()
            return ws
        sess = _TerminalSession(
            session_id=session_id, master_fd=-1, proc=None, winpty=wp, ws=ws, shell=shell,  # wokeignore:rule=master
            fence_shells=fence_shells,
        )
        registry[session_id] = sess
        _sel().log_api_access(
            caller=caller, operation="terminal.ws.open",
            outcome="ok", source="dashboard",
            resources=f"session={session_id},pid={wp.pid},shell={shell}",
        )
    else:
        if rejected_shell:
            logger.warning(
                "terminal: configured shell %r not executable; falling back to %r",
                rejected_shell, shell,
            )
        # Spawn new PTY. Bash is a real login shell (`-l`) so the user's own
        # profile chain runs with `shopt -q login_shell` true, and it inherits a
        # PROMPT_COMMAND that emits a definitive readiness marker once the
        # profiles return. Foreground process ownership alone is insufficient: a
        # profile's builtin `read` runs in the shell process and would consume an
        # early command batch.
        try:
            master_fd, worker_fd = _pty.openpty()  # wokeignore:rule=master
        except Exception as exc:
            registry.pop(session_id, None)  # type: ignore[arg-type]
            _sel().log_api_access(
                caller=caller,
                operation="terminal.ws.open",
                outcome="error",
                source="dashboard",
                resources=f"pty_open_failed={exc}",
            )
            if not ws.closed:
                # Bounded, and generic: the openpty() error stays in the audit.
                await _close_terminal_ws_bounded(
                    ws, error_message="Could not open a terminal"
                )
            return ws
        ready_marker: bytes | None = None
        try:
            fcntl.ioctl(
                worker_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 24, 80, 0, 0),
            )
            cwd = _resolve_cwd(cfg, request.query.get("cwd"))
            env = _pty_child_env({
                "TERM": "xterm-256color",
                "KIROCREW_TERMINAL": "1",
                # Export the shell actually being spawned (already resolved to
                # an absolute path). Without this, a configured shell that
                # differs from the login shell leaves the inherited $SHELL
                # pointing at the login shell, so programs that consult it
                # (vim's :sh, tmux default-shell) open the wrong one. POSIX
                # branch only: PowerShell does not consult $SHELL.
                "SHELL": shell,
            })
            # Security: intentionally unsandboxed — this is the user's own
            # interactive terminal (like SSH), not agent-executed code.
            # Auth is enforced at WS handshake via token_auth_middleware.
            # See CLI_PANEL_DESIGN.md §8 "Security Considerations".
            # The PTY has to become the child's CONTROLLING terminal or Ctrl+C
            # reaches nothing: the kernel needs a foreground process group to
            # deliver SIGINT to, and inheriting an already-open terminal
            # descriptor does not confer one. It has to be claimed, after
            # setsid(), by the session leader itself.
            #
            # The claim is made AFTER exec, by the post-exec shim, against fd 0
            # (the PTY, below). Asking for it with preexec_fn instead is what
            # makes CPython fork this whole multi-GB, ~120-thread gateway and run
            # Python in the clone before exec, and the ioctl is not what costs:
            # the page-table copy blocks the event loop for ~107ms per terminal
            # open at 3GB resident, and a clone that cannot reach exec blocks it
            # without bound, because the parent waits inside an un-awaitable
            # os.read on the loop thread that no timeout can reach. The shim runs
            # the same ioctl single-threaded in the exec'd child, where none of
            # that applies -- see the module docstring of _spawn_exec_shim.py.
            argv = [shell, "-l"]
            if _is_bash_shell(shell):
                token = uuid.uuid4().hex
                ready_marker = (
                    f"\x1b]697;KiroCrewReady;{token}\x07".encode("ascii")
                )
                env.update(_bash_ready_env(token))

            # RLIMIT_PROFILE_NONE: the user's own interactive shell carries no
            # rlimits and no OOM bias, and never did. The controlling terminal is
            # the only thing this spawn asks the shim for.
            ctty_shim = spawn_shim_argv(RLIMIT_PROFILE_NONE, ctty_fd=0)
            if not ctty_shim:
                # Deliberately NOT falling back to preexec_fn: reintroducing the
                # fork is the whole defect this spawn is avoiding, and a terminal
                # whose Ctrl+C is dead is a smaller harm than a gateway the
                # watchdog kills. Reachable only on a truncated install, where
                # the shim source could not be captured at import.
                logger.warning(
                    "terminal: post-exec shim unavailable; opening the shell with no "
                    "controlling terminal, so Ctrl+C will not reach it"
                )

            proc = await asyncio.create_subprocess_exec(
                *ctty_shim,
                *argv,
                stdin=worker_fd,
                stdout=worker_fd,
                stderr=worker_fd,
                start_new_session=True,
                cwd=cwd,
                env=env,
            )
        except Exception as exc:
            try:
                os.close(master_fd)
            except OSError:
                pass
            registry.pop(session_id, None)  # type: ignore[arg-type]
            # WS already prepared — send error over WS then close
            if not ws.closed:
                await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
                await ws.close()
            return ws
        finally:
            os.close(worker_fd)

        sess = _TerminalSession(
            session_id=session_id,
            master_fd=master_fd,
            proc=proc,
            ws=ws,
            ready_marker=ready_marker,
            shell=shell,
            fence_shells=fence_shells,
        )
        registry[session_id] = sess
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="ok",
            source="dashboard",
            resources=f"session={session_id},pid={proc.pid},shell={shell}",
        )
        if ready_marker is None:
            # The reliable injection above intentionally targets Bash, the
            # reported shell. Configured shells whose startup protocol we cannot
            # control fall back to transport-ready.
            sess.shell_ready = True
            try:
                async with sess.send_lock:
                    if sess.ws is ws and not ws.closed:
                        await ws.send_str(json.dumps({
                            "type": "ready",
                            "shell": sess.shell,
                            "fence_shells": sess.fence_shells,
                        }))
            except (ConnectionResetError, RuntimeError, OSError):
                pass

    # --- Read loop: PTY → WebSocket ---
    async def read_pty():
        reader_ended = False
        try:
            loop = asyncio.get_running_loop()
            if sess.winpty is not None:
                reader = lambda: sess.winpty.read(4096)  # noqa: E731
            else:
                reader = lambda: os.read(sess.master_fd, 4096)  # noqa: E731  # wokeignore:rule=master
            while True:
                data = await loop.run_in_executor(None, reader)
                if not data:
                    reader_ended = True
                    break
                await _record_and_forward_terminal_output(sess, data)
        except OSError:
            # EIO: the child exited, or a teardown closed the fd under us.
            # _handle_pty_reader_end tells those apart.
            reader_ended = True
        if reader_ended:
            await _handle_pty_reader_end(
                registry, sess, bus=notification_bus, exited=exited_records
            )

    if sess.reader_task is None or sess.reader_task.done():
        sess.reader_task = asyncio.ensure_future(read_pty())

    # --- Write loop: WebSocket → PTY ---
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                try:
                    accepted = await _write_terminal_input(sess, ws, msg.data)
                except OSError:
                    break
                if not accepted:
                    _sel().log_api_access(
                        caller=caller,
                        operation="terminal.ws.input",
                        outcome="denied",
                        source="dashboard",
                        resources=f"session={session_id},stale_owner=1",
                    )
                    # The displaced handler ends here: later stale frames are
                    # never read, so one denial audit is the ceiling per socket.
                    break
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    ctrl = json.loads(msg.data)
                except ValueError:
                    continue
                if ctrl.get("type") == "resize":
                    try:
                        cols = min(max(int(ctrl.get("cols", 80)), 1), 500)
                        rows = min(max(int(ctrl.get("rows", 24)), 1), 200)
                    except (ValueError, TypeError):
                        continue
                    if not await _resize_terminal(sess, ws, cols, rows):
                        _sel().log_api_access(
                            caller=caller,
                            operation="terminal.ws.resize",
                            outcome="denied",
                            source="dashboard",
                            resources=f"session={session_id},stale_owner=1",
                        )
                        break
                elif ctrl.get("type") == "ping":
                    await _send_owner_control_frame(sess, ws, {"type": "pong"})
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
    finally:
        # WS disconnected — mark for orphan reaper, but keep PTY alive.
        # Identity-guarded: a reconnect (e.g. the terminal panel popping out to
        # its own window) REPLACES sess.ws while this displaced handler is
        # still draining; unconditionally clearing it here would silence PTY
        # output to the freshly attached socket.
        _release_terminal_ws_identity(sess, ws)
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.disconnect",
            outcome="ok",
            source="dashboard",
            resources=f"session={session_id}",
        )

    return ws


async def api_terminal_create(request: web.Request) -> web.Response:
    """POST /api/terminal/sessions — create a new terminal session (returns session_id)."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.session.create",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(
        request, "terminal.session.create"
    )
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.create",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")

    registry = _get_registry(request)
    cfg = _get_config(request)
    max_sessions = cfg.get("max_sessions", _MAX_SESSIONS)

    if len(registry) >= max_sessions:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.create",
            outcome="denied",
            source="dashboard",
            resources=f"max_sessions={max_sessions}",
        )
        return web.json_response(
            {"error": f"Max {max_sessions} sessions", "code": "terminal_max_sessions"},
            status=429,
        )

    session_id = uuid.uuid4().hex[:12]
    # Off-loop for the same reason as the spawn sites: the PATH scan must not
    # stall the event loop at request rate.
    shell, rejected_shell = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _resolve_shell, cfg,
    )
    _sel().log_api_access(
        caller=caller,
        operation="terminal.session.create",
        outcome="ok",
        source="dashboard",
        resources=f"session={session_id}",
    )
    body: dict = {
        "session_id": session_id,
        "shell": shell,
    }
    # Surface a rejected configured shell at the API level rather than
    # silently substituting: an API caller can tell a typo'd setting from a
    # deliberate default. The dashboard panel spawns via the WS handler and
    # does not read these fields — its typo surface is the save-time Settings
    # validation (config PATCH), with the spawn-site log as the backstop.
    if rejected_shell:
        body["shell_fallback"] = True
        body["configured_shell"] = rejected_shell
    return web.json_response(body)


# Selection hand-off size cap. Generous for terminal selections (xterm buffers
# are bounded anyway) while preventing a multi-megabyte POST from tying up the
# redactors on the event loop's executor.
_REDACT_MAX_BYTES = 256 * 1024


async def api_terminal_redact(request: web.Request) -> web.Response:
    """POST /api/terminal/redact — scan a COMPLETE terminal selection before it
    is inserted into chat. This is the whole credential boundary for the web
    terminal: the live PTY stream is forwarded to the browser unscanned, and the
    scrollback ring buffer is replayed only there, so the selection hand-off is
    the one path by which terminal output reaches a model. It therefore runs
    unconditionally, and callers MUST fail closed: no chat insertion unless this
    returns 200 with redacted text."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.selection.redact",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(
        request, "terminal.selection.redact"
    )
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.selection.redact",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")
    try:
        body = await request.json()
        text = body["text"]
        if not isinstance(text, str):
            raise TypeError
    except Exception:
        return web.json_response(
            {"error": "expected JSON body {text: string}", "code": "terminal_invalid_body"},
            status=400,
        )
    if len(text.encode("utf-8", errors="replace")) > _REDACT_MAX_BYTES:
        return web.json_response(
            {"error": "selection too large", "code": "terminal_selection_too_large"},
            status=413,
        )
    # This is the ONLY credential scan on the path from PTY output to a model,
    # so it runs unconditionally and there is no configuration that skips it.
    # A contiguous selection is also the only input the redactors can be
    # accurate on: they are regex scans, and a secret split across two reads is
    # invisible to a per-chunk scan by construction.
    # Run off-loop: the redactors scale with input size.
    loop = asyncio.get_running_loop()

    def _scan(t: str) -> str:
        t, _ = redact_exfiltration_urls(t)
        t, _ = redact_credentials(t)
        return t

    try:
        redacted = await loop.run_in_executor(subprocess_executor(), _scan, text)
    except Exception:
        # Fail closed: the caller gets no text to insert.
        logger.exception("terminal: selection redaction failed")
        return web.json_response(
            {"error": "redaction failed", "code": "terminal_redaction_failed"},
            status=500,
        )
    return web.json_response({"text": redacted})


_COMPLETE_MAX_ENTRIES = 200
_COMPLETE_TOKEN_MAX = 4096
# Hard ceiling on how many directory entries one completion may EXAMINE. The
# retention cap alone does not bound the work: a directory with a million
# entries would still be walked end to end while holding a pool thread at
# keystroke rate. Stopping early is safe because the user narrows by typing.
_COMPLETE_MAX_SCAN = 20000

# C0 controls (0x00-0x1F), DEL (0x7F), C1 controls (0x80-0x9F) and lone surrogate
# code points (U+D800-U+DFFF). A filename may legally contain any of these; the
# client TYPES the accepted completion into the PTY, so a name holding CR/LF
# would submit an executed command line and an ESC would inject a terminal
# escape sequence. Surrogates are how Python's surrogateescape decoding
# represents bytes that are not valid UTF-8: JSON carries them through, but the
# browser's TextEncoder replaces each with U+FFFD, so the client would type a
# path that does not exist on disk. Filter all of them at the source so such
# names never reach a client at all.
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def _split_path_token(token: str) -> tuple[str, str]:
    """Split a shell path token into its (directory part, name prefix).

    ``"../Kiro"`` → ``("../", "Kiro")``; ``"src/"`` → ``("src/", "")``;
    ``"Kiro"`` → ``("", "Kiro")``."""
    idx = token.rfind("/")
    if idx < 0:
        return "", token
    return token[: idx + 1], token[idx + 1:]


def _resolve_completion_dir(cwd: str, dir_part: str) -> str:
    """Absolute directory that a token's directory part refers to.

    ``~`` is expanded (only as a leading segment, matching what the shell shows
    the user); a relative part resolves against the session's live cwd."""
    if not dir_part:
        return cwd
    expanded = os.path.expanduser(dir_part) if dir_part.startswith("~") else dir_part
    base = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
    return os.path.normpath(base)


def _vetted_completion_dir(directory: str) -> str | None:
    """Canonical form of *directory*, or ``None`` when it must not be enumerated.

    Delegates to ``hooks.validate_file_path`` — the named chokepoint the backend
    security rules require every file read to pass through — rather than
    reimplementing its ``realpath`` + ``is_sensitive_path`` pair. Canonicalizing
    before the denylist test is the load-bearing part: without it a benign-looking
    symlink (or a symlinked parent component) whose target lands inside the
    governance trust-root would pass a name-based check and then be enumerated
    through the link, leaking ``profiles/``, ``security_policy.json`` and
    credential-file names.

    The chokepoint stops at "which path is allowed"; it cannot bind the answer to
    the inode the scan will actually read, which is why ``_open_vetted_dir``
    follows. ``hooks.safe_read_file`` layers the same ``O_NOFOLLOW`` open over the
    same check for single-file reads, so the pairing here mirrors the established
    pattern rather than inventing one.

    A failure inside the chokepoint is treated as "do not enumerate": over-refusing
    a path we cannot canonicalize is the safe direction for a read gate."""
    try:
        return validate_file_path(directory)
    except (OSError, ValueError):
        return None


def _entry_is_sensitive(canonical_dir: str, entry: os.DirEntry) -> bool:
    """Whether one directory ENTRY must be withheld from a completion listing.

    Vetting only the DIRECTORY is not enough: ``~/.kiro/crew`` is not itself on
    the denylist while several of its children are (``security_policy.json``,
    ``profiles/``, ``token_signing.key``), so an entry-blind listing of an
    otherwise-allowed directory still discloses trust-root metadata names.

    ``is_sensitive_path`` is given the JOINED path rather than an explicitly
    resolved one. Two reasons:

    * it already builds resolved AND lexical candidate forms internally, so a
      symlinked child whose TARGET is protected is refused through the link —
      adding ``os.path.realpath(entry.path)`` here would only pay a second
      resolution syscall for the same verdict, at keystroke rate;
    * ``canonical_dir`` comes from ``_vetted_completion_dir``, so for an entry
      that is not itself a link the joined path is already canonical.

    ``validate_file_path`` (hooks.py) is the same check wrapped in exactly that
    redundant ``realpath`` plus an ``expanduser``, and belongs to the agent
    tool-call layer — so the underlying predicate is used directly.

    A classification failure counts as sensitive: over-refusing an entry we
    cannot classify is the safe direction for a read gate."""
    try:
        return is_sensitive_path(os.path.join(canonical_dir, entry.name))
    except (OSError, ValueError):
        return True


def _entry_sort_key(entry: dict) -> tuple:
    """Ranking used by BOTH the bounded-retention heap and the response order:
    earliest match offset first (so a true prefix beats a mid-name hit), dirs
    before files among equals, then case-insensitive name."""
    return (entry["at"], not entry["dir"], str(entry["name"]).lower())


def _list_completions(
    directory: str, prefix: str, folders_only: bool, limit: int
) -> tuple[list[dict], bool]:
    """``_list_vetted_completions`` for a not-yet-canonicalized *directory*.

    Returns ``([], False)`` when the directory is missing, unreadable, or
    sensitive — none of those is an error condition for a keystroke-rate
    endpoint, they just have no completions."""
    vetted = _vetted_completion_dir(directory)
    if vetted is None:
        return [], False
    return _list_vetted_completions(vetted, prefix, folders_only, limit)


def _open_vetted_dir(vetted: str) -> int | None:
    """A descriptor pinned to the directory ``vetted`` named when it was vetted.

    Vetting a PATH and then scanning that PATH are two resolutions of the same
    name, and anything may swap the name between them: replace the directory
    with a symlink to ``~/.ssh`` after the sensitive-path test has passed and
    the scan enumerates the target instead. Closing that window needs the scan
    to be pinned to an inode rather than re-resolving a name, which is what
    scanning a descriptor achieves — once this fd is open, no rename or symlink
    swap can redirect it.

    The open itself is still a name resolution, so it is verified afterwards:
    the fd's identity must equal the identity the vetted path resolves to. A
    swap in that remaining window changes one side of the comparison, so the
    mismatch refuses. ``O_NOFOLLOW`` additionally rejects a final component that
    has become a symlink, which ``realpath`` guaranteed it was not at vet time.

    Returns ``None`` when the directory cannot be opened or fails verification —
    for a keystroke-rate endpoint that is simply "no completions", not an error.
    The caller owns closing the descriptor."""
    try:
        fd = os.open(vetted, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        named = os.stat(vetted)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _list_vetted_completions(
    vetted: str, prefix: str, folders_only: bool, limit: int
) -> tuple[list[dict], bool]:
    """Entries of an ALREADY-VETTED directory matching ``prefix`` anywhere in
    the name.

    Matching is a case-insensitive SUBSTRING search, not a prefix test, so a
    long name can be reached by its distinctive middle: ``termi`` finds
    ``KiroCrew-terminal-completion``. Each entry reports ``at``, the offset the
    fragment was found at, which both ranks the results (earliest match first,
    so a true prefix still wins) and lets the client highlight the span.

    A fragment that STARTS with a dot is matched as a prefix instead. The dot is
    what unhides hidden entries, not a distinctive part of a name, so searching
    for it as a substring would pull in every ``foo.bar`` and defeat the very
    filter it just switched on.

    Hidden entries are included only once the user has typed a leading dot,
    mirroring shell completion.

    Only ``limit`` entries are ever RETAINED (a size-bounded heap over the same
    ranking key), and at most ``_COMPLETE_MAX_SCAN`` entries are examined, so
    neither a huge directory nor a hostile one can grow the response or hold the
    worker thread. ``truncated`` reports either cap being hit."""
    want_hidden = prefix.startswith(".")
    lowered = prefix.lower()
    matched = 0
    scan_capped = False

    # Pinned BEFORE the scan so the enumeration cannot be redirected by a swap
    # of the directory name after vetting passed. See _open_vetted_dir.
    dir_fd = _open_vetted_dir(vetted)
    if dir_fd is None:
        return [], False

    def _candidates():
        # Generator (not a list): heapq.nsmallest below pulls lazily and keeps
        # only `limit` items alive, so a 100k-entry directory never materializes.
        nonlocal matched, scan_capped
        scanned = 0
        with os.scandir(dir_fd) as it:
            for entry in it:
                if scanned >= _COMPLETE_MAX_SCAN:
                    scan_capped = True
                    break
                scanned += 1
                name = entry.name
                if _UNSAFE_NAME_RE.search(name):
                    continue
                if not want_hidden and name.startswith("."):
                    continue
                if not lowered:
                    at = 0
                elif want_hidden:
                    at = 0 if name.lower().startswith(lowered) else -1
                else:
                    at = name.lower().find(lowered)
                if at < 0:
                    continue
                # Ordered AFTER the cheap name filters and BEFORE the stat
                # below: only entries the user could actually receive are
                # classified, so a huge directory does not pay the gate for
                # every name it holds.
                if _entry_is_sensitive(vetted, entry):
                    continue
                try:
                    is_dir = entry.is_dir()  # follows symlinks, as the shell does
                except OSError:
                    is_dir = False
                if folders_only and not is_dir:
                    continue
                matched += 1
                yield {"name": name, "dir": is_dir, "at": at}

    try:
        entries = heapq.nsmallest(limit, _candidates(), key=_entry_sort_key)
    except OSError:
        return [], False
    finally:
        # os.scandir(fd) does NOT take ownership of the descriptor, so it is ours
        # to close on every path out of here.
        os.close(dir_fd)
    return entries, scan_capped or matched > limit


def _resolve_vet_and_list(
    cwd: str, dir_part: str, prefix: str, folders_only: bool, limit: int
) -> tuple[str, bool, list[dict], bool]:
    """Everything a completion needs from the filesystem, in ONE call.

    Resolution (``expanduser`` can trigger a synchronous name-service lookup for
    ``~someuser``) and vetting (``realpath``, which can stall on an unresponsive
    mount) are blocking just like the listing itself, so all three run together
    on one worker thread instead of costing the caller three executor hops.

    Returns ``(lexical_directory, allowed, entries, truncated)``; ``allowed`` is
    False when the resolved directory must not be enumerated."""
    directory = _resolve_completion_dir(cwd, dir_part)
    vetted = _vetted_completion_dir(directory)
    if vetted is None:
        return directory, False, [], False
    entries, truncated = _list_vetted_completions(vetted, prefix, folders_only, limit)
    return directory, True, entries, truncated


def _log_complete(caller: str, outcome: str, reason: str) -> None:
    """SEL API-access event for the completion route.

    Every outcome is audited (blocking rule in
    docs/system-specs/modules/learn-cron-dashboard.md: all terminal endpoints
    emit API-access events), but the payload is DELIBERATELY COARSE — a fixed
    reason word only. This route fires per keystroke, and the token, the prefix,
    the resolved directory and the entry names are all user filesystem contents;
    recording them would turn the audit log into a continuous transcript of what
    the user types and what their disk contains.

    The command tier obeys the same rule and is why it needs its own words rather
    than reusing ``listed``: a reason that named the command or the flag being
    completed would put the user's command line in the audit trail, which is
    exactly what this coarseness exists to prevent. ``cmd_unknown`` covers both
    "not allowlisted" and "not on PATH" for the same reason — distinguishing them
    would disclose which tools are installed."""
    _sel().log_api_access(
        caller=caller,
        operation="terminal.complete",
        outcome=outcome,
        source="dashboard",
        resources=reason,
    )


async def api_terminal_complete(request: web.Request) -> web.Response:
    """POST /api/terminal/complete — completions for the word under a terminal cursor.

    Two mutually exclusive tiers, chosen by the CLIENT because only the client can
    see the screen row:

    * **path** (no ``argv`` in the body) — the default tier. Body
      ``{session_id, token, folders_only?}`` where ``token`` is the DEQUOTED
      literal path the cursor sits in (``"../Kiro"``, ``"src/"``, ``""``); the
      client decodes backslash escapes before asking, so an on-screen ``my\\ dir/``
      arrives here as ``my dir/``.
    * **command** (``argv`` present) — subcommands and flags for an allowlisted
      CLI, e.g. ``argv: ["gh", "pr"]`` with ``token: "cre"``. See
      ``dashboard/terminal_commands.py`` for the protocols and the authority
      argument.

    The tiers do not fall back into one another. The client sends ``argv`` only for
    a word that cannot be a path (no separator, not ``~``-rooted) under a command
    that is not a known path command, so the two never both apply — and keeping
    them disjoint means the path tier's response shape is untouched by this
    addition.

    Authority note (path tier): this lists a directory on behalf of an
    authenticated caller who already owns a LIVE PTY in this gateway — i.e. an
    interactive shell with the gateway user's full filesystem access. Requiring an
    existing session id is what keeps it from being a general filesystem-
    enumeration endpoint; it grants nothing the session's own `ls` does not. Paths
    are therefore resolved without a root restriction, exactly like the shell
    would — with one carve-out: the governance trust-root and credential dirs
    (``is_sensitive_path``) are never enumerated, and no individual ENTRY inside an
    allowed directory is returned if it (or its symlink target) is itself
    protected, so the panel cannot be used to harvest protected metadata names."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.complete",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.complete")
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _log_complete(caller, "denied", "feature_disabled")
        return web.Response(status=403, text="Terminal panel disabled")
    try:
        body = await request.json()
        session_id = body["session_id"]
        token = body.get("token", "")
        folders_only = body.get("folders_only", False)
        # folders_only is type-checked like session_id/token instead of being
        # coerced: bool("false") is True, so a client sending the JSON STRING
        # would silently get files dropped from every listing.
        if (
            not isinstance(session_id, str)
            or not isinstance(token, str)
            or not isinstance(folders_only, bool)
        ):
            raise TypeError
    except Exception:
        _log_complete(caller, "denied", "invalid_body")
        return web.json_response(
            {
                "error": "expected JSON body "
                         "{session_id: string, token?: string, folders_only?: boolean, "
                         "argv?: string[]}",
                "code": "terminal_invalid_body",
            },
            status=400,
        )
    if len(token) > _COMPLETE_TOKEN_MAX:
        _log_complete(caller, "denied", "token_too_long")
        return web.json_response(
            {"error": "token too long", "code": "terminal_token_too_long"}, status=413
        )

    sess = _get_registry(request).get(session_id)
    if sess is None:
        _log_complete(caller, "denied", "unknown_session")
        return web.json_response(
            {"error": "Unknown terminal session", "code": "terminal_unknown_session"},
            status=404,
        )

    # Split BEFORE any filesystem work: `prefix` is a pure function of the token
    # and the completion gate below needs it for the empty answer, so the cwd
    # probe must not run ahead of a request that is going to be suppressed.
    dir_part, prefix = _split_path_token(token)

    # `argv` is validated HERE, above the completion gate, because it is a
    # BODY-SHAPE check like the session_id/token/folders_only ones further up: a
    # malformed request must keep its 400 and its `invalid_argv` denial audit
    # whether completion is on or off. Gating first would turn garbage argv into a
    # silent 200 and drop the refusal from the SEL trail — the client would read a
    # contract violation as "no suggestions". Parsing is pure (no filesystem, no
    # subprocess), so doing it before the gate keeps the gate ahead of all real
    # work; `argv is None` below therefore means "no argv in the body", since an
    # unparsable one has already returned 400.
    raw_argv = body.get("argv")
    argv = None
    if raw_argv is not None:
        argv = terminal_commands.parse_argv(raw_argv)
        if argv is None:
            _log_complete(caller, "denied", "invalid_argv")
            return web.json_response(
                {
                    "error": "argv must be a non-empty list of plain words whose first "
                             "entry is a bare command name",
                    "code": "terminal_invalid_argv",
                },
                status=400,
            )

    # `completion` is read ONCE per request, above the tier split, and reused for
    # both the gate here and the engine's operator command map below. Read off the
    # event loop (`_get_config` does a synchronous `read_text()` of config.json and
    # this route fires per keystroke, so on a slow home filesystem an inline read
    # would stall every gateway task) and type-checked at both nesting levels.
    completion_cfg = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _completion_cfg, request,
    )
    if _completion_disabled(completion_cfg):
        # Gated ABOVE the tier split, so `completion.enabled: false` silences the
        # `cd ` path popup as well as subcommand/flag suggestions — gating only the
        # command tier would leave the path popup alive.
        #
        # The empty listing, NOT a 403: 403 is `_is_enabled`'s whole-panel signal
        # and the client treats it differently. This is the same shape the
        # unknown-cwd branch returns, so a configured silence needs no frontend
        # change. Audited as `ok`: nothing was refused, and naming the state lets
        # an operator tell a configured silence from a broken route.
        _log_complete(caller, "ok", "completion_disabled")
        return web.json_response(
            {"dir": None, "prefix": prefix, "entries": [], "truncated": False}
        )

    cwd = await _session_cwd_cached(sess)

    # ── Command tier ──
    # Ordered before the unknown-cwd branch: a subcommand list does not depend on
    # the working directory (a cobra probe answers without one), so a session whose
    # cwd cannot be read still gets `gh pr` completions even though it can get no
    # path ones.
    if argv is not None:
        # Already parsed and validated above the completion gate, so this branch
        # reuses it rather than parsing twice; a non-None value is by construction
        # a well-formed argv.
        # `completion_cfg` was read once above the tier split — off the event loop
        # and type-checked at both nesting levels — so this branch reuses it
        # rather than paying a second per-keystroke read of config.json.
        cmd_entries, reason = await terminal_commands.complete(
            argv, token, cwd, completion_cfg.get("commands"),
        )
        _log_complete(caller, "denied" if reason == "sensitive_path" else "ok", reason)
        # `dir: null` — the same "nothing was resolved on the filesystem" signal
        # the path tier uses for an unknown cwd, so the client needs no new
        # top-level field to tell a command answer from a path one; the per-entry
        # `kind` carries that.
        return web.json_response(
            {
                "dir": None,
                "prefix": prefix,
                "entries": [e.to_json() for e in cmd_entries],
                "truncated": False,
            }
        )

    if not cwd:
        # cwd is unknowable (Windows, or the probe failed) — no completions
        # rather than an error the frontend would have to special-case. A null
        # ``dir`` is the signal that nothing was resolved.
        _log_complete(caller, "ok", "no_cwd")
        return web.json_response(
            {"dir": None, "prefix": prefix, "entries": [], "truncated": False}
        )

    loop = asyncio.get_running_loop()
    # discovery_executor, not subprocess_executor: this is a read-only
    # filesystem scan, and subprocess_executor's workers are shared with PTY
    # teardown (an os.close that can wedge in the kernel) — a slow directory
    # here must not be able to occupy a thread that session cleanup needs.
    # Resolution and vetting ride the SAME hop as the listing: all three touch
    # the filesystem (or the name service, via ``~user`` expansion), so none of
    # them may run inline in this coroutine, and one hop keeps a keystroke's
    # latency to a single thread round-trip.
    directory, allowed, entries, truncated = await loop.run_in_executor(
        discovery_executor(),
        _resolve_vet_and_list,
        cwd,
        dir_part,
        prefix,
        folders_only,
        _COMPLETE_MAX_ENTRIES,
    )
    if not allowed:
        # Protected tree (or a symlink resolving into one). Answer with the SAME
        # empty-listing shape as the unknown-cwd branch so the client needs no
        # special case — and so the response does not disclose whether the path
        # exists.
        _log_complete(caller, "denied", "sensitive_path")
        return web.json_response(
            {"dir": None, "prefix": prefix, "entries": [], "truncated": False}
        )
    _log_complete(caller, "ok", "listed")
    return web.json_response(
        {
            # The LEXICAL path, not the canonicalized one used for the gate: this
            # is displayed back to the user, who typed it, and /tmp reading as
            # /private/tmp would be confusing.
            "dir": directory,
            "prefix": prefix,
            "entries": entries,
            "truncated": truncated,
        }
    )


async def api_terminal_delete(request: web.Request) -> web.Response:
    """DELETE /api/terminal/sessions/{session_id} — kill a terminal session."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(
        request, "terminal.session.delete"
    )
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")

    session_id = request.match_info.get("session_id", "")
    # Same bound ``api_terminal_ws`` applies when a session is created, so an id
    # outside it provably keys nothing in this registry. Rejecting it here means
    # a malformed request is told so, rather than being answered "no such
    # session" after the lookup. Plaintext 400 to match this handler's other
    # responses (401/403/404 above and below are all plaintext).
    if not session_id or len(session_id) > _MAX_SESSION_ID_LEN:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources=f"invalid_session_id={session_id!r}",
        )
        return web.Response(status=400, text="Invalid session_id")

    registry = _get_registry(request)
    sess = registry.get(session_id)
    if not sess:
        return web.Response(status=404, text="Session not found")
    registry.pop(session_id, None)  # type: ignore[arg-type]

    if sess.ws and not sess.ws.closed:
        await sess.ws.close()
    await _kill_session(sess)

    _sel().log_api_access(
        caller=caller,
        operation="terminal.session.delete",
        outcome="ok",
        source="dashboard",
        resources=f"session={session_id}",
    )
    return web.json_response({"deleted": session_id})


async def api_terminal_list(request: web.Request) -> web.Response:
    """GET /api/terminal/sessions — list active terminal sessions."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.session.list",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(
        request, "terminal.session.list"
    )
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.list",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.json_response({"enabled": False, "sessions": []})

    registry = _get_registry(request)
    sessions = []
    for sid, sess in registry.items():
        if sess is None:
            continue  # placeholder during ws.prepare()
        sessions.append(
            {
                "session_id": sid,
                "pid": _sess_pid(sess),
                "alive": _sess_alive(sess),
                "cols": sess.cols,
                "rows": sess.rows,
                "connected": sess.ws is not None and not sess.ws.closed,
            }
        )
    _sel().log_api_access(
        caller=caller,
        operation="terminal.session.list",
        outcome="ok",
        source="dashboard",
        resources=f"count={len(sessions)}",
    )
    return web.json_response({"enabled": True, "sessions": sessions})


async def reap_orphaned_terminals(app: web.Application) -> None:
    """Background task: kill PTY sessions with no WS connection for >5 min."""
    try:
        while True:
            await asyncio.sleep(60)
            state = app.get("state")
            if not state or not hasattr(state, "_terminal_sessions"):
                continue
            registry: dict[str, _TerminalSession | None] = state._terminal_sessions
            bus: NotificationBus | None = getattr(state, "notification_bus", None)
            exited = _exited_for_state(state)
            now = time.monotonic()
            to_remove = []
            for sid, sess in registry.items():
                if sess is None:
                    continue  # placeholder during ws.prepare()
                # A dead child is an exit even when also idle, so check it first.
                if not _sess_alive(sess):
                    to_remove.append((sid, sess, True))
                # Idle too long: killing a live shell is a teardown, not an exit.
                elif sess.last_ws_disconnect and (now - sess.last_ws_disconnect) > _ORPHAN_TIMEOUT_S:
                    to_remove.append((sid, sess, False))
            for sid, sess, died in to_remove:
                if registry.get(sid) is not sess:
                    continue
                if died:
                    status = await _dead_session_status(sess)
                    if registry.get(sid) is not sess:
                        continue  # the reader or a dial disposed of it meanwhile
                    close = _close_on_exit()
                    _announce_exit(sess, status, bus=bus, exited=exited, close=close)
                    # A window may still be attached: a descendant holding the
                    # PTY keeps the reader parked, so the sweep is the one that
                    # finds the shell dead and must tell the tab, as the
                    # reader's end does. The record is already written above.
                    if not await _tell_owner_exited_and_pop(
                        registry, sess, status=status, close=close, exited=None
                    ):
                        continue
                else:
                    registry.pop(sid, None)
                await _kill_session(sess)
                logger.info("Reaped orphaned terminal session %s", sid)
    except asyncio.CancelledError:
        pass


async def poll_terminal_titles(app: web.Application) -> None:
    """Background task: push a per-session title (foreground command name while
    one runs, else the shell's cwd basename) and the shell's full cwd to each
    connected terminal, on change only. Fast commands that finish within the
    poll interval never flip the title, so there's no flicker at the prompt.

    The cadence is an upper bound, not a rate: a session is only probed when it
    has written to the PTY since its last probe, so a terminal idling at a
    prompt costs nothing at all."""
    try:
        while True:
            await asyncio.sleep(1.0)
            state = app.get("state")
            if not state or not hasattr(state, "_terminal_sessions"):
                continue
            registry: dict[str, _TerminalSession] = state._terminal_sessions
            loop = asyncio.get_running_loop()
            for sess in list(registry.values()):
                if sess is None or sess.ws is None or sess.ws.closed:
                    continue
                if not sess.frames_dirty:
                    continue
                # Cleared BEFORE probing, never after: output landing while a
                # probe is in flight must re-dirty the session so the next tick
                # picks up the change instead of the flag swallowing it.
                sess.frames_dirty = False
                # _session_title / _session_cwd do blocking syscalls (tcgetpgrp
                # ioctl, /proc reads, lsof where nothing cheaper answers) that
                # can wedge on a D-state process or a stuck fs; run them off the
                # loop on the subprocess pool (same rationale as the os.close
                # offload in _kill_session) so one stuck read can never freeze
                # the gateway event loop. Both probes share one cwd lookup via
                # the session's memo.
                # The WS can detach (sess.ws = None) or be displaced by a
                # reconnect while an executor probe is in flight — capture +
                # revalidate the socket after EACH hop, and let the bounded
                # owner-checked sender re-confirm identity under the transport
                # lock so a displaced socket never receives a frame and a
                # blocked transport can never park the singleton poller.
                title = await loop.run_in_executor(subprocess_executor(), _session_title, sess)
                ws = sess.ws
                if ws is None or ws.closed:
                    continue
                if title and title != sess.last_title:
                    # Advance the dedup marker only after the frame was handed
                    # to the owner's transport: a timed-out or failed send must
                    # not suppress the retry, or chat handoff would label output
                    # with a stale title/cwd until the value changes again.
                    if await _send_owner_control_frame(
                        sess, ws, {"type": "title", "text": title}
                    ):
                        sess.last_title = title
                # Live cwd (full path) rides the same poll: the frontend uses it
                # to attribute terminal output handed off to chat. Pushed only
                # on change, like the title.
                cwd = await loop.run_in_executor(subprocess_executor(), _session_cwd, sess)
                ws = sess.ws
                if ws is None or ws.closed:
                    continue
                if cwd and cwd != sess.last_cwd:
                    if await _send_owner_control_frame(
                        sess, ws, {"type": "cwd", "path": cwd}
                    ):
                        sess.last_cwd = cwd
    except asyncio.CancelledError:
        pass
