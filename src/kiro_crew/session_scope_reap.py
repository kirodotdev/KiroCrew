"""Reap abandoned agent cgroup scopes by reconciling the cgroup tree.

Each agent session is spawned inside a transient ``systemd-run --user --scope``
placed under a per-instance child of ``kirocrew-agents.slice`` (see
:func:`sandbox._agents_slice_name`). ``--scope`` garbage-collects a transient
unit only *after its process exits*, so a hard gateway kill or restart that
strands the tree leaves the scope resident forever: the leader dies, the
``launcher``/``kiro-cli``/``kiro-cli-chat`` + MCP children reparent to the
systemd user manager, and nothing GCs the unit. The idle/RSS watchdog iterates
only ``SessionManager._sessions``; the PID sweeps know only tracked roots; the
untracked-orphan sweep (:func:`session_pid._is_untracked_managed_agent_orphan`)
is report-only. The population of scopes is therefore the only authority on what
this instance leaked, which is what this reaper reads.

The reaper is Linux/systemd-only and a no-op everywhere else, or wherever cgroup
v2 delegation is absent (the same gate :func:`sandbox._probe_cgroup_scope` uses
to decide whether to wrap a spawn at all). It runs on every session-cleanup
tick, never on the gateway boot path (``AUTOSDE.yaml``
``no-new-work-on-gateway-boot-path``: an orphan sweep whose cost scales with
leaked state must not delay ``KIROCREW_READY``); the first tick after a restart
picks up whatever the previous gateway stranded.

Safety is a conjunction, per scope, before a single signal is sent
(``docs/system-specs/modules/session.md`` §Reaping abandoned agent scopes):

1. no member PID is a tracked root/child (``session_pid`` readers) nor a live
   provider in ``SessionManager`` (the caller's ``active_pids``);
2. at least one member has positive agent-runtime argv identity (the generated
   launcher, a managed runtime, or a marked MCP launcher), authorizing the
   scope-wide stop; and EVERY member is this install's own: it carries the
   ``KIROCREW_SPAWNED`` marker, or its ``ppid`` chain reaches a marker-bearing
   member without leaving the scope (env-clearing grandchildren such as
   ``chrome-headless`` under a playwright daemon); an unreadable ``environ``
   fails closed;
3. the group leader (members' pgid) is dead, OR the scope's
   ``ActiveEnterTimestampMonotonic`` predates this gateway's boot; and
4. the scope is older than the module's conservative grace floor.

Reclaim is ``systemctl --user stop <unit>``; a fallback SIGTERM -> 3s -> SIGKILL
walks a *freshly re-read* ``cgroup.procs``, pins each process with a pidfd, and
re-verifies ownership before each signal, so a recycled PID is never signalled.
Every reclaimed scope emits a SEL event, as the ``session_pid`` sweeps do.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.constants import KIROCREW_SPAWNED_ENV
from kiro_crew.session_pid import (
    _is_agent_runtime_anchor,
    _pid_cmdline,
    _read_env_has_kirocrew_marker,
)

logger = logging.getLogger(__name__)

#: Grace between the fallback SIGTERM and the escalation SIGKILL. The scope's
#: processes have already lost their leader, so this is a courtesy drain for
#: MCP children to flush, not a supervised shutdown budget.
_TERM_GRACE_SECS = 3.0

# A scope cannot be reclaimed until this floor is comfortably wider than the
# spawn-to-tracking window, so a registration append still in flight is safe.
_REAP_MIN_AGE_SECS = 600

#: Cached gateway boot stamp on the systemd monotonic clock (microseconds), or
#: ``None`` when it cannot be derived on this platform. Resolved once: the
#: gateway process does not restart within its own lifetime.
_GATEWAY_BOOT_MONOTONIC_US: int | None = None
_GATEWAY_BOOT_RESOLVED = False


@dataclass
class ReapSummary:
    """Outcome of one reap sweep."""

    supported: bool = True
    reason: str = ""
    scanned: int = 0
    reclaimed: int = 0
    skipped: int = 0


# ── clock / identity helpers ────────────────────────────────────────────────


def gateway_boot_monotonic_us() -> int | None:
    """This gateway process's start, on systemd's ``CLOCK_MONOTONIC`` (µs).

    systemd stamps ``ActiveEnterTimestampMonotonic`` in ``CLOCK_MONOTONIC``
    microseconds, so a scope's stamp and this value are directly comparable. We
    derive the gateway's start by subtracting its own elapsed runtime from
    ``CLOCK_MONOTONIC`` now. Elapsed is measured on ``CLOCK_BOOTTIME`` (which
    ``/proc/self/stat`` field 22 counts from), and the two clocks differ only by
    time spent suspended -- which makes the derived start slightly *earlier*
    than the truth, so the "predates gateway boot" arm errs toward NOT reaping.
    Returns ``None`` off Linux or on any read failure; the leader-dead arm then
    carries condition 3 alone.
    """
    if sys.platform != "linux":
        return None
    try:
        now_mono = time.clock_gettime(time.CLOCK_MONOTONIC)
        now_boot = time.clock_gettime(time.CLOCK_BOOTTIME)
        stat = Path("/proc/self/stat").read_text(encoding="utf-8")
        # comm (field 2) may contain spaces and ')' -- split after the LAST ')'.
        after = stat.rsplit(")", 1)[1].split()
        start_ticks = int(after[19])  # field 22 overall (starttime), 0-indexed 19 here
        clk_tck = os.sysconf("SC_CLK_TCK")
        if clk_tck <= 0:
            return None
        elapsed = now_boot - (start_ticks / clk_tck)
        return int((now_mono - elapsed) * 1_000_000)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _cached_gateway_boot_us() -> int | None:
    global _GATEWAY_BOOT_MONOTONIC_US, _GATEWAY_BOOT_RESOLVED
    if not _GATEWAY_BOOT_RESOLVED:
        _GATEWAY_BOOT_MONOTONIC_US = gateway_boot_monotonic_us()
        _GATEWAY_BOOT_RESOLVED = True
    return _GATEWAY_BOOT_MONOTONIC_US


def _scope_active_enter_us(unit_name: str) -> int | None:
    """``ActiveEnterTimestampMonotonic`` (µs) for *unit_name*, or ``None``.

    ``0`` (systemd's "never entered active" sentinel) and any parse failure both
    return ``None`` so the predates-boot arm cannot fire on missing data.
    """
    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        return None
    try:
        out = subprocess.run(
            [
                systemctl,
                "--user",
                "show",
                unit_name,
                "-p",
                "ActiveEnterTimestampMonotonic",
                "--value",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = out.stdout.strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value or None


# ── /proc + cgroup readers (proc_root/scope_dir are test seams) ──────────────


def _read_cgroup_procs(scope_dir: Path) -> list[int]:
    """PIDs listed in ``<scope_dir>/cgroup.procs`` (empty on any failure)."""
    try:
        raw = (scope_dir / "cgroup.procs").read_text(encoding="utf-8")
    except OSError:
        return []
    pids: list[int] = []
    for token in raw.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 0:
            pids.append(pid)
    return pids


def _pid_alive(pid: int, proc_root: Path) -> bool:
    return (proc_root / str(pid)).exists()


def _pid_pgrp(pid: int, proc_root: Path) -> int | None:
    """Process-group id from ``/proc/<pid>/stat`` field 5, or ``None``.

    ``comm`` (field 2) is parenthesised and may itself contain spaces and
    ``)``, so the numeric fields are read after the LAST ``)``: there, index 0
    is ``state``, 1 is ``ppid``, 2 is ``pgrp``.
    """
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        after = stat.rsplit(")", 1)[1].split()
        return int(after[2])
    except (ValueError, IndexError):
        return None


def _pid_ppid(pid: int, proc_root: Path) -> int | None:
    """Parent pid from ``/proc/<pid>/stat`` field 4 (index 1 after ``)``)."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        after = stat.rsplit(")", 1)[1].split()
        return int(after[1])
    except (ValueError, IndexError):
        return None


