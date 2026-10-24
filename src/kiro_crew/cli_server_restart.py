"""``kirocrew restart``: stop the running gateway, then start and wait for its replacement.

Holds the wait for the incumbent's pids to exit, the detached spawn of ``kirocrew
gateway`` from this install's own console script, the readiness probe with its
deadline and checkpoints, the ``gateway.log`` lines relayed when the replacement dies,
and the sign-in URL printed once it serves. The checks it shares with ``kirocrew
stop`` come from ``cli_server_stop``.

Split out of ``kiro_crew.cli_server``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.cli_server.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New restart work goes here.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.cli_server_peer import (
    _CLI_LOOPBACK,
    _GatewayPeerRefused,
    _owner_verified_secret_urlopen,
)
from kiro_crew.cli_server_stop import (
    _pid_exited,
    _refuse_if_lock_held,
    _refuse_lock_holder,
    _refuse_service_alias,
    _report_authenticated_shutdown,
    _stop,
    _verified_loopback_gateway_pids,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import config_dir, read_local_secret
from kiro_crew.dashboard.origin import dashboard_origin, resolve_dashboard_host
from kiro_crew.env import resolved_command_casing
from kiro_crew.gateway_lock import LockProbeError, lock_holder
from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.owner_only_files import owner_only_opener
from kiro_crew.port_resolution import (
    _basename_stem,
    _gateway_owns_port,
    _is_kirocrew_process,
    resolve_client_port,
)
from kiro_crew.sel import sel
from kiro_crew.service import controller as service_controller
from kiro_crew.service.common import RESTART_NOT_UP, RESTART_REFUSED, RESTART_UNCONFIRMED


def _wait_for_pids_exit(pids: list[int], timeout: float) -> list[int]:
    """Block until every pid in *pids* is gone. Return the ones still alive.

    An empty return means they all exited. :func:`_restart` uses this to keep a
    replacement gateway from starting while the incumbent is still shutting down.

    The incumbent holds an exclusive ``flock`` on ``<KIROCREW_HOME>/gateway.lock``
    (see :mod:`kiro_crew.gateway_lock`) for its whole lifetime, and the release
    happens only after ``asyncio.run(_gateway(...))`` returns — so the lock
    outlives dashboard teardown, cron-scheduler stop, conversation-log flushes,
    and MCP child reaping. The replacement's acquire is a single non-blocking
    attempt that prints a refusal and exits 1, so spawning it too early leaves NO
    gateway running at all. ``_stop`` waits at most 1s and reports nothing back,
    which is why restart does its own bounded wait.

    Pid reuse is possible but fails SAFE: the caller classifies the pids as
    Kiro Crew gateways once, before the stop, and a pid recycled onto an unrelated
    process only makes this wait time out. That produces a loud refusal, never a
    premature "the incumbent is gone" all-clear. Re-classifying inside the loop
    would invert that -- a recycled pid would read as "not a gateway" and let the
    replacement spawn while the real incumbent still holds the lock.
    """
    if not pids:
        return []
    deadline = time.monotonic() + timeout
    while True:
        alive = [p for p in pids if not _pid_exited(p)]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.1)


def _incumbent_from_lock_holder(port: int, *, shutdown_acknowledged: bool) -> list[int]:
    """Name the gateway :func:`_restart` must outwait when the port lookup cannot.

    ``_stop`` has just run without naming a pid: with ``lsof``/``netstat``
    unavailable the port lookup is blind, so ``_stop`` requests a graceful
    shutdown over the authenticated API and returns. The incumbent is now
    exiting but still owns ``gateway.lock`` until its teardown finishes.
    Waiting on an empty pid list returns at once, and a replacement spawned at
    that moment loses the lock race to the exiting incumbent, exits, and
    leaves NO gateway once the incumbent is gone. The lock itself knows who
    the incumbent is, so ask it and wait on that pid with the same timeout
    ladder as the port-lookup path.

    *shutdown_acknowledged* says whether ``_stop`` returned, i.e. a gateway
    accepted the authenticated shutdown. Only then is the live holder the
    exiting incumbent, and only then is it returned as ``[holder_pid]`` to be
    waited on, whatever its argv reads. Returns ``[]`` when the lock is free or its recorded holder
    is dead (nothing left to outwait). Every other live holder -- a foreign
    process, or a Kiro Crew gateway that did not acknowledge the shutdown
    (``_stop`` has already refused it) -- is refused through
    :func:`_refuse_lock_holder` with a ``denied`` event: the lock names the
    pid but never makes it safe to signal or to spawn against. An
    indeterminate probe (:class:`LockProbeError`) refuses to spawn with the
    existing indeterminate message: spawning against a lock whose holder
    cannot be established is the exact no-gateway outcome this guard exists
    to prevent.
    """
    try:
        holder = lock_holder(config_dir())
    except LockProbeError as exc:
        sel().log_api_access(
            caller="cli",
            operation="gateway_restart",
            outcome="denied",
            source="cli",
            resources=f"port={port} reason=lock_probe_indeterminate",
        )
        print(f"❌ {exc}. Not signalling anything and not starting a replacement.")
        sys.exit(1)
    if holder.pid is None or not holder.alive:
        return []
    # No argv check here. The holder is already named twice without it: a gateway answered the shutdown that carries this home's generation
    # secret, and the kernel says this pid holds this home's lock. argv would
    # only refuse a gateway whose spawn shape the patterns miss, and strand
    # the user with no gateway. The pid is only WAITED on, never signalled,
    # so dropping the check cannot widen what gets stopped. A holder that is
    # not really exiting just makes the wait time out and restart refuse.
    if shutdown_acknowledged:
        return [holder.pid]
    _refuse_lock_holder("gateway_restart", port, holder.pid)


def _own_console_script() -> str | None:
    """Absolute path of the console script *this* CLI process was invoked as.

    Returns ``None`` unless ``sys.argv[0]`` is an existing executable file
    basenamed ``kirocrew``.

    :func:`_spawn_detached_gateway` prefers this over ``shutil.which("kirocrew")``
    so a restart replaces the gateway with the *same* entry point that asked for
    the restart. ``which`` returns whatever ``kirocrew`` sits earliest on
    ``PATH``, which is not necessarily this one: a downstream edition composes
    this core behind its own ``[project.scripts]`` entry point of the same name,
    so an editable install of the stock core in another interpreter (mise, a
    stray venv) shadows it. Respawning that one starts a gateway with different
    composed providers than the one just stopped — a silent edition downgrade,
    from a command whose only job was to restart what was already running.

    ``which`` remains the fallback for invocations whose argv[0] is not a script
    path (``python -m kiro_crew restart``, a frozen bundle, a launcher that
    rewrites argv).
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0 or _basename_stem(argv0) != "kirocrew":
        return None
    path = Path(argv0)
    if not path.is_absolute():
        # argv[0] may be a bare name found on PATH ("kirocrew") or a relative
        # path; resolve it the way the shell did.
        # ``resolved_command_casing``: on Windows ``which`` spells the appended
        # extension as PATHEXT does (``kirocrew.EXE``), and a launcher shim that
        # dispatches on its own basename refuses that spelling.
        resolved = resolved_command_casing(shutil.which(argv0))
        if not resolved:
            return None
        # MUST be absolutized. ``shutil.which`` returns an argument that already
        # has a directory component *unchanged*, so ``.venv/bin/kirocrew``
        # (a `cd ~/checkout && .venv/bin/kirocrew restart` invocation) comes back
        # still relative. :func:`_spawn_detached_gateway` passes ``cwd=$HOME`` to
        # ``Popen``, which chdirs the child BEFORE exec, so a relative program
        # path would resolve under ``$HOME`` and raise ``FileNotFoundError`` —
        # after ``_stop()`` has already SIGTERMed the gateway, leaving nothing
        # running. ``absolute()`` and not ``resolve()``: prepending the cwd is the
        # whole fix, while following symlinks could exec under a different
        # basename than the one the user invoked.
        path = Path(resolved).absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path)


