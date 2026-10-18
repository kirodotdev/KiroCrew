"""Process tracking and orphan cleanup for kiro-cli sessions.

Manages PID files (``kiro_pids.txt`` and ``kiro_session_pids.txt``) that
track spawned kiro-cli processes.  Provides startup cleanup, periodic
sweeping, and per-process track/untrack operations.

See the ``session_parts/sweeps.py`` module docstring for the full Process Sweep Architecture.
"""

from __future__ import annotations

import errno  # noqa: F401
import importlib as _importlib
import logging
import os
import secrets  # noqa: F401
import signal
import subprocess
import sys
import threading  # noqa: F401
import time  # noqa: F401
from collections.abc import Callable, Iterable, Mapping  # noqa: F401
from contextlib import contextmanager  # noqa: F401
from pathlib import Path
from stat import S_ISREG  # noqa: F401
from typing import TYPE_CHECKING

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import platform_compat
from kiro_crew.agent_sdk.backends import (  # noqa: F401
    agent_process_markers,
    node_adapter_entry_relpaths,
)
from kiro_crew.atomic_write import atomic_write  # noqa: F401
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import (  # noqa: F401
    KIROCREW_SANDBOX_TOOL_ENV,
    KIROCREW_SANDBOX_TOOL_VALUE,
    KIROCREW_SPAWN_HOME_ENV,
    KIROCREW_SPAWN_INSTANCE_ENV,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS  # noqa: F401
from kiro_crew.owner_only_files import ensure_directory, owner_only_opener_for  # noqa: F401
from kiro_crew.runtime_ownership import (  # noqa: F401
    PidRefcount,
    authorize_runtime_kill,
    commit_runtime_teardown,
    release_runtime_teardown,
    tenancy_epoch,
)
from kiro_crew.session_pid_harness import (
    _is_managed_agent_process,
    _pid_cmdline,
    _work_orphan_basename,
)
from kiro_crew.session_pid_mappings import (
    _prune_stale_session_pid_files,
    _prune_stale_session_token_files,
)
from kiro_crew.session_pid_orphan_kill import (
    _kill_orphan_browser_daemon,
    _kill_orphan_gatewayd,
    _kill_orphan_mcp_descendants,
    _orphan_descendants,
    _sel_orphan_kill,
    _sel_orphan_work_kill,
)
from kiro_crew.session_pid_orphan_scan import (
    _ORPHAN_MIN_AGE_SECONDS,
    _ORPHAN_SWEEP_MAX_KILLS,
    _accepted_subreaper_pids,
    _env_has_kirocrew_marker,
    _env_spawn_instance,
    _is_marked_mcp_launcher,
    _is_orphan_mcp,
    _is_sweepable_orphan_browser_daemon,
    _is_sweepable_orphan_gatewayd,
    _is_sweepable_orphan_mcp,
    _is_sweepable_orphan_work,
    _linux_pid_age,
)
from kiro_crew.session_pid_rss import _build_child_map
from kiro_crew.session_pid_tracking import (
    _commit_teardown,
    _pid_file_lock,
    _pid_file_path,
    _pid_in_spawn_grace,
    _pid_start_token,
    _read_pid_file_text,
    _rewrite_pid_file,
    _session_pid_entry_index,
    _session_pid_file_lock,
    _session_pid_file_path,
    _windows_pending_child_entry,
    _windows_pending_pid_entry,
    _write_back_pid_file,
)

logger = logging.getLogger(__name__)


#: Parent links a descendant check will follow before giving up. A real agent tree is
#: a handful deep; this only bounds the walk against a parent chain that is circular
#: or being rewritten underneath the read.
_ANCESTRY_WALK_LIMIT = 32


def _is_our_descendant(pid: int, root: int) -> bool:
    """Whether *pid*'s parent chain reaches *root*.

    The ancestry edge a captured descendant needs. A pid enumerated as part of a tree
    is only evidence about the instant it was enumerated; walking its parents back to
    the root proves it is still in that tree NOW, which is what makes capturing its
    identity mean anything.

    Subtractive, like every other check on this path: an unreadable parent link ends
    the walk and answers False, so a pid whose ancestry cannot be proven is not
    signalled rather than signalled on the strength of a stale list.
    """
    seen: set[int] = set()
    current = pid
    for _ in range(_ANCESTRY_WALK_LIMIT):
        if current in seen:
            return False  # a cycle in the parent chain proves nothing
        seen.add(current)
        try:
            parent = platform_compat.get_ppid(current)
        except Exception:
            return False
        if parent == root:
            return True
        if parent <= 1:
            # Reached init, or an unreadable link (-1). Either way the chain never
            # passed through our root.
            return False
        current = parent
    return False


def _kill_pid_tree(pid: int, *, expected_start: str | None = None) -> tuple[int, bool]:
    """Kill *pid* and its descendant agent processes (bottom-up).

    Returns ``(total_killed, root_killed)`` so callers can distinguish
    whether the root process itself was sent SIGKILL.

    ``expected_start`` is the root's process-start identity as the CALLER read it,
    and passing it pins that identity across this function instead of only up to its
    door. A caller that verified the pid immediately before calling still hands over
    a verdict with an expiry: the descendant walk below is a ``pgrep``, unbounded in
    time, and the root's own signal happens after it. A candidate that exits inside
    that walk frees its number, and a managed runtime taking the number next passes
    the argv gate -- it IS one of ours -- so the root signal lands on an unrelated
    live session. Re-checked before the walk and again immediately before the root
    signal, which is the rule :func:`_root_identity_holds` states for the sibling
    teardown: before EVERY signal, not once up front.

    Every signal this function sends is judged against a freshly read identity, because
    a verdict older than the last syscall is a verdict about a pid that may have changed
    hands. The ROOT is checked before the descendant read, so a pid that has already
    moved is not walked; again after that read, because the read is itself a window --
    one procfs children read, or a ``pgrep`` spawn -- and a recycled pid hands over a
    stranger's child list; and again immediately before the root signal. Each CHILD
    carries its own token, read when it was discovered, and is re-checked against that
    token before it is signalled: the argv gate answers from cmdline, so it says a pid
    is the KIND of process we manage, never that it is the one just found.

    Subtractive, exactly as the token rule below requires. It only ever WITHHOLDS a
    signal: an identity that differs, or that cannot be read at all, stops the kill
    and the caller sees zero killed. Omitted -- the default -- leaves every existing
    caller's behaviour untouched, because a caller with no captured identity has
    nothing to compare and this adds no evidence it did not have.

    The argv gate below is a PID-RECYCLE guard and the only thing that authorizes a
    signal here: it asks whether this PID still names the kind of process the
    tracking entry described. :data:`_MANAGED_AGENT_MARKERS` is projected from the
    backend registry, so it answers for every harness Crew spawns rather than for
    two of them.

    A recorded start token does NOT authorize a kill anywhere in this module, by
    design. It is subtractive evidence only — a live token that differs from the
    recorded one proves the PID was recycled, and the entry is pruned without a
    signal — because the tracking file is same-uid-writable and a token is readable
    from ``/proc`` for any introspectable PID, so a matching token is not a
    capability this file's contents may confer. That is the rule
    ``kiro_pids.txt``'s own arm follows, and ``session.md`` states it for both.
    """
    if pid <= 0:
        return 0, False
    pinned = expected_start is not None
    if pinned and not _root_identity_holds(pid, expected_start, gated=True):
        logger.warning(
            "_kill_pid_tree: PID %d is not the process the caller verified; not signalling it",
            pid,
        )
        return 0, False
    killed = 0
    root_killed = False
    try:
        # circular import: session_pid → acp.client → session → session_pid
        from kiro_crew.acp.client import _get_child_pids

        children = _get_child_pids(pid)
        # Each child's own identity, and the ancestry edge that makes capturing it
        # meaningful. The loop below signals under the argv gate alone, and that gate
        # answers from cmdline: it says this pid is the KIND of process we manage,
        # never that it is the one just discovered. So each child carries its own
        # token and is re-checked against it before its signal, for the same reason
        # the root is.
        #
        # Identity ALONE is not enough here, because the capture is itself downstream
        # of the enumeration: a child that exits between the two frees its number, and
        # a replacement is captured under its own identity, which then matches at
        # signal time. What rules that out is proving the pid is still OURS at capture
        # -- ``_is_our_descendant`` walks the parent chain up to this root -- so
        # everything captured was in this tree at that moment, and the identity check
        # then holds it to being the same process at signal time.
        child_ids = (
            {
                cpid: platform_compat.get_process_start_id(cpid)
                for cpid in children
                if cpid > 0 and _is_our_descendant(cpid, pid)
            }
            if pinned
            else {}
        )
        if pinned and not _root_identity_holds(pid, expected_start, gated=True):
            # The descendant READ is itself the window -- one procfs children read on
            # Linux, a pgrep spawn on macOS -- and the children below are signalled
            # under the argv gate alone, with no identity pin of their own. A pid
            # recycled during that read means this list belongs to somebody else's
            # runtime, so its children must not be signalled either.
            logger.warning(
                "_kill_pid_tree: PID %d changed identity while its descendants were read; "
                "not signalling them",
                pid,
            )
            return 0, False
        for cpid in reversed(children):
            if cpid <= 0 or not _is_managed_agent_process(cpid):
                continue
            if pinned and not _root_identity_holds(cpid, child_ids.get(cpid), gated=True):
                # This number is not the child that was captured. Withheld, and that
                # covers three cases with one rule: an identity that changed, one that
                # could not be read, and a pid whose ancestry could not be proven at
                # capture -- none of them is in ``child_ids``. This comparison
                # authorizes a SIGNAL, so every unknown withholds it.
                logger.warning(
                    "_kill_pid_tree: child PID %d is not the process discovered under %d; "
                    "not signalling it",
                    cpid,
                    pid,
                )
                continue
            try:
                platform_compat.kill_pid(cpid, platform_compat.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
    except Exception:
        logger.debug("Error killing children of PID %s", pid, exc_info=True)
    if not _is_managed_agent_process(pid):
        return killed, root_killed
    if pinned and not _root_identity_holds(pid, expected_start, gated=True):
        # The descendant walk above is unbounded, so this is the check that matters:
        # the argv gate says this pid is one of OURS, not that it is still the one
        # the caller verified. A managed runtime that took the number during the walk
        # satisfies the first and fails this.
        logger.warning(
            "_kill_pid_tree: PID %d changed identity during the descendant walk; "
            "not signalling its root",
            pid,
        )
        return killed, root_killed
    try:
        if platform_compat.IS_WINDOWS:
            # _get_child_pids() returns [] on Windows (no pgrep/proc), so the
            # per-child loop above is empty — the root kill MUST reap the whole
            # descendant tree here (taskkill /T), or orphaned kiro-cli MCP/node/
            # python children leak and accumulate across gateway restarts. (On
            # POSIX the children were already SIGKILL'd in the loop above and the
            # root is a single-PID kill.) kill_process_tree raises on non-zero
            # taskkill rc, same shape POSIX uses, so the except below catches
            # a genuine failure and leaves root_killed=False for the caller.
            platform_compat.kill_process_tree(pid, platform_compat.SIGKILL)
        else:
            platform_compat.kill_pid(pid, platform_compat.SIGKILL)
        killed += 1
        root_killed = True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return killed, root_killed


def _sweep_pid_entries(
    lines: list[str],
    *,
    should_skip_tagged: "Callable[[int, int], bool]",
    should_skip_bare: "Callable[[int], bool]",
    is_managed: "Callable[[int], bool] | None" = None,
    dry_run: bool = False,
) -> tuple[int, set[str], list[int]]:
    """Shared per-entry sweep logic for startup and periodic cleanup.

    Parses each line, applies caller-provided skip predicates, probes
    liveness, and either kills orphaned kiro-cli processes or collects
    them as candidates (when *dry_run* is True).

    Returns:
        ``(killed_count, killed_or_dead_entries, candidates)`` where
        *candidates* is non-empty only when ``dry_run=True``.
    """
    killed = 0
    killed_or_dead: set[str] = set()
    candidates: list[int] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            recorded_token: str | None = None
            if ":" in stripped:
                # ``gw:pid`` (legacy) or ``gw:pid:start_token`` (recycle guard).
                parts = stripped.split(":")
                if len(parts) == 3:
                    recorded_token = parts[2] or None
                elif len(parts) != 2:
                    killed_or_dead.add(stripped)
                    continue
                try:
                    gw_pid = int(parts[0])
                    pid = int(parts[1])
                except ValueError:
                    killed_or_dead.add(stripped)
                    continue
                if gw_pid <= 0 or pid <= 0:
                    killed_or_dead.add(stripped)
                    continue
                if should_skip_tagged(gw_pid, pid):
                    continue
            else:
                try:
                    pid = int(stripped)
                except ValueError:
                    killed_or_dead.add(stripped)
                    continue
                if pid <= 0:
                    killed_or_dead.add(stripped)
                    continue
                if should_skip_bare(pid):
                    continue
            if _windows_pending_pid_entry(stripped):
                continue
            # Probe liveness, three-way (os.kill(pid, 0) would *terminate* on
            # Windows, so route through platform_compat). DEAD -> prune;
            # UNSIGNALABLE (POSIX EPERM: alive but owned by another user) -> LEAVE
            # ALONE, never prune or kill a PID we merely can't signal; ALIVE ->
            # fall through to the managed-process check below.
            liveness = platform_compat.pid_liveness(pid)
            if liveness == platform_compat.PID_DEAD:
                killed_or_dead.add(stripped)
                continue
            if liveness == platform_compat.PID_UNSIGNALABLE:
                logger.debug("No permission to signal PID %s — skipping", pid)
                continue
            # Managed check (periodic only)
            if is_managed is not None and is_managed(pid):
                continue
            # ── PID-recycle identity check ──────────────────────────
            # The strongest guard: the entry recorded the child's start token
            # at spawn. If the live process's token DIFFERS, this PID has been
            # RECYCLED onto a different (agent) process — e.g. a fresh
            # gateway's own just-spawned backend landing on a stale dead-
            # gateway entry's PID (empirically reproduced on macOS: sweep
            # SIGKILL'd a live kiro-cli, surfacing as 'process exited
            # (rc=None)'). Prune the stale entry, never kill.
            #
            # An UNREADABLE live token (None) is "identity unknown", NOT a
            # mismatch: pruning there would untrack a live genuine orphan and
            # leak it forever, since every sweep keys off this file (same
            # fail-safe as _pid_gone_or_unmanaged — "any inconclusive result
            # retains"). Keep the entry and fall through to the grace check;
            # the next sweep retries.
            #
            # It runs AHEAD of the argv gate below because both answer the same
            # question — "does this PID still name the process the entry
            # described?" — and the token answers it exactly, for every harness,
            # on every platform, while :data:`_MANAGED_AGENT_MARKERS` answers it
            # by resemblance for two of the eight. Behind the argv gate, a
            # confirmed orphan whose argv resembles neither marker took the
            # prune arm: the entry was dropped as though the PID had been
            # recycled and the process was spared, which untracks a live orphan
            # that every sweep mechanism keys off this file to find. The same
            # ordering argument the PPid fallback carries in
            # :func:`cleanup_orphaned_session_roots` applies to the argv gate
            # here: a weaker guard must not veto a settled identity.
            token_settled = False
            if recorded_token is not None:
                live_token = _pid_start_token(pid)
                if live_token is not None and live_token != recorded_token:
                    killed_or_dead.add(stripped)
                    continue
                if live_token is None:
                    continue  # identity unknown — retain entry, retry next sweep
                token_settled = True
            # The argv test is what authorizes the kill, for every entry. A
            # matching token above does not substitute for it: the token is
            # subtractive evidence, and the tracking file is same-uid-writable.
            if not _is_managed_agent_process(pid):
                if token_settled:
                    # Two pieces of evidence disagree, and the DISPOSAL of the entry
                    # must follow the stronger one. A settled token is proof this PID
                    # is still the process this gateway spawned; the argv gate merely
                    # failed to recognise it, which is what happens on Windows, where
                    # only an image name is readable and an interpreter-hosted adapter
                    # reads as ``node.exe``. Pruning there spares the process AND
                    # discards the record -- and that record is the only thing any
                    # sweep keys off to find it, so the process becomes unreclaimable
                    # by anything. Retaining costs one entry re-examined per pass and
                    # keeps the deferred per-platform identity fix able to reach it.
                    logger.debug(
                        "Orphan sweep: PID %s is unrecognised by argv but its token "
                        "settled identity - retaining the entry",
                        pid,
                    )
                    continue
                killed_or_dead.add(stripped)
                continue
            # ── Spawn grace period (Fix A) ──────────────────────────
            # Skip live PIDs younger than SWEEP_SPAWN_GRACE_SECONDS.
            # POSIX-wide (Linux /proc, macOS ps -o etime=); Windows: no age
            # source, falls through to kill (behavior unchanged there).
            # POSIX read failure: treat as young (safe direction).
            # A missed kill self-heals next cycle.
            if _pid_in_spawn_grace(pid):
                continue
            if dry_run:
                candidates.append(pid)
                continue
            total_killed, root_killed = _kill_pid_tree(pid)
            killed += total_killed
            if root_killed:
                killed_or_dead.add(stripped)
            else:
                if not platform_compat.pid_exists(pid):
                    killed_or_dead.add(stripped)
        except Exception:
            logger.debug("Error processing PID entry %s", stripped, exc_info=True)
    return killed, killed_or_dead, candidates


def _periodic_pid_sweep(my_gw_pid: int, active_pids: set[int]) -> tuple[set[str], list[int]]:
    """Phase 1: identify orphan candidates in a thread (no killing).

    Returns ``(killed_or_dead, candidates)`` where *killed_or_dead* are
    entries to prune (dead/invalid) and *candidates* are PIDs that appear
    orphaned and should be killed — but the final kill decision is made
    back on the event loop where ``self._sessions`` is authoritative.
    """
    path = _session_pid_file_path()
    if not path.exists():
        return set(), []
    lock_path = path.with_suffix(".lock")
    ensure_directory(lock_path.parent)
    try:
        # Non-truncating, for the reason spelled out in `_session_pid_file_lock`.
        # This site is the likeliest of the three to feel it: the sweep runs on a
        # timer while `_track_session_pid` is contending for the same lock, which
        # is exactly the interleaving a truncating open turns into a crash.
        # Kept inline rather than routed through `platform_compat.open_lock_file`:
        # this fd is held across the try/finally below, not a `with` block, so a
        # with-scoped opener that closes the fd at block exit does not fit.
        lock_path.touch(mode=0o600, exist_ok=True)
        lock_fd = open(lock_path, "r+")
    except OSError:
        return set(), []
    try:
        # Shared (read) lock so concurrent gateways can scan the pid file together.
        # Windows note: msvcrt has no shared mode, so try_acquire_lock takes an
        # EXCLUSIVE lock there (see file_lock docstring) — a second concurrent
        # gateway's request fails and it simply skips this sweep cycle and retries
        # next tick. Degraded (sweep skipped), never incorrect; no data corruption.
        if not platform_compat.try_acquire_lock(lock_fd.fileno(), exclusive=False):
            return set(), []
        try:
            lines = _read_pid_file_text(path).splitlines() if path.exists() else []
        finally:
            platform_compat.release_lock(lock_fd.fileno())
    finally:
        lock_fd.close()

    if not lines:
        return set(), []

    _, killed_or_dead, candidates = _sweep_pid_entries(
        lines,
        should_skip_tagged=lambda gw, _p: gw != my_gw_pid,
        should_skip_bare=lambda _p: True,
        is_managed=lambda p: p in active_pids,
        dry_run=True,
    )
    return killed_or_dead, candidates


def _kill_confirmed_and_writeback(
    my_gw_pid: int, confirmed: list[int], killed_or_dead: set[str]
) -> int:
    """Phase 2b: kill confirmed orphans and write back PID file (sync, thread-safe).

    The scan phase (``_sweep_pid_entries`` under ``dry_run``) reports PIDs only, so no
    verdict crosses the event-loop hop between the phases. Each candidate is re-judged
    here against the file as it reads NOW: the entry's own recorded token is re-read and
    the subtractive check re-applied, and :func:`_kill_pid_tree` re-applies the argv gate
    that authorizes the signal. A candidate the re-read finds no entry for is skipped
    rather than killed, because nothing in the file then says what that PID was.
    """
    index = _session_pid_entry_index(my_gw_pid)
    orphan_killed = 0
    for pid in confirmed:
        found = index.get(pid)
        if found is None:
            # This gateway's entry for the PID is not in the file the kill phase
            # read. There is therefore nothing that records what this PID was when
            # it was tracked, so the recycle guard cannot be applied to it at all --
            # and an entry that is absent is also an entry this pass owes no
            # write-back. Killing anyway would mean signalling a PID on the strength
            # of a verdict reached before an event-loop hop, which is the exact
            # inheritance this two-phase split exists to refuse. Skip: either the
            # entry reappears on a later pass and is swept with its own evidence, or
            # it is genuinely untracked and no sweep is responsible for it.
            #
            # The unreadable-file arm of _session_pid_entry_index returns an EMPTY
            # index, so this also makes a transient read failure cost zero kills for
            # one pass instead of un-vouched kills for every candidate.
            logger.info("Orphan sweep: no PID-file entry for %s in the kill phase - skipping", pid)
            continue
        entry, recorded_token = found
        if recorded_token is not None:
            live_token = _pid_start_token(pid)
            if live_token is None:
                # Identity unknown: retain the entry and retry next sweep, the
                # same fail-safe the scan phase applies.
                continue
            if live_token != recorded_token:
                # Provably a different incarnation — prune, never kill.
                killed_or_dead.add(entry)
                continue
        total, root = _kill_pid_tree(pid)
        orphan_killed += total
        if root or not platform_compat.pid_exists(pid):
            killed_or_dead.add(entry)
    if killed_or_dead:
        _write_back_pid_file(killed_or_dead)
    return orphan_killed


# Grace given to a TERMed provider tree before the group SIGKILL. Mirrors the
# 3.0 s budget ``AcpClient._kill_process`` allows on the async teardown path, so
# a runtime gets the same chance to flush and exit through its own shutdown
# whichever path reaps it.
_PROVIDER_TERM_GRACE_SECONDS = 3.0
# Poll interval while waiting out that grace.
_PROVIDER_TERM_POLL_SECONDS = 0.05
# ``(start_id, argv0 basename)`` -- as the ACP layer records a descendant at spawn
# time. Only the start id is verified before signalling (see
# :func:`_signal_provider_descendants`); the basename rides along unread. Spelled
# here rather than imported from the ACP layer, which this module must carry no
# knowledge of: ``scripts/check_agent_sdk_boundary.py`` counts even a type-only
# import as such knowledge, and ``test_agent_lifecycle_cycle.py`` pins the absence.
_ProviderChildRecord = tuple[str | None, bytes | None]


def _isolated_provider_group(pid: int) -> int | None:
    """The process GROUP to signal for a provider rooted at *pid*, or ``None``.

    ``None`` means a pid-scoped kill is the only safe option, so the caller must
    not reach for ``killpg``.

    A group id is returned only when *pid* is an isolated group leader
    (``pgid == pid``, not our own group, not init's), which is what a provider
    spawned with ``start_new_session=True`` is. Under that predicate the group
    holds this provider's tree and nothing else, so a group signal can never
    reach a foreign process -- the same guard
    :func:`_kill_orphan_browser_daemon` carries for the same reason.

    Callers resolve this while the root is still alive and reuse the number for
    the escalation: the group outlives its leader, so the id read here still
    names the survivors after a TERM reaps the root, and reading it up front
    keeps a recycled pid out of the answer.

    POSIX only. Windows has no process groups in this sense; teardown there goes
    through ``kill_process_tree`` (``taskkill /T``), which walks the child tree
    itself.
    """
    if platform_compat.IS_WINDOWS:
        return None
    pgid = platform_compat.pgroup_of(pid)
    if pgid is None:
        return None
    if pgid == pid and pgid > 1 and pgid != os.getpgrp():
        return pgid
    return None


def _root_identity_holds(pid: int, recorded_start: str | None, *, gated: bool) -> bool:
    """Whether *pid* is still the process whose identity was recorded.

    Called again before EVERY signal, not once up front: the identity checked at
    entry can go stale inside the SIGTERM grace, because this teardown is not the
    only reaper. ``asyncio``'s child watcher can reap the leader zombie on its own
    (``_reap_provider_root`` says as much), which frees the pid, and under pid-space
    wraparound a new process can take it before the SIGKILL lands.

    ``gated=False`` is the ``_proc`` / ``_active_proc`` shape, whose pid comes from
    a live handle this process owns: there is no recorded id to compare and the
    handle already proves the child is unreaped.
    """
    if not gated:
        return True
    if recorded_start is None:
        return False
    return platform_compat.get_process_start_id(pid) == recorded_start


def _pgroup_still_ours(
    pgid: int,
    root_pid: int,
    recorded_start: str | None,
    records: dict[int, _ProviderChildRecord],
    *,
    gated: bool,
) -> bool:
    """Whether an identity-verified process still owns process group *pgid*.

    A pgid is only as trustworthy as its members. Once every process in the group
    is gone the number is free, and a group signal aimed at it lands on whichever
    tree takes it next -- so the escalation may only signal the group while it can
    still name a member it has verified: the root itself, or one of the descendants
    recorded at spawn. An unverifiable group is not signalled; the caller falls
    back to the verified descendants it can still name individually.

    A held zombie counts, and deliberately so: it is still a group member and its
    identity is still readable, which is the whole point of not reaping the root
    until the last signal is out. Membership is read through
    :func:`_pid_in_pgroup` for that reason -- see there for why ``getpgid`` alone
    would drop the zombie root on macOS. The ungated ``_proc`` shape has no
    recorded start id, so its owned live handle supplies a fresh identity read for
    the listing comparison.
    """
    if platform_compat.IS_WINDOWS:
        return False
    if _root_identity_holds(root_pid, recorded_start, gated=gated):
        root_start = (
            recorded_start
            if recorded_start is not None
            else platform_compat.get_process_start_id(root_pid)
        )
        if _pid_in_pgroup(root_pid, pgid, root_start):
            return True
    for cpid, (start, _basename) in records.items():
        if start is None:
            continue
        if platform_compat.get_process_start_id(cpid) != start:
            continue
        if _pid_in_pgroup(cpid, pgid, start):
            return True
    return False


def _pid_in_pgroup(pid: int, pgid: int, start_id: str | None) -> bool:
    """Whether *pid* is a member of process group *pgid*, zombie or not.

    ``getpgid`` answers for a live member on every POSIX host, and on Linux for a
    zombie too. macOS refuses it for a zombie (``ESRCH``: the kernel looks the
    pid up among running processes only), which is exactly the member
    :func:`_pgroup_still_ours` needs to see -- the exited-but-unreaped root is
    the one verified member left in the group once its children have been
    SIGTERMed, and losing it there suppresses the group SIGKILL, so a child that
    ignored SIGTERM outlives the teardown. ``sysctl KERN_PROC_PGRP`` lists the
    group's zombies alongside its live members, so it settles the question
    ``getpgid`` cannot.

    The listing is consulted ONLY when ``getpgid`` had no answer. A definite
    answer naming another group is final: the pid has left the group, or the
    number has moved on to a stranger's tree, and a second oracle must not be
    allowed to overrule that verdict in the direction of signalling. Unreadable
    on both is "not a member": the caller must not signal a group it cannot
    prove it owns.

    A listed member counts only when its ``start_id`` matches *start_id* as well
    as its pid. The caller verified the identity before asking, but the listing
    is a separate read: a zombie collected by another reaper in between frees the
    pid, and under wraparound a stranger's new group leader can hold it by the
    time the listing runs. Matching the start instant refuses that recycled pid;
    a caller with no identity to offer (``None``) gets "not a member" for the
    same reason.
    """
    answer = platform_compat.pgroup_of(pid)
    if answer is not None:
        return answer == pgid
    if sys.platform != "darwin" or start_id is None:
        return False
    members = platform_compat.darwin_pgroup_members(pgid)
    if members is None:
        return False
    return any(m.pid == pid and m.start_id == start_id for m in members)


def _group_from_witnessed_descendant(
    pid: int,
    recorded_start: str | None,
    records: dict[int, _ProviderChildRecord],
) -> int | None:
    """The group led by *pid*, once :func:`_pgroup_still_ours` vouches for it.

    Companion to :func:`_isolated_provider_group` for the case it cannot serve: a
    reaped leader. ``pgroup_of`` needs the leader in ``/proc``, so once it is gone
    the group has no readable id and the escalation signals nothing -- while the
    members left in that group keep running.

    ``pgroup_of_leader`` resolves the id for a reaped leader, but from the pid
    ALONE, and an unverified pid is what a RECYCLED one looks like: killpg on it
    takes a stranger's tree. So this reads the number and then hands it to the same
    ownership check the escalation itself uses, rather than carrying a second copy
    of that loop.

    What is added here is only the leader predicate: ``candidate == pid`` ties the
    number to the leader we recorded, since a group a descendant ``setsid``-ed into
    is not the one this pid led.

    ``None`` -- no group signal, exactly as before -- when no verified member
    vouches, when the group is not the one ``pid`` leads, and when signalling is
    denied. POSIX only, like :func:`_isolated_provider_group`.
    """
    if platform_compat.IS_WINDOWS:
        return None
    candidate = platform_compat.pgroup_of_leader(pid)
    if candidate is None or candidate != pid or candidate <= 1 or candidate == os.getpgrp():
        return None
    if not _pgroup_still_ours(candidate, pid, recorded_start, records, gated=True):
        return None
    return candidate


def _provider_descendant_records(
    provider: object,
    pid: int,
    *,
    include_live_walk: bool = True,
    recorded_start: str | None = None,
    gated: bool = False,
) -> dict[int, _ProviderChildRecord]:
    """Snapshot a provider's descendants as ``pid -> (start_time, basename)``.

    Merges the runtime's own spawn-time snapshot (``client._child_pids``, which
    holds descendants that have already reparented away from the root and are
    therefore invisible to a live walk) with a fresh recursive scan of the
    process tree, and records each pid's identity so
    :func:`_signal_provider_descendants` can refuse a recycled one.

    Taken BEFORE any signal: once the root exits its children reparent to init
    and no walk can find them from the root again.

    Reads the machine through ``platform_compat`` alone -- ``process_descendants``
    for the walk, ``get_process_start_id`` for the identity -- so this module
    borrows nothing from the agent layer to do it. That is possible because the
    ACP layer records the SAME neutral start id at spawn time, so a stored record
    and a fresh one are directly comparable.

    ``include_live_walk=False`` drops the fresh walk and keeps ONLY the spawn-time
    snapshot. Pass it when the root's own identity could not be verified: a walk
    from an unverified pid enumerates whoever holds it now, and because those pids
    would have their identity captured here and re-read moments later they would
    MATCH and be signalled -- turning a recycled root into a licence to kill a
    stranger's children. The stored snapshot cannot do that: its entries were
    recorded when the tree was provably ours.

    The walk is BRACKETED by the same identity check for the same reason. A root
    verified just before this call can exit while the walk runs, and the pid can be
    reused inside that window, so a walk that started on our tree can finish on a
    replacement's. Re-reading the identity afterwards and discarding the fresh
    entries on a mismatch is what keeps a stale verification from authorising the
    sweep; the spawn-time snapshot is unaffected and is still returned.
    """
    records: dict[int, _ProviderChildRecord] = {}
    client = getattr(provider, "_client", None)
    stored = getattr(client, "_child_pids", None) if client is not None else None
    if isinstance(stored, dict):
        for cpid, record in stored.items():
            if isinstance(cpid, int) and cpid > 1:
                records[cpid] = record if isinstance(record, tuple) else (record, None)
    if not include_live_walk:
        return records
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: skipping the live descendant walk for pid %d -- "
            "identity went stale before it started",
            pid,
        )
        return records
    try:
        fresh = [p for p in platform_compat.process_descendants(pid) if p > 1 and p not in records]
    except Exception:
        logger.debug("_sync_kill_provider: descendant scan failed for %d", pid, exc_info=True)
        fresh = []
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: discarding %d freshly walked descendant(s) of pid %d -- "
            "the root's identity changed while the walk ran, so they are not ours",
            len(fresh),
            pid,
        )
        return records
    # Capture each identity, THEN re-confirm ancestry from a second snapshot. The
    # enumeration above and the capture below are separate reads, so a descendant
    # can exit and its pid be reused in between, handing us a stranger's start id
    # that then verifies at signal time.
    #
    # The second snapshot is bracketed by the root check as well, and it is NOT
    # enough to argue that an intersection can only shrink the set. Membership is
    # not the only thing these two reads decide: if the ROOT pid is itself reused
    # by a foreign process F while this runs, the second snapshot enumerates F's
    # children, so a first-walk pid that F's child Y now holds is confirmed by the
    # filter and carries Y's captured id -- which the signal-time re-read then
    # agrees with instead of catching. Re-reading the root identity here is what
    # separates "this pid is still in OUR tree" from "this pid is in whatever tree
    # holds that number now".
    captured = {cpid: platform_compat.get_process_start_id(cpid) for cpid in fresh}
    try:
        still_ours = set(platform_compat.process_descendants(pid))
    except Exception:
        logger.debug("_sync_kill_provider: re-scan failed for %d", pid, exc_info=True)
        still_ours = set()
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: discarding %d freshly walked descendant(s) of pid %d -- "
            "the root's identity changes across the re-scan, so the tree scanned is "
            "not ours",
            len(captured),
            pid,
        )
        return records
    for cpid, start_id in captured.items():
        if cpid in still_ours:
            records[cpid] = (start_id, None)
        else:
            logger.warning(
                "_sync_kill_provider: dropping pid %d -- it stops being a descendant of "
                "%d between the walk and its identity capture, so the id may be a "
                "stranger's",
                cpid,
                pid,
            )
    return records


