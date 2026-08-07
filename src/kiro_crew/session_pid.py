"""Process tracking and orphan cleanup for kiro-cli sessions.

Manages PID files (``kiro_pids.txt`` and ``kiro_session_pids.txt``) that
track spawned kiro-cli processes.  Provides startup cleanup, periodic
sweeping, and per-process track/untrack operations.

See ``session.py`` module docstring for the full Process Sweep Architecture.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.providers.base import LLMProvider

logger = logging.getLogger(__name__)

_PID_FILE = "kiro_pids.txt"
_SESSION_PID_FILE = "kiro_session_pids.txt"

# ── Orphan-sweep spawn grace period ──────────────────────────────────────────
# A freshly spawned kiro-cli PID is tracked in kiro_session_pids.txt immediately
# by _track_session_pid(), but the _starting_pids protection set is only
# populated AFTER provider.start() returns (multi-second window). During this
# window the sweep may classify the PID as orphaned and SIGKILL it. To prevent
# this, any tracked PID younger than SWEEP_SPAWN_GRACE_SECONDS is unconditionally
# skipped (left alive) in _sweep_pid_entries. A missed kill self-heals next
# cycle; a wrong kill does not.
SWEEP_SPAWN_GRACE_SECONDS = 120


def _pid_age_seconds(pid: int, proc_root: str = "/proc") -> float | None:
    """Return the process age in seconds, or None if it cannot be determined.

    On Linux, reads /proc/<pid>/stat field 22 (starttime in clock ticks since
    boot). The comm field (field 2) can contain spaces and parentheses — split
    on the substring AFTER the LAST ')' in the line.

    On macOS (and other POSIX without /proc): derived from
    ``platform_compat.get_process_start_id``, whose darwin value is the process
    start time in epoch ``seconds.microseconds`` — so this needs no
    ``subprocess`` and is safe on the event loop. Empirically required: the
    startup sweep SIGKILL'd a live kiro-cli off a stale dead-gateway entry on
    macOS because the grace window silently did not apply there.

    On Windows: returns None (no grace — sweep behavior unchanged there).

    The *proc_root* parameter allows injection of a fake /proc tree for testing.
    """
    if platform_compat.IS_WINDOWS:
        return None
    if sys.platform != "linux":
        start_id = platform_compat.get_process_start_id(pid)
        if start_id is None:
            return None
        try:
            return max(0.0, time.time() - float(start_id))
        except ValueError:
            return None
    try:
        stat_data = Path(f"{proc_root}/{pid}/stat").read_text()
        # Field 22 is starttime. Fields before it: pid (1), comm (2, in parens,
        # may contain spaces), state (3), ... The reliable parse is to find the
        # LAST ')' — everything after is space-separated fields starting at
        # field 3 (state).
        close_paren = stat_data.rfind(")")
        if close_paren < 0:
            return None
        fields_after_comm = stat_data[close_paren + 2 :].split()
        # starttime is field 22 overall. After comm (field 2), state is field 3
        # which is index 0 of fields_after_comm. So field 22 = index 19.
        starttime_ticks = int(fields_after_comm[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime = float(Path(f"{proc_root}/uptime").read_text().split()[0])
        now = time.time()
        boot_time = now - uptime
        start_seconds = boot_time + (starttime_ticks / clk_tck)
        return now - start_seconds
    except (OSError, ValueError, IndexError):
        return None


def _pid_in_spawn_grace(pid: int) -> bool:
    """Return True if the PID is within the spawn grace period and should be skipped.

    - Windows: returns False (no age source — fall through to existing kill
      behavior so the sweep remains functional there).
    - POSIX (Linux via /proc, macOS via ``ps -o etime=``) + successful age
      read: True if age < SWEEP_SPAWN_GRACE_SECONDS.
    - POSIX + read failure (age is None): True (treat as young — safe
      direction; dead processes are already pruned by the earlier liveness check).
    """
    if platform_compat.IS_WINDOWS:
        return False
    age = _pid_age_seconds(pid)
    if age is None:
        return True  # cannot determine age → treat as young (safe direction)
    return age < SWEEP_SPAWN_GRACE_SECONDS


def _pid_start_token(pid: int) -> str | None:
    """Stable, persistable identity token for a live PID (PID-recycle guard).

    Thin delegate to ``platform_compat.get_process_start_id``, which is
    in-process on every platform (``/proc`` read on Linux, ``libproc`` ctypes on
    macOS) — deliberately NOT ``ps``, so this is safe to call from the asyncio
    event loop via ``_track_session_pid`` at spawn time
    (``AUTOSDE: no-blocking-call-on-event-loop``).

    Returns ``None`` when identity cannot be determined (Windows, or a process
    we may not introspect). Callers MUST treat ``None`` as "unknown", never as a
    mismatch — see the sweep call sites.

    Note this cannot reuse ``acp.client._get_start_time``: that hashes with
    builtin ``hash()``, which is PYTHONHASHSEED-randomized per interpreter and
    therefore meaningless once written to disk and compared by a later gateway.
    """
    return platform_compat.get_process_start_id(pid)


def _pid_file_path() -> Path:
    return config_dir() / _PID_FILE


def _session_pid_file_path() -> Path:
    return config_dir() / _SESSION_PID_FILE


@contextmanager
def _session_pid_file_lock():  # type: ignore[no-untyped-def]
    """Exclusive file lock for session PID file operations."""
    lock_path = _session_pid_file_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        with platform_compat.file_lock(lock_fd.fileno(), exclusive=True):
            yield


def _track_session_pid(pid: int) -> None:
    """Append a kiro-cli PID to the session tracking file (dedup).

    Entries are written as ``<gateway_pid>:<child_pid>:<start_token>`` so each
    gateway instance can identify and sweep only its own children, and so the
    sweep can verify the PID still names the SAME process before killing
    (PID-recycle guard — see ``_pid_start_token``). When no token is available
    (Windows, ``ps`` failure) the legacy ``<gateway_pid>:<child_pid>`` form is
    written and the sweep falls back to cmdline + spawn-grace checks only.
    """
    token = _pid_start_token(pid)
    prefix = f"{os.getpid()}:{pid}"
    entry = f"{prefix}:{token}" if token else prefix
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # Dedup on the gw:pid prefix (not the full entry) so a re-track
            # never duplicates a legacy 2-field line with a 3-field one.
            for line in path.read_text(encoding="utf-8").split():
                if line == prefix or line.startswith(prefix + ":"):
                    return
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{entry}\n")


@contextmanager
def _pid_file_lock():  # type: ignore[no-untyped-def]
    """Exclusive file lock for all PID file read-modify-write operations."""
    lock_path = _pid_file_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        with platform_compat.file_lock(lock_fd.fileno(), exclusive=True):
            yield


def _is_managed_agent_process(pid: int) -> bool:
    """Check if a PID belongs to an agent process managed by KiroCrew (guards against PID recycling)."""
    return platform_compat.process_matches(pid, ("kiro-cli", "claude"))


def _pid_gone_or_unmanaged(pid: int) -> bool:
    """Return ``True`` when it is safe to *untrack* ``pid`` from the PID files.

    Safe means the process is confirmed gone. Returns ``False`` when a process
    with this PID is still alive (or is unsignalable): a teardown kill may have
    failed to reap our agent (``killpg`` misses children in other process
    groups; a mid-init crash can race the descendant scan in ``_kill_process``),
    so the tracking entry is **retained**. The periodic orphan sweep — which
    re-validates ownership via ``_is_managed_agent_process`` before it kills
    anything — then reaps a genuine survivor and skips a recycled PID.
    Untracking a live survivor here would orphan it permanently, since every
    sweep mechanism keys off these files (the ``kiro-cli-chat acp`` memory-leak
    class). Fail-safe: any inconclusive result retains.

    Routes through ``platform_compat.pid_liveness`` (a non-blocking probe, safe
    on the asyncio event loop) rather than a raw ``os.kill(pid, 0)`` — on
    Windows that call TERMINATES the target. This is stricter than upstream
    ``33da30e6``, which untracks on ``PermissionError`` (assumes a recycled,
    other-user PID): ``pid_liveness`` collapses EPERM into ``PID_UNSIGNALABLE``,
    which we treat as "retain", so an unsignalable PID stays tracked for the
    sweep to re-validate off the hot path. Never orphaning a live survivor is
    the invariant that matters; a retained-but-recycled PID is harmless (the
    sweep's ownership recheck skips it). It deliberately does NOT call
    ``_is_managed_agent_process`` (which shells out to ``ps`` on macOS): that
    would block the loop and could mislabel a live-but-transiently-unreadable
    agent as unmanaged — the exact leak this guards against.
    """
    return platform_compat.pid_liveness(pid) == platform_compat.PID_DEAD


def _collect_active_pids(sessions: "dict") -> tuple[set[int], bool]:
    """Extract PIDs from live sessions. Returns ``(pids, ok)``.

    If any session's PID is not an int or extraction fails,
    returns ``(partial_set, False)`` — caller should skip the sweep.
    """
    pids: set[int] = _protected_pids()  # shared _bg / subagent runtimes shielded from the sweep
    for sess in sessions.values():
        # ACP provider: long-lived process PID via client._pid
        client = getattr(sess.provider, "client", None)
        if client is not None:
            try:
                pid = client._pid  # type: ignore[attr-defined]
                if not isinstance(pid, int):
                    logger.warning(
                        "PID for session is not an int (%r) — skipping orphan sweep this cycle", pid
                    )
                    return pids, False
                pids.add(pid)
            except Exception:
                logger.warning("Failed to read PID for session — skipping orphan sweep this cycle")
                return pids, False
        # CC provider: protect long-lived process PID (per_session mode)
        cc_proc = getattr(sess.provider, "_proc", None)
        if cc_proc is not None and cc_proc.returncode is None:
            pids.add(cc_proc.pid)
        # CC provider: protect in-flight subprocess PID (ephemeral mode)
        active_proc = getattr(sess.provider, "_active_proc", None)
        if active_proc is not None and active_proc.returncode is None:
            pids.add(active_proc.pid)
    return pids, True


def _kill_pid_tree(pid: int) -> tuple[int, bool]:
    """Kill *pid* and its descendant kiro-cli processes (bottom-up).

    Returns ``(total_killed, root_killed)`` so callers can distinguish
    whether the root process itself was sent SIGKILL.
    """
    if pid <= 0:
        return 0, False
    killed = 0
    root_killed = False
    try:
        # circular import: session_pid → acp.client → session → session_pid
        from kiro_crew.acp.client import _get_child_pids

        children = _get_child_pids(pid)
        for cpid in reversed(children):
            if cpid <= 0 or not _is_managed_agent_process(cpid):
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


def _write_back_pid_file(killed_or_dead: set[str]) -> None:
    """Remove *killed_or_dead* entries from the session PID file."""
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if path.exists():
            current = path.read_text(encoding="utf-8").splitlines()
            keep = [
                entry for entry in current if entry.strip() and entry.strip() not in killed_or_dead
            ]
            path.write_text(
                ("\n".join(keep) + "\n") if keep else "",
                encoding="utf-8",
            )


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
            if not _is_managed_agent_process(pid):
                killed_or_dead.add(stripped)
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
            if recorded_token is not None:
                live_token = _pid_start_token(pid)
                if live_token is not None and live_token != recorded_token:
                    killed_or_dead.add(stripped)
                    continue
                if live_token is None:
                    continue  # identity unknown — retain entry, retry next sweep
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
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = open(lock_path, "w")
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
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
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
    """Phase 2b: kill confirmed orphans and write back PID file (sync, thread-safe)."""
    orphan_killed = 0
    for pid in confirmed:
        total, root = _kill_pid_tree(pid)
        orphan_killed += total
        if root:
            killed_or_dead.add(f"{my_gw_pid}:{pid}")
        else:
            if not platform_compat.pid_exists(pid):
                killed_or_dead.add(f"{my_gw_pid}:{pid}")
    if killed_or_dead:
        _write_back_pid_file(killed_or_dead)
    return orphan_killed


def _sync_kill_provider(provider: LLMProvider) -> None:
    """Synchronously kill a provider's process.

    Used during CancelledError handling where async shutdown is unreliable
    (asyncio.shield + await raises CancelledError immediately, leaving
    shutdown fire-and-forget).  Falls back to SIGKILL if SIGTERM fails.
    """
    # ACP provider: long-lived process via client._pid
    client = getattr(provider, "_client", None)
    pid = getattr(client, "_pid", None) if client else None
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
        return
    # Only ever signal a real, positive, non-init PID. Test stand-ins are the
    # sharp edge: a Mock attribute passes the None check and coerces to 1 via
    # __index__, so an unguarded os.kill would SIGTERM init / the container
    # entrypoint (observed as a CI sandbox dying with exit 143). pid <= 1 also
    # excludes the kill(0)/kill(-n) process-group semantics outright.
    if not isinstance(pid, int) or pid <= 1:
        logger.debug("_sync_kill_provider: refusing to signal invalid pid %r", pid)
        return
    # On Windows there is no SIGTERM/SIGKILL distinction (taskkill /F is a hard
    # kill) and no os.waitpid for non-child PIDs, so a single kill suffices.
    if platform_compat.IS_WINDOWS:
        # kill_pid raises ProcessLookupError / PermissionError / OSError on a
        # non-zero taskkill rc (same shape POSIX uses). Catch those so the
        # audit log doesn't record a phantom "killed" when nothing was
        # actually terminated.
        try:
            platform_compat.kill_pid(pid, platform_compat.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug(
                "_sync_kill_provider: taskkill did not terminate PID %d (%s)",
                pid,
                exc,
            )
            return
        logger.warning("_sync_kill_provider: killed PID %d for leaked provider", pid)
        return
    for sig in (platform_compat.SIGTERM, platform_compat.SIGKILL):
        try:
            platform_compat.kill_pid(pid, sig)
        except ProcessLookupError:
            return  # already dead
        except OSError:
            return
        if sig == platform_compat.SIGTERM:
            # Brief wait for graceful exit before escalating (POSIX only)
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
    logger.warning("_sync_kill_provider: killed PID %d for leaked provider", pid)


def _cleanup_orphaned_mcp_servers() -> int:
    """Kill tracked child PIDs whose parent kiro-cli session is dead.

    Child entries are stored as ``child_pid:parent_pid`` in ``kiro_pids.txt``.
    A child is orphaned when its parent PID is no longer alive.  Bare PID
    lines (sandbox root PIDs) are pruned when the process is confirmed dead.

    Zero false positives: we only kill PIDs we tracked, and only when the
    specific parent session that spawned them is confirmed dead.
    """
    path = _pid_file_path()
    if not path.exists():
        return 0

    # Hold the lock for the entire read-kill-write cycle so that a concurrent
    # _untrack_child_pids (clean shutdown) cannot remove an entry between our
    # read and our kill decision.  os.kill is non-blocking so lock duration is
    # negligible.
    with _pid_file_lock():
        lines = path.read_text(encoding="utf-8").splitlines()
        killed = 0
        lines_to_remove: set[str] = set()

        for line in lines:
            stripped = line.strip()
            if not stripped:
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
            parts = stripped.split(":", 1)
            try:
                child_pid = int(parts[0])
                parent_pid = int(parts[1])
            except (ValueError, IndexError):
                continue

            # Is the child still alive? (os.kill(pid, 0) would terminate on Windows)
            if not platform_compat.pid_exists(child_pid):
                lines_to_remove.add(stripped)  # confirmed dead — prune
                continue

            # Is the parent session still alive?
            if platform_compat.pid_exists(parent_pid):
                continue  # parent alive (or unknown) — leave child running

            # Parent confirmed dead → child is orphaned — kill it.
            # Guard against PID reuse: if the child was truly ours, its PPid
            # should be 1 (reparented to init) since the parent died. A reused
            # PID would have a different PPid.
            actual_ppid = platform_compat.get_ppid(child_pid)
            if actual_ppid not in (1, parent_pid):
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
            path.write_text(
                "\n".join(kept) + "\n" if kept else "",
                encoding="utf-8",
            )

    return killed


def cleanup_orphaned_sessions() -> None:
    """Kill leftover kiro-cli processes from a previous gateway run.

    Reads ``kiro_session_pids.txt`` (written at spawn time), validates each
    PID still belongs to a kiro-cli process (guards against PID recycling),
    kills descendants bottom-up, then truncates the file.

    Runs at gateway startup before any new sessions are created, so the file
    contains only PIDs from the previous run.

    Also sweeps orphaned MCP server processes via ``_cleanup_orphaned_mcp_servers``
    which uses the separate ``kiro_pids.txt`` (child:parent format).

    Additionally cleans up:
    - Stale ``session_pid_*.txt`` files for processes that no longer exist.
    - Empty directories under ``sessions/`` left by subagents that produced
      no output before timing out.
    """
    # Step 1: Read file under lock (fast I/O only)
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        lines: list[str] = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    # Step 2: Process outside lock (slow: os.kill, _get_child_pids, SIGKILL)
    def _skip_tagged(gw_pid: int, _pid: int) -> bool:
        """Skip if owning gateway is still alive."""
        # pid_exists() returns True on a live PID or one we can't signal
        # (can't tell — preserve), and False only when confirmed dead.
        return platform_compat.pid_exists(gw_pid)

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
    stale_pid_files = 0
    for pid_file in config_dir().glob("session_pid_*.txt"):
        try:
            pid = int(pid_file.stem.removeprefix("session_pid_"))
        except ValueError:
            # Malformed filename (e.g. MagicMock leak) -- safe to delete
            logger.debug("Removing malformed pid file: %s", pid_file.name)
            try:
                pid_file.unlink(missing_ok=True)
                stale_pid_files += 1
            except OSError:
                logger.debug("Could not remove malformed pid file: %s", pid_file.name)
            continue
        # os.kill(pid, 0) would terminate the process on Windows — probe instead.
        if not platform_compat.pid_exists(pid):
            pid_file.unlink(missing_ok=True)
            # Remove the HMAC sidecar (session_pid_<pid>.sig) alongside its
            # .txt — a dangling sidecar is harmless (verification requires
            # both) but would accumulate forever.
            pid_file.with_suffix(".sig").unlink(missing_ok=True)
            stale_pid_files += 1
    if stale_pid_files:
        logger.info("Cleaned up %d stale session PID files", stale_pid_files)

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
    """Kill session root PIDs whose owning gateway is confirmed dead.

    Reads ``kiro_session_pids.txt`` entries (format ``<gateway_pid>:<child_pid>``),
    checks if the gateway PID is alive, and for dead gateways validates the
    child PID is still a kiro-cli process (PID-reuse guard via
    ``_is_managed_agent_process`` and PPid reparent-to-init check) before
    issuing SIGKILL.

    Called periodically from ``session.py``'s ``_cleanup_loop`` to reap
    kiro-cli processes left behind by crashed gateway instances.

    Returns the number of orphaned processes killed.
    """
    path = _session_pid_file_path()
    if not path.exists():
        return 0

    with _session_pid_file_lock():
        lines = path.read_text(encoding="utf-8").splitlines()

    if not lines:
        return 0

    my_gw_pid = os.getpid()
    killed = 0
    entries_to_remove: set[str] = set()

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
        if gw_liveness == platform_compat.PID_ALIVE:
            continue  # gateway alive — its responsibility
        if gw_liveness == platform_compat.PID_UNSIGNALABLE:
            continue  # can't determine — skip
        # gw_liveness == PID_DEAD — orphan candidate

        # Gateway is dead. Check if the child PID is still alive.
        child_liveness = platform_compat.pid_liveness(child_pid)
        if child_liveness == platform_compat.PID_DEAD:
            # Already dead — just prune the entry
            entries_to_remove.add(stripped)
            continue
        if child_liveness == platform_compat.PID_UNSIGNALABLE:
            continue  # can't signal — skip

        # Child is alive. Guard against PID reuse: verify it's still a
        # managed agent process (kiro-cli/claude in cmdline).
        if not _is_managed_agent_process(child_pid):
            # PID was recycled by an unrelated process — prune entry
            entries_to_remove.add(stripped)
            continue

        # Additional PID-reuse guard: verify PPid is 1 (reparented to init)
        # or the dead gateway PID (race window). A recycled PID would have
        # a completely different parent. platform_compat.get_ppid returns
        # -1 on failure (Linux /proc, macOS libproc, Windows snapshot).
        try:
            actual_ppid = platform_compat.get_ppid(child_pid)
        except Exception:
            actual_ppid = -1

        # Valid orphan: PPid should be 1 (reparented to init/systemd) since
        # the original parent (gateway) is dead. Also accept the dead gateway
        # PID itself (brief race window before reparenting completes).
        if actual_ppid not in (1, gw_pid, -1):
            # PPid is something else entirely — PID was reused, prune
            entries_to_remove.add(stripped)
            continue

        # Strongest PID-reuse guard: the entry recorded the child's start
        # token at spawn (see _pid_start_token). A MISMATCH means this PID now
        # names a DIFFERENT process — prune, never kill. An unreadable live
        # token is "identity unknown", not a mismatch: retain the entry so a
        # live genuine orphan is not untracked (and thus leaked forever) on one
        # transient probe failure; the next sweep retries.
        if recorded_token is not None:
            live_token = _pid_start_token(child_pid)
            if live_token is not None and live_token != recorded_token:
                entries_to_remove.add(stripped)
                continue
            if live_token is None:
                continue  # identity unknown — retain entry, retry next sweep

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
            "cleanup_orphaned_session_roots: killed %d orphaned session root processes",
            killed,
        )

    return killed


def _track_pid(pid: int) -> None:
    """Append a PID to the tracking file."""
    with _pid_file_lock():
        path = _pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{pid}\n")


def _track_child_pids(pids: Mapping[int, object], parent_pid: int = 0) -> None:
    """Append descendant PIDs to the tracking file as ``child:parent`` pairs."""
    if not pids:
        return
    with _pid_file_lock():
        path = _pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
        with open(path, "a", encoding="utf-8") as f:
            for pid in pids:
                entry = f"{pid}:{parent_pid}"
                if entry not in existing:
                    f.write(f"{entry}\n")
                    existing.add(entry)


def _untrack_child_pids(pids: Mapping[int, object]) -> None:
    """Remove descendant PIDs from the tracking file."""
    if not pids:
        return
    to_remove = {str(p) for p in pids}
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [
            ln for ln in lines if ":" not in ln.strip() or ln.strip().split(":")[0] not in to_remove
        ]
        path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _untrack_pid(pid: int) -> None:
    """Remove a PID from the tracking file."""
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in lines if ln.strip() != str(pid)]
        path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _untrack_session_pid(pid: int) -> None:
    """Remove this gateway's ``<gw_pid>:<pid>`` entry from the session PID
    tracking file.  Called on clean provider shutdown so the periodic
    orphan sweep doesn't race against legitimate still-running kiro-cli
    processes whose in-memory session entry has transiently gone away
    (e.g. during compaction/reset/replace)."""
    prefix = f"{os.getpid()}:{pid}"
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        # Match both the legacy ``gw:pid`` form and the token-bearing
        # ``gw:pid:token`` form (see _track_session_pid).
        lines = [
            ln for ln in lines if ln.strip() != prefix and not ln.strip().startswith(prefix + ":")
        ]
        path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


# ── Sweep-protected PIDs ──────────────────────────────────────────────────
# Live agent-process PIDs tracked in the PID file but NOT registered as
# SessionMap sessions (e.g. app-managed worker pools / shared ACP runtimes).
# The periodic orphan sweep consults _protected_pids() to avoid killing them.
_PROTECTED_PIDS: set[int] = set()
_PROTECTED_LOCK = threading.Lock()


def register_protected_pid(pid: int) -> None:
    """Shield a live agent-process PID from the periodic orphan sweep.

    For app-managed worker pools whose processes are tracked in the PID file but
    not registered as SessionMap sessions. Pair every call with
    ``unregister_protected_pid`` on worker shutdown/replacement."""
    if isinstance(pid, int) and pid > 0:
        with _PROTECTED_LOCK:
            _PROTECTED_PIDS.add(pid)


def unregister_protected_pid(pid: int) -> None:
    """Drop a PID from the sweep-protected set (worker shut down / replaced)."""
    with _PROTECTED_LOCK:
        _PROTECTED_PIDS.discard(pid)


def _protected_pids() -> set[int]:
    with _PROTECTED_LOCK:
        return set(_PROTECTED_PIDS)


# ── Untracked orphan MCP sweep (defense-in-depth) ──────────
# Catches KiroCrew-spawned MCP subtrees that escaped PID-file tracking.
# Split into find + kill so the caller can re-verify active PIDs between phases.

_ORPHAN_SWEEP_MAX_KILLS = 30
_ORPHAN_MIN_AGE_SECONDS = 120  # Never reap processes younger than this

# A candidate PID can exit between the /proc (or ps) snapshot and the per-PID
# probe. Linux surfaces that as FileNotFoundError/ProcessLookupError reading
# /proc/<pid>/cmdline; macOS as a non-zero `ps -p <pid>` exit. All three mean
# "already gone", which is the sweep's goal — not a failure worth a traceback.
_PID_VANISHED_ERRORS = (
    FileNotFoundError,
    ProcessLookupError,
    subprocess.CalledProcessError,
)

# Entrypoints that positively identify a KiroCrew-spawned MCP/worker process.
# Each marker MUST be unique to a process KiroCrew itself launches — the sweep
# SIGKILLs any user-owned orphan that matches, so a marker naming a server the
# core does not spawn would reap an unrelated process. The upstream project's
# reaper also lists an enterprise-only MCP server it manages, but this public
# fork never spawns that server (the CPP companion contributes it, not the
# core), so that marker is deliberately omitted here.
_MCP_ENTRYPOINT_MARKERS = (
    b"kirocrew_sandbox_",  # sandbox wrapper script (session-spawned)
    b"kiro_crew.mcp_gateway.stub",  # gateway pool worker (not gatewayd itself)
)

# Gateway/CLI entrypoints — these are peer gateways, never orphan MCP targets.
# Checked BEFORE _MCP_ENTRYPOINT_MARKERS to prevent prefix overlap.
_GATEWAY_MARKERS = (
    b"kiro_crew.mcp_gateway.gatewayd",
    b"kiro_crew.cli",
    b"kiro_crew.__main__",
)

# MCP launcher cmdline shapes that carry NO KiroCrew fingerprint (a user's own
# shell can produce identical cmdlines), so matching them requires the
# ``KIROCREW_SPAWNED`` environ marker as positive identity — the public fork's
# only fingerprint-less launcher is the public ``@playwright/mcp`` server, which
# runs as ``npx @playwright/mcp`` -> node (see ``mcp_playwright_proxy``): neither
# its argv0 (``npx``/``node``) nor its args mention KiroCrew, so a grandchild
# escaping the probe/session tree evades the cmdline-fingerprint sweep entirely.
_MARKED_MCP_LAUNCHER_MARKERS = (
    b"@playwright/mcp",  # ``npx @playwright/mcp`` (npx shim + node server)
    b"mcp start-server",  # generic ``<launcher> mcp start-server <name>`` shims
)


def _our_orphan_pids() -> list[int]:
    """PIDs owned by current user whose parent is init (pid 1) or systemd --user.

    POSIX-only: relies on ``os.getuid`` and either ``/proc`` (Linux) or ``ps``
    (macOS). On Windows there is no init/systemd concept and no ``os.getuid``;
    the orphan-sweep is inactive there and returns an empty list.
    """
    if platform_compat.IS_WINDOWS:
        return []
    my_uid = os.getuid()
    # An orphaned process reparents to init (pid 1) or the nearest subreaper
    # (systemd --user), never back to its original launcher. We deliberately do
    # NOT include the gateway's launcher ppid: doing so would widen the
    # candidate set to the launcher's other live children (peer processes from
    # the same shell/tmux/supervisor), adding wrong-kill surface with no
    # orphan-reaping benefit.
    accepted_ppids: set[int] = {1}
    try:
        if sys.platform == "linux":
            # Two /proc passes: pass 1 detects systemd --user subreaper PIDs,
            # pass 2 classifies orphans (needs the complete subreaper set
            # before any child can be matched against accepted_ppids).
            result: list[int] = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != my_uid:
                        continue
                    pid = int(entry.name)
                    # Detect systemd --user (user-session subreaper)
                    try:
                        if (entry / "comm").read_text().strip() == "systemd":
                            accepted_ppids.add(pid)
                    except OSError:
                        pass
                except (OSError, ValueError):
                    continue
            # Second pass now that accepted_ppids is complete
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != my_uid:
                        continue
                    pid = int(entry.name)
                    for ln in (entry / "status").read_text().splitlines():
                        if ln.startswith("PPid:"):
                            parts = ln.split(maxsplit=1)
                            if len(parts) < 2:
                                break
                            if int(parts[1]) in accepted_ppids:
                                result.append(pid)
                            break
                except (OSError, ValueError, IndexError):
                    pass
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
    """True if cmdline matches a KiroCrew MCP entrypoint (not a peer gateway)."""
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


def _env_has_kirocrew_marker(pid: int) -> bool:
    """True if *pid*'s environment carries the ``KIROCREW_SPAWNED`` marker.

    Reads ``/proc/<pid>/environ`` (exec-time environment, same-UID readable).
    Linux-only and FAIL-CLOSED: any read failure — and every non-Linux
    platform, where there is no reliable same-UID environ read — returns
    ``False`` so the marked-launcher sweep path never kills without positive
    identity. macOS/Windows keep the pre-existing cmdline-marker-only behavior.
    """
    if sys.platform != "linux":
        return False
    needle = f"{KIROCREW_SPAWNED_ENV}={KIROCREW_SPAWNED_VALUE}".encode()
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return needle in environ.split(b"\x00")


def _is_sweepable_orphan_mcp(pid: int, cmdline: bytes) -> bool:
    """Positive-identity gate for the orphan sweep (find AND pre-kill re-verify).

    Two independent paths:
    1. cmdline carries a KiroCrew fingerprint (:func:`_is_orphan_mcp`) —
       the pre-existing behavior, works on Linux and macOS.
    2. cmdline is a fingerprint-less MCP launcher shape AND the process
       environ carries the ``KIROCREW_SPAWNED`` marker (catches escaped
       ``npx @playwright/mcp`` trees; Linux-only, fail-closed elsewhere).
    """
    if _is_orphan_mcp(cmdline):
        return True
    return _is_marked_mcp_launcher(cmdline) and _env_has_kirocrew_marker(pid)


def find_orphan_mcp_candidates(active_pids: set[int]) -> list[int]:
    """Scan process table for orphaned MCP processes not in any active set.

    Returns candidate PIDs. Caller should re-verify against fresh active PIDs
    before killing (two-phase pattern to eliminate races).
    """
    candidates: list[int] = []
    my_pid = os.getpid()
    now = time.time()

    for pid in _our_orphan_pids():
        if pid == my_pid or pid in active_pids:
            continue
        try:
            if sys.platform == "linux":
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                # Use /proc/pid/stat field 22 (starttime in clock ticks) for
                # canonical process age — immune to /proc mtime heuristic issues.
                pid_age = _linux_pid_age(pid, now)
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
        if not _is_sweepable_orphan_mcp(pid, cmdline):
            continue
        candidates.append(pid)

    return candidates


def _linux_pid_age(pid: int, now: float) -> float:
    """Process age in seconds using /proc/pid/stat starttime (canonical)."""
    try:
        stat_data = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 is starttime (after comm which may contain spaces/parens)
        close_paren = stat_data.rfind(")")
        fields = stat_data[close_paren + 2 :].split()
        starttime_ticks = int(fields[19])  # field 22 is index 19 after state
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        boot_time = now - uptime
        start_seconds = boot_time + (starttime_ticks / clk_tck)
        return now - start_seconds
    except (OSError, ValueError, IndexError):
        return 0.0  # Cannot determine age — min-age guard will skip


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
    for pid in pids:
        if killed >= _ORPHAN_SWEEP_MAX_KILLS:
            break
        if pid == my_pid:
            continue
        try:
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
            if not _is_sweepable_orphan_mcp(pid, cmdline):
                continue
            pgid = os.getpgid(pid)
            if pgid == pid and pgid != my_pgid and pgid > 1:
                os.killpg(pgid, signal.SIGKILL)
                killed += 1
                _sel_orphan_kill(pid, pgid, cmdline, "killpg")
            else:
                # Candidate already passed UID + orphan-ppid + positive MCP
                # marker + two-phase active-PID re-verify + cmdline re-check.
                # Direct os.kill of the confirmed-orphan PID only — NOT a tree
                # walk. _kill_pid_tree is gated by kiro-cli/claude markers that
                # MCP processes don't carry. If this orphan shares a pgid (not
                # its own group leader) and has children, those children that
                # carry an MCP marker are reclaimed on a subsequent sweep; any
                # without a marker were never sweep candidates to begin with.
                os.kill(pid, signal.SIGKILL)
                killed += 1
                _sel_orphan_kill(pid, pgid, cmdline, "kill")
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


_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _read_rss_pages(pid: int, proc_root: Path | None = None) -> int:
    """Resident *pages* of a single PID via ``/proc/<pid>/statm`` (Linux only).

    Returns pages, NOT MiB: callers accumulate the whole process tree and
    convert to MiB once at the end, so per-PID sub-MiB remainders are not
    truncated away. (A per-PID ``// MiB`` would under-count a tree by up to
    ~1 MiB per process, i.e. the recycle could fire late or never for a tree
    sitting just over the ceiling.) Returns 0 if the process is gone or the
    field can't be read — a missing PID simply contributes nothing to the sum.

    Windows never reaches here: ``get_session_rss_mb`` measures whole trees
    through ``platform_compat.proc_rss_tree_mb_for_pid`` instead.

    *proc_root* overrides the ``/proc`` mount (test seam only).
    """
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        # statm fields are in pages; field 2 (index 1) is resident set size.
        fields = (root / str(pid) / "statm").read_text().split()
        return int(fields[1])
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, OSError):
        return 0


def _build_child_map(proc_root: Path | None = None) -> dict[int, list[int]]:
    """Parent-PID -> direct-children map from one pass over ``/proc/<pid>/stat``.

    Reads the ``PPid`` (4th) field of every process's ``stat`` file. This is
    authoritative and complete for all live processes regardless of kernel
    config, and deliberately replaces the earlier
    ``/proc/<pid>/task/*/children`` walk, which requires
    ``CONFIG_CHECKPOINT_RESTORE``/``CONFIG_PROC_CHILDREN`` and is documented as
    reliable only for frozen/stopped tasks — for a live task it could return an
    incomplete child set, silently dropping whole descendant subtrees from the
    RSS sum (so the memory-protection feature could no-op with no signal).

    A failure to scan ``/proc`` is logged at debug rather than swallowed
    silently, so a degraded reading is diagnosable.

    Windows deliberately has NO branch here and returns an empty map: Toolhelp's
    ``th32ParentProcessID`` is never cleared when a parent exits and Windows
    recycles PIDs aggressively, so a raw parent->child walk can attach an
    unrelated subtree to a recycled PID -- which would let the watchdog recycle a
    healthy session. ``get_session_rss_mb`` routes Windows through
    ``platform_compat.proc_rss_tree_mb_for_pid``, which validates every
    parent->child edge against creation/exit times, instead of coming here.

    *proc_root* overrides the ``/proc`` mount (test seam only).
    """
    root = proc_root if proc_root is not None else Path("/proc")
    child_map: dict[int, list[int]] = {}
    try:
        for entry in root.iterdir():
            name = entry.name
            if not name.isdigit():
                continue
            try:
                # Format: "pid (comm) state ppid ...". comm can contain spaces
                # and parentheses, so locate the LAST ')' and read ppid after
                # it rather than naively splitting on whitespace.
                stat = (entry / "stat").read_text()
                rparen = stat.rfind(")")
                ppid = int(stat[rparen + 2 :].split()[1])
            except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, OSError):
                # Process exited mid-scan or stat unreadable — skip this PID.
                continue
            child_map.setdefault(ppid, []).append(int(name))
    except (FileNotFoundError, OSError):
        logger.debug("RSS watchdog: /proc scan for child map failed", exc_info=True)
    return child_map


def _rss_mb_from_tree(
    pid: int,
    child_map: dict[int, list[int]],
    exclude_pids: set[int] = frozenset(),  # type: ignore[assignment]
    proc_root: Path | None = None,
) -> int:
    """RSS (MiB) of *pid* + its descendant tree using a PREBUILT child map.

    Split out from ``get_session_rss_mb`` so a caller measuring many session
    trees in one sweep can build the ``/proc`` parent->child map ONCE (via
    ``_build_child_map``) and reuse it across every tree, rather than re-scanning
    all of ``/proc`` per tree. The map is read-only here, so it is safe to share
    across sequential/threaded calls. Any PID in *exclude_pids* is skipped along
    with its subtree. Resident pages are summed across the tree and converted to
    MiB once at the end.
    """
    total_pages = 0
    seen: set[int] = set()
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        if current in seen or current in exclude_pids:
            continue
        seen.add(current)
        total_pages += _read_rss_pages(current, proc_root)
        frontier.extend(child_map.get(current, ()))
    return (total_pages * _PAGE_SIZE) // (1024 * 1024)


def get_session_rss_mb(
    pid: int,
    exclude_pids: set[int] = frozenset(),  # type: ignore[assignment]
    proc_root: Path | None = None,
) -> int:
    """Total RSS (MiB) of *pid* plus its descendant tree, via ``/proc``.

    Single-tree convenience: builds the parent->child map with one
    ``/proc/*/stat`` scan (see ``_build_child_map``) and delegates to
    ``_rss_mb_from_tree``. To measure MANY trees in one sweep, build the map
    once with ``_build_child_map()`` and call ``_rss_mb_from_tree()`` per tree so
    ``/proc`` is scanned only once, not once per tree.

    Any PID in *exclude_pids* is skipped along with the entire subtree beneath
    it — a defensive barrier so a caller can exclude a shared sub-tree (e.g. a
    pooled backend). Resident pages are summed and converted to MiB once at the
    end, so the reading is not biased downward by per-PID truncation.

    *proc_root* overrides the ``/proc`` mount (test seam only).

    Linux reads ``/proc``. Windows has neither ``/proc`` nor a safe parent->child
    walk (see ``_build_child_map``), so it delegates to
    ``platform_compat.proc_rss_tree_mb_for_pid``, which sums only
    lineage-validated descendants; without that the ceiling measured every tree
    as 0 MiB there and no session was ever recycled. macOS has no ctypes-only
    per-pid RSS path, so it returns 0 and the ceiling stays inert.

    *exclude_pids* is honoured on the ``/proc`` route. The Windows route derives
    its own validated descendant set, so a caller that needs a subtree barrier
    there must exclude the pid before calling.
    """
    if platform_compat.IS_WINDOWS and proc_root is None:
        tree_mb = platform_compat.proc_rss_tree_mb_for_pid(pid)
        return 0 if tree_mb is None else int(tree_mb)
    if sys.platform != "linux":
        return 0
    child_map = _build_child_map(proc_root)
    return _rss_mb_from_tree(pid, child_map, exclude_pids, proc_root)
