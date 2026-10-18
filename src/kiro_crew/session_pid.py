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
    _is_sweepable_orphan_browser_daemon,
    _is_sweepable_orphan_gatewayd,
    _is_sweepable_orphan_mcp,
    _is_sweepable_orphan_work,
    _linux_pid_age,
)
from kiro_crew.session_pid_provider_kill import (
    _root_identity_holds,
    _tracked_child_has_runtime_identity,
)
from kiro_crew.session_pid_rss import _build_child_map
from kiro_crew.session_pid_tracking import (
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
# descendants through the ACP layer. The tracking files, the harness classification, the
# session RSS, the mapping prune, the orphan classification, the provider teardown and the
# orphan kills live in the owners below.
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
    "kiro_crew.session_pid_provider_kill",
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
        _env_spawn_instance,
        _env_value,
        _gatewayd_socket_arg,
        _is_agent_runtime_anchor,
        _is_generated_browser_session,
        _is_marked_mcp_launcher,
        _is_marked_sandbox_credential_helper,
        _is_orphan_mcp,
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
    from kiro_crew.session_pid_provider_kill import (  # noqa: F401
        _PIDFD_ABSENT_ERRNOS,
        _PROVIDER_TERM_GRACE_SECONDS,
        _PROVIDER_TERM_POLL_SECONDS,
        _commit_teardown,
        _group_from_witnessed_descendant,
        _isolated_provider_group,
        _marked_group_members,
        _pgroup_has_member_besides,
        _pgroup_still_ours,
        _pid_exited_but_unreaped,
        _pid_in_pgroup,
        _provider_descendant_records,
        _provider_tree_gone,
        _ProviderChildRecord,
        _reap_provider_root,
        _release_client_seed_registrations,
        _root_identity_refuted,
        _signal_orphaned_runtime_group,
        _signal_pid_by_identity,
        _signal_provider_descendants,
        _sync_kill_provider,
        _vouch_and_signal_orphaned_group,
        group_vouching_available,
        signal_orphaned_spawn_group,
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
