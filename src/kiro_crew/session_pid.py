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
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
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
    macOS) — deliberately NOT ``ps``, so the token lookup itself is non-blocking
    and safe to call from the asyncio event loop. (Whether an enclosing tracker
    may run on the loop is governed by that tracker's exclusive file lock, not
    by this lookup — see ``AUTOSDE: no-blocking-call-on-event-loop``.)

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


def _rewrite_pid_file(path: Path, content: str) -> bool:
    """Replace *path*'s content atomically; log and return ``False`` on failure.

    Atomic (temp file + rename) because a plain ``write_text`` truncates the
    file to zero BEFORE writing the kept entries: a failure inside that window
    leaves a SHORT file whose surviving content is perfectly well-formed, and
    every dropped entry is an agent runtime no reaper can find again.

    A failure here is REPORTED, not propagated. Pruning an entry is idempotent
    and self-retrying: the next sweep — or the next gateway start — re-reads the
    file, finds that PID already dead, and prunes it again, so a failed rewrite
    costs one stale line rather than a runtime. Propagating would be worse than
    the problem: ``cleanup_orphaned_sessions`` runs unguarded on the gateway's
    startup path, and on Windows ``replace_with_retry`` deliberately declines to
    retry a sharing violation while an event loop is running (an indexer or AV
    scanner holding the temp file is enough), so an escaping error there aborts
    startup entirely.

    The tracking direction is the opposite case and must NOT be quieted this
    way: failing to RECORD a freshly spawned PID is unrecoverable, because no
    reaper can identify that runtime afterwards.
    """
    try:
        atomic_write(path, content)
        return True
    except OSError:
        logger.error(
            "Could not rewrite PID file %s; its entries stay until the next sweep",
            path,
            exc_info=True,
        )
        return False


# Basenames of agent runtimes whose lifecycle Kiro Crew manages through PID-file
# tracking (kiro_pids.txt / kiro_session_pids.txt). Used to re-validate tracked
# PIDs before a kill, and as a NEGATIVE gate in the work-orphan sweep: these
# runtimes are reclaimed by their own tracked-PID sweep, never by the
# marker-based work sweep (see _is_sweepable_orphan_work).
_MANAGED_AGENT_MARKERS: tuple[str, ...] = ("kiro-cli", "claude")


def _is_managed_agent_process(pid: int) -> bool:
    """Check if a PID belongs to an agent process managed by KiroCrew (guards against PID recycling)."""
    return platform_compat.process_matches(pid, _MANAGED_AGENT_MARKERS)


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
    """Remove *killed_or_dead* entries from the session PID file.

    Rewrites via :func:`atomic_write` (temp file + rename). A plain
    ``write_text`` truncates the file to zero BEFORE writing the kept entries,
    so a crash or a write failure inside that window leaves a SHORT file whose
    surviving content is perfectly well-formed — every dropped entry becomes an
    agent runtime no reaper can ever find again, with nothing raised and
    nothing logged. Rename makes the file either wholly old or wholly new.
    """
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if path.exists():
            current = path.read_text(encoding="utf-8").splitlines()
            keep = [
                entry for entry in current if entry.strip() and entry.strip() not in killed_or_dead
            ]
            _rewrite_pid_file(path, ("\n".join(keep) + "\n") if keep else "")


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
            _rewrite_pid_file(path, "\n".join(kept) + "\n" if kept else "")

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
        identity_confirmed = False
        if recorded_token is not None:
            live_token = _pid_start_token(child_pid)
            if live_token is not None and live_token != recorded_token:
                entries_to_remove.add(stripped)
                continue
            if live_token is None:
                continue  # identity unknown — retain entry, retry next sweep
            identity_confirmed = True

        # Fallback PID-reuse guard for entries with NO recorded token (Windows,
        # or a failed token probe at spawn): verify PPid is 1 (reparented to
        # init) or the dead gateway PID (race window). A recycled PID would have
        # a completely different parent. platform_compat.get_ppid returns -1 on
        # failure (Linux /proc, macOS libproc, Windows snapshot). This is only
        # reached when the token could not establish identity.
        if not identity_confirmed:
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
        _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


