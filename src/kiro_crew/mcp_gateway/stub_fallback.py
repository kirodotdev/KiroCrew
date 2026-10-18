"""The stub's way out when the gateway cannot serve it: exec the real server directly.

Owner module of :mod:`kiro_crew.mcp_gateway.stub`, which re-exports every name here;
read and patch them there. This module owns:

* :func:`fallback_exec`, which replaces the stub with the target server (on Windows,
  :func:`_fallback_spawn_child` runs it as a child that inherits the stub's stdio), and
  the checks it makes first: whether a session token may ride along
  (:func:`_fallback_target_is_own_control_plane`) and whether the env names a secret
  only the gateway can resolve (:func:`_refuse_unresolved_secret_references`);
* keeping the daemon's connection open across that exec, so the daemon's charge for the
  backend lasts as long as the backend (:func:`_keep_socket_across_exec`);
* the fallback log every degraded start appends to, and the counts the gateway reads
  back from it (:func:`log_fallback`, :func:`alog_fallback`, :func:`fallback_counts`).

It logs under the stub's logger, defined in :mod:`kiro_crew.mcp_gateway.stub_bridge`. It never imports the facade.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, NoReturn

from kiro_crew import platform_compat
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.stub_bridge import logger
from kiro_crew.mcp_gateway.stub_register import _crew_home, _parse_env_file, _resolve_target_args


def _keep_socket_across_exec(writer: asyncio.StreamWriter) -> None:
    """Let the daemon see this connection until the exec'd backend EXITS.

    A fallback exec replaces this process with the real MCP server; the daemon
    charged that server to its host budget when it sent the fallback rejection
    and releases the charge when this connection reaches EOF. Python opens
    sockets non-inheritable, so without this the EOF would come at the exec
    itself and the fallback would be free. The backend inherits one extra
    descriptor it never touches; the kernel closes it when the process tree
    exits. Best-effort: a transport without a socket (a Windows pipe, where the
    stub stays alive as the backend's parent anyway) simply keeps today's shape.
    """
    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        os.set_inheritable(sock.fileno(), True)
    except (OSError, ValueError, AttributeError):
        logger.debug("could not keep the gateway socket across exec", exc_info=True)


def _fallback_log_path() -> Path:
    return _crew_home() / "logs" / "stub_fallback.jsonl"


# Rotate the fallback log once it exceeds this size, keeping ONE previous
# generation (``.jsonl.1``). Unrotated it grows unbounded (467 KB in 15 h on
# one degraded host); a 1 MiB cap bounds total disk use at ~2 MiB
# while keeping enough history for the gateway's per-server fallback-rate
# aggregation (see ``gatewayd`` stats).
_FALLBACK_LOG_MAX_BYTES = 1024 * 1024


def log_fallback(
    reason: str,
    stub_uuid: str,
    pool_label: str,
    args: argparse.Namespace,
    *,
    terminal: bool = False,
) -> None:
    """Append one JSON record to the fallback audit log. OS errors are
    swallowed — logging failure must never block the exec that keeps
    kiro-cli working.

    ``terminal=True`` marks a record as a stub that DIED rather than degraded.
    Both events belong in this one log — same rotation, one place for an operator
    to look — but they must not be added together: :func:`fallback_counts` feeds
    gatewayd's ``stats``, and a fallback rate that silently includes terminals
    misreports whether pooling is engaging. The split is driven by this
    structured field, never by parsing the ``reason`` prefix, and a record
    written before the field existed has no key and so still counts as a
    fallback — which is what it was."""
    try:
        # The log is always under the stub's own data home (_crew_home), so it is
        # owner-only unconditionally: no lexical home test, which would compare
        # a raw KIROCREW_HOME against the resolved one and import config.paths.
        from kiro_crew.owner_only_files import mkdirs_owner_only, owner_only_opener

        log_path = _fallback_log_path()
        mkdirs_owner_only(log_path.parent)
        record = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pid": os.getpid(),
            "stub_uuid": stub_uuid,
            "pool_label": pool_label,
            "reason": reason,
            "terminal": bool(terminal),
            "server": args.server,
            "agent": args.agent,
            "channel_id": args.channel_id or "",
            "target_command": args.target_command,
        }
        # Rotation (shared helper): O(1) rotate-by-rename at the cap, guarded
        # by a non-blocking try-lock so two stubs hitting the cap together
        # cannot both rotate, and a loser never waits — no log_fallback call
        # can stall its stub's event loop. The helper is best-effort by
        # contract (a missing file, a Windows sharing violation, or an
        # unopenable lock file degrades to not rotating), so the append below
        # always still runs; only a failure of the append itself may drop the
        # record, caught by this function's own handler.
        from kiro_crew.jsonl_util import rotate_jsonl_at

        rotate_jsonl_at(log_path, _FALLBACK_LOG_MAX_BYTES)
        with open(log_path, "a", encoding="utf-8", opener=owner_only_opener) as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


# Sliding window for fallback aggregation: long enough to catch a slow drip
# (tens per hour), short enough that a fixed incident ages out of the count.
_FALLBACK_COUNT_WINDOW_SECS = 24 * 3600.0


def fallback_counts() -> dict[str, Any]:
    """Aggregate the fallback audit log into per-server counts.

    Reads the live log plus the one rotated generation (see
    ``_FALLBACK_LOG_MAX_BYTES``) and counts records whose ``ts`` falls within
    the last ``_FALLBACK_COUNT_WINDOW_SECS``. This is the reader the log never
    had: degradations accrued with no signal anywhere. gatewayd folds the
    result into its ``stats`` reply, making the per-server fallback rate
    queryable from the gateway control socket instead of the operator having
    to read a process tree.

    Returns ``{"window_secs": ..., "total": n, "by_server": {name: n},
    "by_reason": {reason: n}, "terminal_total": n,
    "terminal_by_server": {name: n}}``. Never raises — an unreadable or torn log
    yields the counts of whatever parsed.

    ``total`` / ``by_server`` / ``by_reason`` count DEGRADATIONS only. A record
    flagged ``terminal`` is a stub that died instead of degrading, which is a
    different event with a different remedy, so it is tallied separately rather
    than inflating the fallback rate this function exists to report. Records
    predating the flag have no key and count as fallbacks, which is what they
    were.
    """
    cutoff = time.time() - _FALLBACK_COUNT_WINDOW_SECS
    from kiro_crew.jsonl_util import bounded_records

    total = 0
    by_server: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    terminal_total = 0
    terminal_by_server: dict[str, int] = {}
    live = _fallback_log_path()
    for path in (live.with_suffix(".jsonl.1"), live):
        try:
            with open(path, "rb") as f:
                for line in bounded_records(f, path, label="stub_fallback"):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # torn tail line from a racing writer
                    if not isinstance(rec, dict):
                        continue
                    ts = rec.get("ts")
                    if not isinstance(ts, (int, float)) or ts < cutoff:
                        continue
                    server = str(rec.get("server") or rec.get("pool_label") or "?")
                    if rec.get("terminal") is True:
                        terminal_total += 1
                        terminal_by_server[server] = terminal_by_server.get(server, 0) + 1
                        continue
                    total += 1
                    by_server[server] = by_server.get(server, 0) + 1
                    reason = str(rec.get("reason") or "?")
                    by_reason[reason] = by_reason.get(reason, 0) + 1
        except OSError:
            continue
    return {
        "window_secs": _FALLBACK_COUNT_WINDOW_SECS,
        "total": total,
        "by_server": by_server,
        "by_reason": by_reason,
        "terminal_total": terminal_total,
        "terminal_by_server": terminal_by_server,
    }


async def alog_fallback(
    reason: str,
    stub_uuid: str,
    pool_label: str,
    args: argparse.Namespace,
    *,
    terminal: bool = False,
) -> None:
    """Run :func:`log_fallback` in a worker thread.

    The async bridge calls this before every terminal fallback: the write is
    plain blocking file I/O (open/append, plus the best-effort rotation), so
    it runs off the event loop, and awaiting it guarantees the record is on
    disk before the caller proceeds to ``fallback_exec`` (which replaces the
    process image and would otherwise race the write)."""
    await asyncio.to_thread(
        functools.partial(log_fallback, reason, stub_uuid, pool_label, args, terminal=terminal)
    )


def _inherited_std_fd(stream: Any, default: int) -> int:
    """The fd to hand a child for one of our own standard streams.

    ``sys.stdin`` and friends can be replaced or detached (pytest's capture, a
    ``pythonw`` host), in which case ``fileno()`` raises rather than answering.
    Fall back to the well-known number: this runs in a process kiro-cli spawned
    with real pipes on 0/1/2, so the constant is right whenever the object is
    not.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return default
    return fd if isinstance(fd, int) and fd >= 0 else default


def _child_exit_status(rc: object) -> int:
    """The status to exit with after relaying a Windows child's own.

    Windows reports an exit code as an unsigned ``DWORD``, and a crash lands far
    above ``INT_MAX``: an access violation is ``0xC0000005``, i.e. 3221225477.
    ``os._exit`` parses its argument as a C ``int``, so handing that straight
    over raises ``OverflowError`` -- the stub would die on an exception instead
    of relaying the status, which is the one thing this function exists to do,
    and it would do so on the ordinary signature of a crashing backend rather
    than in some corner.

    Reinterpret such a value as signed 32-bit instead of clamping it. The OS
    reads the low 32 bits back out, so the code kiro-cli observes is the exact
    ``DWORD`` the backend exited with -- a clamp would silently rewrite a crash
    into some unrelated status. Anything still outside a C ``int``, or not an
    int at all, becomes 1: unrepresentable, so report plain failure.
    """
    if not isinstance(rc, int):
        return 1
    if rc > 0x7FFFFFFF:
        rc -= 0x100000000
    return rc if -0x80000000 <= rc <= 0x7FFFFFFF else 1


def _fallback_spawn_child(argv: list[str], exec_env: dict[str, str]) -> NoReturn:
    """Stand in for ``execvpe`` on Windows, which has no in-place exec.

    CPython documents the replacement as in-place *"On Unix"*, where the image
    is loaded into this process and keeps its pid. Windows has no such call, so
    the ``exec*`` family is emulated as spawn-then-exit: the backend comes up
    under a NEW pid and THIS process dies. kiro-cli owns this process's stdio
    pipe and waits on the pid it spawned, so that exit reads to it as the server
    hanging up -- it reports ``connection closed: initialize response`` and the
    server's whole tool surface is missing from the session. The degrade path was
    fatal on the one platform it existed to rescue, and every stubbed server
    failed while every directly-launched one worked.

    Run the backend as a CHILD and stay alive as its parent instead. Its stdin,
    stdout and stderr are this process's own fds, duplicated into it explicitly
    (``close_fds`` blocks handle inheritance on Windows, so passing the numbers
    is what guarantees the child gets the real pipe rather than a fresh
    console). kiro-cli therefore talks to the real backend over the pipe it
    already holds, and the pid it waits on lives for the session. Every
    ``fallback_exec`` call site is reached with kiro-cli's ``initialize`` still
    unread -- the stub's own "clean per-session exec" invariant, asserted by the
    comments at each of those sites -- so the hand-off loses no buffered request.

    Resolve the command against the CHILD's ``PATH``, because that is what
    ``execvpe`` searches; Windows ``CreateProcess`` would search this process's
    instead, so a relative command could otherwise resolve differently on the
    two platforms. An unresolvable name is passed through unchanged so the
    resulting ``FileNotFoundError`` still surfaces, matching ``execvpe``.

    Exit through ``os._exit``: this stands in for a process replacement, which
    runs no cleanup handlers and flushes nothing, and a ``SystemExit`` raised
    here would have to survive the teardown of the stub's own event loop.
    """
    resolved = shutil.which(argv[0], path=exec_env.get("PATH")) or argv[0]
    # Our own buffers, not the child's: it inherits the same fds, so anything
    # still queued here would interleave into the middle of its JSON-RPC stream.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            stream.flush()
    proc = subprocess.Popen(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        [resolved, *argv[1:]],
        env=exec_env,
        stdin=_inherited_std_fd(sys.stdin, 0),
        stdout=_inherited_std_fd(sys.stdout, 1),
        stderr=_inherited_std_fd(sys.stderr, 2),
    )
    try:
        rc = proc.wait()
    except BaseException:
        # Do not leave the backend holding kiro-cli's pipe with nobody waiting on
        # it. A hard ``TerminateProcess`` on us cannot be intercepted at all, and
        # the child survives that; what saves the session there is the pipe
        # itself -- kiro-cli's exit closes fd 0 and an MCP server exits on stdin
        # EOF, exactly as a directly-launched one would.
        with contextlib.suppress(OSError):
            proc.terminate()
        raise
    os._exit(_child_exit_status(rc))


def _fallback_target_is_own_control_plane(
    args: argparse.Namespace, target_args: list[str], exec_env: Mapping[str, str]
) -> bool:
    """Whether the process this fallback is about to exec is Kiro Crew's own control plane.

    Delegates to the check gatewayd runs before it hands a pooled backend the
    session token (``_spawns_own_control_plane``): the server name must be a
    control plane, the binary must be the managed invocation's by real path, the
    args must match exactly, the child env must carry no loader or ``PYTHON*``
    overlay, and no import root may shadow ``kiro_crew``. The server NAME alone is
    never trusted.

    The env it judges is the one the child really gets, with exactly one
    allowance: Kiro Crew's own UTF-8 pinning (``platform_compat._UTF8_PROCESS_ENV``)
    is dropped when its value is the pinned one. The runtime sets those two
    ``PYTHON*`` keys on every process it starts, so they reach this stub from
    kiro-cli, and gatewayd likewise applies them only after its verdict. Any
    other value for those keys still counts. The cwd judged is this process's,
    because ``exec`` inherits it.

    Fails closed: any error means "not ours", which is the behaviour before this
    check existed.

    A refusal leaves a record here, on the stub's own stderr: a reserved name
    that loses the token presents later only as every policy-reading tool
    answering ``identity_unattested``, and the vetting's own log line lands in
    the daemon's logger wording ("spawned ... will answer 403"), which does not
    name this fallback. A third-party target is the ordinary answer and stays
    silent. Neither line ever carries the token or an env value.

    The name is tested FIRST, against the leaf ``KIROCREW_BIN_MCP_SERVERS`` the
    daemon's own ``CONTROL_PLANE_BACKENDS`` is built from, and only a Crew name
    reaches the import below. The fallback runs for EVERY stubbed server while
    gatewayd is down, and the token is always in a stub's env, so importing
    gatewayd here unconditionally would pull its whole module graph (and run its
    module-level SSL setup) into every third-party fallback that the vetting
    then refuses on its first line anyway.
    """

    server_name = str(getattr(args, "server", "") or "")
    if server_name not in KIROCREW_BIN_MCP_SERVERS:
        return False
    try:
        # circular import: gatewayd imports this stub; defer its heavy daemon
        # dependencies to the rare fallback path, off the timed cold start.
        from kiro_crew.mcp_gateway.gatewayd import _spawns_own_control_plane
        from kiro_crew.platform_compat import _UTF8_PROCESS_ENV

        verdict_env = {
            key: value
            for key, value in exec_env.items()
            if key != STUB_SESSION_TOKEN_ENV and _UTF8_PROCESS_ENV.get(key) != value
        }
        denial: list[str] = []
        ours = bool(
            _spawns_own_control_plane(
                server_name,
                str(getattr(args, "target_command", "") or ""),
                list(target_args),
                env=verdict_env,
                work_dir=os.getcwd(),
                denial=denial,
            )
        )
    except Exception:
        # The vetting itself broke (an import, a signature, a config read), which
        # is not a verdict: say so above DEBUG, or a drift here silently returns
        # every control-plane fallback to the tokenless behaviour this fixed.
        logger.warning(
            "fallback: control-plane vetting of %r raised; dropping the session token",
            server_name,
            exc_info=True,
        )
        return False
    if not ours:
        # Only a Crew name reaches here, so a False verdict is a refusal of a
        # reserved name, never the ordinary third-party answer.
        logger.warning(
            "fallback: %r is a control-plane name but gatewayd's vetting refused it, so "
            "the direct backend runs without the session token (%s); every tool that "
            "reads the session's policy will answer identity_unattested",
            server_name,
            "; ".join(denial) or "no reason recorded",
        )
    return ours


def _fallback_user_site_holds_our_package() -> bool:
    """gatewayd's user-site probe, failing toward disabling user-site."""
    try:
        # circular import: gatewayd imports this stub; defer its heavy daemon
        # dependencies to the rare fallback path, off the timed cold start.
        from kiro_crew.mcp_gateway.gatewayd import _user_site_holds_our_package

        return bool(_user_site_holds_our_package())
    except Exception:
        return False


#: Exit status of a fallback that refuses to start the backend because its env
#: still holds a ``secret://`` reference. ``EX_CONFIG`` from sysexits: the
#: operator's configuration asks for something this path cannot provide.
_EXIT_UNRESOLVED_SECRET = 78


def _refuse_unresolved_secret_references(
    args: argparse.Namespace, declared_env: Mapping[str, str]
) -> None:
    """Exit instead of exec'ing a backend whose env carries a ``secret://`` value.

    The sidecar keeps the raw reference on purpose -- the value is resolved from
    the vault only on the gateway's backend spawn. This direct exec runs in the
    session's own process tree, which cannot read the vault, so exec'ing would
    start the server with the literal reference as its credential: the server
    comes up and every authenticated call fails, which is exactly the outcome the
    reference promises not to produce. Refusing the start is the documented
    behaviour for a reference that cannot be resolved.

    Names only the env KEYS, never a value or the secret's name, for the reason
    given in :mod:`kiro_crew.mcp_gateway.secret_uri`.
    """
    # Deferred: the module imports the vault, which only this rare path needs.
    from kiro_crew.mcp_gateway.secret_uri import secret_reference_keys

    keys = secret_reference_keys(declared_env)
    if not keys:
        return
    message = (
        f"MCP server {getattr(args, 'server', '') or '?'!r} was not started: env "
        f"{', '.join(repr(k) for k in keys)} holds a secret:// reference, which is "
        "resolved only when the gateway starts the server, and this session fell "
        "back to starting it directly. Start a new session once the gateway is "
        "serving it; `kirocrew doctor` reports the gateway's state."
    )
    logger.error("fallback: %s", message)
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(_EXIT_UNRESOLVED_SECRET)


def fallback_exec(args: argparse.Namespace) -> None:
    """Replace the current process with the real MCP backend. ``execvpe``
    never returns on success; a return raises so the caller surfaces a
    diagnostic. Windows has no in-place exec, so there the backend runs as a
    child inheriting this process's stdio -- see :func:`_fallback_spawn_child`
    for why the emulated ``exec*`` would kill the session outright."""
    target_args = _resolve_target_args(args)
    argv = [args.target_command, *target_args]
    # Restore the server's declared env. The rewriter moves declared env
    # (which routinely holds tokens / API keys) into a 0600 sidecar the stub
    # otherwise reads only for PoolKey hashing. On this fallback path we exec
    # the real backend directly, so it must run with its declared env to match
    # the non-pooled baseline — the daemon's own environment lacks it.
    session_token = os.environ.get(STUB_SESSION_TOKEN_ENV, "")
    exec_env = dict(os.environ)
    declared_env = _parse_env_file(getattr(args, "env_file", "") or "")
    # Only the DECLARED env is checked: an unrelated inherited value must not
    # cost a working fallback.
    _refuse_unresolved_secret_references(args, declared_env)
    exec_env.update(declared_env)
    # The token is a bearer name for this session's identity. A third-party
    # server binary must never inherit it: on a later gateway start it could
    # register with it and be answered as this session. Kiro Crew's OWN control
    # plane is the opposite case: the token exists for it, and without it every
    # tool call that reads the session's policy is refused identity_unattested
    # wherever the kernel peer check cannot name the session (a runtime hosting
    # several sessions, or a TCP connection). So keep it only when the target
    # passes the same vetting gatewayd applies before it hands a pooled backend
    # the token, and drop it for anything else.
    exec_env.pop(STUB_SESSION_TOKEN_ENV, None)
    if session_token and _fallback_target_is_own_control_plane(args, target_args, exec_env):
        exec_env[STUB_SESSION_TOKEN_ENV] = session_token
        # Same defense in depth gatewayd applies to an accepted control plane.
        exec_env["PYTHONSAFEPATH"] = "1"
        if not _fallback_user_site_holds_our_package():
            exec_env["PYTHONNOUSERSITE"] = "1"
    if platform_compat.IS_WINDOWS:
        _fallback_spawn_child(argv, exec_env)
    # exec IS this fallback stub's whole purpose: when the gateway is
    # unavailable, replace this process with the operator's real MCP backend.
    # argv (target_command / target_args) and exec_env (the server's declared
    # env) both originate from the operator's own ~/.kiro/agents/*.json via the
    # rewriter — never from a peer or stub — so the tainted-input audit rule is
    # a false positive under this threat model.
    # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
    os.execvpe(argv[0], argv, exec_env)
    raise RuntimeError(f"execvpe({argv[0]!r}) returned unexpectedly")
