"""The life of a terminal session's shell child: whether it is alive, how it exited, the
failure notice a shell that died at once raises, and tearing a session down -- on a
delete, on reader EOF, or when the orphan reaper finds no socket for too long.

An owner of :mod:`kiro_crew.dashboard.handlers.terminal`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers.terminal_transport import (
    _close_terminal_ws_bounded,
    _send_owner_control_frame,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.notifications.bus import NotificationPayload

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.dashboard.handlers.terminal_session import _TerminalSession
    from kiro_crew.notifications.bus import NotificationBus

# The facade's logger, so a log filter or capture keyed on the terminal handler's
# name sees this module's records too.
logger = logging.getLogger("kiro_crew.dashboard.handlers.terminal")


_ORPHAN_TIMEOUT_S = 900  # 15 min with no WS → reap PTY (grace window for reload/network drops; in-app nav keeps the WS alive)


# A submitted close retains ownership of its descriptor after this wait ends.
# Repeated handler cancellation must not restart the deadline or cancel it.
_TERMINAL_FD_CLOSE_TIMEOUT_S = 5.0


# Private-use WebSocket close code: the shell is gone, so clients must close the
# tab instead of treating the close as a transient drop and redialing.
_TERMINAL_WS_CLOSE_SHELL_EXITED = 4001


# Ceiling on the wait for the child to be reaped after reader EOF. Set by
# ConPTY, which took 3.08s to report a clean ``cmd.exe`` exit dead on
# windows-latest; POSIX resolves in well under 20ms, so there the bound only
# matters for a child that never goes.
_CHILD_REAP_TIMEOUT_S = 10.0


# ConPTY has no child watcher, so its liveness is polled at this interval.
_CONPTY_STATUS_POLL_S = 0.05


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


async def _child_exit_status(sess: _TerminalSession) -> int | None:
    """Wait, bounded, for the session's child to go; return its exit status.

    ``None`` when the status is unavailable: the child outlived
    ``_CHILD_REAP_TIMEOUT_S``, or the binding could not report one. Callers
    tell a child still running from an unavailable code with ``_sess_alive``.
    """
    wp = sess.winpty
    if wp is not None:
        # Poll the non-blocking ``isalive()`` rather than pywinpty's ``wait()``,
        # whose executor thread cannot be cancelled at the deadline.
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


def _shell_failure_note(status: int) -> tuple[str, str]:
    """Title and body of the bell note for a shell that exited non-zero.

    The title is fixed; the body says how the shell ended, and asyncio reports
    a death by signal as a negative code, so that case names the signal
    instead. The note carries
    nothing about the session itself -- no id, shell path or directory --
    because the notification feed reaches every dashboard session, including
    ones that are not the terminal's owner.
    """
    if status < 0:
        try:
            how = f"was killed by {signal.Signals(-status).name}"
        except ValueError:
            how = f"was killed by signal {-status}"
    else:
        how = f"exited with code {status}"
    return "Terminal exited abnormally", f"The shell {how}."


def _notify_shell_failed(bus: NotificationBus, sess: _TerminalSession, status: int) -> None:
    """Push one bell-feed note for a shell that exited with a non-zero code.

    The tab closes with the shell, so this note is the only place the code is
    left to read. One ``group_key`` for all sessions, so a shell dying in a
    loop stacks. Best-effort: a refusing bus is logged and the reap goes on.
    """
    title, body = _shell_failure_note(status)
    try:
        bus.push(
            NotificationPayload(
                source="system",
                channel="system.terminal",
                title=title,
                body=body,
                group_key="terminal-exit",
                meta={"status": status},
            )
        )
    except Exception:  # noqa: BLE001 -- the reap must not depend on the feed
        logger.warning(
            "terminal %s: could not publish the exit notification", sess.session_id, exc_info=True
        )


async def _handle_pty_reader_end(
    registry: dict[str, _TerminalSession | None],
    sess: _TerminalSession,
    *,
    bus: NotificationBus | None = None,
) -> None:
    """Close the tab of a session whose shell exited on its own, then reap it.

    The reader also ends on a deliberate teardown (``DELETE``, the orphan
    sweep, a dial replacing a dead entry), which raises the same ``OSError``.
    Each of those pops the registry entry or cancels this task before closing
    the fd, so the identity check below tells them apart from an exit.

    The attached window gets ``{"type": "exit", "status": N|null}`` and a 4001
    close, and removes the tab. A detached window learns nothing here: its next
    dial finds no entry, which the client already reads as a fresh session.
    """
    if registry.get(sess.session_id) is not sess:
        return  # deliberate teardown, already accounted for by its own path
    status = await _child_exit_status(sess)
    if registry.get(sess.session_id) is not sess:
        return  # teardown or replacement took the slot while status resolved
    if status is None and _sess_alive(sess):
        return  # the fd closed under a child that is still running: not an exit
    if bus is not None and status is not None and status != 0:
        # Published before any transport await so owner churn cannot lose it.
        _notify_shell_failed(bus, sess, status)
    async with sess.replace_lock:
        # Serialized with reconnect publication: a reconnect that won the lock
        # is the owner told below; one queued behind it finds the entry gone.
        if registry.get(sess.session_id) is not sess:
            return
        ws = sess.ws
        if ws is not None:
            await _send_owner_control_frame(sess, ws, {"type": "exit", "status": status})
            if sess.ws is ws:
                await _close_terminal_ws_bounded(ws, close_code=_TERMINAL_WS_CLOSE_SHELL_EXITED)
        if registry.get(sess.session_id) is not sess:
            return
        registry.pop(sess.session_id, None)
    # We run inside reader_task. On ConPTY, _kill_session cancels and awaits the
    # reader before terminating the child, which from inside that task would
    # abort the teardown before the pseudo-console is closed.
    sess.reader_task = None
    await _kill_session(sess)


async def _close_terminal_fd(fd: int) -> None:
    """Submit close once, retaining queued work through cancellation or timeout."""
    loop = asyncio.get_running_loop()
    closing = loop.run_in_executor(subprocess_executor(), os.close, fd)
    deadline = loop.time() + _TERMINAL_FD_CLOSE_TIMEOUT_S
    cancelled: asyncio.CancelledError | None = None

    def report_late_close(done: asyncio.Future[None]) -> None:
        try:
            done.result()
        except (asyncio.CancelledError, OSError, RuntimeError):
            logger.warning("terminal: delayed descriptor close failed", exc_info=True)

    try:
        while not closing.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                await asyncio.wait_for(asyncio.shield(closing), timeout=remaining)
            except asyncio.CancelledError as exc:
                cancelled = exc
        closing.result()
    except asyncio.TimeoutError:
        logger.warning(
            "terminal: descriptor close still pending after %ss", _TERMINAL_FD_CLOSE_TIMEOUT_S
        )
        raise
    finally:
        if not closing.done():
            # Never retry the numeric fd: the submitted close may finish and
            # the OS may reuse it before this callback observes completion.
            closing.add_done_callback(report_late_close)
        if cancelled is not None:
            raise cancelled


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
                await platform_compat.kill_process_tree_async(pid, platform_compat.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        # Free the pseudo-console + handles (TerminateProcess is a backstop).
        try:
            await loop.run_in_executor(
                subprocess_executor(),
                wp.terminate,  # type: ignore[attr-defined]
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
                await asyncio.wait_for(sess.proc.wait(), timeout=platform_compat.REAP_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                logger.warning(
                    "terminal: session %s pid %s did not exit %ss after SIGKILL; "
                    "continuing teardown without reaping it",
                    sess.session_id,
                    sess.proc.pid,
                    platform_compat.REAP_TIMEOUT_SECS,
                )
    # os.close() on a PTY controller fd can still BLOCK in the kernel when the far
    # end is wedged (uninterruptible sleep, or a child this process may not
    # signal). Run it on the dedicated subprocess pool, never the event loop, so a
    # wedged close then costs at most one pool thread instead of freezing the
    # whole gateway, and shares no workers with the orphan-reaping maintenance
    # sweep.
    try:
        if sess.master_fd >= 0:  # wokeignore:rule=master
            fd = sess.master_fd  # wokeignore:rule=master
            # Ownership transfers to the submitted close before awaiting it.
            sess.master_fd = -1  # wokeignore:rule=master
            try:
                await _close_terminal_fd(fd)
            except (OSError, RuntimeError, asyncio.TimeoutError):
                # A timed-out close stays owned by its executor work. If the
                # pool has already shut down, process exit reaps the descriptor.
                pass
    finally:
        if sess.reader_task is not None:
            sess.reader_task.cancel()
            try:
                await sess.reader_task
            except (asyncio.CancelledError, Exception):
                pass


async def reap_orphaned_terminals(app: web.Application) -> None:
    """Background task: kill PTY sessions with no WS connection for >5 min."""
    try:
        while True:
            await asyncio.sleep(60)
            state = app.get("state")
            if not state or not hasattr(state, "_terminal_sessions"):
                continue
            registry: dict[str, _TerminalSession | None] = state._terminal_sessions
            for sid, sess in list(registry.items()):
                # Earlier teardown can yield to DELETE, a new opener, or reconnect.
                if sess is None or registry.get(sid) is not sess:
                    continue
                now = time.monotonic()
                if not (
                    sess.last_ws_disconnect and (now - sess.last_ws_disconnect) > _ORPHAN_TIMEOUT_S
                ) and _sess_alive(sess):
                    continue
                registry.pop(sid)
                await _kill_session(sess)
                logger.info("Reaped orphaned terminal session %s", sid)
    except asyncio.CancelledError:
        pass
