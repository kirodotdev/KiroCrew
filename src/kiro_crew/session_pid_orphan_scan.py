"""Finding untracked orphans: processes Kiro Crew spawned that no tracking record names.

The positive-identity predicate of each orphan class (MCP server, unreachable gatewayd,
browser daemon, work process), the environ markers they read, the reparented-pid walk,
the tracked-pid snapshot and the report of untracked managed runtimes, and the find phase
of the orphan sweep, :func:`find_orphan_mcp_candidates`.

Split out of ``kiro_crew.session_pid``, which stays the import path and the patch target:
every name here is reached as ``kiro_crew.session_pid.<name>``, and a patch through that
module reaches this one (``kiro_crew._facade_mirror``). Nothing here imports it.
"""

from __future__ import annotations

import errno
import logging
import os
import subprocess
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.constants import (
    KIROCREW_SANDBOX_TOOL_ENV,
    KIROCREW_SANDBOX_TOOL_VALUE,
    KIROCREW_SPAWN_HOME_ENV,
    KIROCREW_SPAWN_INSTANCE_ENV,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.session_pid_harness import (
    _MANAGED_AGENT_RUNTIME_BASENAMES,
    _argv_tokens,
    _basename_names_a_harness,
    _work_orphan_basename,
)
from kiro_crew.session_pid_tracking import (
    _pid_age_seconds,
    _pid_file_path,
    _read_pid_file_text,
    _session_pid_file_path,
)

#: The family logs as ``kiro_crew.session_pid``, the name callers, log filters and
#: ``caplog`` assertions use.
logger = logging.getLogger("kiro_crew.session_pid")


# ── Untracked orphan MCP sweep (defense-in-depth) ──────────
# Catches MCP subtrees Kiro Crew spawned that escaped PID-file tracking.
# Split into find + kill so the caller can re-verify active PIDs between phases.

_ORPHAN_SWEEP_MAX_KILLS = 30


_ORPHAN_MIN_AGE_SECONDS = 120  # Never reap processes younger than this

# Dedicated, more conservative age floor for the WORK-process orphan class
# (agent-spawned pytest/build/shim subtrees identified purely by the
# KIROCREW_SPAWNED environ marker — see _is_sweepable_orphan_work). Work
# processes get a much more generous grace than the 120s MCP floor: a
# long-running legitimate build or test run whose agent briefly detaches must
# not be raced, and the floor also guarantees a just-detached spawn is never
# swept before its agent could have re-attached tracking.
_ORPHAN_WORK_MIN_AGE_SECONDS = 600

# Execnet's popen-worker bootstrap: the single ``-c`` payload pytest-xdist
# workers run under (verified empirically against pytest-xdist 3.x). Matched
# as an EXACT argv element, never as a substring.
_XDIST_BOOTSTRAP = b"import sys;exec(eval(sys.stdin.readline()))"


def _work_sweep_cmdline_is_test_runner(cmdline: bytes) -> bool:
    """Structural test-runner match on parsed argv — never substring.

    A test runner is never a legitimate long-lived daemon, unlike other
    marked-but-detached processes an agent may deliberately leave running
    (a preview server, for instance) — so this is the positive shape gate for
    the work-orphan sweep. Matching is structural to avoid path-fragment
    false positives (``node /work/pytest-dashboard/server.js`` must NOT
    match). Exactly three shapes qualify:

    * argv0 basename is exactly ``pytest`` (a venv console script), or
    * an adjacent ``-m pytest`` argument pair (``python -m pytest ...``), or
    * a ``-c`` argument whose payload is exactly execnet's worker bootstrap
      (:data:`_XDIST_BOOTSTRAP`).
    """
    args = [a for a in cmdline.split(b"\x00") if a]
    if len(args) <= 1:
        # Space-joined fallback (ps output); NUL-split is canonical on Linux.
        args = [a for a in cmdline.split(b" ") if a]
    if not args:
        return False
    if args[0].rsplit(b"/", 1)[-1] == b"pytest":
        return True
    for i in range(len(args) - 1):
        if args[i] == b"-m" and args[i + 1] == b"pytest":
            return True
        if args[i] == b"-c" and args[i + 1] == _XDIST_BOOTSTRAP:
            return True
    return False


# A candidate PID can exit between the /proc (or ps) snapshot and the per-PID
# probe. Linux surfaces that as FileNotFoundError/ProcessLookupError reading
# /proc/<pid>/cmdline; macOS as a non-zero `ps -p <pid>` exit. All three mean
# "already gone", which is the sweep's goal — not a failure worth a traceback.
_PID_VANISHED_ERRORS = (
    FileNotFoundError,
    ProcessLookupError,
    subprocess.CalledProcessError,
)

#: Must track :data:`kiro_crew.sandbox._SANDBOX_ARTIFACT_PREFIX`. Spelled as a literal
#: rather than read through the accessor the managed-agent gate uses, because the tuple
#: below is a MODULE CONSTANT and reading it here would put a ``sandbox`` import at
#: ``session_pid`` import time -- the one thing :func:`_namespace_launcher_shape` exists
#: to avoid. Same trade :data:`_BROWSER_SESSION_ENV` makes, and pinned the same way, by
#: ``test_the_mcp_marker_tracks_the_sandbox_artifact_prefix``.
_SANDBOX_LAUNCHER_MARKER = b"kirocrew_sandbox_"

# Entrypoints that positively identify an MCP/worker process Kiro Crew spawned.
# Each marker MUST be unique to a process Kiro Crew itself launches — the sweep
# SIGKILLs any user-owned orphan that matches, so a marker naming a server the
# core does not spawn would reap an unrelated process. The upstream project's
# reaper also lists an enterprise-only MCP server it manages, but this public
# fork never spawns that server (the CPP companion contributes it, not the
# core), so that marker is deliberately omitted here.
_MCP_ENTRYPOINT_MARKERS = (
    _SANDBOX_LAUNCHER_MARKER,  # sandbox wrapper script (session-spawned)
    b"kiro_crew.mcp_gateway.stub",  # gateway pool worker (not gatewayd itself)
)

# Gateway/CLI entrypoints — these are peer gateways, never orphan MCP targets.
# Checked BEFORE _MCP_ENTRYPOINT_MARKERS to prevent prefix overlap.
_GATEWAYD_MODULE = b"kiro_crew.mcp_gateway.gatewayd"


_GATEWAY_MARKERS = (
    _GATEWAYD_MODULE,
    b"kiro_crew.cli",
    b"kiro_crew.__main__",
)

# MCP launcher cmdline shapes that carry NO Kiro Crew fingerprint (a user's own
# shell can produce identical cmdlines), so matching them requires the
# ``KIROCREW_SPAWNED`` environ marker as positive identity — the public fork's
# only fingerprint-less launcher is the public ``@playwright/mcp`` server, which
# runs as ``npx @playwright/mcp`` -> node (see ``mcp_playwright_proxy``): neither
# its argv0 (``npx``/``node``) nor its args mention Kiro Crew, so a grandchild
# escaping the probe/session tree evades the cmdline-fingerprint sweep entirely.
_MARKED_MCP_LAUNCHER_MARKERS = (
    b"@playwright/mcp",  # ``npx @playwright/mcp`` (npx shim + node server)
    b"mcp start-server",  # generic ``<launcher> mcp start-server <name>`` shims
)

# ── Stranded playwright-cli browser daemon ───────────────────────────────────
# playwright-core spawns its browser daemon as
#   ``node <...>/playwright-core/lib/entry/cliDaemon.js <session-name> [flags]``
# with ``detached: true`` and no ``env`` override (cli-client/session.js
# ``startDaemon``). Two consequences make it its own orphan class:
#
# * detached => it is its own SESSION and PROCESS-GROUP leader, so it is
#   invisible to the teardown child snapshot, to ``kill_process_tree``, and to
#   the SID-based ownership test the work class uses (its SID is its own pid).
# * no env override => it inherits the spawning agent's environment verbatim,
#   so its EXEC-TIME environ carries both ``KIROCREW_SPAWNED`` and the
#   generated ``PLAYWRIGHT_CLI_SESSION``. Exec-time environ is kernel-held and
#   immutable after exec, so it is ownership evidence no process can forge for
#   another -- unlike any on-disk registry or claim file, which a same-UID
#   agent can write.
#
# Deliberately NOT keyed on the socket path the way the gatewayd class is:
# ``Session._connect`` UNLINKS the socket whenever a connect fails, so an
# absent socket means "a client already cleaned up after a refused connect",
# not "the daemon is unreachable" -- and the daemon holds its listening fd
# regardless, so absence proves nothing about the browser tree.
_BROWSER_DAEMON_ENTRY = b"cliDaemon.js"

#: Must track :data:`kiro_crew.browser_cli.launch.SESSION_ENV`. Duplicated as a
#: literal rather than imported because ``session_pid`` is imported early by
#: ``acp.runtime`` and must not pull the browser package's import graph onto
#: that path; a test asserts the two stay equal.
_BROWSER_SESSION_ENV = "PLAYWRIGHT_CLI_SESSION"

#: Generated-session prefix from ``browser_cli.launch`` (``kc-<8hex>``).
_BROWSER_SESSION_PREFIX = b"kc-"


def _is_generated_browser_session(name: bytes) -> bool:
    """True for a Kiro-Crew-generated ``kc-<8hex>`` session name.

    Mirrors ``browser_cli.launch._session_leaf``. ONLY generated names are
    ever sweepable: an operator who named a session (``default``, ``chrome``,
    an ``attach`` workflow) owns its lifetime, and the ``kc-`` prefix is
    reserved precisely so the two populations cannot be confused.
    """
    if not name.startswith(_BROWSER_SESSION_PREFIX):
        return False
    leaf = name[len(_BROWSER_SESSION_PREFIX) :]
    return len(leaf) == 8 and all(c in b"0123456789abcdef" for c in leaf)


def _browser_daemon_session_arg(cmdline: bytes) -> bytes | None:
    """Generated session name from a cliDaemon cmdline, or ``None``.

    The daemon's argv is ``node <entry>/cliDaemon.js <session-name> [flags]``,
    so the name is the element immediately following the entry script --
    matched on the script's BASENAME so an npx/global/vendored install path
    all resolve. NUL-separated argv ONLY: the space-joined ``ps`` fallback
    cannot delimit a path containing spaces, and a mis-split argv could pair
    the script with the wrong token, so anything without NULs fails closed.
    """
    args = [a for a in cmdline.split(b"\x00") if a]
    if len(args) <= 1:
        return None
    for index, arg in enumerate(args[:-1]):
        if arg.rsplit(b"/", 1)[-1] == _BROWSER_DAEMON_ENTRY:
            name = args[index + 1]
            return name if _is_generated_browser_session(name) else None
    return None


def _env_value(pid: int, key: str, proc_root: Path | None = None) -> bytes | None:
    """Exec-time environment value for *key* in *pid*, or ``None`` if unset.

    Deliberately PROPAGATES ``OSError`` instead of swallowing it like
    :func:`_env_has_kirocrew_marker`: the callers here need to tell "read
    said the key is absent" apart from "the read failed", because those two
    outcomes must fail closed in OPPOSITE directions -- an absent owner
    permits a kill, an unreadable one normally forbids it. Linux-only; returns
    ``None`` elsewhere so every caller fails closed off Linux. *proc_root* is a
    fixture seam for tests and never changes the production ``/proc`` root.
    """
    if sys.platform != "linux":
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    prefix = key.encode() + b"="
    environ = (root / str(pid) / "environ").read_bytes()
    for item in environ.split(b"\x00"):
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


_BROWSER_PLAUSIBLE_OWNER_NAMES = frozenset(
    {
        "bash",
        "claude",
        "codex",
        "dash",
        "fish",
        "java",
        "kas",
        "kiro-cli",
        "kirocrew",
        "launcher",
        "node",
        "opencode",
        "playwright-cli",
        "sh",
        "zsh",
    }
)


_BROWSER_PLAUSIBLE_OWNER_PREFIXES = ("chrome", "chromium", "kiro-", "playwright", "python")


def _browser_process_name_is_plausible(name: str) -> bool:
    """Whether an unreadable process could own generated browser tooling."""
    return name in _BROWSER_PLAUSIBLE_OWNER_NAMES or name.startswith(
        _BROWSER_PLAUSIBLE_OWNER_PREFIXES
    )


def _browser_session_owner_alive(
    pid: int,
    session: bytes,
    *,
    proc_root: Path | None = None,
) -> bool:
    """True while any live process OUTSIDE *pid*'s own tree holds *session*.

    This is the ownership proof, and it is drawn entirely from the kernel.
    Kiro Crew injects the generated ``PLAYWRIGHT_CLI_SESSION`` into exactly
    one spawned agent process, so that value in a live process's EXEC-TIME
    environ means the browser still has an owner. Scanning the whole process
    table (not a manager-local set) is what makes a peer gateway sharing this
    data home see its own live sessions and protect them.

    The daemon's own tree is excluded by SID: it is spawned ``detached``, so
    it is its own session leader and every Chromium child inherits that SID --
    those inherit the variable too and must not be mistaken for owners.

    FAIL-CLOSED to "alive" for an unreadable ``/proc`` listing, process stat,
    process name, or plausible owner's environ. A positively named non-owner in
    a stable different cgroup from the daemon cannot be part of the spawn tree
    that inherited its generated browser session, so its unreadable environ
    does not veto the scan. A plausible process, same cgroup, unreadable cgroup,
    or changing cgroup keeps the daemon. A process that vanishes is safe to skip.
    """
    if sys.platform != "linux":
        return True
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        entries = [entry for entry in root.iterdir() if entry.name.isdigit()]
    except OSError:
        return True
    my_uid = os.getuid()
    for entry in entries:
        try:
            other = int(entry.name)
        except ValueError:
            continue
        if other == pid:
            continue
        try:
            if entry.stat().st_uid != my_uid:
                continue
        except _PID_VANISHED_ERRORS:
            continue
        except OSError:
            return True
        if _linux_pid_sid(other, root) == pid:
            continue  # the daemon's own detached tree, not an owner
        try:
            owner_session = _env_value(other, _BROWSER_SESSION_ENV, root)
            if owner_session == session:
                logger.debug(
                    "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                    "decision=keep reason=matching_session",
                    pid,
                    other,
                )
                return True
        except _PID_VANISHED_ERRORS:
            continue
        except OSError:
            process_name = platform_compat.linux_process_name(other, proc_root=root)
            if process_name is None:
                logger.debug(
                    "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                    "decision=keep reason=process_name_unreadable",
                    pid,
                    other,
                )
                return True
            if _browser_process_name_is_plausible(process_name):
                logger.debug(
                    "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                    "decision=keep reason=plausible_owner_unreadable",
                    pid,
                    other,
                )
                return True
            cgroups_match = platform_compat.process_cgroups_match(
                other,
                pid,
                proc_root=root,
            )
            if cgroups_match is False:
                logger.debug(
                    "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                    "decision=ignore reason=different_cgroup_unreadable",
                    pid,
                    other,
                )
                continue
            reason = (
                "same_cgroup_unreadable" if cgroups_match is True else "cgroup_identity_unreadable"
            )
            logger.debug(
                "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                "decision=keep reason=%s",
                pid,
                other,
                reason,
            )
            return True
    return False


def _browser_sweep_decision(pid: int, *, sweep: bool, reason: str) -> bool:
    """Log and return one structured browser-daemon sweep verdict."""
    logger.debug(
        "browser_daemon_sweep pid=%s decision=%s reason=%s",
        pid,
        "sweep" if sweep else "keep",
        reason,
    )
    return sweep


def _is_sweepable_orphan_browser_daemon(
    pid: int,
    cmdline: bytes,
    age_seconds: float,
    *,
    proc_root: Path | None = None,
) -> bool:
    """Fifth positive-identity path: a browser daemon whose owner is gone.

    Positive identity is the conjunction of:

    1. a structural cliDaemon argv carrying a GENERATED ``kc-<8hex>`` session
       name (:func:`_browser_daemon_session_arg`, NUL-argv only) -- an
       operator-named session is structurally excluded and never signalled;
    2. that same name in the process's exec-time environ, which ties this
       daemon to a name Kiro Crew itself generated rather than one an agent
       passed with ``-s=``;
    3. the ``KIROCREW_SPAWNED`` environ marker, proving Kiro Crew spawned the
       tree (:func:`_env_has_kirocrew_marker`, Linux-only, fail-closed);
    4. NO live process outside the daemon's own tree still holding that
       session (:func:`_browser_session_owner_alive`);
    5. age past :data:`_ORPHAN_WORK_MIN_AGE_SECONDS` -- the generous work-class
       floor, not the 120s MCP one, so a daemon whose agent is mid-spawn or
       briefly detached is never raced.

    Every signal is a kernel fact (argv, exec-time environ, SID, process
    liveness). Nothing here reads agent-writable filesystem state, which is
    what would make a reaper unsafe. *proc_root* exists only for fixture-owned
    process-table tests.
    """
    if not cmdline:
        return False  # kernel thread / zombie -- nothing meaningful to kill
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    session = _browser_daemon_session_arg(cmdline)
    if session is None:
        return False
    if age_seconds < _ORPHAN_WORK_MIN_AGE_SECONDS:
        return _browser_sweep_decision(pid, sweep=False, reason="below_age_floor")
    try:
        daemon_session = _env_value(pid, _BROWSER_SESSION_ENV, proc_root)
        if daemon_session != session:
            return _browser_sweep_decision(
                pid,
                sweep=False,
                reason="session_environment_mismatch",
            )
    except OSError:
        return _browser_sweep_decision(
            pid,
            sweep=False,
            reason="daemon_environment_unreadable",
        )
    if not _env_has_kirocrew_marker(pid, proc_root):
        return _browser_sweep_decision(pid, sweep=False, reason="spawn_marker_absent")
    if _browser_session_owner_alive(pid, session, proc_root=proc_root):
        return _browser_sweep_decision(
            pid,
            sweep=False,
            reason="owner_alive_or_inconclusive",
        )
    return _browser_sweep_decision(
        pid,
        sweep=True,
        reason="no_owner_outside_daemon_tree",
    )


def _accepted_subreaper_pids() -> set[int]:
    """PIDs an orphan may legitimately reparent to: init plus same-uid systemd.

    An orphaned process reparents to init (pid 1) or the nearest subreaper --
    under a ``systemd --user`` gateway that is the user manager process, not
    pid 1. On Linux this scans ``/proc`` for same-uid processes whose comm is
    ``systemd``; elsewhere only init/launchd (pid 1) is a reparent target.
    Single source of truth shared by :func:`_our_orphan_pids` and the PID-reuse
    guard in :func:`_cleanup_orphaned_mcp_servers`, so the two reapers agree on
    what an orphan's parent may look like -- a guard accepting only pid 1 would
    misread a systemd-reparented orphan as PID reuse and prune it without
    killing.

    We deliberately do NOT include the gateway's launcher ppid: doing so would
    widen the candidate set to the launcher's other live children (peer
    processes from the same shell/tmux/supervisor), adding wrong-kill surface
    with no orphan-reaping benefit.
    """
    accepted: set[int] = {1}
    if sys.platform != "linux":
        return accepted
    try:
        my_uid = os.getuid()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != my_uid:
                    continue
                # Detect systemd --user (user-session subreaper). Compared as
                # bytes: a name is whatever bytes its process chose.
                if (entry / "comm").read_bytes().strip() == b"systemd":
                    accepted.add(int(entry.name))
            except (OSError, ValueError):
                continue
    except Exception:
        # Callers include the startup sweep, which has no catch-all of its
        # own: degrade to the init-only set rather than aborting the sweep.
        logger.warning("_accepted_subreaper_pids /proc scan failed", exc_info=True)
    return accepted


def _our_orphan_pids() -> list[int]:
    """PIDs owned by current user whose parent is init (pid 1) or systemd --user.

    POSIX-only: relies on ``os.getuid`` and either ``/proc`` (Linux) or ``ps``
    (macOS). On Windows there is no init/systemd concept and no ``os.getuid``;
    the orphan-sweep is inactive there and returns an empty list.
    """
    if platform_compat.IS_WINDOWS:
        return []
    my_uid = os.getuid()
    # Pass 1 detects the accepted reparent targets (init + systemd --user
    # subreapers); pass 2 classifies orphans (needs the complete subreaper set
    # before any child can be matched against accepted_ppids).
    accepted_ppids = _accepted_subreaper_pids()
    try:
        if sys.platform == "linux":
            result: list[int] = []
            proc = Path("/proc")
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != my_uid:
                        continue
                except OSError:
                    continue
                pid = int(entry.name)
                stat = platform_compat.read_proc_stat(pid, proc_root=proc)
                if stat is not None and stat.ppid in accepted_ppids:
                    result.append(pid)
            return result
        else:
            result = []
            out = subprocess.check_output(
                ["ps", "-o", "pid=,ppid=", "-U", str(my_uid)],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            for ln in out.decode().splitlines():
                parts = ln.split()
                if len(parts) == 2 and parts[0].isdigit():
                    pid, ppid = int(parts[0]), int(parts[1])
                    if ppid in accepted_ppids:
                        result.append(pid)
            return result
    except Exception:
        logger.warning("_our_orphan_pids failed", exc_info=True)
    return []


def _is_orphan_mcp(cmdline: bytes) -> bool:
    """True if cmdline matches a Kiro Crew MCP entrypoint (not a peer gateway)."""
    # Exclude peer gateways — they're not orphan MCP targets
    if any(marker in cmdline for marker in _GATEWAY_MARKERS):
        return False
    # Parse argv: null-separated on Linux, space-separated on macOS ps output
    args = cmdline.split(b"\x00")
    if len(args) == 1:
        args = cmdline.split(b" ")
    argv0 = args[0].rsplit(b"/", 1)[-1]
    # A sandbox/worker script exec'd directly via its shebang puts the script
    # (not a python interpreter) in argv0 — match the marker there too so such
    # orphans aren't missed.
    if any(marker in argv0 for marker in _MCP_ENTRYPOINT_MARKERS):
        return True
    # Otherwise require python interpreter + known entrypoint in remaining args
    if b"python" not in argv0:
        return False
    return any(any(marker in a for marker in _MCP_ENTRYPOINT_MARKERS) for a in args[1:])


def _is_marked_mcp_launcher(cmdline: bytes) -> bool:
    """True if cmdline looks like a fingerprint-less MCP launcher (e.g. ``npx``).

    NOT sufficient on its own — the caller MUST pair this with
    :func:`_env_has_kirocrew_marker` because a user's own shell produces
    identical cmdlines. NULs are normalized to spaces first so the multi-token
    markers match both the Linux NUL-separated ``/proc`` form and the macOS
    space-separated ``ps`` form.
    """
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    return any(marker in normalized for marker in _MARKED_MCP_LAUNCHER_MARKERS)


def _read_env_has_kirocrew_marker(pid: int, proc_root: Path | None = None) -> bool | None:
    """Tri-state read of *pid*'s ``KIROCREW_SPAWNED`` environment marker.

    ``None`` distinguishes an unreadable environment from a readable one that
    lacks the marker. An explicit *proc_root* permits fixture-owned process
    tables on every host and always takes the ``/proc`` path, so a fixture's
    verdict never depends on the host it runs on.

    Two production arms, one per platform that HAS a same-uid environ oracle:

    * Linux reads ``/proc/<pid>/environ``.
    * macOS reads the same exec-time environment out of ``sysctl
      KERN_PROCARGS2`` (:func:`platform_compat.darwin_process_environ`) -- the
      kernel record ``ps -E`` reads, answered for a same-uid process with no
      entitlement and no elevated privilege, and already relied on in this
      codebase for argv.

    Both are in-process kernel reads of a copy fixed at exec, which is what
    makes the marker ownership evidence rather than a claim: a same-uid process
    can write any file and set any argv, but it cannot alter another process's
    exec-time environment. Every other platform has no such oracle and stays
    ``None``, which the boolean wrapper turns into a refusal.
    """
    needle = f"{KIROCREW_SPAWNED_ENV}={KIROCREW_SPAWNED_VALUE}".encode()
    if proc_root is None:
        if sys.platform == "darwin":
            entries = platform_compat.darwin_process_environ(pid)
            return None if entries is None else needle in entries
        if sys.platform != "linux":
            return None
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        environ = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    return needle in environ.split(b"\x00")


def _env_spawn_instance(pid: int, proc_root: Path | None = None) -> str | None:
    """*pid*'s ``KIROCREW_SPAWN_INSTANCE``, or ``None`` when absent or unreadable.

    Same read as :func:`_read_env_has_kirocrew_marker`, same Linux-only,
    fail-closed posture. The value is the per-spawn token the runtime put on its
    root's environment; every descendant inherits it, and a runtime spawned
    later carries a different one -- which is the whole point of reading it.
    """
    if sys.platform != "linux" and proc_root is None:
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    prefix = f"{KIROCREW_SPAWN_INSTANCE_ENV}=".encode()
    try:
        environ = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for entry in environ.split(b"\x00"):
        if entry.startswith(prefix):
            value = entry[len(prefix) :]
            return value.decode("ascii", "replace") if value else None
    return None


def _env_spawn_home(pid: int, proc_root: Path | None = None) -> str | None:
    """*pid*'s ``KIROCREW_SPAWN_HOME``, or ``None`` when absent or unreadable. Linux only."""
    if sys.platform != "linux" and proc_root is None:
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    prefix = f"{KIROCREW_SPAWN_HOME_ENV}=".encode()
    try:
        environ = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for entry in environ.split(b"\x00"):
        if entry.startswith(prefix):
            value = entry[len(prefix) :]
            return value.decode("utf-8", "replace") if value else None
    return None


def _env_is_sandbox_tool(pid: int, proc_root: Path | None = None) -> bool | None:
    """Tri-state read of *pid*'s ``KIROCREW_SANDBOX_TOOL`` environment marker.

    The marker ``sandbox.sandboxed_spawn_argv`` stamps on every tree it spawns -- a
    build, an ``npx`` install, a ``git``/``gh`` read, a provisioning run -- and which
    that tree inherits, so it answers ``True`` for a descendant no spawn recorded.
    Same read, same two production arms and the same tri-state contract as
    :func:`_read_env_has_kirocrew_marker`: ``/proc/<pid>/environ`` on Linux, ``sysctl
    KERN_PROCARGS2`` on macOS, and ``None`` on every platform with no same-uid environ
    oracle.

    What ``None`` buys is the same thing it buys there: an unreadable environment is
    told apart from a readable one lacking the marker, so a caller can decline to act
    on doubt. The two callers want OPPOSITE things from doubt, which is why this stays
    tri-state rather than collapsing here. A caller reading this marker to grant a kill
    would have to treat ``None`` as "not marked"; the reconciler reads it to WITHHOLD
    one, so ``None`` must not withhold -- a pid whose marker cannot be established
    stays in the candidate population under the ownership, argv and age conditions.

    *proc_root* is the fixture seam: an explicit value always takes the ``/proc`` path,
    so a test's verdict never depends on the host it runs on.
    """
    needle = f"{KIROCREW_SANDBOX_TOOL_ENV}={KIROCREW_SANDBOX_TOOL_VALUE}".encode()
    if proc_root is None:
        if sys.platform == "darwin":
            entries = platform_compat.darwin_process_environ(pid)
            return None if entries is None else needle in entries
        if sys.platform != "linux":
            return None
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        environ = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    return needle in environ.split(b"\x00")


def _env_has_kirocrew_marker(pid: int, proc_root: Path | None = None) -> bool:
    """True if *pid*'s environment carries the ``KIROCREW_SPAWNED`` marker.

    Reads the exec-time environment through :func:`_read_env_has_kirocrew_marker`
    (``/proc`` on Linux, ``sysctl KERN_PROCARGS2`` on macOS) and collapses its
    tri-state answer to a verdict. FAIL-CLOSED: any read failure, and every
    platform with no same-UID environ oracle — Windows — returns ``False``, so a
    sweep path that needs this marker never kills without positive identity.
    *proc_root* is a test seam for fixture-owned process tables.
    """
    return _read_env_has_kirocrew_marker(pid, proc_root) is True


def _is_sweepable_orphan_mcp(pid: int, cmdline: bytes) -> bool:
    """Positive-identity gate for the orphan sweep (find AND pre-kill re-verify).

    Two independent paths:
    1. cmdline carries a Kiro Crew fingerprint (:func:`_is_orphan_mcp`) —
       the pre-existing behavior, works on Linux and macOS.
    2. cmdline is a fingerprint-less MCP launcher shape AND the process
       environ carries the ``KIROCREW_SPAWNED`` marker (catches escaped
       ``npx @playwright/mcp`` and ``<launcher> mcp start-server`` trees).
       Needs a same-uid environ oracle, which Linux and macOS have and Windows
       does not, so this arm is fail-closed there.
    """
    if _is_orphan_mcp(cmdline):
        return True
    return _is_marked_mcp_launcher(cmdline) and _env_has_kirocrew_marker(pid)


def _gatewayd_socket_arg(cmdline: bytes) -> bytes | None:
    """Extract the ``--socket`` argument from a gatewayd cmdline, or ``None``.

    Accepts the two-token ``--socket <path>`` form (the shape every Kiro Crew
    spawn site produces) and the argparse-equivalent ``--socket=<path>``.
    NUL-separated argv ONLY: the space-joined ``ps`` fallback (macOS) cannot
    delimit a path containing spaces, and statting a truncated path would
    read as ENOENT — a wrong-kill — so anything without NULs fails closed.
    ABSOLUTE paths only, for the same reason: a relative path would be
    resolved against the SWEEPER's working directory, not the daemon's, so
    a reachable daemon bound to ``gw.sock`` in another cwd would read as
    ENOENT here. When the flag repeats, the LAST occurrence is returned —
    argparse binds last-wins, so that is the path the daemon actually
    created.
    """
    args = [a for a in cmdline.split(b"\x00") if a]
    if len(args) <= 1:
        return None
    candidate: bytes | None = None
    for i, arg in enumerate(args):
        if arg == b"--socket" and i + 1 < len(args):
            candidate = args[i + 1]
        elif arg.startswith(b"--socket="):
            candidate = arg[len(b"--socket=") :]
    if candidate is not None and os.path.isabs(os.fsdecode(candidate)):
        return candidate
    return None


def _is_sweepable_orphan_gatewayd(cmdline: bytes) -> bool:
    """Fourth positive-identity path: a gatewayd whose listening socket is gone.

    :data:`_GATEWAY_MARKERS` excludes gateway entrypoints from every other
    sweep path because the cmdline alone cannot distinguish a live dev pod's
    daemon from a dead launcher's. This path supplies the missing
    information: gatewayd creates the socket it is invoked with, and once
    that path is absent from disk no stub can ever connect to the daemon
    again — it is provably unreachable regardless of who launched it.

    Positive identity is the conjunction of:

    1. a structural ``-m kiro_crew.mcp_gateway.gatewayd`` argv pair — never
       ``kiro_crew.cli`` / ``kiro_crew.__main__``, which stay unconditionally
       excluded (they carry no socket argument and no equivalent
       reachability predicate);
    2. a ``--socket`` path in argv (:func:`_gatewayd_socket_arg`, NUL-argv
       only, fail-closed);
    3. that path absent from disk — ``ENOENT`` only; any other stat failure
       is inconclusive and fails closed.

    The callers preserve the rest of the sweep discipline: same-uid +
    reparented-to-init candidacy, the age floor, the kill budget, and
    re-verification immediately before signalling.
    """
    args = [a for a in cmdline.split(b"\x00") if a]
    is_gatewayd = any(
        args[i] == b"-m" and args[i + 1] == _GATEWAYD_MODULE for i in range(len(args) - 1)
    )
    if not is_gatewayd:
        return False
    sock = _gatewayd_socket_arg(cmdline)
    if sock is None:
        return False
    try:
        os.stat(os.fsdecode(sock))
    except FileNotFoundError:
        return True
    except OSError:
        return False  # inconclusive (EACCES, EIO, …) — fail closed
    return False


#: argv0 path SUFFIX of the credential helper the agent toolbox starts beside a
#: managed runtime to vend that runtime's credentials. Matched as a suffix, and
#: with the ``sandbox/`` parent component included, for two separate reasons: the
#: parent component distinguishes the toolbox's helper from a same-named binary of
#: the user's own somewhere else on PATH, and the toolbox VERSION sits in a path
#: component above ``sandbox/``, so a version never appears here and one spelling
#: covers every installed version at once.
_SANDBOX_CREDENTIAL_HELPER_ARGV0_SUFFIX = b"/sandbox/creds_agent"

#: The helper's own per-sandbox-session argument. Required alongside the argv0
#: suffix so a bare binary placed at a path ending that way does not present the
#: shape on argv0 alone.
_SANDBOX_CREDENTIAL_HELPER_ARG = b"--session-id"


def _is_marked_sandbox_credential_helper(cmdline: bytes) -> bool:
    """True if *cmdline* is the toolbox's sandbox credential helper.

    NOT sufficient on its own -- the caller MUST pair this with the
    ``KIROCREW_SPAWNED`` marker, as :func:`_is_marked_mcp_launcher` is paired,
    because the helper is the agent toolbox's own binary rather than one Kiro
    Crew spawns by name, and a user's own shell can start an identical one.

    Deliberately NOT one of :func:`_is_agent_runtime_anchor`'s identities. That
    anchor is existential -- one member authorizes a stop of the whole scope --
    and a helper shares its scope with whatever else the session left behind,
    including work a user meant to keep. The helper's only consumer is the scope
    reaper's UNIVERSAL rule, which asks whether every surviving member is one.
    """
    tokens = _argv_tokens(cmdline)
    if not tokens:
        return False
    if not tokens[0].endswith(_SANDBOX_CREDENTIAL_HELPER_ARGV0_SUFFIX):
        return False
    return _SANDBOX_CREDENTIAL_HELPER_ARG in tokens[1:]


def _is_agent_runtime_anchor(cmdline: bytes, *, has_kirocrew_marker: bool) -> bool:
    """True when *cmdline* positively identifies an agent-runtime tree member.

    This is the scope-reaper's authorization anchor, deliberately separate from
    marker/descent ownership. It recognizes the generated sandbox launcher and
    fingerprinted MCP workers through :func:`_is_orphan_mcp`, direct managed
    runtimes (``kiro-cli`` / ``kiro-cli-chat`` / ``claude-agent-acp`` /
    ``claude``) by exact argv0 basename, and the existing fingerprint-less MCP
    launcher shapes only when that member itself carries the Kiro Crew spawn
    marker. Peer gateway/CLI entrypoints are excluded.
    """
    if not cmdline:
        return False
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    if _is_orphan_mcp(cmdline):
        return True
    basename = _work_orphan_basename(cmdline)
    if basename in _MANAGED_AGENT_RUNTIME_BASENAMES:
        return True
    return has_kirocrew_marker and _is_marked_mcp_launcher(cmdline)


def _is_sweepable_orphan_work(pid: int, cmdline: bytes, age_seconds: float) -> bool:
    """Third positive-identity path: agent-spawned TEST-RUNNER process
    (pytest coordinator or pytest-xdist/execnet worker) that outlived its
    agent session.

    The positive identity is the conjunction of:

    1. A structural test-runner argv match
       (:func:`_work_sweep_cmdline_is_test_runner`). Test runners
       are never legitimate long-lived daemons, unlike other marked-but-
       detached processes an agent may deliberately leave running (a preview
       server started with ``start_new_session=True``, for instance) — those
       are intentional survivors and MUST NOT be swept, so a marker alone is
       not sufficient identity.
    2. The ``KIROCREW_SPAWNED`` environ marker (:func:`_env_has_kirocrew_marker`,
       Linux-only, fail-closed elsewhere). The marker is only ever injected
       into environments Kiro Crew itself spawns (sandbox wrapper, ACP client
       and runtime, MCP gateway backend) and is inherited by every descendant,
       so it can never identify a user-launched process.
    3. Reparenting to init/systemd --user — guaranteed by the caller, which
       only iterates :func:`_our_orphan_pids` — AND the owning session's
       LEADER being gone (:func:`_work_orphan_session_leader_alive`). Kiro
       Crew starts every agent runtime with ``start_new_session=True``, so
       the runtime is a session leader and every descendant inherits its SID
       — including through ``nohup`` and reparenting. A work process whose
       session leader still exists belongs to a LIVE agent session that may
       be polling its output (a backgrounded test run, for instance) and is
       never swept; only when the leader is gone has the owning session
       positively ended, making the run unreachable by any agent.
    4. Age above :data:`_ORPHAN_WORK_MIN_AGE_SECONDS` — deliberately much
       higher than the 120s MCP floor so a just-detached spawn is never raced
       and a slow-but-legitimate long test run gets generous grace.

    Two NEGATIVE gates keep the blast radius tight: managed agent runtimes
    (:data:`_MANAGED_AGENT_MARKERS`) stay owned by their own tracked-PID
    lifecycle, and gateway/CLI entrypoints (:data:`_GATEWAY_MARKERS`) are
    excluded so agent-launched peer gateways (e.g. dev pods) are never swept.
    """
    if age_seconds < _ORPHAN_WORK_MIN_AGE_SECONDS:
        return False
    if not cmdline:
        return False  # kernel thread / zombie — nothing meaningful to kill
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    if not _work_sweep_cmdline_is_test_runner(cmdline):
        return False
    basename = _work_orphan_basename(cmdline)
    if _basename_names_a_harness(basename):
        return False
    if _work_orphan_session_leader_alive(pid):
        return False  # owning agent session still live — a backgrounded run
    return _env_has_kirocrew_marker(pid)


# PIDs already reported by the untracked-runtime detector, so a persisting
# orphan costs ONE log line rather than one line per sweep. Replaced wholesale
# at the end of each scan with the set still detected, which bounds the set by
# the live orphan count and re-arms the report if the PID disappears and a
# later process reappears under the same detection.
_reported_untracked_agent_pids: set[int] = set()


# Per tracking file, the index of the colon-field naming the process a reaper
# actually TERMINATES for that entry. Only that field counts as tracked: the
# other one names the OWNER whose death makes the entry reapable
# (``_sweep_pid_entries`` skips a session entry while its gateway lives;
# ``_cleanup_orphaned_mcp_servers`` kills the child once its parent is gone), and
# an owner is never reclaimed *through* the entry that names it. Counting an
# owner field would let a stale entry whose owner has died and had its PID
# recycled silently suppress a genuine leak report — exactly the silence this
# field exists to prevent. A bare line names its own process, whichever file it
# is in.
_REAPABLE_PID_FIELD: tuple[tuple[str, int], ...] = (
    ("session", 1),  # kiro_session_pids.txt: <gateway_pid>:<child_pid>[:start-id]
    ("child", 0),  # kiro_pids.txt: <child_pid>:<parent_pid>[:start-id]
)


def _read_tracked_agent_pids() -> tuple[set[int], bool]:
    """Return the tracked PID snapshot and whether it is complete.

    Both files are read because a runtime absent from BOTH is exactly what
    :func:`_is_untracked_managed_agent_orphan` reports, and each reaper keys off
    only one of them. Within a line only the reapable field counts — see
    :data:`_REAPABLE_PID_FIELD` for which, and why the owner field does not. A
    session entry's third field is a start-time identity, numeric on Linux, and
    is never read as a PID.

    Reads remain lock-free. Readers cannot tear: rewrites
    go through :func:`_rewrite_pid_file` (temp file + rename, so a reader sees
    either the whole old or the whole new content) and tracking appends are
    single short lines. ``complete`` is false whenever a potential PID could
    have been dropped -- a reapable field that does not parse, a byte that was
    not UTF-8 in it included; a missing file is a complete empty contribution.
    """
    tracked: set[int] = set()
    complete = True
    paths = (_session_pid_file_path(), _pid_file_path())
    for path, (_label, reapable_index) in zip(paths, _REAPABLE_PID_FIELD):
        try:
            # Bytes no writer produces are damage: they fail the per-line int()
            # below and mark the snapshot incomplete instead of raising out of
            # every reaper that reads it.
            raw = _read_pid_file_text(path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                complete = False
            continue
        for line in raw.split():
            # A damaged byte gets the malformed-entry rule: in the reapable field
            # it fails the int() below; elsewhere the pid still counts as tracked.
            fields = line.split(":")
            index = 0 if len(fields) == 1 else reapable_index
            if index >= len(fields):
                complete = False
                continue
            try:
                value = int(fields[index])
            except ValueError:
                complete = False
                continue
            if value > 0:
                tracked.add(value)
    return tracked, complete


def _tracked_agent_pids() -> set[int]:
    """PIDs a reaper can terminate, preserving report-only fail-open behavior.

    Diagnostic callers intentionally accept a partial set. Any caller that can
    authorize a kill must use :func:`_read_tracked_agent_pids` and require its
    completeness flag.
    """
    tracked, _complete = _read_tracked_agent_pids()
    return tracked


def tracked_agent_pid_owners() -> dict[int, int]:
    """``{tracked pid: the pid that owns its registry entry}`` -- READ ONLY.

    A diagnostic accessor, added for :mod:`kiro_crew.diag.procs` so a process
    view does not have to re-spell this file format. It grants nothing and
    authorizes nothing: it neither writes, locks, signals, nor reports
    completeness, and no reaper consults it.

    The two files record opposite field orders (:data:`_REAPABLE_PID_FIELD`),
    and the owner is the field the reapers deliberately ignore: in
    ``kiro_session_pids.txt`` it is the GATEWAY that spawned the runtime, and in
    ``kiro_pids.txt`` it is the tracked PARENT the descendant hangs off. Both
    answer "who does this process belong to" for an operator reading a tree,
    which is why they are surfaced together here and nowhere else.

    Session entries win a collision: the two files share one number space, and
    the session entry is the one that names a gateway. A legacy single-field
    line records no owner at all and is skipped rather than given a fabricated
    one. This is a SUBTRACTIVE read in the same sense as
    :func:`_pid_start_token` -- a pid absent from the result means "no recorded
    owner", never "not ours".
    """
    owners: dict[int, int] = {}
    paths = (_session_pid_file_path(), _pid_file_path())
    for path, (_label, reapable_index) in zip(paths, _REAPABLE_PID_FIELD):
        try:
            raw = _read_pid_file_text(path)
        except OSError:
            continue
        owner_index = 1 - reapable_index
        for line in raw.split():
            fields = line.split(":")
            if len(fields) < 2:
                continue  # legacy bare-PID line: no owner recorded
            try:
                pid = int(fields[reapable_index])
                owner = int(fields[owner_index])
            except ValueError:
                continue
            if pid > 0 and owner > 0:
                owners.setdefault(pid, owner)
    return owners


def tracked_session_roots() -> dict[int, tuple[int, str | None]] | None:
    """``{runtime root pid: (gateway pid, recorded start token)}`` -- READ ONLY.

    The runtime roots ``kiro_session_pids.txt`` records: one per runtime a gateway
    spawned, with the gateway that wrote the entry and the start identity it
    recorded (``None`` for an entry written without one). A diagnostic accessor
    for the doctor's runtime census, so that report does not re-spell this file
    format. Like :func:`tracked_agent_pid_owners` it neither writes, locks,
    signals nor reports completeness, and no reaper consults it; a malformed line
    is skipped.

    ``None`` when the file exists and cannot be read, so a report can say the
    ownership is unknown rather than call every runtime untracked. A missing file
    is an empty registry.
    """
    try:
        raw = _read_pid_file_text(_session_pid_file_path())
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    roots: dict[int, tuple[int, str | None]] = {}
    for line in raw.split():
        fields = line.split(":")
        if len(fields) not in (2, 3):
            continue
        try:
            gateway, pid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if gateway > 0 and pid > 0:
            token = (fields[2] or None) if len(fields) == 3 else None
            roots.setdefault(pid, (gateway, token))
    return roots


def _is_untracked_managed_agent_orphan(pid: int, cmdline: bytes, tracked_pids: set[int]) -> bool:
    """REPORT-ONLY: a managed agent runtime that no reaper can reach.

    Every existing reaper declines this process, which is why a leaked runtime
    has no reproduction:

    * :func:`cleanup_orphaned_sessions` and :func:`_periodic_pid_sweep` iterate
      ``kiro_session_pids.txt`` and cannot see a PID the file never recorded.
    * :func:`_is_sweepable_orphan_mcp` declines it — a runtime argv is not an
      MCP entrypoint.
    * :func:`_is_sweepable_orphan_work` NEGATIVE-gates
      :data:`_MANAGED_AGENT_MARKERS` (runtimes are owned by their tracked-PID
      lifecycle, not by the marker sweep) and separately requires a test-runner
      argv.

    Positive identity is the conjunction of: reparenting to init/``systemd
    --user`` — guaranteed by the caller, which only iterates
    :func:`_our_orphan_pids`, so ownership is not re-derived here — an argv0
    basename naming a managed runtime (:data:`_MANAGED_AGENT_MARKERS`), the
    ``KIROCREW_SPAWNED`` environ marker (:func:`_env_has_kirocrew_marker`,
    Linux-only and fail-closed elsewhere), and absence from BOTH PID files
    (:func:`_tracked_agent_pids`). Peer gateways and CLIs
    (:data:`_GATEWAY_MARKERS`) are excluded: they are not agent runtimes and
    are never tracked as such.

    ARGV0 ONLY, deliberately, and NOT :func:`_cmdline_names_a_harness` — which means this
    predicate does not recognise Crew's own Linux sandbox launcher even though
    :func:`_is_managed_agent_process` does. The asymmetry is the point, because the
    question here is different: not "is this a managed runtime" but "is this one NO
    REAPER CAN REACH". An orphaned launcher is reached — its command line carries
    :data:`_SANDBOX_LAUNCHER_MARKER`, so :func:`_is_orphan_mcp` accepts it and the
    orphan-MCP sweep kills it on the same pass. Recognising it here would make this arm
    log "leaked agent runtime, not terminated" about a pid that is already a kill
    candidate, and inflate ``leaked_untracked`` and ``leaked_rss_bytes`` with it. A
    launcher whose own parent is still alive is not this predicate's subject either: the
    caller only iterates reparented pids.

    This grants NO kill authority and nothing scheduled terminates on it — a
    hit only logs. The one consumer that can end such a process is the
    user-confirmed ``RuntimeReconciler.reclaim_untracked``, which additionally
    requires the runtime's ``KIROCREW_SPAWN_HOME`` to name this data home.
    This predicate is uid-wide: a second install's live runtime, tracked in ITS
    config dir and so absent from ours, is a hit here. In the report that is
    diagnostic noise; the reclaim's home check is what keeps it from a kill.
    """
    if not cmdline:
        return False  # kernel thread / zombie — no argv to identify
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    basename = _work_orphan_basename(cmdline)
    if not _basename_names_a_harness(basename):
        return False
    if pid in tracked_pids:
        return False  # a reaper can already reach it
    return _env_has_kirocrew_marker(pid)


def reported_untracked_agent_pids() -> set[int]:
    """The runtimes the last orphan scan reported as untracked -- READ ONLY, grants nothing."""
    return set(_reported_untracked_agent_pids)


def confirm_untracked_agent_runtimes() -> set[int]:
    """Re-detect untracked runtimes now, for a caller that may act on the answer.

    Unlike the scan's report this requires a COMPLETE tracked snapshot and raises
    without one, because a dropped row is the input that makes a live runtime look
    untracked.
    """
    tracked, complete = _read_tracked_agent_pids()
    if not complete:
        raise RuntimeError("the tracked-pid snapshot is incomplete")
    found: set[int] = set()
    for pid in _our_orphan_pids():
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if _is_untracked_managed_agent_orphan(pid, cmdline, tracked):
            found.add(pid)
    return found


def _work_orphan_session_leader_alive(pid: int, proc_root: Path | None = None) -> bool:
    """True when *pid*'s session LEADER still exists as a session leader.

    The SID of an agent-spawned work process is the PID of the kiro-cli
    runtime that (transitively) spawned it — the runtime is started with
    ``start_new_session=True`` and neither ``nohup`` nor reparenting to init
    changes a process's SID. A live leader means the owning agent session may
    still be driving or polling the work process, so the sweep must leave it
    alone. PID-recycling is handled by requiring the leader candidate to
    itself be a session leader (a leader's SID equals its own PID); a
    recycled PID that is not a leader does not resurrect ownership.

    FAIL-CLOSED for the sweep: any read failure returns True ("assume
    alive"), so the work path never kills without positively verifying the
    owning session ended.
    """
    sid = _linux_pid_sid(pid, proc_root)
    if sid <= 0:
        return True  # unreadable — assume the owner is alive, do not sweep
    if sid == pid:
        # The work process became its own session leader (setsid'd daemon):
        # SID carries no ownership information. Assume alive — the shape gate
        # already restricts this path to test runners, and a coordinator that
        # setsid'd itself is not distinguishable from an owned one.
        return True
    return _linux_session_leader_alive(sid, proc_root)


def _linux_session_leader_alive(sid: int, proc_root: Path | None = None) -> bool:
    """True unless *sid* has provably stopped leading session *sid*.

    It has when its ``/proc`` entry is gone, or when it reads a different
    session (a recycled pid that is not a leader does not resurrect
    ownership; a kernel thread reads session 0, which is such a reading). A
    leader that is present but whose stat cannot be read is assumed alive:
    only a pid whose entry is absent counts as gone. Shared by the orphan-work
    sweep and ``runtime_reconcile._session_leader_alive``.
    """
    leader_sid = _linux_pid_sid(sid, proc_root)
    if leader_sid >= 0:
        return leader_sid == sid
    root = proc_root if proc_root is not None else Path("/proc")
    return (root / str(sid)).exists()


def find_orphan_mcp_candidates(active_pids: set[int]) -> list[int]:
    """Scan process table for orphaned MCP processes not in any active set.

    Returns candidate PIDs. Caller should re-verify against fresh active PIDs
    before killing (two-phase pattern to eliminate races).

    Also REPORTS — never returns as a candidate — any untracked managed-agent
    runtime orphan (:func:`_is_untracked_managed_agent_orphan`). That class is
    unreachable by every reaper, so it would otherwise leak silently with no
    reproduction; the report deliberately carries no kill authority, which is
    why such a PID is excluded from ``candidates``.
    """
    candidates: list[int] = []
    my_pid = os.getpid()

    orphan_pids = _our_orphan_pids()
    # Read once per scan, not per PID: the files are small but the scan is not.
    # Empty on Windows and on any run with no orphans, so the diagnostic read is
    # skipped entirely in the common case.
    tracked_pids = _tracked_agent_pids() if orphan_pids else set()
    untracked_seen: set[int] = set()

    for pid in orphan_pids:
        if pid == my_pid or pid in active_pids:
            continue
        try:
            if sys.platform == "linux":
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                # Use /proc/pid/stat field 22 (starttime in clock ticks) for
                # canonical process age — immune to /proc mtime heuristic issues.
                pid_age = _linux_pid_age(pid)
            else:
                # Single ps call fetches both age and command (two -o flags
                # avoid the BSD header-label comma ambiguity). etime is
                # whitespace-free, so split(None, 1) cleanly separates the
                # two fields.
                ps_out = subprocess.check_output(
                    ["ps", "-o", "etime=", "-o", "command=", "-p", str(pid)],
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
                fields = ps_out.split(None, 1)
                pid_age = _parse_etime(fields[0].decode() if fields else "")
                cmdline = fields[1] if len(fields) > 1 else b""
        except _PID_VANISHED_ERRORS:
            # Expected TOCTOU race: the PID was in the /proc (or ps) snapshot
            # taken by _our_orphan_pids() and exited before this probe read it.
            # That is the outcome the sweep wants, so log one line — a stack
            # trace here would overstate a routine event.
            logger.debug("Orphan candidate pid %s vanished before probe", pid)
            continue
        except Exception:
            logger.debug(
                "Orphan candidate probe failed for pid %s",
                pid,
                exc_info=True,
            )
            continue
        if pid_age < _ORPHAN_MIN_AGE_SECONDS:
            continue
        # Report-only arm. Placed AFTER the age gate so a runtime whose tracking
        # append has not landed yet is never reported: the gate is orders of
        # magnitude wider than the spawn-to-append window. Reported PIDs are
        # deliberately NOT appended to ``candidates`` — this arm has no kill
        # authority (see the predicate's docstring).
        if _is_untracked_managed_agent_orphan(pid, cmdline, tracked_pids):
            untracked_seen.add(pid)
            if pid not in _reported_untracked_agent_pids:
                # %r, not %s: argv0 is set by the process itself, so a newline
                # in it would forge whole log lines in gateway.log and through
                # /api/logs. repr escapes control characters.
                logger.error(
                    "Leaked agent runtime pid=%s (argv0 %r, age %.0fs): reparented "
                    "to init/systemd with a KIROCREW_SPAWNED environ marker but "
                    "recorded in NEITHER PID file, so no reaper can reclaim it. "
                    "Not terminated — report only.",
                    pid,
                    _work_orphan_basename(cmdline).decode("utf-8", "replace"),
                    pid_age,
                )
        if not (
            _is_sweepable_orphan_mcp(pid, cmdline)
            or _is_sweepable_orphan_gatewayd(cmdline)
            or _is_sweepable_orphan_work(pid, cmdline, pid_age)
            or _is_sweepable_orphan_browser_daemon(pid, cmdline, pid_age)
        ):
            continue
        candidates.append(pid)

    # Keep only what is still detected, so a persisting orphan stays deduped
    # while a vanished PID re-arms the report for a future process.
    _reported_untracked_agent_pids.clear()
    _reported_untracked_agent_pids.update(untracked_seen)

    return candidates


def _linux_pid_sid(pid: int, proc_root: Path | None = None) -> int:
    """Session id (SID): field 6 of /proc/<pid>/stat, via ``read_proc_stat``.

    The SID of an agent-spawned work process points at the kiro-cli session
    leader that (transitively) spawned it — kiro-cli is started with
    ``start_new_session=True``, so every descendant inherits its SID even
    after the direct parent dies and the process reparents to init. Returns
    -1 when unreadable (caller must fail closed). *proc_root* is a test seam
    for fixture-owned process tables.
    """
    stat = platform_compat.read_proc_stat(pid, proc_root=proc_root)
    if stat is None or stat.session is None:
        return -1
    return stat.session


def _linux_pid_age(pid: int, proc_root: Path | None = None) -> float:
    """Process age in seconds from /proc/<pid>/stat starttime (canonical).

    ``0.0`` when it cannot be read, which the min-age guard reads as too young.
    *proc_root* is a test seam for fixture-owned process tables, which carry
    their own ``uptime``.
    """
    age = _pid_age_seconds(pid, str(proc_root) if proc_root is not None else "/proc")
    return 0.0 if age is None else age


def _parse_etime(etime: str) -> float:
    """Parse ps etime format [[DD-]HH:]MM:SS into seconds."""
    try:
        days = 0
        if "-" in etime:
            day_part, etime = etime.split("-", 1)
            days = int(day_part)
        parts = etime.split(":")
        if len(parts) == 3:
            return days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return days * 86400 + int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        pass
    return 0.0
