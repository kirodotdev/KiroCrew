"""Killing orphans a sweep has confirmed, and the security event log record of each kill.

The unreachable gatewayd and stranded browser daemon kills, the descendant walk and
leaf-first kill of an MCP launcher's subtree, and the audit records of those kills and of
a work orphan's tree kill.

Split out of ``kiro_crew.session_pid``, which stays the import path and the patch target:
every name here is reached as ``kiro_crew.session_pid.<name>``, and a patch through that
module reaches this one (``kiro_crew._facade_mirror``). Nothing here imports it.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
from kiro_crew.session_pid_harness import _pid_cmdline
from kiro_crew.session_pid_orphan_scan import _GATEWAY_MARKERS, _env_has_kirocrew_marker
from kiro_crew.session_pid_tracking import _pid_start_token

#: The family logs as ``kiro_crew.session_pid``, the name callers, log filters and
#: ``caplog`` assertions use.
logger = logging.getLogger("kiro_crew.session_pid")


# Grace given to a TERMed browser-daemon GROUP before escalating to SIGKILL.
# Chromium exits on TERM within a second or two; this leaves room for a profile
# flush without letting a wedged tree hold the sweep's budget.
_BROWSER_DAEMON_TERM_GRACE_SECONDS = 5.0


# Grace given to a TERMed unreachable gatewayd before killpg SIGKILL. TERM is
# sent first, deliberately: gatewayd's signal handler routes into the same
# graceful stop path a supervised shutdown takes, so the daemon drains
# in-flight work and reaps its own pooled backend subprocesses — a direct
# SIGKILL would orphan them for a later sweep instead. Derived from the
# daemon's own total shutdown budget (the same discipline the supervisor's
# SIGTERM→SIGKILL grace follows) so the escalation can never fire while a
# correctly-draining daemon is still inside its drain window.
_GATEWAYD_TERM_GRACE_SECONDS = float(TOTAL_SHUTDOWN_BUDGET_SECS)


def _kill_orphan_gatewayd(pid: int, cmdline: bytes) -> int:
    """SIGTERM an unreachable gatewayd; escalate to killpg SIGKILL if wedged.

    TERM first so the daemon's graceful stop path drains and reaps its own
    pooled backends. If the process is still alive after
    :data:`_GATEWAYD_TERM_GRACE_SECONDS`, its identity is re-verified (PID
    recycling) and the whole group is SIGKILLed — the daemon is its own
    group leader (``start_new_session=True``), so ``killpg`` cannot reach
    any foreign process.
    """
    try:
        platform_compat.kill_pid(pid, platform_compat.SIGTERM)
    except ProcessLookupError:
        return 0
    deadline = time.monotonic() + _GATEWAYD_TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        # platform_compat.pid_exists, not a raw `os.kill(pid, 0)`: on Windows a
        # signal-zero "probe" TERMINATES the target instead of testing it, and
        # raw os.kill/SIGKILL do not exist there. This path is POSIX-only in
        # practice, but routing through the shim keeps it correct on its own
        # terms rather than depending on a caller's early-out — the same
        # rationale the browser-daemon probe below already carries.
        if not platform_compat.pid_exists(pid):
            _sel_orphan_kill(pid, pid, cmdline, "sigterm")
            return 1
        time.sleep(0.1)
    # Still alive past the grace: re-verify identity before force-kill so a
    # recycled PID is never SIGKILLed.
    try:
        if sys.platform == "linux":
            current = Path(f"/proc/{pid}/cmdline").read_bytes()
            if current != cmdline:
                _sel_orphan_kill(pid, pid, cmdline, "sigterm")
                return 1
        pgid = os.getpgid(pid)
        if pgid == pid and pgid != os.getpgrp() and pgid > 1:
            os.killpg(pgid, signal.SIGKILL)
        else:
            platform_compat.kill_pid(pid, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    _sel_orphan_kill(pid, pid, cmdline, "sigterm+sigkill")
    return 1


def _kill_orphan_browser_daemon(pid: int, cmdline: bytes) -> int:
    """Group-TERM a stranded browser daemon, escalating to a group SIGKILL.

    Signals the process GROUP, not the pid. The daemon is spawned
    ``detached``, so it is its own group leader and its Chromium children
    inherit that group -- the browser tree is the whole point of the reclaim
    (it is where the gigabytes are), and a pid-only signal would kill the
    supervisor and leave Chromium reparented to init as a fresh, now
    completely unattributable leak.

    TERM first so Chromium exits through its own shutdown path and flushes
    its profile. The group is signalled only when the daemon is genuinely an
    isolated leader (``pgid == pid``, not our own group, not init's), so
    ``killpg`` can never reach a foreign process; identity is re-verified
    before the escalation so a PID recycled inside the grace window is never
    SIGKILLed.
    """
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return 0
    if not (pgid == pid and pgid != os.getpgrp() and pgid > 1):
        # Not an isolated group leader: killpg would reach processes this
        # predicate never identified. Leave it for a later sweep.
        return 0
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return 0
    deadline = time.monotonic() + _BROWSER_DAEMON_TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        # platform_compat.pid_exists, not a raw `os.kill(pid, 0)`: on Windows a
        # signal-zero "probe" TERMINATES the target instead of testing it. This
        # path is POSIX-only in practice, but routing through the shim keeps it
        # correct on its own terms rather than depending on a caller's early-out.
        if not platform_compat.pid_exists(pid):
            _sel_orphan_kill(pid, pgid, cmdline, "browser-daemon-sigterm")
            return 1
        time.sleep(0.1)
    try:
        if sys.platform == "linux":
            if Path(f"/proc/{pid}/cmdline").read_bytes() != cmdline:
                _sel_orphan_kill(pid, pgid, cmdline, "browser-daemon-sigterm")
                return 1
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    _sel_orphan_kill(pid, pgid, cmdline, "browser-daemon-sigterm+sigkill")
    return 1


def _sel_orphan_kill(pid: int, pgid: int, cmdline: bytes, method: str) -> None:
    """Emit SEL audit event for an orphan MCP kill."""
    try:
        # Lazy import to avoid a circular import (see kill_orphan_mcps).
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="gateway",
            agent="kirocrew",
            source="background",
            tool_name="orphan_mcp_sweep",
            tool_kind="process_kill",
            outcome="completed",
            resources=f"pid={pid} pgid={pgid} method={method}",
            metadata={
                "cmdline": cmdline[:200].decode("utf-8", errors="replace"),
            },
        )
    except Exception:
        logger.debug("SEL orphan-kill audit failed", exc_info=True)


def _pid_parent_and_token(pid: int, proc_root: Path | None = None) -> tuple[int | None, str | None]:
    """``(ppid, start_token)`` for *pid* from ONE ``/proc/<pid>/stat`` read.

    Both values must come from the SAME read. Reading the parent edge and the
    start identity separately leaves a window in which the PID exits between
    them, so a recycled PID's fresh token gets paired with the dead process's
    parent edge -- and that token then matches at kill time, which is precisely
    how a live worker gets SIGKILLed.

    ``platform_compat.get_process_start_identity`` is that one read, and its
    start id is the token :func:`_pid_start_token` compares at kill time.

    ``(None, None)`` on any failure, and off Linux -- where the whole subtree
    reap is already a no-op because :func:`_env_has_kirocrew_marker` is
    fail-closed. Callers must treat ``None`` as unproven, never as a mismatch.
    *proc_root* substitutes a fixture process table.
    """
    if sys.platform != "linux":
        return (None, None)
    identity = platform_compat.get_process_start_identity(pid, proc_root=proc_root)
    if identity is None:
        return (None, None)  # exited mid-read or unreadable — fail closed
    return (identity.ppid, identity.start_id)


def _prune_from_orphan_walk(pid: int) -> bool:
    """True when the walk must neither include *pid* NOR descend into it.

    Prunes gateway/CLI entrypoints (:data:`_GATEWAY_MARKERS`) -- an
    agent-launched peer gateway or dev pod. Excluding only the entrypoint's own
    PID is not enough: the walk is flat, so its live workers would still be
    enumerated, and each carries ``KIROCREW_SPAWNED`` with no gateway marker in
    its own argv, so each would pass the per-member gate and be SIGKILLed,
    crashing that pod's active sessions. The whole subtree has to go.

    An unreadable argv also prunes: it is either a process that just exited (no
    children to find) or one whose identity cannot be established, and neither
    is a case for descending.
    """
    cmdline = _pid_cmdline(pid)
    if not cmdline:
        return True
    return any(marker in cmdline.replace(b"\x00", b" ") for marker in _GATEWAY_MARKERS)


def _orphan_descendants(pid: int, child_map: dict[int, list[int]]) -> list[tuple[int, str | None]]:
    """Preorder descendants of a confirmed orphan root, each with its identity.

    Traverses *child_map* -- the authoritative parent->children map from
    :func:`_build_child_map`, which reads every process's ``stat`` PPid field.
    Deliberately NOT ``/proc/<pid>/task/*/children``: that needs
    ``CONFIG_CHECKPOINT_RESTORE``/``CONFIG_PROC_CHILDREN`` and is documented
    reliable only for frozen/stopped tasks, so for a live task it can return an
    incomplete child set and silently drop whole subtrees -- which is precisely
    the leak this sweep exists to close, so reaping through it could no-op with
    no signal.

    Always called BEFORE the root is signalled: after the root dies its children
    reparent to init and the parent links this walk needs are gone.

    Each member is returned with its :func:`_pid_start_token`, captured HERE so
    the kill can refuse a PID that was recycled in between (see
    :func:`_kill_orphan_mcp_descendants`).

    *child_map* is a SNAPSHOT, and it is reused across every candidate root in
    one sweep, so an edge in it can be stale by the time the walk reads it: the
    child may have exited and its PID been reused. Each child's live PPid is
    therefore re-read here and must still equal the parent it was traversed
    from; a PID that does not point back at that parent now is a different
    process and is dropped with its subtree. The PPid and the start token come
    from ONE ``stat`` read (:func:`_pid_parent_and_token`) so they cannot
    describe two different processes.

    Iterative, not recursive: an orphan chain deeper than Python's recursion
    limit would raise ``RecursionError``, which the caller's ``except`` clause
    does not name and which fires BEFORE the root is signalled -- aborting the
    whole sweep, every cycle, and preserving the very tree being reclaimed.

    The visited set bounds the walk, so a PID cycle terminates instead of
    looping forever -- checked per CHILD, which covers a self-parent too.

    A pruned child (:func:`_prune_from_orphan_walk`) is skipped WITH its whole
    subtree, so a peer gateway's live workers are never enumerated.
    """
    out: list[tuple[int, str | None]] = []
    seen: set[int] = {pid}
    # DFS stack of (child, parent) pairs still to validate. The parent travels
    # WITH the child because it is what the live-PPid check compares against.
    # Each pair is validated and emitted when POPPED, and its own children are
    # pushed reversed, which is what makes the emitted order preorder -- the
    # order the leaf-first kill reverses. Emitting inside the child loop instead
    # would yield level order and kill a parent before its children.
    stack: list[tuple[int, int]] = [(c, pid) for c in reversed(child_map.get(pid, []))]
    while stack:
        child, parent = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        live_ppid, token = _pid_parent_and_token(child)
        if live_ppid != parent:
            # Stale edge: this PID exited and a different process now holds it,
            # or its identity cannot be read. Either way it is not the child
            # that was enumerated, so neither it nor anything the snapshot hangs
            # beneath it may be signalled.
            continue
        if _prune_from_orphan_walk(child):
            continue  # gateway subtree (or unreadable) -- do not descend
        out.append((child, token))
        for grandchild in reversed(child_map.get(child, [])):
            stack.append((grandchild, child))
    return out


def _kill_orphan_mcp_descendants(
    descendants: list[tuple[int, str | None]], *, root: int, budget: int
) -> int:
    """SIGKILL leftover subtree members of a reaped MCP-launcher orphan, leaf-first.

    Mirrors :func:`_kill_orphan_work_tree`: descendants were enumerated once
    (preorder) and are killed in reverse so every process dies before its
    parent. *budget* is the caller's remaining
    :data:`_ORPHAN_SWEEP_MAX_KILLS` allowance, so subtree members count
    against the same global cap; survivors are re-reaped next sweep.

    Positive identity per member — the root passing the sweep gate does NOT
    license killing arbitrary descendants:

    * ``KIROCREW_SPAWNED`` in the member's exec-time environ, proving it
      belongs to a tree Kiro Crew spawned (:func:`_env_has_kirocrew_marker`,
      Linux-only and fail-closed, so this whole reap is a no-op off Linux —
      matching the work-class floor).
    * NOT a gateway/CLI entrypoint (:data:`_GATEWAY_MARKERS`), so an
      agent-launched peer gateway or dev pod under the same tree survives.
    * Never this process, its group leader, or pid <= 1.
    * The SAME process the walk saw -- its ``_pid_start_token`` must still
      match the one captured at enumeration. Without this the reap has a
      PID-recycle hole: the root's ``killpg`` reaps a descendant, the kernel
      hands that PID to a NEW Kiro-Crew-spawned worker, and the stale entry
      then SIGKILLs a live process that passes every other gate. A token that
      cannot be read on either side is treated as unproven identity and the
      member is skipped, never as a mismatch -- declining to act is not the
      same as asserting recycling, and a skipped orphan is re-reaped next
      sweep. Logged at debug so a host where identity is never available is
      diagnosable rather than a silent no-op.

    A member whose cmdline is unreadable is skipped rather than killed: the
    marker read and the exclusion check both need it, and failing closed here
    costs one sweep cycle while failing open could kill a live peer.

    Returns the number of processes killed.
    """
    if budget <= 0 or not descendants:
        return 0
    my_pid = os.getpid()
    my_pgid = os.getpgrp()
    killed = 0
    for target, walk_token in reversed(descendants):
        if killed >= budget:
            break  # global kill cap exhausted; next sweep cycle finishes the job
        if target <= 1 or target == my_pid or target == my_pgid or target == root:
            continue
        cmdline = _pid_cmdline(target)
        if not cmdline:
            continue  # vanished or unreadable — fail closed
        if any(marker in cmdline.replace(b"\x00", b" ") for marker in _GATEWAY_MARKERS):
            # Defence in depth: _prune_from_orphan_walk already dropped this
            # subtree during enumeration. Kept because a caller could pass a
            # list it assembled some other way.
            continue
        if not _env_has_kirocrew_marker(target):
            continue  # not provably part of a Kiro Crew tree
        live_token = _pid_start_token(target)
        if walk_token is None or live_token is None:
            logger.debug(
                "Orphan MCP sweep: skipping pid=%d — start identity unavailable "
                "(walk=%r live=%r), re-reaped next sweep",
                target,
                walk_token,
                live_token,
            )
            continue  # identity unproven — never kill on an unverifiable PID
        if live_token != walk_token:
            logger.debug(
                "Orphan MCP sweep: skipping pid=%d — PID recycled since enumeration",
                target,
            )
            continue  # a different process now holds this PID
        try:
            platform_compat.kill_pid(target, platform_compat.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        killed += 1
        logger.info(
            "Orphan MCP sweep: SIGKILL pid=%d reason=descendant of MCP launcher orphan %d",
            target,
            root,
        )
    if killed:
        _sel_orphan_mcp_subtree_kill(root, killed)
    return killed


def _sel_orphan_mcp_subtree_kill(root: int, killed: int) -> None:
    """Emit SEL audit event for an MCP-launcher subtree kill."""
    try:
        # Lazy import to avoid a circular import (see kill_orphan_mcps).
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="gateway",
            agent="kirocrew",
            source="background",
            tool_name="orphan_mcp_sweep",
            tool_kind="process_kill",
            outcome="completed",
            resources=f"root={root} method=mcp_subtree",
            metadata={
                "killed_in_tree": killed,
                "reason": "descendants of KIROCREW_SPAWNED MCP launcher orphan",
            },
        )
    except Exception:
        logger.debug("SEL orphan-mcp-subtree-kill audit failed", exc_info=True)


def _sel_orphan_work_kill(
    pid: int, basename: str, age_seconds: float, cmdline: bytes, killed: int
) -> None:
    """Emit SEL audit event for a work-orphan subtree kill."""
    try:
        # Lazy import to avoid a circular import (see kill_orphan_mcps).
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="gateway",
            agent="kirocrew",
            source="background",
            tool_name="orphan_work_sweep",
            tool_kind="process_kill",
            outcome="completed",
            resources=f"pid={pid} basename={basename} age={int(age_seconds)}s method=work_tree",
            metadata={
                "cmdline": cmdline[:200].decode("utf-8", errors="replace"),
                "killed_in_tree": killed,
                "reason": "KIROCREW_SPAWNED orphan work process",
            },
        )
    except Exception:
        logger.debug("SEL orphan-work-kill audit failed", exc_info=True)
