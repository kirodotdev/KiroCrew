"""The bounded resolver: an agent-supplied path's canonical spellings, established under a deadline.

A path check runs synchronously on the event loop against an agent-supplied token, so a
stalled automount under that token would wedge the gateway. ``realpath`` therefore runs
in a child interpreter (``kiro_crew.subprocess_pool`` through
``executors.path_resolve_executor``) and the caller waits a bounded time. A miss raises
:class:`PathResolutionStalled`, which every gate refuses rather than falling back to the
lexical spelling, and opens a cooldown that refuses paths under the stalled prefix
without touching the filesystem until the mount answers again. The stall accounting
(the load arm, the blocked-syscall probe of the child) and the Windows alias folding live
here too. A change to how a path's canonical form is established goes here.

``kiro_crew.security.paths`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`); the target builder and the path
gates read it, and this module imports no other part of the path floor.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from kiro_crew.executors import path_resolve_executor
from kiro_crew.subprocess_pool import (
    OP_REALPATH_MANY,
    OP_REALPATH_SPELLINGS,
    SubprocessPoolTimeout,
    SubprocessPoolUnavailable,
    pack_strings,
    unpack_strings,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("kiro_crew.security.paths")


# ── Bounded symlink resolution for the sensitive-path gates ──
#
# ``os.path.realpath`` / ``Path.resolve`` ``lstat`` every component of the path
# they are handed.  The path gates hand them AGENT-SUPPLIED tokens -- including
# tokens that name nothing on this host at all, like the remote side of
# ``ssh host 'cd /home/user/ws && ...'`` -- and a component that lands on a
# stalled automount (macOS ``/home`` is an autofs map resolved through
# opendirectoryd; a dead NFS/SSHFS mount; a disconnected mapped drive) blocks in
# the kernel for as long as the mount does.  No exception is raised, so the
# ``except OSError`` around the call never fired: the call simply never
# returned.  Because :func:`is_sensitive_path` runs synchronously inside
# ``on_tool_call`` on the event loop, that was a loop
# wedge and the stall watchdog's dump-then-exit -- ten identical crash dumps on
# a corp macOS during a VPN transition, the loop parked in ``_joinrealpath`` for
# the full watchdog budget.  Widening the budget only moved the crash.
#
# So resolution runs on its own tiny pool and the caller waits a BOUNDED time.
# A timeout is NOT treated like the ``OSError`` fallback (lexical forms only):
# that would make the degraded state a lever -- stall one token under a wedged
# mount and, for the cooldown, a workspace symlink into a credential store
# would pass on its lexical spelling.  A path whose canonical form cannot be
# established is instead REFUSED (:class:`PathResolutionStalled`, fail-closed
# in every gate), the same posture the rest of the path floor takes when a proof
# is missing.  The cost is a false refusal of paths under a wedged mount for
# the cooldown window -- the ``ssh`` command above is refused for 30s during a
# VPN transition instead of killing the gateway -- and the refusal names why.
#
# A timeout also opens a short cooldown during which paths under the SAME
# prefix are refused without touching the filesystem: one tool call can
# carry many path tokens against the same wedged mount, each of which would
# otherwise pay the full timeout -- ten tokens at 2s would put the loop back
# past the watchdog.  The cooldown is scoped to the stalled prefix
# (:func:`_stall_prefix`), never process-wide, so a stall on ``/home/<user>``
# leaves ``/tmp`` and the workspace fully resolved.  It doubles on every
# repeat stall under the same prefix (up to the cap below).
#
# The ``realpath`` itself runs in a CHILD INTERPRETER (``kiro_crew.subprocess_pool``,
# ``executors.path_resolve_executor``), reached from the calling thread with
# ``call_op``: ``realpath`` is pure Python and reacquires the GIL twice per path
# component, so on a thread it paid one switch interval per component beside every
# other busy thread in the gateway and expired a budget the disk never touched.
# A child pays one handoff for the whole answer, and a child that misses
# the deadline is KILLED and respawned, so a stall costs one refused resolution
# rather than a pinned worker.
_PATH_RESOLVE_TIMEOUT_SECS = 2.0


# The ANCHOR REBUILD's own budget. One pool job there performs a few hundred
# `realpath` calls -- one per declared entry under `$HOME`, plus the override
# re-anchors -- where a candidate resolution performs one or two, so a single
# budget sized for the candidate leaves the rebuild running far closer to its
# ceiling -- measured with every override set: a cold rebuild resolves about 350
# paths in one request, ~21 ms median on an idle Linux host. Sizing this to the
# work actually done is what stops an
# ordinarily-slow rebuild from being mistaken for a wedged mount on a loaded host
# (4 xdist workers plus real-time antivirus on a 4-vCPU Windows runner is where it
# was first observed); raising `_PATH_RESOLVE_TIMEOUT_SECS` globally instead would
# relax the latency guarantee on the candidate path, which does not need it.
_PATH_RESOLVE_REBUILD_TIMEOUT_SECS = 8.0


# Fail-closed tightening: successful waits and stalls under DISTINCT prefixes all
# block the calling thread. Allow 12s total (one rebuild plus its maximum grace),
# leaving 13s of the 25s watchdog for heartbeat age and other tool-call work.
# Retain that spend until 25s after the LAST wait, not a fixed window boundary:
# otherwise two adjacent windows can spend twice the cap inside one watchdog gap.
# Background callers have their own allowance, never the event-loop thread's.
_PATH_RESOLVE_WAIT_CAP_SECS = 12.0


_PATH_RESOLVE_WAIT_WINDOW_SECS = 25.0


# A wait below this floor is resolver-pool round-trip overhead, not filesystem
# latency, so it is excluded from the cumulative spend above -- otherwise ordinary
# bulk work (a project-tree listing, a knowledge-indexing pass, a directory-wide
# path_contains_sensitive scan) accumulates thousands of sub-millisecond on-time
# waits and exhausts the allowance with zero mount evidence, trading the rare
# crash this bound removes for a reachable silent host-wide refusal instead.
# Measured on a 32-core host: 3000 calls against a healthy path cost 1.151s of
# accounted wait, 0.384ms/call -- 100ms is ~260x that overhead, so realistic pool
# jitter stays free, and 20x under the 2.0s default candidate budget, so it stays
# far below both a single missed-budget timeout and the slow-but-completing case
# this bound must still catch: a run of waits that each finish just under budget
# (13 at ~2s apiece is ~25s of loop block) all clear the floor and still count.
_PATH_RESOLVE_WAIT_FLOOR_SECS = 0.1


# How much longer a resolution that missed its budget is given to finish before the
# prefix is charged with a stall. A miss is not itself proof of a wedged mount -- a
# merely slow one completes -- and on any platform where the syscall probe below
# cannot discriminate, this is what separates the two, empirically rather than by
# syscall table. Paid at most once per prefix per cooldown, because the charge that
# follows a grace miss refuses later paths under the prefix without probing.
#
# The grace is FOLDED INTO the child's one deadline (budget plus grace) rather than
# waited as a second phase: a child that misses its deadline is destroyed, so there
# is nothing left to wait on, and a second request would be a re-probe, not a grace.
#
# Expressed as a FRACTION of the caller's budget, not a constant: the grace is "half
# again as long as this caller already agreed to wait", so a caller that deliberately
# chooses a tight budget keeps a tight worst case (the whole point of taking a budget
# per call) instead of inheriting a fixed multi-second tail. Capped so the generous
# rebuild budget cannot compound into the loop-stall watchdog this bound protects:
# 8s + 4s stays well inside 25s.
_PATH_RESOLVE_GRACE_FACTOR = 1.5


_PATH_RESOLVE_GRACE_MAX_SECS = 4.0


_PATH_RESOLVE_COOLDOWN_SECS = 30.0


_PATH_RESOLVE_COOLDOWN_MAX_SECS = 1800.0


# The load arm declines to charge the prefix, so it carries the event-loop bound the cooldown
# would otherwise supply: this many uncharged probes per prefix per window.
_PATH_RESOLVE_LOAD_WINDOW_SECS = 10.0


_PATH_RESOLVE_LOAD_MAX_PROBES = 3


# ``/proc/<pid>/syscall`` reports the syscall NUMBER, which is per-architecture. An unmapped
# architecture yields an empty set, which fails toward charging the prefix.
_FS_BLOCKING_SYSCALLS_BY_ARCH: dict[str, frozenset[int]] = {
    "x86_64": frozenset(
        {4, 5, 6, 89, 262, 267, 332}
    ),  # stat fstat lstat readlink newfstatat readlinkat statx
    "aarch64": frozenset({78, 79, 80, 291}),  # readlinkat newfstatat fstat statx
}


_FS_BLOCKING_SYSCALLS: frozenset[int] = _FS_BLOCKING_SYSCALLS_BY_ARCH.get(
    platform.machine(), frozenset()
)


# stall prefix -> (monotonic deadline until which paths under it are refused,
# consecutive stalls recorded under it -- drives the exponential backoff)
_path_resolve_degraded: dict[str, tuple[float, int]] = {}


_path_resolve_load_probes: dict[str, tuple[float, int]] = {}


# calling thread id -> (quiet-window end, accumulated seconds in result waits)
_path_resolve_thread_waits: dict[int, tuple[float, float]] = {}


_path_resolve_lock = threading.Lock()


_path_resolve_clock: Callable[[], float] = time.monotonic  # tests advance this


# The absolute ``time.monotonic`` deadline every child request made under the
# current :func:`_run_resolution_bounded` call shares.  Thread-local because the
# whole resolution -- the worker AND its child round trips -- runs on the calling
# thread.  It is ALSO the switch between the two places a ``realpath`` may run: a
# resolution with no deadline armed is one made OUTSIDE any bounded call -- the
# inline rebuild under :func:`is_sensitive_resolved_path`, a direct
# :func:`_resolved_env_root` -- and those run in this interpreter, as they always
# have, never in the pool.  The pool is sized and queued for the event loop's bounded
# calls; a caller with no deadline would hold a child up to the pool's ceiling and
# queue ahead of the loop, which is the starvation the inline entry point exists to
# prevent.
_child_budget = threading.local()


_child_fallback_warned = False


# Consecutive child TRANSPORT faults (the child started, then died or answered out of
# frame).  One fault is a debug-level event: a child is killed and respawned as a
# matter of course.  A RUN of them is a host where children spawn but keep dying (a
# half-upgraded interpreter, antivirus or a cgroup OOM killer reaping the child, a pool
# whose every slot is held by killed children that will not exit), and on that host
# every path gate would be refusing fail-closed -- an outage as total as a stalled
# mount.  So at this threshold the run is warned about once and resolution degrades to
# the bounded in-process fallback the cannot-spawn arm uses (still on the deadline,
# still fail-closed on a miss), and the counter resets on the next child answer.
_CHILD_FAULT_WARN_STREAK = 3


_child_fault_streak = 0


_child_fault_warned = False


def _outside_bounded_call() -> bool:
    """True when no :func:`_run_resolution_bounded` deadline is armed on this thread."""
    return getattr(_child_budget, "deadline", None) is None


def _pool_down_to_last_child() -> bool:
    """True when the resolver pool has at most one child it can still lease.

    Read through :meth:`SubprocessPoolExecutor.serviceable_children`, which counts
    live children plus slots the reaper can still fill; an executor without that
    method (a test stub) reports plenty, so the re-probe guard in
    :func:`_run_resolution_bounded` stays out of the way.
    """
    count = getattr(path_resolve_executor(), "serviceable_children", None)
    if count is None:
        return False
    return count() <= 1


def _child_request(op: int, payload: bytes) -> bytes | None:
    """One round trip to the resolver child, on THIS thread, inside the shared deadline.

    Only ever called under an armed deadline (callers check
    :func:`_outside_bounded_call` first and resolve in-process otherwise), so the
    pool never holds a child for a request with no bound.  ``None`` means the child
    could not be STARTED at all (``Popen`` raised), or that children have faulted
    ``_CHILD_FAULT_WARN_STREAK`` times in a row: the caller resolves in-process
    instead, today's behaviour, after one warning.  Below that streak every
    other fault propagates: :class:`SubprocessPoolUnavailable` (the child died,
    faulted or answered out of frame) and :class:`SubprocessPoolTimeout` (it took
    the request and missed the deadline) are the caller's to classify, and neither
    may ever read as an empty answer.  A run of ``_CHILD_FAULT_WARN_STREAK``
    consecutive transport faults is logged once at warning level and, from then until
    a child answers again, resolved in-process, because a gate that refuses everything
    on a host whose children keep dying is an outage, not a defence.
    """
    global _child_fallback_warned, _child_fault_streak, _child_fault_warned
    deadline = getattr(_child_budget, "deadline", None)
    if deadline is None:  # pragma: no cover - callers route inline first
        raise RuntimeError("resolver child requested outside a bounded call")
    timeout = max(0.0, deadline - time.monotonic())
    try:
        answer = path_resolve_executor().call_op(op, payload, timeout)
    except SubprocessPoolUnavailable as exc:
        with _path_resolve_lock:
            _child_fault_streak += 1
            streak = _child_fault_streak
            degraded = streak >= _CHILD_FAULT_WARN_STREAK
            escalate = degraded and not _child_fault_warned
            if escalate:
                _child_fault_warned = True
        if escalate:
            logger.warning(
                "sensitive-path resolver child has faulted %d times in a row (%s); "
                "resolving in-process, where interpreter load counts against the budget, "
                "until a child answers again",
                streak,
                exc,
            )
        if degraded:
            # A sustained run of faults is a host where children start and keep dying
            # (antivirus, an OOM killer, a pool with every slot held by killed children
            # that will not exit).  Refusing every gate for as long as that lasts would
            # take file reads, edits and shell calls down with the child on a host that
            # worked with the thread pool, so the run degrades to the same bounded
            # in-process fallback the cannot-spawn arm uses: still on the deadline,
            # still fail-closed when it misses, and never an empty answer.  A single
            # fault stays a refusal -- one fault is a killed-and-respawned child, and an
            # answer computed in this interpreter for it would be the load-shaped
            # refusal the pool exists to remove, taken for no reason.
            return None
        raise
    except TimeoutError:
        raise  # an ``OSError`` subclass, but the caller's to classify, not a spawn failure
    except OSError as exc:
        if not _child_fallback_warned:
            _child_fallback_warned = True
            logger.warning(
                "sensitive-path resolver child could not be started (%s); resolving "
                "in-process, where interpreter load counts against the budget",
                type(exc).__name__,
            )
        return None
    if _child_fault_streak:
        with _path_resolve_lock:
            _child_fault_streak = 0
            _child_fault_warned = False
    return answer


def _absolutized(path: str) -> str:
    """*path* anchored to THIS process's working directory, with no other rewriting.

    Deliberately not ``os.path.abspath``: that also ``normpath``s, which collapses a
    ``..`` LEXICALLY before the child has resolved the symlink in front of it, so
    ``ws/link/../credentials`` (``link`` pointing into a credential home) would be
    sent as ``ws/credentials`` and the sensitive form ``realpath`` finds would be
    lost.  ``realpath`` resolves each component in order, so joining against the
    CWD and leaving the rest alone answers exactly what the in-process resolver
    answers for the same relative input.
    """
    if os.path.isabs(path):
        return path
    return os.path.join(os.getcwd(), path)


def _inline_bounded(path: str, fn: Callable[[], _ResolvedT]) -> _ResolvedT:
    """Run *fn* in-process when no child can be started, still inside the deadline.

    The fallback keeps the gate alive on a host where ``Popen`` fails, but an
    unbounded ``realpath`` on the calling thread is the original stall; so when a
    bounded call is in progress the work goes to the pool's internal thread pool
    and this thread waits on the future with the shared deadline.  A miss raises
    :class:`PathResolutionStalled` uncharged: a thread wedged in the kernel says
    nothing this code can classify, and the thread itself is not reclaimable.
    Outside a bounded call (no deadline) *fn* simply runs here, as before.
    """
    deadline = getattr(_child_budget, "deadline", None)
    if deadline is None:  # pragma: no cover - the callers resolve inline before asking a child
        return fn()
    future = path_resolve_executor().submit(fn)
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except FuturesTimeoutError:
        logger.debug("in-process fallback resolution missed the budget; prefix not charged")
        raise PathResolutionStalled(path, _stall_prefix(path)) from None


def _resolved_spellings_inline(expanded: str) -> set[str]:
    """Symlink-resolved spellings of *expanded*, computed in THIS interpreter.

    What the child mirrors (``_child_realpath.realpath_spellings``), and the
    fallback when no child can be started.
    """
    out: set[str] = set()
    try:
        out.add(os.path.realpath(expanded))
    except (OSError, ValueError):
        pass
    try:
        # Guarded false-positive: this resolve() is INSIDE is_sensitive_path — the
        # sanitizer itself — building candidate forms to CHECK a path against the
        # sensitive denylist. It performs no read/write. CodeQL surfaces
        # py/path-injection here only because a new caller (artifact relocate)
        # reaches it with user input; the function's whole purpose is to vet that
        # input, so suppress the alert on the resolution step.
        out.add(str(Path(expanded).resolve()))  # lgtm[py/path-injection]
    except (OSError, ValueError, RuntimeError):
        pass
    return out


def _resolved_spellings(expanded: str) -> set[str]:
    """Symlink-resolved spellings of *expanded*, resolved in the child.

    Absolutized here, against THIS process's working directory, before it is sent:
    ``realpath`` anchors a relative path to the CWD, and the child's CWD is not the
    caller's guarantee.  A transport fault raises :class:`PathResolutionStalled`
    rather than returning an empty set, because an empty set reads as "resolved,
    no other spelling" and would leave a workspace symlink into a credential store
    matched on its lexical spelling alone.  No cooldown is charged for it: the disk
    did not stall.  Outside a bounded call the resolution runs in this interpreter
    (see the note on ``_child_budget``).
    """
    if _outside_bounded_call():
        return _resolved_spellings_inline(expanded)
    try:
        payload = _child_request(OP_REALPATH_SPELLINGS, os.fsencode(_absolutized(expanded)))
    except SubprocessPoolUnavailable as exc:
        logger.debug("resolver child faulted on a candidate: %s", exc)
        raise PathResolutionStalled(expanded, _stall_prefix(expanded)) from None
    if payload is None:
        return _inline_bounded(expanded, lambda: _resolved_spellings_inline(expanded))
    return {os.fsdecode(value) for value in unpack_strings(payload)}


class PathResolutionStalled(RuntimeError):
    """Symlink resolution of an agent-supplied path did not complete in time.

    Raised by :func:`_resolved_forms_bounded` when the bounded ``realpath``
    times out, and for the cooldown that follows under the same path prefix.
    The sensitive-path gates treat it as FAIL-CLOSED: a path whose canonical
    form cannot be established is refused, never matched on its lexical
    spelling alone -- a lexical-only match would let a workspace symlink into a
    credential store pass while the mount it does not even live on is wedged.
    """

    def __init__(self, path: str, prefix: str) -> None:
        super().__init__(
            f"symlink resolution of {path!r} is unavailable (stalled mount under {prefix!r})"
        )
        self.path = path
        self.prefix = prefix


# Tracebacks and logs name the class at the import path callers use.
PathResolutionStalled.__module__ = "kiro_crew.security.paths"


def _stall_prefix(expanded: str) -> str:
    """The path prefix a stall is charged to: the first two components, extended
    by one when the second names a container of home directories.

    A wedged mount stalls everything beneath its mount point, and mount points
    sit at depth one or two (``/home/<user>`` autofs, ``/Volumes/<share>``,
    ``/net/<host>``, ``C:\\Users\\<user>``), so two components is the narrowest key
    that still covers the whole stalled subtree.  Scoping the cooldown here is
    what keeps a stall on the REMOTE half of an ``ssh`` command from switching
    resolution off for the local workspace where a bypass symlink would live.

    **The DRIVE is split off first, and on Windows that is what makes the key two
    components rather than one.**  A POSIX absolute path starts with an empty
    component (``"/a/b"`` -> ``["", "a", "b"]``), which is why three are kept; a
    Windows path does not (``"C:\\Users\\bob"`` -> ``["C:", "Users", "bob"]``), so
    counting components without splitting the drive kept ``C:`` as one of the two
    and collapsed every user path to ``C:\\Users``.  That single key contains
    ``$HOME``, ``%TEMP%``, the workspace and the checkout, so one stall anywhere in
    the profile refused path resolution for essentially the whole host -- the exact
    opposite of the per-mount isolation this function exists to provide.

    **The key never ENDS on a container of homes**, a component spelled ``home``.
    A host whose homes sit one level deeper than ``/home`` reproduces the
    ``C:\\Users`` collapse on POSIX: two components of ``/local/home/<user>/ws/f``
    is ``/local/home``, which holds every user, every workspace and checkout, the
    data home and the credential stores, so one stall anywhere under it refused
    path resolution for the whole host (kirodotdev/KiroCrew#12386).  When the
    second component is such a container the key takes one more, so the boundary
    is the home directory itself -- ``/local/home/<user>`` -- exactly the key a
    ``/home/<user>`` layout already gets.  Every other key is unchanged, and the
    container alone (``/local/home``) stays its own key.

    The rule is lexical, like the rest of this function: it must not touch the
    filesystem, because it is consulted BEFORE deciding whether to probe at all
    and it names the prefix charged when the ``$HOME`` anchor itself stalls.  A
    mount-derived key (``os.path.ismount`` walked upward) would both ``lstat``
    the very mount that may be wedged and, on a single-filesystem host, reach
    ``/`` -- a process-wide key, worse than the collapse.  The price of a lexical
    key stands: two spellings of one tree (``/home/<user>`` and its
    ``/local/home/<user>`` target) remain two keys, each paying its own timeout.
    And when the container is itself ONE wedged mount (an NFS ``/export/home``),
    each user's tree pays its own timeout and pins its own worker -- the cost a
    ``/home/<user>`` autofs layout already carries per user, bounded the same
    way, by the pool size -- in exchange for the isolation this key exists for.

    A UNC share root is returned whole: ``\\\\server\\share`` IS the mount point,
    and ``splitdrive`` already reports it as the drive, so no component of the
    remainder belongs in the key.
    """
    normalized = os.path.normpath(expanded)
    drive, rest = os.path.splitdrive(normalized)
    if drive[:1] in ("\\", "/") and drive[1:2] in ("\\", "/"):
        return drive
    parts = rest.split(os.sep)
    keep = 3 if parts and parts[0] == "" else 2  # leading "" for an absolute path
    # A component spelled ``home`` is a CONTAINER of home directories, never a home:
    # ``/home/<user>`` is the common layout, and the layouts that put the homes one
    # level deeper all keep the word -- ``/local/home/<user>`` on a cloud desktop
    # (where ``/home/<user>`` is a symlink into it), ``/usr/home`` on FreeBSD,
    # ``/var/home`` on Fedora Silverblue, ``/export/home`` on Solaris and NFS
    # servers.  ``Users`` needs no entry: it only ever sits at depth one
    # (``/Users/<user>``, ``C:\Users\<user>``), where the key already ends on the
    # user.  Matched exactly: the container is spelled by the OS or the
    # administrator, not by the agent.
    if len(parts) > keep and parts[keep - 1] == "home":
        keep += 1  # the container holds every user: key on the home beneath it
    return (drive + os.sep.join(parts[:keep])) or normalized


def _child_blocked_in_filesystem(sampled: bytes | None) -> bool:
    """True when the child's syscall at the deadline was one a path resolution blocks in.

    *sampled* is what the executor read from ``/proc/<pid>/syscall`` the instant the
    budget ran out, before it killed the child (:class:`SubprocessPoolTimeout`).  It
    separates the two causes of a started-but-unfinished resolution, which the budget
    alone cannot: a child stuck on a wedged mount versus one that was starved of the
    CPU.  Both consume almost no CPU, so a CPU-time comparison cannot tell them apart,
    and neither can the ``/proc`` state field: measured on a 48-core host, a thread
    doing ordinary ``lstat`` work and a thread doing nothing but burn CPU BOTH
    alternate between ``R`` and ``S`` from one sample to the next.

    What does separate them is WHICH syscall the process is in.  One genuinely blocked
    in a kernel wait reports that syscall on every sample; one merely contending
    reports ``running`` or a ``futex``.  And these syscalls complete in microseconds
    on a healthy filesystem, so sampling one at all is itself evidence that it is not
    completing.  The child has no GIL to wait on -- it is alone in its interpreter --
    so the starved case is now the rarer one, but it is still the one that must not
    open a prefix-wide cooldown.

    UNVERIFIED, and deliberately not claimed: that a process stuck in one of these
    syscalls on a real wedged NFS, FUSE or CIFS mount reports it stably.  No wedged
    mount could be produced where this was measured (an unprivileged ``fusermount3``
    and ``unshare(CLONE_NEWUSER)`` both return EPERM).  What IS confirmed is the
    sampling this rests on, including for the state class a wedged FUSE or CIFS mount
    actually waits in: an ``openat`` on a FIFO with no writer blocks INTERRUPTIBLY on a
    real filesystem path and reported state ``S`` with syscall 257 on 15 of 15
    samples.  See ``test_an_in_s_filesystem_wait_is_sampled_stably_and_reads_as_blocked``.

    The table covers ``readlink`` as well as the stat family because CPython's
    ``posixpath.realpath`` calls ``os.lstat`` AND ``os.readlink`` per component: a
    mount that answers the lstat from cache and hangs the readlink would otherwise
    read as not blocked, take the load arm, and pay an uncharged full-budget probe per
    token rather than opening one cooldown.

    Fails toward the EXISTING behaviour -- no sample (non-Linux, or the child was
    already gone), or an architecture whose syscall numbers are not mapped -- by
    returning True, so the caller still charges the prefix rather than silently
    withholding an escalation the gate would otherwise make.
    """
    if sampled is None or not _FS_BLOCKING_SYSCALLS:
        return True
    if sampled == b"running":
        blocked = False
    else:
        try:
            blocked = int(sampled) in _FS_BLOCKING_SYSCALLS
        except ValueError:
            blocked = True
    logger.debug(
        "resolver child syscall=%s blocked_in_filesystem=%s",
        sampled.decode("ascii", "replace"),
        blocked,
    )
    return blocked


def _load_arm_budget_spent(prefix: str) -> bool:
    """Record a load-arm probe under *prefix*; True once the window's allowance is gone.

    The per-prefix cooldown has a second job besides remembering a dead mount: it BOUNDS how
    much event-loop time one call can spend probing. A single tool call can carry many path
    tokens, and ten tokens each paying the full budget puts the event loop back past the
    watchdog this whole bound exists to protect. Declining to charge the prefix removes that
    bound, so the load arm has to carry it: the first ``_PATH_RESOLVE_LOAD_MAX_PROBES`` probes
    in a window are free, and the next one charges the prefix normally.

    The count is cleared ONLY by the window expiring, never by a successful resolution. It
    measures event-loop time already spent, which a later success cannot refund: clearing it on
    success let an alternating success / CPU-starved-timeout run under one prefix pay the full
    budget on every timeout while never crossing the allowance.
    """
    now = _path_resolve_clock()
    with _path_resolve_lock:
        if len(_path_resolve_load_probes) > 64:
            _path_resolve_load_probes.clear()
        window_end, probes = _path_resolve_load_probes.get(prefix, (0.0, 0))
        if now >= window_end:
            window_end, probes = now + _PATH_RESOLVE_LOAD_WINDOW_SECS, 0
        probes += 1
        _path_resolve_load_probes[prefix] = (window_end, probes)
        return probes > _PATH_RESOLVE_LOAD_MAX_PROBES


def _extend_cooldown(prefix: str) -> None:
    """Re-arm *prefix*'s existing cooldown WITHOUT recording a new stall.

    For a refusal that observed nothing -- the re-probe guard in
    :func:`_run_resolution_bounded` declines to probe a stalled prefix while the pool
    is down to its last child -- so the stall count, the backoff it drives and the
    warning :func:`_mark_stalled` emits all stay as the last real observation left
    them.  The window re-armed is the one that count already earned.
    """
    now = _path_resolve_clock()
    with _path_resolve_lock:
        _, stalls = _path_resolve_degraded.get(prefix, (0.0, 1))
        stalls = max(stalls, 1)
        cooldown = min(
            _PATH_RESOLVE_COOLDOWN_SECS * (2 ** (stalls - 1)),
            _PATH_RESOLVE_COOLDOWN_MAX_SECS,
        )
        _path_resolve_degraded[prefix] = (now + cooldown, stalls)


def _mark_stalled(prefix: str, budget: float) -> None:
    """Record an OBSERVED stall under *prefix*: back off exponentially on repeats.

    Only a resolution that actually RAN in a child and timed out blocked in the
    filesystem is recorded.  A refusal issued because no child took the request,
    or because the child was on the CPU rather than in a syscall when the budget
    ran out, says nothing about the filesystem and must not charge the refused
    prefix -- often the local workspace -- a backoff it never earned, or a
    transient dual-mount outage would keep refusing healthy paths for the accrued
    window after the mounts recover.  The log line deliberately omits
    the path: the token is agent-supplied and is what the gates exist to keep
    out of clear-text logs.
    """
    now = _path_resolve_clock()
    with _path_resolve_lock:
        if len(_path_resolve_degraded) > 64:
            _path_resolve_degraded.clear()
        _, stalls = _path_resolve_degraded.get(prefix, (0.0, 0))
        stalls += 1
        cooldown = min(
            _PATH_RESOLVE_COOLDOWN_SECS * (2 ** (stalls - 1)),
            _PATH_RESOLVE_COOLDOWN_MAX_SECS,
        )
        _path_resolve_degraded[prefix] = (now + cooldown, stalls)
    logger.warning(
        "sensitive-path symlink resolution did not complete in %.1fs (stalled "
        "mount?); refusing paths under the stalled prefix for the next %.0fs "
        "(stall #%d)",
        budget,
        cooldown,
        stalls,
    )


_UNC_PREFIX_RE = re.compile(r"^[\\/]{2}[^\\/]")


_ON_WINDOWS = os.name == "nt"


#: Local-drive namespace prefixes and default-stream suffixes: Windows opens each
#: spelling as the plain drive path. UNC, volume GUID and named streams stay as written.
_WIN_LOCAL_NS_RE = re.compile(r"^(?:[\\/]{2}[?.]|\\\?\?)[\\/](?=[A-Za-z]:(?:[\\/]|$))")


_WIN_DEFAULT_STREAM_RE = re.compile(
    r"(?:::\$DATA|::\$INDEX_ALLOCATION|:\$I30:\$INDEX_ALLOCATION)$", re.IGNORECASE
)


def _fold_windows_alias(path: str) -> str:
    """Lexically fold a Windows alias of a local path to its plain spelling; identity off Windows."""
    if not _ON_WINDOWS:
        return path
    folded = _WIN_LOCAL_NS_RE.sub("", path, count=1)
    folded += "\\" if folded != path and len(folded) == 2 else ""  # bare volume -> drive root
    return _WIN_DEFAULT_STREAM_RE.sub("", folded)


def _is_unc_path(expanded: str) -> bool:
    """``\\\\server\\share\\...`` in either separator spelling.

    On Windows ``os.path.realpath`` on a UNC path opens it
    (``GetFinalPathNameByHandle``), which is a network round-trip to the named
    host -- a dead or slow host stalls the caller for the SMB timeout, and a
    UNC token in an agent's command is the ordinary way to name a share, not a
    symlink-bypass vector: the fence's targets are local drive spellings that a
    UNC realpath never produces (``\\\\?\\UNC\\...``).  So a UNC token is matched
    lexically and never probed, the same stance the mapped-drive fence below
    takes for a foreign drive letter.
    """
    return bool(_UNC_PREFIX_RE.match(expanded))


_ResolvedT = TypeVar("_ResolvedT")


def _resolve_on_calling_thread(
    worker: Callable[[str], _ResolvedT], expanded: str, timeout: float
) -> _ResolvedT:
    """Run *worker(expanded)* HERE, with *timeout* as the deadline its child requests share.

    On the calling thread by design, not for want of a pool: the answer comes back
    from the child over a pipe, and the thread that wants it must be the one blocked
    in that read.  Handing the round trip to a worker thread puts two more GIL
    handoffs between question and answer (worker pickup, caller wake-up), each up to a
    switch interval behind every other runnable thread, which is what made the
    thread-pool version of this resolver a no-op under load -- measured at 48
    contenders, 81 ms this way against 2681 ms pooled, the latter over the budget.
    The worker's own Python (env reads, casefolding) is microseconds; only the
    ``realpath`` was ever the cost, and that now runs one process over.
    """
    _child_budget.deadline = time.monotonic() + timeout
    try:
        return worker(expanded)
    finally:
        _child_budget.deadline = None


def _run_resolution_bounded(
    expanded: str, worker: Callable[[str], _ResolvedT], *, budget: float | None = None
) -> _ResolvedT | None:
    """Run *worker(expanded)*, its ``realpath`` work in the resolver child, within the budget.

    The shared core under :func:`_resolved_forms_bounded` (the agent-supplied
    CANDIDATE), :func:`_resolved_root_key` and :func:`_rebuild_targets_bounded`
    (the TARGET anchors: ``$HOME``, the override roots and the keystone leaves).
    Both kinds of resolution stat the same filesystem from the event loop, so they
    share one child pool, one budget and one per-prefix cooldown: a stall observed
    while anchoring ``$HOME`` refuses candidate resolution under that prefix for
    the same window, and a stall on a candidate keeps the anchors from re-probing
    the same wedged mount every time the target cache expires.

    Returns the worker's value, or ``None`` when resolution FAILED -- the pool
    refused work at interpreter exit, or faulted in a way the worker did not
    classify.  A resolution that does not COMPLETE is different and raises
    :class:`PathResolutionStalled` instead, both on the timing-out call and,
    without touching the filesystem, for every later call under the same
    :func:`_stall_prefix` until the cooldown lapses.  Repeated stalls under one
    prefix double the cooldown up to ``_PATH_RESOLVE_COOLDOWN_MAX_SECS``.  Never
    blocks the caller for longer than *budget* plus its grace -- a capped fraction
    of *budget* folded into the child's single deadline, since a child that misses
    it is destroyed rather than waited on.  The wait also consumes the calling
    thread's cumulative allowance -- excluding a wait below the floor, round-trip
    overhead rather than filesystem latency; exhaustion refuses without a request
    or a charge until the quiet window expires.

    WHAT A TIMEOUT MEANS, and how the prefix is charged.  The thread pool this
    replaced classified a miss by thread-shaped signals -- was the future ever
    claimed, was the worker started, what syscall was that thread in -- and none of
    those has a referent once the work is a process reached from this thread.  The
    child-shaped signals are the exception the pool raises:

    * a plain ``TimeoutError`` means NO child ever took the request (every child was
      leased to another caller for the whole budget): evidence about load, not the
      mount, so this call alone is refused and nothing is charged;
    * :class:`SubprocessPoolTimeout` means a child took it and missed the deadline,
      and carries what that child was doing when the budget ran out.  Blocked in a
      stat/readlink syscall, or unsampleable (non-Linux, unmapped architecture), and
      the mount is charged; on-CPU or in a ``futex``, and it is the load arm:
      refused, not charged, bounded by :func:`_load_arm_budget_spent`.  Charging
      ALSO requires that the budget and grace were granted in full: a wait clamped by
      the allowance proves nothing about the mount.

    The UNC shortcut is NOT here: skipping a ``\\\\server\\share`` token is a
    stance about agent-supplied CANDIDATES (:func:`_resolved_forms_bounded`),
    whose fence targets a UNC realpath never produces.  The anchors are the
    fence itself, and a UNC home with a junction inside ``KIROCREW_HOME`` must
    still be canonicalised or a canonical-spelling request would miss the
    governance file (found in review); the bound makes that probe safe.

    *budget* sizes the wait to the work the caller submits: the anchor REBUILD is
    one request carrying a few hundred paths and passes
    ``_PATH_RESOLVE_REBUILD_TIMEOUT_SECS``, while a candidate resolution keeps the
    default.
    """
    if budget is None:
        budget = _PATH_RESOLVE_TIMEOUT_SECS
    now = _path_resolve_clock()
    prefix = _stall_prefix(expanded)
    with _path_resolve_lock:
        history = _path_resolve_degraded.get(prefix)
    if history is not None and now < history[0]:
        raise PathResolutionStalled(expanded, prefix)
    if history is not None and _pool_down_to_last_child():
        # A re-probe of a prefix that has already stalled, with one live child left:
        # the probe is expected to wedge, and a wedged child is killed and replaced
        # only while the pool's list of killed-but-unexited children has room, so
        # spending the last child here would leave every OTHER prefix refused
        # uncharged for as long as that mount stays wedged.  Extend
        # the prefix's own cooldown instead and refuse; the mount is probed again when
        # a second child is back.  Nothing was observed, so no stall is recorded.
        logger.debug(
            "sensitive-path re-probe of a stalled prefix refused: the resolver pool is "
            "down to its last child; prefix cooldown extended"
        )
        _extend_cooldown(prefix)
        raise PathResolutionStalled(expanded, prefix)
    caller_tid = threading.get_ident()
    with _path_resolve_lock:
        if len(_path_resolve_thread_waits) > 64:
            # Clearing live entries would let thread churn refund the loop's spend.
            expired = [tid for tid, (end, _) in _path_resolve_thread_waits.items() if now >= end]
            for expired_tid in expired:
                del _path_resolve_thread_waits[expired_tid]
        window_end, seconds_spent = _path_resolve_thread_waits.get(caller_tid, (0.0, 0.0))
        if now >= window_end:
            seconds_spent = 0.0
    remaining = _PATH_RESOLVE_WAIT_CAP_SECS - seconds_spent
    if remaining <= 0:
        # No work ran, so this refusal says nothing about the prefix's mount.
        logger.debug(
            "sensitive-path symlink resolution refused without probing: calling thread's "
            "cumulative wait allowance exhausted; prefix not charged"
        )
        raise PathResolutionStalled(expanded, prefix)
    # Budget plus grace, as ONE deadline (see the note above _PATH_RESOLVE_GRACE_FACTOR).
    entitled = budget + min(budget * _PATH_RESOLVE_GRACE_FACTOR, _PATH_RESOLVE_GRACE_MAX_SECS)
    granted = min(entitled, remaining)
    wait_start = _path_resolve_clock()
    try:
        value = _resolve_on_calling_thread(worker, expanded, granted)
    except TimeoutError as exc:
        sampled = exc.child_syscall if isinstance(exc, SubprocessPoolTimeout) else None
        if not isinstance(exc, SubprocessPoolTimeout):
            # No child took the request: every child was leased for the whole budget.
            # Evidence about load, not the mount.
            logger.debug(
                "sensitive-path symlink resolution refused: the resolver pool was "
                "saturated and the resolution never started; prefix not charged"
            )
            raise PathResolutionStalled(expanded, prefix) from None
        if not _child_blocked_in_filesystem(sampled) and not _load_arm_budget_spent(prefix):
            # The child RAN but was not in the filesystem when the budget ran out: the
            # same conclusion as the arm above, reached one step later.  Refuse THIS
            # resolution instead of opening a cooldown across every path under the
            # prefix.
            logger.debug("sensitive-path resolution timed out under load; prefix not charged")
            raise PathResolutionStalled(expanded, prefix) from None
        if granted < entitled:
            # The deadline was clamped by the calling thread's allowance, not by
            # anything the filesystem did -- and on a host where the syscall probe
            # cannot discriminate (every Windows host, Apple silicon) the grace is
            # the ONLY signal, so a truncated one answers nothing.  Refuse this call.
            logger.debug(
                "sensitive-path resolution timed out with its budget or grace clamped "
                "by the calling thread's cumulative wait allowance; prefix not charged"
            )
            raise PathResolutionStalled(expanded, prefix) from None
        logger.debug("sensitive-path resolution timed out blocked in the filesystem")
        _mark_stalled(prefix, budget)
        raise PathResolutionStalled(expanded, prefix) from None
    except PathResolutionStalled:
        # The worker itself refused (a transport fault to the child): fail-closed,
        # exactly like a stall, and never the lexical forms.
        raise
    except Exception:
        # The worker's own exceptions are already swallowed inside the worker;
        # anything else here is a pool fault, and the gate's contract is to keep
        # the lexical forms rather than fail the tool call.
        logger.debug("sensitive-path symlink resolution failed", exc_info=True)
        return None
    finally:
        wait_end = _path_resolve_clock()
        elapsed = max(0.0, wait_end - wait_start)
        if elapsed >= _PATH_RESOLVE_WAIT_FLOOR_SECS:
            with _path_resolve_lock:
                _path_resolve_thread_waits[caller_tid] = (
                    wait_end + _PATH_RESOLVE_WAIT_WINDOW_SECS,
                    seconds_spent + elapsed,
                )
    if history is not None:
        # The mount answered again: forget the stall history so the next stall
        # starts from the base cooldown rather than an inherited backoff.
        with _path_resolve_lock:
            _path_resolve_degraded.pop(prefix, None)
    return value


def _resolved_forms_bounded(expanded: str) -> set[str]:
    """Return the symlink-resolved spellings of *expanded*, or an empty set.

    Empty means resolution FAILED (see :func:`_run_resolution_bounded`) or was
    deliberately not attempted -- a UNC path on Windows, see
    :func:`_is_unc_path`: the caller keeps the lexical forms, exactly as before
    the bound existed.  A resolution that does not COMPLETE raises
    :class:`PathResolutionStalled` through here, and every gate turns that into
    a refusal.  Tests swap :func:`_resolved_spellings` at module level for a
    blocking stub and advance ``_path_resolve_clock``.
    """
    if _ON_WINDOWS and _is_unc_path(expanded):
        return set()
    forms = _run_resolution_bounded(expanded, _resolved_spellings)
    return set() if forms is None else forms


def _realpaths_or_none(paths: list[str]) -> list[str | None]:
    """``os.path.realpath`` of every anchor in *paths*, in order, in ONE child round trip.

    ``None`` per entry where ``realpath`` raised.  Absolutized before sending, for
    the reason :func:`_resolved_spellings` gives.  A transport fault raises
    :class:`PathResolutionStalled` for the first anchor: the target set is never
    rebuilt from lexical spellings (see :func:`_resolved_root_key` for the fallbacks
    review found open), and no cooldown is charged, because a child fault is not a
    stalled mount.
    """
    if not paths:
        return []
    if _outside_bounded_call():
        return [_realpath_inline(path) for path in paths]
    try:
        payload = _child_request(
            OP_REALPATH_MANY, pack_strings(os.fsencode(_absolutized(p)) for p in paths)
        )
    except SubprocessPoolUnavailable as exc:
        logger.debug("resolver child faulted on the anchors: %s", exc)
        raise PathResolutionStalled(paths[0], _stall_prefix(paths[0])) from None
    if payload is None:
        return _inline_bounded(paths[0], lambda: [_realpath_inline(path) for path in paths])
    answers = unpack_strings(payload)
    if len(answers) != len(paths):
        raise PathResolutionStalled(paths[0], _stall_prefix(paths[0]))
    return [os.fsdecode(value) if value else None for value in answers]


def _realpath_inline(path: str) -> str | None:
    """``os.path.realpath`` in THIS interpreter, ``None`` where it raises (the child's mirror)."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _realpath_or_none(path: str) -> str | None:
    """``os.path.realpath`` for one target anchor; see :func:`_realpaths_or_none`."""
    return _realpaths_or_none([path])[0]
