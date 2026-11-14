"""What one idle MCP stub costs this host, measured once per gateway run.

The MCP page prices "with sharing" by one stub per chat that runs its own copy
of a shareable server (:mod:`kiro_crew.mcp_gateway.footprint`). With sharing
off no stub runs, so there is no stub to read, and the forecast is missing
exactly when it matters most. This module measures one: it starts a single
``kiro_crew.mcp_gateway.stub`` process, brings it to the state an attached
stub idles in, reads its PSS from ``smaps_rollup``, stops it and keeps the
reading for the rest of this gateway run, so a page that polls does not start
a stub on every poll. It is a reading, never a constant.

The stub is the same interpreter and module a real stub runs
(``sys.executable -m``, as the rewriter writes it), launched through the sandbox
chokepoint with the same scrubbed environment and resource limits as any other
routed spawn. It registers, as every real stub does, but on a private unix
socket this module opens for it in a fresh ``0700`` directory under the
gateway's own runtime dir (the socket ``0600``, unix only, gone on every way
out), not on the gateway's socket: the answer accepts the registration and
serves no backend, so no server starts and nothing goes over the network. The probe then sends one
``initialize`` through the stub's bridge and answers it on the socket, so the
reading is taken with the Register payload built, the bridge running and the
first request round-tripped -- the state a real attached stub sits in between
calls. A stub that only answered as an empty server (``--refuse``) skips the
payload and the bridge and reads about 40% low.

A probe that cannot run or read (no sandbox, a stub that does not register or
answer, an unreadable ``smaps_rollup``) is no reading, and that answer is kept
too: the page shows the reading now on its own, and the probe is not retried
every poll. Linux only, like the rest of the footprint. BLOCKING: call it off
the event loop.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.read_limits import config_read_buffer_limit
from kiro_crew.platform_compat_metrics import _proc_pss_kb
from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv

logger = logging.getLogger(__name__)

#: How long the probe stub may take to start, register and answer
#: ``initialize``. A cold interpreter import on a loaded host fits well inside.
_PROBE_TIMEOUT_SECS = 15.0
#: How long a stub that saw its stdin close may take to exit before its tree is
#: killed.
_STOP_GRACE_SECS = 2.0
_REAP_TIMEOUT_SECS = 5.0
#: The name the probe stub gives its server and agent.
_PROBE_NAME = "kirocrew-footprint-probe"
#: ``sun_path`` holds 108 bytes on Linux, the terminating NUL included.
_MAX_SOCKET_PATH = 107

_lock = threading.Lock()
_measured = False
_reading: int | None = None


def idle_stub_bytes() -> int | None:
    """The PSS in bytes of one idle stub, or None when it could not be read.

    Measured on the first call of this gateway run and kept, whatever the
    answer, so concurrent and repeated page polls start at most one stub.
    """
    global _measured, _reading
    with _lock:
        if not _measured:
            _reading = _probe()
            _measured = True
        return _reading


def _reset_for_tests() -> None:
    global _measured, _reading
    with _lock:
        _measured = False
        _reading = None


def _probe_argv(socket_path: str) -> list[str] | None:
    """A stub's argv that registers on *socket_path*, or None with no target.

    The target is ``true``: the stub only hashes it for its Register payload,
    and should any fallback path exec it, the process ends at once instead of
    running anything. ``--poolable`` keeps the stub from degrading to that
    fallback when the answer advertises no capabilities.
    """
    target = shutil.which("true")
    if target is None:
        return None
    return [
        sys.executable,
        "-m",
        STUB_MODULE,
        "--server",
        _PROBE_NAME,
        "--agent",
        _PROBE_NAME,
        "--target-command",
        target,
        "--work-dir",
        tempfile.gettempdir(),
        "--socket",
        socket_path,
        # Match rewritten stubs without loading the config package in the child.
        "--read-limit",
        str(config_read_buffer_limit()),
        "--poolable",
    ]


def _initialize_line() -> bytes:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": _PROBE_NAME, "version": "1"},
        },
    }
    return _line(request)


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def _read_line(fd: int, deadline: float, buf: bytearray) -> bytes | None:
    """One newline-terminated line from *fd* before *deadline*, or None."""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            return None
        chunk = os.read(fd, 65536)
        if not chunk:
            return None
        buf += chunk
    line, _, rest = bytes(buf).partition(b"\n")
    buf[:] = rest
    return line


def _object(line: bytes | None) -> dict[str, Any] | None:
    if line is None:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _attach(
    proc: subprocess.Popen[bytes], server: socket.socket, deadline: float
) -> socket.socket | None:
    """Accept the stub's Register, answer it, and round-trip one ``initialize``.

    Returns the open connection once the stub has written the answer to its
    stdout, or None when any step does not happen before *deadline*. The
    connection stays open while the stub is read: closing it ends the bridge.
    """
    assert proc.stdin is not None and proc.stdout is not None
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    server.settimeout(left)
    try:
        conn, _ = server.accept()
    except OSError:
        return None
    conn.setblocking(False)
    sock_buf, out_buf = bytearray(), bytearray()
    register = _object(_read_line(conn.fileno(), deadline, sock_buf))
    if register is None:
        conn.close()
        return None
    reply: dict[str, Any] = {"type": "registered", "capabilities": []}
    fingerprint = register.get("stub_code_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        reply["fingerprint"] = fingerprint
    try:
        conn.sendall(_line(reply))
        proc.stdin.write(_initialize_line())
        proc.stdin.flush()
    except OSError:
        conn.close()
        return None
    forwarded = _object(_read_line(conn.fileno(), deadline, sock_buf))
    if forwarded is None or forwarded.get("method") != "initialize":
        conn.close()
        return None
    answer = {
        "jsonrpc": "2.0",
        "id": forwarded.get("id"),
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": _PROBE_NAME, "version": "1"},
        },
    }
    try:
        conn.sendall(_line(answer))
    except OSError:
        conn.close()
        return None
    if _object(_read_line(proc.stdout.fileno(), deadline, out_buf)) is None:
        conn.close()
        return None
    return conn


def _stub_pid(root: int) -> int | None:
    """The stub process under *root*.

    The sandbox launcher and its namespace helper carry the stub's command line
    in their own argv, so the stub is the DEEPEST process naming the module.
    """
    from kiro_crew.mcp_gateway.footprint import _argv

    children = platform_compat.proc_child_map() or {}
    stack: list[tuple[int, int]] = [(root, 0)]
    seen: set[int] = set()
    best: tuple[int, int] | None = None
    while stack:
        pid, depth = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if any(STUB_MODULE in part for part in _argv(pid, None)) and (
            best is None or depth > best[1]
        ):
            best = (pid, depth)
        stack.extend((child, depth + 1) for child in children.get(pid, ()))
    return best[0] if best is not None else None


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Close the stub's stdin (it exits on EOF), then kill and reap its tree."""
    for pipe in (proc.stdin, proc.stdout):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        proc.wait(timeout=_STOP_GRACE_SECS)
        return
    except subprocess.TimeoutExpired:
        pass
    platform_compat.kill_popen_tree(proc)
    try:
        proc.wait(timeout=_REAP_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        logger.warning("mcp footprint: probe stub pid %s did not exit after a kill", proc.pid)


def _probe_root() -> str:
    """The gateway's own runtime dir: where its daemon socket lives.

    Real stubs reach their socket there, so the sandbox lets a stub reach it
    too, which a private temp dir under a masked scratch tree would not.
    """
    from kiro_crew.mcp_gateway.daemon_control import configured_socket_path

    root = configured_socket_path().parent
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _probe(*, timeout: float = _PROBE_TIMEOUT_SECS, socket_dir: str | None = None) -> int | None:
    """Start one stub, attach it to a private socket, read its PSS, reap it. BLOCKING.

    The socket sits in a fresh ``0700`` directory (``mkdtemp``) under
    *socket_dir*, the gateway's runtime dir by default. It is a unix socket
    only (no TCP), made ``0600``, and unlinked with its directory on every way
    out, a timeout included.
    """
    if not platform_compat.IS_LINUX:
        return None
    try:
        private = tempfile.mkdtemp(prefix="kc-fp-", dir=socket_dir or _probe_root())
    except OSError:
        logger.debug("mcp footprint: probe directory could not be made", exc_info=True)
        return None
    socket_path = os.path.join(private, "s")
    try:
        argv = _probe_argv(socket_path)
        if argv is None or len(os.fsencode(socket_path)) > _MAX_SOCKET_PATH:
            return None
        return _probe_on(argv, socket_path, timeout)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(socket_path)
        shutil.rmtree(private, ignore_errors=True)


def _probe_on(argv: list[str], socket_path: str, timeout: float) -> int | None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(socket_path)
        os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR)
        server.listen(1)
    except OSError:
        server.close()
        logger.debug("mcp footprint: probe socket could not be opened", exc_info=True)
        return None
    try:
        try:
            wrapped, env, cleanup = sandboxed_spawn_argv(argv, strip_python_env=True)
        except Exception:  # no sandbox for it: no reading, as for an unreadable process
            logger.debug("mcp footprint: probe stub could not be wrapped", exc_info=True)
            return None
        proc: subprocess.Popen[bytes] | None = None
        conn: socket.socket | None = None
        try:
            try:
                proc = popen_limited(
                    wrapped,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                logger.debug("mcp footprint: probe stub did not start", exc_info=True)
                return None
            conn = _attach(proc, server, time.monotonic() + timeout)
            if conn is None:
                return None
            pid = _stub_pid(proc.pid)
            kb = _proc_pss_kb(pid) if pid is not None else -1
            return kb * 1024 if kb >= 0 else None
        finally:
            if proc is not None:
                _stop(proc)
            if conn is not None:
                conn.close()
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)
    finally:
        server.close()