def _signal_provider_descendants(records: dict[int, _ProviderChildRecord], sig: int) -> None:
    """Send *sig* to each snapshotted descendant still alive, leaf-first.

    Covers the descendant a group signal cannot reach: one that ``setsid``-ed
    out of the group, and one already reparented before the snapshot. Ownership
    is re-verified per pid against the recorded start id, so a pid recycled
    inside the teardown window is skipped rather than signalled (deny-by-default).
    Leaf-first so a parent cannot fork a replacement while its own children are
    being reaped.

    The start id alone decides this, through ``platform_compat`` rather than the
    agent layer's own check: it is microsecond libproc on macOS, 100 ns creation
    time on Windows and stat field 22 on Linux, so two processes on the same pid
    at different times cannot share one. The recorded basename is deliberately
    NOT compared, because reading a live pid's basename would mean borrowing the
    ACP layer's reader and this module is held to no knowledge of that layer
    (``scripts/check_agent_sdk_boundary.py``). Basename was only ever a second
    opinion on top of the start id, and it cannot rescue a case the start id
    misses -- a matching start id already means it is the same process.
    """
    if platform_compat.IS_WINDOWS:
        return
    for cpid in reversed(list(records)):
        try:
            if not platform_compat.pid_exists(cpid):
                continue
            expected_start, _expected_basename = records[cpid]
            actual_start = platform_compat.get_process_start_id(cpid)
            if expected_start is None or actual_start is None or actual_start != expected_start:
                logger.debug("_sync_kill_provider: skipping pid %d -- not ours (recycled?)", cpid)
                continue
            platform_compat.kill_pid(cpid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _pid_exited_but_unreaped(pid: int) -> bool:
    """True when *pid* has finished running: a zombie, or already gone.

    Lets the teardown observe the root's exit WITHOUT reaping it. That matters
    because a zombie still owns its pid, and for a group leader that pid IS the
    process group id -- reaping it frees the number, after which the pgid may be
    handed to an unrelated new leader. So the escalation has to be able to say
    "the root has exited" without also making its pgid ambiguous.

    Conservative on an unreadable state: returns False, i.e. "still running", so
    a caller waits out its grace rather than exiting early on a guess. On macOS
    the state comes from ``sysctl KERN_PROC_PID``, which lists zombies where
    libproc refuses them; a liveness probe would not do, because a zombie
    answers ``kill(pid, 0)`` as present and the root would read as running until
    someone else reaped it -- which this teardown deliberately does not, until
    the last group signal is sent. On other non-Linux platforms there is no
    zombie state to read, so this falls back to plain liveness: an
    exited-but-unreaped root reads as alive and the caller waits out the grace.
    """
    if sys.platform == "darwin":
        zombie = platform_compat.darwin_pid_is_zombie(pid)
        return bool(zombie) if zombie is not None else False
    if sys.platform != "linux":
        return not platform_compat.pid_exists(pid)
    zombie = platform_compat.pid_is_zombie(pid)
    if zombie is None:
        # Gone, or present but unreadable: only a pid that has gone has exited.
        return not platform_compat.pid_exists(pid)
    return zombie


def _pgroup_has_member_besides(pgid: int, root_pid: int, *, proc_root: Path | None = None) -> bool:
    """True when some process other than *root_pid* is still in group *pgid*.

    ``pgroup_exists`` cannot answer this: a retained zombie leader is itself a
    group member, so ``killpg(pgid, 0)`` keeps succeeding after everything real
    has died (measured). Without this distinction, holding the zombie for pgid
    safety would cost every teardown its full SIGTERM grace.

    One ``/proc`` pass through ``platform_compat.linux_pgroup_members``, which
    reads every stat as bytes. Callers gate it behind the cheap probes, so the
    common path runs it once. *proc_root* substitutes a fixture process table.

    Conservative on a scan failure: returns True, i.e. "assume the group still
    holds something", so the caller escalates rather than declaring the tree
    gone on unread evidence.

    macOS lists the group with ``sysctl KERN_PROC_PGRP`` and applies the same
    rule -- a zombie member is not holding the group open. Other non-Linux
    platforms can only ask whether the group exists, which a retained zombie
    leader keeps answering yes to, so there the caller waits out its grace.
    """
    if sys.platform == "darwin":
        members = platform_compat.darwin_pgroup_members(pgid)
        if members is None:
            return True
        return any(m.pid != root_pid and not m.zombie for m in members)
    if sys.platform != "linux":
        return platform_compat.pgroup_exists(pgid)
    group = platform_compat.linux_pgroup_members(pgid, proc_root=proc_root)
    if group is None:
        logger.debug("_sync_kill_provider: /proc scan failed for pgid %d", pgid)
        return True
    return any(member != root_pid for member in group)


def group_vouching_available() -> bool:
    """Whether the instance-vouched group read can reach a tree on this host.

    The vouch reads ``/proc/<pid>/environ`` for the per-spawn incarnation token,
    which exists on Linux alone. Everywhere else :func:`_marked_group_members`
    answers ``{}`` and a teardown whose root identity is not proven reaches
    NOTHING -- a bounded leak the orphan sweep reports, never a signal to a
    stranger. Callers need to be able to SAY that, so the platform test lives
    here once rather than being re-derived at each reader.
    """
    return sys.platform == "linux"


def _marked_group_members(
    pgid: int,
    instance: str,
    *,
    require_runtime_identity: bool = True,
    proc_root: Path | None = None,
) -> dict[int, str | None]:
    """Live members of process group *pgid* spawned as incarnation *instance*.

    Returned as ``pid -> start id`` so a caller that signals the group twice can
    prove on the second pass that it is still the SAME group: a pid and a start
    instant name one process for good, where the group number alone does not
    (see :func:`_signal_orphaned_runtime_group`).

    *instance* is the per-spawn ``KIROCREW_SPAWN_INSTANCE`` the runtime put on
    its root's environment, and a member counts only if it carries that exact
    value. The generic ``KIROCREW_SPAWNED`` marker proves a group is SOME Kiro
    Crew runtime's; it cannot prove it is this one's, because every runtime here
    is a marked session leader and a root pid released to a fresh spawn names a
    group that carries the marker just as well. The instance is what tells them
    apart. An empty *instance* matches nothing.

    The proof a teardown needs once the group's LEADER is gone. A leader spawned
    with ``start_new_session=True`` names its group by its own pid, so the number
    survives the leader -- but a bare number is what a recycled pid looks like
    too, and ``killpg`` on it would take a stranger's tree. A member that carries
    ``KIROCREW_SPAWNED`` in its exec-time environment is the positive identity
    that a group is one Kiro Crew spawned; a detached survivor that merely
    inherited the marker is excluded by the argv identity check, the same pair
    the tracked sweep's systemd arm requires.

    The start id is the one ``platform_compat.linux_pgroup_members`` read
    alongside the group and state, so it names the process that was admitted;
    a member whose start cannot be read is left out. *proc_root* substitutes a
    fixture process table.

    Linux only. The environ read is Linux-only and fail-closed everywhere else,
    so the answer is ``{}`` on macOS and Windows and the caller signals nothing
    -- a missed reap there, never a wrong kill. Zombies are skipped: they hold
    no memory and cannot be signalled into exiting.

    *require_runtime_identity* is the ARGV gate
    (:func:`_tracked_child_has_runtime_identity`), on by default because it is the
    shape the ACP runtime's own group has. A caller whose tree is not an agent
    runtime turns it OFF -- an app backend's members are whatever its manifest runs
    (a uvicorn worker, a build subprocess), so requiring the runtime shape excludes
    every one of them and the reap reaches nothing. Turning it off is not "no
    identity": the group and instance checks above still apply and are already
    conclusive, because the instance is a fresh value per spawn and cannot be
    inherited from an earlier or later incarnation of the same group number. The
    ACP path's reason for the extra gate -- an intentional survivor that merely
    inherited the TREE-WIDE ``KIROCREW_SPAWNED`` marker -- does not reach a member
    of THIS group: a process that deliberately detaches calls ``setsid`` and
    thereby leaves the group. See :func:`signal_orphaned_spawn_group`.
    """
    if not group_vouching_available() or pgid <= 1 or not instance:
        return {}
    group = platform_compat.linux_pgroup_members(pgid, proc_root=proc_root)
    members: dict[int, str | None] = {}
    for member, start_ticks in (group or {}).items():
        # The start id comes from the read that admitted the member: one read
        # later, the pid may already name another process.
        if start_ticks is None:
            continue
        if (
            _env_spawn_instance(member, proc_root) == instance
            and _env_has_kirocrew_marker(member, proc_root)
            and (not require_runtime_identity or _tracked_child_has_runtime_identity(member))
        ):
            members[member] = str(start_ticks)
    return members


#: Errnos that mean "this kernel does not have the pidfd syscall", as opposed to
#: "it has it and refused this call". Only the first may use the numeric path.
_PIDFD_ABSENT_ERRNOS = frozenset({errno.ENOSYS, errno.EPERM})


def _signal_pid_by_identity(pid: int, sig: int, start: str) -> bool:
    """Signal *pid* only while it is still the process whose start id is *start*.

    Returns True when the signal was delivered, False when the identity fails to
    match, and raises the delivery error otherwise.

    Re-reading the start id and then calling ``os.kill`` leaves a window between
    the two: the number can be recycled in it, and the signal lands on whatever
    holds it now. Narrowing that window cannot close it, because a pid is not a
    handle -- it is a key the kernel may reissue. A pidfd IS a handle: it refers
    to one process for as long as it is open, so once the identity is confirmed
    AFTER opening it, ``pidfd_send_signal`` cannot reach a later occupant of the
    number no matter how long the descriptor is held. The order is open, then
    verify, then signal -- opening first is what makes the verification binding.

    ``pidfd_open`` needs Linux 5.3 and this whole path is Linux-only, but a
    kernel without it must still be reachable, so there the re-verified
    ``os.kill`` stands: the same narrow window as before, never a wider one.
    """

    def _by_number() -> bool:
        """The identity-verified numeric signal, for a host with no pidfd."""
        if platform_compat.get_process_start_id(pid) != start:
            return False
        os.kill(pid, sig)
        return True

    opener = getattr(os, "pidfd_open", None)
    sender = getattr(signal, "pidfd_send_signal", None)
    if opener is None or sender is None:
        return _by_number()
    try:
        fd = opener(pid)
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno in _PIDFD_ABSENT_ERRNOS:
            # The attribute exists on any Linux build, but the SYSCALL answers
            # ENOSYS before 5.3 (and a seccomp filter can hide it with EPERM).
            # The capability is absent on this host, which is precisely the case
            # the numeric path is for.
            return _by_number()
        # The call exists and refused this open (EMFILE, ENOMEM): no handle, so no
        # atomic signal. Falling back to the number here would reopen the window
        # the handle closes, so fail closed and leave the member to the orphan
        # sweep -- a leak over a signal to a stranger, the trade the rest of this
        # path makes. A fallback is for a capability that is missing, never for one
        # that is present and said no.
        logger.warning(
            "_signal_pid_by_identity: no pidfd for pid %d, so signal %d is not sent; "
            "leaving it to the orphan sweep",
            pid,
            sig,
            exc_info=True,
        )
        return False
    try:
        # AFTER the open: the descriptor already pins whichever process answered,
        # so a match here proves the pinned process is the one that vouched.
        if platform_compat.get_process_start_id(pid) != start:
            return False
        try:
            sender(fd, sig)
        except OSError as exc:
            # pidfd_send_signal landed in 5.1 and pidfd_open in 5.3, so the two
            # can disagree; the same split applies.
            if exc.errno in _PIDFD_ABSENT_ERRNOS:
                return _by_number()
            raise
        return True
    finally:
        os.close(fd)


def _vouch_and_signal_orphaned_group(
    pgid: int,
    sig: int,
    instance: str,
    *,
    expected: Mapping[int, str | None] | None = None,
    require_runtime_identity: bool = True,
) -> tuple[dict[int, str | None], dict[int, str | None]]:
    """Signal a runtime's group members after its leader has been reaped, if ours.

    The kill path that signals a live tree is ``killpg(getpgid(root))``, and it
    has a hole exactly where the leak lives: ``getpgid`` raises once the root has
    exited, the caller reads that as "already dead", and the launcher, agent and
    chat processes left in the group are never signalled. They reparent to init
    holding their memory, and if the root died before any descendant was
    recorded there is no tracking entry to find them by either.

    So resolve the group from the contract instead of from the dead pid -- the
    root was a session leader, so ``pgid == root pid`` -- and find its members
    through :func:`_marked_group_members`, which vouches each one by *instance*:
    the per-spawn token the runtime put on its root's environment, which its
    whole tree inherited. Then signal THOSE MEMBERS, each re-verified by start id
    at the instant of the signal -- not the group number. The number is the
    reaped root's pid and can be handed to a fresh session leader at any moment,
    including between the vouch and the signal; a member's pid plus its start
    instant cannot be. No vouching member means no signal: declining to act
    costs a leak the sweep still reports, where acting costs an unrelated
    process. ``pgid`` is still refused for 0, 1 and our own group, since a
    member listing keyed on one of those would be a listing of the wrong thing.

    The instance is the incarnation pin, and it is what the generic marker
    cannot supply. Every runtime here is a marked session leader, so a root pid
    released and handed to a fresh spawn names a group that carries the marker
    just as well -- and a signal aimed by the number and the marker alone would
    terminate that fresh runtime's live session. A fresh spawn carries a
    different instance, so it does not vouch for this one's group.

    An escalation additionally passes the members the first pass vouched, as
    *expected*, and signals only those of them still alive under the same start
    id -- nothing found for the first time now: the instance proves the
    incarnation, the start id proves the pass is looking at the same processes,
    and a process that was not signalled on the first pass owes no escalation.

    Returns ``(vouched, signalled)``: the live members the vouch FOUND, and the
    subset a signal was actually delivered to -- the latter being the value a
    caller hands back as *expected*. A member that exited between the vouch and the
    signal, or whose identity stops reading as vouched, is skipped; so is one
    whose signal the kernel REFUSED, which is why the two maps are reported
    separately. A teardown that only has to clear its own state can ignore the
    census (see :func:`_signal_orphaned_runtime_group`); a caller deciding whether
    to keep an orphan's only record cannot, because an empty ``signalled`` alone
    cannot say whether the group is gone or merely unreachable right now.

    *require_runtime_identity* is passed through to :func:`_marked_group_members`;
    a caller whose tree is not an agent runtime turns it off. See
    :func:`signal_orphaned_spawn_group`, the public entry point for those.
    """
    if platform_compat.IS_WINDOWS or pgid <= 1 or pgid == os.getpgrp() or not instance:
        return {}, {}
    vouched = _marked_group_members(
        pgid, instance, require_runtime_identity=require_runtime_identity
    )
    if not vouched:
        return {}, {}
    members = vouched
    if expected is not None:
        still_ours: dict[int, str | None] = {
            p: start
            for p, start in members.items()
            if p in expected and start is not None and start == expected[p]
        }
        if not still_ours:
            logger.info(
                "_vouch_and_signal_orphaned_group: none of the %d member(s) vouched for "
                "group %d are still alive; not re-signalling a group that may be a "
                "newer runtime's",
                len(expected),
                pgid,
            )
            return vouched, {}
        # The escalation's target set is the FIRST pass's, not a fresh census: a
        # member found only now was not signalled then, owes no grace, and is
        # exactly what a newer incarnation of the number would look like.
        members = still_ours
    # Signal the MEMBERS, each re-verified by start id at the instant of the
    # signal, never the group number. ``killpg(pgid)`` is aimed at a number, and
    # the number is the reaped root's pid, which the kernel can hand to a fresh
    # session leader between the vouch above and the signal; a pid plus a start
    # instant names one process for good, so a member whose identity still reads
    # as vouched is the process that vouched. Highest pid first as a cheap
    # leaf-first order, so a parent cannot fork a replacement while its own
    # children are being signalled.
    signalled: dict[int, str | None] = {}
    for member in sorted(members, reverse=True):
        start = members[member]
        if start is None:
            continue
        try:
            if not _signal_pid_by_identity(member, sig, start):
                continue
        except ProcessLookupError:
            continue
        except OSError:
            logger.warning(
                "_vouch_and_signal_orphaned_group: signal %d to pid %d (group %d) refused; "
                "leaving the member alive; the caller decides what a refusal means",
                sig,
                member,
                pgid,
                exc_info=True,
            )
            continue
        signalled[member] = start
    return vouched, signalled


def _signal_orphaned_runtime_group(
    pgid: int,
    sig: int,
    instance: str,
    *,
    expected: Mapping[int, str | None] | None = None,
) -> dict[int, str | None]:
    """The ACP teardown's view of :func:`_vouch_and_signal_orphaned_group`.

    Returns only what was SIGNALLED, which is all this caller acts on: an ACP
    teardown clears its own state and prunes its PID entries whatever the kernel
    answered, so a refused signal changes nothing it does next. A caller that must
    tell "the group is empty" apart from "every signal was refused" -- because it
    is deciding whether to keep a record that is an orphan's only handle -- needs
    the vouched census too and calls the implementation through
    :func:`signal_orphaned_spawn_group`.
    """
    return _vouch_and_signal_orphaned_group(pgid, sig, instance, expected=expected)[1]


def signal_orphaned_spawn_group(
    pgid: int,
    sig: int,
    instance: str,
    *,
    expected: Mapping[int, str | None] | None = None,
) -> tuple[dict[int, str | None], dict[int, str | None]]:
    """Signal a NON-agent-runtime spawn's group members after its leader is gone.

    Public entry point onto :func:`_vouch_and_signal_orphaned_group` for a tree
    that Kiro Crew spawned as its own session leader but which is not an ACP
    runtime -- today an app backend (``kiro_crew.apps.backend``), whose members are
    whatever the app's manifest runs. Read that function's docstring for the
    reasoning; the single difference in the signalling is that the per-member ARGV
    gate is off, because an app backend's tree never has the runtime shape and
    would otherwise vouch for nothing.

    Every other guarantee is the one the ACP path makes and is deliberately not
    re-implemented here: the group is resolved from the session-leader contract
    rather than from the dead pid, each member is vouched by the caller's exact
    per-spawn instance token, each signal is pinned to a pid plus its start
    instant (never aimed at the group NUMBER, which the kernel may have reissued),
    an escalation signals only members the first pass vouched, and a host that
    cannot read the vouch signals nothing at all.

    Returns ``(vouched, signalled)``: the live members the vouch FOUND, and the
    subset a signal was actually delivered to. The ACP path discards the census
    because a refused signal changes nothing it does next, but this caller is
    deciding whether to keep a record that is an orphan's only handle, and for that
    the two must be told apart: an empty ``signalled`` with a non-empty ``vouched``
    means the members are alive and the kernel refused (a later attempt can
    succeed), while both empty means the group is genuinely gone. Collapsing them
    into one count is how a refusal comes to look like a completed reap.

    Callers must stamp a fresh ``KIROCREW_SPAWN_INSTANCE`` on the tree's root at
    spawn time and persist it with the pid they will later reap by; without the
    token there is no identity to vouch with and both maps come back empty. Check
    :func:`group_vouching_available` to report a host where the vouch cannot be
    read as the leak it is, rather than as a completed reap.
    """
    return _vouch_and_signal_orphaned_group(
        pgid,
        sig,
        instance,
        expected=expected,
        require_runtime_identity=False,
    )


def _provider_tree_gone(
    pid: int, pgid: int | None, records: dict[int, _ProviderChildRecord]
) -> bool:
    """True when nothing of the provider's tree is alive any more.

    Probes the GROUP when one was resolved -- a group outlives its leader, so a
    dead root proves nothing about the launcher and agent processes left in it,
    which is the whole failure this teardown exists to close. The root is judged
    by :func:`_pid_exited_but_unreaped` rather than by liveness, because the
    escalation deliberately holds its zombie to keep the pgid unambiguous, and a
    zombie answers every liveness probe as present.

    The cheap record probe runs first so the ``/proc`` group scan is reached only
    once the recorded descendants are all gone.
    """
    if any(platform_compat.pid_exists(cpid) for cpid in records):
        return False
    if not _pid_exited_but_unreaped(pid):
        return False
    if pgid is not None:
        return not _pgroup_has_member_besides(pgid, pid)
    return True


def _reap_provider_root(pid: int, recorded_start: str | None, *, gated: bool) -> None:
    """Reap *pid* if it is still the process whose identity was recorded.

    A zombie answers every liveness probe as present until someone waits on it.
    ``ChildProcessError`` means the process is not ours to reap (or asyncio's
    child watcher got there first), which is not a failure here.

    Identity is re-checked immediately before the wait, for the same reason every
    signal re-checks it: that watcher can reap the leader zombie on its own, which
    frees the pid, and ``waitpid`` on a recycled pid consumes an UNRELATED child's
    exit status. That loss is silent and nothing detects or repairs it, so this
    reads the identity here rather than trusting the one taken at entry.

    WHY THE RE-READ IS ENOUGH, on every platform. Linux keeps ``/proc/<pid>/stat``
    readable for a zombie. macOS ``proc_pidinfo`` refuses one, so
    :func:`platform_compat.get_process_start_id` falls back to ``sysctl``, which
    reads the same start instant from the kernel's zombie list. Either way the
    identity of the state this teardown CREATES -- a killed root held unreaped to
    keep its pgid unambiguous -- survives the exit and the check passes for the case
    this exists to handle.

    Where an identity cannot be read at all, refusing is deny-by-default working as
    designed, not a gap to route around. An unreadable identity is exactly the case
    where a recycled pid cannot be told from our own zombie: a freed pid can be taken
    by another child of THIS process that is itself an unreaped zombie, and that
    occupant reads as unreadable too, so a wait would steal its exit status and its
    own watcher would report a code that never happened.

    What such a refusal leaves behind is a ZOMBIE, which has already released its
    memory: one process-table entry and an exit status, bounded by pid space and
    gone when this process exits. That is not the leak this teardown exists to
    close -- that one is a RUNNING descendant holding hundreds of megabytes with no
    tracking entry left to find it by. And the entry is not necessarily permanent:
    asyncio's child watcher may still reap it afterwards, since the root is this
    process's child. A benign bounded entry is the cheaper side of the trade.
    """
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: NOT reaping pid %d -- identity does not hold, so a "
            "wait would consume an unrelated child's status",
            pid,
        )
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _release_client_seed_registrations(client: object) -> None:
    """Run the client's synchronous settings-seed hand-back, if it has one.

    ``AcpClient.release_settings_seed_claim`` withdraws the owner claim or reader
    lease the client holds on its ``settings.local.json`` seed; a torn-down client
    whose ``shutdown`` never runs otherwise keeps that registration for the life of
    the gateway, and every later session on the same work dir reads the path as a
    live sibling's. Resolved by name rather than by type, as every client
    read in this leaf is: ``session_pid`` imports nothing from the ACP layer. A
    client without the method -- a test stand-in, a provider shape that never
    seeds -- costs nothing, and one that holds no registration performs no I/O.
    Never lets an exception out: the teardown that follows is the process's only
    remaining end, and a hand-back that did not land is the recorded-orphan shape
    a later session already repairs.
    """
    hand_back = getattr(client, "release_settings_seed_claim", None)
    if not callable(hand_back):
        return
    try:
        hand_back()
    except Exception:
        logger.debug(
            "_sync_kill_provider: the client's settings-seed hand-back failed", exc_info=True
        )


