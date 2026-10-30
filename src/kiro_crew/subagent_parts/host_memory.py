"""Host memory readings and the sub-agent count ceiling they size.

``/proc/meminfo`` and the native macOS and Windows readers, the cgroup v1/v2 headroom of
the container and of the agents slice, process-subtree sampling for the reaper, and the
auto or explicit ceiling ``resolve_max_subagents`` derives from them.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from kiro_crew.constants import DEFAULT_SPAWN_MIN_MEMORY_GB

if TYPE_CHECKING:
    from kiro_crew.subagent import (
        DEFAULT_SUBAGENT_COST_GB,
        STUB_MODULE,
        KiroCrewConfig,
        Optional,
        Path,
        PurePosixPath,
        _agents_slice_cgroup_dir,
        logger,
        math,
        platform_compat,
        read_cap_costs,
    )


#: Cause recorded when a finite cgroup memory limit is set but that level's
#: usage file cannot be read: the headroom is unknown, not measured as low.
MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE = "cgroup_usage_unreadable"


#: Cause the spawn gate's off-loop read records when its worker did not answer
#: in time or no thread could be started: the host was not read at all, so the
#: start waits for a re-check instead of failing open or reading on the loop.
MEMORY_CAUSE_READ_UNANSWERED = "host_read_unanswered"


#: Set by the cgroup probe inside :func:`check_memory_available` and read by the
#: spawn gate with :func:`pop_memory_check_cause`. Thread-local, so a probe on
#: another thread (the sizing sampler, a session transfer) never writes the
#: cause a gate on this thread reads. Diagnostic only: the admission verdict
#: never depends on it.
_memory_check_cause = threading.local()


def pop_memory_check_cause() -> str:
    """Return this thread's last memory-check cause ("" for none) and clear it."""
    cause = getattr(_memory_check_cause, "value", "")
    _memory_check_cause.value = ""
    return cause


def check_memory_available(
    min_gb: float = DEFAULT_SPAWN_MIN_MEMORY_GB, *, path: str = "/proc/meminfo"
) -> tuple[bool, float]:
    """Check if enough memory is available to spawn a subagent.

    Reads /proc/meminfo MemAvailable with a plain ``open`` and compares
    against *min_gb*. The read deliberately does NOT go through
    ``hooks.safe_read_file``: that gate polices agent-supplied paths, and
    this path is a fixed module constant that no caller overrides in
    production, so the gate adds no protection here — while a gate refusal
    under load would silently disable spawn back-pressure exactly when it
    matters (the gate's refusal modes correlate with CPU contention).
    ``platform_compat._linux_available_mib`` reads the same file the same
    way. The ``path`` keyword is keyword-only and exists for tests only;
    production callers always take the constant.
    Native macOS/Windows readers handle the production path on those hosts:
    on both, the floor compares against ``platform_compat.host_available_mib``
    through ``_host_available_gb`` (see it for why macOS does not use the
    looser sum the auto cap and the posture read).
    Linux production reads also respect cgroup headroom. An explicit test path
    always exercises the file reader. With no readable host memory or finite
    cgroup limit, returns (True, -1.0).
    """
    if path == "/proc/meminfo" and not platform_compat.IS_LINUX:
        if platform_compat.IS_MACOS or platform_compat.IS_WINDOWS:
            avail = _host_available_gb()
        else:
            avail = -1.0
        return (True, -1.0) if avail < 0 else (avail >= min_gb, round(avail, 2))
    avail = -1.0
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for line in text.splitlines():
            if line.startswith("MemAvailable:"):
                kb = int(line.split()[1])
                avail = kb / (1024 * 1024)
                break
    except (OSError, ValueError, IndexError):
        # A failed host read cannot discard a known container constraint.
        pass
    if path == "/proc/meminfo":
        pop_memory_check_cause()  # the cgroup probe below records a fresh one
        cgroup_gb = _cgroup_available_gb()
        if cgroup_gb >= 0:
            avail = cgroup_gb if avail < 0 else min(avail, cgroup_gb)
    return (True, -1.0) if avail < 0 else (avail >= min_gb, round(avail, 2))