#: How much of a dead replacement's gateway.log a restart relays to the terminal.
#: A boot refusal (persistence preflight, signing-key preflight or its deadline, no
#: worker thread, a truncated key) writes its reason and repair steps in a handful of
#: lines just before the exit, so the tail is what carries the diagnosis.
_RESTART_LOG_RELAY_LINES = 20


_RESTART_LOG_RELAY_BYTES = 16 * 1024


_RESTART_LOG_RELAY_LINE_CHARS = 400


#: Where gateway.log stood before a spawn: ``(st_dev, st_ino, size)``, with
#: ``st_dev``/``st_ino`` ``None`` when there was no file yet.
_GatewayLogMark = tuple[int | None, int | None, int]


def _gateway_log_mark() -> _GatewayLogMark | None:
    """Record which file gateway.log is and how long it is, before a replacement spawns.

    The replacement rotates a non-empty gateway.log to gateway.log.prev at boot
    (``cli._setup_cli_logging``) and writes everything after that into a fresh file, so
    a size alone cannot say where its output starts: the identity says whether the file
    read later is still the one measured here. ``None`` when gateway.log cannot be
    stat'ed, which makes the relay print nothing rather than an older gateway's lines
    passed off as the replacement's.
    """
    try:
        st = (config_dir() / "gateway.log").stat()
    except FileNotFoundError:
        return (None, None, 0)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size)


