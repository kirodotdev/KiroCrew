"""WebSocket PTY handler for the built-in CLI panel."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_path
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# PTY support is POSIX-only (openpty/fork/ioctl/termios). On Windows these
# modules do not exist; the web-terminal panel degrades to a clear error.
if platform_compat.IS_POSIX:
    import fcntl
    import pty as _pty
    import signal
    import termios
else:  # pragma: no cover — Windows fallback
    fcntl = None  # type: ignore[assignment]
    _pty = None  # type: ignore[assignment]
    signal = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

# Global ceiling across ALL chats' terminal tabs. Each chat's activity bar caps
# its own terminals (frontend MAX_TERMINALS_PER_CHAT); this is the server-side
# backstop. Override via config.json dashboard.terminal.max_sessions.
_MAX_SESSIONS = 12
_ORPHAN_TIMEOUT_S = 900  # 15 min with no WS → reap PTY (grace window for reload/network drops; in-app nav keeps the WS alive)
_SCROLLBACK_MAX = 50 * 1024  # 50KB ring buffer per session for reconnect replay

# Fail-fast message + SEL reason for the Windows-unsupported path. Kept as a
# module constant so the POST create-session handler and the WebSocket open
# handler return byte-identical wording (avoids drift; CLAUDE.md forbids
# scattered business-logic string literals).
_UNSUPPORTED_PLATFORM_MSG = "The web terminal is not supported on Windows."
_UNSUPPORTED_PLATFORM_REASON = "unsupported_platform"


def _redact_terminal(data: bytes | bytearray) -> bytes:
    """Strip credentials/exfiltration URLs from PTY output before it reaches a
    client. ``kiro_crew.security`` redactors return ``(text, warnings)`` tuples
    (unlike upstream's str-returning ``redaction`` module), so unpack both.

    Accepts ``bytearray`` too: the reconnect-replay path passes the
    ``_TerminalSession.scrollback`` ring buffer (a ``bytearray``) directly, and
    ``.decode()`` behaves identically on both."""
    text = data.decode("utf-8", errors="replace")
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text.encode("utf-8")


def _sel():
    import kiro_crew.dashboard.handlers as _pkg  # circular import: __init__ imports terminal

    return _pkg.sel()


@dataclass
class _TerminalSession:
    """Server-side state for one PTY session."""

    session_id: str
    master_fd: int
    proc: asyncio.subprocess.Process
    cols: int = 80
    rows: int = 24
    created_at: float = field(default_factory=time.monotonic)
    last_ws_disconnect: float | None = None  # set when WS drops, cleared on reconnect
    ws: web.WebSocketResponse | None = None
    reader_task: asyncio.Task | None = None
    scrollback: bytearray = field(default_factory=bytearray)
    last_title: str | None = None  # last title pushed to the client (dedup)
    last_cwd: str | None = None  # last cwd pushed to the client (dedup)
    # Serializes concurrent WS writes (reader loop + title poller + pong);
    # aiohttp's WebSocket writer is not safe for concurrent sends.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _get_registry(request: web.Request) -> dict[str, _TerminalSession | None]:
    state: DashboardState = request.app["state"]
    return state._terminal_sessions


def _get_config(request: web.Request) -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data.get("dashboard", {}).get("terminal", {})
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


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
    """Current working directory of a process. Linux /proc first; on hosts
    without /proc (macOS/BSD) falls back to `lsof -d cwd`, whose ``-Fn`` output
    carries the path on an ``n``-prefixed line. Blocking (subprocess) — callers
    must run this off the event loop (the title poller already does)."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
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


def _session_cwd(sess: "_TerminalSession") -> str | None:
    """Full current working directory of the session's shell, or None."""
    if not platform_compat.IS_POSIX or sess.proc is None:
        return None
    return _proc_cwd(sess.proc.pid)


def _session_title(sess: "_TerminalSession") -> str | None:
    """Best-effort "what is this terminal doing" label: the foreground command
    name while one runs, else the shell's cwd basename. Linux /proc based;
    returns None when it can't tell (client keeps its current title, so on
    non-Linux hosts the tab simply stays at its cwd default)."""
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
    cwd = _proc_cwd(sess.proc.pid)
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return None


async def _kill_session(sess: _TerminalSession) -> None:
    """Kill PTY process and close FDs for a session."""
    # Close master_fd first — unblocks reader_task's os.read() in executor.
    #
    # os.close() on a PTY master fd can BLOCK in the kernel: when the far-end
    # shell is wedged (uninterruptible sleep), the tty teardown waits on it.
    # Run it on the dedicated subprocess pool, never the event loop — a wedged
    # close then costs at most one pool thread instead of freezing the whole
    # gateway, and shares no workers with the orphan-reaping maintenance sweep.
    # Captured 2026-06-28 as a 25s loop-stall wedge here (reap_orphaned_terminals
    # -> _kill_session); same family as the _get_start_time and
    # _cleanup_orphaned_mcp_servers off-loop offloads.
    if sess.master_fd >= 0:
        fd = sess.master_fd
        # Clear the handle BEFORE the await: if this coroutine is cancelled while
        # suspended on the executor (e.g. aiohttp cancels the request handler on
        # client disconnect), the fd must not be left referenced on the session.
        sess.master_fd = -1
        try:
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), os.close, fd,
            )
        except (OSError, RuntimeError):
            # OSError: close failed. RuntimeError: the subprocess pool was
            # already torn down (shutdown races interpreter exit) — submit
            # raises rather than returning a future; the fd is reaped on exit.
            pass
    if sess.reader_task is not None:
        sess.reader_task.cancel()
        try:
            await sess.reader_task
        except (asyncio.CancelledError, Exception):
            pass
    if sess.proc is not None and sess.proc.returncode is None:
        # Route through platform_compat.kill_process_tree so the whole terminal
        # handler stays platform-portable (killpg on POSIX, taskkill /T on
        # Windows). This PTY teardown is POSIX-only in practice — api_terminal_
        # ws returns an error on Windows before any session is created — but
        # keeping a single shim call site avoids a raw-os.killpg vs shim
        # inconsistency across the module, and the tests all patch the shim.
        try:
            # Async variants offload Windows taskkill to subprocess_executor
            # so this PTY teardown path never blocks the event loop on
            # taskkill.exe. POSIX os.killpg stays inline.
            await platform_compat.kill_process_tree_async(
                sess.proc.pid, platform_compat.SIGTERM
            )
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
            await sess.proc.wait()


