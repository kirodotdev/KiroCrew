"""The fixed launcher KAS starts in place of an MCP server whose env stayed local.

``python -P -m kiro_crew.kas_env_shim --session S --agent A --server N --version V``

The gateway's projection (:mod:`kiro_crew.acp.kas_env_launch`) wrote one launch
snapshot for that session's server and gave the engine this command instead of the
server's own. This module turns the reference back into the launch: it derives the
snapshot's file name from the reference (a caller never names a path), opens it
without following a link, refuses one that is not owner-only, checks that it names
the same session, agent, server and version, and execs the recorded command with
the recorded env values added. Any mismatch, or a snapshot it cannot read, ends the
launch with one line on stderr that names the reason; the server is never started
with a partial environment.

A snapshot that records a ``url`` instead of a command is a remote server whose
``headers`` stayed local. The launcher then does not exec anything: it becomes
the stdio server itself and relays each message to that url with the recorded
headers added (:mod:`kiro_crew.kas_http_bridge`).

Standard library only and at the top of the package: the engine starts this once
per server, so it must not import the ACP stack. The bridge path adds the
package's trust bootstrap (:mod:`kiro_crew._ssl_compat`) and, once a request
fails, :mod:`kiro_crew.asset_downloader`. The Windows job layout comes from
:mod:`kiro_crew.platform_compat_native`, itself standard library only, and a
Windows launch reads its engine's pid through the ``platform_compat`` owners. It prints
no env value.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import Any, NoReturn

from kiro_crew.bounded_read import bounded_read
from kiro_crew.platform_compat_native import _JobObjectExtendedLimitInformation

#: The crew data-home leaf the snapshots live in.
SNAPSHOT_LEAF = "kas-mcp-launch"
#: The leaf Crew-owned Connection bridge snapshots live in: a connection slug, the
#: gateway port and a per-bridge token, no vendor credential. Unlike
#: :data:`SNAPSHOT_LEAF` an enforced harness's child may read it.
BRIDGE_LEAF = "crew-oauth-bridge"
#: The module KAS runs (``python -m``).
SHIM_MODULE = "kiro_crew.kas_env_shim"
#: The key and value that mark a file as a launch snapshot of this format.
SNAPSHOT_MARKER = "kirocrew_kas_env_launch"
SNAPSHOT_FORMAT = 1
#: The env key the projection uses to tell the launcher where the data home is.
_HOME_ENV = "KIROCREW_HOME"
#: ``EX_CONFIG``: the launch's configuration is unusable.
_EXIT_REFUSED = 78
_MAX_SNAPSHOT_BYTES = 1 << 20
#: The writer's own Windows budget (``atomic_write``'s replace retry), copied so
#: this launcher stays standard library only.
_OPEN_ATTEMPTS = 10
_OPEN_BACKOFF_SECONDS = 0.05
#: ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK``: the job
#: ends its members when its last handle closes, and a server that asks for
#: ``CREATE_BREAKAWAY_FROM_JOB`` explicitly may still detach a process.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
_CHILD_JOB_LIMITS = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
#: ``JobObjectExtendedLimitInformation``, the information class of the struct.
_JOB_EXTENDED_LIMITS = 9
_SYNCHRONIZE = 0x00100000
_INFINITE = 0xFFFFFFFF
#: What ``OpenProcess`` reports for a pid no process holds: the parent is gone.
_ERROR_INVALID_PARAMETER = 87
#: The status a launcher ends with once the process that started it has gone.
_EXIT_PARENT_GONE = 1


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]


def snapshot_name(session_key: str, agent: str, server: str, version: str) -> str:
    """The snapshot file name for one write of one session's server.

    A Connection bridge's *version* is random per write, so two of its
    projections never share a file. A stdio launch or a header bridge is shared
    by every session of its agent (``kas_env_launch.SHARED_SESSION``), and its
    version is keyed to the file's content, so they all read one file. Every
    part is hashed, so no reference can name a path outside the leaf.
    """
    return (
        "-".join(
            (
                _digest("session", session_key),
                _digest("agent", agent),
                _digest("server", server),
                _digest("version", version),
            )
        )
        + ".json"
    )


def _refuse(server: str, reason: str) -> NoReturn:
    print(
        f"kirocrew: MCP server {server!r} was not started: {reason}. Start a new session "
        "or resume this one so Kiro Crew writes its launch again.",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(_EXIT_REFUSED)


def _open_snapshot(path: str, flags: int) -> int:
    """``os.open``, retried on Windows while the gateway replaces the file.

    Windows refuses an open with ``PermissionError`` during a replace, and the
    gateway rewrites a shared snapshot on every projection there.
    """
    for _ in range(_OPEN_ATTEMPTS - 1):
        try:
            return os.open(path, flags)
        except PermissionError:
            if os.name != "nt":
                raise
            time.sleep(_OPEN_BACKOFF_SECONDS)
    return os.open(path, flags)


def _read_snapshot(path: str, server: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        if stat.S_ISLNK(os.lstat(os.path.dirname(path)).st_mode):
            _refuse(server, "its launch directory is a link")
        fd = _open_snapshot(path, flags)
    except FileNotFoundError:
        _refuse(
            server,
            "its launch snapshot is missing (the session ended, or Kiro Crew restarted)",
        )
    except OSError as exc:
        _refuse(server, f"its launch snapshot cannot be opened ({exc.strerror or exc})")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _refuse(server, "its launch snapshot is not a regular file")
        if os.name == "posix":
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                _refuse(server, "its launch snapshot is not owner-only")
        if info.st_size > _MAX_SNAPSHOT_BYTES:
            _refuse(server, "its launch snapshot is too large")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = bounded_read(fh, _MAX_SNAPSHOT_BYTES + 1)
    finally:
        if fd != -1:
            os.close(fd)
    try:
        doc = json.loads(raw)
    except ValueError:
        _refuse(server, "its launch snapshot is not valid JSON")
    if not isinstance(doc, dict):
        _refuse(server, "its launch snapshot has the wrong shape")
    return doc


def resolve_launch(argv: list[str], environ: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """The verified ``(argv, env)`` to exec for this reference, or a refusal."""
    server, doc, environ = _resolve_snapshot(argv, environ)
    return _stdio_launch(server, doc, environ)


def resolve_bridge(server: str, doc: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """The verified ``(url, headers)`` of a remote server's snapshot, or a refusal."""
    url, headers = doc.get("url"), doc.get("headers")
    if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
        _refuse(server, "its launch snapshot names no http(s) url")
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        _refuse(server, "its launch snapshot has malformed headers")
    return url, headers