def _scope_owned_pids(pids: list[int], proc_root: Path) -> tuple[set[int], str]:
    """``(owned members, reason)`` -- *reason* is non-empty when ANY member is not ours.

    Ownership is by TREE, not by per-process environ. The spawn wrapper stamps
    ``KIROCREW_SPAWNED`` on the exec'd root and every child that inherits its
    environment, but a grandchild that clears its environment keeps none of it
    -- ``chrome-headless`` renderers under a playwright ``node`` daemon carry no
    marker at all, and a leaked playwright ``cliDaemon`` tree is exactly the
    multi-GB survivor users report. So a member is owned when it carries the
    marker itself, OR its ``ppid`` chain reaches a marker-bearing member without
    leaving the scope's own member set.

    The decision path treats a non-empty *reason* as "not reclaimable": the
    reaper never stops a scope it cannot fully attribute. The reclaim fallback
    uses *owned* alone, so a stranger (a recycled PID, a foreign process placed
    in our cgroup, an unreadable environ) is skipped while our own members are
    still signalled.
    """
    members = set(pids)
    marked: set[int] = set()
    unmarked: list[int] = []
    reason = ""
    for pid in pids:
        marker = _read_env_has_kirocrew_marker(pid, proc_root)
        if marker is None:
            reason = reason or f"pid {pid} environ unreadable"
        elif marker:
            marked.add(pid)
        else:
            unmarked.append(pid)
    owned = set(marked)
    for pid in unmarked:
        cur = pid
        seen: set[int] = set()
        while True:
            parent = _pid_ppid(cur, proc_root)
            if parent is None or parent <= 1 or parent not in members or parent in seen:
                reason = reason or f"pid {pid} missing {KIROCREW_SPAWNED_ENV} marker"
                break
            if parent in owned:
                owned.add(pid)
                break
            seen.add(parent)
            cur = parent
    return owned, reason