async def api_terminal_ws(request: web.Request) -> web.WebSocketResponse | web.Response:
    """WebSocket PTY for the built-in CLI panel.

    Protocol:
      - Binary frames: raw terminal I/O (both directions)
      - Text frames (JSON): control messages
        - Client→Server: {"type":"resize","cols":N,"rows":N}
        - Client→Server: {"type":"ping"}
        - Server→Client: {"type":"pong"}
    """
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
    if not session_id or len(session_id) > 64:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"invalid_session_id={session_id!r}",
        )
        return web.Response(status=400, text="Invalid session_id")

    registry = _get_registry(request)
    cfg = _get_config(request)
    max_sessions = cfg.get("max_sessions", _MAX_SESSIONS)

    # Check if reconnecting to existing session
    existing = registry.get(session_id)
    if existing and existing.proc.returncode is not None:
        # Process died — clean up stale entry
        await _kill_session(existing)
        del registry[session_id]
        existing = None

    # Reserve slot synchronously before any await to prevent race condition (#5)
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
        # Reconnect to existing PTY.
        # Replay scrollback BEFORE assigning ws to prevent read_pty from
        # forwarding live data before replay completes.
        if existing.scrollback:
            await ws.send_bytes(_redact_terminal(existing.scrollback))
        existing.ws = ws
        existing.last_ws_disconnect = None
        # A fresh client starts with empty title/cwd state; clear the dedup
        # markers so the next poll re-pushes both frames even when unchanged.
        existing.last_title = None
        existing.last_cwd = None
        sess = existing
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.reconnect",
            outcome="ok",
            source="dashboard",
            resources=f"session={session_id},pid={sess.proc.pid}",
        )
    elif not platform_compat.IS_POSIX:
        # PTY/fork are POSIX-only; the web terminal is unavailable on Windows.
        if placeholder:
            registry.pop(session_id, None)  # type: ignore[arg-type]
        _sel().log_api_access(
            caller=caller, operation="terminal.ws.open",
            outcome="denied", source="dashboard",
            resources=_UNSUPPORTED_PLATFORM_REASON,
        )
        if not ws.closed:
            await ws.send_str(json.dumps({
                "type": "error",
                "message": _UNSUPPORTED_PLATFORM_MSG,
            }))
            await ws.close()
        return ws
    else:
        # Spawn new PTY
        master_fd, worker_fd = _pty.openpty()
        try:
            fcntl.ioctl(
                worker_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 24, 80, 0, 0),
            )
            shell = str(cfg.get("shell") or os.environ.get("SHELL", "/bin/bash"))
            cwd = _resolve_cwd(cfg, request.query.get("cwd"))
            env = {
                **os.environ,
                "TERM": "xterm-256color",
                "KIROCREW_TERMINAL": "1",
            }
            # Security: intentionally unsandboxed — this is the user's own
            # interactive terminal (like SSH), not agent-executed code.
            # Auth is enforced at WS handshake via token_auth_middleware.
            # See CLI_PANEL_DESIGN.md §8 "Security Considerations".
            # TIOCSCTTY makes the PTY the controlling terminal after
            # setsid(). Without this, Ctrl+C (SIGINT) doesn't work
            # because the kernel can't find the foreground process group.
            tiocsctty = getattr(termios, "TIOCSCTTY", 0x540E)

            def _setup_ctty():
                # Safe in forked child: single ioctl with pre-resolved int,
                # no Python allocation or lock acquisition.
                fcntl.ioctl(0, tiocsctty, 0)

            proc = await asyncio.create_subprocess_exec(
                shell,
                "-l",
                stdin=worker_fd,
                stdout=worker_fd,
                stderr=worker_fd,
                start_new_session=True,
                preexec_fn=_setup_ctty,
                cwd=cwd,
                env=env,
            )
        except Exception as exc:
            # Clean up master_fd on failure (#6)
            try:
                os.close(master_fd)
            except OSError:
                pass
            registry.pop(session_id, None)  # type: ignore[arg-type]
            # WS already prepared — send error over WS then close (#3)
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
        )
        registry[session_id] = sess
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="ok",
            source="dashboard",
            resources=f"session={session_id},pid={proc.pid},shell={shell}",
        )

    # --- Read loop: PTY → WebSocket ---
    async def read_pty():
        try:
            loop = asyncio.get_running_loop()
            while True:
                data = await loop.run_in_executor(
                    None,
                    lambda: os.read(sess.master_fd, 4096),
                )
                if not data:
                    break
                sess.scrollback.extend(data)
                if len(sess.scrollback) > _SCROLLBACK_MAX:
                    sess.scrollback = sess.scrollback[-_SCROLLBACK_MAX:]
                if sess.ws and not sess.ws.closed:
                    async with sess.send_lock:
                        await sess.ws.send_bytes(_redact_terminal(data))
        except OSError:
            pass

    if sess.reader_task is None or sess.reader_task.done():
        sess.reader_task = asyncio.ensure_future(read_pty())

    # --- Write loop: WebSocket → PTY ---
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        os.write,
                        sess.master_fd,
                        msg.data,
                    )
                except OSError:
                    break
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    ctrl = json.loads(msg.data)
                except (json.JSONDecodeError, ValueError):
                    continue
                if ctrl.get("type") == "resize":
                    try:
                        cols = min(max(int(ctrl.get("cols", 80)), 1), 500)
                        rows = min(max(int(ctrl.get("rows", 24)), 1), 200)
                    except (ValueError, TypeError):
                        continue
                    sess.cols = cols
                    sess.rows = rows
                    try:
                        fcntl.ioctl(
                            sess.master_fd,
                            termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0),
                        )
                    except OSError:
                        pass
                elif ctrl.get("type") == "ping":
                    if not ws.closed:
                        async with sess.send_lock:
                            await ws.send_str(json.dumps({"type": "pong"}))
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
    finally:
        # WS disconnected — mark for orphan reaper, but keep PTY alive
        sess.ws = None
        sess.last_ws_disconnect = time.monotonic()
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
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.create",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")

    if platform_compat.IS_WINDOWS:
        # PTY/fork are POSIX-only; on Windows we fail fast at session-create so
        # the frontend surfaces the "not supported" error immediately instead of
        # opening a WebSocket that dies during PTY spawn. Same wording as the WS
        # handler's error frame so the frontend rendering is uniform. A ConPTY
        # backend is deferred.
        _sel().log_api_access(
            caller=caller, operation="terminal.session.create",
            outcome="denied", source="dashboard",
            resources=_UNSUPPORTED_PLATFORM_REASON,
        )
        return web.json_response(
            {"error": _UNSUPPORTED_PLATFORM_MSG,
             "reason": _UNSUPPORTED_PLATFORM_REASON},
            status=501,
        )

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
            {"error": f"Max {max_sessions} sessions"},
            status=429,
        )

    session_id = uuid.uuid4().hex[:12]
    shell = cfg.get("shell") or os.environ.get("SHELL", "/bin/bash")
    _sel().log_api_access(
        caller=caller,
        operation="terminal.session.create",
        outcome="ok",
        source="dashboard",
        resources=f"session={session_id}",
    )
    return web.json_response(
        {
            "session_id": session_id,
            "shell": shell,
        }
    )