# Process-subtree readings come from ONE shared walker,
# :func:`platform_compat.proc_subtree_sample`. RSS, CPU and the two counts all
# come from that single walk, which lives above both this module and
# ``mcp_gateway.pool``, so the 256 ceiling and the sentinels cannot drift
# between separate copies.


def _proc_subtree_sample(pid: Optional[int], *, pss: bool = False) -> platform_compat.SubtreeSample:
    """One walk of *pid*'s subtree, carrying all four readings the sweep needs.

    Thin adapter over :func:`platform_compat.proc_subtree_sample` that supplies
    the needle this module counts by: ``STUB_MODULE``, the module path the
    rewriter itself puts on the stub launch line. So ``sample.matched`` is the
    stub count here, and the shared walker stays free of gateway vocabulary
    while this module stays free of a second walk. ``pss`` adds the settled
    capture's summed PSS to the same walk (costly; only while a capture is due).

    Blocking: reads a handful of ``/proc`` entries per process in the subtree, so
    it belongs on an executor thread, never on the event loop (see
    ``_reaper_loop`` -> ``_sample_live_costs``).
    """
    return platform_compat.proc_subtree_sample(pid, counts=True, needles=(STUB_MODULE,), pss=pss)


def _subtree_cpu_jiffies(pid: int, *, pids: Optional[list[int]] = None) -> int:
    """Sum utime+stime across ``pid`` and its descendants (clock ticks).

    Two routes, and the caller picks one by argument. WITHOUT ``pids`` it asks
    the shared walker for the CPU reading alone, so the subtree is the one the
    task rows describe and no ``status`` read is paid for an RSS figure the
    caller does not use.

    ``pids`` is that subtree when the caller has ALREADY walked it, so the tree
    is not enumerated a second time to total the same processes -- the same
    hand-over ``_get_rss_tree_mb`` takes, and for the same reason: nearly all of
    the cost is the enumeration, not the per-process read. It also makes the CPU
    figure describe exactly the set the caller's other figures describe, where
    two enumerations could disagree (the walker stops at ``_SUBTREE_MAX_PROCS``
    and a caller's own walk need not). This is the route the Sessions session
    rows take, so their CPU figure spans their whole uncapped tree -- the same
    set every other figure on those rows spans -- rather than the walker's first
    ``_SUBTREE_MAX_PROCS`` processes.
    """
    if pids is not None:
        return platform_compat.proc_cpu_jiffies_for_pids(pids)
    return platform_compat.proc_subtree_sample(pid, rss=False, counts=False).jiffies


def _attributed_count(total: Optional[int], sharers: int, previous: Optional[int]) -> Optional[int]:
    """One co-tenant's share of a subtree *total*, or *previous* if unmeasured.

    Counts follow the same per-sharer split as the RSS/CPU attribution (see
    ``SubagentManager._live_shared_count``) so every attributed column on a row
    describes the same fraction of the runtime. Two differences follow from a
    count being a whole number:

    * The quotient is rounded to the nearest whole process.
    * A nonzero total never rounds down to zero. "This runtime carries stubs,
      your share is 0" is the reading that would reproduce the original bug in a
      new form, so the floor is 1 whenever anything was counted.

    Passing *previous* through on an unmeasured sweep (``total is None`` — the
    pid died, or the platform has no ``/proc``) keeps the last good reading
    rather than blanking a column mid-run, matching how RSS only writes when its
    own read succeeded.

    NOTE on the divisor: ``_live_shared_count`` counts SUBAGENT tenants. A
    subagent shares its parent session's runtime whenever one is available
    (``_create_shared_session``), and the parent session is not a subagent, so
    with a parent co-tenant the divisor is a lower bound and a task's share is an
    upper bound. That is the divisor RSS and CPU have always used; unifying it is
    a separate change to numbers users already read, not a side effect of adding
    two columns.
    """
    if total is None:
        return previous
    if sharers <= 1 or total <= 0:
        return total
    return max(1, round(total / sharers))