def _untrack_pid(pid: int) -> None:
    """Remove a PID from the tracking file."""
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in lines if ln.strip() != str(pid)]
        _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


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
        _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


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
_GATEWAYD_MODULE = b"kiro_crew.mcp_gateway.gatewayd"
_GATEWAY_MARKERS = (
    _GATEWAYD_MODULE,
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

# ── Stranded playwright-cli browser daemon (issue #5986) ─────────────────────
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

# Grace given to a TERMed browser-daemon GROUP before escalating to SIGKILL.
# Chromium exits on TERM within a second or two; this leaves room for a profile
# flush without letting a wedged tree hold the sweep's budget.
_BROWSER_DAEMON_TERM_GRACE_SECONDS = 5.0


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


def _env_value(pid: int, key: str) -> bytes | None:
    """Exec-time environment value for *key* in *pid*, or ``None`` if unset.

    Deliberately PROPAGATES ``OSError`` instead of swallowing it like
    :func:`_env_has_kirocrew_marker`: the callers here need to tell "read
    said the key is absent" apart from "the read failed", because those two
    outcomes must fail closed in OPPOSITE directions -- an absent owner
    permits a kill, an unreadable one must forbid it. Linux-only; returns
    ``None`` elsewhere so every caller fails closed off Linux.
    """
    if sys.platform != "linux":
        return None
    prefix = key.encode() + b"="
    environ = Path(f"/proc/{pid}/environ").read_bytes()
    for item in environ.split(b"\x00"):
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


def _browser_session_owner_alive(pid: int, session: bytes) -> bool:
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

    FAIL-CLOSED to "alive": an unreadable ``/proc`` listing or an inconclusive
    per-process read (EACCES, EIO) returns ``True``, so the sweep never kills
    on a failed probe. A process that VANISHES mid-scan is simply not an
    owner, which is the one error that is safe to skip.
    """
    if sys.platform != "linux":
        return True
    try:
        entries = [e for e in Path("/proc").iterdir() if e.name.isdigit()]
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
        if _linux_pid_sid(other) == pid:
            continue  # the daemon's own detached tree, not an owner
        try:
            if _env_value(other, _BROWSER_SESSION_ENV) == session:
                return True
        except _PID_VANISHED_ERRORS:
            continue
        except OSError:
            return True
    return False


def _is_sweepable_orphan_browser_daemon(pid: int, cmdline: bytes, age_seconds: float) -> bool:
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
    what made the previously withdrawn reapers unsafe.
    """
    if age_seconds < _ORPHAN_WORK_MIN_AGE_SECONDS:
        return False
    if not cmdline:
        return False  # kernel thread / zombie — nothing meaningful to kill
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    session = _browser_daemon_session_arg(cmdline)
    if session is None:
        return False
    try:
        if _env_value(pid, _BROWSER_SESSION_ENV) != session:
            return False
    except OSError:
        return False  # inconclusive — fail closed
    if not _env_has_kirocrew_marker(pid):
        return False
    return not _browser_session_owner_alive(pid, session)


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


# Grace given to a TERMed unreachable gatewayd before killpg SIGKILL. TERM is
# sent first, deliberately: gatewayd's signal handler routes into the same
# graceful stop path a supervised shutdown takes, so the daemon drains
# in-flight work and reaps its own pooled backend subprocesses — a direct
# SIGKILL would orphan them for a later sweep instead. Derived from the
# daemon's own total shutdown budget (the same discipline the supervisor's
# SIGTERM→SIGKILL grace follows) so the escalation can never fire while a
# correctly-draining daemon is still inside its drain window.
_GATEWAYD_TERM_GRACE_SECONDS = float(TOTAL_SHUTDOWN_BUDGET_SECS)


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


def _work_orphan_basename(cmdline: bytes) -> bytes:
    """argv0 basename from a raw cmdline (NUL-separated Linux, space macOS)."""
    args = cmdline.split(b"\x00")
    if len(args) == 1:
        args = cmdline.split(b" ")
    return args[0].rsplit(b"/", 1)[-1]


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
    if any(marker.encode() in basename for marker in _MANAGED_AGENT_MARKERS):
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
# recycled silently suppress a genuine leak report — exactly the silence issue
# #2930 is about. A bare line names its own process, whichever file it is in.
_REAPABLE_PID_FIELD: tuple[tuple[str, int], ...] = (
    ("session", 1),  # kiro_session_pids.txt: <gateway_pid>:<child_pid>[:start-id]
    ("child", 0),  # kiro_pids.txt: <child_pid>:<parent_pid>
)


def _tracked_agent_pids() -> set[int]:
    """PIDs a reaper can terminate, per both tracking files.

    Both files are read because a runtime absent from BOTH is exactly what
    :func:`_is_untracked_managed_agent_orphan` reports, and each reaper keys off
    only one of them. Within a line only the reapable field counts — see
    :data:`_REAPABLE_PID_FIELD` for which, and why the owner field does not. A
    session entry's third field is a start-time identity, numeric on Linux, and
    is never read as a PID.

    Deliberately read WITHOUT either file lock. Readers cannot tear: rewrites
    go through :func:`_rewrite_pid_file` (temp file + rename, so a reader sees
    either the whole old or the whole new content) and tracking appends are
    single short lines. Locking here would put a lock acquisition inside the
    sweep's per-scan path for a purely diagnostic read. Any read failure yields
    the entries found so far — the detector is report-only, so the worst
    outcome is one spurious or one missing log line, never a kill.
    """
    tracked: set[int] = set()
    paths = (_session_pid_file_path(), _pid_file_path())
    for path, (_label, reapable_index) in zip(paths, _REAPABLE_PID_FIELD):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue  # absent or unreadable — nothing this file can claim
        for line in raw.split():
            fields = line.split(":")
            index = 0 if len(fields) == 1 else reapable_index
            if index >= len(fields):
                continue  # truncated entry — no reapable field to read
            try:
                value = int(fields[index])
            except ValueError:
                continue  # malformed or partially-appended line
            if value > 0:
                tracked.add(value)
    return tracked


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

    This grants NO kill authority and is wired to nothing that terminates — a
    hit only logs. Blast radius is therefore zero, which is what makes the
    detector safe to ship ahead of a maintainer's ruling on whether an
    untracked runtime may be reaped at all. It also means a cross-data-home
    false positive (a second install's live runtime, tracked in ITS config dir
    and so absent from ours) is diagnostic noise rather than a wrong kill.
    """
    if not cmdline:
        return False  # kernel thread / zombie — no argv to identify
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    basename = _work_orphan_basename(cmdline)
    if not any(marker.encode() in basename for marker in _MANAGED_AGENT_MARKERS):
        return False
    if pid in tracked_pids:
        return False  # a reaper can already reach it
    return _env_has_kirocrew_marker(pid)


def _work_orphan_session_leader_alive(pid: int) -> bool:
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
    sid = _linux_pid_sid(pid)
    if sid <= 0:
        return True  # unreadable — assume the owner is alive, do not sweep
    if sid == pid:
        # The work process became its own session leader (setsid'd daemon):
        # SID carries no ownership information. Assume alive — the shape gate
        # already restricts this path to test runners, and a coordinator that
        # setsid'd itself is not distinguishable from an owned one.
        return True
    leader_sid = _linux_pid_sid(sid)
    return leader_sid == sid  # alive AND still a session leader


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
    now = time.time()

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


def _linux_pid_sid(pid: int) -> int:
    """Session id (SID) from /proc/pid/stat (field 6, index 3 after state).

    The SID of an agent-spawned work process points at the kiro-cli session
    leader that (transitively) spawned it — kiro-cli is started with
    ``start_new_session=True``, so every descendant inherits its SID even
    after the direct parent dies and the process reparents to init. Returns
    -1 when unreadable (caller must fail closed).
    """
    try:
        stat_data = Path(f"/proc/{pid}/stat").read_text()
        close_paren = stat_data.rfind(")")
        fields = stat_data[close_paren + 2 :].split()
        return int(fields[3])  # field 6 (session) = index 3 after state
    except (OSError, ValueError, IndexError):
        return -1


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
            gw_age = _linux_pid_age(pid, time.time()) if sys.platform == "linux" else 0.0
            if gw_age >= _ORPHAN_MIN_AGE_SECONDS and _is_sweepable_orphan_gatewayd(cmdline):
                killed += _kill_orphan_gatewayd(pid, cmdline)
                continue
            # Work-class orphan (KIROCREW_SPAWNED marker, no launcher shape).
            # Re-verify the full identity — including the age floor — right
            # before the kill; _is_sweepable_orphan_work fails closed off Linux.
            work_age = _linux_pid_age(pid, time.time()) if sys.platform == "linux" else 0.0
            if _is_sweepable_orphan_work(pid, cmdline, work_age):
                killed += _kill_orphan_work_tree(
                    pid, cmdline, work_age, budget=_ORPHAN_SWEEP_MAX_KILLS - killed
                )
                continue
            # Stranded browser daemon. Re-verify the FULL identity — argv
            # shape, exec-time environ, live-owner probe and the age floor —
            # immediately before signalling, so a PID recycled since the find
            # phase cannot inherit the verdict.
            daemon_age = _linux_pid_age(pid, time.time()) if sys.platform == "linux" else 0.0
            if _is_sweepable_orphan_browser_daemon(pid, cmdline, daemon_age):
                killed += _kill_orphan_browser_daemon(pid, cmdline)
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


def _pid_cmdline(pid: int) -> bytes:
    """Best-effort argv for *pid* on Linux; ``b""`` when unreadable or off-Linux.

    Empty is inconclusive, never "clean": every caller treats it as fail-closed
    (skip the process) rather than assuming it is safe to touch.

    Off-Linux deliberately has NO ``ps`` branch. Every consumer of this argv
    feeds a decision that also requires :func:`_env_has_kirocrew_marker`, which
    is fail-closed off Linux, so a subprocess here would only ever supply
    evidence for a verdict that is already "refuse".
    """
    if sys.platform != "linux":
        return b""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def _pid_parent_and_token(pid: int) -> tuple[int | None, str | None]:
    """``(ppid, start_token)`` for *pid* from ONE ``/proc/<pid>/stat`` read.

    Both values must come from the SAME read. Reading the parent edge and the
    start identity separately leaves a window in which the PID exits between
    them, so a recycled PID's fresh token gets paired with the dead process's
    parent edge -- and that token then matches at kill time, which is precisely
    how a live worker gets SIGKILLed.

    ``stat`` field 4 is PPid and field 22 is starttime; ``comm`` (field 2) can
    contain spaces and parentheses, so both are read after the LAST ``)``, the
    same way :func:`_build_child_map` and
    ``platform_compat.get_process_start_id`` parse it.

    ``(None, None)`` on any failure, and off Linux -- where the whole subtree
    reap is already a no-op because :func:`_env_has_kirocrew_marker` is
    fail-closed. Callers must treat ``None`` as unproven, never as a mismatch.
    """
    if sys.platform != "linux":
        return (None, None)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        rparen = stat.rfind(")")
        if rparen < 0:
            return (None, None)
        fields = stat[rparen + 2 :].split()
        return (int(fields[1]), fields[19])
    except (OSError, ValueError, IndexError):
        return (None, None)  # exited mid-read or unreadable — fail closed


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
    from; a PID that no longer points back at that parent is a different
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