def _read_log_tail(path: Path, start: int) -> tuple[bytes, bool]:
    """Return *path*'s bytes from *start*, at most the final relay cap, and whether cut."""
    with open(path, "rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        if end <= start:
            return b"", False
        begin = max(start, end - _RESTART_LOG_RELAY_BYTES)
        fh.seek(begin)
        return fh.read(end - begin), begin > start


def _gateway_log_lines_since(mark: _GatewayLogMark | None) -> list[str]:
    """Return the last lines a dead replacement wrote to gateway.log after *mark*.

    When gateway.log is still the file *mark* measured, only the bytes past its size
    are read. When the replacement rotated it, its output is whatever it wrote to the
    old file before the rotation (now gateway.log.prev, read past the mark's size when
    that file is the one measured) followed by the whole fresh gateway.log. Nothing from
    an earlier gateway is shown. At most the final ``_RESTART_LOG_RELAY_BYTES`` are kept
    and the final ``_RESTART_LOG_RELAY_LINES`` non-blank lines returned. Each line is
    reduced to printable characters and capped, because log text can quote values
    another process chose and must not drive the operator's terminal. Any read error
    yields an empty list: the caller already points at ``kirocrew logs -f``.
    """
    if mark is None:
        return []
    dev, ino, size = mark
    log_file = config_dir() / "gateway.log"
    prev_log = log_file.with_suffix(".log.prev")

    def _same(path: Path) -> bool:
        if ino is None:
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == (dev, ino)

    segments: list[tuple[Path, int]]
    if _same(log_file):
        segments = [(log_file, size)]
    else:
        segments = [(prev_log, size)] if _same(prev_log) else []
        segments.append((log_file, 0))
    data = b""
    cut = False
    try:
        for path, start in segments:
            try:
                chunk, chunk_cut = _read_log_tail(path, start)
            except FileNotFoundError:
                continue
            if chunk_cut:
                data, cut = chunk, True
            else:
                data += chunk
    except OSError:
        return []
    if len(data) > _RESTART_LOG_RELAY_BYTES:
        data, cut = data[-_RESTART_LOG_RELAY_BYTES:], True
    lines = data.decode("utf-8", errors="replace").splitlines()
    if cut and lines:
        lines = lines[1:]  # the first line was cut mid-way by the byte cap
    safe = [
        "".join(ch for ch in line if ch.isprintable())[:_RESTART_LOG_RELAY_LINE_CHARS]
        for line in lines
    ]
    return [line for line in safe if line.strip()][-_RESTART_LOG_RELAY_LINES:]


def _spawn_detached_gateway(port: int | None = None) -> subprocess.Popen[bytes]:
    """Spawn a detached ``kirocrew gateway`` so the calling shell returns.

    Used by :func:`_restart` when no platform service is active. The
    new process:

    - Detaches via ``start_new_session=True`` (own session + process
      group), so closing the calling terminal does not SIGHUP it.
    - Drops stdin to ``/dev/null`` and redirects stdout/stderr to
      ``~/.kiro/crew/gateway.log`` (same file the existing ``logs``
      command tails for foreground gateways), so the user has one
      place to look regardless of how the gateway was started.
    - Resolves the console script this CLI was invoked as
      (:func:`_own_console_script`) first, so a restart respawns the
      *same* ``kirocrew`` rather than whichever one happens to sit
      earliest on ``PATH``; then ``shutil.which("kirocrew")``, falling
      back to ``sys.executable -m kiro_crew`` so editable/source-tree
      dev installs also work without a global ``kirocrew`` symlink.
    - Closes all inherited file descriptors so it does not pin sockets
      or pipes from the parent CLI process.
    - Binds *port* when given (``--port N``).

    Passing *port* is what keeps a restart coherent. The caller has already
    resolved a port, stopped the gateway on it, and will poll *that* port for
    readiness — but the child re-resolves independently, and its resolution
    order has no access to the parent's. Once ``resolve_client_port`` can
    discover a port from a run-marker (or once the marker is cleared by the
    stop we just performed), parent and child can disagree: the replacement
    would bind 5476 while the parent polls 6776 and prints a 6776 URL. Naming
    the port explicitly removes the disagreement by construction.

    Returns the ``Popen`` handle, not just the pid: the caller must be able to
    ask whether the replacement is still alive before it reports success, and
    only the handle yields the child's **exit status** when it is not. A
    replacement refused by the ``KIROCREW_HOME`` ownership guard exits 1 within
    milliseconds, and that status is the whole diagnosis.
    """
    log_path = config_dir() / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Open in append mode so successive restarts accumulate history in
    # one log file. The fd is owned by the child after Popen returns.
    # Created 0600, like every file in the data home (owner_only_files).
    log_fh = open(log_path, "a", encoding="utf-8", opener=owner_only_opener)  # noqa: SIM115

    # The PATH fallback goes through ``resolved_command_casing`` for the same
    # reason as in ``_own_console_script``: a ``kirocrew.EXE`` spelling makes a
    # Windows launcher shim exit at once, after the old gateway was stopped.
    bin_path = _own_console_script() or resolved_command_casing(shutil.which("kirocrew"))
    if bin_path:
        argv: list[str] = [bin_path, "gateway"]
    else:
        # Source-tree/editable-install fallback: run the module directly.
        # This also covers the case where the wrapper script is not on PATH
        # (e.g. running from an unactivated checkout). ``-P`` because this
        # child is spawned with ``cwd=Path.home()`` below and ``-m`` would put
        # that directory first on sys.path, ahead of the standard library --
        # the launch shape under which a stray ``~/concurrent/`` replaced the
        # stdlib package in the field.
        argv = platform_compat.isolated_python_argv("-P", "-m", "kiro_crew", "gateway")
    if port is not None:
        argv += ["--port", str(int(port))]

    # Detach so closing the calling terminal doesn't take the gateway with it.
    # Pass both flags explicitly (NOT **dict unpack — that breaks mypy's Popen
    # overload resolution on the build fleet). POSIX: start_new_session=True (own
    # session/group, immune to SIGHUP); creationflags resolves to 0 (no-op).
    # Windows: there is no setsid (start_new_session is silently ignored), so
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP gives the child its own
    # console-less process group that survives the parent. The flags come from
    # platform_compat (getattr) so referencing them doesn't fail mypy's
    # [attr-defined] check on Linux where subprocess.* lacks them.
    proc = subprocess.Popen(  # noqa: S603 — argv from trusted sources
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        close_fds=True,
        cwd=str(Path.home()),
        start_new_session=platform_compat.IS_POSIX,
        creationflags=(platform_compat.DETACHED_PROCESS | platform_compat.CREATE_NEW_PROCESS_GROUP),
    )
    return proc


_RESTART_TOKEN_TTL = "20h"


_RESTART_TOKEN_WAIT = 15  # seconds to wait for a token after restart


# Seconds the fork path waits for the replacement gateway to become ready. A
# loaded install (many agent files and MCP servers to wrap at boot) routinely
# needs 25-35s, so the default carries margin; KIROCREW_RESTART_READY_TIMEOUT
# overrides it within the clamp. An early death still returns at once, so the
# margin costs nothing on a refused startup.
_RESTART_READY_TIMEOUT_DEFAULT = 60


_RESTART_READY_TIMEOUT_MIN = 15


_RESTART_READY_TIMEOUT_MAX = 180


# Elapsed seconds after which the readiness wait prints one "still starting"
# line on its next check.
_RESTART_READY_SOFT_CHECKPOINT = 15


def _resolve_restart_ready_timeout() -> int:
    """Readiness deadline from ``KIROCREW_RESTART_READY_TIMEOUT``, clamped.

    Read each time the fork path runs, not at import. Unset, non-numeric, non-finite,
    zero or negative values fall back to :data:`_RESTART_READY_TIMEOUT_DEFAULT`;
    fractional values are rounded UP to whole seconds (so the wait is never
    shorter than what was asked for), and the result is clamped to
    ``[_RESTART_READY_TIMEOUT_MIN, _RESTART_READY_TIMEOUT_MAX]`` so a typo
    cannot make restart fail instantly or hang for an unbounded time.
    """
    raw = os.environ.get("KIROCREW_RESTART_READY_TIMEOUT", "")
    if not raw.strip():
        return _RESTART_READY_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        _warn_restart_ready_override(
            raw, _RESTART_READY_TIMEOUT_DEFAULT, "is not a positive number"
        )
        return _RESTART_READY_TIMEOUT_DEFAULT
    requested = math.ceil(value)
    resolved = max(_RESTART_READY_TIMEOUT_MIN, min(_RESTART_READY_TIMEOUT_MAX, requested))
    if resolved != requested:
        _warn_restart_ready_override(
            raw,
            resolved,
            f"is outside {_RESTART_READY_TIMEOUT_MIN}..{_RESTART_READY_TIMEOUT_MAX}s",
        )
    return resolved


def _warn_restart_ready_override(raw: str, used: int, problem: str) -> None:
    print(
        f"⚠️  KIROCREW_RESTART_READY_TIMEOUT={raw!r} {problem}; using {used}s.",
        file=sys.stderr,
        flush=True,
    )


def _print_still_starting(elapsed: float) -> None:
    print(f"⏳ Still starting ({int(elapsed)}s elapsed), continuing to wait...", flush=True)


# Gap between readiness probes while waiting for the replacement gateway. Short
# enough that a fast boot is reported promptly, long enough not to hammer the
# starting gateway's event loop while it restores sessions.
_RESTART_READY_POLL_INTERVAL = 0.5


# Seconds to wait for the incumbent gateway to exit before spawning its
# replacement. Generous because a graceful shutdown reaps MCP servers and
# kiro-cli children; the wait ends as soon as the pids are gone, so the common
# case costs a fraction of a second.
_RESTART_STOP_TIMEOUT = 30.0

# Verdicts returned by :func:`_wait_gateway_ready`. The two failure modes are
# kept apart because they need different operator action: a replacement that
# DIED is a refused/broken startup (read the log), while one that never became
# READY is still running and may simply be slow.
_READY_OK = "ready"


_READY_DIED = "died"


_READY_TIMEOUT = "timeout"


def _probe_gateway_ready(port: int, timeout: int = 3) -> int:
    """HTTP status of ``GET /api/ready`` on the loopback gateway, ``0`` if unreachable.

    Same contract as :func:`kiro_crew.pod.runtime.health` (the status code, or
    ``0`` when the connection itself fails) but pointed at ``/api/ready`` rather
    than ``/api/health``. That choice is the point of the probe: liveness only
    proves a socket is bound, whereas readiness returns 503 until
    ``DashboardState.ready`` is published *and* 503 again the moment shutdown is
    requested — exactly the difference between "something answers this port" and
    "the new gateway is serving". Both paths are in ``origin.PROBE_PATHS`` and
    need no auth, so this hands no secret to whatever answers.

    Every failure mode collapses to ``0`` (unreachable), including a listener
    that answers the TCP handshake but does not speak HTTP. That case raises
    ``http.client.HTTPException`` (``BadStatusLine`` and friends), which is NOT
    an ``OSError`` or a ``URLError``, so it has to be caught explicitly -- and it
    is exactly the case this probe exists to survive: a wedged fork holding the
    port is the reason restart is being run at all, and it must produce a
    "not ready" verdict, never an uncaught traceback out of the CLI.
    """
    url = f"http://{_CLI_LOOPBACK}:{port}/api/ready"
    try:
        # Loopback-only probe to our own gateway on 127.0.0.1; the URL is
        # internally derived (never attacker-supplied), so the dynamic-URL SSRF
        # audit rule is a false positive here.
        with loopback_urlopen(url, timeout=timeout) as resp:  # nosemgrep
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return 0


def _replacement_is_serving(port: int, prior_pid: int | None) -> bool:
    """True when the gateway now answering *port* is NOT the pre-restart one.

    A bare port probe cannot tell a replacement from the incumbent — the old
    gateway keeps answering until its socket closes, so a 200 taken during the
    handover would report success for the process we just asked to die. The
    discriminator is the run-marker pid: every dashboard-serving gateway records
    its own pid in ``run/gateway-<port>.pid`` while wiring the dashboard, i.e.
    *before* it publishes readiness, so a ready gateway's marker always names the
    process that is serving. Waiting for that identity to CHANGE (rather than for
    any 200) mirrors the ``_gateway_start_id`` handshake in the dev-fleet app.

    On POSIX with a working listener lookup the marker claim is additionally
    checked against reality via :func:`_gateway_owns_port` (recorded pid holds
    the port, owned by us, looks like a gateway). That check denies outright on
    Windows — and on any POSIX host without ``lsof``/``netstat`` it cannot
    succeed either — so it is only applied where it can pass; elsewhere the
    marker comparison alone stands, which keeps restart from reporting a false
    failure on those hosts.

    **No marker means not proven, never proven.** An absent marker is the state
    the handover itself produces: ``clear_marker`` runs on graceful shutdown
    BEFORE the outgoing gateway's ``_shutdown()``, so there is a window in which
    the old gateway has erased its marker and its socket still answers. Treating
    that as "the replacement is serving" would report the outgoing gateway's 200
    as the new one's — precisely the confusion this function exists to prevent —
    and on a host without a listener lookup nothing downstream would catch it.
    So an unreadable marker returns ``False``: the caller keeps polling until a
    marker appears or the deadline passes. The cost is a gateway whose marker
    write failed (the write is best-effort, wrapped in ``except Exception``)
    being reported as "not ready" while it is in fact serving. That is the right
    way to be wrong here — a misleading timeout leaves a working gateway and an
    accurate ``kirocrew token``, whereas a false success leaves the operator
    believing a dead replacement is up.
    """
    recorded = run_marker.read_pid(port)
    if recorded is None:
        return False
    if prior_pid is not None and recorded == prior_pid:
        return False
    if platform_compat.IS_POSIX and platform_compat.listening_pid_tool_available():
        return _gateway_owns_port(port)
    return True


def _wait_gateway_ready(
    proc: subprocess.Popen[bytes],
    port: int,
    prior_pid: int | None,
    timeout: float,
) -> tuple[str, int | None]:
    """Poll until the spawned gateway actually serves *port*, or fail.

    Returns ``(_READY_OK, None)`` once the replacement answers ``/api/ready``,
    ``(_READY_DIED, exit_status)`` as soon as the child has exited, or
    ``(_READY_TIMEOUT, None)`` when the deadline passes with it still up.

    Two details earn their keep:

    * **Early death short-circuits the wait.** A replacement refused by the
      ``KIROCREW_HOME`` ownership guard exits within milliseconds; polling the
      port for the full timeout would turn a instantly-knowable failure into a
      full-deadline stall with a worse message. ``proc.poll()`` is used rather than a pid
      liveness probe because we are the child's parent, so it both detects the
      exit and yields the status the operator needs. (Same shape as ``pod``'s
      ``_wait_healthy`` bailing out on a dead unit instead of burning the wait.)
    * **A zero timeout still probes once.** The deadline is checked *after* the
      probe, so a collapsed timeout reports what is actually there instead of a
      reflexive failure.
    * **The child is re-polled before the timeout verdict.** A replacement that
      exits DURING the last probe would otherwise be reported as "still running
      but not ready", sending the operator to look for a live process that no
      longer exists. The extra poll costs nothing and makes the two verdicts
      mutually exclusive in fact, not just by intention.

    One "still starting" line is printed on the first check after
    :data:`_RESTART_READY_SOFT_CHECKPOINT` seconds, if the deadline is still
    ahead, so a slow boot does not look hung.
    """
    start = time.monotonic()
    deadline = start + timeout
    checkpoint = start + _RESTART_READY_SOFT_CHECKPOINT
    checkpoint_printed = False
    while True:
        status = proc.poll()
        if status is not None:
            return _READY_DIED, status
        if _probe_gateway_ready(port) == 200 and _replacement_is_serving(port, prior_pid):
            return _READY_OK, None
        now = time.monotonic()
        if not checkpoint_printed and checkpoint <= now < deadline:
            _print_still_starting(now - start)
            checkpoint_printed = True
        if now >= deadline:
            status = proc.poll()
            if status is not None:
                return _READY_DIED, status
            return _READY_TIMEOUT, None
        time.sleep(_RESTART_READY_POLL_INTERVAL)


def _print_token_url(port: int) -> None:
    """Wait for the gateway to come up, then print a fresh token URL."""
    deadline = time.monotonic() + _RESTART_TOKEN_WAIT
    while time.monotonic() < deadline:
        try:
            secret = read_local_secret(port, dial_host=_CLI_LOOPBACK)
            if not secret:
                time.sleep(_RESTART_READY_POLL_INTERVAL)
                continue
            # /api/token/local MINTS owner tokens, so the request below goes out
            # over a transport that authenticates the CONNECTED PEER of the
            # actual send connection (see _owner_verified_secret_urlopen): the
            # unix socket kernel-verifies the peer at connect time (SO_PEERCRED +
            # recorded gateway), and where there is no unix socket -- native
            # Windows or a socket-degraded POSIX host -- the send goes over
            # loopback TCP whose connected-peer verdict proves the far end is
            # this install's recorded gateway. A TCP-only gateway whose
            # run-marker write has not landed yet while the port already serves
            # yields a transient MISMATCH during the restart window -- exactly
            # the transient this loop absorbs -- so a peer refusal (not an
            # OSError) is caught by the loop's except below and falls into the
            # non-fatal fallback. No pre-send gate is needed here; the live
            # connected-peer verdict is the single authority.
            url = f"http://{_CLI_LOOPBACK}:{port}/api/token/local?ttl={_RESTART_TOKEN_TTL}"
            req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
            with _owner_verified_secret_urlopen(
                req, port, timeout=3, operation="gateway_token"
            ) as resp:
                data = json.loads(resp.read())
                token = data.get("token", "")
            if token:
                # Print the canonical loopback host (kirocrew.localhost when it
                # resolves, else localhost) — same host the gateway auto-opens —
                # so the post-restart URL doesn't land the browser on a different
                # origin and split the SPA's per-origin localStorage settings.
                # (The /api/token/local call above stays localhost: it's a loopback
                # API request, not a browser URL.)
                host = resolve_dashboard_host(local_only=True)
                print(f"\n🔑 http://{host}:{port}?token={token}")
                origin = dashboard_origin(KiroCrewConfig.load().dashboard.url)
                if origin and "localhost" not in origin:
                    print(f"   {origin}/?token={token}")
                return
        except (_GatewayPeerRefused, OSError, urllib.error.URLError, FileNotFoundError, ValueError):
            # _GatewayPeerRefused is not an OSError, so it must be named here or
            # it escapes this retry loop. A TCP-only gateway whose best-effort
            # run-marker write has not landed yet while the port already serves
            # yields a MISMATCH verdict during the restart window -- exactly the
            # transient this loop exists to absorb -- so a peer refusal falls into
            # the non-fatal fallback below like any other transport failure.
            pass
        time.sleep(1)
    # Non-fatal — gateway might just be slow to start
    print("\n⚠️  Could not generate token (gateway still starting?). Run: kirocrew token")


# How `kirocrew restart` labels the remedy for each kind of failed scope restart
# (the command itself comes from the report), and the word the SEL audit row
# carries for it: `reason=service_restart_denied` is the refusal the privileged
# path has always recorded; the other two are new kinds, not new spellings.
_RESTART_REMEDY_LABEL = {
    RESTART_REFUSED: "Run the restart yourself",
    RESTART_NOT_UP: "Read why it exits",
    RESTART_UNCONFIRMED: "Check its state",
}


_RESTART_AUDIT_KIND = {
    RESTART_REFUSED: "denied",
    RESTART_NOT_UP: "not_up",
    RESTART_UNCONFIRMED: "unconfirmed",
}


def _restart(cli_port: int | None = None) -> None:
    """Restart a running Kiro Crew gateway.

    Service-aware, mirroring :func:`_stop`:

    1. If a systemd/launchd service is active AND the caller did not
       explicitly request a specific port, ask the platform to restart
       it (``systemctl restart`` / ``launchctl kickstart -k``). When that
       restart was attempted and did not take everywhere, fail loudly with one
       line per scope: a scope that restarted reads ``restarted``, and a scope
       that did not names the remedy for its kind: the manager REFUSED the verb
       (system-scope unit, unprivileged caller / polkit denial) → the restart
       command for THAT scope to run by hand; the manager ran it and the unit
       is NOT UP (``activating (auto-restart)``, ``failed``) → that scope's
       journal; the manager stopped answering → ``kirocrew service status``.
       The headline never says the gateway was not restarted when a scope's
       was — with a unit in both scopes, the working per-user gateway restarts
       while the stale system unit beside it does not.
       An attempted restart never falls through to the listener path, which
       cannot see a service gateway bound to a unix socket, would misreport the
       outcome, and would spawn an unmanaged gateway beside an installed unit.
    2. Otherwise, SIGTERM the foreground gateway via the existing
       lsof+SIGTERM path used by ``kirocrew stop``, then spawn a
       detached replacement and **verify it is serving** before reporting
       success: a spawn only proves a pid was created, so the replacement is
       polled on ``/api/ready`` until it answers. If it dies or never becomes
       ready the command prints why and exits non-zero rather than claiming a
       gateway that is not there.

    When ``cli_port is not None`` (user passed ``--port N``), branch (1) is
    bypassed: the systemd unit name is not bound to a specific port, so
    short-circuiting through it would target the wrong gateway.
    """
    port = resolve_client_port(cli_port)
    if cli_port is None:
        report = service_controller.restart_service()
        if report:
            sel().log_api_access(
                caller="cli",
                operation="gateway_restart",
                outcome="allowed",
                source="cli",
                resources=f"port={port} via=service",
            )
            print("✅ Restarted kirocrew service.")
            _print_token_url(port)
            return
        if report.attempted or service_controller.is_service_active():
            # A managed unit was there and the restart did not take. An ATTEMPTED
            # restart never falls through, whatever the unit reads now: a unit
            # that landed `failed` (start limit hit) or `inactive` is still an
            # installed, enabled unit, and the listener path below would spawn an
            # unmanaged gateway beside it. Falling through is worse than failing
            # in every shape: on a unix-socket deployment nothing listens on TCP,
            # so the fallback finds nothing to stop, spawns a competitor the
            # KIROCREW_HOME lock refuses, and the original unit keeps running (or
            # flapping) while the command's outcome reads like a restart. The
            # report tells the shapes apart per scope and each gets ITS remedy:
            # the manager REFUSED the verb (a system-scope unit needs root/polkit
            # this process lacks — "Interactive authentication required") → the
            # restart command for that scope with the privilege it needs,
            # `systemctl --user restart` for the user unit and never `sudo
            # systemctl restart`, which answers "Unit kirocrew.service not found"
            # on a host whose only unit is the user one; the manager ran it and
            # the unit is NOT UP (a `Type=simple` start job succeeds the moment
            # the process is forked, so a gateway that exits on start lands in
            # `activating (auto-restart)` with `systemctl restart` having exited
            # 0; or the job itself failed) → that scope's journal, since a
            # hand-run restart would fail the same way; the manager stopped
            # answering while the unit was re-read → its health is UNKNOWN, check
            # `kirocrew service status`. The live active-check is only for a
            # restart that attempted nothing: a unit that came up in between
            # must not be replaced either, while a service that merely stopped
            # falls through below.
            #
            # Per scope, never as one verdict: with a unit running in BOTH
            # scopes (a stale crash-looping system unit beside the working
            # per-user one) `restart()` restarts one and not the other, and "the
            # gateway was NOT restarted" would be false for the gateway the
            # operator uses. The headline names the scope that restarted, the
            # scope that did not carries its reason and remedy, and the exit code
            # says something still needs a hand — the same shape `service
            # uninstall` gives a two-scope teardown that finished in one scope.
            failures = report.failures
            restarted = report.restarted
            kinds = sorted({_RESTART_AUDIT_KIND.get(f.kind, f.kind) for f in failures})
            sel().log_api_access(
                caller="cli",
                operation="gateway_restart",
                outcome="partial" if restarted else "denied",
                source="cli",
                resources=(
                    f"port={port} via=service"
                    + (f" restarted={','.join(o.scope for o in restarted)}" if restarted else "")
                    + f" reason=service_restart_{'+'.join(kinds) if kinds else 'nothing_attempted'}"
                ),
            )
            if restarted:
                lines = [
                    "⚠️ Restarted kirocrew service in the "
                    f"{' and '.join(o.scope for o in restarted)} scope; the restart did "
                    f"not take in the {' and '.join(f.scope for f in failures)} scope:"
                ]
                for outcome in restarted:
                    lines.append(f"   {outcome.scope} scope: restarted.")
            else:
                lines = [
                    "❌ A kirocrew service is installed and running, but the restart did "
                    "not take — the gateway was NOT restarted."
                ]
            for failure in failures:
                lines.append(f"   ⚠️ {failure.scope} scope: {failure.reason}")
                lines.append(
                    f"       {_RESTART_REMEDY_LABEL.get(failure.kind, 'Then')}:  {failure.hint}"
                )
            if not failures:
                # The unit came up between the restart's own scope selection
                # and the check above; nothing was issued at it.
                lines.append(
                    "   The service manager restarted nothing; run `kirocrew restart` again."
                )
            print("\n".join(lines))
            sys.exit(1)

        # The restart attempted nothing and no managed unit is active — but our
        # name may be an alias of another unit (restart_service() /
        # is_service_active() both fail closed on an alias). Refuse before the
        # foreground path below, which would SIGTERM the alias target's managed
        # process on the port and spawn a detached replacement beside it.
        if _refuse_service_alias("gateway_restart", port):
            sys.exit(1)

    # No service active — bounce the foreground gateway and detach a fresh one.
    # Reuse _stop() for the SIGTERM path so behavior stays in sync if _stop
    # ever gains new safety checks. _stop() exits the process with sys.exit(1)
    # when no gateway is running, which is wrong for restart: a user running
    # `kirocrew restart` after the gateway crashed should still get a fresh
    # gateway. Detect that case up-front instead of letting _stop() exit.
    # Also enter _stop() when the lookup tool is absent: find_listening_pids()
    # returns [] both when nothing listens AND when lsof is missing, so guarding
    # only on a truthy result would skip the stop and double-spawn a second
    # gateway on a lsof-less POSIX host. _stop() tries the authenticated API
    # and the lock-holder oracle first (neither needs the tool) and surfaces
    # the distinct "lsof not found" diagnostic (and exits) only when both find
    # nothing.
    #
    # Capture the incumbent pids HERE, before the stop, so we can wait for them
    # afterwards: the replacement must not start while the old gateway still owns
    # the KIROCREW_HOME lock (see _wait_for_pids_exit). Filter with _stop()'s own
    # kirocrew-process predicate so an unrelated listener on this port never
    # becomes something we block on. The entry condition below stays on the
    # UNFILTERED lookup so _stop() keeps emitting its existing diagnostics.
    # Identity of the gateway we are about to replace, read BEFORE the stop: a
    # graceful shutdown clears the run-marker, so afterwards there is nothing
    # left to compare the replacement against. _replacement_is_serving() uses it
    # so a 200 from the OUTGOING gateway can never be mistaken for the new one
    # coming up. None (no marker, e.g. after a crash) simply means there is no
    # old identity to exclude.
    prior_marker_pid = run_marker.read_pid(port)
    listeners = platform_compat.find_listening_pids(port)
    incumbents = [p for p in listeners if _is_kirocrew_process(p)]
    # argv named no incumbent, so the stop below may go through the
    # authenticated endpoint instead. Resolve who would answer it NOW, while the
    # gateway is still up: after the stop that identity is gone, and it is the
    # pid that must exit before a replacement can bind.
    endpoint_incumbents = (
        _verified_loopback_gateway_pids(port) if listeners and not incumbents else []
    )
    wait_for_incumbents = False
    if listeners or not platform_compat.listening_pid_tool_available():
        # TOCTOU: the gateway can exit between the check above and _stop()'s own
        # lookup. _stop() raises SystemExit(1) when it finds nothing — for restart
        # that's the wrong behavior. Swallow SystemExit so we always proceed to
        # spawn a fresh gateway. The user asked for a restart; an exit-before-spawn
        # here would leave them with no running gateway at all. _stop() also
        # exits when it REFUSES (a live lock holder it does not signal); that
        # case is re-read from the lock below, so the swallow does not turn a
        # refusal into a spawn.
        stop_returned = False
        try:
            _stop(cli_port)
            stop_returned = True
        except SystemExit:
            pass
        wait_for_incumbents = True
        if not incumbents and stop_returned:
            # The stop returned while the argv filter named nobody to wait for:
            # the gateway acknowledged the authenticated shutdown and is now
            # exiting, still owning the port and the lock. Wait for the pid that
            # answered on loopback -- not every listener on the port, so an
            # unrelated process sharing the number cannot stall the restart for
            # the full timeout. An empty wait here returns at once and the
            # replacement loses the race to the gateway still shutting down.
            incumbents = endpoint_incumbents
        if not incumbents:
            # The port lookup named nobody to wait for. If _stop returned, a
            # gateway acknowledged the authenticated shutdown (the one path that
            # returns without a pid) and is exiting while still owning
            # gateway.lock; an empty wait returns at once and the replacement
            # loses the lock race to it. Resolve the pid through the lock so
            # the wait below is a real one. If _stop exited instead, a live
            # holder means it refused, and restart refuses too.
            incumbents = _incumbent_from_lock_holder(port, shutdown_acknowledged=stop_returned)
    elif _report_authenticated_shutdown(port):
        sel().log_api_access(
            caller="cli",
            operation="gateway_restart",
            outcome="denied",
            source="cli",
            resources=f"port={port} reason=shutdown_ack_listener_lookup_blind",
        )
        print(
            f"❌ Gateway accepted graceful shutdown on port {port}, but listener PID "
            "lookup is unavailable. Not starting a replacement because safe lock "
            "release cannot be proven.\n   Wait for shutdown to finish, then run: "
            "kirocrew restart"
        )
        sys.exit(1)
    else:
        # The port probe found nothing, but that is not how single-instance is
        # actually enforced (see the matching note in _stop): a gateway
        # reachable only over a unix socket, or one the probe simply missed,
        # still holds gateway.lock. Spawning a replacement here would have it
        # cold-refused by that lock a moment later — exactly the split-brain
        # `kirocrew stop`/`kirocrew restart` reporting "nothing running" while
        # `kirocrew gateway` refuses for a live pid. Ask the lock-owner oracle;
        # a live holder is refused and named, never signalled, so the operator
        # stops it by hand and retries.
        try:
            holder = lock_holder(config_dir())
        except LockProbeError as exc:
            # Indeterminate: neither signal the recorded pid (it may name an
            # unrelated process) nor spawn a replacement the lock may refuse.
            sel().log_api_access(
                caller="cli",
                operation="gateway_restart",
                outcome="denied",
                source="cli",
                resources=f"port={port} reason=lock_probe_indeterminate",
            )
            print(f"❌ {exc}. Not signalling anything and not starting a replacement.")
            sys.exit(1)
        if holder.pid is not None and holder.alive:
            _refuse_lock_holder("gateway_restart", port, holder.pid)

    if wait_for_incumbents:
        alive = _wait_for_pids_exit(incumbents, _RESTART_STOP_TIMEOUT)
        if alive:
            # Refuse rather than spawn a replacement that the lock would reject.
            # Aborting leaves the user with a (slow) gateway; spawning anyway
            # would leave them with none, reported as a success.
            pids = ", ".join(str(p) for p in alive)
            sel().log_api_access(
                caller="cli",
                operation="gateway_restart",
                outcome="denied",
                source="cli",
                resources=f"port={port} reason=incumbent_still_running pids={alive}",
            )
            print(
                f"❌ Gateway (pid {pids}) did not exit within "
                f"{int(_RESTART_STOP_TIMEOUT)}s. Not starting a replacement.\n"
                f"   The old gateway still owns {config_dir()}, so a new one "
                f"would be refused and exit immediately.\n"
                f"   To inspect the shutdown, run: kirocrew logs -f\n"
                f"   If the process is wedged, force it: kill -9 {pids}"
            )
            sys.exit(1)

    # The incumbents are gone (or there were none), yet the lock is the oracle
    # for "is something running": a child that inherited the descriptor keeps
    # the flock alive after its parent's exit, and a replacement spawned into
    # that lock is refused a moment later, reported here as success. Probe once
    # more, right before the spawn, and refuse on a held or indeterminate lock.
    _refuse_if_lock_held("gateway_restart", port)

    log_mark = _gateway_log_mark()
    proc = _spawn_detached_gateway(port)
    pid = proc.pid
    # A pid is not a running gateway. The replacement can be refused by the
    # ownership guard, crash on a bad config, or hang before it binds — all of
    # which would print the success line below and exit 0 with nothing serving.
    # Report success only once the NEW gateway answers, and audit what happened.
    ready_timeout = _resolve_restart_ready_timeout()
    verdict, exit_status = _wait_gateway_ready(proc, port, prior_marker_pid, ready_timeout)
    if verdict != _READY_OK:
        reason = (
            f"replacement_died exit={exit_status}"
            if verdict == _READY_DIED
            else f"replacement_not_ready_within={ready_timeout}s"
        )
        sel().log_api_access(
            caller="cli",
            operation="gateway_restart",
            outcome="denied",
            source="cli",
            resources=f"port={port} via=fork pid={pid} reason={reason}",
        )
        if verdict == _READY_DIED:
            print(
                f"❌ Replacement gateway (pid {pid}) died immediately "
                f"(exit status {exit_status}). Nothing is serving port {port}.\n"
                f"   A replacement that exits at once is usually refused startup — "
                f"another process still owning {config_dir()}, or a broken config.\n"
                f"   To see why it exited, run: kirocrew logs -f"
            )
            tail = _gateway_log_lines_since(log_mark)
            if tail:
                print("   Its last lines in gateway.log:")
                for line in tail:
                    print(f"     {line}")
        else:
            print(
                f"❌ Replacement gateway (pid {pid}) did not become ready within "
                f"{ready_timeout}s. It is still running but not "
                f"serving port {port}.\n"
                f"   It may be slow to start or wedged during startup; nothing is "
                f"serving the dashboard yet.\n"
                f"   To follow its startup, run: kirocrew logs -f"
            )
        sys.exit(1)

    sel().log_api_access(
        caller="cli",
        operation="gateway_restart",
        outcome="allowed",
        source="cli",
        resources=f"port={port} via=fork pid={pid}",
    )
    print(f"✅ Started detached gateway (pid {pid}). Logs: kirocrew logs -f")
    _print_token_url(port)