# Legacy hard-coded concurrent cap; also the lower clamp bound for auto-sizing
# so dynamic sizing never regresses below today's behavior.
_LEGACY_DEFAULT_MAX = 3


def _available_memory_gb() -> float:
    """Effective available memory (GB), dispatched per operating system.

    Each OS reports "available" memory through a different, non-portable
    interface, so the probe is a small per-platform branch. Every branch
    returns a best-effort available-GB figure, or ``-1.0`` when this platform
    has no probe yet / the read failed — in which case
    ``compute_max_subagents`` falls back to the legacy default cap, and the
    TaskRunner's ``compute_memory_sized_parallel_cap`` to its floor.

        • Linux  — ``/proc/meminfo`` ``MemAvailable`` (via ``check_memory_available``),
                   then clamped by cgroup headroom so the tighter of a
                   container's limit and the agents slice's ceiling binds.
        • macOS  — reclaimable memory via Mach ``host_statistics64`` (ctypes,
                   in-process, no subprocess); see ``_macos_available_memory_gb``.
                   No cgroups.
        • Windows — ``GlobalMemoryStatusEx`` through
                   ``platform_compat.host_available_mib``. No cgroups.
        • other  — no probe yet → ``-1.0`` (fail open).

    NOTE (adding a new OS): implement a ``_<os>_available_memory_gb()`` helper
    returning GB or -1.0, add an ``IS_<OS>`` flag to ``platform_compat``, and
    wire one branch below. Keep the -1.0 fail-open contract so an unmeasurable
    host degrades to the safe legacy default rather than over-spawning.
    """
    if platform_compat.IS_LINUX:
        _ok, host_gb = check_memory_available(min_gb=0.0)
        if host_gb <= 0:
            return host_gb  # unreadable → caller fails open
        cg_gb = _cgroup_available_gb()
        if cg_gb < 0:
            return host_gb  # no cgroup cap (unconstrained)
        return min(host_gb, cg_gb)
    if platform_compat.IS_MACOS:
        return _macos_available_memory_gb()
    if platform_compat.IS_WINDOWS:
        return _host_available_gb()
    # Unsupported platform: no probe yet → fail open.
    return -1.0


def _host_available_gb() -> float:
    """``platform_compat.host_available_mib`` in GB, or ``-1.0`` when it cannot be read.

    Delegates instead of calling ``GlobalMemoryStatusEx`` or the Mach counters
    here. That shim is the single place the MiB unit and the "0 means
    unreadable, never zero memory" contract are defined, and a second reader
    would have to restate both to stay correct.

    Readers:

    * Windows, everywhere: without it the cap loses its memory term on Windows
      entirely and falls open to ``_LEGACY_DEFAULT_MAX``, so a host with tens of
      GB free is held to the same three concurrent sub-agents as an
      unmeasurable one.
    * macOS, the spawn floor only (``check_memory_available``). There the shim
      counts free plus purgeable pages plus ``min(inactive,
      external_page_count)``. The floor promises that
      ``agent.spawn_min_memory_gb`` stays free AFTER a start, so it reads what a
      new process can really take. The looser ``_macos_available_memory_gb``
      adds speculative pages a second time (``free_count`` already holds them)
      and counts dirty anonymous inactive pages as free; on a loaded Apple
      silicon host it reads about 3.5 GiB above this figure, so a floor compared
      against it admits starts with less headroom than the operator set. The
      auto cap's memory-sized fallback, the TaskRunner's memory-sized auto value
      and the memory posture keep reading ``_macos_available_memory_gb``.
    """
    available_mib = platform_compat.host_available_mib()
    if available_mib <= 0:
        return -1.0  # unreadable → caller fails open
    return available_mib / 1024.0


