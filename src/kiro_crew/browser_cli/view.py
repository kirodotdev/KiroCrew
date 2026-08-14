"""Supervises ``playwright-cli show``, the CLI's own dashboard, over loopback.

``show --port`` is a blocking HTTP server, so it is a long-lived supervised
child with its own lifecycle, not a call that returns a result. The dashboard it
serves carries the live viewport, the tab bar, and **full remote mouse and
keyboard input** into a browser that holds the operator's logged-in sessions.

Three properties are load-bearing; each was established by running the CLI, and
getting any of them wrong presents as a broken feature rather than as an error:

1. **Bind explicitly to ``127.0.0.1``.** The default listener is IPv6-only, so
   ``http://127.0.0.1:<port>/`` is unreachable and an iframe pointed there gets
   a connection failure while the server is running fine.
2. **Health is "any HTTP response", never "200".** ``/`` answers ``302``.
3. **Never ``--host 0.0.0.0``.** That would publish an interactive
   remote-input browser view, holding live logins, to the whole network. This
   module takes no host parameter at all, so there is no argument through which
   a caller could ask for a non-loopback bind.

The port is chosen by bind-probe rather than hardcoded: a fixed port collides
with whatever else the operator runs, and the collision would surface as an
unexplained dead panel.
"""

from __future__ import annotations

import contextlib
import http.client
import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.browser_cli.install import cli_env, cli_path

logger = logging.getLogger(__name__)

# Loopback IPv4, as a constant rather than a parameter. See property 3 above.
LOOPBACK_HOST = "127.0.0.1"

# The server binds, starts Node, and initializes before it answers, so the
# readiness gate is a poll rather than a single probe.
_STARTUP_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.25
_HEALTH_TIMEOUT_S = 2.0
_TERMINATE_GRACE_S = 5.0


@dataclass(frozen=True)
class ShowInfo:
    """Where the running dashboard is reachable."""

    url: str
    port: int


_lock = threading.Lock()
_proc: subprocess.Popen[bytes] | None = None
_info: ShowInfo | None = None


def _free_port() -> int:
    """An OS-assigned ephemeral loopback port.

    Binding port 0 lets the kernel pick one that is free right now. This is
    advisory: the socket is closed before the child binds it, so there is a
    TOCTOU window. A lost race shows up as the child failing its readiness
    gate, which :func:`ensure_running` reports as a failed start rather than
    retrying blindly.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return int(sock.getsockname()[1])


def _healthy(port: int) -> bool:
    """Whether the dashboard answers HTTP on *port*.

    ANY status line counts, including the ``302`` that ``/`` actually returns.
    Only a transport-level failure (nothing listening, hang, reset) is unhealthy
    — the question is whether an HTTP server is there, not what it thinks of the
    request.
    """
    conn = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=_HEALTH_TIMEOUT_S)
    try:
        conn.request("GET", "/")
        conn.getresponse()
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _show_argv(cli: str, port: int) -> list[str]:
    """Argv for the dashboard server.

    ``--host`` is always present and always loopback: omitting it yields an
    IPv6-only listener that ``127.0.0.1`` cannot reach.
    """
    return [cli, "show", "--port", str(port), "--host", LOOPBACK_HOST]


def _alive(proc: subprocess.Popen[bytes] | None) -> bool:
    return proc is not None and proc.poll() is None


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Terminate *proc* and its descendants, escalating to a kill.

    The CLI spawns a browser and helper processes, so signalling only the direct
    child leaves the tree behind holding the port.
    """
    with contextlib.suppress(Exception):
        platform_compat.kill_process_tree(proc.pid)
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        logger.warning("playwright-cli show (pid %s) ignored terminate; killing", proc.pid)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=_TERMINATE_GRACE_S)


def _spawn(cli: str, port: int) -> subprocess.Popen[bytes] | None:
    """Start the dashboard server, or ``None`` if it cannot be spawned.

    Output goes to ``DEVNULL`` on purpose. An unread ``PIPE`` fills its buffer
    and blocks the server permanently, and the readiness signal is the health
    probe rather than a parsed log line, so the output has no reader to justify
    the risk.

    ``start_new_session`` puts the child in its own process group on POSIX so
    the whole tree can be signalled at stop time without touching the gateway's
    own group.
    """
    try:
        return subprocess.Popen(
            _show_argv(cli, port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=platform_compat.IS_POSIX,
            env=cli_env(),
        )
    except OSError as exc:
        logger.warning("could not start playwright-cli show: %s", exc)
        return None


def ensure_running() -> ShowInfo | None:
    """Return the running dashboard, starting it if needed.

    Idempotent: a process that is alive and answering is reused, so repeated
    calls from a panel mount do not spawn a second server. A recorded process
    that has died or stopped answering is reaped first, because leaving it
    would make every later call reuse a corpse.

    ``None`` means no dashboard is available: the CLI is not installed, or the
    server did not become healthy within the startup budget.
    """
    global _proc, _info
    with _lock:
        if _alive(_proc) and _info is not None and _healthy(_info.port):
            return _info
        if _proc is not None:
            _reap(_proc)
            _proc = None
            _info = None

        cli = cli_path()
        if cli is None:
            return None

        port = _free_port()
        proc = _spawn(cli, port)
        if proc is None:
            return None

        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                logger.warning(
                    "playwright-cli show exited during startup (rc=%s) on port %d",
                    proc.returncode,
                    port,
                )
                return None
            if _healthy(port):
                _proc = proc
                _info = ShowInfo(url=f"http://{LOOPBACK_HOST}:{port}", port=port)
                return _info
            time.sleep(_POLL_INTERVAL_S)

        logger.warning("playwright-cli show did not answer on port %d within the budget", port)
        _reap(proc)
        return None


def stop() -> None:
    """Stop the supervised dashboard child and its entire process tree.

    Reaping is scoped to the child we spawned: ``_spawn`` places it in its
    own session (``start_new_session=IS_POSIX``), so ``kill_process_tree``
    signals the whole group — the Node server, the browser, and any helpers
    — without touching processes outside that group. A global ``show --kill``
    is deliberately NOT issued because it would terminate an operator's own
    independently-launched ``playwright-cli show`` session, destroying their
    unsaved work.
    """
    global _proc, _info
    with _lock:
        if _proc is not None:
            _reap(_proc)
        _proc = None
        _info = None


def status() -> dict[str, Any]:
    """Current dashboard state, without starting or stopping anything.

    ``unavailable`` is reported when the CLI is absent, and is distinct from
    ``stopped``: the first cannot be fixed by starting the server.
    """
    with _lock:
        if cli_path() is None:
            return {
                "status": "unavailable",
                "url": None,
                "port": None,
                "reason": "playwright-cli is not installed",
            }
        if _alive(_proc) and _info is not None and _healthy(_info.port):
            return {
                "status": "running",
                "url": _info.url,
                "port": _info.port,
                "reason": None,
            }
        return {"status": "stopped", "url": None, "port": None, "reason": None}