def _root_identity_refuted(pid: int, recorded_start: str | None, live_start: str | None) -> bool:
    """Whether an unverified recorded root is KNOWN to be gone or recycled.

    Decides the settings-seed hand-back for a root whose identity did not hold.
    True when the pid names no process at all (the runtime is gone) or when a live
    start identity was read and differs from the recorded one (the pid was handed to
    a stranger): in both, the process that read the seed is not the one behind this
    pid, so handing the claim back lets the next session on the work dir adopt the seed.
    False when nothing can be read -- the pid exists but its start identity is
    unavailable, or no identity was recorded at spawn -- because an unreadable
    identity cannot tell our live runtime from a stranger's, and a successor must not
    re-seed the permission file under a runtime that may still be ours.
    """
    if not platform_compat.pid_exists(pid):
        return True
    return (
        isinstance(recorded_start, str) and live_start is not None and live_start != recorded_start
    )


def _sync_kill_provider(provider: object) -> None:
    """Synchronously kill a provider's whole process tree.

    Used during CancelledError handling where async shutdown is unreliable
    (asyncio.shield + await raises CancelledError immediately, leaving
    shutdown fire-and-forget).  Escalates SIGTERM to SIGKILL after a bounded
    grace.

    Signals the process GROUP, not the pid. An agent runtime is a tree -- a
    sandbox launcher, the agent binary, its own chat subprocess and a handful of
    MCP stub children -- and the pid recorded for a provider is the tree's group
    leader (spawned ``start_new_session=True``). A pid-scoped signal reaps that
    leader alone; everything below it survives, reparents to init, and holds its
    memory for the life of the machine, with no tracking entry left to find it
    by. Descendants outside the group are swept individually.

    ``provider`` is deliberately ``object`` rather than ``LLMProvider``.  Every
    read below goes through ``getattr(..., None)`` against a PRIVATE attribute
    that the provider ABC does not declare, so the ABC never described this
    parameter -- and importing it here for the annotation alone closed a cycle:
    session_pid -> providers.base -> acp.types -> acp/__init__ -> acp.runtime ->
    session_pid.  That cycle was fatal, not cosmetic: importing this module
    first raised ``ImportError`` on ``_track_pid``.  It is why sibling
    leaves carry ``LLMProvider = Any`` runtime stubs and why this module reaches
    acp.client through function-local imports.  ``test_agent_lifecycle_cycle.py``
    pins the absence; keep this leaf ignorant of the agent layer.

    """
    # ACP provider: long-lived process via client._pid
    client = getattr(provider, "_client", None)
    # The provider is being abandoned: nothing will call its client's shutdown, so
    # the registrations that client took on its settings seed are handed back by
    # this funnel -- but only once the gates below have said what stands behind the
    # pid. A seed is written before the spawn publishes a pid and the claim belongs
    # to the client OBJECT, so with no pid at all there is no runtime to protect and
    # the claim is handed back at once. With a pid, the hand-back follows the gate's
    # verdict: released when the teardown is authorized (before the first signal, so
    # it lands ahead of the grace), when the recorded root is known to be gone or
    # recycled, and otherwise KEPT -- while a lease or tenancy still holds the
    # runtime, and while the root's identity cannot be read -- because a successor
    # adopting the seed would re-write ``permissions.defaultMode`` under a runtime
    # that may still be live. Duck-typed like every other client read in this leaf.
    pid = getattr(client, "_pid", None) if client else None
    # Whether the pid is a RECORDED number (staleness-prone, so identity-gated
    # below) or one read from a live handle this process owns.
    pid_from_client = pid is not None
    # CC provider: long-lived process via _proc.pid or ephemeral via _active_proc.pid
    if pid is None:
        proc = getattr(provider, "_proc", None)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
    if pid is None:
        proc = getattr(provider, "_active_proc", None)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
    if pid is None:
        _release_client_seed_registrations(client)
        return
    # Only ever signal a real, positive, non-init PID. Test stand-ins are the
    # sharp edge: a Mock attribute passes the None check and coerces to 1 via
    # __index__, so an unguarded os.kill would SIGTERM init / the container
    # entrypoint (observed as a CI sandbox dying with exit 143). pid <= 1 also
    # excludes the kill(0)/kill(-n) process-group semantics outright.
    if not isinstance(pid, int) or pid <= 1:
        logger.debug("_sync_kill_provider: refusing to signal invalid pid %r", pid)
        # No process stands behind an invalid pid, so the seed claim is handed back
        # exactly as for no pid.
        _release_client_seed_registrations(client)
        return
    # Asked ONCE, here, before any signal. Everything below is one careful
    # escalation -- a group resolved while the leader was alive, a SIGTERM grace,
    # a recorded descendant sweep -- and asking per signal would both write the
    # same shot three times and leave a window where the answer changed
    # mid-escalation.
    #
    # This is the ONE gate every hard kill of a provider passes: all three
    # ``_dispatch_hard_kill`` implementations (the facade's static seam, the
    # allocator's, and the warm pool's) resolve their killer through
    # ``get_sync_kill_provider()``, and the dashboard's reset-all fallback calls
    # this function directly.
    #
    # A caller legitimately ending this runtime releases its lease first. The
    # owning provider does so as the first statement of its shutdown's owning
    # branch -- a plain statement, not a ``finally``, so a shutdown cancelled
    # BEFORE it has no release and this gate would refuse the cleanup that
    # follows. The two paths where that is reachable release explicitly:
    # ``session_allocation``'s handler that spans registration, and the
    # dashboard's reset-all timeout arm.
    #
    # At ``cap=1`` a refusal therefore does NOT mean a second tenant exists --
    # one runtime has one owning session. It means a lease outlived the session
    # that took it, so read a ``REFUSED`` line as a release site that did not
    # run, not as a near miss the gate handled.
    if not authorize_runtime_kill(
        pid,
        reason="leaked provider teardown",
        caller="session_pid._sync_kill_provider",
    ):
        # The runtime is still leased or in use, so its settings seed stays
        # claimed: a successor must not re-seed the permission file under a
        # process another party is still mid-flight on. The claim is handed back
        # only by a later teardown of this runtime, or when this process exits
        # and the persisted holder reads as stale.
        logger.debug(
            "_sync_kill_provider: keeping the settings-seed claim of pid %d; the "
            "runtime is still leased or in use",
            pid,
        )
        return
    # The verdict above is a statement about the past, and this function is where
    # that matters most: it runs on an EXECUTOR thread while a session-sharing
    # subagent takes its turn's tenancy on the event loop, and between here and the
    # first signal sit a start-id read, a group resolution and an unbounded
    # descendant walk. A claim landing in that window is invisible to the verdict,
    # so the epoch is captured here and re-checked immediately before every signal
    # below -- the only place the check is worth anything, since narrowing the gap
    # is the entire point.
    tenancy_token = tenancy_epoch(pid)
    # Deny-by-default on the ROOT's own identity -- but only where the pid can go
    # stale. ``_client._pid`` is a RECORDED number that outlives a failed start, so
    # it can name a process the OS has since handed to someone else; and for a group
    # leader that pid IS the pgid, so an unverified ``killpg`` takes a stranger's
    # whole process tree. The pid-scoped fallback is the same hazard one process
    # wide, so an unproven root is not signalled AT ALL rather than signalled
    # narrowly. Recorded descendants are still swept: each carries its own
    # spawn-time identity and is verified against it, which is exactly the check
    # the root was missing.
    #
    # The ``_proc`` / ``_active_proc`` pids are NOT gated: they come from a live
    # handle this process owns whose ``returncode`` is None, so the child is
    # unreaped and its pid cannot have been recycled -- the handle is already
    # better evidence than a recorded id would be, and there is no recorded id
    # for that shape to compare against.
    #
    # Same source on both sides or the comparison is meaningless: the ACP layer
    # records ``_start_time`` with ``platform_compat.get_process_start_id`` (both
    # its client and its runtime do), which is what this reads back. It is also
    # what makes this work on Windows, where there are no process groups but a
    # recycled pid would still send ``taskkill /T`` down a foreign tree.
    root_verified = True
    recorded_start: str | None = None
    # Set once the seed claim has been handed back on this path, so the authorized
    # arms below do not hand it back a second time.
    seed_claim_handed_back = False
    if pid_from_client:
        recorded_start = getattr(client, "_start_time", None)
        live_start = platform_compat.get_process_start_id(pid)
        root_verified = (
            isinstance(recorded_start, str)
            and live_start is not None
            and live_start == recorded_start
        )
        if not root_verified:
            logger.warning(
                "_sync_kill_provider: NOT signalling root pid %d -- identity unproven "
                "(recorded=%r, live=%r); sweeping only descendants recorded at spawn",
                pid,
                recorded_start,
                live_start,
            )
            # The seed claim follows what the identity read established. A root
            # that is gone or recycled has none of our processes behind its pid,
            # so the claim is handed back here (the Windows
            # arm below returns without signalling, and the POSIX arm may still
            # sweep recorded descendants). An identity that could not be read is
            # kept: it cannot tell our live runtime from a stranger's.
            if _root_identity_refuted(pid, recorded_start, live_start):
                _release_client_seed_registrations(client)
                seed_claim_handed_back = True
            else:
                logger.debug(
                    "_sync_kill_provider: keeping the settings-seed claim of pid %d; the "
                    "root's identity could not be read",
                    pid,
                )
    # On Windows there is no SIGTERM/SIGKILL distinction (taskkill /F is a hard
    # kill) and no os.waitpid for non-child PIDs, so a single kill suffices.
    if platform_compat.IS_WINDOWS:
        if not root_verified:
            # Nothing further to do here: the descendant sweep is a POSIX-only
            # arm (no process groups to escape on Windows, and ``taskkill /T``
            # is what normally covers the tree), so refusing the root refuses
            # the whole kill rather than narrowing it.
            return
        # Every Windows shape uses exact-tree cleanup and the same capacity
        # admission. Refusal preserves the whole tree; no root-only fallback.
        #
        # Committed HERE, before the one hard kill this platform sends: taskkill /F
        # has no grace to re-check inside, so the barrier is the only thing standing
        # between the verdict and a claim taken while the tree comes down.
        if not _commit_teardown(pid, tenancy_token):
            return
        # Authorized and committed: the runtime is coming down, so its seed claim
        # is handed back now, ahead of the kill, and the replacement spawn on the
        # same work dir finds the slot free when it seeds.
        if not seed_claim_handed_back:
            _release_client_seed_registrations(client)
            seed_claim_handed_back = True
        try:
            if pid_from_client:
                # PINNED: the query handle that verified this identity is held
                # open across taskkill, and Windows reserves a pid while any
                # handle to the process object exists -- so the pid still means
                # the same process when taskkill resolves it. Reading the start
                # id and then calling the plain variant releases that handle
                # first, which is the window taskkill /T would tear a stranger's
                # whole tree down through. False means identity unconfirmed, and
                # is a refusal to reap rather than a failure to report.
                assert recorded_start is not None  # implied by root_verified
                if not platform_compat.kill_process_tree_pinned(
                    pid, recorded_start, platform_compat.SIGKILL
                ):
                    logger.warning(
                        "_sync_kill_provider: NOT killing tree of pid %d -- Windows "
                        "identity could not be pinned across the terminate",
                        pid,
                    )
                    return
            else:
                # An owned Popen still participates in the same cleanup budget.
                # Its handle pins the source while the identity is read; an
                # unreadable identity is not permission for a numeric fallback.
                start = platform_compat.get_process_start_id(pid)
                if start is None or not platform_compat.kill_process_tree_pinned(
                    pid, start, platform_compat.SIGKILL
                ):
                    return
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.warning(
                "_sync_kill_provider: Windows tree cleanup incomplete for PID %d (%s)",
                pid,
                exc,
            )
            return
        finally:
            # Every exit above returns, so the barrier is dropped here or not at
            # all: a pid left committed is one no tenant can ever claim again.
            release_runtime_teardown(pid)
        logger.warning("_sync_kill_provider: killed PID %d for leaked provider", pid)
        return
    # Resolved once, while the root is alive. The root's zombie is then held
    # unreaped until every group signal has been sent (see below), so this id
    # keeps naming OUR group for the whole escalation. Read BEFORE the descendant
    # scan: that scan is unbounded in the width of the tree, and the watcher can
    # reap the leader while it runs, which would cost a verified root its group.
    pgid = _isolated_provider_group(pid) if root_verified else None
    records = _provider_descendant_records(
        provider,
        pid,
        include_live_walk=root_verified,
        recorded_start=recorded_start,
        gated=pid_from_client,
    )
    if pgid is None and pid_from_client:
        # No group id from the root. Usually that root is a RECYCLED pid, and
        # reading a group off it would name whatever group holds that pid now. But
        # it is also the shape a REAPED leader presents -- no /proc entry to verify
        # against -- and that root's group is still full of our running members.
        # Telling the two apart needs evidence the pid cannot give, so the group is
        # derived from a spawn-recorded descendant still in it; failing that this
        # stays None and nothing is signalled, as before.
        #
        # Only for the RECORDED-pid shape. A `_proc`/`_active_proc` root came from a
        # live handle whose returncode was None, so it was alive moments ago and
        # `_isolated_provider_group` already had its answer -- there is no reaped
        # leader to recover. That shape also passes `gated=False`, which makes every
        # `_root_identity_holds` check pass on trust, including the ones bracketing
        # the live descendant walk: a pid recycled to a foreign root would have that
        # root's children walked into `records` and verified against identities read
        # from the same stranger, so a witness drawn from them proves nothing. The
        # recorded shape cannot reach that state -- an unverified root turns the walk
        # off (`include_live_walk=root_verified`), leaving only the spawn snapshot.
        pgid = _group_from_witnessed_descendant(pid, recorded_start, records)
    # Committed ONCE here, immediately before the first signal, and held for the
    # whole escalation. A per-round re-read cannot serve: by the second round the
    # SIGTERM has already been delivered and its grace -- seconds, by this
    # function's own comment ample time for a shared turn to start -- has passed,
    # so abandoning the SIGKILL leaves the turn that claimed in the grace dying
    # from a signal no table can recall. Closing the pid to new tenants is the only
    # answer that holds for the duration of a kill that cannot be taken back.
    if not _commit_teardown(pid, tenancy_token):
        return
    # Authorized and committed: the runtime is coming down, so its seed claim is
    # handed back here, before the first signal. Ahead of the grace on purpose --
    # the replacement spawn on the same work dir runs beside this teardown and
    # reads the live slot when it seeds; a hand-back that waited out the grace
    # would land after that read and the replacement would decline its own seed
    # as a live sibling's. Verified roots only: an unverified root is never
    # signalled below (only its recorded descendants are swept), so a root whose
    # identity could not be read may still be running and keeps its claim.
    if root_verified and not seed_claim_handed_back:
        _release_client_seed_registrations(client)
        seed_claim_handed_back = True
    try:
        for sig in (platform_compat.SIGTERM, platform_compat.SIGKILL):
            # killpg is authorized by GROUP OWNERSHIP, not by the root still being
            # alive. `_pgroup_still_ours` proves an identity-verified member of our
            # tree owns this pgid -- the root if it is still there, otherwise a
            # spawn-recorded descendant -- and that is exactly the property killpg
            # needs. Demanding the root's identity on top of it suppresses the
            # escalation in the one case it matters: asyncio's watcher collects the
            # leader zombie during the grace, so the SIGKILL round is skipped, and a
            # descendant that forked into the group AFTER the snapshot is reached by
            # neither the group signal nor the recorded sweep. It survives the
            # teardown, which is the leak this whole change exists to stop.
            #
            # The pid-scoped fallback below is different: it names the root itself,
            # so it keeps the root identity check.
            group_ok = pgid is not None and _pgroup_still_ours(
                pgid, pid, recorded_start, records, gated=pid_from_client
            )
            root_ok = root_verified and _root_identity_holds(
                pid, recorded_start, gated=pid_from_client
            )
            if group_ok or root_ok:
                try:
                    if group_ok:
                        os.killpg(pgid, sig)  # type: ignore[arg-type]
                    elif pgid is None:
                        platform_compat.kill_pid(pid, sig)
                    else:
                        logger.warning(
                            "_sync_kill_provider: pgid %d holds no verified member "
                            "of pid %d's tree; signalling recorded descendants only",
                            pgid,
                            pid,
                        )
                except ProcessLookupError:
                    # The root (or its whole group) is gone. Descendants that escaped
                    # it can still be alive, so sweep before deciding this teardown
                    # is done.
                    pass
                except OSError:
                    pass
            _signal_provider_descendants(records, sig)
            if sig == platform_compat.SIGTERM:
                deadline = time.monotonic() + _PROVIDER_TERM_GRACE_SECONDS
                while True:
                    # Deliberately NOT reaping here. A zombie owns its pid, and for a
                    # group leader that pid IS the pgid, so reaping the root mid-grace
                    # frees the number while the SIGKILL escalation below still aims
                    # at it -- a pid recycled into a new group leader would take that
                    # SIGKILL. The root's exit is read from its zombie state instead,
                    # and it is reaped only once no further group signal can be sent.
                    if _provider_tree_gone(pid, pgid, records):
                        _reap_provider_root(pid, recorded_start, gated=pid_from_client)
                        logger.warning(
                            "_sync_kill_provider: killed PID %d for leaked provider "
                            "(SIGTERM, scope=%s, descendants=%d)",
                            pid,
                            "pgid" if pgid is not None else "pid",
                            len(records),
                        )
                        return
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_PROVIDER_TERM_POLL_SECONDS)
        _reap_provider_root(pid, recorded_start, gated=pid_from_client)
        logger.warning(
            "_sync_kill_provider: killed PID %d for leaked provider "
            "(SIGKILL, scope=%s, descendants=%d)",
            pid,
            "pgid" if pgid is not None else "pid",
            len(records),
        )
    finally:
        # The grace's early return is inside this block, so the barrier is dropped
        # here for every path out of the escalation. Left standing it would refuse
        # this pid's tenancies for the life of the gateway -- the leak that made a
        # reservation released at each early exit the wrong shape.
        release_runtime_teardown(pid)


