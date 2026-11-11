"""One supervised driver child per gateway, spoken to over stdio MCP.

``cua-driver mcp`` is a line-delimited JSON-RPC MCP server on stdin/stdout. On
Linux and Windows a plain ``mcp`` process owns its own desktop runtime, so one
child is the whole driver; on macOS it attaches to the signed ``CuaDriver.app``
runtime, which is what keeps the permission grant on that bundle.

:class:`DriverSupervisor` is the only code that talks to the child:

* **Single owner, serialized.** One child per supervisor and one call in flight
  at a time (:attr:`_call_lock`): the desktop is one action stream, and two
  sessions' clicks interleaving inside one window would be a race neither could
  see. A second caller waits for the first to finish.
* **Closed allowlist.** :meth:`call` refuses any tool name not in
  :data:`ALLOWED_TOOLS` before a byte is written. The upstream tools that reach
  the network, install software, run scripts or record the screen are not in it.
* **Bounded calls.** Every call has a deadline. A child that does not answer in
  time is killed (with its process group) and restarted on the next call; the
  caller gets :class:`OutcomeUnknown`.
* **Never replays an unknown outcome.** A crash or stall mid-call means the
  action may or may not have happened. The call is NOT retried; the caller is
  told to re-read the window. Only calls marked ``idempotent`` (pure reads) are
  retried once on a fresh child.
* **Restart with capped backoff**, re-running ``initialize`` and ``tools/list``
  on every new child. Crash loops stop after :data:`MAX_RESTARTS_IN_WINDOW`
  starts inside :data:`RESTART_WINDOW_SECS` until someone asks for a restart.
* **Clean shutdown, no orphan.** :meth:`stop` closes stdin, then kills the
  group through the ``Popen`` handle the supervisor owns. A gateway that dies
  without stopping leaves no driver behind either: the child's stdin is the
  gateway's pipe, and ``mcp`` exits when that pipe reaches end-of-file. No pid
  is ever killed by number.

The child's stdout is read on a dedicated thread (pipes cannot be polled on
Windows) and stderr is drained into a bounded ring for diagnostics. Neither the
arguments of a call nor its replies are logged: typed text and screen content
pass through here.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.computer_use.driver_runtime import env as driver_env_mod
from kiro_crew.computer_use.driver_runtime import install as install_mod

logger = logging.getLogger(__name__)

#: The upstream tools Kiro Crew may call. Everything else is unreachable.
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "list_apps",
        "list_windows",
        "get_window_state",
        "click",
        "drag",
        "type_text",
        "press_key",
        "hotkey",
        "set_value",
        "scroll",
        "check_permissions",
        "health_report",
        "get_screen_size",
    }
)

#: Upstream tools that are never callable, named so a test pins the exclusion.
FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "install_ffmpeg",
        "install_extension",
        "check_for_update",
        "run_script",
        "run_steps",
        "replay_trajectory",
        "page",
        "kill_app",
        "launch_app",
        "clipboard_read",
        "clipboard_write",
        "start_recording",
        "stop_recording",
        "set_config",
        "parse_visual_regions",
    }
)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "kiro-crew", "version": "1"}

DEFAULT_CALL_TIMEOUT_SECS = 30.0
START_TIMEOUT_SECS = 20.0
STOP_GRACE_SECS = 3.0
#: How long a child whose pipes just closed may take to finish exiting before a
#: discard counts as Kiro Crew killing it.
_EXIT_SETTLE_SECS = 0.3
BACKOFF_SECS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RESTART_WINDOW_SECS = 120.0
MAX_RESTARTS_IN_WINDOW = 6
MAX_LINE_BYTES = 32 * 1024 * 1024
STDERR_RING_LINES = 50

STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_UNAVAILABLE = "unavailable"
STATE_CRASH_LOOP = "crash_loop"


class DriverError(Exception):
    """A driver call that did not produce a result. The message is model-safe."""


class DriverUnavailable(DriverError):
    """The driver cannot be started (not installed, verification failed, crash loop)."""


class OutcomeUnknown(DriverError):
    """The child died or stalled mid-call: the action may or may not have happened."""


class ToolNotAllowed(DriverError):
    """A tool outside :data:`ALLOWED_TOOLS` was requested."""


@dataclass
class _Child:
    proc: "subprocess.Popen[bytes]"
    lines: "queue.Queue[bytes | None]"
    stderr: "collections.deque[str]"
    generation: int
    tools: frozenset[str] = frozenset()
    next_id: int = 0
    #: True once Kiro Crew itself killed this child (a timeout, a discard), so its
    #: exit by signal is never mistaken for the system blocking the launch.
    killed: bool = False


Spawner = Callable[[list[str], Mapping[str, str], str], "subprocess.Popen[bytes]"]


def _default_spawn(argv: list[str], env: Mapping[str, str], cwd: str) -> "subprocess.Popen[bytes]":
    kwargs: dict[str, Any] = {}
    if platform_compat.IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(  # noqa: S603 - argv is the verified pinned binary
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        **kwargs,
    )


def _pump_stdout(stream: Any, out: "queue.Queue[bytes | None]") -> None:
    try:
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                # Oversized reply: drop the rest of the line rather than buffer it.
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_LINE_BYTES)
                continue
            out.put(line)
    except (OSError, ValueError):
        pass
    finally:
        out.put(None)


def _pump_stderr(stream: Any, ring: "collections.deque[str]") -> None:
    try:
        for raw in iter(lambda: stream.readline(65536), b""):
            ring.append(raw.decode("utf-8", "replace").rstrip()[:500])
    except (OSError, ValueError):
        pass


class DriverSupervisor:
    """Owns at most one driver child. Thread-safe; every public method is blocking."""

    def __init__(
        self,
        home: "str | os.PathLike[str]",
        *,
        resolve_binary: "Callable[[], tuple[Path | None, str]] | None" = None,
        recheck_binary: "Callable[[Path], Path] | None" = None,
        spawn: "Spawner | None" = None,
        base_env: "Mapping[str, str] | None" = None,
        mcp_args: "tuple[str, ...]" = ("mcp",),
        mac_daemon: "Any | None" = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECS,
        start_timeout: float = START_TIMEOUT_SECS,
        backoff: "tuple[float, ...]" = BACKOFF_SECS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._home = Path(home)
        self._resolve_binary = resolve_binary or (lambda: install_mod.verify_installed(self._home))
        # Re-hash right before the exec (the start of a daemon can take seconds
        # after ``resolve_binary``). An injected resolver brings its own binary,
        # so it brings its own re-check too.
        if recheck_binary is None:
            recheck_binary = (
                (lambda _b: install_mod.exec_ready_binary(self._home))
                if resolve_binary is None
                else (lambda b: b)
            )
        self._recheck_binary = recheck_binary
        self._spawn = spawn or _default_spawn
        self._base_env = base_env
        self._mcp_args = tuple(mcp_args)
        # On macOS plain ``mcp`` would let upstream start an unfenced daemon, so
        # Kiro Crew starts a fenced one itself and the proxy only ever attaches
        # to it (see :mod:`.macos_daemon`). Made for the real installed driver;
        # a caller that injects its own binary (a test double) injects its own
        # daemon too.
        if (
            mac_daemon is None
            and resolve_binary is None
            and install_mod.current_os() == install_mod.OS_MACOS
        ):
            from kiro_crew.computer_use.driver_runtime.macos_daemon import MacDaemon

            mac_daemon = MacDaemon(self._home)
        self._mac = mac_daemon
        self._call_timeout = call_timeout
        self._start_timeout = start_timeout
        self._backoff = backoff
        self._sleep = sleep
        self._clock = clock
        self._call_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._child: "_Child | None" = None
        self._generation = 0
        self._starts: "collections.deque[float]" = collections.deque()
        self._failures = 0
        #: How the OS stopped the driver at its last start ("" when it did not).
        #: Latched: no automatic retry until an explicit restart.
        self._blocked = ""
        self._state = STATE_STOPPED
        self._last_error = ""

    # ── status ──

    @property
    def generation(self) -> int:
        """Increments on every new child; element tokens never cross generations."""
        return self._generation

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            child = self._child
            alive = child is not None and child.proc.poll() is None
            return {
                "state": STATE_RUNNING if alive else self._state,
                "pid": child.proc.pid if alive and child is not None else None,
                "generation": self._generation,
                "restarts": max(0, len(self._starts) - 1),
                "last_error": self._last_error,
                "blocked": self._blocked,
                "stderr_tail": list(child.stderr)[-5:] if child is not None else [],
            }

    def is_running(self) -> bool:
        child = self._child
        return child is not None and child.proc.poll() is None

    def available_tools(self) -> frozenset[str]:
        """The tools the current child listed at start (empty when none runs)."""
        child = self._child
        return child.tools if child is not None else frozenset()

    # ── lifecycle ──

    def ensure(self) -> None:
        """Start the child if it is not running. Raises :class:`DriverUnavailable`."""
        with self._call_lock:
            self._ensure_locked()

    def restart(self) -> None:
        """Stop the child and clear the crash-loop latch; the next call starts a new one.

        On macOS the fenced daemon is stopped too, so a permission granted since
        it started applies to the next one.
        """
        with self._call_lock:
            self._kill_child("restart requested")
            self._stop_mac_daemon()
            with self._state_lock:
                self._starts.clear()
                self._failures = 0
                self._state = STATE_STOPPED
                self._last_error = ""
                self._blocked = ""

    def stop(self) -> None:
        """Stop the child (gateway shutdown, before an in-place restart). Idempotent."""
        with self._call_lock:
            self._stop_child()
            with self._state_lock:
                self._state = STATE_STOPPED

    def call(
        self,
        tool: str,
        arguments: "Mapping[str, Any] | None" = None,
        *,
        timeout: "float | None" = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        """Call one upstream tool and return its MCP result object.

        Raises :class:`ToolNotAllowed` for a tool outside the allowlist,
        :class:`DriverUnavailable` when no child can run, and
        :class:`OutcomeUnknown` when the child died or stalled mid-call.
        """
        if tool not in ALLOWED_TOOLS:
            raise ToolNotAllowed(f"the driver tool {tool!r} is not available to Kiro Crew")
        args = dict(arguments or {})
        with self._call_lock:
            try:
                return self._call_locked(tool, args, timeout)
            except OutcomeUnknown:
                if not idempotent:
                    raise
            # A pure read may be repeated on a fresh child: it changes nothing.
            return self._call_locked(tool, args, timeout)

    # ── internals ──

    def _call_locked(
        self, tool: str, args: dict[str, Any], timeout: "float | None"
    ) -> dict[str, Any]:
        child = self._ensure_locked()
        reply = self._request(
            child, "tools/call", {"name": tool, "arguments": args}, timeout or self._call_timeout
        )
        if "error" in reply:
            err = reply.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise DriverError(f"the driver rejected the call: {message}")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise DriverError("the driver returned no result")
        with self._state_lock:
            self._failures = 0
        return result

    def _ensure_locked(self) -> _Child:
        child = self._child
        if child is not None and child.proc.poll() is None:
            return child
        if child is not None:
            self._discard(child, "the driver exited")
        with self._state_lock:
            if self._blocked:
                # Blocked at launch by the system: never retried by itself.
                raise DriverUnavailable(f"the desktop driver cannot start: {self._last_error}")
            now = self._clock()
            while self._starts and now - self._starts[0] > RESTART_WINDOW_SECS:
                self._starts.popleft()
            if self._state == STATE_CRASH_LOOP or len(self._starts) >= MAX_RESTARTS_IN_WINDOW:
                self._state = STATE_CRASH_LOOP
                raise DriverUnavailable(
                    "the desktop driver keeps exiting; it was stopped. Restart it from "
                    "Settings -> Computer Use, once the cause is fixed"
                    + (f" (last error: {self._last_error})" if self._last_error else "")
                )
            delay = (
                self._backoff[min(self._failures, len(self._backoff) - 1)]
                if self._failures
                else 0.0
            )
        if delay:
            self._sleep(delay)
        try:
            binary, reason = self._resolve_binary()
        except install_mod.DriverBlocked as exc:
            raise self._blocked_at_launch(exc.how, exc.detail) from exc
        if binary is None:
            with self._state_lock:
                self._state = STATE_UNAVAILABLE
                self._last_error = reason
            raise DriverUnavailable(
                f"the desktop driver cannot start: {reason}; check it in Settings -> Computer Use"
            )
        return self._start(binary)

    def _start(self, binary: Path) -> _Child:
        home = install_mod.private_home(self._home)
        env = driver_env_mod.driver_env(
            self._base_env if self._base_env is not None else os.environ, home
        )
        with self._state_lock:
            self._starts.append(self._clock())
            self._generation += 1
            generation = self._generation
        argv: list[str]
        daemon_pid: "int | None" = None
        if self._mac is not None:
            from kiro_crew.computer_use.driver_runtime.macos_daemon import DaemonError

            try:
                daemon_pid = self._mac.ensure()
            except DaemonError as exc:
                self._note_failure(str(exc))
                raise DriverUnavailable(f"the desktop driver cannot start: {exc}") from exc
            try:
                # The sealed copy, re-hashed now; /Applications is never executed.
                argv = self._mac.proxy_argv()
            except DaemonError as exc:
                self._note_failure(str(exc))
                raise DriverUnavailable(f"the desktop driver cannot start: {exc}") from exc
            env = self._mac.proxy_env(env)
        else:
            try:
                argv = [str(self._recheck_binary(binary)), *self._mcp_args]
            except install_mod.InstallError as exc:
                self._note_failure(exc.detail)
                raise DriverUnavailable(f"the desktop driver cannot start: {exc.detail}") from exc
        try:
            proc = self._spawn(argv, env, str(home))
        except OSError as exc:
            how = install_mod.blocked_how(error=exc)
            if how:
                raise self._blocked_at_launch(how) from exc
            self._note_failure(f"could not start the driver ({exc.strerror or exc})")
            raise DriverUnavailable(
                f"the desktop driver could not start ({exc.strerror or exc})"
            ) from exc
        lines: "queue.Queue[bytes | None]" = queue.Queue()
        ring: "collections.deque[str]" = collections.deque(maxlen=STDERR_RING_LINES)
        threading.Thread(
            target=_pump_stdout, args=(proc.stdout, lines), name="cu-driver-out", daemon=True
        ).start()
        if proc.stderr is not None:
            threading.Thread(
                target=_pump_stderr, args=(proc.stderr, ring), name="cu-driver-err", daemon=True
            ).start()
        child = _Child(proc=proc, lines=lines, stderr=ring, generation=generation)
        self._child = child
        try:
            init = self._request(
                child,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                self._start_timeout,
            )
            if not isinstance(init.get("result"), dict):
                raise DriverError("the driver did not complete the MCP handshake")
            self._notify(child, "notifications/initialized", {})
            listed = self._request(child, "tools/list", {}, self._start_timeout)
            tools = (listed.get("result") or {}).get("tools") or []
            child.tools = frozenset(t.get("name", "") for t in tools if isinstance(t, dict))
            if self._mac is not None and daemon_pid is not None:
                self._confirm_mac(daemon_pid)
        except DriverError as exc:
            with contextlib.suppress(Exception):
                child.proc.wait(timeout=1.0)
            exit_code = child.proc.poll()
            killed_by_us = child.killed
            time.sleep(0.1)  # let the stderr pump read the last line
            tail = [line for line in child.stderr if line.strip()][-1:]
            self._discard(child, str(exc))
            # Only a child that died by itself can have been blocked by the system:
            # our own timeout kill also ends in SIGKILL and must never read as one.
            how = (
                install_mod.blocked_how(exit_code)
                if exit_code is not None and not killed_by_us
                else None
            )
            if how:
                raise self._blocked_at_launch(how) from exc
            if exit_code is not None:
                # Died before it answered the handshake: no action was in flight.
                why = f"it exited during startup (exit code {exit_code})"
                exc = DriverError(f"{why}: {tail[0]}" if tail else why)
            self._note_failure(str(exc))
            raise DriverUnavailable(f"the desktop driver did not start: {exc}") from exc
        with self._state_lock:
            self._state = STATE_RUNNING
        with contextlib.suppress(OSError):
            install_mod.clear_blocked(self._home)
        return child

    def _blocked_at_launch(self, how: str, message: str = "") -> "DriverUnavailable":
        """Latch a launch the OS stopped (endpoint security software, policy): record
        it for setup and ``auto``, and return the refusal. Never retried by itself."""
        message = message or install_mod.blocked_message(how, install_mod.current_os())
        with self._state_lock:
            self._state = STATE_UNAVAILABLE
            self._last_error = message
            self._blocked = how
        with contextlib.suppress(OSError):
            install_mod.record_blocked(self._home, how)
        logger.warning("computer-use driver was stopped at launch (%s)", how)
        return DriverUnavailable(f"the desktop driver cannot start: {message}")

    def _note_failure(self, reason: str) -> None:
        with self._state_lock:
            self._failures += 1
            self._last_error = reason

    def _send(self, child: _Child, message: dict[str, Any], timeout: "float | None" = None) -> None:
        """Write one message; with *timeout*, a child that stops reading is killed.

        A payload larger than the pipe buffer blocks the write until the child
        reads. The watchdog kills the child's process tree when the write is
        not done by *timeout*, which closes the pipe and makes the write raise,
        so a stalled child can never hold :attr:`_call_lock` forever.
        """
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        stdin = child.proc.stdin
        if stdin is None:
            raise OutcomeUnknown("the desktop driver has no input pipe")
        fired = threading.Event()
        watchdog = None
        if timeout is not None:

            def _kill() -> None:
                fired.set()
                child.killed = True
                platform_compat.kill_popen_tree(child.proc)

            watchdog = threading.Timer(max(timeout, 0.0), _kill)
            watchdog.daemon = True
            watchdog.start()
        try:
            stdin.write(data)
            stdin.flush()
        finally:
            if watchdog is not None:
                watchdog.cancel()
        if fired.is_set():
            # The watchdog fired as the last byte left; the child is dead either way.
            raise OSError("the driver stopped reading its input")

    def _notify(self, child: _Child, method: str, params: dict[str, Any]) -> None:
        try:
            self._send(child, {"jsonrpc": "2.0", "method": method, "params": params})
        except (OSError, ValueError) as exc:
            raise DriverError(f"the driver closed its input ({exc})") from exc

    def _request(
        self, child: _Child, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        child.next_id += 1
        rid = child.next_id
        # The deadline covers delivery too: a child that stops reading stdin
        # must not block the write past it.
        deadline = self._clock() + timeout
        try:
            self._send(
                child,
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                timeout=timeout,
            )
        except (OSError, ValueError) as exc:
            self._discard(child, "the driver closed its input")
            self._note_failure("the driver closed its input")
            raise OutcomeUnknown(
                "the desktop driver stopped before the call was fully delivered and will "
                "be restarted; whether the action happened is unknown. Re-read the window "
                "with computer_get_state before acting again"
            ) from exc
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._discard(child, f"no reply within {timeout:.0f}s")
                self._note_failure(f"the driver stalled on {method}")
                raise OutcomeUnknown(
                    f"the desktop driver did not answer within {timeout:.0f}s and was "
                    "restarted; whether the action happened is unknown. Re-read the window "
                    "with computer_get_state before acting again"
                )
            try:
                line = child.lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                self._discard(child, "the driver exited mid-call")
                self._note_failure("the driver exited mid-call")
                raise OutcomeUnknown(
                    "the desktop driver exited during the call and will be restarted; "
                    "whether the action happened is unknown. Re-read the window with "
                    "computer_get_state before acting again"
                )
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == rid:
                return message
            # Notifications and replies to calls that already timed out are dropped.

    def _discard(self, child: _Child, reason: str) -> None:
        if child.proc.poll() is None:
            # A child that just closed its pipes may still be exiting: give it a
            # moment, so only a child that is really still running counts as killed
            # by us (and a block by the system is not hidden behind our own kill).
            try:
                child.proc.wait(timeout=_EXIT_SETTLE_SECS)
            except subprocess.TimeoutExpired:
                child.killed = True
        platform_compat.kill_popen_tree(child.proc)
        with contextlib.suppress(Exception):
            child.proc.wait(timeout=STOP_GRACE_SECS)
        for stream in (child.proc.stdin, child.proc.stdout, child.proc.stderr):
            with contextlib.suppress(Exception):
                if stream is not None:
                    stream.close()
        if self._child is child:
            self._child = None
        logger.info("computer-use driver child %s discarded: %s", child.proc.pid, reason)

    def _kill_child(self, reason: str) -> None:
        child = self._child
        if child is not None:
            self._discard(child, reason)

    def _stop_child(self) -> None:
        child = self._child
        if child is None:
            self._stop_mac_daemon()
            return
        with contextlib.suppress(Exception):
            if child.proc.stdin is not None:
                child.proc.stdin.close()
        with contextlib.suppress(Exception):
            child.proc.wait(timeout=STOP_GRACE_SECS)
        self._discard(child, "stopped")
        self._stop_mac_daemon()

    def _confirm_mac(self, pid: int) -> None:
        """The proxy is connected: the socket must still be served by the proved pid."""
        from kiro_crew.computer_use.driver_runtime.macos_daemon import DaemonError

        mac = self._mac
        if mac is None:
            return
        try:
            mac.confirm(pid)
        except DaemonError as exc:
            self._stop_mac_daemon()
            raise DriverError(str(exc)) from exc

    def _stop_mac_daemon(self) -> None:
        if self._mac is None:
            return
        try:
            self._mac.stop()
        except Exception:  # noqa: BLE001 - stopping must never raise into shutdown
            logger.warning("computer-use driver daemon stop failed", exc_info=True)


# ── one per gateway ──

_shared: "DriverSupervisor | None" = None
_shared_lock = threading.Lock()


def get_supervisor(home: "str | os.PathLike[str] | None" = None) -> DriverSupervisor:
    """The process-wide supervisor for *home* (the data home when omitted)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            if home is None:
                from kiro_crew.config.paths import data_home

                home = data_home()
            _shared = DriverSupervisor(home)
        return _shared


def stop_shared() -> None:
    """Stop the shared child, if any (gateway shutdown and pre-exec)."""
    with _shared_lock:
        sup = _shared
    if sup is not None:
        sup.stop()


def reset_shared() -> None:
    """Stop and forget the shared supervisor (tests, data-home change)."""
    global _shared
    with _shared_lock:
        sup, _shared = _shared, None
    if sup is not None:
        sup.stop()


__all__ = [
    "ALLOWED_TOOLS",
    "DriverError",
    "DriverSupervisor",
    "DriverUnavailable",
    "FORBIDDEN_TOOLS",
    "OutcomeUnknown",
    "ToolNotAllowed",
    "get_supervisor",
    "reset_shared",
    "stop_shared",
]
