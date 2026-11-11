"""The macOS driver daemon, started by Kiro Crew inside the env fence.

On macOS upstream's ``cua-driver mcp`` is only a proxy: with no daemon
listening it starts ``/Applications/CuaDriver.app`` through LaunchServices
(``/usr/bin/open ... --args serve``) and passes no environment. The fence in
:mod:`.env` (telemetry off, update check off, a private home) would then never
reach the process that does the work, and that process would share upstream's
per-user socket with any other tool. So on macOS Kiro Crew never lets the proxy
start anything:

1. **Launch** the daemon itself: ``/usr/bin/open -n -g --env K=V ...
   /Applications/CuaDriver.app --args serve --socket <private> --pid-file
   <private>``, with exactly the fence variables. LaunchServices still starts
   the app, so macOS attributes the permissions to CuaDriver.
2. **Prove the fence** before any use. The daemon at the pin reports no
   telemetry or home state over its socket, so the proof is read from the
   kernel's record of how the process was started (``KERN_PROCARGS2``): the
   executable must be the verified ``/Applications`` copy, its argv must be
   ``serve`` on OUR socket, and every fence variable must hold our value.
   Anything that cannot be proved stops the daemon and refuses. No fallback.
3. **Connect** only with ``cua-driver mcp --socket <private>``, never plain
   ``mcp``. The proxy runs with ``CUA_DRIVER_EMBEDDED=1``, which makes upstream
   refuse to auto-launch a daemon when the socket is down, so a daemon that died
   can never be replaced by an unfenced one.
4. **Stop** only our own daemon: ``cua-driver stop --socket <private>
   --expected-pid <pid>``, and only for a pid whose argv names our socket. A
   daemon that ignores it is never killed by pid: Kiro Crew refuses to start
   another. A stale socket or pid file is reclaimed at startup.

Nothing in ``/Applications`` is executed directly. The user can write there, and
so can a sandboxed agent, so a check followed by an exec of that path would race
a swap. ``stop`` and the ``mcp`` proxy run the copy in the sealed runtime tree
(``install.exec_ready_binary``, hashed against the pin right before each run);
the app itself is only started through LaunchServices, right after
``install.check_app_copy`` re-proves its signature and pinned hash.

The socket and pid file live in ``<data-home>/computer-use-ipc/run``, a HIDDEN
sandbox leaf (as ``browser-harness-ipc``): whoever can connect to the socket can
call every upstream tool, outside Kiro Crew's allowlist and floors, so no
sandboxed process may even see it. A data home too deep for the socket path is
refused, never served from an unmasked directory.

Every external effect (``open``, the stop command, the kernel read, the socket
probe) is injectable, so the unit tests drive all of it with fakes.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import logging
import os
import socket as socket_mod
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import owner_only_files, platform_compat
from kiro_crew.computer_use.driver_runtime import env as driver_env_mod
from kiro_crew.computer_use.driver_runtime import install as install_mod

logger = logging.getLogger(__name__)

OPEN = "/usr/bin/open"
NO_PERMISSIONS_GATE = "--no-permissions-gate"
#: The hidden crew-home leaf holding the daemon's socket and pid file
#: (``sandbox_leaves`` masks it; ``sandbox_precreate`` creates it empty at 0700).
IPC_LEAF = "computer-use-ipc"
RUN_DIR_NAME = "run"
SOCKET_NAME = "d.sock"
PID_NAME = "d.pid"
#: macOS ``sockaddr_un.sun_path`` is 104 bytes including the terminating NUL.
MAX_SOCKET_PATH_BYTES = 103
#: Set on the ``mcp`` proxy only: upstream then refuses to auto-launch a daemon.
EMBEDDED_ENV = "CUA_DRIVER_EMBEDDED"
START_TIMEOUT_SECS = 20.0
STOP_TIMEOUT_SECS = 10.0
_POLL_SECS = 0.1
_TOOL_TIMEOUT_SECS = 20.0

ERR_NO_OPEN_ENV = (
    "this version of macOS cannot pass a private environment to an app it starts "
    "(/usr/bin/open has no --env), so the driver cannot be started inside Kiro Crew's "
    "privacy settings; the upstream driver needs a newer macOS"
)
ERR_LAUNCH = "the driver app could not be started ({detail})"
ERR_NO_DAEMON = "the driver app did not start its service within {secs:.0f}s"
ERR_UNPROVEN = (
    "the driver service started, but Kiro Crew could not prove it runs with telemetry "
    "and update checks off and its private home ({detail}); it was stopped"
)
ERR_SOCKET_TOO_LONG = (
    "the driver's private socket path {path} is longer than macOS allows; "
    "use a shorter Kiro Crew data folder"
)
ERR_UNVERIFIED_APP = "{app} failed its checks ({why}); it was not started"
ERR_UNVERIFIED_BINARY = "the driver could not be run: {why}"
ERR_STOP_FAILED = (
    "the driver service (pid {pid}) did not stop, so a new one was not started; "
    "quit CuaDriver and try again"
)
ERR_SUBSTITUTED = (
    "the driver's private socket at {path} is no longer served by the service Kiro "
    "Crew started and checked; the driver was stopped"
)
ERR_FOREIGN_SOCKET = (
    "another process holds the driver's private socket at {path}; it was left alone "
    "and the driver was not started"
)


class DaemonError(Exception):
    """The fenced daemon cannot be used. The message is model-safe."""


@dataclass(frozen=True)
class ProcArgs:
    """How a process was started, as the kernel recorded it."""

    exec_path: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    #: Names that appear more than once; ``env`` keeps the FIRST, as ``getenv`` does.
    duplicates: frozenset[str] = frozenset()


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_run(
    argv: list[str], *, timeout: float, env: Mapping[str, str]
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - fixed system tool or the verified pinned binary
        argv,
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


# ── the kernel's record of a process ──


def parse_procargs2(buf: bytes) -> ProcArgs:
    """Parse a ``KERN_PROCARGS2`` buffer: argc, exec path, NUL padding, argv, env."""
    if len(buf) < 4:
        raise ValueError("short procargs buffer")
    argc = int.from_bytes(buf[:4], "little")
    rest = buf[4:]
    end = rest.find(b"\0")
    if end < 0:
        raise ValueError("no executable path")
    exec_path = rest[:end].decode("utf-8", "replace")
    pos = end
    while pos < len(rest) and rest[pos] == 0:
        pos += 1
    strings = rest[pos:].split(b"\0")
    if len(strings) < argc:
        raise ValueError("truncated argv")
    argv = tuple(s.decode("utf-8", "replace") for s in strings[:argc])
    env: dict[str, str] = {}
    duplicates: set[str] = set()
    for raw in strings[argc:]:
        if not raw:
            break
        key, sep, value = raw.decode("utf-8", "replace").partition("=")
        if not sep:
            continue
        if key in env:
            duplicates.add(key)
            continue
        env[key] = value
    return ProcArgs(exec_path=exec_path, argv=argv, env=env, duplicates=frozenset(duplicates))


def read_procargs(pid: int) -> "ProcArgs | None":
    """The kernel's ``KERN_PROCARGS2`` for *pid* (same user only), or ``None``."""
    if not platform_compat.IS_MACOS:
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        libc.sysctl.restype = ctypes.c_int
        ctl_kern, kern_argmax, kern_procargs2 = 1, 8, 49
        size = ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        argmax = ctypes.c_int(0)
        mib = (ctypes.c_int * 2)(ctl_kern, kern_argmax)
        if libc.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
            return None
        buf = ctypes.create_string_buffer(max(argmax.value, 4096))
        size = ctypes.c_size_t(len(buf))
        mib3 = (ctypes.c_int * 3)(ctl_kern, kern_procargs2, int(pid))
        if libc.sysctl(mib3, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        return parse_procargs2(buf.raw[: size.value])
    except (OSError, ValueError, AttributeError):
        logger.debug("KERN_PROCARGS2 read failed for %s", pid, exc_info=True)
        return None


SOCK_ABSENT = "absent"
SOCK_STALE = "stale"
SOCK_LIVE = "live"
SOCK_UNKNOWN = "unknown"

#: ``getsockopt`` level and option for a Unix socket's peer pid on macOS.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 0x002


def _peer_pid(sock: "socket_mod.socket") -> "int | None":
    """The pid of the process serving the other end of a connected Unix socket."""
    try:
        if platform_compat.IS_MACOS:
            raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4)
            return int.from_bytes(raw, "little") or None
        so_peercred = getattr(socket_mod, "SO_PEERCRED", None)
        if so_peercred is None:
            return None
        raw = sock.getsockopt(socket_mod.SOL_SOCKET, so_peercred, 12)
        return int.from_bytes(raw[:4], "little") or None
    except OSError:
        return None