# Selection hand-off size cap. Generous for terminal selections (xterm buffers
# are bounded anyway) while preventing a multi-megabyte POST from tying up the
# redactors on the event loop's executor.
_REDACT_MAX_BYTES = 256 * 1024


async def api_terminal_redact(request: web.Request) -> web.Response:
    """POST /api/terminal/redact — re-scan a COMPLETE terminal selection before
    it is inserted into chat. Streaming output is redacted per read chunk, so a
    credential straddling a chunk boundary can evade both scans; the selection
    hand-off re-runs the redactors over the contiguous text. Callers MUST fail
    closed: no chat insertion unless this returns 200 with redacted text."""
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
        return web.json_response({"error": "expected JSON body {text: string}"}, status=400)
    if len(text.encode("utf-8", errors="replace")) > _REDACT_MAX_BYTES:
        return web.json_response({"error": "selection too large"}, status=413)
    # Same redactors as the streaming path (_redact_terminal), applied to the
    # contiguous selection so boundary-straddling secrets cannot slip through.
    # Run off-loop: the redactors are regex scans that scale with input size.
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
        return web.json_response({"error": "redaction failed"}, status=500)
    return web.json_response({"text": redacted})


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
    registry = _get_registry(request)
    sess = registry.pop(session_id, None)  # type: ignore[arg-type]
    if not sess:
        return web.Response(status=404, text="Session not found")

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
                "pid": sess.proc.pid if sess.proc else None,
                "alive": sess.proc.returncode is None if sess.proc else False,
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
            registry: dict[str, _TerminalSession] = state._terminal_sessions
            now = time.monotonic()
            to_remove = []
            for sid, sess in registry.items():
                if sess is None:
                    continue  # placeholder during ws.prepare()
                # Reap if disconnected too long
                if sess.last_ws_disconnect and (now - sess.last_ws_disconnect) > _ORPHAN_TIMEOUT_S:
                    to_remove.append(sid)
                # Reap if process died
                elif sess.proc.returncode is not None:
                    to_remove.append(sid)
            for sid in to_remove:
                removed = registry.pop(sid, None)
                if removed is not None:
                    await _kill_session(removed)
                    logger.info("Reaped orphaned terminal session %s", sid)
    except asyncio.CancelledError:
        pass