def _scope_has_agent_runtime_anchor(pids: list[int], proc_root: Path) -> bool:
    """True when at least one member positively identifies an agent runtime.

    This authorizes a scope-wide stop; it is intentionally existential. Other
    members (notably env-clearing ``chrome-headless`` descendants) remain
    reclaimable through :func:`_scope_owned_pids` once one sibling anchors the
    scope. Cmdline and environ reads retain the fixture ``proc_root`` seam.
    """
    for pid in pids:
        cmdline = _pid_cmdline(pid, proc_root)
        marked = _read_env_has_kirocrew_marker(pid, proc_root) is True
        if _is_agent_runtime_anchor(cmdline, has_kirocrew_marker=marked):
            return True
    return False


def _leaders_dead(pids: list[int], proc_root: Path) -> bool:
    """True when every process-group leader of *pids* is gone.

    A live leader that is still a leader (``pgrp == pid``) means the owning
    session may still be driving the tree -- fail closed to "alive". An
    unreadable pgrp is inconclusive and also fails closed.
    """
    pgids: set[int] = set()
    for pid in pids:
        pg = _pid_pgrp(pid, proc_root)
        if pg is None or pg <= 0:
            return False
        pgids.add(pg)
    for pg in pgids:
        if not _pid_alive(pg, proc_root):
            continue
        # A recycled PID that is NOT itself a group leader does not resurrect
        # ownership; a live true leader (pgrp == self) does.
        if _pid_pgrp(pg, proc_root) == pg:
            return False
    return True


# ── per-scope decision ───────────────────────────────────────────────────────


def _scope_reclaimable(
    scope_dir: Path,
    *,
    proc_root: Path,
    active_pids: set[int],
    tracked_pids: set[int],
    gateway_boot_us: int | None,
    min_age_secs: int,
    now_monotonic: float,
    active_enter_us: Callable[[str], int | None],
) -> tuple[bool, str, float | None]:
    """Evaluate the four-condition conjunction for one scope directory."""
    pids = _read_cgroup_procs(scope_dir)
    if not pids:
        # No members: either a mid-spawn unit or an already-drained shell.
        # Signalling nothing is pointless and stopping a racing spawn is unsafe,
        # so leave empty scopes to systemd's own --collect GC.
        return False, "no members", None

    enter_us = active_enter_us(scope_dir.name)
    age = _scope_age_secs(enter_us, pids, proc_root, now_monotonic)

    # (i) nothing tracked / no live provider tree.
    for pid in pids:
        if pid in active_pids:
            return False, f"pid {pid} is a live provider", age
        if pid in tracked_pids:
            return False, f"pid {pid} is tracked", age

    # (ii-a) every member is ours -- by marker or by descent from a marked
    # member inside this scope; an unreadable environ fails closed.
    _owned, why = _scope_owned_pids(pids, proc_root)
    if why:
        return False, why, age

    # (ii-b) ownership alone is not scope-wide stop authority: intentional
    # detached work inherits the marker too. At least one member must still
    # positively identify the abandoned agent-runtime tree this reaper owns.
    if not _scope_has_agent_runtime_anchor(pids, proc_root):
        return False, "no agent-runtime anchor in scope", age

    # (iii) leader dead OR scope predates this gateway's boot.
    leaders_dead = _leaders_dead(pids, proc_root)
    predates_boot = (
        enter_us is not None and gateway_boot_us is not None and enter_us < gateway_boot_us
    )
    if not (leaders_dead or predates_boot):
        return False, "leader alive and scope postdates gateway boot", age

    # (iv) older than the reap threshold. Prefer the scope's own active-enter
    # stamp; fall back to the youngest member's /proc age when it is absent.
    if age is None:
        return False, "age unknown", None
    if age <= min_age_secs:
        return False, f"age {age:.0f}s <= {min_age_secs}s threshold", age

    return (
        True,
        f"reclaimable (members={len(pids)} leaders_dead={leaders_dead} "
        f"predates_boot={predates_boot} age={age:.0f}s)",
        age,
    )


