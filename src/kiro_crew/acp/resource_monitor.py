"""Attribute live memory/CPU usage back to each chat session and worker.

The dashboard's ``resource_status`` probe answers "is the host under pressure",
but never "which chat is causing it". This module walks the process tree of every
live agent runtime and produces one :class:`EntrySample` per tracked root, so an
operator can see the top consumer the way a task manager does.

It reuses the ``/proc`` primitives that already exist rather than inventing new
probes: ``acp.runtime._iter_descendant_pids`` and ``acp.runtime._get_rss_mb`` on
Linux (pure ``/proc`` reads, no subprocess), and ``acp.runtime._get_rss_tree_mb``
as the cross-platform fallback for macOS/Windows. Because those reads block, the
per-root walk is offloaded to ``subprocess_executor()`` — the same dedicated pool
the runtime's own RSS probe uses — so the gateway event loop never stalls on a
tree that can be dozens of processes deep.

This file is the sampler *core*: the data models, the enumeration/measurement
pass, the CPU-delta pass, the attribution/dedupe/gateway-self pass, and the
host-context + request-driven caching pass.

The cache is deliberately *pull-only*: ``snapshot()`` returns the previous result
whenever it is younger than ``interval_s`` (default 2.0s) and otherwise walks the
trees inline on the calling request. Nothing schedules a timer or a background
task — a host that no client is watching pays nothing (Req 8.2).

The per-pid ``/proc`` reads — RSS *and* CPU ticks — all happen inside the blocking
walk on the executor thread (``_RootWalk`` carries the summed ticks back). The
event loop then does only arithmetic: a tick delta divided by elapsed time. This
keeps the loop off every ``/proc`` read, not just the RSS ones.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Generator, Optional

from kiro_crew import platform_compat, resource_status, sandbox
from kiro_crew.acp import runtime_registry
from kiro_crew.acp.liveness import read_pid_stat
from kiro_crew.acp.runtime import (
    _PS_TABLE_TTL_S,
    _get_rss_mb,
    _get_rss_tree_mb,
    _iter_descendant_pids,
)
from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)

#: Default staleness window for the request-driven cache (task 2.4). Declared
#: here so the model default and the not-yet-built cache agree on one value.
DEFAULT_INTERVAL_S = 2.0

#: The ``/proc`` root every CPU-tick read is resolved against. A module constant
#: (not a literal) so a test can point the reads at a faked tree, mirroring the
#: ``proc_root`` parameter ``read_pid_stat`` already carries.
_PROC_ROOT = "/proc"

#: Where the host's total-memory figure is read from. A module constant so a
#: test can redirect the read at a faked file (the same seam the subagent memory
#: probes use), rather than the real host's ``/proc``.
_MEMINFO_PATH = "/proc/meminfo"

#: The one bound on how many pids a single root's tree may OCCUPY -- in the
#: enumeration's own walk (order, visited set and frontier), the per-pid
#: readings, the entry's ``pids`` set, the dedupe partition and the serialized
#: snapshot. The population is agent-controlled (a runaway agent can fork
#: without limit), so it is applied at the walk (``max_pids``) rather than after
#: it: nothing past the bound is enumerated, read or stored. A tree that reaches
#: the bound is flagged ``truncated`` and its figures (RSS, CPU, ``proc_count``)
#: are a LOWER BOUND over the pids it did read. Far above any healthy tree (a
#: busy chat runs tens of processes), so the flag is itself a signal.
MAX_TREE_PIDS = 4096

#: The one bound on how many rows of the HOST process table the macOS sampler
#: retains per snapshot (its parent map and per-pid RSS map together). A
#: different population from ``MAX_TREE_PIDS``: that bounds one root's tree,
#: this bounds every process on the box, which a fork storm anywhere -- not only
#: under an agent root -- can inflate towards the kernel's pid ceiling. Applied
#: while the ``ps`` stream is READ: a row past the bound is never parsed or
#: stored and ``ps`` is stopped, so the retained structure cannot outgrow it.
#: A table that reached the bound is flagged truncated, and every tree walked
#: from it is reported ``truncated`` too, since a descendant may sit in the rows
#: that were never read. Far above any healthy host (hundreds to a few thousand
#: processes), so the flag is itself a signal.
MAX_HOST_TABLE_ROWS = 32768

#: Wall-clock budget for one ``ps`` table read, the same as the shared reader's.
_PS_TABLE_TIMEOUT_S = 2.0

#: ``(children, rss_kib, truncated)``: parent map, per-pid RSS and whether the
#: read stopped at ``MAX_HOST_TABLE_ROWS`` before the table was exhausted.
_HostTable = tuple[dict[int, list[int]], dict[int, int], bool]

_host_table_lock = threading.Lock()
_host_table_cache: Optional[tuple[float, _HostTable]] = None


def _ps_rows(deadline: float) -> Optional[Generator[bytes, None, None]]:
    """Stream ``ps -Ao pid=,ppid=,rss=`` one line at a time, or None without ``ps``.

    A generator rather than ``check_output`` so the consumer can stop after
    ``MAX_HOST_TABLE_ROWS`` lines without the rest of the table ever being
    buffered; closing the generator (or exhausting it) reaps the child. Each
    line waits at most until *deadline* (monotonic) so a wedged ``ps`` cannot
    hold the sampler -- and a stream cut by that deadline ends with
    ``_STREAM_CUT`` (an empty row, which ``readline`` never yields for a real
    line) so the consumer can tell "table exhausted" from "table cut short":
    the rows it never saw may hold anyone's descendants, so the table is
    truncated exactly as if the row bound had been hit.
    """
    ps_bin = platform_compat.trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        proc = subprocess.Popen(
            [ps_bin, "-Ao", "pid=,ppid=,rss="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None

    def _lines() -> Generator[bytes, None, None]:
        assert proc.stdout is not None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield _STREAM_CUT
                    return
                ready, _, _ = select.select([proc.stdout], [], [], remaining)
                if not ready:
                    yield _STREAM_CUT
                    return
                line = proc.stdout.readline()
                if not line:
                    return
                yield line
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.SubprocessError:
                pass
            if proc.stdout is not None:
                proc.stdout.close()

    return _lines()


#: Trailing row of a ``_ps_rows`` stream that hit its deadline before ``ps``
#: finished. Empty on purpose: a real ``readline`` row is never empty.
_STREAM_CUT = b""


def _read_host_table(max_rows: int = MAX_HOST_TABLE_ROWS) -> Optional[_HostTable]:
    """Parse at most *max_rows* rows of the ``ps`` stream into the two maps.

    The bound is enforced on the STREAM: the (max_rows + 1)th row is the signal
    that the table is larger than what is retained -- it is not parsed, ``ps``
    is stopped, and the table is flagged truncated. A stream that ends with
    ``_STREAM_CUT`` (deadline hit) is flagged the same way: what was not read
    is unknown either way. Rows are ints only, so the row count is the only
    field that needs bounding.
    """
    rows = _ps_rows(time.monotonic() + _PS_TABLE_TIMEOUT_S)
    if rows is None:
        return None
    children: dict[int, list[int]] = {}
    rss_kib: dict[int, int] = {}
    retained = 0
    truncated = False
    try:
        for raw in rows:
            if raw == _STREAM_CUT:
                truncated = True
                break
            if retained >= max_rows:
                truncated = True
                break
            parts = raw.split()
            if len(parts) < 3:
                continue
            try:
                cpid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(cpid)
            rss_kib[cpid] = rss
            retained += 1
    finally:
        rows.close()
    return (children, rss_kib, truncated)


def _host_process_table() -> Optional[_HostTable]:
    """The bounded ``ps`` table, memoized for ``_PS_TABLE_TTL_S``.

    One read serves every root the sampler walks in a snapshot (and the
    gateway-self pass), so the cost is one bounded ``ps`` per interval rather
    than one per row. Taken under the lock so concurrent first callers do not
    each spawn ``ps``. Returns None when ``ps`` is unavailable so callers fall
    back to a single-pid read rather than a phantom-empty tree.
    """
    global _host_table_cache
    with _host_table_lock:
        cached = _host_table_cache
        if cached is not None and (time.monotonic() - cached[0]) < _PS_TABLE_TTL_S:
            return cached[1]
        table = _read_host_table()
        if table is not None:
            _host_table_cache = (time.monotonic(), table)
        return table


#: The cgroup v2 unlimited sentinel: ``memory.max`` reads the literal ``max``,
#: but ``memory.high`` (and a v1 limit) reads a sentinel-large integer. Anything
#: at or above this is "no cap", so the cgroup gauge is omitted rather than shown
#: as a meaningless ceiling. Mirrors ``subagent._CGROUP_UNLIMITED``.
_CGROUP_UNLIMITED = 1 << 62

_BYTES_PER_GB = 1024**3

#: Clock ticks per second — the denominator that turns a utime+stime tick delta
#: into CPU-seconds. Read once at import (same source as ``subagent._CLK_TCK``);
#: falls back to the conventional 100 Hz on a platform without ``sysconf`` so the
#: divisor is never zero.
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# A callable that yields the live agent runtimes to sample. Defaults to the
# runtime registry, but is injectable so this module — and its tests — need not
# depend on ``acp.runtime_registry`` (written concurrently) or on a real gateway.
LiveRuntimes = Callable[[], list[Any]]

# Resolves a session key to the dashboard slot id that hosts that chat, or
# ``None`` when the session is not a dashboard chat (a channel, a background run,
# a review-pool worker). Injected by the dashboard layer (task 3.1) so this
# module never imports DashboardState — it stays dashboard-agnostic. The default
# resolves nothing, so with no resolver every runtime falls through to worker.
SlotResolver = Callable[[str], Optional[str]]

# Looks a runtime's owning subagent record up from the subagent manager, or
# returns ``None`` when the runtime is not a dedicated subagent process. The
# returned object is duck-typed (``id`` / ``task`` / ``agent`` /
# ``parent_session_key`` read best-effort), so this module need not import
# ``subagent`` and the real accessor is wired later (task 3.1). The default
# never finds a subagent, so classification degrades to chat-or-worker.
SubagentLookup = Callable[[Any], Optional[Any]]


def _no_slot(_session_key: str) -> Optional[str]:
    """Default :data:`SlotResolver`: no session is a dashboard chat."""
    return None


def _no_subagent(_runtime: Any) -> Optional[Any]:
    """Default :data:`SubagentLookup`: no runtime is a dedicated subagent."""
    return None


def _default_live_runtimes() -> list[Any]:
    """Default :data:`LiveRuntimes`: enumerate the live-runtime registry.

    A separate function (rather than passing ``runtime_registry.live_runtimes``
    directly) so a test can monkeypatch the registry module attribute and so the
    sampler's constructor default is a stable, patchable name.
    """
    return runtime_registry.live_runtimes()


@dataclass
class EntrySample:
    """One attributed process tree in a :class:`ResourceSnapshot`.

    ``rss_mb`` / ``cpu_pct`` / ``uptime_s`` are ``None`` when unreadable rather
    than zero, so a UI can render "unknown" (a dash) distinctly from "idle". A
    root pid that has vanished by the time it is walked yields no entry at all —
    the sampler omits it rather than emitting a hollow row.
    """

    kind: str  # "chat" | "subagent" | "worker" | "gateway"
    session_key: str  # "" when not applicable
    label: str  # chat title / subagent task / worker role
    agent: str  # agent name when known, else ""
    pid: int
    proc_count: int
    rss_mb: Optional[float]
    cpu_pct: Optional[float]  # None until a delta base exists (task 2.2)
    uptime_s: Optional[float]
    slot: str = ""  # dashboard slot id for chat entries (task 2.3)
    subagent_id: str = ""  # dedicated subagent entries (task 2.3)
    # The full pid set this entry covers. Carried on the sample so the dedupe /
    # gateway-self pass (task 2.3) can partition the pid union without re-walking.
    # Bounded by ``MAX_TREE_PIDS``; ``truncated`` marks a tree that reached the
    # bound, whose rss/cpu/proc_count are then a lower bound over the pids read.
    pids: frozenset[int] = field(default_factory=frozenset)
    truncated: bool = False
    # The runtime's per-spawn instance id (``process_instance``), "" when the
    # runtime exposes none. A pid alone does not name a process for long -- the
    # OS reuses pids -- so a Stop issued from this row submits BOTH, and the
    # stop route refuses unless the slot's current runtime matches both.
    instance: str = ""


@dataclass
class ResourceSnapshot:
    """A point-in-time attribution of host resources to gateway-owned trees."""

    entries: list[EntrySample]
    posture: str = "unknown"
    available_gb: float = -1.0
    host_total_gb: Optional[float] = None
    cpu_count: Optional[int] = None
    cgroup_used_gb: Optional[float] = None
    cgroup_limit_gb: Optional[float] = None
    sampling_supported: bool = True
    captured_at: float = 0.0
    interval_s: float = DEFAULT_INTERVAL_S


@dataclass
class _RootWalk:
    """The blocking-walk result for one root pid, before identity is attached.

    Separated from :class:`EntrySample` because the walk runs on the executor
    thread and knows only pids and raw measurements; identity/attribution
    (kind, label, dedupe) is decided back on the event loop by later passes.

    ``cpu_ticks`` is the utime+stime total summed across the whole tree during
    the SAME walk that sums RSS — every per-pid ``/proc`` read stays on the
    executor thread. ``None`` off Linux (no ``/proc`` tick semantics), so the
    event-loop CPU pass has only a subtraction and a division to do.

    ``per_pid`` keeps the individual ``(rss_mb, cpu_ticks)`` reading behind those
    totals, keyed by pid, so a later pass can re-aggregate over a SUBSET of the
    tree without another ``/proc`` read. The gateway-self pass needs this: the
    gateway is the ancestor of every runtime it spawned, so its whole-tree totals
    are the fleet's totals, and only the pids no runtime claimed describe the
    gateway itself. Populated on Linux (``/proc``) and macOS (the ``ps`` table);
    root-only on Windows, whose lineage-validated tree sum yields no pid list.
    """

    root_pid: int
    pids: list[int]
    rss_mb: Optional[float]
    cpu_ticks: Optional[int]
    per_pid: dict[int, tuple[Optional[float], Optional[int]]] = field(default_factory=dict)
    #: The walk reached ``MAX_TREE_PIDS`` and stopped: the totals above cover
    #: only the pids it read.
    truncated: bool = False

    def restricted_to(self, keep: frozenset[int]) -> "_RootWalk":
        """The same walk re-aggregated over ``keep`` only.

        RSS is the sum of the kept pids' readable RSS (``None`` when none of them
        had one); ticks likewise, but ``None`` whenever the original walk had no
        tick semantics so the CPU pass keeps treating the platform as
        tick-less. A pid in ``keep`` that the walk never measured contributes
        nothing rather than raising.
        """
        rss_total = 0.0
        rss_found = False
        ticks_total = 0
        for pid in keep:
            rss, ticks = self.per_pid.get(pid, (None, None))
            if rss is not None:
                rss_total += rss
                rss_found = True
            if ticks is not None:
                ticks_total += ticks
        return _RootWalk(
            root_pid=self.root_pid,
            pids=sorted(keep),
            rss_mb=rss_total if rss_found else None,
            cpu_ticks=None if self.cpu_ticks is None else ticks_total,
            per_pid={p: self.per_pid[p] for p in keep if p in self.per_pid},
        )


class ResourceSampler:
    """Measures per-runtime process trees and assembles a snapshot.

    Stateful across calls: it keeps the CPU-tick baseline and the cached snapshot
    that a point-in-time observation cannot derive on its own. A ``snapshot()``
    call younger than ``interval_s`` after the last one returns that cached result
    untouched; only a stale (or first-ever) call walks the trees. There is no
    timer and no background task — sampling happens strictly inside a request that
    finds the cache stale (Req 8.2).
    """

    def __init__(
        self,
        *,
        live_runtimes: Optional[LiveRuntimes] = None,
        slot_resolver: Optional[SlotResolver] = None,
        subagent_lookup: Optional[SubagentLookup] = None,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._live_runtimes: LiveRuntimes = live_runtimes or _default_live_runtimes
        # None-safe defaults keep the sampler usable before the dashboard/subagent
        # accessors are wired (task 3.1): with them a runtime is only ever a chat
        # when its session resolves to a slot, and only ever a subagent when the
        # manager owns it — otherwise it is a worker.
        self._slot_resolver: SlotResolver = slot_resolver or _no_slot
        self._subagent_lookup: SubagentLookup = subagent_lookup or _no_subagent
        self._interval_s = interval_s
        # CPU-delta baseline keyed by root pid: {root_pid: (cpu_ticks, monotonic)}.
        # Written on every pass by ``_cpu_pct_for_root`` and pruned of absent roots
        # by ``_prune_cpu_cache`` so a delta always describes the same live tree.
        self._cpu_prev: dict[int, tuple[int, float]] = {}
        # Request-driven snapshot cache: the last full result and the monotonic
        # instant it was captured. A call within ``interval_s`` of ``_cache_ts``
        # returns ``_cache`` verbatim; there is no timer refreshing it. ``None``
        # until the first sample completes.
        self._cache: Optional[ResourceSnapshot] = None
        self._cache_ts: Optional[float] = None
        # Serializes the cache-MISS path. Two pollers that both find the cache
        # stale must not both sample: the second would write a CPU baseline
        # milliseconds after the first (an invalid delta) and whichever finished
        # last would overwrite the newer snapshot with the older one. The lock is
        # taken only on a miss, and freshness is re-checked under it, so the
        # follower serves the leader's fresh result instead of sampling again.
        self._miss_lock = asyncio.Lock()

    # ── measurement (task 2.1) ──────────────────────────────────────────────
    def _walk_root(self, root_pid: int) -> Optional[_RootWalk]:
        """Blocking walk of one root's tree. Runs on the executor thread.

        On Linux the tree is enumerated with ``_iter_descendant_pids``; RSS is
        summed per pid with ``_get_rss_mb`` and CPU ticks with ``read_pid_stat``
        in the SAME loop, so every per-pid ``/proc`` read is paid here on the
        executor thread — the event loop later does only a delta and a divide. An
        unreadable pid is skipped for whichever field it lacks (it narrows the
        total rather than voiding it), and a tree with no readable RSS reports
        ``rss_mb=None``. Off Linux there is no per-pid ``/proc``, so the
        cross-platform ``_get_rss_tree_mb`` fallback both walks and sums RSS, the
        pid set collapses to the root alone, and ``cpu_ticks`` is ``None`` (no
        tick semantics) — descendant enumeration and tick reads are Linux-only.

        Returns ``None`` when the root pid has vanished — no readable pid and no
        tree — so the caller omits the entry without raising (Req 1.6).
        """
        if sys.platform == "linux":
            # Bounded at the walk: one past the cap is asked for so that the
            # length alone says whether the tree exceeded it, and the extra pid
            # is dropped unread. The walk puts the root first, so the root is
            # always kept.
            pids = _iter_descendant_pids(root_pid, max_pids=MAX_TREE_PIDS + 1)
            if not pids:
                # No pids at all means the root's /proc entry was gone before the
                # walk even started: treat it as vanished.
                return None
            truncated = len(pids) > MAX_TREE_PIDS
            if truncated:
                del pids[MAX_TREE_PIDS:]
            total = 0.0
            found = False
            ticks = 0
            any_stat = False
            per_pid: dict[int, tuple[Optional[float], Optional[int]]] = {}
            for p in pids:
                r = _get_rss_mb(p)
                if r is not None:
                    total += r
                    found = True
                stat = read_pid_stat(_PROC_ROOT, p)
                pid_ticks: Optional[int] = None
                if stat is not None:
                    # read_pid_stat -> (state, starttime, cpu_ticks); sum the cpu.
                    pid_ticks = stat[2]
                    ticks += pid_ticks
                    any_stat = True
                per_pid[p] = (r, pid_ticks)
            if not found and not any_stat:
                # The enumeration always yields at least the root (a process
                # missing from the map reads as "root alone"), so a root whose
                # every pid has neither RSS nor stat is gone, not merely opaque:
                # omit it rather than emit a phantom row with dashes (Req 1.6).
                return None
            return _RootWalk(
                root_pid=root_pid,
                pids=pids,
                rss_mb=total if found else None,
                cpu_ticks=ticks,
                per_pid=per_pid,
                truncated=truncated,
            )

        # No per-pid CPU tick reads exist off Linux, so CPU is unavailable on
        # both remaining platforms. A None RSS means the root is unreadable or
        # gone — omit the entry.
        if sys.platform == "darwin":
            return self._walk_root_darwin(root_pid)
        return self._walk_root_windows(root_pid)

    @staticmethod
    def _walk_root_darwin(root_pid: int) -> Optional[_RootWalk]:
        """macOS: the same bounded walk as Linux, read from one ``ps`` table.

        ``_host_process_table`` is a single (memoized) ``ps -Ao pid,ppid,rss``
        snapshot -- a parent map and a per-pid RSS map -- read under
        ``MAX_HOST_TABLE_ROWS`` at the stream, so the retained table is bounded
        before any tree is walked from it. The descendant walk is the shared
        ``_iter_descendant_pids`` over that map with ``max_pids``, so the tree
        is bounded exactly as on Linux (``MAX_TREE_PIDS``, flagged
        ``truncated``), and a table that was itself cut short flags every tree
        walked from it, since a descendant may sit in the unread rows. Every
        pid's own RSS is retained, which is what lets the gateway-self pass
        subtract attributed pids here too.
        """
        table = _host_process_table()
        if table is None:
            # ``ps`` unavailable: the root alone, as ``_get_rss_tree_mb`` does.
            rss = _get_rss_mb(root_pid)
            if rss is None:
                return None
            return _RootWalk(
                root_pid=root_pid,
                pids=[root_pid],
                rss_mb=rss,
                cpu_ticks=None,
                per_pid={root_pid: (rss, None)},
            )
        children, rss_kib, table_truncated = table
        if root_pid not in rss_kib:
            return None
        pids = _iter_descendant_pids(root_pid, children=children, max_pids=MAX_TREE_PIDS + 1)
        truncated = table_truncated or len(pids) > MAX_TREE_PIDS
        if truncated:
            del pids[MAX_TREE_PIDS:]
        per_pid: dict[int, tuple[Optional[float], Optional[int]]] = {}
        total = 0.0
        for p in pids:
            kib = rss_kib.get(p)
            mb = None if kib is None else kib / 1024.0
            if mb is not None:
                total += mb
            per_pid[p] = (mb, None)
        return _RootWalk(
            root_pid=root_pid,
            pids=pids,
            rss_mb=total,
            cpu_ticks=None,
            per_pid=per_pid,
            truncated=truncated,
        )

    @staticmethod
    def _walk_root_windows(root_pid: int) -> Optional[_RootWalk]:
        """Windows: the lineage-validated tree sum, admitted only under the cap.

        ``proc_rss_tree_mb_for_pid`` validates every parent edge against
        creation times (a recycled pid must not pull an unrelated subtree in),
        which is why its walk is not the shared one and yields no pid list. It
        is therefore SIZED before it runs: one Toolhelp PID->PPID snapshot is
        walked with ``platform_compat``'s own bounded descendant walk (the one
        its cleanup identity uses, which raises past its ``limit``), and a
        tree that exceeds ``MAX_TREE_PIDS`` is not summed at all: the row falls
        back to the root's own RSS, flagged ``truncated``, so a fork storm
        cannot drive an unbounded validated walk and a lower bound is never
        presented as a total. Descendant identities stay unretained (the
        validated set is the helper's own), so the gateway root reads as its
        own single process here, as the feature map states.
        """
        own = _get_rss_mb(root_pid)
        if root_pid == os.getpid():
            if own is None:
                return None
            return _RootWalk(
                root_pid=root_pid,
                pids=[root_pid],
                rss_mb=own,
                cpu_ticks=None,
                per_pid={root_pid: (own, None)},
            )
        if _windows_tree_exceeds_cap(root_pid):
            if own is None:
                return None
            return _RootWalk(
                root_pid=root_pid,
                pids=[root_pid],
                rss_mb=own,
                cpu_ticks=None,
                per_pid={root_pid: (own, None)},
                truncated=True,
            )
        rss = _get_rss_tree_mb(root_pid)
        if rss is None:
            return None
        return _RootWalk(
            root_pid=root_pid,
            pids=[root_pid],
            rss_mb=rss,
            cpu_ticks=None,
            per_pid={root_pid: (rss, None)},
        )

    def _blocking_walk(self, root_pids: list[int]) -> dict[int, _RootWalk]:
        """Walk every distinct root pid once, off the event loop.

        Deduplicated on root pid so a runtime shared by co-tenant sessions is
        walked a single time. A per-root failure is swallowed (a dying pid must
        never fail the whole snapshot) and simply omits that root.
        """
        out: dict[int, _RootWalk] = {}
        for root_pid in root_pids:
            if root_pid in out:
                continue
            try:
                walk = self._walk_root(root_pid)
            except Exception:  # pragma: no cover — a dying pid must not fail the pass
                logger.debug("resource walk failed for pid %s", root_pid, exc_info=True)
                continue
            if walk is not None:
                out[root_pid] = walk
        return out

    def _blocking_sample(self, root_pids: list[int]) -> tuple[dict[int, _RootWalk], _HostProbes]:
        """One executor hop for everything that touches the filesystem.

        The process-tree walk and the host probes are independent reads; bundling
        them here means a stale snapshot costs exactly one ``run_in_executor`` and
        the event loop never performs a ``/proc``, cgroup or config read itself.
        """
        return self._blocking_walk(root_pids), _read_host_probes()

    def _entry_for_runtime(self, runtime: Any, walk: _RootWalk, now: float) -> EntrySample:
        """Build the measured entry for one runtime from its completed walk.

        Task 2.1 fills the measurement fields (pid, proc_count, rss_mb, uptime)
        and leaves identity at its neutral default (``kind="worker"``, empty
        label/slot/subagent_id): the classification/attribution pass (task 2.3)
        rewrites those, and the CPU pass (task 2.2) fills ``cpu_pct``.
        """
        agent, spawn_monotonic = _runtime_identity(runtime)
        uptime_s = None if spawn_monotonic is None else max(0.0, now - spawn_monotonic)
        return EntrySample(
            kind="worker",
            session_key="",
            label="",
            agent=agent,
            pid=walk.root_pid,
            proc_count=len(walk.pids),
            rss_mb=walk.rss_mb,
            cpu_pct=self._cpu_pct_for_root(walk, now),
            uptime_s=uptime_s,
            pids=frozenset(walk.pids),
            truncated=walk.truncated,
            instance=_runtime_instance(runtime),
        )

    # ── CPU delta tracking (task 2.2) ───────────────────────────────────────
    def _cpu_pct_for_root(self, walk: _RootWalk, now: float) -> Optional[float]:
        """CPU percentage for a tree between successive samples.

        The utime+stime tick total was already summed across the tree on the
        executor thread (``walk.cpu_ticks``); this pass only stores
        ``{root_pid: (ticks, monotonic)}`` as the baseline for the next pass and
        returns ``100 * Δticks / SC_CLK_TCK / Δt`` — the tree's share of one core
        scaled to a percentage, so a tree pinning two full cores reads ~200. The
        result is clamped to ``[0, 100 * cpu_count]`` so a clock skew or a torn
        read can never report negative or superhuman CPU. Keeping every ``/proc``
        read out of this method means the event loop pays only arithmetic here.

        ``None`` (unavailable, never ``0.0``) is returned in every case a delta
        cannot be trusted:

        * a non-``/proc`` platform, where the walk supplied no ticks (Req 7.2);
        * first sight of this root pid — no baseline to subtract (Req 1.4);
        * ``Δt <= 0`` (clock non-monotonicity / same-instant re-read);
        * ``Δticks < 0`` (the root pid was recycled to a lighter process, so the
          old baseline describes a different tree).

        Updating ``self._cpu_prev`` here — even when returning ``None`` — is what
        lets the *second* observation of a root produce a real figure. Baselines
        for roots absent from the current pass are pruned by :meth:`snapshot`.
        """
        ticks = walk.cpu_ticks
        if ticks is None:
            # Off Linux / no /proc tick semantics: no baseline, nothing to prune.
            return None

        prev = self._cpu_prev.get(walk.root_pid)
        self._cpu_prev[walk.root_pid] = (ticks, now)
        if prev is None:
            return None

        prev_ticks, prev_ts = prev
        dt = now - prev_ts
        if dt <= 0 or ticks < prev_ticks:
            return None

        pct = 100.0 * (ticks - prev_ticks) / _CLK_TCK / dt
        ceiling = 100.0 * (os.cpu_count() or 1)
        return max(0.0, min(pct, ceiling))

    def _prune_cpu_cache(self, live_root_pids: set[int]) -> None:
        """Drop CPU baselines for roots absent from the current pass.

        A runtime that has exited leaves a stale ``{root_pid: (ticks, ts)}``
        entry behind; without pruning the cache grows without bound and, worse, a
        recycled pid would delta against a dead tree's ticks. Called once per
        snapshot with the roots actually walked (Req 1.6).
        """
        for stale in [root for root in self._cpu_prev if root not in live_root_pids]:
            del self._cpu_prev[stale]

    # ── classification & attribution (task 2.3) ─────────────────────────────
    def _classify(self, runtime: Any, entry: EntrySample) -> EntrySample:
        """Assign chat/subagent/worker identity to a measured entry.

        The order is deliberate — a runtime is a *chat* when one of its sessions
        resolves to a dashboard slot, a *subagent* when the subagent manager owns
        it, and a *worker* otherwise:

        * **chat**: the slot resolver maps one of the runtime's registered session
          keys to a dashboard slot id. The first session that resolves wins (a
          multiplexed runtime can serve several, but the slot is the chat the
          operator clicks through to). ``label`` is left for the resolver's caller
          to enrich with the slot title in task 3.1; here it carries the session
          key so the entry is never nameless.
        * **subagent**: the subagent manager's lookup returns a record for this
          runtime — a DEDICATED run in its own process. The record supplies
          ``label`` (task description), ``subagent_id`` and the parent session
          key. ``is_subagent_owner`` is not a subagent signal: it marks a runtime
          that HOSTS shared backend subagents, and those ride the host runtime's
          entry (Req 2.2), so they are never split out here.
        * **worker**: anything else — a background runtime, a review-pool worker,
          a channel session with no dashboard slot.

        Both accessors are None-safe: with the default resolver/lookup every
        runtime falls through to ``worker``, which is exactly the pre-wiring
        behaviour. Neither accessor is trusted to be well-behaved — a raising
        resolver degrades that runtime to worker rather than failing the pass.
        """
        # chat: a session key resolves to a dashboard slot.
        for session_key in _runtime_session_keys(runtime):
            try:
                slot = self._slot_resolver(session_key)
            except Exception:  # pragma: no cover — a bad resolver must not fail the pass
                logger.debug("slot resolver raised for %s", session_key, exc_info=True)
                slot = None
            if slot:
                return replace(
                    entry,
                    kind="chat",
                    session_key=session_key,
                    slot=slot,
                    label=entry.label or session_key,
                )

        # subagent: the manager owns this runtime as a DEDICATED run. The lookup
        # (pid-keyed in the dashboard wiring) is the authority. ``is_subagent_owner``
        # is deliberately NOT consulted: that marker flags a runtime that HOSTS
        # shared backend subagents, which is a chat/worker, never a subagent row.
        info = None
        try:
            info = self._subagent_lookup(runtime)
        except Exception:  # pragma: no cover — a bad lookup must not fail the pass
            logger.debug("subagent lookup raised for pid %s", entry.pid, exc_info=True)
            info = None
        if info is not None:
            sub_id, task, parent, sub_agent = _subagent_fields(info)
            return replace(
                entry,
                kind="subagent",
                subagent_id=sub_id,
                session_key=parent,
                label=task or entry.label,
                agent=sub_agent or entry.agent,
            )

        # worker: the measured default is already correct.
        return entry

    def _attribute(self, entries: list[EntrySample]) -> list[EntrySample]:
        """Partition the attributed pid union so no pid is counted twice (Req 8.4).

        Entries are visited in deterministic order (registry order, which
        ``live_runtimes()`` sorts by root pid): the first entry to claim a pid
        keeps it, and every later entry has that pid removed from its set and its
        ``proc_count`` reduced to match. This makes the union of all entry pid
        sets a true partition — the invariant Property 1 checks. An entry whose
        pids are entirely subsumed by earlier entries survives as a zero-pid row
        (its runtime is real; its tree is merely wholly claimed elsewhere) rather
        than vanishing, so the operator still sees it.

        Returns a new list of adjusted entries; ``pids``/``proc_count`` are the
        only fields touched, RSS/CPU are left as measured on the full tree.
        """
        claimed: set[int] = set()
        out: list[EntrySample] = []
        for entry in entries:
            own = entry.pids - claimed
            claimed |= entry.pids
            out.append(replace(entry, pids=own, proc_count=len(own)))
        return out

    def _gateway_entry(
        self,
        walk: Optional[_RootWalk],
        attributed: set[int],
        now: float,
        *,
        partition_complete: bool = True,
    ) -> Optional[EntrySample]:
        """Build the gateway-self entry: the gateway's own tree minus everything
        already attributed (Req 2.4, 8.4).

        The gateway is the current process, so its root is ``os.getpid()`` and its
        tree is walked on the executor alongside the runtime trees (``walk`` is
        that result, ``None`` when the gateway root vanished — which cannot
        normally happen for the running process). The attributed pid union is
        subtracted — a runtime child that happens to also be a gateway descendant
        must not be double-counted. When every pid of the gateway tree is already
        attributed (nothing left that is the gateway's alone), no gateway entry is
        emitted.

        RSS and CPU on this entry ARE re-aggregated over the remainder, unlike the
        runtime-vs-runtime dedupe pass. The gateway is the parent of every runtime
        it spawned, so its whole-tree figures are the fleet's totals: reporting
        them here would double-count every chat's memory on the gateway row and
        flag the gateway as the top consumer on any busy host. The per-pid
        readings the walk already took (``_RootWalk.per_pid``) make this a sum on
        the event loop, not another ``/proc`` pass. The CPU baseline is keyed on
        the gateway root as usual; because the own-pid set can shrink between
        passes (a new runtime claims a pid), a negative tick delta is possible and
        is already reported as ``None`` by ``_cpu_pct_for_root``.

        The subtraction is exact only while every tree was walked whole. Once
        any RUNTIME tree hit ``MAX_TREE_PIDS`` (``partition_complete`` False),
        a pid of that runtime past its bound may still sit inside the gateway's
        walk and would be claimed here as the gateway's own; and once the
        GATEWAY's walk hit the bound its remainder is a lower bound like any
        truncated tree. Either way the row is flagged ``truncated`` so the
        reader knows the partition, not just the count, is inexact. Only pids
        the gateway walk actually read are ever counted — nothing unread is
        added back — so the flag marks imprecision, never invention.
        """
        if walk is None:
            return None
        own = frozenset(walk.pids) - attributed
        if not own:
            return None
        own_walk = walk.restricted_to(own)
        cpu_pct = self._cpu_pct_for_root(own_walk, now)
        return EntrySample(
            kind="gateway",
            session_key="",
            label="gateway",
            agent="",
            pid=walk.root_pid,
            proc_count=len(own),
            rss_mb=own_walk.rss_mb,
            cpu_pct=cpu_pct,
            uptime_s=None,
            pids=own,
            truncated=walk.truncated or not partition_complete,
        )

    def _host_context(self, snapshot: ResourceSnapshot, probes: _HostProbes) -> ResourceSnapshot:
        """Attach host posture, memory, cpu count and cgroup gauges.

        ``probes`` is the result of :func:`_read_host_probes`, which ran on the
        executor thread — this method only copies fields; no filesystem read
        happens on the event loop here.

        Fills the host headroom fields so a per-chat figure can be read against
        the real budget (Req 3.1-3.3). Each source degrades independently — a
        source that cannot be read leaves its field at the model default (``-1.0``
        for ``available_gb``, ``None`` for the optional gauges) rather than
        failing the pass (Req 7.2):

        * **posture / available_gb**: ``resource_status.probe()``, which already
          clamps to cgroup headroom and never raises (posture ``unknown`` /
          ``available_gb=-1.0`` on its own failure).
        * **cpu_count**: ``os.cpu_count()`` (``None`` when the platform hides it).
        * **host_total_gb**: ``MemTotal`` from ``/proc/meminfo`` when readable,
          otherwise ``None`` — the platform does not expose it (Req 3.2).
        * **cgroup gauges**: the agents-slice ``memory.current`` / ``memory.max``
          when the slice is active and the limit is a real number; OMITTED (left
          ``None``) on an unconstrained host or off Linux, so the UI shows no
          permanent N/A bar (Req 3.3, KRS default).
        """
        posture, available_gb, host_total_gb, cgroup_used_gb, cgroup_limit_gb = probes
        return replace(
            snapshot,
            posture=posture,
            available_gb=available_gb,
            host_total_gb=host_total_gb,
            cpu_count=os.cpu_count(),
            cgroup_used_gb=cgroup_used_gb,
            cgroup_limit_gb=cgroup_limit_gb,
        )

    async def _unsupported_snapshot(self, now: float) -> ResourceSnapshot:
        """Host-context-only snapshot for a platform without per-process sampling.

        A platform the sampler cannot walk per-process (Req 7.3) still answers
        with the host budget so the page is not blank: ``sampling_supported`` is
        ``False``, ``entries`` is empty, and the host-context pass fills whatever
        the platform DOES expose (posture, cpu count, and — off Linux — no cgroup
        gauge). The host probes still read files, so they take the same executor
        hop the Linux path does. Cached like any other snapshot so repeated polls
        are free.
        """
        probes = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _read_host_probes
        )
        base = ResourceSnapshot(
            entries=[],
            sampling_supported=False,
            captured_at=time.time(),
            interval_s=self._interval_s,
        )
        return self._host_context(base, probes)

    # ── public API ──────────────────────────────────────────────────────────
    async def snapshot(self) -> ResourceSnapshot:
        """Produce a :class:`ResourceSnapshot`, serving a fresh cache when able.

        Caching is pull-only (Req 8.2): a call within ``interval_s`` of the last
        completed sample returns that cached result verbatim, so a page polling
        faster than the interval never re-walks a single ``/proc`` tree. There is
        no timer and no background task — the ONLY thing that ever refreshes the
        cache is a ``snapshot()`` call that finds it stale (or absent). The
        CPU-delta baseline persists between calls, so the second stale call onward
        carries real CPU figures. Both fresh and stale results (including the
        platform-unsupported one) are cached identically.

        Concurrent misses are serialized: the first caller to find the cache
        stale samples under ``_miss_lock``; any caller that arrives meanwhile
        waits, re-checks freshness once it holds the lock, and returns the
        leader's snapshot rather than sampling a second time (a second sample
        milliseconds later would corrupt the CPU-delta baseline and could land
        in the cache after — and older than — the first).
        """
        cached = self._fresh_cached(time.monotonic())
        if cached is not None:
            return cached

        async with self._miss_lock:
            now = time.monotonic()
            cached = self._fresh_cached(now)
            if cached is not None:
                return cached

            if not _sampling_supported():
                # A platform with no per-process mechanism answers with host
                # context only and a machine-readable flag (Req 7.3). Cached like
                # any other result so rapid polls stay free.
                snapshot = await self._unsupported_snapshot(now)
            else:
                snapshot = await self._sample(now)

            self._cache = snapshot
            # Stamped when the sample COMPLETES, not when it started: a slow walk
            # (a big host, a stressed disk) that took most of ``interval_s`` would
            # otherwise leave the fresh cache already near-stale and the next
            # poll would re-walk at once. ``now`` still feeds the sample itself
            # (uptime, CPU deltas) since that is when the readings were taken.
            self._cache_ts = time.monotonic()
            return snapshot

    def _fresh_cached(self, now: float) -> Optional[ResourceSnapshot]:
        """The cached snapshot if it is younger than ``interval_s``, else None."""
        cached = self._cache
        if (
            cached is not None
            and self._cache_ts is not None
            and (now - self._cache_ts) < self._interval_s
        ):
            return cached
        return None

    async def _sample(self, now: float) -> ResourceSnapshot:
        """Walk the trees and assemble a fresh snapshot (the cache-miss path).

        The blocking process-tree walk is offloaded to ``subprocess_executor()``
        so the event loop is never held for the duration of the ``/proc`` reads
        (Req 8.3) — the same offload the runtime's own RSS probe uses. The host
        probes (posture, ``/proc/meminfo``, cgroup files) ride the SAME executor
        call, so the loop pays no filesystem read at all. Enumeration, identity
        resolution, dedupe and the gateway-self assembly stay on the loop; only
        the measurement crosses the executor boundary.

        Attribution order: runtimes are classified into chat/subagent/worker
        entries, the dedupe pass partitions their pid union so no pid lands in two
        entries (Req 8.4), and the gateway-self entry claims the gateway's own tree
        minus everything already attributed (Req 2.4). The gateway root is walked
        in the SAME executor batch as the runtimes so its ``/proc`` reads also stay
        off the loop.
        """
        runtimes = list(self._live_runtimes())
        root_pids = [pid for pid in (_runtime_pid(rt) for rt in runtimes) if pid is not None]

        # The gateway is walked alongside the runtimes so its tree read is
        # offloaded too; its pid joins the batch (deduped by _blocking_walk).
        gateway_pid = os.getpid()
        walks, probes = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), self._blocking_sample, [*root_pids, gateway_pid]
        )

        entries: list[EntrySample] = []
        for runtime in runtimes:
            pid = _runtime_pid(runtime)
            if pid is None:
                continue
            walk = walks.get(pid)
            if walk is None:
                # Root vanished between enumeration and the walk — omit it.
                continue
            entry = self._entry_for_runtime(runtime, walk, now)
            entries.append(self._classify(runtime, entry))

        # Partition the attributed pids (deterministic registry order wins), then
        # the gateway self-entry claims only what no runtime already did.
        entries = self._attribute(entries)
        attributed = set().union(*(e.pids for e in entries)) if entries else set()
        gateway_entry = self._gateway_entry(
            walks.get(gateway_pid),
            attributed,
            now,
            partition_complete=not any(e.truncated for e in entries),
        )
        if gateway_entry is not None:
            entries.append(gateway_entry)

        # Roots whose /proc tree was actually observed this pass — including the
        # gateway, whose CPU baseline the gateway-self entry also records. A
        # baseline for any other root is stale (its runtime exited) and is pruned
        # so the cache cannot leak and a recycled pid cannot delta against a dead
        # tree.
        self._prune_cpu_cache(set(walks))

        snapshot = ResourceSnapshot(
            entries=entries,
            captured_at=time.time(),
            interval_s=self._interval_s,
        )
        return self._host_context(snapshot, probes)


def _windows_tree_exceeds_cap(root_pid: int) -> bool:
    """True when *root_pid*'s naive Toolhelp tree has more than ``MAX_TREE_PIDS`` pids.

    The size probe for the Windows branch: one PID->PPID snapshot, walked with
    ``platform_compat``'s bounded descendant walk, which raises once ``limit``
    pids are seen -- so the probe's own working set is bounded too. The naive
    map over-counts (a recycled parent pid can attach an unrelated subtree),
    which is safe here: a false "too big" degrades one row to its root's RSS
    with the flag set; it never admits an oversized tree. An unreadable
    snapshot answers False and leaves the validated helper to do what it does
    on its own.
    """
    try:
        parent_map = platform_compat._windows_process_parent_map()
        # ``seen`` includes the root and the walk raises when it would grow past
        # ``limit``, so ``limit=MAX_TREE_PIDS`` fires exactly on the (cap+1)th pid.
        platform_compat._descendants_from_parent_map(root_pid, parent_map, limit=MAX_TREE_PIDS)
    except platform_compat._WindowsTreeOverflow:
        return True
    except Exception:
        return False
    return False


def _runtime_pid(runtime: Any) -> Optional[int]:
    """Read a runtime's root pid, tolerating either the public property or the
    private attribute. Returns ``None`` when unset or unreadable."""
    pid = getattr(runtime, "pid", None)
    return pid if isinstance(pid, int) else None


def _runtime_instance(runtime: Any) -> str:
    """A runtime's per-spawn ``process_instance`` id, ``""`` when it has none.

    Both runtime kinds mint one at spawn (``AcpRuntime`` and ``AcpClient``), for
    the same reason this reads it: a pid names a process only until the OS
    reuses it, and the instance id is what tells a successor apart.
    """
    inst = getattr(runtime, "process_instance", None)
    return inst if isinstance(inst, str) else ""


def _runtime_identity(runtime: Any) -> tuple[str, Optional[float]]:
    """Best-effort ``(agent, spawn_monotonic)`` for a runtime.

    Reads the public identity accessors task 1.2 adds when present, and falls
    back to the private attributes so this pass works before that wiring lands.
    The remaining identity (session key, subagent marker, dashboard slot) is the
    attribution pass's job (task 2.3), not this one's.
    """
    agent = getattr(runtime, "agent", None)
    if not isinstance(agent, str):
        agent = getattr(runtime, "_agent", None)
    agent_str = agent if isinstance(agent, str) else ""

    spawn = getattr(runtime, "spawn_monotonic", None)
    if not isinstance(spawn, (int, float)):
        spawn = getattr(runtime, "_spawn_monotonic", None)
    spawn_monotonic = float(spawn) if isinstance(spawn, (int, float)) else None

    return agent_str, spawn_monotonic


def _runtime_session_keys(runtime: Any) -> list[str]:
    """The Kiro Crew session keys that own sessions on a runtime, best-effort.

    Reads ``session_owners`` (``{acp_session_id: kiro_crew_session_key}``) and
    returns the non-empty owner keys in registration order, deduplicated — those
    are what ``dashboard_slot_key`` can resolve. The ACP ``sessionId`` values in
    ``session_keys`` are opaque protocol ids the dashboard cannot map, so they are
    consulted only when a runtime exposes no owner map at all (a stub in tests);
    a runtime that exposes owners but has none yet claimed (a warm-pool worker)
    yields an empty list and classifies as a worker, which is what it is.
    Anything unexpected degrades to an empty list.
    """
    owners = getattr(runtime, "session_owners", None)
    if isinstance(owners, dict):
        seen: list[str] = []
        for key in owners.values():
            if isinstance(key, str) and key and key not in seen:
                seen.append(key)
        return seen
    keys = getattr(runtime, "session_keys", None)
    if keys is None:
        queues = getattr(runtime, "_session_queues", None)
        keys = list(queues) if isinstance(queues, dict) else None
    if not isinstance(keys, (list, tuple)):
        return []
    return [k for k in keys if isinstance(k, str)]


def _subagent_fields(info: Any) -> tuple[str, str, str, str]:
    """Extract ``(subagent_id, task, parent_session_key, agent)`` from a subagent
    record, tolerating a missing record or absent fields.

    The record is duck-typed (a ``SubagentInfo`` in production, a stub in tests)
    so this module never imports ``subagent``. ``None`` or a record missing a
    field yields the empty string for that field — the entry stays a subagent but
    reads as unnamed rather than raising.
    """
    if info is None:
        return "", "", "", ""

    def _s(attr: str) -> str:
        value = getattr(info, attr, "")
        return value if isinstance(value, str) else ""

    return _s("id"), _s("task"), _s("parent_session_key"), _s("agent")


def provider_identity_for_session(session_key: str) -> Optional[tuple[Optional[int], str]]:
    """``(pid, process_instance)`` of the live runtime hosting *session_key*, or None.

    This is the identity a monitor row's Stop must be checked against. A row is
    a SAMPLE: between the snapshot that drew it and the operator's confirm (a
    dialog can sit open indefinitely) the slot may have moved on -- its
    conversation reset and a new turn started on a different runtime -- and a
    stop addressed by slot alone would then abort the replacement's work. The
    page therefore submits the pid AND instance it sampled and the stop route
    compares both to this answer, refusing when either differs: the pid alone is
    not enough because the OS reuses pids, so a successor can inherit its
    predecessor's pid, while the instance id is minted fresh per spawn. Read
    from the live registry (the same source the sampler enumerates), so the
    comparison is against what is running NOW, not the cached snapshot.
    """
    if not session_key:
        return None
    for runtime in runtime_registry.live_runtimes():
        if session_key in _runtime_session_keys(runtime):
            return (_runtime_pid(runtime), _runtime_instance(runtime))
    return None


# ── host context (task 2.4) ──────────────────────────────────────────────────
def _sampling_supported() -> bool:
    """Whether per-process sampling is possible on this platform.

    Linux (``/proc``), macOS and Windows all expose a per-process RSS mechanism
    the sampler already reuses, so they are supported — a gap in any single
    statistic is reported as a field-level ``None`` on the entry, not as
    whole-pass unavailability (Req 7.2). A platform with none of those
    mechanisms answers with host context only and this predicate ``False``
    (Req 7.3). Kept a module function (not an ``if`` inline in ``snapshot``) so a
    test can force the unsupported branch on any host.
    """
    return sys.platform in ("linux", "darwin", "win32")


#: ``(posture, available_gb, host_total_gb, cgroup_used_gb, cgroup_limit_gb)`` —
#: every host-context reading, taken together on the executor thread.
_HostProbes = tuple[str, float, Optional[float], Optional[float], Optional[float]]


def _read_host_probes() -> _HostProbes:
    """Every host-context reading in one call, for the executor thread.

    Each source degrades independently (see :meth:`ResourceSampler._host_context`
    for the per-field defaults); none of them raises. Kept as a single function so
    the event-loop side has exactly one blocking thing to offload.
    """
    posture, available_gb = _probe_host_posture()
    host_total_gb = _read_mem_total_gb()
    cgroup_used_gb, cgroup_limit_gb = _read_agents_cgroup_gb()
    return posture, available_gb, host_total_gb, cgroup_used_gb, cgroup_limit_gb


def _probe_host_posture() -> tuple[str, float]:
    """``(posture, available_gb)`` from ``resource_status.probe()`` (Req 3.1).

    The probe itself never raises — it returns an ``unknown`` posture on
    failure — but a broken probe stack still degrades to the model defaults
    rather than failing the whole snapshot. Kept as a module function so a test
    can stub it.
    """
    try:
        status = resource_status.probe()
        return status.posture, status.available_gb
    except Exception:  # pragma: no cover — probe stack broken degrades to unknown
        logger.debug("host posture probe failed", exc_info=True)
        return "unknown", -1.0


def _read_mem_total_gb() -> Optional[float]:
    """Host ``MemTotal`` in GB from ``/proc/meminfo``, or ``None`` when unreadable.

    The platform exposes total memory here only on Linux; off Linux (or on a
    read/parse failure) the figure is genuinely unknown and stays ``None`` rather
    than a fabricated zero (Req 3.2). ``MemTotal`` is reported in KiB.
    """
    if sys.platform != "linux":
        return None
    try:
        with open(_MEMINFO_PATH, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    kib = int(line.split()[1])
                    return kib / (1024**2)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_cgroup_int(path: str) -> Optional[int]:
    """Read a single integer from a cgroup file; ``None`` on absence/``max``/garbage.

    Mirrors ``subagent._read_int_file``: the cgroup v2 unlimited sentinel is the
    literal ``max``, which reads as ``None`` (no cap), and an unreadable or
    non-numeric file also reads ``None``.
    """
    try:
        with open(path, encoding="ascii") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_agents_cgroup_gb() -> tuple[Optional[float], Optional[float]]:
    """``(used_gb, limit_gb)`` for the gateway's agents-slice cgroup, else both None.

    Read from the same ``kirocrew-agents.slice`` directory the reaper and the
    pressure sampler use (``sandbox._agents_slice_cgroup_dir``), so the monitor
    reports the aggregate cgroup the agent trees actually run under. BOTH gauges
    are omitted (``None``) unless the slice is active AND its ``memory.max`` is a
    real number — an unconstrained host (or one off Linux, where the slice dir is
    ``None``) shows no permanent N/A bar (Req 3.3, KRS default). A limit at or
    above the unlimited sentinel is likewise treated as "no cap".

    Read defensively: a slice that vanishes mid-read degrades to "no gauge",
    never to a raised snapshot.
    """
    try:
        slice_dir = sandbox._agents_slice_cgroup_dir()
    except Exception:  # pragma: no cover — slice resolve failure omits gauge
        logger.debug("agents slice cgroup dir resolve failed", exc_info=True)
        return None, None
    if slice_dir is None:
        return None, None

    limit = _read_cgroup_int(str(slice_dir / "memory.max"))
    if limit is None or limit >= _CGROUP_UNLIMITED:
        # Unconstrained (or unreadable) limit — omit the whole gauge so the UI
        # never renders a ceiling that is not really a ceiling.
        return None, None
    used = _read_cgroup_int(str(slice_dir / "memory.current"))
    used_gb = None if used is None else used / _BYTES_PER_GB
    return used_gb, limit / _BYTES_PER_GB