def _resolve_snapshot(
    argv: list[str], environ: dict[str, str]
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """``(server, snapshot, environ)`` once the snapshot matches the reference."""
    parser = argparse.ArgumentParser(prog=SHIM_MODULE, add_help=False)
    for flag in ("--session", "--agent", "--server", "--version"):
        parser.add_argument(flag, required=True)
    try:
        ns = parser.parse_args(argv)
    except SystemExit:
        _refuse("?", "the launcher reference is malformed")
    server = ns.server
    home = environ.pop(_HOME_ENV, "")
    if not home or not os.path.isabs(home):
        _refuse(server, "the launcher was not told where Kiro Crew's data home is")
    name = snapshot_name(ns.session, ns.agent, server, ns.version)
    # A Connection bridge's snapshot lives in its own leaf, the one an enforced
    # harness's child may read; it may only ever name a Connection.
    bridge = os.path.join(home, BRIDGE_LEAF, name)
    doc = _read_snapshot(
        bridge if os.path.lexists(bridge) else os.path.join(home, SNAPSHOT_LEAF, name), server
    )
    if os.path.lexists(bridge) and "connection" not in doc:
        _refuse(server, "a Connection bridge snapshot names no Connection")
    if doc.get(SNAPSHOT_MARKER) != SNAPSHOT_FORMAT:
        _refuse(server, "its launch snapshot has an unknown format")
    for key in ("session", "agent", "server", "version"):
        if doc.get(key) != getattr(ns, key):
            _refuse(server, f"its launch snapshot belongs to a different {key}")
    return server, doc, environ


def _stdio_launch(
    server: str, doc: dict[str, Any], environ: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    command, args, env = doc.get("command"), doc.get("args"), doc.get("env")
    if not isinstance(command, str) or not command.strip():
        _refuse(server, "its launch snapshot names no command")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        _refuse(server, "its launch snapshot has malformed arguments")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        _refuse(server, "its launch snapshot has a malformed environment")
    return [command, *args], {**environ, **env}


def _unstartable(command: str, exc: OSError) -> NoReturn:
    """End the launch with the one line both platforms print when the command won't start."""
    reason = exc.strerror or exc
    print(
        f"kirocrew: MCP server command {command!r} could not be started: {reason}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(127) from None


def windows_child_command(command: str, env: dict[str, str]) -> str | None:
    """The file a Windows launcher should start for *command*, or ``None``.

    A bare name is looked up only in the absolute entries of *env*'s ``PATH``, by
    full path: on Windows a bare-name ``shutil.which`` searches the current
    directory first, and so does ``CreateProcess``, so neither is given a bare
    name. ``PATHEXT`` still applies. A command with a directory part is used as
    given. ``None`` means nothing was found, and the caller must not start it.
    """
    if os.path.dirname(command):
        return shutil.which(command) or command
    for entry in env.get("PATH", "").split(os.pathsep):
        found = shutil.which(os.path.join(entry, command)) if os.path.isabs(entry) else None
        if found:
            return found
    return None


def _on_windows() -> bool:
    return os.name == "nt"


def _kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


def _last_error() -> int:
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


def _exit_process(code: int) -> None:
    os._exit(code)


def windows_bind_children_to_self() -> int | None:
    """Put this process in a Windows job that ends its children when it ends.

    A Windows launcher runs the server as a child, and a launcher that is
    terminated (its engine exited, or was killed) runs no cleanup, so without
    this its server tree outlives it with no parent. Every process this one
    starts afterwards joins the job, and the job closes with this process's
    handle to it, which the kernel closes however the process ends. Called
    before the child is started, so no child can escape it. Windows does not end
    a child when its parent exits, so the launcher also ends itself, and with it
    the job, once the process that started it is gone
    (:func:`_windows_exit_with_parent`).

    Returns the job handle, held for the life of the process, or ``None`` off
    Windows or when the job cannot be made; the server then starts without it.
    """
    if not _on_windows():
        return None
    try:
        kernel32 = _kernel32()
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        # Anonymous and not inheritable: no child holds the job open.
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _CHILD_JOB_LIMITS
        if not kernel32.SetInformationJobObject(
            job, _JOB_EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)
        ) or not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            kernel32.CloseHandle(job)
            return None
        _windows_exit_with_parent(kernel32)
        return int(job)
    except Exception:
        # Containment only: a host that cannot make the job still starts the server.
        return None


def _engine_pid() -> int:
    """The pid of the engine that started this process.

    Started through a Windows venv's ``python.exe`` redirector, this process's
    parent is that redirector, which waits on this process and so never exits
    first: the engine is one hop above it. Best-effort: a pid that cannot be
    read falls back to the direct parent.
    """
    parent = os.getppid()
    try:
        # Deferred: only a Windows launch needs them, and a POSIX one stays standard library only.
        from kiro_crew.platform_compat_identity import get_ppid
        from kiro_crew.platform_compat_runtime import python_launcher_hops

        above = get_ppid(parent) if python_launcher_hops() else -1
    except Exception:
        above = -1
    return above if above > 0 else parent


def _windows_exit_with_parent(kernel32: Any) -> threading.Thread | None:
    """End this process once its parent (the engine) has exited.

    The parent is opened now, before any child starts, and waited on by a daemon
    thread, which ends this process and so closes its job. A parent already gone
    before the open ends this process at once, before any server starts; one that
    cannot be opened for another reason is not watched.
    """
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    parent = kernel32.OpenProcess(_SYNCHRONIZE, False, _engine_pid())
    if not parent:
        if _last_error() == _ERROR_INVALID_PARAMETER:
            _exit_process(_EXIT_PARENT_GONE)
        return None

    def _watch() -> None:
        if kernel32.WaitForSingleObject(parent, _INFINITE) == 0:
            _exit_process(_EXIT_PARENT_GONE)

    watcher = threading.Thread(target=_watch, name="kirocrew-launcher-parent", daemon=True)
    watcher.start()
    return watcher


def _spawn_child(argv: list[str], env: dict[str, str]) -> NoReturn:
    """Windows has no in-place exec: run the server as a child on this process's stdio.

    The engine waits on this process's pid and talks over its pipes, so the
    launcher stays alive as the server's parent and exits with its status, the
    shape ``mcp_gateway.stub_fallback._fallback_spawn_child`` uses. The child
    runs in this process's job (:func:`windows_bind_children_to_self`), so its
    tree ends with the launcher.
    """
    resolved = windows_child_command(argv[0], env)
    if resolved is None:
        _unstartable(argv[0], FileNotFoundError(errno.ENOENT, "not found on its PATH"))
    windows_bind_children_to_self()
    try:
        proc = subprocess.Popen([resolved, *argv[1:]], env=env, stdin=0, stdout=1, stderr=2)
    except OSError as exc:
        _unstartable(argv[0], exc)
    try:
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        raise
    os._exit(rc if 0 <= rc <= 0x7FFFFFFF else 1)


def main(argv: list[str] | None = None) -> NoReturn:
    server, doc, environ = _resolve_snapshot(
        list(sys.argv[1:] if argv is None else argv), dict(os.environ)
    )
    if "connection" in doc:
        # A Crew-owned Connection: this process relays to the gateway's proxy, which
        # holds the token, over an attested channel (kas_connection_bridge).
        port, token = doc.get("port"), doc.get("bridge_token")
        if not isinstance(port, int) or not isinstance(token, str) or not token:
            _refuse(server, "its launch snapshot names no Connection bridge")
        from kiro_crew.kas_connection_bridge import run_connection_bridge

        raise SystemExit(run_connection_bridge(doc, server=server))
    if "url" in doc:
        # A remote server whose headers stayed local: this process becomes the
        # stdio server the engine talks to, and adds them to every request.
        # Platform trust comes first, as in every other Crew entry point, so a
        # bundled interpreter verifies an https url against the system's CAs.
        from kiro_crew._ssl_compat import _ensure_ssl_certs
        from kiro_crew.kas_http_bridge import run_bridge

        url, headers = resolve_bridge(server, doc)
        _ensure_ssl_certs()
        raise SystemExit(run_bridge(url, headers, server=server))
    launch, env = _stdio_launch(server, doc, environ)
    if os.name == "nt":
        _spawn_child(launch, env)
    try:
        # The recorded command is the operator's own server from their agent file,
        # read back from the snapshot the gateway wrote; nothing here comes from a peer.
        # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
        os.execvpe(launch[0], launch, env)
    except OSError as exc:
        _unstartable(launch[0], exc)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