def probe_socket(path: Path) -> "tuple[str, int | None]":
    """``(state, peer pid)`` of the Unix socket at *path*.

    ``absent``: nothing there. ``stale``: a socket file nobody serves (the
    connect was refused). ``live``: a server accepted, with its pid when the
    kernel reports it. ``unknown``: anything else (a timeout, a full backlog, a
    non-socket file); callers treat it as "someone else's" and never unlink it.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return SOCK_ABSENT, None
    except OSError:
        return SOCK_UNKNOWN, None
    if not stat.S_ISSOCK(info.st_mode):
        return SOCK_UNKNOWN, None
    sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    try:
        sock.settimeout(1.0)
        sock.connect(str(path))
    except ConnectionRefusedError:
        return SOCK_STALE, None
    except OSError:
        return SOCK_UNKNOWN, None
    else:
        return SOCK_LIVE, _peer_pid(sock)
    finally:
        sock.close()


# ── private paths ──


def _private_dir(path: Path) -> Path:
    """Create *path* as a 0700 directory we own, refusing a link or a foreign one."""
    owner_only_files.mkdirs_owner_only(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DaemonError(f"{path} is not a private directory; the driver was not started")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise DaemonError(f"{path} belongs to another user; the driver was not started")
    platform_compat.chmod_safe(path, 0o700)
    return path


def run_dir(home: "str | os.PathLike[str]") -> Path:
    """Where the private socket and pid file live: ``<data-home>/computer-use-ipc/run``.

    Inside the hidden IPC leaf, always. There is no fallback: a socket anywhere
    else would sit outside every sandbox mask, so a path too long for the socket
    is refused at start (:meth:`MacDaemon.ensure`).
    """
    return Path(home) / IPC_LEAF / RUN_DIR_NAME


def fence_env(home: "str | os.PathLike[str]") -> dict[str, str]:
    """Exactly the fence variables the daemon must run with."""
    private = str(install_mod.private_home(home))
    return {
        **driver_env_mod.FORCED_ENV,
        driver_env_mod.HOME_ENV: private,
        driver_env_mod.SHARED_HOME_ENV: private,
    }


class MacDaemon:
    """The one fenced driver daemon this gateway started. Not thread-safe on
    its own: the supervisor calls it under its call lock."""

    def __init__(
        self,
        home: "str | os.PathLike[str]",
        *,
        app: "Path | None" = None,
        run: "Runner | None" = None,
        procargs: "Callable[[int], ProcArgs | None]" = read_procargs,
        probe: "Callable[[Path], tuple[str, int | None]]" = probe_socket,
        pid_alive: "Callable[[int], bool] | None" = None,
        driver_binary: "Callable[[], Path] | None" = None,
        verify_app: "Callable[[], str] | None" = None,
        ipc_dir: "Path | None" = None,
        start_timeout: float = START_TIMEOUT_SECS,
        stop_timeout: float = STOP_TIMEOUT_SECS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._home = Path(home)
        self._app = app
        self._run = run or _default_run
        self._procargs = procargs
        self._probe = probe
        self._pid_alive = pid_alive or platform_compat.pid_exists
        self._driver_binary = driver_binary or (lambda: install_mod.exec_ready_binary(self._home))
        self._verify_app = verify_app or self._check_app
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._sleep = sleep
        self._clock = clock
        # ``ipc_dir`` is for tests (a short directory); production always uses
        # the hidden leaf under the data home.
        self._dir = ipc_dir if ipc_dir is not None else run_dir(self._home)

    # ── paths ──

    @property
    def app(self) -> Path:
        return self._app if self._app is not None else install_mod.applications_app()

    @property
    def socket_path(self) -> Path:
        return self._dir / SOCKET_NAME

    @property
    def pid_path(self) -> Path:
        return self._dir / PID_NAME

    def app_binary(self) -> Path:
        """The /Applications copy's binary: compared against the kernel's record of
        the daemon, NEVER executed (see :meth:`_driver_command`)."""
        return self.app / install_mod.MACOS_APP_BINARY

    def _check_app(self) -> str:
        pinned = install_mod.asset_for(install_mod.OS_MACOS)
        if pinned is None:
            return "there is no pinned macOS driver"
        return install_mod.check_app_copy(self.app, pinned, self._home, check_running_version=False)

    def _driver_command(self, *args: str) -> list[str]:
        """The ONLY way this module builds a driver command it executes: the sealed,
        just-re-hashed copy (``install.exec_ready_binary``), never /Applications."""
        try:
            binary = self._driver_binary()
        except install_mod.InstallError as exc:
            raise DaemonError(ERR_UNVERIFIED_BINARY.format(why=exc.detail)) from exc
        return [str(binary), *args]

    # ── argv ──

    def launch_argv(self) -> list[str]:
        """The exact ``open`` command: fence variables, then the app, then serve."""
        argv = [OPEN, "-n", "-g"]
        for key, value in sorted(fence_env(self._home).items()):
            argv += ["--env", f"{key}={value}"]
        argv += [
            str(self.app),
            "--args",
            "serve",
            "--socket",
            str(self.socket_path),
            "--pid-file",
            str(self.pid_path),
            # Upstream's own startup gate opens System Settings on every daemon
            # start until both grants exist. Kiro Crew prompts only when the
            # owner clicks in the setup dialog (``request_permissions`` or an
            # Open Settings button), so the gate is always off here.
            NO_PERMISSIONS_GATE,
        ]
        return argv

    def command_env(self) -> dict[str, str]:
        """The environment for every command Kiro Crew runs here (``open``, ``stop``):
        the same fence as the driver, so not even a finite ``stop`` reports home."""
        return driver_env_mod.driver_env(os.environ, install_mod.private_home(self._home))

    def proxy_argv(self) -> list[str]:
        """The proxy command, from the sealed verified copy (never /Applications)."""
        return self._driver_command(*self.mcp_args())

    def mcp_args(self) -> tuple[str, ...]:
        """The only proxy invocation used on macOS: always on our private socket."""
        return ("mcp", "--socket", str(self.socket_path))

    def proxy_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """The proxy's environment: the fence plus "never auto-launch a daemon"."""
        return {**env, EMBEDDED_ENV: "1"}

    # ── lifecycle ──

    def open_supports_env(self) -> bool:
        try:
            res = self._run([OPEN, "-h"], timeout=_TOOL_TIMEOUT_SECS, env=self.command_env())
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "--env" in f"{res.stdout or ''}\n{res.stderr or ''}"

    def ensure(self) -> int:
        """A running, fence-proved daemon of ours; returns its pid or raises."""
        # Measured on the resolved path, which is the one the daemon binds.
        real = Path(os.path.realpath(self._dir)) / SOCKET_NAME
        if len(os.fsencode(str(real))) > MAX_SOCKET_PATH_BYTES:
            raise DaemonError(ERR_SOCKET_TOO_LONG.format(path=real))
        _private_dir(self._dir.parent)
        _private_dir(self._dir)
        pid = self._read_pid()
        if pid is not None and self._pid_alive(pid) and self._is_ours(pid):
            why = self._fence_problem(pid)
            if why is None and self._serves(pid):
                return pid
            # Ours, but unproven or not serving: stop it and start clean.
            self._stop_pid(pid)
        self._reclaim()
        if not self.open_supports_env():
            raise DaemonError(ERR_NO_OPEN_ENV)
        # The sealed copy is what stops the daemon later: never start one that
        # Kiro Crew could not stop (raises DaemonError when it fails its hash).
        self._driver_command("stop")
        # Right before LaunchServices starts it: signature, team, bundle id and the
        # pinned hash of the /Applications copy, every launch.
        why = self._verify_app()
        if why:
            raise DaemonError(ERR_UNVERIFIED_APP.format(app=self.app, why=why))
        try:
            res = self._run(self.launch_argv(), timeout=_TOOL_TIMEOUT_SECS, env=self.command_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DaemonError(ERR_LAUNCH.format(detail=exc)) from exc
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or f"exit code {res.returncode}").strip()
            raise DaemonError(ERR_LAUNCH.format(detail=detail[:300]))
        deadline = self._clock() + self._start_timeout
        pid = None
        while self._clock() < deadline:
            pid = self._read_pid()
            if pid is not None and self._pid_alive(pid) and self._serves(pid):
                break
            self._sleep(_POLL_SECS)
        else:
            if pid is not None and self._pid_alive(pid) and self._is_ours(pid):
                self._stop_pid(pid)
            raise DaemonError(ERR_NO_DAEMON.format(secs=self._start_timeout))
        assert pid is not None  # the loop only breaks with a live pid
        why = self._fence_problem(pid)
        if why is not None:
            if self._is_ours(pid):
                self._stop_pid(pid)
            raise DaemonError(ERR_UNPROVEN.format(detail=why))
        return pid

    def stop(self) -> None:
        """Stop our daemon, if one runs. Never touches a process that is not ours."""
        pid = self._read_pid()
        if pid is not None and self._pid_alive(pid) and self._is_ours(pid):
            self._stop_pid(pid)
        self._reclaim()

    # ── proof ──

    def _serves(self, pid: int) -> bool:
        """True when the process answering on OUR socket is exactly *pid*."""
        state, peer = self._probe(self.socket_path)
        return state == SOCK_LIVE and peer == pid

    def confirm(self, pid: int) -> None:
        """Re-check, after the proxy connected, that our socket is still served by
        the proved daemon, so a substituted endpoint is caught before any call."""
        if not (self._pid_alive(pid) and self._serves(pid)):
            raise DaemonError(ERR_SUBSTITUTED.format(path=self.socket_path))

    def _is_ours(self, pid: int) -> bool:
        """True when *pid* is a ``serve`` on OUR socket (so pid reuse cannot fool us)."""
        args = self._procargs(pid)
        if args is None:
            return False
        argv = list(args.argv)
        try:
            i = argv.index("--socket")
        except ValueError:
            return False
        return "serve" in argv and i + 1 < len(argv) and argv[i + 1] == str(self.socket_path)

    def _fence_problem(self, pid: int) -> "str | None":
        """``None`` when the daemon provably runs inside the fence, else why not."""
        args = self._procargs(pid)
        if args is None:
            return "its start record cannot be read"
        expected_exe = os.path.realpath(self.app_binary())
        if os.path.realpath(args.exec_path) != expected_exe:
            return f"it is not the verified {self.app}"
        if not self._is_ours(pid):
            return "it is not serving on Kiro Crew's private socket"
        fence = fence_env(self._home)
        for key, value in fence.items():
            if key in args.duplicates:
                return f"{key} is set more than once"
            if args.env.get(key) != value:
                return f"{key} is not set to Kiro Crew's value"
        # The macOS launch environment is inherited too (``launchctl setenv``); any
        # other driver setting there could reopen what the fence closes.
        extra = sorted(k for k in args.env if k.startswith("CUA_") and k not in fence)
        if extra:
            return f"it also received {', '.join(extra)}"
        return None

    # ── internals ──

    def _read_pid(self) -> "int | None":
        try:
            info = self.pid_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return None
            text = self.pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not (text.isascii() and text.isdigit()):
            return None
        pid = int(text)
        return pid if pid > 0 else None

    def _stop_pid(self, pid: int) -> None:
        argv = self._driver_command(
            "stop", "--socket", str(self.socket_path), "--expected-pid", str(pid)
        )
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self._run(argv, timeout=_TOOL_TIMEOUT_SECS, env=self.command_env())
        if self._wait_gone(pid):
            return
        # The socket stop did not reach it. Kiro Crew never kills a pid by
        # number; it refuses instead, so no second, untracked daemon starts.
        logger.warning("computer-use driver daemon %s did not stop when asked", pid)
        raise DaemonError(ERR_STOP_FAILED.format(pid=pid))

    def _wait_gone(self, pid: int) -> bool:
        deadline = self._clock() + self._stop_timeout
        while self._pid_alive(pid) and self._clock() < deadline:
            self._sleep(_POLL_SECS)
        return not self._pid_alive(pid)

    def _reclaim(self) -> None:
        """Remove a stale pid file and socket; refuse a socket someone else serves."""
        pid = self._read_pid()
        if pid is not None and self._pid_alive(pid) and self._is_ours(pid):
            return
        with contextlib.suppress(FileNotFoundError):
            self.pid_path.unlink()
        state, _peer = self._probe(self.socket_path)
        if state in (SOCK_LIVE, SOCK_UNKNOWN):
            # Served by someone else, or not provably dead: never unlink it.
            raise DaemonError(ERR_FOREIGN_SOCKET.format(path=self.socket_path))
        if state == SOCK_STALE:
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()

    def describe(self) -> dict[str, Any]:
        return {"socket": str(self.socket_path), "pid_file": str(self.pid_path)}


__all__ = [
    "EMBEDDED_ENV",
    "DaemonError",
    "MacDaemon",
    "ProcArgs",
    "fence_env",
    "parse_procargs2",
    "read_procargs",
    "IPC_LEAF",
    "run_dir",
    "probe_socket",
]