def _tracked_child_has_runtime_identity(child_pid: int) -> bool:
    """Positive argv identity for the tracked sweep's systemd kill arm.

    True only when the live process looks like something Kiro Crew tracks in
    ``kiro_pids.txt``: a managed agent runtime (:data:`_MANAGED_AGENT_MARKERS`),
    an MCP entrypoint (:func:`_is_orphan_mcp`), or a fingerprint-less MCP
    launcher shape (:func:`_is_marked_mcp_launcher` -- callers pair this arm
    with the environ marker). FAIL-CLOSED: unreadable argv is inconclusive and
    returns ``False``, so the sweep prunes without killing. This keeps an
    intentional survivor (a detached process that merely inherited the
    tree-wide ``KIROCREW_SPAWNED`` marker, e.g. a preview server) out of the
    systemd arm's kill authority even if a tracking entry names its PID.
    """
    if _is_managed_agent_process(child_pid):
        return True
    cmdline = _pid_cmdline(child_pid)
    if not cmdline:
        return False
    return _is_orphan_mcp(cmdline) or _is_marked_mcp_launcher(cmdline)


def _cleanup_orphaned_mcp_servers() -> int:
    """Kill tracked child PIDs whose parent kiro-cli session is dead.

    Child entries are stored as ``child_pid:parent_pid[:start-id]`` in
    ``kiro_pids.txt`` (the optional third field is the child's process-start
    identity, recorded at track time).  A child is orphaned when its parent
    PID is dead.  Bare PID lines (sandbox root PIDs) are pruned
    when the process is confirmed dead.

    Zero false positives: we only kill PIDs we tracked, only when the
    specific parent session that spawned them is confirmed dead, and never
    when the start identity proves the PID was recycled.

    A row whose own pid or whose parent is a Windows tree holding an unretired
    cleanup pin is left untouched, kill and prune alike: that drain owns the
    tree, and this file is where the identities it still needs are recorded.
    """
    path = _pid_file_path()
    if not path.exists():
        return 0

    # Hold the lock for the entire read-kill-write cycle so that a concurrent
    # _untrack_child_pids (clean shutdown) cannot remove an entry between our
    # read and our kill decision.  os.kill is non-blocking so lock duration is
    # negligible.
    with _pid_file_lock():
        lines = _read_pid_file_text(path).splitlines()
        killed = 0
        lines_to_remove: set[str] = set()
        # Lazily computed on the first orphan-kill decision: the /proc scan is
        # only worth paying when at least one tracked child has a dead parent.
        accepted_ppids: set[int] | None = None

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if _windows_pending_child_entry(stripped):
                # An unretired pin owns this tree, so the drain -- not this
                # sweep -- decides when it is gone. Every arm below ends in
                # retiring the row, including the arms that reach it after a
                # kill that RAISED, and this file is the only record that
                # survives the process, so retiring one here would strand a
                # live descendant with nothing left to name it.
                continue
            if ":" not in stripped:
                # Bare PID (sandbox root). Prune if dead.
                try:
                    bare_pid = int(stripped)
                except ValueError:
                    continue
                if not platform_compat.pid_exists(bare_pid):
                    lines_to_remove.add(stripped)
                continue
            parts = stripped.split(":")
            try:
                child_pid = int(parts[0])
                parent_pid = int(parts[1])
            except (ValueError, IndexError):
                continue
            # Optional third field: process-start identity recorded at track
            # time (see _track_child_pids). Legacy two-field entries have none.
            recorded_token = parts[2] if len(parts) >= 3 and parts[2] else None

            # Is the child still alive? (os.kill(pid, 0) would terminate on Windows)
            if not platform_compat.pid_exists(child_pid):
                lines_to_remove.add(stripped)  # confirmed dead — prune
                continue

            # Is the parent session still alive?
            if platform_compat.pid_exists(parent_pid):
                continue  # parent alive (or unknown) — leave child running

            # Parent confirmed dead -> child is orphaned -- kill it, unless
            # the PID names a different incarnation than the one we tracked.
            #
            # The start token (recorded at track time via _pid_start_token)
            # is SUBTRACTIVE evidence only: a live token that differs from
            # the recorded one proves the PID was recycled -> prune without
            # killing. A matching or unreadable token never authorizes the
            # kill by itself -- the record is same-uid-writable, so a forged
            # line must not be able to aim the sweep at an arbitrary
            # process. The kill still requires the reparent heuristic below,
            # exactly the authority the sweep has always had.
            #
            # Heuristic: a true orphan reparented to init or the nearest
            # subreaper (systemd --user) -- the same accepted-parent set
            # _our_orphan_pids uses -- or still shows the dead parent's PID
            # (kill/reparent race). The init and dead-parent arms keep their
            # historical shape on every platform. The systemd arm is
            # stricter: under systemd --user EVERY manager-started service
            # carries the manager's PID as its PPid for its whole life, and
            # the KIROCREW_SPAWNED environ marker is tree-wide (an
            # intentional survivor such as a detached preview server
            # inherits it too), so killing there requires BOTH the marker
            # AND positive runtime argv identity -- the process must look
            # like something this file tracks (managed agent runtime, MCP
            # entrypoint, or marked launcher). Unreadable argv fails closed
            # to prune-without-kill.
            if recorded_token is not None:
                live_token = _pid_start_token(child_pid)
                if live_token is not None and live_token != recorded_token:
                    # Provably a different incarnation -- PID reuse.
                    lines_to_remove.add(stripped)
                    continue
            if accepted_ppids is None:
                accepted_ppids = _accepted_subreaper_pids()
            actual_ppid = platform_compat.get_ppid(child_pid)
            ours = actual_ppid in (1, parent_pid) or (
                actual_ppid in accepted_ppids
                and _env_has_kirocrew_marker(child_pid)
                and _tracked_child_has_runtime_identity(child_pid)
            )
            if not ours:
                # PID was reused by an unrelated process — just prune
                lines_to_remove.add(stripped)
                continue
            try:
                platform_compat.kill_pid(child_pid, platform_compat.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
            lines_to_remove.add(stripped)

        if lines_to_remove:
            kept = [ln for ln in lines if ln.strip() not in lines_to_remove]
            _rewrite_pid_file(path, "\n".join(kept) + "\n" if kept else "")

    return killed


def cleanup_orphaned_sessions(*, narrow_with_leaders: bool = True) -> None:
    """Kill leftover agent-harness processes from a previous gateway run.

    Reads ``kiro_session_pids.txt`` (written at spawn time), validates each
    PID still names the process the entry recorded — by start token, falling
    back to cmdline resemblance for a token-less entry (guards against PID
    recycling) — kills descendants bottom-up, then truncates the file.

    Runs at gateway startup before any new sessions are created, so the file
    contains only PIDs from the previous run.

    Also sweeps orphaned MCP server processes via ``_cleanup_orphaned_mcp_servers``
    which uses the separate ``kiro_pids.txt`` (child:parent[:start-id] format).

    ``narrow_with_leaders`` is forwarded to
    :func:`_prune_stale_session_pid_files`. The gateway passes ``False`` on its
    boot path and in its force-exit handler, so both do exactly the work they
    did before the recycled-pid change; the narrowing applies on the graceful
    shutdown path, which is not spawning sessions.


    Additionally cleans up:
    - Stale ``session_pid_*.txt`` files for processes that no longer exist.
    - Empty directories under ``sessions/`` left by subagents that produced
      no output before timing out.
    """
    # Step 1: Read file under lock (fast I/O only)
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        lines: list[str] = _read_pid_file_text(path).splitlines() if path.exists() else []

    # Step 2: Process outside lock (slow: os.kill, _get_child_pids, SIGKILL)
    supervisor_verdicts: dict[int, bool] = {}

    def _skip_tagged(gw_pid: int, _pid: int) -> bool:
        """Skip if owning gateway is still alive.

        One live owner is NOT a gateway: the supervisor an in-app restart
        through a supervising launcher (``toolbox-exec``) leaves behind. The
        restart ``os.execv``s the launcher, which runs this gateway as its
        child, so the old pid stays alive with its start identity unchanged and
        never reads dead while this gateway runs. Its rows were filed by this
        gateway's own previous image, whose sessions the exec left unmanaged,
        so they are swept here as the previous run's. Sweeping them signals no
        foreign process: each row's child is still checked against its
        recorded start token and the managed-agent argv before any kill, and a
        live gateway ancestor still runs Python
        (:func:`platform_compat.is_exec_supervisor_of_this_process`) and keeps
        its rows.
        """
        # pid_exists() returns True on a live PID or one we can't signal
        # (can't tell — preserve), and False only when confirmed dead.
        if not platform_compat.pid_exists(gw_pid):
            return False
        # One ancestry walk per owner, not per row.
        if gw_pid not in supervisor_verdicts:
            supervisor_verdicts[gw_pid] = platform_compat.is_exec_supervisor_of_this_process(gw_pid)
        return not supervisor_verdicts[gw_pid]

    killed, killed_or_dead, _ = _sweep_pid_entries(
        lines,
        should_skip_tagged=_skip_tagged,
        should_skip_bare=lambda _pid: False,  # startup processes all entries
    )

    # Step 3: Re-read and write under lock — only remove handled entries,
    # preserving entries for alive gateways and un-signalable processes.
    if killed_or_dead:
        _write_back_pid_file(killed_or_dead)

    if killed:
        logger.info("Cleaned up %d orphaned kiro-cli processes", killed)

    # Second pass: sweep MCP servers that escaped process-group kill
    mcp_killed = _cleanup_orphaned_mcp_servers()
    if mcp_killed:
        logger.info("Cleaned up %d orphaned MCP server processes", mcp_killed)

    # Third pass: remove stale session_pid_*.txt files for dead processes
    _prune_stale_session_pid_files(narrow_with_leaders=narrow_with_leaders)
    # Fourth pass: bound the accumulation of session-token mappings, which are
    # keyed by a token hash rather than by a pid and so cannot be probed for
    # liveness at all.
    _prune_stale_session_token_files()

    # Fourth pass: remove empty session workspace dirs (orphaned subagent dirs)
    sessions_dir = config_dir() / "sessions"
    empty_dirs = 0
    if sessions_dir.exists():
        for d in sessions_dir.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                    empty_dirs += 1
                except OSError:
                    pass  # directory became non-empty or was already removed
    if empty_dirs:
        logger.info("Cleaned up %d empty session workspace dirs", empty_dirs)


def cleanup_orphaned_session_roots() -> int:
    """Advance failed exact-handle drains, then reap dead-gateway PID records.

    On Windows, process-local cleanup state is attempted first. It owns exact
    root and intermediary handles transferred by failed runtime/client teardown;
    this pass never reconstructs that authority from a PID. The ordinary
    ``kiro_session_pids.txt`` crash-orphan sweep then keeps its existing rules.

    Reads ``kiro_session_pids.txt`` entries (format
    ``<gateway_pid>:<child_pid>[:<start_token>]``), checks if the gateway PID is
    alive, and for dead gateways validates the child PID still names the process the
    entry recorded before issuing SIGKILL. What AUTHORIZES the signal is
    ``_is_managed_agent_process`` — the argv gate, applied to every entry whatever it
    recorded. The recorded start token only ever SUBTRACTS: a live value that differs
    prunes without a signal, an unreadable one retains the entry, and a value that
    matches settles identity well enough to skip the weaker PPid reparent test. A
    token-less entry is judged by the argv gate plus that PPid test. An entry whose
    owning pid is the exec supervisor of this process
    (:func:`platform_compat.is_exec_supervisor_of_this_process`) was filed by this
    gateway's own previous image and is judged as a dead owner's.

    Called periodically from ``SessionManager._cleanup_loop``. Returns the
    number of exact-handle trees completed plus orphaned processes killed.
    """

    # The drain calls ``retire_windows_tree_tracking`` itself, under the
    # pending-state lock and while the exact root handle still pins this
    # incarnation, so the reuse window is already closed for every caller and
    # this sweep adds no retirement of its own. A second retirement here would be
    # a second chance to fail a write that has already succeeded, holding a fully
    # drained tree's handles for another tick.
    completed_roots = platform_compat.retry_pending_windows_process_trees()
    killed = len(completed_roots)
    path = _session_pid_file_path()
    if not path.exists():
        return killed

    with _session_pid_file_lock():
        lines = _read_pid_file_text(path).splitlines()

    if not lines:
        return killed

    my_gw_pid = os.getpid()
    entries_to_remove: set[str] = set()
    supervisor_verdicts: dict[int, bool] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue

        # ``gw:pid`` (legacy) or ``gw:pid:start_token`` (recycle guard) — see
        # _track_session_pid. A bare split(":", 1) would leave "pid:token" in
        # parts[1] and int() it into a prune, silently discarding every
        # token-bearing entry instead of sweeping it.
        parts = stripped.split(":")
        recorded_token: str | None = None
        token_settled = False
        if len(parts) == 3:
            recorded_token = parts[2] or None
        elif len(parts) != 2:
            entries_to_remove.add(stripped)
            continue
        try:
            gw_pid = int(parts[0])
            child_pid = int(parts[1])
        except (ValueError, IndexError):
            entries_to_remove.add(stripped)
            continue

        if gw_pid <= 0 or child_pid <= 0:
            entries_to_remove.add(stripped)
            continue

        # Skip entries owned by the current (live) gateway
        if gw_pid == my_gw_pid:
            continue

        # Check if the owning gateway is still alive. Route through
        # platform_compat: os.kill(pid, 0) would *terminate* the process on
        # Windows, so use the three-way liveness probe instead.
        gw_liveness = platform_compat.pid_liveness(gw_pid)
        if gw_liveness == platform_compat.PID_UNSIGNALABLE:
            continue  # can't determine — skip
        if gw_liveness == platform_compat.PID_ALIVE:
            # One live owner is NOT a gateway: the supervisor an in-app restart
            # through a supervising launcher (``toolbox-exec``) leaves as this
            # process's ancestor. Its rows were filed by this gateway's own
            # previous image, and the startup sweep does not settle all of them:
            # a child still in the spawn grace at boot, or one whose token could
            # not be read then, is retained, and this is the only sweep that runs
            # again before the next start. Such a row goes through every identity
            # test below exactly as a dead owner's does. A live gateway ancestor
            # still runs Python
            # (:func:`platform_compat.is_exec_supervisor_of_this_process`) and
            # keeps its rows. One ancestry walk per owner, not per row.
            if gw_pid not in supervisor_verdicts:
                is_supervisor = platform_compat.is_exec_supervisor_of_this_process(gw_pid)
                supervisor_verdicts[gw_pid] = is_supervisor
            if not supervisor_verdicts[gw_pid]:
                continue  # gateway alive — its responsibility
        # Owner dead, or this gateway's own exec supervisor — orphan candidate

        # A process-local pending tree still owns its tracking.
        if platform_compat.windows_tree_cleanup_pending(child_pid, recorded_token):
            continue
        # Check if the child PID is still alive.
        child_liveness = platform_compat.pid_liveness(child_pid)
        if child_liveness == platform_compat.PID_DEAD:
            # Already dead — just prune the entry
            entries_to_remove.add(stripped)
            continue
        if child_liveness == platform_compat.PID_UNSIGNALABLE:
            continue  # can't signal — skip

        # Child is alive. Two independent questions follow, in this order: is this
        # PID provably a DIFFERENT incarnation (the entry's own start token, which can
        # only subtract), and is it a harness at all (the argv gate, which is what
        # authorizes the signal and runs for every entry).

        # Strongest PID-reuse guard FIRST: the entry recorded the child's start
        # token at spawn (see _pid_start_token). A MISMATCH means this PID now
        # names a DIFFERENT process — prune, never kill. An unreadable live
        # token is "identity unknown", not a mismatch: retain the entry so a
        # live genuine orphan is not untracked (and thus leaked forever) on one
        # transient probe failure; the next sweep retries.
        #
        # A token that MATCHES is positive proof this PID is still the process
        # we spawned, so it settles identity on its own and the weaker PPid
        # heuristic below MUST NOT be allowed to veto it. That ordering is
        # load-bearing: an orphan does not always reparent to init. A child
        # placed in its own cgroup scope by the service manager reparents to
        # that *user manager*, which is a subreaper, so its PPid is neither 1
        # nor the dead gateway's. Running the PPid check first classified every
        # such orphan as "PID recycled" and pruned its tracking entry WITHOUT
        # killing it — sparing the process and then forgetting it, so no later
        # sweep could ever reap it.
        if recorded_token is not None:
            live_token = _pid_start_token(child_pid)
            if live_token is not None and live_token != recorded_token:
                entries_to_remove.add(stripped)
                continue
            if live_token is None:
                continue  # identity unknown — retain entry, retry next sweep
            token_settled = True

        # The argv test authorizes, for every entry. A matching token does not
        # substitute for it: the tracking file is same-uid-writable, so its contents
        # may not confer a capability.
        if not _is_managed_agent_process(child_pid):
            if token_settled:
                # The stronger evidence decides the entry's FATE, exactly as in the
                # periodic sweep: a settled token proves this PID still names the
                # process the entry recorded, so pruning would spare the process and
                # discard the only record any sweep could find it by. That is the
                # unreclaimable state, not a conservative one. Retain and re-examine
                # next start; the deferred per-platform identity work is what closes
                # the gap for good.
                logger.debug(
                    "Startup reclaim: PID %s is unrecognised by argv but its token "
                    "settled identity - retaining the entry",
                    child_pid,
                )
                continue
            # PID was recycled by an unrelated process — prune entry
            entries_to_remove.add(stripped)
            continue

        # The PPid test is a SECOND recycle guard, and a settled token makes it
        # unnecessary. That ordering predates this change and is load-bearing: an
        # orphan does not always reparent to init, because a child placed in its own
        # cgroup scope by the service manager reparents to that user manager, which is
        # a subreaper — so its PPid is neither 1 nor the dead gateway's. Running PPid
        # over a settled token classified every such orphan as recycled and pruned its
        # entry WITHOUT killing it: spared, then forgotten. platform_compat.get_ppid
        # returns -1 on failure (Linux /proc, macOS libproc, Windows snapshot).
        if not token_settled:
            try:
                actual_ppid = platform_compat.get_ppid(child_pid)
            except Exception:
                actual_ppid = -1

            if actual_ppid not in (1, gw_pid, -1):
                # PPid is something else entirely — PID was reused, prune
                entries_to_remove.add(stripped)
                continue

        # Confirmed orphan: kill the process tree
        total_killed, root_killed = _kill_pid_tree(child_pid)
        killed += total_killed
        if root_killed:
            entries_to_remove.add(stripped)
        else:
            # Check if root died between our signal and now
            if not platform_compat.pid_exists(child_pid):
                entries_to_remove.add(stripped)

    # Write back cleaned entries
    if entries_to_remove:
        _write_back_pid_file(entries_to_remove)

    if killed:
        logger.info(
            "cleanup_orphaned_session_roots: settled %d session roots "
            "(completed exact-handle tree drains plus orphan kills)",
            killed,
        )

    return killed


def kill_orphan_mcps(pids: list[int]) -> int:
    """Kill confirmed orphan MCP processes. Uses killpg if isolated, else direct kill.

    Re-verifies cmdline immediately before kill to mitigate PID-reuse TOCTOU.

    POSIX-only: the whole flow depends on process groups (``os.getpgrp`` /
    ``os.killpg`` / ``os.getpgid``) and ``signal.SIGKILL``, none of which exist
    on Windows. On Windows the orphan sweep is a no-op — the tree-kill after a
    session ends already went through ``taskkill /T``.
    """
    if platform_compat.IS_WINDOWS:
        return 0
    my_pgid = os.getpgrp()
    my_pid = os.getpid()
    killed = 0
    # Parent->children map for the subtree reap, built at most ONCE per sweep
    # (one full /proc pass) and only when a marked MCP orphan is actually
    # confirmed -- the common sweep finds none and pays nothing.
    child_map: dict[int, list[int]] | None = None
    for pid in pids:
        if killed >= _ORPHAN_SWEEP_MAX_KILLS:
            break
        if pid == my_pid:
            continue
        try:
            # Start identity FIRST -- before any other read about this pid.
            # Everything below (the cmdline, the eligibility verdict, the pgid)
            # is evidence about whichever process held this PID at the moment it
            # was read, so capturing identity after any of them leaves a window
            # where the orphan exits, the PID is reused, and that stale evidence
            # licenses signalling the replacement. There is no earlier point.
            root_token = _pid_start_token(pid)
            # Re-verify identity right before kill (TOCTOU mitigation):
            # PID may have been recycled between find and kill phases.
            if sys.platform == "linux":
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            else:
                cmdline = subprocess.check_output(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            if _is_sweepable_orphan_mcp(pid, cmdline):
                pgid = os.getpgid(pid)
                # ── PID-recycle invariant ──────────────────────────────
                # No signal in this branch reaches a PID whose start identity
                # was not captured BEFORE any other read about it and
                # re-confirmed IMMEDIATELY before the signal. The three signal
                # sites are this root killpg, this root kill, and each
                # descendant's kill inside _kill_orphan_mcp_descendants (guarded
                # there, with its own live parent-edge check).
                #
                # Enumerate the subtree BEFORE signalling the root: once the root
                # dies its children reparent to init and the parent links this
                # walk needs are gone.
                if child_map is None:
                    child_map = _build_child_map()
                subtree = _orphan_descendants(pid, child_map)
                # ── Descendants FIRST, root LAST ───────────────────────
                # The root is the handle on this tree: it is marked and
                # sweepable, so while it lives the whole tree stays
                # re-enumerable on a later sweep. Killing it before the
                # descendants are accounted for is what loses that handle --
                # when the tree exceeds the kill cap the survivors can include
                # the UNMARKED intermediate, which reparents to init, is not
                # sweepable, and hides its marked children behind a non-init
                # ppid. That is precisely the leak this function exists to
                # close, so the ordering below is load-bearing, not stylistic.
                #
                # Observed shape, produced by any launcher wrapper that resolves
                # a package and then execs the resolved binary:
                #     <wrapper> mcp start-server <pkg>      <- marked
                #       -> <wrapper> mcp start-server ...   <- marked
                #         -> node .../bin/<pkg>-server      <- UNMARKED
                #           -> npm exec <pkg>@latest        <- marked
                # One host accumulated 112 such processes (15.2 GB RSS) over 23
                # days of sweeps that were running the whole time.
                #
                # Reaping descendants first also makes the killpg below pure
                # belt-and-braces for anything still sharing the root's group:
                # a launcher that ``setsid``-s its payload escapes killpg
                # entirely, which is why the explicit walk exists at all.
                killed += _kill_orphan_mcp_descendants(
                    subtree, root=pid, budget=_ORPHAN_SWEEP_MAX_KILLS - killed
                )
                if killed >= _ORPHAN_SWEEP_MAX_KILLS:
                    # Budget spent on the subtree. Leave the root ALIVE and
                    # unsignalled: it stays a marked, sweepable candidate, so the
                    # next sweep re-enumerates what is left of this tree with a
                    # fresh budget. Killing it here would strand the survivors
                    # behind an unsweepable ancestor.
                    logger.debug(
                        "Orphan MCP sweep: kill cap reached on the subtree of root "
                        "pid=%d — leaving the root alive so the remainder stays "
                        "discoverable next sweep",
                        pid,
                    )
                    continue
                # Revalidate the FULL evidence set immediately before signalling
                # the root: identity, eligibility, and the group being targeted.
                # The token alone is not enough -- it proves the process, not that
                # the argv still qualifies it or that it is still in this group.
                live_token = _pid_start_token(pid)
                if root_token is None or live_token is None or live_token != root_token:
                    # Unproven or changed identity: never signal a PID that may
                    # now belong to someone else. An unavailable token is never
                    # read as a match -- see _pid_start_token's contract -- and a
                    # genuine orphan is re-reaped next sweep.
                    logger.debug(
                        "Orphan MCP sweep: skipping root pid=%d — identity changed or "
                        "unavailable across the subtree scan (pre=%r post=%r)",
                        pid,
                        root_token,
                        live_token,
                    )
                    continue
                try:
                    if sys.platform == "linux":
                        live_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                    else:
                        live_cmdline = subprocess.check_output(
                            ["ps", "-o", "command=", "-p", str(pid)],
                            stderr=subprocess.DEVNULL,
                            timeout=2,
                        )
                    if not _is_sweepable_orphan_mcp(pid, live_cmdline):
                        continue  # no longer qualifies — do not signal it
                    if os.getpgid(pid) != pgid:
                        continue  # left the group; that group is no longer ours
                except (OSError, subprocess.SubprocessError):
                    continue  # exited between the token read and here
                if pgid == pid and pgid != my_pgid and pgid > 1:
                    os.killpg(pgid, signal.SIGKILL)
                    killed += 1
                    _sel_orphan_kill(pid, pgid, cmdline, "killpg")
                else:
                    # Candidate already passed UID + orphan-ppid + positive MCP
                    # marker + two-phase active-PID re-verify + the full
                    # evidence revalidation above. Routed through the
                    # platform_compat shim like the work-tree reaper (exception
                    # types are identical on POSIX).
                    #
                    # This signals the root ALONE. Its descendants were already
                    # reaped explicitly above, which is what this commit adds:
                    # the older reasoning here -- that surviving children with an
                    # MCP marker get reclaimed on a subsequent sweep, and that
                    # unmarked ones were never candidates -- is what the
                    # 112-process leak falsified. An UNMARKED intermediate IS a
                    # candidate yet is not sweepable, so it never reparents into
                    # view and keeps its marked children behind a non-init ppid.
                    platform_compat.kill_pid(pid, platform_compat.SIGKILL)
                    killed += 1
                    _sel_orphan_kill(pid, pgid, cmdline, "kill")
                continue
            # Unreachable-gatewayd orphan: re-verify the FULL identity —
            # the socket-path stat AND the age floor — right before
            # signalling. The age recheck matters: a candidate that exited
            # after the find phase can have its PID recycled by a brand-new
            # gatewayd that has not bound its socket yet, and without the
            # floor that pre-bind daemon would read as "socket absent" and
            # be TERMed. TERM-first so the daemon drains its own backends.
            gw_age = _linux_pid_age(pid) if sys.platform == "linux" else 0.0
            if gw_age >= _ORPHAN_MIN_AGE_SECONDS and _is_sweepable_orphan_gatewayd(cmdline):
                killed += _kill_orphan_gatewayd(pid, cmdline)
                continue
            # Work-class orphan (KIROCREW_SPAWNED marker, no launcher shape).
            # Re-verify the full identity — including the age floor — right
            # before the kill; _is_sweepable_orphan_work fails closed off Linux.
            work_age = _linux_pid_age(pid) if sys.platform == "linux" else 0.0
            if _is_sweepable_orphan_work(pid, cmdline, work_age):
                killed += _kill_orphan_work_tree(
                    pid, cmdline, work_age, budget=_ORPHAN_SWEEP_MAX_KILLS - killed
                )
                continue
            # Stranded browser daemon. Re-verify the FULL identity — argv
            # shape, exec-time environ, live-owner probe and the age floor —
            # immediately before signalling, so a PID recycled since the find
            # phase cannot inherit the verdict.
            daemon_age = _linux_pid_age(pid) if sys.platform == "linux" else 0.0
            if _is_sweepable_orphan_browser_daemon(pid, cmdline, daemon_age):
                live_token = _pid_start_token(pid)
                if root_token is None or live_token is None or live_token != root_token:
                    logger.debug(
                        "Orphan browser sweep: skipping pid=%d — identity changed "
                        "or unavailable before TERM (pre=%r post=%r)",
                        pid,
                        root_token,
                        live_token,
                    )
                    continue
                live_cmdline = _pid_cmdline(pid)
                if not live_cmdline or live_cmdline != cmdline:
                    logger.debug(
                        "Orphan browser sweep: skipping pid=%d — cmdline changed "
                        "or became unreadable before TERM",
                        pid,
                    )
                    continue
                killed += _kill_orphan_browser_daemon(pid, live_cmdline)
        except (
            ProcessLookupError,
            PermissionError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            try:
                # Lazy import: session_pid is imported early by acp.runtime, so
                # a module-level `from kiro_crew.sel import sel` would be circular.
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    session_key="gateway",
                    agent="kirocrew",
                    source="background",
                    tool_name="orphan_mcp_sweep",
                    tool_kind="process_kill",
                    outcome="failed",
                    resources=f"pid={pid}",
                    metadata={"error": str(exc)},
                )
            except Exception:
                logger.debug("SEL orphan-kill audit failed", exc_info=True)
    if killed:
        logger.warning("Orphan MCP sweep: killed %d untracked process(es)", killed)
    return killed


def _kill_orphan_work_tree(pid: int, cmdline: bytes, age_seconds: float, budget: int) -> int:
    """SIGKILL a confirmed work-class orphan and its WHOLE subtree, leaf-first.

    Deliberately NOT :func:`_kill_pid_tree`: that helper only reaps
    descendants that are themselves managed agent runtimes (kiro-cli/claude),
    which is correct for tracked agent PIDs but wrong here — every descendant
    of a marked work orphan inherited the ``KIROCREW_SPAWNED`` environment
    and is sweepable (an orphaned pytest's own python/shim children are
    exactly the processes that pile up).

    Descendants are enumerated once (preorder) and killed in reverse, so
    every process dies before its parent — no child is re-parented away
    mid-kill and the enumeration stays valid. The root goes last. *budget*
    bounds the total SIGKILLs so the caller's global
    :data:`_ORPHAN_SWEEP_MAX_KILLS` cap covers subtree members too; if the
    budget runs out mid-subtree the survivors are re-reaped next sweep cycle.

    Returns the number of processes killed.
    """
    if budget <= 0:
        return 0
    descendants: list[int] = []
    try:
        # circular import: session_pid → acp.client → session → session_pid
        from kiro_crew.acp.client import _get_child_pids

        descendants = _get_child_pids(pid)
    except Exception:
        logger.debug("Error enumerating descendants of work orphan %s", pid, exc_info=True)
    my_pid = os.getpid()
    basename = _work_orphan_basename(cmdline).decode("utf-8", errors="replace")
    killed = 0
    for target in [*reversed(descendants), pid]:
        if killed >= budget:
            break  # global kill cap exhausted; next sweep cycle finishes the job
        if target <= 0 or target == my_pid:
            continue
        try:
            platform_compat.kill_pid(target, platform_compat.SIGKILL)
            killed += 1
            if target == pid:
                logger.info(
                    "Orphan work sweep: SIGKILL pid=%d basename=%s age=%ds "
                    "reason=KIROCREW_SPAWNED work orphan (reparented to init)",
                    target,
                    basename,
                    int(age_seconds),
                )
            else:
                logger.info(
                    "Orphan work sweep: SIGKILL pid=%d reason=descendant of work orphan %d",
                    target,
                    pid,
                )
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if killed:
        _sel_orphan_work_kill(pid, basename, age_seconds, cmdline, killed)
    return killed


# ── The split-out owners ──
# This module keeps the reaper's kill passes: the startup and periodic sweeps of the tracked
# pids, the pass over orphaned MCP children in ``kiro_pids.txt``, the session-root sweep, the
# kill phase of the untracked-orphan sweep, and the two tree kills they end in, which walk
# descendants through the ACP layer; and the synchronous provider teardown with the orphaned
# spawn-group signalling. The tracking files, the harness classification, the session RSS,
# the mapping prune, the orphan classification and the orphan kills live in the owners below.
# Every name an owner defines still resolves as ``kiro_crew.session_pid.<name>``, and a write
# through this module (``monkeypatch.setattr``, ``mock.patch``) reaches every owner holding the
# name (``kiro_crew._facade_mirror``). No owner imports this module; a higher owner imports a
# lower one. The owners log as ``kiro_crew.session_pid``. The header keeps every import the
# unsplit module bound, so each still resolves here; the ones only the owners read carry
# ``noqa: F401``.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.session_pid_tracking",
    "kiro_crew.session_pid_harness",
    "kiro_crew.session_pid_rss",
    "kiro_crew.session_pid_mappings",
    "kiro_crew.session_pid_orphan_scan",
    "kiro_crew.session_pid_orphan_kill",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(sys.modules[__name__], _OWNERS, excluded=()).served)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.session_pid_harness import (  # noqa: F401
        _HARNESS_INTERPRETERS,
        _LAUNCHER_INTERPRETER_NAME_PREFIX,
        _LEGACY_AGENT_MARKERS,
        _MANAGED_AGENT_BASENAMES,
        _MANAGED_AGENT_MARKERS,
        _MANAGED_AGENT_RUNTIME_BASENAMES,
        _NODE_ADAPTER_ENTRY_SEGMENTS,
        _SCOPE_REAP_ANCHOR_NAMES,
        _argv_tokens,
        _basename_names_a_harness,
        _basename_of,
        _cmdline_names_a_harness,
        _harness_naming_tokens,
        _namespace_launcher_script_dir,
        _namespace_launcher_shape,
        _sandbox_launcher_wrapped_argv,
        _token_is_node_adapter_entry,
    )
    from kiro_crew.session_pid_mappings import (  # noqa: F401
        _LINK_NOFOLLOW,
        _RETRACT_LOCK,
        _RETRACT_SUFFIX,
        _SESSION_TOKEN_TTL_SECS,
        _discard,
        _is_recycled_record,
        _mapping_may_return,
        _private_name,
        _prune_session_pid_files_pass,
        _restore_no_replace,
        _retract_recycled_mapping,
        _retract_sidecar,
        _retraction_leftover_is_orphaned,
        _settle_retraction_leftover,
    )
    from kiro_crew.session_pid_orphan_kill import (  # noqa: F401
        _BROWSER_DAEMON_TERM_GRACE_SECONDS,
        _GATEWAYD_TERM_GRACE_SECONDS,
        _pid_parent_and_token,
        _prune_from_orphan_walk,
        _sel_orphan_mcp_subtree_kill,
    )
    from kiro_crew.session_pid_orphan_scan import (  # noqa: F401
        _BROWSER_DAEMON_ENTRY,
        _BROWSER_PLAUSIBLE_OWNER_NAMES,
        _BROWSER_PLAUSIBLE_OWNER_PREFIXES,
        _BROWSER_SESSION_ENV,
        _BROWSER_SESSION_PREFIX,
        _GATEWAY_MARKERS,
        _GATEWAYD_MODULE,
        _MARKED_MCP_LAUNCHER_MARKERS,
        _MCP_ENTRYPOINT_MARKERS,
        _ORPHAN_WORK_MIN_AGE_SECONDS,
        _PID_VANISHED_ERRORS,
        _REAPABLE_PID_FIELD,
        _SANDBOX_CREDENTIAL_HELPER_ARG,
        _SANDBOX_CREDENTIAL_HELPER_ARGV0_SUFFIX,
        _SANDBOX_LAUNCHER_MARKER,
        _XDIST_BOOTSTRAP,
        _browser_daemon_session_arg,
        _browser_process_name_is_plausible,
        _browser_session_owner_alive,
        _browser_sweep_decision,
        _env_is_sandbox_tool,
        _env_spawn_home,
        _env_value,
        _gatewayd_socket_arg,
        _is_agent_runtime_anchor,
        _is_generated_browser_session,
        _is_marked_sandbox_credential_helper,
        _is_untracked_managed_agent_orphan,
        _linux_pid_sid,
        _linux_session_leader_alive,
        _our_orphan_pids,
        _parse_etime,
        _read_env_has_kirocrew_marker,
        _read_tracked_agent_pids,
        _reported_untracked_agent_pids,
        _tracked_agent_pids,
        _work_orphan_session_leader_alive,
        _work_sweep_cmdline_is_test_runner,
        confirm_untracked_agent_runtimes,
        find_orphan_mcp_candidates,
        reported_untracked_agent_pids,
        tracked_agent_pid_owners,
        tracked_session_roots,
    )
    from kiro_crew.session_pid_rss import (  # noqa: F401
        _PAGE_SIZE,
        _off_linux_tree_mb,
        _read_rss_pages,
        _rss_ceiling_inert_warned,
        _rss_mb_from_tree,
        _warn_rss_ceiling_inert_once,
        get_session_rss_mb,
    )
    from kiro_crew.session_pid_tracking import (  # noqa: F401
        _FILETIME_TICKS_PER_SECOND,
        _FILETIME_UNIX_EPOCH_OFFSET_SECONDS,
        _PID_FILE,
        _PROTECTED_LOCK,
        _PROTECTED_PIDS,
        _SESSION_PID_FILE,
        SWEEP_SPAWN_GRACE_SECONDS,
        _collect_active_pids,
        _pid_age_seconds,
        _pid_gone_or_unmanaged,
        _protected_pids,
        _recorded_start_token,
        _replace_child_pids,
        _track_child_pids,
        _track_pid,
        _track_session_pid,
        _untrack_child_pids,
        _untrack_pid,
        _untrack_pid_if_dead,
        _untrack_root_by_identity,
        _untrack_session_pid,
        register_protected_pid,
        retained_gateway_pids,
        retire_windows_tree_tracking,
        unregister_protected_pid,
    )
