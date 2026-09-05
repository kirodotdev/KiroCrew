"""Shared fakes for the supervisor tests.

This module is named ``test_supervisor_*`` so it falls inside Track S3's
ownership, but it defines no test functions -- pytest collects nothing from it.
It provides a fake backend process that is a real OS process tree (so process
groups, draining and killpg escalation are exercised for real) and never boots a
Kiro Crew gateway.
"""

from __future__ import annotations

import sys
from pathlib import Path

# A real process that can: bind a loopback port, write a gateway secret after a
# delay, fork a grandchild into its own group, and either drain on SIGTERM or
# ignore it (to force SIGKILL escalation). No Kiro Crew import.
FAKE_BACKEND_SRC = r"""
import argparse, os, signal, socket, sys, time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--port", type=int, default=0)
p.add_argument("--run-dir", default="")
p.add_argument("--secret", default="fake-boot-secret")
p.add_argument("--secret-delay", type=float, default=0.0)
p.add_argument("--pidfile", default="")
p.add_argument("--child-pidfile", default="")
p.add_argument("--spawn-child", action="store_true")
p.add_argument("--child-setsid", action="store_true")
p.add_argument("--reap-on-term", action="store_true")
p.add_argument("--ignore-sigterm", action="store_true")
p.add_argument("--ttl", type=float, default=30.0)
a = p.parse_args()

if a.pidfile:
    Path(a.pidfile).write_text(str(os.getpid()))

# Holds the escaped child's pid so the SIGTERM handler can reap its group,
# modelling the real backend, whose kiro-cli workers setsid into their own
# session and are reaped by the backend's own graceful shutdown.
_child = {}

def _on_term(*_):
    if a.reap_on_term and _child.get("pid"):
        try:
            os.killpg(_child["pid"], signal.SIGKILL)  # setsid child: pgid == pid
        except ProcessLookupError:
            pass
    sys.exit(0)

if a.ignore_sigterm:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, _on_term)

lsock = None
if a.port:
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", a.port))
    lsock.listen(16)

if a.spawn_child:
    pid = os.fork()
    if pid == 0:
        # Child. With --child-setsid it becomes its own session/group leader,
        # escaping the parent's process group exactly as a kiro-cli worker does.
        if a.child_setsid:
            os.setsid()
        if a.child_pidfile:
            Path(a.child_pidfile).write_text(str(os.getpid()))
        signal.signal(signal.SIGTERM, signal.SIG_IGN if a.ignore_sigterm else signal.SIG_DFL)
        time.sleep(a.ttl)
        os._exit(0)
    else:
        _child["pid"] = pid

if a.port and a.run_dir:
    time.sleep(a.secret_delay)
    Path(a.run_dir).mkdir(parents=True, exist_ok=True)
    (Path(a.run_dir) / ("gateway-%d.secret" % a.port)).write_text(a.secret)

deadline = time.time() + a.ttl
while time.time() < deadline:
    if lsock:
        lsock.settimeout(0.2)
        try:
            conn, _ = lsock.accept()
            conn.close()
        except socket.timeout:
            pass
        except OSError:
            break
    else:
        time.sleep(0.2)
"""


def write_fake_backend(dir_: Path) -> Path:
    path = Path(dir_) / "fake_backend.py"
    path.write_text(FAKE_BACKEND_SRC)
    return path


def fake_argv(script: Path, **kwargs) -> list[str]:
    """Build argv for the fake backend. Bools are flags; others are --k v."""
    argv = [sys.executable, str(script)]
    for key, value in kwargs.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        else:
            argv += [flag, str(value)]
    return argv


def pid_alive(pid: int) -> bool:
    # This is host-side test code, not container image source: the Dockerfile
    # copies only `container/`, so `kiro_crew` is importable here. Route the
    # liveness check through the shim rather than a raw `os.kill(pid, 0)` probe,
    # which on Windows TerminateProcess()es the pid it is asking about instead
    # of testing it. `pid_exists` keeps the POSIX semantics this helper relied
    # on (EPERM means alive, not gone).
    from kiro_crew.platform_compat import pid_exists

    return pid_exists(pid)


def free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