def _skip_reason_category(reason: str) -> str:
    """Stable operator-facing bucket for one per-scope skip reason."""
    if "is tracked" in reason:
        return "tracked"
    if "live provider" in reason:
        return "live-provider"
    if "marker" in reason or "environ unreadable" in reason:
        return "unowned"
    if reason == "no agent-runtime anchor in scope":
        return "no-runtime-anchor"
    if reason.startswith("leader alive"):
        return "leader-alive"
    if reason == "age unknown":
        return "age-unknown"
    if reason.startswith("age "):
        return "too-young"
    if reason == "no members":
        return "no-members"
    return "other"


def _scope_age_secs(
    enter_us: int | None,
    pids: list[int],
    proc_root: Path,
    now_monotonic: float,
) -> float | None:
    """Age of the scope in seconds, from its active-enter stamp when present.

    ``enter_us`` is on ``CLOCK_MONOTONIC`` (µs), the same clock as
    ``now_monotonic``. When the stamp is absent, fall back to the *youngest*
    member's ``/proc`` age (a scope is at least as old as its youngest live
    process), which is monotonic-independent.
    """
    if enter_us is not None:
        return max(0.0, now_monotonic - enter_us / 1_000_000)
    youngest: float | None = None
    for pid in pids:
        age = _pid_age_secs(pid, proc_root)
        if age is None:
            continue
        youngest = age if youngest is None else min(youngest, age)
    return youngest