def _macos_vm_reclaimable_pages() -> Optional[int]:
    """Reclaimable memory in **pages** via Mach ``host_statistics64``, or ``None``.

    macOS-only; validated live against ``vm_stat`` on Apple silicon (matches
    within live-fluctuation noise). The Mach call itself lives in
    ``platform_compat.macos_vm_statistics``, so the kernel struct is declared in
    one place; what stays here is this caller's own composition of it.

    Reclaimable ≈ ``free + inactive + speculative + purgeable`` page classes:
    memory that can back a new allocation without swapping (the closest analogue
    to Linux ``MemAvailable``). Wired/active/compressed pages are excluded.
    Returns ``None`` on any failure (non-macOS, ``libSystem`` absent, non-zero
    ``kern_return_t``) so the caller falls back to the legacy default.

    This sum is knowingly looser than ``platform_compat.host_available_mib``,
    which bounds ``inactive`` by ``external_page_count`` and does not re-add
    ``speculative`` (``free_count`` already contains it). The spawn floor does
    not read it: it reads the tight figure (``_host_available_gb``).
    This sum feeds the TaskRunner's memory-sized auto value, the auto
    cap's disabled-floor fallback (``compute_memory_sized_parallel_cap``) and
    the memory posture, figures that are documented and that operators tune
    against.
    """
    probe = platform_compat.macos_vm_statistics()
    if probe is None:
        return None
    stats, _filled = probe
    return stats.free_count + stats.inactive_count + stats.speculative_count + stats.purgeable_count