async def poll_terminal_titles(app: web.Application) -> None:
    """Background task: push a per-session title (foreground command name while
    one runs, else the shell's cwd basename) to each connected terminal ~1/s,
    and only when it changes. Fast commands that finish within the poll interval
    never flip the title, so there's no flicker at the prompt."""
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
                # _session_title / _session_cwd do blocking syscalls (tcgetpgrp
                # ioctl, /proc reads, lsof on macOS) that can wedge on a D-state
                # process or a stuck fs; run them off the loop on the subprocess
                # pool (same rationale as the os.close offload in _kill_session)
                # so one stuck read can never freeze the gateway event loop.
                # The WS can detach (sess.ws = None) while an executor probe is
                # in flight — capture + revalidate the socket after EACH hop so
                # a disconnect can never AttributeError the singleton poller.
                title = await loop.run_in_executor(subprocess_executor(), _session_title, sess)
                ws = sess.ws
                if ws is None or ws.closed:
                    continue
                if title and title != sess.last_title:
                    sess.last_title = title
                    try:
                        async with sess.send_lock:
                            await ws.send_str(json.dumps({"type": "title", "text": title}))
                    except (ConnectionResetError, RuntimeError, OSError):
                        pass
                # Live cwd (full path) rides the same poll: the frontend uses it
                # to attribute terminal output handed off to chat. Pushed only
                # on change, like the title.
                cwd = await loop.run_in_executor(subprocess_executor(), _session_cwd, sess)
                ws = sess.ws
                if ws is None or ws.closed:
                    continue
                if cwd and cwd != sess.last_cwd:
                    sess.last_cwd = cwd
                    try:
                        async with sess.send_lock:
                            await ws.send_str(json.dumps({"type": "cwd", "path": cwd}))
                    except (ConnectionResetError, RuntimeError, OSError):
                        pass
    except asyncio.CancelledError:
        pass