def _pid_age_secs(pid: int, proc_root: Path) -> float | None:
    """Age of *pid* from ``/proc/<pid>/stat`` starttime, or ``None``.

    ``starttime`` (field 22) is in clock ticks from boot on the same base as
    ``CLOCK_BOOTTIME``, so ``boottime_now - starttime`` is the process age. Used
    only as a fallback when the scope's active-enter stamp is unavailable.
    """
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        after = stat.rsplit(")", 1)[1].split()
        start_ticks = int(after[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    if clk_tck <= 0:
        return None
    return max(0.0, uptime - start_ticks / clk_tck)


# ── reclaim ──────────────────────────────────────────────────────────────────


def _systemctl_stop(unit_name: str) -> bool:
    """``systemctl --user stop <unit>``; True on a clean exit."""
    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        return False
    try:
        out = subprocess.run(
            [systemctl, "--user", "stop", unit_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _pidfd_signal_owned(
    pid: int,
    sig: int,
    members: list[int],
    scope_dir: Path,
    proc_root: Path,
) -> tuple[bool, str]:
    """Pin *pid*, re-verify membership and ownership, then signal it.

    The pidfd is opened before membership and ownership are read. A process
    recycled after the open cannot retarget the fd. A process that inherits the
    PID before the open is not placed in this scope, so the post-pin membership
    read distinguishes it from the dead member we intended to signal.
    ``reason`` is non-empty only when the host cannot safely perform this signal.
    """
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return False, "pidfd signalling unavailable"
    try:
        fd = pidfd_open(pid)
    except ProcessLookupError:
        return False, ""
    except OSError as exc:
        return False, f"pidfd_open failed ({exc.errno})"
    try:
        # A pidfd pins the process object, not its cgroup. A process that reused
        # this number before the pin is not a member of the abandoned scope.
        if pid not in _read_cgroup_procs(scope_dir):
            return False, ""
        owned, _why = _scope_owned_pids(members, proc_root)
        if pid not in owned:
            return False, ""
        try:
            pidfd_send_signal(fd, sig)
        except ProcessLookupError:
            return False, ""
        except OSError as exc:
            return False, f"pidfd_send_signal failed ({exc.errno})"
        return True, ""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _reclaim_scope(
    scope_dir: Path,
    unit_name: str,
    *,
    proc_root: Path,
    stop_unit: Callable[[str], bool],
    signal_owned: Callable[[int, int, list[int], Path, Path], tuple[bool, str]],
    sleep: Callable[[float], None],
) -> bool:
    """Stop *unit_name*, then SIGTERM -> grace -> SIGKILL survivors.

    Every signal is preceded by a fresh ``cgroup.procs`` read, a pidfd pin, and
    ownership re-verification. ``pid <= 1`` and the gateway's own PID are never
    signalled.
    """
    stop_unit(unit_name)
    if not _read_cgroup_procs(scope_dir):
        return True

    my_pid = os.getpid()
    refusal_reasons: set[str] = set()

    def warn_refusals() -> None:
        if refusal_reasons:
            logger.warning(
                "agent_scope_reap signalling skipped unit=%s reasons=%s",
                unit_name,
                ",".join(sorted(refusal_reasons)),
            )

    for sig in (signal.SIGTERM, signal.SIGKILL):
        remaining = _read_cgroup_procs(scope_dir)
        if not remaining:
            warn_refusals()
            return True
        sent_any = False
        for pid in remaining:
            if pid <= 1 or pid == my_pid:
                continue
            sent, reason = signal_owned(pid, sig, remaining, scope_dir, proc_root)
            sent_any = sent_any or sent
            if reason:
                refusal_reasons.add(reason)
        if sig is signal.SIGTERM and sent_any:
            sleep(_TERM_GRACE_SECS)
    warn_refusals()
    return not _read_cgroup_procs(scope_dir)


def _sel_scope_reap(unit_name: str, member_count: int, reason: str, outcome: str) -> None:
    """Emit one SEL audit event per reclaimed (or failed) scope."""
    try:
        # Lazy import: sel pulls in heavy modules and this file is imported
        # early by the session cleanup path.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="gateway",
            agent="kirocrew",
            source="background",
            tool_name="agent_scope_reap",
            tool_kind="process_kill",
            outcome=outcome,
            resources=f"unit={unit_name} members={member_count}",
            metadata={"reason": reason[:200]},
        )
    except Exception:
        logger.debug("SEL agent-scope-reap audit failed", exc_info=True)


# ── slice resolution + orchestration ─────────────────────────────────────────


def _instance_scope_dir() -> tuple[Path | None, str]:
    """This install's per-instance agent-slice cgroup directory.

    Returns ``(dir, "")`` on success, or ``(None, reason)``. Only the
    per-instance CHILD slice (``kirocrew-agents-<token>.slice``) is returned:
    the bare shared parent cannot be attributed to one install, so a degraded
    token (no per-instance child) is treated as "nothing to reap here" rather
    than reaching into a co-resident gateway's scopes.
    """
    from kiro_crew import sandbox

    parent = sandbox._agents_slice_cgroup_dir()
    if parent is None:
        return None, "agents slice cgroup dir absent"
    child_name = sandbox._agents_slice_name()
    if child_name == sandbox._CGROUP_AGENTS_SLICE:
        return None, "per-instance slice token unavailable (shared slice not reapable)"
    inst = parent / child_name
    if not inst.is_dir():
        return None, "per-instance slice has no cgroup dir (no scopes)"
    return inst, ""


def reap_scopes(
    slice_dir: Path,
    *,
    active_pids: set[int],
    tracked_pids: set[int],
    gateway_boot_us: int | None,
    min_age_secs: int,
    now_monotonic: float,
    proc_root: Path = Path("/proc"),
    stop_unit: Callable[[str], bool] = _systemctl_stop,
    signal_owned: Callable[
        [int, int, list[int], Path, Path], tuple[bool, str]
    ] = _pidfd_signal_owned,
    sleep: Callable[[float], None] = time.sleep,
    active_enter_us: Callable[[str], int | None] = _scope_active_enter_us,
) -> ReapSummary:
    """Testable core: evaluate and reclaim every ``*.scope`` under *slice_dir*.

    Every seam (``proc_root``, ``stop_unit``, ``signal_owned``, ``sleep``,
    ``active_enter_us``) is injectable so the whole decision + reclaim path runs
    against a fake cgroup/``/proc`` tree with no real systemd or signals.
    """
    summary = ReapSummary()
    skipped_reasons: Counter[str] = Counter()
    has_old_skipped_scope = False
    try:
        children = sorted(p for p in slice_dir.iterdir() if p.suffix == ".scope" and p.is_dir())
    except OSError as exc:
        summary.reason = f"cannot list slice dir: {exc}"
        return summary
    for scope_dir in children:
        summary.scanned += 1
        unit_name = scope_dir.name
        reclaimable, reason, age = _scope_reclaimable(
            scope_dir,
            proc_root=proc_root,
            active_pids=active_pids,
            tracked_pids=tracked_pids,
            gateway_boot_us=gateway_boot_us,
            min_age_secs=min_age_secs,
            now_monotonic=now_monotonic,
            active_enter_us=active_enter_us,
        )
        logger.debug(
            "agent_scope_reap unit=%s reclaimable=%s reason=%s", unit_name, reclaimable, reason
        )
        if not reclaimable:
            summary.skipped += 1
            skipped_reasons[_skip_reason_category(reason)] += 1
            has_old_skipped_scope = has_old_skipped_scope or (
                age is not None and age > min_age_secs
            )
            continue
        members = len(_read_cgroup_procs(scope_dir))
        cleared = _reclaim_scope(
            scope_dir,
            unit_name,
            proc_root=proc_root,
            stop_unit=stop_unit,
            signal_owned=signal_owned,
            sleep=sleep,
        )
        _sel_scope_reap(unit_name, members, reason, "completed" if cleared else "failed")
        if cleared:
            summary.reclaimed += 1
        else:
            summary.skipped += 1
            logger.warning("agent_scope_reap could not fully clear unit=%s", unit_name)
    if has_old_skipped_scope:
        counts = " ".join(f"{name}={count}" for name, count in sorted(skipped_reasons.items()))
        logger.info("agent_scope_reap: skipped old scope(s): %s", counts)
    return summary


def reap_abandoned_agent_scopes(active_pids: set[int] | None = None) -> ReapSummary:
    """Production entry: reap this install's abandoned agent scopes.

    A no-op (``supported=False``) off Linux, without cgroup v2 delegation, or
    when this install has no per-instance agent-slice cgroup directory. Gathers
    tracked PIDs from the ``session_pid`` files itself; ``active_pids`` (the
    live provider/pool/in-flight PIDs) is supplied by the caller because only
    the ``SessionManager`` knows them. Runs synchronously and blocks on
    subprocesses -- callers dispatch it to a worker thread.
    """
    summary = ReapSummary()
    if sys.platform != "linux":
        summary.supported = False
        summary.reason = "not Linux"
        return summary

    from kiro_crew import sandbox

    available, reason = sandbox._probe_cgroup_scope()
    if not available:
        summary.supported = False
        summary.reason = f"cgroup delegation unavailable: {reason}"
        return summary

    slice_dir, why = _instance_scope_dir()
    if slice_dir is None:
        summary.supported = True
        summary.reason = why
        return summary

    from kiro_crew.session_pid import _read_tracked_agent_pids

    tracked, complete = _read_tracked_agent_pids()
    if not complete:
        logger.warning("agent_scope_reap: tracked-pid snapshot incomplete; sweep skipped")
        summary.reason = "tracked-pid snapshot incomplete"
        return summary
    result = reap_scopes(
        slice_dir,
        active_pids=set(active_pids or set()),
        tracked_pids=tracked,
        gateway_boot_us=_cached_gateway_boot_us(),
        min_age_secs=_REAP_MIN_AGE_SECS,
        now_monotonic=time.clock_gettime(time.CLOCK_MONOTONIC),
    )
    if result.reclaimed:
        logger.info(
            "agent_scope_reap: reclaimed %d abandoned scope(s) of %d scanned (%d skipped)",
            result.reclaimed,
            result.scanned,
            result.skipped,
        )
    return result