def _macos_available_memory_gb() -> float:
    """macOS available-memory probe (GB), or ``-1.0`` on failure.

    Combines the in-process Mach reclaimable-page count
    (``_macos_vm_reclaimable_pages``) with the page size from ``os.sysconf``.
    macOS has no ``/proc/meminfo`` and ``os.sysconf`` exposes only *total*
    physical pages (no ``SC_AVPHYS_PAGES``), so the Mach VM statistics are the
    only cheap, non-blocking source of *available* memory — which the sizing
    formula needs so a memory-pressured Mac is not handed an inflated cap. Any
    read failure returns -1.0 so the caller falls back to the legacy default.
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return -1.0
    if page_size <= 0:
        return -1.0
    pages = _macos_vm_reclaimable_pages()
    if pages is None or pages <= 0:
        return -1.0
    avail_gb = pages * page_size / (1024**3)
    return round(avail_gb, 2) if avail_gb > 0 else -1.0


# Values at/above this are the kernel's "no limit" sentinel (PAGE_COUNTER_MAX).
_CGROUP_UNLIMITED = 1 << 62


def _read_int_file(path: str) -> int | None:
    """Read a single integer from *path*; None on absence/garbage. 'max' → None.

    Deliberately reads through ``kiro_crew.subagent``'s ``open`` so the sizing tests can
    fabricate every kernel input (membership, mounts, limits) by patching one
    name; the slice probe below shares it for the same reason.
    """
    try:
        with open(path, encoding="ascii") as fh:
            txt = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if txt == "max":  # cgroup v2 unlimited sentinel
        return None
    try:
        return int(txt)
    except ValueError:
        return None


#: Upper bound on one ``memory.stat`` read; the real file is a few KB.
_MEMORY_STAT_READ_CAP = 64 * 1024


def _read_reclaimable_file_bytes(directory: PurePosixPath | Path, v2: bool) -> int:
    """Clean page cache charged to a cgroup, in bytes; 0 when unreadable.

    A cgroup's ``memory.current`` (v1: ``memory.usage_in_bytes``) counts the
    page cache its members touched. On a build-heavy host that cache fills the
    group up to its ceiling and stays there, so ``limit - usage`` reads as zero
    headroom while almost all of it is cache the kernel drops on demand.
    Both file LRU lists are reclaimed before the kernel throttles at
    ``memory.high`` or OOM-kills at ``memory.max``: active pages are only
    demoted first. So the figure is ``active_file + inactive_file`` less the
    dirty and writeback pages, which must reach disk before they can go.
    Anonymous memory and ``shmem`` (tmpfs, which sits on the anon lists and
    cannot be dropped without swap) stay counted as used. Host ``MemAvailable``
    also counts most file cache as available, so the two probes roughly agree.

    v2 ``memory.stat`` is hierarchical. v1 keeps the subtree figures in the
    ``total_`` keys. Any unparseable value returns 0, so usage counts in full.
    Read through ``kiro_crew.subagent``'s ``open`` for the same reason as
    :func:`_read_int_file`.
    """
    # The file LRU lists, then the dirty and writeback pages on them.
    if v2:
        reclaimable = ("inactive_file", "active_file")
        unreclaimable = ("file_dirty", "file_writeback")
    else:
        reclaimable = ("total_inactive_file", "total_active_file")
        unreclaimable = ("total_dirty", "total_writeback")
    values: dict[str, int] = {}
    try:
        with open(str(directory / "memory.stat"), encoding="ascii") as fh:
            # A kernel-written file of ~40 short lines; the cap only keeps a
            # read on a fabricated or unexpected file bounded.
            text = fh.read(_MEMORY_STAT_READ_CAP)
        for line in text.splitlines():
            name, _, value = line.partition(" ")
            if name in reclaimable or name in unreclaimable:
                values[name] = max(0, int(value))
    except (OSError, UnicodeDecodeError, ValueError):
        return 0
    cache = sum(values.get(name, 0) for name in reclaimable)
    pinned = sum(values.get(name, 0) for name in unreclaimable)
    return max(0, cache - pinned)


def _working_set(directory: PurePosixPath | Path, v2: bool, usage: int) -> int:
    """*usage* minus the group's clean page cache, never below zero.

    The two files are read at different instants, so the cache figure can
    briefly exceed the usage figure; the floor keeps headroom from ever
    reading larger than the limit itself.
    """
    return max(0, usage - _read_reclaimable_file_bytes(directory, v2))


def _cgroup_memory_roots() -> list[tuple[PurePosixPath, PurePosixPath, bool]]:
    """Return (process directory, mount boundary, v2) for visible memory mounts."""
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            memberships = handle.read().splitlines()
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            mounts = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        memberships, mounts = [], []

    groups: dict[bool, PurePosixPath] = {}
    for line in memberships:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        if fields[0] == "0" and not fields[1]:
            v2 = True
        elif "memory" in fields[1].split(","):
            v2 = False
        else:
            continue
        membership = PurePosixPath(fields[2])
        if membership.is_absolute() and ".." not in membership.parts:
            groups[v2] = membership

    roots = []
    for line in mounts:
        before, separator, after = line.partition(" - ")
        fields, fs = before.split(), after.split()
        if not separator or len(fields) < 6 or len(fs) < 3:
            continue
        v2 = fs[0] == "cgroup2"
        if not v2 and not (fs[0] == "cgroup" and "memory" in fs[2].split(",")):
            continue
        group = groups.get(v2)
        if group is None:
            continue
        # mountinfo escapes whitespace and backslashes in its path fields.
        paths = [
            PurePosixPath(
                field.replace(r"\040", " ")
                .replace(r"\011", "\t")
                .replace(r"\012", "\n")
                .replace(r"\134", "\\")
            )
            for field in fields[3:5]
        ]
        root, mount = paths
        if not root.is_absolute() or not mount.is_absolute():
            continue
        try:
            relative = group.relative_to(root)
        except ValueError:
            continue  # This bind mount does not expose our cgroup.
        roots.append((mount / relative, mount, v2))

    if not roots:
        # Preserve the root-only probe on hosts without readable proc metadata.
        for directory, v2 in (("/sys/fs/cgroup", True), ("/sys/fs/cgroup/memory", False)):
            path = PurePosixPath(directory)
            roots.append((path, path, v2))
    return roots


def _cgroup_available_gb() -> float:
    """Cgroup memory headroom (GB) from whichever ceiling binds, or -1.0 if none.

    Two cgroups can bound the agents this host runs, and either may be the
    binding one:

    * the **process's own cgroup ancestry** (the container's limit when the
      gateway runs inside a memory-limited container) -- see
      :func:`_container_cgroup_available_gb`;
    * the **agents slice** (``kirocrew-agents.slice``), the aggregate ceiling
      the sandbox itself places on every agent process on a bare Linux host --
      see :func:`_agents_slice_available_gb`.

    The slice is a sibling of the gateway's own cgroup, not an ancestor, so the
    ancestry walk never sees it; and the walk reads hard limits only, never
    ``memory.high``, which is the ceiling the kernel throttles at. Without the
    slice term a bare host with tens of GB free reads as "ample" while agent
    process memory has already filled that ceiling, so admission keeps
    admitting into a throttle that reclaim cannot relieve. Both terms measure
    the working set, so a slice held at ``memory.high`` only by clean page
    cache is not refused; ``sandbox.agents_slice_throttling`` still reports
    that state to the cold-start handshake. The tighter of the two readings is
    returned; -1.0 only when neither constrains (``dynamic-subagent-sizing.md``
    §9).
    """
    readings = [
        gb for gb in (_container_cgroup_available_gb(), _agents_slice_available_gb()) if gb >= 0
    ]
    return min(readings) if readings else -1.0


def _container_cgroup_available_gb() -> float:
    """Tightest visible cgroup headroom (GB), or -1.0 if unlimited/unknown.

    Reads cgroup v2 (``memory.max``/``memory.current``) then v1
    (``memory.limit_in_bytes``/``memory.usage_in_bytes``) at the process's
    cgroup and its visible ancestors. Each limit is paired with usage at the
    SAME level, including siblings charged to a parent. A finite limit with
    unknown usage contributes zero headroom, never zero usage. Ancestors
    hidden above a mount cannot be measured. Usage excludes the level's
    clean page cache (:func:`_read_reclaimable_file_bytes`). An unknown usage
    records :data:`MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE` so a deferral can say
    the headroom is unknown rather than exhausted.
    """
    available = -1.0
    for leaf, mount, v2 in _cgroup_memory_roots():
        limit_name = "memory.max" if v2 else "memory.limit_in_bytes"
        usage_name = "memory.current" if v2 else "memory.usage_in_bytes"
        directory = leaf
        while True:
            # Older v1 kernels can disable descendant accounting per group.
            if (
                v2
                or directory == leaf
                or _read_int_file(str(directory / "memory.use_hierarchy")) == 1
            ):
                limit = _read_int_file(str(directory / limit_name))
                current = _read_int_file(str(directory / usage_name))
                if limit is not None and 0 <= limit < _CGROUP_UNLIMITED:
                    # No spare capacity is established when usage is unknown.
                    if current is not None and current >= 0:
                        headroom = max(
                            0.0, (limit - _working_set(directory, v2, current)) / (1024**3)
                        )
                    else:
                        headroom = 0.0
                        _memory_check_cause.value = MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE
                    available = headroom if available < 0 else min(available, headroom)
            if directory == mount:
                break
            directory = directory.parent
    return available


def _agents_slice_available_gb() -> float:
    """Headroom (GB) under the agents slice's own ceiling, or -1.0 if none applies.

    The slice carries two ceilings: ``memory.high`` (past it the kernel
    throttles-and-reclaims the whole subtree) and ``memory.max`` (past it the
    kernel OOM-kills a scope). The lower one binds, so headroom is
    ``min(high, max) - working set``, floored at zero: usage can sit ABOVE
    ``memory.high`` while the kernel reclaims, and a negative figure would
    mislead every threshold comparison downstream. The working set is
    ``memory.current`` minus clean page cache
    (:func:`_read_reclaimable_file_bytes`); without that, a slice whose cache has
    filled it to ``memory.high`` reads as zero headroom and raises a critical
    memory alert on a host with hundreds of GB available.

    The slice directory comes from ``sandbox._agents_slice_cgroup_dir`` (which
    knows systemd's dash-hierarchy); the files are read through the same
    ``_read_int_file`` as the container probe so ``max`` and an absent file
    both mean "does not constrain". -1.0 when not Linux, the slice is not
    materialized, or neither ceiling is set.
    """
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return -1.0
    ceilings = [
        limit
        for limit in (
            _read_int_file(str(slice_dir / "memory.high")),
            _read_int_file(str(slice_dir / "memory.max")),
        )
        if limit is not None and limit < _CGROUP_UNLIMITED
    ]
    if not ceilings:
        return -1.0
    current = _read_int_file(str(slice_dir / "memory.current")) or 0
    return max(0.0, (min(ceilings) - _working_set(slice_dir, True, current)) / (1024**3))


def _auto_ceiling(cfg: KiroCrewConfig) -> int:
    """``agent.subagent_auto_max``, never below 3: the auto cap's count ceiling."""
    try:
        return max(_LEGACY_DEFAULT_MAX, int(cfg.agent.subagent_auto_max))
    except (AttributeError, TypeError, ValueError):
        return _LEGACY_DEFAULT_MAX


def _configured_max_subagents(cfg: KiroCrewConfig) -> int:
    """``agent.max_subagents`` as an int; an unparseable value reads as the legacy 3."""
    try:
        return int(cfg.agent.max_subagents)
    except (AttributeError, TypeError, ValueError):
        return _LEGACY_DEFAULT_MAX


def subagent_count_ceiling(cfg: KiroCrewConfig) -> int:
    """The highest cap :func:`resolve_max_subagents` can return, from config alone.

    An explicit pin (floored at 3), else the auto ceiling. It never reads host
    memory, so the gateway boot path can size the MCP spawn gate's ceiling with
    it before the subagent manager exists; the gate only needs an upper bound,
    and the resolved cap (memory-dependent only in its fallbacks) never exceeds
    this figure.
    """
    configured = _configured_max_subagents(cfg)
    if configured > 0:
        return max(configured, _LEGACY_DEFAULT_MAX)
    return _auto_ceiling(cfg)


def compute_max_subagents(cfg: KiroCrewConfig) -> int:
    """The AUTO subagent cap (``agent.max_subagents == 0``): a high count ceiling.

    ``agent.subagent_auto_max`` (32 by default, never below 3) as written. It is
    NOT sized from host memory: memory is bounded per start by the spawn floor
    (``agent.spawn_min_memory_gb``), which prices every start at what it will
    settle at and queues the ones that would not leave the floor free. A count
    cap sized from the same memory on top of that floor only made a second
    chat's subagents wait for slots the first chat's wave held while memory was
    still free. The ceiling stands in for what the floor does not model -- the
    LLM provider's concurrency, and fd / PID limits -- so on any 16-32 GB host
    the floor is reached long before it.

    Where host memory cannot be read at all (no probe on this platform, or the
    read failed) the floor fails open, so the count is the only guard left and
    the cap falls back to the legacy 3 (``_LEGACY_DEFAULT_MAX``). A floor the
    operator disabled (``agent.spawn_min_memory_gb <= 0``) admits every start
    the same way, so there the count is sized from host memory instead
    (:func:`compute_memory_sized_parallel_cap`), the bound the floor would
    otherwise have provided.

    See ``dynamic-subagent-sizing.md``.
    """
    ceiling = _auto_ceiling(cfg)
    try:
        floor_off = float(cfg.agent.spawn_min_memory_gb) <= 0
    except (AttributeError, TypeError, ValueError):
        floor_off = False
    if floor_off:
        cap = compute_memory_sized_parallel_cap(cfg, settled=True)
        logger.info(
            "subagent auto cap = %d (agent.spawn_min_memory_gb disables the spawn "
            "floor, so the count is sized from host memory)",
            cap,
        )
        return cap
    if _available_memory_gb() <= 0:
        logger.info(
            "subagent auto cap = %d (memory unreadable, so the spawn floor cannot "
            "bound starts; fail-safe to the legacy default)",
            _LEGACY_DEFAULT_MAX,
        )
        return _LEGACY_DEFAULT_MAX
    logger.info("subagent auto cap = %d (agent.subagent_auto_max)", ceiling)
    return ceiling


def compute_memory_sized_parallel_cap(cfg: KiroCrewConfig, *, settled: bool = False) -> int:
    """A parallel-step count sized from host memory, for the TaskRunner's auto value.

    TaskRunner steps run on the runner lane, which no per-start memory floor
    prices, so their auto value (``taskrunner.max_parallel_steps == 0``) is the
    memory arithmetic the subagent cap does not use: a buffered memory
    budget divided by a per-agent memory cost, clamped to ``[3,
    agent.subagent_auto_max]``. CPU is deliberately not a term: over-committing
    memory ends in the OOM killer, while over-committing CPU only slows work
    down. Fails safe to the legacy 3 when memory cannot be read.

    ``settled`` prices from the settled runtime instead of the whole run (see
    :func:`_host_mem_term`): the subagent cap's disabled-floor fallback passes
    it, since it stands in for a floor that prices starts that way. The
    TaskRunner never does: nothing re-checks memory when a step starts, so its
    figure keeps the whole-run reserve for what its steps build and test.
    """
    agent = cfg.agent
    hard_cap = max(_LEGACY_DEFAULT_MAX, agent.subagent_auto_max)
    lo = _LEGACY_DEFAULT_MAX
    mem_term = _host_mem_term(cfg, settled=settled)
    if mem_term is None:
        return lo
    return max(lo, min(mem_term, hard_cap))


def _host_mem_term(cfg: KiroCrewConfig, *, settled: bool = False) -> int | None:
    """How many agents fit in this host's available memory, or None when unreadable.

    The one place the memory arithmetic lives, for
    :func:`compute_memory_sized_parallel_cap`. It sizes the subagent cap only
    when the spawn floor is disabled.

    ``floor((avail * buf - pool_size * typical - heavy_peak) / typical)``:
    every slot is priced at a typical agent (median across agents of each
    agent's p50), and the heaviest agent's p90 (``heavy_peak``) is reserved
    once. By default both come from the whole-run peak ``mem_gb``, so the
    reserve covers that agent's growth after it starts, which no per-start
    price models. With ``settled`` they come from ``settled_gb`` instead: the
    agent's own processes -- kiro-cli and the MCP servers up once its session
    answered, idle -- read from dedicated runs only and aged out after
    ``SETTLED_SAMPLE_MAX_AGE_SECS``, exactly the figure the spawn floor prices
    a start at. A run's builds and test suites are not in it, so one 132 GB
    whole-run sample does not hold that count at its floor; growth after start
    is left to the buffer, as the floor leaves it.
    """
    agent = cfg.agent
    avail_gb = _available_memory_gb()
    if avail_gb <= 0:
        return None
    buf = 1.0 - agent.subagent_mem_buffer_pct / 100.0
    if settled:
        from kiro_crew.subagent_cost import SETTLED_SAMPLE_MAX_AGE_SECS

        typical, heavy_peak = read_cap_costs(
            "settled_gb", dedicated_only=True, max_age_secs=SETTLED_SAMPLE_MAX_AGE_SECS
        )
    else:
        typical, heavy_peak = read_cap_costs("mem_gb")
    mem_cost = typical or agent.subagent_cost_gb or DEFAULT_SUBAGENT_COST_GB
    heavy_peak = heavy_peak or 0.0
    pool_size = cfg.session.pool_size
    return math.floor((avail_gb * buf - pool_size * mem_cost - heavy_peak) / mem_cost)


def resolve_max_subagents(cfg: KiroCrewConfig) -> int:
    """Resolve the subagent count ceiling: the explicit value when > 0, else auto.

    ``agent.max_subagents == 0`` is the "auto" sentinel: :func:`compute_max_subagents`,
    the high ``agent.subagent_auto_max`` ceiling that memory (the spawn floor)
    reaches first. Never 0, so every consumer -- the manager, the prompt's
    ``{{MAX_SUBAGENTS}}``, the spawn tool description -- has a defined figure.
    See ``dynamic-subagent-sizing.md``.
    """
    configured = _configured_max_subagents(cfg)
    if configured > 0:
        # An explicit pin below the legacy floor (1 or 2) would silently disable
        # auto-sizing AND run below today's default; floor it to 3. 0 stays the
        # auto sentinel. The config loader and dashboard API also enforce this;
        # defend here so a directly-constructed config can't drop the runtime cap
        # below the floor.
        return max(configured, _LEGACY_DEFAULT_MAX)
    return compute_max_subagents(cfg)


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
