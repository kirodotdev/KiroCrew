"""Finding and stopping this home's gateway: ``kirocrew stop`` and ``kirocrew gateway-pid``.

``_stop`` asks a running gateway for an authenticated shutdown first, and signals a pid
only once it is shown to be this home's gateway (its lock, its loopback listener, its
command line); it then stops the MCP gateway daemon. The service-alias and lock-holder
refusals live here because ``kirocrew restart`` makes the same checks.

Split out of ``kiro_crew.cli_server``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.cli_server.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New stop work goes here.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import shlex
import signal
import sys
import time
import urllib.error
import urllib.request
from typing import NoReturn

from kiro_crew import platform_compat
from kiro_crew.cli_server_peer import (
    _CLI_LOOPBACK,
    _GatewayPeerRefused,
    _owner_verified_secret_urlopen,
    _recorded_gateway_pid,
)
from kiro_crew.config.loader import config_dir, read_local_secret
from kiro_crew.gateway_lock import LockProbeError, lock_holder
from kiro_crew.port_resolution import _basename_stem, _is_kirocrew_process, resolve_client_port
from kiro_crew.sel import sel
from kiro_crew.service import controller as service_controller

_SHUTDOWN_RESPONSE_MAX_BYTES = 4096


def _request_gateway_shutdown(port: int) -> bool:
    """Request graceful shutdown through the gateway's self-authenticating API.

    This is the safe fallback when the platform listener lookup returns no pid,
    whether because nothing listens on TCP or because the lookup tool itself is
    unavailable -- the request needs no port tool. The request targets a fixed
    IPv4 loopback URL and presents
    the per-generation local secret; the handler independently requires both
    loopback origin and a constant-time secret match before setting the same
    shutdown event as SIGTERM.

    A stale secret cannot authorize another gateway generation, and no process
    identity is guessed or signaled on this path. Any missing credential,
    refusal, malformed response, or transport failure returns ``False`` so the
    caller retains the existing no-target diagnostic.

    No port-ownership pre-check is applied here: the empty-lookup ``_stop`` arm
    is the path taken precisely when the listener lookup cannot be trusted, so a
    port-lookup gate would delete the fallback rather than protect it.

    The secret is instead bound to the connection that carries it, through
    :func:`_owner_verified_secret_urlopen`. With the gateway's owner-only
    ``AF_UNIX`` socket present it rides that socket and the CONNECTED peer is
    checked (``SO_PEERCRED`` uid plus the recorded gateway pid and start token)
    -- no listener lookup involved. Without one (a TCP-only gateway, native
    Windows) it rides loopback TCP and the far end of THIS 4-tuple must be the
    recorded gateway owned by this account (``/proc`` on Linux,
    ``GetExtendedTcpTable`` on Windows, ``lsof`` on macOS). There is no
    unverified send: a host that cannot prove the peer refuses, and a refused
    peer returns ``False`` like any other failure, so ``_stop`` keeps its
    no-target diagnostic.
    """
    secret = read_local_secret(port, dial_host=_CLI_LOOPBACK)
    if not secret:
        return False
    request = urllib.request.Request(
        f"http://{_CLI_LOOPBACK}:{port}/api/shutdown",
        method="POST",
        headers={"X-Local-Secret": secret, "Content-Type": "application/json"},
        data=b"{}",
    )
    try:
        with _owner_verified_secret_urlopen(
            request, port, timeout=5, operation="gateway_stop"
        ) as response:
            if int(response.status) != 200:
                return False
            raw = response.read(_SHUTDOWN_RESPONSE_MAX_BYTES + 1)
            if len(raw) > _SHUTDOWN_RESPONSE_MAX_BYTES:
                return False
            payload = json.loads(raw)
    except (
        _GatewayPeerRefused,
        http.client.HTTPException,
        OSError,
        RecursionError,
        UnicodeError,
        ValueError,
        urllib.error.URLError,
    ):
        return False
    return bool(isinstance(payload, dict) and payload.get("ok") and payload.get("shutting_down"))


def _report_authenticated_shutdown(port: int, reason: str = "listener_lookup_empty") -> bool:
    """Request, audit, and report an authenticated graceful shutdown.

    *reason* names the path that asked, since two of them do: the port lookup
    found nobody (the default), or it found a listener no argv pattern
    classifies (``argv_declined_listener``). The SEL record separates them.
    """
    if not _request_gateway_shutdown(port):
        return False
    sel().log_api_access(
        caller="cli",
        operation="gateway_stop",
        outcome="allowed",
        source="cli",
        resources=f"port={port} via=api reason={reason}",
    )
    print(f"✅ Requested graceful shutdown from gateway on port {port}.")
    return True


#: Longest process basename ``_stop`` echoes to the operator's terminal.
_MAX_ECHOED_NAME_LEN = 64


def _terminal_safe_name(name: str) -> str:
    """Reduce an untrusted process name to characters safe to print.

    The name comes from another process's own ``argv[0]``: any local process
    can bind the gateway port and choose it, so it is attacker-producible text
    headed for the operator's terminal. Every non-printable code point is dropped
    -- C0/C1 controls (so ESC and BEL, which start and end ANSI SGR and OSC
    sequences), and Unicode format characters -- and the result is capped, so a
    crafted name can neither drive the terminal nor flood the line.
    ``str.isprintable`` is the filter: it keeps letters, digits, punctuation and
    ordinary spaces of every script and rejects the whole control and format
    classes without enumerating escape grammars.
    """
    return "".join(ch for ch in name if ch.isprintable())[:_MAX_ECHOED_NAME_LEN]


def _verified_loopback_gateway_pids(port: int) -> list[int]:
    """The pids a ``127.0.0.1:<port>`` request reaches, if they are our gateway.

    Empty unless the process that would answer is provably the gateway that
    recorded itself on this port. That proof has to exist before the request is
    made, because the request carries the per-generation local secret and that
    secret mints owner tokens: handing it to whatever answers would let a
    foreign local process act as the operator. Reachability is not identity, and
    argv cannot supply it here -- being argv-declined is the whole situation
    this path serves. So the proof is the one
    ``port_resolution._gateway_owns_port`` documents, minus its argv step (which
    that contract itself keeps as defense in depth rather than proof), plus the
    start identity and the ADDRESS the request will actually reach:

    1. the pid and start token the gateway recorded for this port --
       ``run/gateway-<port>.pid`` and its ``.start`` sidecar, written ``0600``
       inside the ``0700`` ``run/`` dir, which is on the ``is_sensitive_path``
       floor, so another local user cannot nominate a process of theirs;
    2. that start token still matches the live pid's, so a pid left behind by a
       crash and recycled onto an unrelated process cannot inherit the claim;
    3. the pid is one of those a loopback connect actually reaches
       (:func:`platform_compat.loopback_owner_pids` mirrors the kernel's
       most-specific-bind dispatch) -- so a gateway bound to some other specific
       address can never vouch for a process squatting ``127.0.0.1``;
    4. the pid is owned by this account.

    Fails closed at every step. Denies outright off POSIX, where
    ``process_owner_uid`` reports no owner and the file-permission argument the
    recorded identity rests on does not hold -- the same boundary
    ``_gateway_owns_port`` draws, for the same reason. A same-account attacker is
    out of scope by construction: they can already read the secret file itself.
    """
    if not platform_compat.IS_POSIX:
        return []
    pid = _recorded_gateway_pid(port)
    if pid is None:
        return []
    if pid not in platform_compat.loopback_owner_pids(platform_compat.find_port_listeners(port)):
        return []
    owner = platform_compat.process_owner_uid(pid)
    if owner is None or owner != os.getuid():
        return []
    return [pid]


def _gateway_pid() -> None:
    """Print this home's gateway-lock holder as one line of JSON, then exit 0.

    Read-only. Resolves the incumbent through :func:`gateway_lock.lock_holder`,
    the same non-destructive oracle ``stop --expect-pid`` and ``restart`` use,
    so the answer is **socket-independent**: it names a draining gateway that
    has already released its listener socket but still holds ``gateway.lock``,
    which the desktop's own ``lsof``/``netstat`` listener snapshot cannot. Left
    unhandled, the desktop cannot name the incumbent, refuses to respawn, and
    sits with no gateway until a human relaunches. The desktop supervisor calls
    this when its listener snapshot comes up empty, so it can wait for the named
    pid to exit (releasing the lock) before spawning.

    Output is a single JSON object on stdout, exit code 0 in every case the
    probe can answer:

    - ``{"holder": "process", "pid": N, "alive": true, "source": "..."}``
      -- a live holder is named; the caller waits for pid N to exit.
    - ``{"holder": "nobody"}`` -- the lock is free (or its recorded holder is
      dead); nothing is holding it, so the caller may spawn at once.

    An indeterminate probe (:class:`gateway_lock.LockProbeError`) splits two
    ways. When the lock is POSITIVELY held by a gateway that hides its own pid
    -- a draining gateway whose stamp is unreadable under a Windows mandatory
    lock -- it prints ``{"holder": "held"}`` and exits 0: a gateway IS alive,
    so the caller waits for the lock to be released (the gateway to finish
    exiting) and then spawns, rather than giving up. Otherwise -- the probe
    could not establish whether the lock is held (an unopenable lock file, an
    unmeasurable filesystem), or a forked inheritor that nothing ends holds it
    -- it prints ``{"holder": "indeterminate", "reason": "..."}`` and exits 2:
    waiting has no end, so the caller must NOT spawn and must not wait
    unbounded -- it surfaces the terminal error.
    """
    try:
        holder = lock_holder(config_dir())
    except LockProbeError as exc:
        if exc.held:
            # Positively held, holder unnameable: a live gateway (likely still
            # draining). Name it as held so the caller waits for release.
            print(json.dumps({"holder": "held", "reason": str(exc)}))
            return
        print(json.dumps({"holder": "indeterminate", "reason": str(exc)}))
        sys.exit(2)
    if holder.pid is None or not holder.alive:
        print(json.dumps({"holder": "nobody"}))
        return
    print(
        json.dumps(
            {
                "holder": "process",
                "pid": holder.pid,
                "alive": holder.alive,
                "source": holder.source,
            }
        )
    )


def _stop_expected_pid(port: int, expect_pid: int) -> None:
    """SIGTERM ``expect_pid`` only while it is provably this home's gateway on ``port``.

    Three facts are read here, immediately before the signal: ``expect_pid`` is
    the sole listener on ``port``, it looks like a Kiro Crew gateway, and it is the
    live holder of this home's ``gateway.lock``. A caller that identified a
    gateway earlier names it by pid, so a listener that replaced it in between
    -- another home's gateway, or a fresh one -- is refused instead of stopped.

    The signal is pinned to the process those checks examined, not to its number:
    a pidfd is opened before the checks and the signal goes through it, so a pid
    recycled after the checks cannot receive it. Where no pidfd exists (macOS,
    Windows) the stop refuses. Every refusal signals nothing and exits 1.
    """

    def refuse(reason: str, message: str) -> NoReturn:
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="denied",
            source="cli",
            resources=f"port={port} expect_pid={expect_pid} reason={reason}",
        )
        print(f"❌ {message} Not signalling anything.")
        sys.exit(1)

    def check_listener() -> None:
        listeners = platform_compat.find_listening_pids(port)
        if listeners != [expect_pid]:
            shown = ", ".join(str(p) for p in listeners) or "nothing"
            refuse(
                "listener_mismatch",
                f"Port {port} is held by {shown}, not by the expected pid {expect_pid}.",
            )

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if platform_compat.IS_WINDOWS or pidfd_open is None or pidfd_send_signal is None:
        refuse(
            "unpinnable_platform",
            "--expect-pid needs a pidfd (Linux) to pin the process it stops.",
        )
    if expect_pid <= 0:
        refuse("invalid_pid", f"Pid {expect_pid} is not a process id.")
    try:
        pidfd = pidfd_open(expect_pid)
    except ProcessLookupError:
        refuse("process_already_exited", f"Pid {expect_pid} has already exited.")
    except OverflowError:
        refuse("invalid_pid", f"Pid {expect_pid} is not a process id.")
    except OSError as exc:
        refuse("identity_unavailable", f"Cannot pin pid {expect_pid} ({exc}).")
    try:
        check_listener()
        if not _is_kirocrew_process(expect_pid):
            refuse("not_kirocrew", f"Pid {expect_pid} does not look like a Kiro Crew gateway.")
        try:
            holder = lock_holder(config_dir())
        except LockProbeError as exc:
            refuse("lock_probe_indeterminate", f"{exc}.")
        if holder.pid != expect_pid or not holder.alive:
            refuse(
                "lock_holder_mismatch",
                f"Pid {expect_pid} does not hold this home's gateway lock.",
            )
        check_listener()
        try:
            pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            refuse("process_already_exited", f"Pid {expect_pid} has already exited.")
        except PermissionError:
            refuse("permission_denied", f"No permission to stop pid {expect_pid}.")
    finally:
        os.close(pidfd)

    for _ in range(10):  # up to 1s, so the port is freed
        time.sleep(0.1)
        if _pid_exited(expect_pid):
            break
    sel().log_api_access(
        caller="cli",
        operation="gateway_stop",
        outcome="allowed",
        source="cli",
        resources=f"pids=[{expect_pid}] port={port} via=expect_pid",
    )
    print(f"✅ Sent SIGTERM to gateway (pid {expect_pid}).")
    _stop_mcp_gateway_daemon()


def _refuse_service_alias(operation: str, port: int) -> bool:
    """Refuse ``kirocrew stop`` / ``kirocrew restart`` when ``kirocrew.service``
    is an alias of another unit, before the foreground-gateway fallback.

    Returns True (and prints the refusal, audits it, and the caller must exit 1)
    when a systemd scope holds our name as an alias; False when none does, so the
    caller proceeds unchanged.

    ``service_controller.stop_service()`` / ``restart_service()`` /
    ``is_service_active()`` all fail CLOSED on an alias — the aliased unit is not
    this gateway, so they never select, count, or act on it. That is correct, but
    it reads to the fallback below like "no managed service here", which then
    finds the process the alias target's manager is supervising on the port,
    SIGTERMs it (the manager restarts it at once — the rendered unit carries
    ``Restart=always``) and, for restart, spawns a detached replacement that
    competes for the port and the ``KIROCREW_HOME`` lock. The CLI cannot justify
    mutating a process it does not manage, so it stops here: exit 1, nothing
    signalled, nothing spawned. ``service uninstall`` already refuses an alias
    whole and ``service status`` already names it; this is the same guard for the
    two verbs that otherwise fall through to signalling.
    """
    holder = service_controller.service_alias_holder()
    if holder is None:
        return False
    sel().log_api_access(
        caller="cli",
        operation=operation,
        outcome="denied",
        source="cli",
        resources=f"port={port} reason=service_name_is_alias scope={holder.scope}",
    )
    # Spell the remedy for the ACTUAL scope and verb, not a fixed
    # `sudo systemctl restart`: a user-scope alias is reached with
    # `systemctl --user` (plain `sudo systemctl` hits the system manager and
    # answers "Unit not found" or acts on a different unit), and a user who ran
    # `kirocrew stop` must be told to STOP the holder, not restart it.
    verb = "restart" if operation == "gateway_restart" else "stop"
    systemctl = "systemctl --user" if holder.scope == "user" else "sudo systemctl"
    print(
        f"❌ kirocrew.service is an alias of {holder.unit_id} in the {holder.scope} "
        f"scope; this command does not manage that unit. {verb.capitalize()} it with\n"
        f"   `{systemctl} {verb} {holder.unit_id}`, or remove the alias."
    )
    return True


def _stop(cli_port: int | None = None, *, expect_pid: int | None = None) -> None:
    """Stop a running Kiro Crew gateway.

    Accepts the raw CLI ``--port`` value (``None`` when not passed).
    Resolution and service-bypass are both derived from this single input:

    - ``cli_port is None``: user didn't pass ``--port``, so we resolve via
      env/config/default AND try the systemd/launchd service first.
    - ``cli_port is not None``: user explicitly targeted a port, so we
      bypass the service short-circuit and SIGTERM the gateway bound to
      that port directly.

    ``expect_pid`` (CLI ``--expect-pid``, only together with ``--port``) narrows
    the stop to that one pid; see :func:`_stop_expected_pid`.
    """
    if expect_pid is not None:
        if cli_port is None:
            print("❌ --expect-pid needs --port. Not signalling anything.")
            sys.exit(2)
        _stop_expected_pid(resolve_client_port(cli_port), expect_pid)
        return
    port = resolve_client_port(cli_port)
    if cli_port is None and service_controller.stop_service():
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="allowed",
            source="cli",
            resources=f"port={port} via=service",
        )
        print("✅ Stopped kirocrew service. To remove it: kirocrew service uninstall")
        _stop_mcp_gateway_daemon()
        return

    # Before the foreground-gateway fallback: refuse when our name is an alias of
    # another unit. ``stop_service()`` returned False for it (it fails closed on
    # an alias), but the fallback below would then SIGTERM the alias target's
    # managed process on the port — exactly what this CLI must not do.
    if cli_port is None and _refuse_service_alias("gateway_stop", port):
        sys.exit(1)

    # Cross-platform port -> listening PID lookup (lsof on POSIX, netstat -ano
    # on Windows — there is no lsof there, so an lsof-only lookup makes
    # `kirocrew stop` a no-op on Windows).
    pids = platform_compat.find_listening_pids(port)

    if not pids:
        # Neither fallback below needs the port tool, so both run BEFORE the
        # tool-absent exit: a live gateway is stopped through its API or its
        # lock file whether or not `lsof`/`netstat` can be trusted, and the
        # tool diagnostic is reserved for the case where nothing else found a
        # gateway either. Graceful API first -- it signals no guessed pid.
        if _report_authenticated_shutdown(port):
            return
        # The port probe found nothing, but the port probe is not how a gateway
        # is actually kept single-instance — the gateway.lock flock is (see
        # gateway_lock.py). A gateway bound to a unix socket only, or one whose
        # listener the port probe missed, still holds that lock; refusing here
        # would tell the operator "nothing running" the same turn `kirocrew
        # gateway` refuses to start because the lock IS held. Fall back to the
        # lock-owner oracle before giving up.
        try:
            holder = lock_holder(config_dir())
        except LockProbeError as exc:
            # Indeterminate is neither "free" nor "held": signalling the pid the
            # file records could hit an unrelated process that reused the number.
            sel().log_api_access(
                caller="cli",
                operation="gateway_stop",
                outcome="denied",
                source="cli",
                resources=f"port={port} reason=lock_probe_indeterminate",
            )
            print(f"❌ {exc}. Not signalling anything; check the file and retry.")
            sys.exit(1)
        if holder.pid is not None and holder.alive:
            # A live holder is never signalled from here, whoever it is: the
            # lock proves a gateway is up, not that the pid it names is one
            # this command may stop or has confirmed the state of. Name the
            # holder and the manual step instead.
            _refuse_lock_holder("gateway_stop", port, holder.pid)
        # Distinguish "lookup tool absent" from "genuinely no listener":
        # find_listening_pids folds a missing lsof into an empty list, so without
        # this a running gateway would be mis-reported as stopped (and _restart
        # would then double-spawn).
        if not platform_compat.listening_pid_tool_available():
            _tool = platform_compat.listening_pid_tool()
            # A host that keeps its binaries outside the system directories
            # (NixOS, a Homebrew or conda prefix) has the tool and still lands
            # here, because the lookup is pinned to those directories. Telling
            # that operator to install what they already have sends them in
            # circles, so name where it actually is instead.
            _unpinned = platform_compat.tool_outside_trusted_dirs(_tool)
            _reason = f"{_tool}_outside_trusted_dirs" if _unpinned else f"{_tool}_not_found"
            sel().log_api_access(
                caller="cli",
                operation="gateway_stop",
                outcome="no_target",
                source="cli",
                resources=f"port={port} reason={_reason}",
            )
            if _unpinned:
                print(
                    f"`{_tool}` is installed at {_unpinned}, outside the system directories "
                    f"Kiro Crew resolves it from, so it cannot look up the gateway process "
                    f"on port {port}. Falling back to PATH is deliberately refused: a "
                    f"gateway's PATH can lead with writable directories."
                )
            else:
                print(
                    f"`{_tool}` not found — cannot look up the gateway process on "
                    f"port {port}. Install {_tool} and retry."
                )
            sys.exit(1)
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="no_target",
            source="cli",
            resources=f"port={port}",
        )
        print(f"No Kiro Crew gateway currently running on port {port}.")
        sys.exit(1)

    # Only kill processes that are actually Kiro Crew gateways.
    # Note: TOCTOU race exists between this check and the kill — the PID could be
    # recycled. Acceptable risk for an interactive CLI tool with low blast radius.
    unrecognized = [p for p in pids if not _is_kirocrew_process(p)]
    pids = [p for p in pids if _is_kirocrew_process(p)]
    if (
        not pids
        and _verified_loopback_gateway_pids(port)
        and _report_authenticated_shutdown(port, "argv_declined_listener")
    ):
        # argv is not the only identity a gateway has, and it is the weakest:
        # every spawn shape has to be taught to the patterns, and the desktop
        # app's is not among them. So before refusing, ask the gateway to stop
        # ITSELF -- one loopback request carrying this generation's secret, which
        # it published at startup whatever its command line reads. Answering
        # shuts down the answerer, so no pid is guessed and nothing here signals
        # a process it has not identified. The request is only made once the
        # process that will receive that secret is proven to be our gateway
        # (_verified_loopback_gateway_pids); unproven keeps the refusal below.
        return
    if not pids:
        # Something holds the port, but nothing on it classifies as a Kiro Crew
        # gateway. Reporting "no gateway running" here is misleading — the port
        # is occupied — and it sends ``kirocrew restart`` on to spawn a
        # replacement the KIROCREW_HOME lock then refuses. Name the pids and, when
        # a cmdline is cheap to read, its basename, so the operator can see what
        # actually holds the port (an unregistered wrapper module is the common
        # case). Exit 1 with a distinct audit reason.
        basenames: list[str] = []
        for p in unrecognized:
            cmdline = platform_compat.process_command_line(p)
            if not cmdline:
                continue
            # Tokenize the way _args_look_like_kirocrew does: a quoted executable
            # path ("C:\Program Files\...\python.exe") is one token, so its
            # basename is reported rather than the fragment before the first space.
            try:
                tokens = shlex.split(cmdline, posix=not platform_compat.IS_WINDOWS)
            except ValueError:
                tokens = cmdline.split()
            if tokens:
                safe = _terminal_safe_name(_basename_stem(tokens[0]))
                if safe:
                    basenames.append(safe)
        pid_list = ", ".join(str(p) for p in unrecognized)
        detail = f" ({', '.join(basenames)})" if basenames else ""
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="no_target",
            source="cli",
            resources=f"port={port} reason=unrecognized_listener pids={unrecognized}",
        )
        print(
            f"Port {port} is held by pid {pid_list}{detail}, not recognised as a "
            f"Kiro Crew gateway. Not stopping it."
        )
        sys.exit(1)

    sent: set[int] = set()
    denied: list[int] = []
    for pid in pids:
        if platform_compat.IS_WINDOWS:
            # No POSIX signals or graceful shutdown for a detached console-less
            # gateway: kill_process_tree uses `taskkill /T /F` so the gateway's
            # detached kiro-cli / MCP-server children are reaped too (a single-PID
            # kill_pid would orphan them). kill_process_tree raises
            # ProcessLookupError / PermissionError / OSError on non-zero
            # taskkill exit — same shape POSIX uses.
            try:
                platform_compat.kill_process_tree(pid, platform_compat.SIGTERM)
                sent.add(pid)
            except ProcessLookupError:
                pass  # already gone
            except PermissionError:
                denied.append(pid)
            except OSError:
                # Generic taskkill failure — re-check liveness rather than
                # guessing whether the pid is denied vs really gone.
                if platform_compat.pid_exists(pid):
                    denied.append(pid)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            sent.add(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            denied.append(pid)

    # Wait briefly for processes to exit so the port is freed
    if sent:
        for _ in range(10):  # up to 1s
            time.sleep(0.1)
            if all(_pid_exited(p) for p in sent):
                break

    if sent:
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="allowed",
            source="cli",
            resources=f"pids={sorted(sent)} port={port}",
        )
        _verb = "Terminated" if platform_compat.IS_WINDOWS else "Sent SIGTERM to"
        print(f"✅ {_verb} gateway (pid {', '.join(str(p) for p in sorted(sent))}).")
        _stop_mcp_gateway_daemon()
    if denied:
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="denied",
            source="cli",
            resources=f"pids={denied} port={port}",
        )
        print(
            f"❌ No permission to stop pid {', '.join(str(p) for p in denied)} — try: sudo kirocrew stop"
        )
        sys.exit(1)
    if not sent:
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="no_target",
            source="cli",
            resources=f"port={port} reason=process_already_exited",
        )
        print(f"No Kiro Crew gateway currently running on port {port} (process already exited).")
        sys.exit(1)


def _manual_stop_command(pid: int) -> str:
    """The command an operator runs to stop *pid* by hand on this platform."""
    if platform_compat.IS_WINDOWS:
        return f"taskkill /PID {pid} /T"
    return f"kill -TERM {pid}"


def _refuse_if_lock_held(operation: str, port: int) -> None:
    """Refuse *operation* unless ``gateway.lock`` is positively free.

    A live holder is refused and named (:func:`_refuse_lock_holder`); an
    indeterminate probe is refused too, since "free" is the one answer that
    would let a second writer spawn. Returns only when the lock is free.
    """
    try:
        holder = lock_holder(config_dir())
    except LockProbeError as exc:
        sel().log_api_access(
            caller="cli",
            operation=operation,
            outcome="denied",
            source="cli",
            resources=f"port={port} reason=lock_probe_indeterminate",
        )
        print(f"❌ {exc}. Not signalling anything and not starting a replacement.")
        sys.exit(1)
    if holder.pid is not None and holder.alive:
        _refuse_lock_holder(operation, port, holder.pid)


def _refuse_lock_holder(operation: str, port: int, pid: int) -> NoReturn:
    """Refuse *operation* because ``gateway.lock`` is held by the live *pid*.

    Reached when the port lookup found nothing and the authenticated shutdown
    got no answer, yet the lock says a gateway is up. The lock is the oracle
    for "is something running" -- without it ``kirocrew stop`` and ``kirocrew
    restart`` report "nothing running" the same moment ``kirocrew gateway``
    refuses to start for a live pid. It is NOT a path to signal through: a
    Kiro Crew holder that neither the port nor the API could reach is in a
    state this command cannot verify, and a foreign holder is not this
    command's to stop. Both are refused the same way -- one SEL ``denied``
    event whose reason names the kind of holder, a refusal that names the
    lock path, the pid and the exact manual command, exit 1 -- and nothing is
    signalled.
    """
    kirocrew = _is_kirocrew_process(pid)
    kind = "kirocrew" if kirocrew else "foreign"
    sel().log_api_access(
        caller="cli",
        operation=operation,
        outcome="denied",
        source="cli",
        resources=f"pids=[{pid}] port={port} reason=lock_holder_{kind}",
    )
    lock_path = config_dir() / "gateway.lock"
    manual = _manual_stop_command(pid)
    tail = "\n   Not starting a replacement." if operation == "gateway_restart" else ""
    if kirocrew:
        print(
            f"❌ {lock_path} is held by pid {pid}: a Kiro Crew gateway is running, but "
            f"the port lookup and the authenticated shutdown could not reach it, so it is "
            f"not signalled from here.\n"
            f"   To stop it manually, run: {manual}{tail}"
        )
    else:
        print(
            f"❌ {lock_path} is held by pid {pid}, but it does not look like a Kiro Crew "
            f"gateway process — refusing to stop it. If this is wrong, stop it manually: "
            f"{manual}{tail}"
        )
    sys.exit(1)


def _stop_mcp_gateway_daemon() -> None:
    """Take the MCP gateway daemon down with the gateway it served.

    The daemon is a separate session leader, so the gateway's own SIGTERM never
    reaches it; it exits on its own once its owner is gone (``--owner-pid``), but
    that probe runs on an interval, and a ``kirocrew restart`` spawns the next
    gateway inside that window -- which then finds a healthy daemon on the socket
    and adopts it. A daemon adopted across a code change is how pooled MCP
    backends kept speaking a wire shape the new gateway did not read. Stopping
    it here, synchronously, means the replacement always spawns its own.

    Best-effort: a stop that finds no daemon, or one this user may not signal,
    is reported and never fails the command that called it.
    """
    try:
        from kiro_crew.mcp_gateway.daemon_control import stop_daemon

        outcome = stop_daemon()
    except Exception:  # pragma: no cover - defensive; the stop already succeeded
        logging.getLogger("kiro_crew.cli_server").debug(
            "mcp gateway daemon stop failed", exc_info=True
        )
        return
    if outcome == "stopped":
        print("✅ Stopped the MCP gateway daemon and its pooled MCP servers.")
    elif outcome == "draining":
        print("⏳ MCP gateway daemon is draining its pooled MCP servers; it exits on its own.")
    elif outcome == "denied":
        print(
            "⚠️  No permission to stop the MCP gateway daemon; it exits once it sees its gateway is gone."
        )
    elif outcome == "unverified":
        print("⚠️  Something other than gatewayd answers the MCP gateway socket; left it alone.")


def _pid_exited(pid: int) -> bool:
    """Return True if *pid* does not exist.

    Routes through ``platform_compat.pid_exists`` — a raw ``os.kill(pid, 0)``
    would TERMINATE the process on Windows instead of probing it.
    """
    return not platform_compat.pid_exists(pid)
