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
   launcher, a managed runtime, or a marked MCP launcher), OR every member is a
   marked toolbox sandbox credential helper -- a scope with nothing else left in
   it, the helper being the one member that routinely outlives the runtime it
   served; either way authorizing the
   scope-wide stop; and EVERY member is this install's own: it carries the
   ``KIROCREW_SPAWNED`` marker, or its ``ppid`` chain reaches a marker-bearing
   member without leaving the scope (env-clearing grandchildren such as
   ``chrome-headless`` under a playwright daemon); an unreadable ``environ``
   fails closed;
3. the group leader (members' pgid) is dead, OR the scope's
   ``ActiveEnterTimestampMonotonic`` predates this gateway's boot; and
4. the scope is older than the module's conservative grace floor.

Reclaim is ``systemctl --user stop <unit>``; a fallback SIGTERM -> grace -> SIGKILL
walks a *freshly re-read* ``cgroup.procs``, pins each process with a pidfd, and
re-verifies its identity (or, for a member not attributed before the stop, its
ownership) before each signal, so a recycled PID is never signalled.
Every reclaimed scope emits a SEL event, as the ``session_pid`` sweeps do.
"""

from __future__ import annotations

import functools
import logging
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from kiro_crew import platform_compat
from kiro_crew.constants import KIROCREW_SPAWNED_ENV
from kiro_crew.runtime_ownership import (
    RUNTIME_TENANCY,
    authorize_runtime_kill,
    commit_runtime_teardown,
    outstanding_leases,
    release_runtime_teardown,
    tenancy_epoch,
)
from kiro_crew.session_pid import (
    _is_agent_runtime_anchor,
    _is_marked_sandbox_credential_helper,
    _pid_cmdline,
    _read_env_has_kirocrew_marker,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

#: Grace between the fallback SIGTERM and the escalation SIGKILL. The scope's
#: processes have already lost their leader, so this is a courtesy drain for
#: MCP children to flush, not a supervised shutdown budget.
_TERM_GRACE_SECS = 3.0

#: After SIGKILL, how long the reclaim waits for the killed members to leave
#: ``cgroup.procs`` before judging it: a killed task stays listed until its exit
#: completes, so a read taken at once audits a kill that worked as a failure.
_KILL_SETTLE_SECS = 2.0

#: How often ``cgroup.procs`` is re-read while waiting for signalled members.
_EXIT_POLL_SECS = 0.1

# A scope cannot be reclaimed until this floor is comfortably wider than the
# spawn-to-tracking window, so a registration append still in flight is safe.
_REAP_MIN_AGE_SECS = 600

#: Cached gateway boot stamp on the systemd monotonic clock (microseconds), or
#: ``None`` when it cannot be derived on this platform. Resolved once: the
#: gateway process does not restart within its own lifetime.
_GATEWAY_BOOT_MONOTONIC_US: int | None = None
_GATEWAY_BOOT_RESOLVED = False

#: A scope error that keeps recurring is re-reported at WARNING this often.
_SCOPE_ERROR_REWARN_SECS = 3600.0

#: ``(unit, phase) -> (exception type, monotonic time of its last WARNING)`` for
#: errors a sweep raised; phase is ``"evaluation"`` or ``"reclaim"``. A recurring
#: error warns again once the interval passes or its type changes. A key is
#: dropped by the first sweep that does not raise it (the scope checked clean, or
#: is gone), so a clean check re-arms the warning and the map stays bounded by
#: the scopes failing.
_SCOPE_ERRORS_WARNED: dict[tuple[str, str], tuple[str, float]] = {}


#: ``pid -> start_ticks`` of the members a reclaim attributed before its stop.
_Pinned = Mapping[int, int]

#: The signal seam: ``(pid, sig, members, scope_dir, proc_root, pinned)``.
_SignalOwned = Callable[[int, int, list[int], Path, Path, _Pinned], tuple[bool, str]]


@dataclass
class ReapSummary:
    """Outcome of one reap sweep."""

    supported: bool = True
    reason: str = ""
    scanned: int = 0
    reclaimed: int = 0
    skipped: int = 0


# ── clock / identity helpers ────────────────────────────────────────────────


def gateway_boot_monotonic_us(proc_root: Path = Path("/proc")) -> int | None:
    """This gateway process's start, on systemd's ``CLOCK_MONOTONIC`` (µs).

    systemd stamps ``ActiveEnterTimestampMonotonic`` in ``CLOCK_MONOTONIC``
    microseconds, so a scope's stamp and this value are directly comparable. We
    derive the gateway's start by subtracting its own elapsed runtime from
    ``CLOCK_MONOTONIC`` now. Elapsed is measured on ``CLOCK_BOOTTIME`` (which
    ``/proc/<pid>/stat`` field 22 counts from), and the two clocks differ only by
    time spent suspended -- which makes the derived start slightly *earlier*
    than the truth, so the "predates gateway boot" arm errs toward NOT reaping.
    The stat line is read as bytes, so a gateway whose ``comm`` is not UTF-8
    still gets its stamp. Returns ``None`` off Linux or on any read failure; the
    leader-dead arm then carries condition 3 alone.
    """
    if sys.platform != "linux":
        return None
    # MONOTONIC first: any delay before the boot-clock read inside the age then
    # lengthens the elapsed time, which moves the derived start EARLIER.
    now_mono = time.clock_gettime(time.CLOCK_MONOTONIC)
    stat = platform_compat.read_proc_stat(os.getpid(), proc_root=proc_root)
    if stat is None or stat.start_ticks is None:
        return None
    elapsed = platform_compat.process_age_secs(stat.start_ticks)
    if elapsed is None:
        return None
    return int((now_mono - elapsed) * 1_000_000)


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
            timeout=5,
            **UTF8_TEXT,
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


class _ProcReads:
    """One evaluation's ``/proc`` reads, each made at most once per pid.

    Lazy, so a scope rejected before a rule needs a field pays nothing for it;
    memoized, so every rule of one decision -- and the reclaim that follows --
    judges the same process, and an ``environ`` read (the one that can block on
    a process's mmap lock) is not repeated. The per-signal re-verification
    takes a FRESH instance, never the decision's. Deliberately not a mapping:
    a ``__getitem__`` would let ``in`` and iteration walk ``/proc/0``, ``/1``, ...
    """

    __slots__ = ("proc_root", "stat", "marker", "cmdline")

    def __init__(self, proc_root: Path) -> None:
        self.proc_root = proc_root

        @functools.cache
        def stat(pid: int) -> platform_compat.ProcStat:
            return (
                platform_compat.read_proc_stat(pid, proc_root=proc_root)
                or platform_compat.ProcStat()
            )

        @functools.cache
        def marker(pid: int) -> bool | None:
            return _read_env_has_kirocrew_marker(pid, proc_root)

        @functools.cache
        def cmdline(pid: int) -> bytes:
            return _pid_cmdline(pid, proc_root)

        self.stat = stat
        self.marker = marker
        self.cmdline = cmdline


def _scope_owned_pids(pids: list[int], reads: _ProcReads) -> tuple[set[int], str]:
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

    Only unmarked members' chains read ``ppid``.
    """
    members = set(pids)
    marked: set[int] = set()
    unmarked: list[int] = []
    reason = ""
    for pid in pids:
        marker = reads.marker(pid)
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
            parent = reads.stat(cur).ppid
            if parent is None or parent <= 1 or parent not in members or parent in seen:
                reason = reason or f"pid {pid} missing {KIROCREW_SPAWNED_ENV} marker"
                break
            if parent in owned:
                owned.add(pid)
                break
            seen.add(parent)
            cur = parent
    return owned, reason


def _scope_has_agent_runtime_anchor(pids: list[int], reads: _ProcReads) -> bool:
    """True when at least one member positively identifies an agent runtime.

    This authorizes a scope-wide stop; it is intentionally existential. Other
    members (notably env-clearing ``chrome-headless`` descendants) remain
    reclaimable through :func:`_scope_owned_pids` once one sibling anchors the
    scope. Cmdline and environ reads retain the fixture ``proc_root`` seam.
    """
    for pid in pids:
        marked = reads.marker(pid) is True
        if _is_agent_runtime_anchor(reads.cmdline(pid), has_kirocrew_marker=marked):
            return True
    return False


def _scope_is_only_credential_helpers(pids: list[int], reads: _ProcReads) -> bool:
    """True when EVERY member of a non-empty scope is a marked credential helper.

    The second, UNIVERSAL authorization: the toolbox's credential helper serves
    one runtime and routinely outlives it, so a scope holding nothing else has
    no client left and only holds threads. The helper is kept out of
    :func:`_is_agent_runtime_anchor` precisely because that arm is existential --
    one member would authorize stopping every sibling, including a detached
    server the user meant to keep -- and "every member is a helper" is the same
    observation without that reach: any other survivor, marked or not, withdraws
    the authorization and the scope falls back to needing a runtime anchor.

    Each member's own marker is required, so an unreadable ``environ`` denies
    the whole rule rather than one member.
    """
    if not pids:
        return False
    for pid in pids:
        if reads.marker(pid) is not True:
            return False
        if not _is_marked_sandbox_credential_helper(reads.cmdline(pid)):
            return False
    return True


def _leaders_dead(pids: list[int], reads: _ProcReads) -> bool:
    """True when every process-group leader of *pids* is gone.

    A live leader that is still a leader (``pgrp == pid``) means the owning
    session may still be driving the tree -- fail closed to "alive". An
    unreadable pgrp is inconclusive and also fails closed.
    """
    pgids: set[int] = set()
    for pid in pids:
        pg = reads.stat(pid).pgrp
        if pg is None or pg <= 0:
            return False
        pgids.add(pg)
    for pg in pgids:
        if not _pid_alive(pg, reads.proc_root):
            continue
        # A recycled PID that is NOT itself a group leader does not resurrect
        # ownership; a live true leader (pgrp == self) does. A live leader is
        # normally a member already read; one outside the scope is a recycled or
        # foreign pid, or a group the spawn did not lead, and is read here.
        if reads.stat(pg).pgrp == pg:
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
    reads: _ProcReads,
) -> tuple[bool, str, float | None]:
    """Evaluate the four-condition conjunction for one scope directory.

    *reads* is this evaluation's memo; a reclaim that follows reuses it.
    """
    pids = _read_cgroup_procs(scope_dir)
    if not pids:
        # No members: either a mid-spawn unit or an already-drained shell.
        # Signalling nothing is pointless and stopping a racing spawn is unsafe,
        # so leave empty scopes to systemd's own --collect GC.
        return False, "no members", None

    enter_us = active_enter_us(scope_dir.name)
    age = _scope_age_secs(enter_us, pids, now_monotonic, reads)

    # (i) nothing tracked / no live provider tree.
    for pid in pids:
        if pid in active_pids:
            return False, f"pid {pid} is a live provider", age
        if pid in tracked_pids:
            return False, f"pid {pid} is tracked", age

    # (ii-a) every member is ours -- by marker or by descent from a marked
    # member inside this scope; an unreadable environ fails closed.
    _owned, why = _scope_owned_pids(pids, reads)
    if why:
        return False, why, age

    # (ii-b) ownership alone is not scope-wide stop authority: intentional
    # detached work inherits the marker too. At least one member must still
    # positively identify the abandoned agent-runtime tree this reaper owns, OR
    # every member must be a marked credential helper -- a scope with nothing
    # else left in it.
    if not _scope_has_agent_runtime_anchor(pids, reads) and not (
        _scope_is_only_credential_helpers(pids, reads)
    ):
        return False, "no agent-runtime anchor in scope", age

    # (iii) leader dead OR scope predates this gateway's boot.
    leaders_dead = _leaders_dead(pids, reads)
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
    now_monotonic: float,
    reads: _ProcReads,
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
        ticks = reads.stat(pid).start_ticks
        age = None if ticks is None else platform_compat.process_age_secs(ticks)
        if age is None:
            continue
        youngest = age if youngest is None else min(youngest, age)
    return youngest


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
            timeout=15,
            **UTF8_TEXT,
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
    pinned: _Pinned,
) -> tuple[bool, str]:
    """Pin *pid*, re-verify membership and identity or ownership, then signal it.

    The pidfd is opened before membership and ownership are read. A process
    recycled after the open cannot retarget the fd. A process that inherits the
    PID before the open is not placed in this scope, so the post-pin membership
    read distinguishes it from the dead member we intended to signal.

    A member the reclaim attributed before its stop (*pinned*) is signalled on
    IDENTITY -- the same pid with the same ``start_ticks``, read after the pin --
    whatever its parent is now: once its marked parent dies to SIGTERM, an
    env-cleared child is reparented out of the scope's tree, and an ancestry
    check would spare exactly the process that ignored SIGTERM. Any other
    member is held to fresh tree ownership. ``reason`` is non-empty when the
    host cannot safely perform this signal or the member cannot be attributed.
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
        reads = _ProcReads(proc_root)
        start = pinned.get(pid)
        if start is None or reads.stat(pid).start_ticks != start:
            owned, _why = _scope_owned_pids(members, reads)
            if pid not in owned:
                return False, "member not attributable to this install"
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
    signal_owned: _SignalOwned,
    sleep: Callable[[float], None],
    on_refusal: Callable[[str], None] | None = None,
    reads: _ProcReads | None = None,
) -> bool:
    """Stop *unit_name*, then SIGTERM -> grace -> SIGKILL survivors.

    Every signal is preceded by a fresh ``cgroup.procs`` read, a pidfd pin, and
    identity (or, for a member not attributed before the stop, ownership)
    re-verification. ``pid <= 1`` and the gateway's own PID are never signalled.

    Ownership is asked of every current member BEFORE the unit is stopped, and a
    single claimed member aborts the whole reclaim. Stopping the unit IS the kill
    here -- systemd terminates the entire cgroup -- so a gate consulted only on
    the processes that survived the stop is consulted after the thing it exists to
    prevent. That pre-stop pass QUERIES ownership rather than calling the gate: the
    gate's allow path writes a kill attribution, so a survey that may end in an
    abort would otherwise record a kill of every unclaimed member that was never
    signalled. The gate is still called per pid in the signal loop below, where a
    signal is actually issued.

    Both tables are queried, not just leases. A LEASE says a session may end the
    runtime; a TENANCY says somebody who does not own it is mid-flight on it, and
    the second survives the last lease release -- which is exactly the moment a
    co-tenant becomes undefended. A survey reading leases alone therefore called a
    scope holding a live shared turn abandoned, and stopping the unit would have
    SIGKILLed that turn with no retry. ``claims_on_pid`` is the plain count, which
    records no debt: the debt belongs with the commit below, not with a survey that
    may abort.

    A survey is a statement about the past, so the reclaim runs behind COMMITTED
    barriers. Every member's tenancy epoch is read and committed immediately before
    ``stop_unit``; a member that gained a tenant between the survey and the commit
    fails its commit and abandons the whole reclaim, and every barrier taken is
    released in a ``finally`` -- a barrier left standing is a pid no tenant can
    claim for the life of the gateway. The barriers stay up across the signal loop
    too, which is what stops a tenant arriving inside the 3 s SIGTERM grace from
    being granted a defence against a signal already delivered.

    A process that joins the cgroup after the member list is read is outside this
    fence, as it was before: systemd owns that set, and the per-pid gate in the
    signal loop is what answers for a late arrival.

    *reads* is the deciding evaluation's memo, so attributing the members
    before the stop repeats no read; ``None`` reads fresh.

    ``on_refusal`` is called with the reason when the reclaim is abandoned, so the
    caller can record a refusal distinctly from a stop that was attempted and left
    members behind; the return is ``False`` either way, because the scope was not
    cleared.
    """
    my_pid = os.getpid()
    members = [pid for pid in _read_cgroup_procs(scope_dir) if pid > 1 and pid != my_pid]
    claimed = [
        pid
        for pid in members
        if outstanding_leases(pid) > 0 or RUNTIME_TENANCY.claims_on_pid(pid) > 0
    ]
    if claimed:
        # A scope with a live tenant is not abandoned, whatever the scope-level
        # verdict concluded before that tenant joined: reclaimability is decided
        # from pid sets snapshotted before the sweep, and a session joining a
        # runtime already inside this scope takes its lease -- or a shared turn its
        # tenancy -- afterwards. Leave the whole scope for the next pass rather
        # than stopping a unit that would take the tenant's process with it.
        logger.warning(
            "agent_scope_reap refused unit=%s: %d member(s) still leased or claimed; "
            "not stopping the unit",
            unit_name,
            len(claimed),
        )
        if on_refusal is not None:
            on_refusal("still leased")
        return False
    # The survey above is a statement about the past. These barriers make it
    # current: each epoch is read and committed here, and a member that gained a
    # tenant in between fails its own commit.
    epochs = {pid: tenancy_epoch(pid) for pid in members}
    committed: list[int] = []
    try:
        for pid in members:
            if commit_runtime_teardown(pid, epochs[pid]):
                committed.append(pid)
                continue
            logger.warning(
                "agent_scope_reap ABANDONED unit=%s: pid %d gained a tenant after the "
                "survey, so stopping the unit would land on a live turn; the next pass "
                "revisits it",
                unit_name,
                pid,
            )
            if on_refusal is not None:
                on_refusal("still leased")
            return False
        # Who is ours, fixed by identity BEFORE the stop: the stop and the
        # SIGTERM kill parents and reparent their children, so ancestry read
        # afterwards cannot say which survivors this reclaim is for.
        if reads is None:
            reads = _ProcReads(proc_root)
        owned, _why = _scope_owned_pids(members, reads)
        pinned = {pid: start for pid in owned if (start := reads.stat(pid).start_ticks) is not None}
        return _stop_and_signal_members(
            unit_name,
            scope_dir,
            proc_root,
            my_pid=my_pid,
            stop_unit=stop_unit,
            signal_owned=signal_owned,
            sleep=sleep,
            pinned=pinned,
        )
    finally:
        # Every exit above returns, so the barriers are dropped here or not at all:
        # a pid left committed is one no tenant can ever claim again.
        for pid in committed:
            release_runtime_teardown(pid)


def _stop_and_signal_members(
    unit_name: str,
    scope_dir: Path,
    proc_root: Path,
    *,
    my_pid: int,
    stop_unit: Callable[[str], bool],
    signal_owned: _SignalOwned,
    sleep: Callable[[float], None],
    pinned: _Pinned,
) -> bool:
    """Stop the unit, then signal whatever survived it; whether the scope is clear.

    Split out of :func:`_reclaim_scope` so the teardown barriers its caller holds
    cover this whole body through one ``try``/``finally`` instead of one per exit.
    Each wait ends as soon as every member signalled in that rung has left
    ``cgroup.procs``: the grace for SIGTERM, a short settle after SIGKILL.
    """
    stop_unit(unit_name)
    if not _read_cgroup_procs(scope_dir):
        return True

    refusal_reasons: set[str] = set()
    try:
        # Through ``platform_compat``, which defines both on every platform, rather than
        # off ``signal``, where SIGKILL does not exist on Windows. The values are
        # identical on POSIX; what this buys is a function whose ladder can be exercised
        # by a test on any host, so the control for the refusal tests above is not
        # silently skipped on one CI platform.
        for sig in (platform_compat.SIGTERM, platform_compat.SIGKILL):
            remaining = _read_cgroup_procs(scope_dir)
            if not remaining:
                return True
            signalled: set[int] = set()
            for pid in remaining:
                if pid <= 1 or pid == my_pid:
                    continue
                # The ownership question, asked per pid and per signal, because both
                # can change between them: this reclaim spans a 3 s grace, and a scope
                # judged abandoned can gain a tenant inside it -- a session joining a
                # runtime that was already in this scope takes its lease here, and the
                # scope-level verdict above was reached before that.
                #
                # A refusal is not a near miss the reaper handled. Scope
                # reclaimability already excludes every scope holding a tracked or
                # live-provider pid, so a leased pid reaching this loop means the two
                # records disagree, and the lease is the one that says a session is
                # still using the process.
                if not authorize_runtime_kill(
                    pid,
                    reason=f"abandoned agent scope {unit_name}",
                    caller="session_scope_reap._reclaim_scope",
                ):
                    refusal_reasons.add("still leased")
                    continue
                sent, reason = signal_owned(pid, sig, remaining, scope_dir, proc_root, pinned)
                if sent:
                    signalled.add(pid)
                if reason:
                    refusal_reasons.add(reason)
            if signalled:
                cap = _TERM_GRACE_SECS if sig == platform_compat.SIGTERM else _KILL_SETTLE_SECS
                _await_exit(scope_dir, signalled, cap_secs=cap, sleep=sleep)
        return not _read_cgroup_procs(scope_dir)
    finally:
        # In a finally so a signal seam that raises mid-ladder still reports the
        # refusals gathered before it.
        if refusal_reasons:
            logger.warning(
                "agent_scope_reap signalling skipped unit=%s reasons=%s",
                unit_name,
                ",".join(sorted(refusal_reasons)),
            )


def _await_exit(
    scope_dir: Path,
    signalled: set[int],
    *,
    cap_secs: float,
    sleep: Callable[[float], None],
) -> None:
    """Wait up to *cap_secs* for every pid in *signalled* to leave ``cgroup.procs``.

    Only the signalled pids are waited on: a member this rung did not signal (a
    refusal, a stranger) will not leave because of it, and waiting on the whole
    scope would spend the full cap on an answer already known.
    """
    for _ in range(round(cap_secs / _EXIT_POLL_SECS)):
        if signalled.isdisjoint(_read_cgroup_procs(scope_dir)):
            return
        sleep(_EXIT_POLL_SECS)


def _sel_scope_reap(unit_name: str, member_count: int, reason: str, outcome: str) -> None:
    """Emit one SEL audit event per reclaimable scope: completed, refused or failed."""
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


def _warn_scope_error(
    unit_name: str, phase: str, exc: BaseException, errored: set[tuple[str, str]]
) -> None:
    """Report a scope whose *phase* raised: at WARNING when new, changed or due again.

    Records the key in this sweep's *errored* set, which is what keeps it from
    being forgotten at the sweep's end. The traceback is always logged, at DEBUG
    when the WARNING is withheld. The same policy as the cleanup loop's
    reconcile-refusal warning, per scope and phase.
    """
    key = (unit_name, phase)
    errored.add(key)
    kind = type(exc).__name__
    now = time.monotonic()
    last = _SCOPE_ERRORS_WARNED.get(key)
    if last is not None and last[0] == kind and now - last[1] < _SCOPE_ERROR_REWARN_SECS:
        logger.debug("agent_scope_reap unit=%s %s failed again", unit_name, phase, exc_info=True)
        return
    logger.warning(
        "agent_scope_reap unit=%s: %s failed (%s: %s); the other scopes are still swept "
        "(repeats at most once per %.0fs while it keeps failing)",
        unit_name,
        phase,
        kind,
        exc,
        _SCOPE_ERROR_REWARN_SECS,
        exc_info=True,
    )
    _SCOPE_ERRORS_WARNED[key] = (kind, now)


def _reclaim_and_audit(
    scope_dir: Path,
    reason: str,
    *,
    proc_root: Path,
    stop_unit: Callable[[str], bool],
    signal_owned: _SignalOwned,
    sleep: Callable[[float], None],
    reads: _ProcReads,
    errored: set[tuple[str, str]],
) -> bool:
    """Reclaim one reclaimable scope and emit its SEL event; whether it cleared.

    A reclaim that raises is audited by what it left behind -- the stop may
    already have landed -- so an emptied scope is ``completed`` and one with
    members left is ``failed``; it is never an evaluation error and never silent.
    """
    unit_name = scope_dir.name
    members = len(_read_cgroup_procs(scope_dir))
    refused: list[str] = []
    raised = False
    try:
        cleared = _reclaim_scope(
            scope_dir,
            unit_name,
            proc_root=proc_root,
            stop_unit=stop_unit,
            signal_owned=signal_owned,
            sleep=sleep,
            on_refusal=refused.append,
            reads=reads,
        )
    except AssertionError:
        raise
    except Exception as exc:
        _warn_scope_error(unit_name, "reclaim", exc, errored)
        raised = True
        cleared = not _read_cgroup_procs(scope_dir)
    # A refusal and a failure are different events for an operator: one says a
    # tenant is still using the scope and the next pass should try again, the
    # other says the stop was attempted and did not finish.
    if cleared:
        outcome = "completed"
    elif refused:
        outcome = "refused"
    else:
        outcome = "failed"
    _sel_scope_reap(unit_name, members, reason, outcome)
    # A reclaim that raised was already named under the per-scope rate limit, so
    # repeating the unit at WARNING on every tick would defeat that limit.
    level = logging.DEBUG if raised else logging.WARNING
    if refused:
        logger.log(
            level,
            "agent_scope_reap left unit=%s for the next pass: %s",
            unit_name,
            ",".join(refused),
        )
    elif not cleared:
        logger.log(level, "agent_scope_reap could not fully clear unit=%s", unit_name)
    return cleared


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
    signal_owned: _SignalOwned = _pidfd_signal_owned,
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
    errored: set[tuple[str, str]] = set()
    for scope_dir in children:
        summary.scanned += 1
        unit_name = scope_dir.name
        # A scope whose evaluation raises costs that scope alone: the loop is
        # sorted, so an error escaping here would starve every scope after it on
        # every tick, and the session-cleanup hook does not retry within a tick.
        # An AssertionError is a broken invariant, not an unreadable scope.
        reads = _ProcReads(proc_root)
        try:
            reclaimable, reason, age = _scope_reclaimable(
                scope_dir,
                proc_root=proc_root,
                active_pids=active_pids,
                tracked_pids=tracked_pids,
                gateway_boot_us=gateway_boot_us,
                min_age_secs=min_age_secs,
                now_monotonic=now_monotonic,
                active_enter_us=active_enter_us,
                reads=reads,
            )
        except AssertionError:
            raise
        except Exception as exc:
            _warn_scope_error(unit_name, "evaluation", exc, errored)
            summary.skipped += 1
            skipped_reasons["error"] += 1
            # Its age is unknown, so it counts as old: the INFO summary then
            # counts it on every tick it keeps failing.
            has_old_skipped_scope = True
            continue
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
        cleared = _reclaim_and_audit(
            scope_dir,
            reason,
            proc_root=proc_root,
            stop_unit=stop_unit,
            signal_owned=signal_owned,
            sleep=sleep,
            reads=reads,
            errored=errored,
        )
        if cleared:
            summary.reclaimed += 1
            continue
        summary.skipped += 1
        if (unit_name, "reclaim") in errored:
            skipped_reasons["reclaim_error"] += 1
            has_old_skipped_scope = True
    # A key this sweep did not raise (checked clean, or its scope is gone) is
    # forgotten: its next error is new.
    for key in _SCOPE_ERRORS_WARNED.keys() - errored:
        del _SCOPE_ERRORS_WARNED[key]
    if has_old_skipped_scope:
        counts = " ".join(f"{name}={count}" for name, count in sorted(skipped_reasons.items()))
        logger.info("agent_scope_reap: skipped old scope(s): %s", counts)
    return summary


def instance_slice_pids() -> set[int]:
    """Every pid the kernel currently places inside THIS install's agent slice.

    Kernel truth for :mod:`kiro_crew.runtime_reconcile`: the population of
    processes that exist, read from ``cgroup.procs`` rather than from any record
    this gateway keeps, which is what lets the two be compared at all.

    Scoped to the per-instance child slice for the same reason
    :func:`_instance_scope_dir` is -- the bare shared parent can hold a
    co-resident gateway's scopes, and a pid of theirs would read here as one of
    ours with no record, which is precisely the shape that gets something killed.
    An unresolvable slice returns the EMPTY set, and the reconciler treats an
    empty kernel reading as nothing to compare rather than as an empty install.
    """
    slice_dir, _why = _instance_scope_dir()
    if slice_dir is None:
        return set()
    pids: set[int] = set()
    # The slice's own cgroup.procs carries anything attached directly to it;
    # each transient scope beneath it carries one spawn's tree.
    pids.update(_read_cgroup_procs(slice_dir))
    try:
        children = [p for p in slice_dir.iterdir() if p.suffix == ".scope" and p.is_dir()]
    except OSError:
        return pids
    for scope_dir in children:
        pids.update(_read_cgroup_procs(scope_dir))
    return pids


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
