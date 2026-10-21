"""Host-resource rows of ``kirocrew doctor``.

Memory-pressure preparedness, the live ACP runtimes and what their backends cost
in CPU, the sandbox's tmpfs roots, leftover kiro-cli installers, and what the
shared agents directory and the workspace root have accumulated. Every scan is
bounded and read-only: the doctor names what a sweep would reclaim and deletes
nothing.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import render

# Alias count above which the skill-view census warns. The projection publishes
# one ``kirocrew-skill-view-*.json`` per distinct agent view into the shared
# agents directory -- spawns that derive the same view share one file -- and
# kiro-cli reads EVERY file there on startup, so the count is a startup cost for
# every session on the host. A healthy host carries roughly authored agents x
# workspaces; the measured trouble starts past a couple of thousand -- about 8s
# of prune walk per spawn at 2,360 files, and ``EMFILE: too many open files``
# from kiro-cli at 15k. A boot drain clears a backlog at gateway start and the
# per-spawn reclaim covers steady-state orphans, so a count above this is either
# a backlog this gateway has not drained yet or one it cannot drain (another
# data home's aliases, an unreadable lease record); the warning tells which.
#
# This is the SAME number the projection enforces as a hard ceiling
# (:data:`kiro_crew.acp.skill_projection.SKILL_VIEW_PROJECTION_CEILING`): at or
# above it, preparation stops minting new views and falls back to the authored
# agent, so a host the doctor warns about is a host whose next spawn already fell
# back. Read through the ``cli_doctor`` facade, not imported by name here: this
# family binds no project module (test_cli_doctor_refactor_family_reads), and the
# facade holds the constant so the two can never drift.
_SKILL_VIEW_BACKLOG_WARN = cli_doctor.SKILL_VIEW_PROJECTION_CEILING


# Where SwapTotal is read from. A module attribute (not inlined) so tests can
# point it at a fabricated meminfo file.
_PROC_MEMINFO = Path("/proc/meminfo")


def _swap_total_kib() -> int | None:
    """``SwapTotal`` from ``/proc/meminfo`` in KiB, ``None`` when unreadable.

    Read from procfs directly rather than shelling out to ``free``/``swapon``:
    the file is world-readable and parsing it cannot hang or prompt.
    """
    try:
        text = _PROC_MEMINFO.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.startswith("SwapTotal:"):
            parts = line.split()
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def _gateway_lock_indeterminate() -> bool:
    """True when the gateway lock probe cannot say whether a gateway runs.

    The distinction :func:`kiro_crew.cli_perf._read_gateway_pid` deliberately
    collapses (it fails closed, since its caller must not profile the wrong
    process) but a report must keep: "nobody holds the lock" is a fact about
    the gateway, an indeterminate probe is a fact about the probe. Any other
    exception propagates to the caller's own "probe failed" line.
    """
    from kiro_crew import gateway_lock

    try:
        gateway_lock.lock_holder(cli_doctor.config_dir())
    except gateway_lock.LockProbeError:
        return True
    return False


def _gateway_memory_lines() -> list[str]:
    """The ``session ceiling`` and ``gateway rss`` lines of the Memory Pressure section.

    The ceiling is ``session.watchdog_rss_max_mb`` from the loaded config (``0``
    = disabled, called out as such because an operator reading this section is
    usually asking "what stops a runaway session tree?"). The RSS is read from
    the live gateway's pid via the lock-holder oracle ``cli_perf`` already uses,
    so a stale recorded pid can never be reported as the gateway's memory; no
    live gateway prints "not running", and a lock probe that cannot answer says
    so rather than reading as "not running". Every failure degrades to a line saying
    so — this is advisory and must never abort doctor.
    """
    lines: list[str] = []
    try:
        ceiling = int(cli_doctor.KiroCrewConfig.load().session.watchdog_rss_max_mb)
    except Exception:
        lines.append("  session ceiling: ⚠️  could not read session.watchdog_rss_max_mb")
    else:
        if ceiling > 0:
            lines.append(
                f"  session ceiling: ✅ {ceiling} MiB per session process tree "
                "(session.watchdog_rss_max_mb; idle sessions above it are recycled)"
            )
        else:
            lines.append(
                "  session ceiling: ⏹ disabled (session.watchdog_rss_max_mb = 0) — "
                "nothing bounds a runaway session tree"
            )
    try:
        pid = cli_doctor._read_gateway_pid()
        if pid is None and _gateway_lock_indeterminate():
            # ``_read_gateway_pid`` folds "the probe could not answer" into the
            # same None as "nobody holds the lock". On Windows a serving
            # gateway holds its lock file under a mandatory lock, so the pid
            # inside cannot be read and the probe is indeterminate -- printing
            # "not running" there contradicts the Connectivity row of the same
            # run. Say what is actually known instead.
            lines.append(
                "  gateway rss:     ⚠️  could not locate the gateway process to measure "
                "it (lock probe indeterminate; this does not mean it is stopped)"
            )
        elif pid is None:
            lines.append("  gateway rss:     ⏹ not running")
        else:
            rss = cli_doctor._gateway_rss_bytes(pid)
            if rss is None:
                lines.append(f"  gateway rss:     ⚠️  pid {pid} alive but RSS unreadable")
            else:
                lines.append(f"  gateway rss:     {rss // (1024 * 1024)} MiB (pid {pid})")
    except Exception:
        lines.append("  gateway rss:     ⚠️  could not determine (probe failed)")
    return lines


def _windows_inert_limit_lines() -> list[str]:
    """The ``inert limits`` line: ``resource_limits`` keys Windows accepts but ignores.

    Windows only; an empty list elsewhere. The key list is the one the startup
    warning uses (``sandbox_slice._WINDOWS_INERT_RESOURCE_LIMIT_KEYS``): every key
    whose only mechanism is a cgroup/systemd property or a POSIX rlimit. A key counts
    when set to a non-zero value, as at startup -- and ``max_open_files`` also when
    UNSET, because unset means the built-in 1024 default, which Windows never
    installs either. Advisory: never added to ``issues``.
    """
    if not cli_doctor.platform_compat.IS_WINDOWS:
        return []
    try:
        from kiro_crew.sandbox_slice import _WINDOWS_INERT_RESOURCE_LIMIT_KEYS

        limits = cli_doctor.KiroCrewConfig.load().resource_limits
    except Exception:
        return ["  inert limits:    ⚠  could not read resource_limits"]
    named = [
        f"resource_limits.{key}"
        for key in _WINDOWS_INERT_RESOURCE_LIMIT_KEYS
        if getattr(limits, key, None)
    ]
    if getattr(limits, "max_open_files", 0) is None:
        named.append("resource_limits.max_open_files (default 1024)")
    if not named:
        return ["  inert limits:    ✅ no resource_limits key set that Windows ignores"]
    return [
        "  inert limits:    ⚠  set but not enforced on Windows: " + ", ".join(named),
        "                   The Job object bounds only per-spawn max_processes and "
        "max_memory_mb; these need a systemd user manager or POSIX rlimits.",
    ]


def _doctor_memory_pressure(issues: list[str]) -> None:
    """Report whether the host can degrade gracefully under memory pressure.

    A Linux host with zero swap and no userspace OOM killer has no pressure
    release valve: sustained memory pressure evicts file-backed pages (running
    code included) faster than they re-fault in, and the host livelocks —
    unresponsive for minutes, sometimes until a power cycle — before the kernel
    OOM killer's conservative heuristics fire. Either protection alone (swap to
    absorb the spike, or earlyoom/systemd-oomd to kill a hog early) prevents
    the freeze, so this warns only when BOTH are absent. When detection is
    inconclusive it reports "unknown" instead of warning.

    Advisory only (never appended to ``issues``): swap sizing and OOM-killer
    policy are host configuration the user owns — doctor reports the exposure,
    it does not fail the install over it. Linux-only: the freeze mode and both
    detection sources are Linux-specific.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nMemory Pressure")
    # What is bounding memory right now, on every platform: the gateway's own
    # resident set and the per-session tree ceiling the cleanup watchdog
    # recycles at. Printed before the Linux-only freeze check so a Windows or
    # macOS operator still sees the numbers that matter for a runaway tree.
    for line in _gateway_memory_lines():
        print(line)
    for line in _windows_inert_limit_lines():
        print(line)
    if not sys.platform.startswith("linux"):
        print(
            f"  freeze risk: ⏹ not applicable ({sys.platform} — the swap/OOM-killer "
            "check reads Linux procfs)"
        )
        return

    swap_kib = _swap_total_kib()
    if swap_kib is None:
        print("  swap:        ⚠️  could not read SwapTotal from /proc/meminfo — check skipped")
        return
    if swap_kib > 0:
        print(f"  swap:        ✅ {swap_kib / 1048576:.1f} GiB configured")
    else:
        print("  swap:        ⏹ none (SwapTotal = 0)")

    killer = cli_doctor._detect_userspace_oom_killer()
    if isinstance(killer, str):
        print(f"  oom killer:  ✅ {killer} active")
    elif killer is False:
        print(
            "  oom killer:  ⏹ none active (checked: "
            + ", ".join(cli_doctor._OOM_KILLER_UNITS)
            + ")"
        )
    else:
        print("  oom killer:  ⏹ could not determine (no systemctl, or the probe failed)")

    if swap_kib > 0 or isinstance(killer, str):
        return
    if killer is None:
        # Uncertain detection must not warn — a container or non-systemd host
        # may run a killer doctor cannot see.
        print("  freeze risk: ⏹ unknown — no swap, and OOM-killer detection was inconclusive")
        return
    print("  freeze risk: ⚠️  host can freeze under sustained memory pressure")
    print("               With no swap and no userspace OOM killer, memory pressure")
    print("               thrashes file-backed pages and the host can livelock before")
    print("               the kernel OOM killer intervenes.")
    print("               Fix: add swap, enable systemd-oomd, or install earlyoom.")


# ── ACP runtimes (live count and backend CPU; Linux procfs) ──────────────────
# A runtime is what one ACP spawn leaves running: the root the gateway recorded
# in its session registry (a sandbox launcher, or the harness itself when nothing
# wraps it) and the harness processes under it. Its BACKEND is that root plus
# every process on a path from it down to a harness process -- the launcher and
# the ``kiro-cli``/``kiro-cli-chat`` pair -- and nothing else. Tool commands and
# MCP servers hang below the harness and are left out, so a session running a
# build is not mistaken for a spinning backend.

#: Seconds between the census's two CPU reads. CPU time is a cumulative tick
#: count, so one read is not a rate; across one second a clock tick (1/100 s on a
#: typical host) is about one percent of a core, the same floor the process
#: view's rate sampler holds for the same reason.
_ACP_CPU_WINDOW_SECS = 1.0

#: Share of one core at or above which a runtime's backend reads as hot. A
#: backend waiting between turns costs close to nothing and one streaming a turn
#: a few percent, while the runaway this row is for held about a third of a core
#: per launcher with no turn in flight, so a quarter of a core sits clear of both.
_ACP_HOT_CPU_PCT = 25.0

#: Most hot runtimes, and most unowned pids, the section names one by one.
_ACP_LISTED = 5

#: Most processes the census reads from the host's process table, and most one
#: runtime's tree walk keeps. The two bound different populations (the whole
#: host, one runtime's descendants). Every field either structure retains is a
#: fixed-width number the kernel wrote -- ``comm`` is never read -- so the count
#: is what bounds them, and past either cap the overflow is counted and the
#: section says so rather than reading as a smaller host.
_ACP_TABLE_CAP = 65_536
_ACP_TREE_CAP = 4096

_PROC_ROOT = Path("/proc")


class _AcpRuntime(NamedTuple):
    """One live runtime as the census found it.

    ``owner`` is ``gateway`` (the running gateway recorded it, or spawned it and
    is still its ancestor), ``other`` (another live process recorded it and is
    still its ancestor -- a ``kirocrew chat`` records its own runtime under its
    own pid), ``stale`` (whoever recorded it is not its ancestor, so it is gone
    or the runtime was orphaned), ``untracked`` (this install spawned it, no
    registry names it and the running gateway is not its ancestor) or
    ``unknown`` (the gateway lock could not say which gateway is running).
    ``cpu_pct`` is the backend's share of one core over the window, ``None`` when
    no rate could be taken.
    """

    root: int
    owner: str
    cpu_pct: float | None
    age_secs: float | None
    #: Resident memory of the runtime's whole process tree in bytes, ``None`` when
    #: the page size is unavailable or the tree census failed. Used by
    #: :func:`_acp_runtime_lines` to report total unowned memory pressure.
    rss_bytes: int | None = None


class _AcpCensus(NamedTuple):
    """A finished census and what its caps left unread.

    ``unread`` counts processes past :data:`_ACP_TABLE_CAP` in the host table;
    ``capped_trees`` counts runtimes whose tree held more than
    :data:`_ACP_TREE_CAP` processes and ``capped_procs`` the processes those
    walks refused. Any of them non-zero is printed, so a truncated read never
    passes for a complete one.
    """

    runtimes: list[_AcpRuntime]
    unread: int = 0
    capped_trees: int = 0
    capped_procs: int = 0


def _proc_table(proc_root: Path) -> tuple[dict[int, Any], int] | None:
    """``({pid: ProcStat}, unread)`` for the processes with a readable parent and start.

    At most :data:`_ACP_TABLE_CAP` entries are kept; ``unread`` counts the pids
    past it. ``None`` when the process table cannot be listed at all, which the
    section reports as a skipped check rather than as a host with no runtimes.
    """
    try:
        # In pid order, so a capped read and every child list it builds are the
        # same on each run rather than whatever order the directory listed.
        pids = sorted(int(entry.name) for entry in proc_root.iterdir() if entry.name.isdigit())
    except OSError:
        return None
    table: dict[int, Any] = {}
    unread = 0
    for pid in pids:
        if len(table) >= _ACP_TABLE_CAP:
            unread += 1
            continue
        stat = cli_doctor.platform_compat.read_proc_stat(pid, proc_root=proc_root)
        if stat is not None and stat.ppid is not None and stat.start_ticks is not None:
            table[pid] = stat
    return table, unread


def _subtree(root: int, children: Mapping[int, list[int]]) -> tuple[dict[int, int], int]:
    """``({pid: parent}, refused)`` for *root*'s tree, *root* mapped to 0.

    The cap is checked at every insertion, so the map never holds more than
    :data:`_ACP_TREE_CAP` entries; ``refused`` counts the descendants it turned
    away, and none of them is walked further.
    """
    parents = {root: 0}
    refused = 0
    frontier = [root]
    while frontier:
        reached: list[int] = []
        for pid in frontier:
            for child in children.get(pid, ()):
                if child in parents:
                    continue
                if len(parents) >= _ACP_TREE_CAP:
                    refused += 1
                    continue
                parents[child] = pid
                reached.append(child)
        frontier = reached
    return parents, refused


def _reaches(pid: int, targets: set[int], table: Mapping[int, Any]) -> bool:
    """Whether *pid* or one of its ancestors in *table* is in *targets*.

    Walks parent edges, so a process a capped tree walk refused is still found
    inside the runtime it belongs to.
    """
    seen: set[int] = set()
    cursor = pid
    while cursor > 1 and cursor not in seen:
        if cursor in targets:
            return True
        seen.add(cursor)
        stat = table.get(cursor)
        if stat is None:
            return False
        cursor = stat.ppid
    return False


def _backend_chain(tree: Mapping[int, int], harness: set[int], root: int) -> set[int]:
    """*root* plus each path from it down through wrappers into a run of harnesses.

    A path qualifies when its non-harness processes all sit ABOVE its first
    harness: the launcher above ``kiro-cli`` above ``kiro-cli-chat``. A
    ``kiro-cli`` that a tool command runs below the harness is the tool's, and
    pulling it in would bring the tool command's CPU with it.
    """
    chain = {root}
    for pid in tree:
        if pid not in harness or pid in chain:
            continue
        path = [pid]
        while path[-1] != root:
            path.append(tree[path[-1]])
        in_harness = False
        for node in reversed(path):
            if node in harness:
                in_harness = True
            elif in_harness:
                break
        else:
            chain.update(path)
    return chain


def _acp_runtime_census(
    *,
    tracked: Mapping[int, tuple[int, str | None]],
    tracked_pids: set[int],
    gateway_pid: int | None,
    ownership_known: bool,
    own_home: str,
    proc_root: Path = _PROC_ROOT,
    window_secs: float = _ACP_CPU_WINDOW_SECS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    clk_tck: int | None = None,
    age_of: Callable[[int], float | None] | None = None,
) -> _AcpCensus | None:
    """Every live ACP runtime on this host, with its backend's CPU over a window.

    Read-only, and only ``/proc``: two reads of each backend's ``stat``, one
    ``cmdline`` per process to recognise a harness, and the environment of a
    harness no registry root covers, to learn whether this install spawned it.

    *tracked* is the session registry (see
    :func:`kiro_crew.session_pid.tracked_session_roots`). A recorded root is
    counted only while a process with that pid AND the recorded start identity is
    alive, so a recycled pid is never reported as a runtime.

    A harness outside every recorded root is counted only when the reaper's own
    report calls it untracked (:func:`kiro_crew.session_pid.
    _is_untracked_managed_agent_orphan`, against *tracked_pids*, both pid files)
    AND it names this data home -- the pairing the leaked-runtime reclaim makes,
    because the reaper's predicate is uid-wide and a pod or a second install on
    this account is not ours to report. One that still hangs off the running
    gateway is that gateway's, whatever the registry says. Harness recognition is
    the reaper's :func:`~kiro_crew.session_pid._cmdline_names_a_harness`.

    ``None`` when the process table cannot be listed.
    """
    sp = cli_doctor.session_pid
    first_at = clock()
    read = _proc_table(proc_root)
    if read is None:
        return None
    first, unread = read
    children: dict[int, list[int]] = {}
    for pid, stat in first.items():
        children.setdefault(stat.ppid, []).append(pid)
    harness = {pid for pid in first if sp._cmdline_names_a_harness(sp._pid_cmdline(pid, proc_root))}

    found: list[tuple[int, str, set[int], frozenset[int]]] = []
    claimed: set[int] = set()
    capped_trees = capped_procs = 0

    def _claim(root: int, owner: str) -> None:
        nonlocal capped_trees, capped_procs
        tree, refused = _subtree(root, children)
        if refused:
            capped_trees += 1
            capped_procs += refused
        claimed.update(tree)
        found.append((root, owner, _backend_chain(tree, harness, root), frozenset(tree)))

    for root, (recorder, token) in sorted(tracked.items()):
        stat = first.get(root)
        if stat is None or _reaches(root, claimed, first):
            continue
        if token is not None and token != str(stat.start_ticks):
            continue  # the recorded runtime is gone and its pid was handed on
        if not ownership_known:
            _claim(root, "unknown")
        elif recorder == gateway_pid:
            _claim(root, "gateway")
        else:
            # A recorder that is still an ancestor is alive and still holds the
            # runtime; asking the tree rather than whether the pid exists also
            # keeps a recycled recorder pid from reading as an owner.
            _claim(root, "other" if _reaches(root, {recorder}, first) else "stale")
    for pid in sorted(harness):
        if first[pid].ppid in harness or _reaches(pid, claimed, first):
            continue  # not the top of a harness chain, or inside a counted runtime
        if not sp._is_untracked_managed_agent_orphan(
            pid, sp._pid_cmdline(pid, proc_root), tracked_pids
        ):
            continue
        if sp._env_spawn_home(pid, proc_root) != own_home:
            continue
        root = pid
        parent = first[pid].ppid
        if parent in first and sp._SANDBOX_LAUNCHER_MARKER in sp._pid_cmdline(parent, proc_root):
            root = parent  # its own sandbox launcher is part of its backend
        if not ownership_known:
            owner = "unknown"
        elif gateway_pid is not None and _reaches(root, {gateway_pid}, first):
            owner = "gateway"
        else:
            owner = "untracked"
        _claim(root, owner)

    if clk_tck is None:
        try:
            clk_tck = int(os.sysconf("SC_CLK_TCK"))
        except (AttributeError, OSError, ValueError):
            clk_tck = 0
    page_size: int = 0
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        pass
    if age_of is None:
        age_of = cli_doctor.platform_compat.process_age_secs
    sleep(max(0.0, window_secs - (clock() - first_at)))
    elapsed = clock() - first_at
    runtimes: list[_AcpRuntime] = []
    for root, owner, chain, tree_pids in found:
        cpu_pct: float | None = None
        if clk_tck > 0 and elapsed > 0:
            ticks = 0
            for pid in chain:
                before = first[pid]
                after = cli_doctor.platform_compat.read_proc_stat(pid, proc_root=proc_root)
                if after is None or after.start_ticks != before.start_ticks:
                    continue  # exited or replaced inside the window: no delta to take
                if after.cpu_ticks is None or before.cpu_ticks is None:
                    continue
                ticks += max(0, after.cpu_ticks - before.cpu_ticks)
            cpu_pct = 100.0 * ticks / clk_tck / elapsed
        rss_bytes: int | None = None
        if page_size > 0:
            try:
                total_pages = sum(
                    first[p].rss_pages
                    for p in tree_pids
                    if p in first and first[p].rss_pages is not None
                )
                if total_pages > 0:
                    rss_bytes = total_pages * page_size
            except Exception:
                pass
        runtimes.append(
            _AcpRuntime(root, owner, cpu_pct, age_of(first[root].start_ticks), rss_bytes)
        )
    return _AcpCensus(runtimes, unread, capped_trees, capped_procs)


def _acp_runtime_lines(census: _AcpCensus, window_secs: float) -> list[str]:
    """The section's rows for a finished census. Pure, so every shape is testable.

    Names processes by pid, share of a core and age only: a command line can
    carry a token, so none is ever printed.
    """
    runtimes = census.runtimes
    caps = _acp_cap_lines(census)
    if not runtimes:
        return ["  runtimes:    ✅ none running", *caps]
    owned = sum(1 for r in runtimes if r.owner == "gateway")
    other = sum(1 for r in runtimes if r.owner == "other")
    unowned = [r for r in runtimes if r.owner in ("stale", "untracked")]
    unknown = sum(1 for r in runtimes if r.owner == "unknown")
    parts = []
    if owned:
        parts.append(f"{owned} owned by the running gateway")
    if other:
        parts.append(f"{other} owned by another live process (a kirocrew chat)")
    if unowned:
        parts.append(f"{len(unowned)} no running gateway owns")
    if unknown:
        parts.append(f"{unknown} whose owner is unknown (the gateway lock probe could not answer)")
    lines = [f"  runtimes:    {len(runtimes)} alive: " + ", ".join(parts)]

    rated = [r for r in runtimes if r.cpu_pct is not None]
    if not rated:
        lines.append("  cpu:         ⏹ could not take a rate (no clock tick rate on this host)")
    else:
        total = sum(r.cpu_pct or 0.0 for r in rated)
        hot = sorted(
            (r for r in rated if (r.cpu_pct or 0.0) >= _ACP_HOT_CPU_PCT),
            key=lambda r: r.cpu_pct or 0.0,
            reverse=True,
        )
        overall = f"all {len(rated)} backends: {total:.0f}% of a core over {window_secs:.1f}s"
        if not hot:
            lines.append(
                f"  cpu:         ✅ none at or above {_ACP_HOT_CPU_PCT:.0f}% of a core ({overall})"
            )
        else:
            lines.append(
                f"  cpu:         ⚠️  {len(hot)} at or above {_ACP_HOT_CPU_PCT:.0f}% of a core "
                f"({overall})"
            )
            for runtime in hot[:_ACP_LISTED]:
                age = "" if runtime.age_secs is None else f", up {runtime.age_secs / 3600:.1f}h"
                whose = {
                    "gateway": "owned by the running gateway",
                    "other": "owned by another live process",
                    "unknown": "owner unknown",
                }.get(runtime.owner, "no running gateway owns it")
                lines.append(
                    f"               pid {runtime.root}: {runtime.cpu_pct or 0.0:.0f}%{age}, {whose}"
                )
            if len(hot) > _ACP_LISTED:
                lines.append(f"               … and {len(hot) - _ACP_LISTED} more")
            lines.append("               A backend waiting between turns costs almost nothing,")
            lines.append("               so one this busy is working a turn or spinning. Doctor")
            lines.append("               cannot see which: check the session before ending it.")
    if unowned:
        pids = ", ".join(str(r.root) for r in unowned[:_ACP_LISTED])
        more = f" and {len(unowned) - _ACP_LISTED} more" if len(unowned) > _ACP_LISTED else ""
        lines.append(
            f"  no owner:    ⚠️  {len(unowned)} no running gateway owns, so no turn can be "
            "in flight on them"
        )
        # Only untracked runtimes (no registry record) are reclaimed by Services >
        # Leaked agent runtimes; stale runtimes have a record and are handled separately.
        untracked_rss = sum(
            r.rss_bytes for r in unowned if r.owner == "untracked" and r.rss_bytes is not None
        )
        if untracked_rss > 0:
            lines.append(
                f"               ~{untracked_rss // (1024 * 1024)} MiB (untracked);"
                " reclaim from Services > Leaked agent runtimes in the dashboard"
            )
        lines.append(f"               pids {pids}{more}")
    return lines + caps


def _acp_cap_lines(census: _AcpCensus) -> list[str]:
    """The rows that say a cap cut the read short, empty when nothing was cut."""
    lines = []
    if census.unread:
        lines.append(
            f"  read cap:    ⚠️  {census.unread} processes past the first {_ACP_TABLE_CAP} were "
            "not read; a runtime among them is not counted"
        )
    if census.capped_trees:
        lines.append(
            f"  tree cap:    ⚠️  {census.capped_trees} runtime(s) hold more than {_ACP_TREE_CAP} "
            f"processes; {census.capped_procs} past the cap were not walked"
        )
    return lines


def _doctor_acp_runtimes(issues: list[str]) -> None:
    """How many ACP runtimes are alive, who owns them, and what their backends cost.

    A runtime that outlives its session can keep a core busy with nothing to show
    for it, and enough of them take the whole host down; this row names them in
    the ordinary health pass instead of leaving them to a raw ``ps``.

    Advisory only (never appended to ``issues``), and every failure degrades to a
    line saying the check was skipped: an unreadable process table is a fact
    about where doctor ran, not a fault in the install. Linux-only, because the
    census reads procfs.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nACP Runtimes")
    if not sys.platform.startswith("linux"):
        print(f"  runtimes:    ⏹ not measured ({sys.platform} — the census reads Linux procfs)")
        return
    try:
        tracked = cli_doctor.session_pid.tracked_session_roots()
        if tracked is None:
            print("  runtimes:    ⚠️  could not read the session registry — check skipped")
            return
        gateway_pid = cli_doctor._read_gateway_pid()
        census = _acp_runtime_census(
            tracked=tracked,
            tracked_pids=cli_doctor.session_pid._tracked_agent_pids(),
            gateway_pid=gateway_pid,
            ownership_known=gateway_pid is not None or not _gateway_lock_indeterminate(),
            own_home=str(cli_doctor.data_home()),
            proc_root=_PROC_ROOT,
            window_secs=_ACP_CPU_WINDOW_SECS,
        )
    except Exception:
        print("  runtimes:    ⚠️  could not take the census (probe failed) — check skipped")
        return
    if census is None:
        print("  runtimes:    ⚠️  could not read the process table — check skipped")
        return
    for line in _acp_runtime_lines(census, _ACP_CPU_WINDOW_SECS):
        print(line)


# ── Task ceilings (agents slice TasksMax, per-scope TasksMax, thread pools) ──
# Most scope directories read under the agents slice. A host runs a few dozen;
# the cap keeps a leak of thousands from turning doctor into a long walk.
_TASK_SCOPE_SCAN_CAP = 2048


class _ScopeTasks(NamedTuple):
    """Task counts of the live agent scopes, read from the cgroup tree."""

    scopes: int
    total: int
    largest: int
    near_limit: int


def _read_scope_tasks(slice_dir: Path, tight_ratio: float) -> _ScopeTasks:
    """Every ``*.scope`` up to two levels below *slice_dir*, bounded and non-raising.

    A scope counts as near its limit at *tight_ratio* of its ``pids.max``, the
    band ``resource_status`` applies to the slice.
    """
    count = total = largest = near = 0
    candidates: list[Path] = []
    try:
        for child in slice_dir.iterdir():
            if child.name.endswith(".scope"):
                candidates.append(child)
            elif child.name.endswith(".slice") and child.is_dir():
                candidates.extend(p for p in child.iterdir() if p.name.endswith(".scope"))
            if len(candidates) >= _TASK_SCOPE_SCAN_CAP:
                break
    except OSError:
        pass
    for scope in candidates[:_TASK_SCOPE_SCAN_CAP]:
        current = cli_doctor.sandbox.read_cgroup_int(scope / "pids.current")
        if current is None:
            continue
        limit = cli_doctor.sandbox.read_cgroup_int(scope / "pids.max")
        count += 1
        total += current
        largest = max(largest, current)
        if limit and current >= limit * tight_ratio:
            near += 1
    return _ScopeTasks(count, total, largest, near)


def _task_ceiling_lines() -> list[str]:
    """The Task ceilings rows."""
    sizing = cli_doctor.host_task_sizing
    facts = sizing.host_task_facts()
    rl = cli_doctor.KiroCrewConfig.load().resource_limits
    lines = [f"  host:        {facts.describe()}"]

    slice_mem = rl.max_total_memory_mb if (rl.max_total_memory_mb or 0) >= 1 else None
    recommended_slice = sizing.size_slice_tasks(facts, slice_mem)
    configured_slice = rl.max_total_processes if (rl.max_total_processes or 0) >= 1 else None
    want_slice = configured_slice or recommended_slice
    source = "config" if configured_slice else "host-sized"
    slice_line = f"  slice:       TasksMax {want_slice} ({source})"
    if configured_slice:
        slice_line += f", the host-sized value would be {recommended_slice}"
    lines.append(slice_line)

    # The live reading and its tight band come from resource_status, the one
    # reader of the slice's task count, so doctor and the resource_status tool
    # cannot disagree about how full the slice is.
    from kiro_crew import resource_status

    status = resource_status.probe()
    slice_dir = cli_doctor.sandbox._agents_slice_cgroup_dir()
    scopes = (
        _read_scope_tasks(slice_dir, resource_status._SLICE_TASKS_TIGHT_RATIO)
        if slice_dir is not None
        else None
    )
    live_text = status.slice_tasks_text()
    if not live_text:
        lines.append("  live:        ⏹ the agents slice is not active; nothing measured")
    else:
        current, live_max = status.slice_tasks, status.slice_tasks_limit
        lines.append(f"  live:        {live_text}")
        if 0 < live_max < want_slice:
            lines.append(
                f"               ⚠️  the slice's pids.max {live_max} is below this install's "
                f"{want_slice}: another gateway or pod wrote it after this one. "
                "Restarting this gateway writes its value again."
            )
        if status.slice_tasks_tight:
            lines.append(
                "               ⚠️  close to the ceiling: new sessions fail to start "
                "(process exited) once it is reached"
            )
        if scopes is not None and scopes.scopes:
            mean = scopes.total / scopes.scopes
            lines.append(
                f"  runtimes:    {scopes.scopes} scopes, {mean:.0f} tasks each on average, "
                f"largest {scopes.largest}"
            )
            if live_max > 0:
                room = int((live_max - current) / mean)
                note = f"  headroom:    about {room} more runtimes at the observed average"
                if room < scopes.scopes:
                    note += " ⚠️  fewer than are running now"
                    if want_slice < recommended_slice:
                        note += (
                            f"; raise resource_limits.max_total_processes toward "
                            f"{recommended_slice} or unset it"
                        )
                lines.append(note)

    configured_scope = rl.max_processes if (rl.max_processes or 0) >= 1 else None
    scope_value = configured_scope or cli_doctor.sandbox._CGROUP_DEFAULT_MAX_PROCESSES
    lines.append(
        f"  per scope:   TasksMax {scope_value} ({'config' if configured_scope else 'default'})"
    )
    if scopes is not None and scopes.near_limit:
        lines.append(
            f"               ⚠️  {scopes.near_limit} scope(s) in the tight band of "
            "their own TasksMax"
        )

    # What config decides, in spawn order: agent.env is laid over the inherited
    # environment, and child_env_defaults and the host default only fill a name
    # still unset. The gateway's own environment is not read back: it can change
    # in-process after exec (the data home's .env loads at start-up), so a
    # procfs reading would be stale; the note below says it wins instead.
    agent_cfg = cli_doctor.KiroCrewConfig.load().agent
    overlay = agent_cfg.env
    configured_env = agent_cfg.child_env_defaults
    host_env = sizing.thread_pool_env_defaults(facts)
    for name in sizing.THREAD_POOL_ENV:
        if name in overlay:
            value, origin = overlay[name], "agent.env"
        elif name in configured_env:
            value, origin = configured_env[name], "agent.child_env_defaults"
        elif name in host_env:
            value, origin = host_env[name], "host-sized"
        else:
            value, origin = "unset", "kiro-cli uses one worker per core"
        lines.append(f"  {name}: {value} ({origin})")
    lines.append(
        "               a value already in the gateway's environment (its unit or "
        "the data home's .env) wins over every source but agent.env"
    )
    return lines


def _doctor_task_ceilings(issues: list[str]) -> None:
    """Report the task ceilings in force against the host and the live agent slice.

    The kernel counts threads against ``TasksMax``, so a ceiling that fits a
    laptop fills on a many-core host where every runtime sizes its pools by core
    count; the symptom is a session dying with ``process exited (rc=None)``.
    This row prints the host facts, the value in force for the slice, each
    scope and the thread-pool variables with where each came from, the live
    usage and an estimate of how many more runtimes fit, with a warning line
    when the slice is close to its ceiling or holds a lower one than this
    install's. Advisory only (never appended to ``issues``), like the ACP
    runtime census: the agents slice is shared by every gateway under this
    user, so its live state is a fact about the host, not a fault in this
    install. Linux only: the ceilings are cgroup v2 properties.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    if not sys.platform.startswith("linux"):
        return
    print("\nTask ceilings")
    try:
        lines = _task_ceiling_lines()
    except Exception:
        print("  ceilings:    ⚠️  could not read the host or the agents slice — check skipped")
        return
    for line in lines:
        print(line)


# ── Runtime tmpfs headroom (sandbox mount-source roots) ──────────────────────
# Warn thresholds for the tmpfs roots the sandbox launcher stages bind-mount
# sources on. Leaked ``tmp*`` mount dirs once filled ``/run/user/$UID`` until its
# inodes ran out, at which point every tool spawn failed with a bare ``rc=1``.
# Inode exhaustion is the more likely face on a tmpfs (each leaked dir is tiny
# but costs an inode), so both free-space and free-inode fractions are checked,
# plus an absolute inode floor: a small tmpfs at 11% free inodes can still be a
# few hundred dirs from failure. The numbers are the resource_status tool's, so
# the doctor and the tool never disagree about one directory.
_TMPFS_FREE_PCT_WARN = cli_doctor.resource_status.TEMP_FREE_PCT_WARN
_TMPFS_FREE_INODES_FLOOR = cli_doctor.resource_status.TEMP_FREE_INODES_FLOOR

# The shared system temp dir. The sandbox chooser lists ``tempfile.gettempdir()``,
# which is ``/tmp`` only while ``TMPDIR`` is unset; an agent shell's ``TMPDIR`` is
# its own scratch dir, so a doctor run from there would otherwise never measure
# the ``/tmp`` that test homes and AF_UNIX sockets still use.
_SHARED_TMP = "/tmp"

# A fake HOME left by a test run holds ``.cache/kirocrew`` (the conftest's
# bytecode and Hypothesis caches resolve under ``~/.cache/kirocrew`` when
# ``XDG_CACHE_HOME`` is unset). Counting them names the usual cause of a full
# ``/tmp``. The scan reads top-level entries only and stops at the cap, because
# the temp dir is shared and can hold a very large number of entries.
_TEST_HOME_MARKER = (".cache", "kirocrew")
_TEST_HOME_SCAN_CAP = 4096


def _runtime_tmpfs_roots() -> list[str]:
    """The roots the sandbox would stage mount sources on, in launcher order.

    Reuses the sandbox's own chooser rather than hardcoding ``/run/user`` so a
    change to the launcher's fallback chain moves this check with it.
    """
    return cli_doctor.sandbox._mount_source_candidate_roots()


def _resource_status_tmp_roots() -> list[str]:
    """Temp roots the pull diagnostic can measure independently."""
    return cli_doctor.resource_status.temp_roots()


def _measured_tmp_roots() -> list[str]:
    """Sandbox roots, shared ``/tmp``, then other pull-diagnostic roots."""
    roots = list(_runtime_tmpfs_roots())
    if _SHARED_TMP not in roots:
        roots.append(_SHARED_TMP)
    for root in _resource_status_tmp_roots():
        if root not in roots:
            roots.append(root)
    return roots


def _kirocrew_test_homes(root: str) -> tuple[int, bool]:
    """``(count, capped)``: top-level dirs of *root* that look like test homes.

    ``capped`` is true when the scan stopped at :data:`_TEST_HOME_SCAN_CAP`
    entries, so the count is a lower bound. An unreadable root counts zero.
    """
    count = 0
    seen = 0
    try:
        with os.scandir(root) as it:
            for entry in it:
                seen += 1
                if seen > _TEST_HOME_SCAN_CAP:
                    return count, True
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if os.path.isdir(os.path.join(entry.path, *_TEST_HOME_MARKER)):
                    count += 1
    except OSError:
        return 0, False
    return count, False


def _tmpfs_usage(root: str) -> tuple[float, float, int, int] | None:
    """``(free_space_pct, free_inode_pct, free_inodes, tmp_entries)`` for *root*.

    ``None`` when the root does not exist or cannot be measured, or when the
    platform has no ``os.statvfs`` (Windows; the doctor section that calls this
    is Linux-only, so this is belt-and-braces for direct callers). ``tmp_entries``
    counts the names carrying the sandbox launcher's mount-source prefix
    (``kirocrew_sb_<pid>_``), so a warning can say how much of the pressure is
    Kiro Crew's own; every other temporary entry belongs to somebody else and
    is deliberately not counted, so the cleanup advice never points at it. A
    filesystem that reports no inode accounting (``f_files == 0``) reads as
    100% free inodes rather than as exhausted. The measurement is the
    resource_status tool's own, so both report the same figures.
    """
    reading = cli_doctor.resource_status.temp_headroom(root)
    if reading is None:
        return None
    try:
        with os.scandir(root) as it:
            tmp_entries = sum(1 for e in it if e.name.startswith(cli_doctor._MOUNT_SOURCE_PREFIX))
    except OSError:
        tmp_entries = 0
    return reading.free_space_pct, reading.free_inode_pct, reading.free_inodes, tmp_entries


def _doctor_runtime_tmpfs(issues: list[str]) -> None:
    """Warn when a sandbox tmp root is close to running out of space or inodes.

    The failure this pre-empts is silent until total: leaked mount-source dirs
    accumulate in the runtime tmpfs, and once its inodes are gone every sandboxed
    tool spawn fails with nothing more than ``rc=1``. Reclaim runs on the
    gateway, but an operator looking at a wall of ``rc=1`` needs somewhere that
    names the disk. Appended to *issues*: a full tmp root breaks every tool, so
    it is a fault, not host trivia. Linux only -- the launcher is.
    """
    if not sys.platform.startswith("linux"):
        return
    print("\nRuntime tmpfs")
    for root in _measured_tmp_roots():
        usage = _tmpfs_usage(root)
        if usage is None:
            print(f"  {root}: ⏭  not present or unreadable")
            continue
        free_space_pct, free_inode_pct, free_inodes, tmp_entries = usage
        detail = (
            f"{free_space_pct:.0f}% space free, {free_inode_pct:.0f}% inodes free "
            f"({free_inodes} inodes), {tmp_entries} {cli_doctor._MOUNT_SOURCE_PREFIX}* entries"
        )
        low_space = free_space_pct < _TMPFS_FREE_PCT_WARN
        low_inodes = free_inode_pct < _TMPFS_FREE_PCT_WARN or free_inodes < _TMPFS_FREE_INODES_FLOOR
        if low_space or low_inodes:
            what = "inodes" if low_inodes and not low_space else "space"
            if low_space and low_inodes:
                what = "space and inodes"
            print(f"  {root}: ⚠️  low on {what} — {detail}")
            print(
                "               Sandboxed tool spawns fail with rc=1 once this fills, "
                "and test runs fail with ENOSPC. "
                f"Kiro Crew's own leaked mount dirs are the {cli_doctor._MOUNT_SOURCE_PREFIX}* "
                "entries; a gateway restart reclaims them."
            )
            homes, capped = _kirocrew_test_homes(root)
            if homes:
                at_least = "at least " if capped else ""
                print(
                    f"               {at_least}{homes} top-level dirs here look like "
                    "leftover test homes (they hold .cache/kirocrew), left by test runs "
                    "that set HOME here. Remove the ones no running process uses."
                )
            print("               Other entries belong to other applications: leave them alone.")
            issues.append(f"runtime tmpfs {root} low on {what} ({detail})")
        else:
            print(f"  {root}: ✅ {detail}")


# ── Temp directory in use (tmpfs / no swap / age-cleaned) ────────────────────
# Distinct from "Runtime tmpfs" above, which warns on space and inode headroom
# for the sandbox launcher's mount-source roots, the shared ``/tmp`` and the
# temp roots this process resolves (this very directory among them). This row adds what the doctor does not already show about the
# directory ``tempfile.gettempdir()`` resolves — the filesystem backing it and
# whether ``systemd-tmpfiles`` deletes files here by age — plus a warning when
# it is a RAM-backed tmpfs with no swap. It deliberately does NOT re-measure
# space/inodes (the Runtime tmpfs row owns those for this dir) and does NOT
# re-print the swap total (the Memory Pressure section already shows it); swap
# is read here only to decide the tmpfs-with-no-swap warning.
#
# The path is the one THIS doctor process resolves. With no ``TMPDIR`` set it is
# the system default (``/tmp``), which is also what a gateway installed with no
# ``TMPDIR`` override uses; a service given a divergent ``TMPDIR`` resolves that
# instead, and this probe — reading the CLI's own environment and mount
# namespace — cannot see it, so the row names the directory it actually measured
# rather than claiming to read the service.
#
# Best effort and advisory only — never appended to ``issues`` and never fails
# the doctor: a RAM-backed temp dir and its ageing policy are host configuration
# the operator owns. Linux carries the probe; macOS and Windows report the dir
# informationally, since the filesystem type, swap and tmpfiles sources are all
# Linux procfs/systemd.


def _tmpfiles_ages(temp_dir: str) -> bool | None:
    """Whether a ``tmpfiles.d`` rule ages *temp_dir*, or ``None`` when unknown.

    Best effort: runs ``systemd-tmpfiles --cat-config`` (resolved through the
    trusted-bin pin so a PATH-planted shim cannot run) and matches ONLY plain
    literal rule paths against *temp_dir*.

    The invariant that keeps it honest: a rule path this reader cannot resolve
    to one definite directory — one carrying a glob metacharacter, a systemd
    ``%`` specifier, a quote or a backslash escape — is never matched literally,
    because reconstructing systemd's glob/specifier/quoting semantics belongs to
    systemd. So the result is ``None`` (unknown) rather than a guess whenever
    ``systemd-tmpfiles`` is absent, the probe fails, or an AGEING rule's path is
    unresolvable or an ``x``/``X`` exclusion provably covers the dir. ``True``
    only on a plain literal ageing rule that covers the dir; ``False`` when every
    ageing rule was a plain literal path and none covers it. Non-privileged and
    capped at 5s so it can never hang the doctor.
    """
    tmpfiles = cli_doctor.platform_compat.trusted_system_bin("systemd-tmpfiles")
    if tmpfiles is None:
        return None
    try:
        res = cli_doctor.subprocess.run(
            [tmpfiles, "--cat-config"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, cli_doctor.subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    target = os.path.realpath(temp_dir)
    aged = False
    for raw in res.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Split off the argument (field 7) from the six STRUCTURAL fields
        # (type, path, mode, uid, gid, age). The argument can legitimately
        # carry quotes -- stock systemd ships ``f+! /run/nologin 0644 - - -
        # "System is booting up."`` -- and it is irrelevant to path/age
        # matching, so a quote THERE must not blind the whole probe.
        structural = line.split(None, 6)[:6]
        # A quote or backslash in a STRUCTURAL field means the path can contain
        # whitespace or escapes, so the whitespace split mis-aligns the age and
        # path and no structural field can be trusted. Such a line is unreadable
        # here, so the honest answer for the whole probe is unknown -- never a
        # verdict derived from mis-aligned fields.
        if any(ch in tok for tok in structural for ch in "\"'\\"):
            return None
        fields = line.split()
        # type, path, mode, uid, gid, age. The base type is the first alpha of
        # field 0 (modifiers like +!=- may precede/follow it).
        if len(fields) < 2:
            continue
        type_field = fields[0]
        base_type = next((c for c in type_field if c.isalpha()), "")
        rule_path = fields[1]
        age = fields[5] if len(fields) >= 6 else "-"
        ages = age not in ("", "-")

        # A systemd ``%`` specifier expands to a directory this reader cannot
        # compute (``q %T 10d`` -> /tmp), so an AGEING rule carrying one is
        # unknowable; a non-ageing one cannot make the dir aged, so skip it.
        # (Quotes and backslashes are handled at the line level above.)
        if "%" in rule_path:
            if ages:
                return None
            continue

        # A glob metacharacter: the rule is anchored at the directory holding
        # the first one (``/tmp/crew-*`` is anchored at ``/tmp``, matching a
        # wildcard WITHIN the ``crew-*`` component, not just at a ``/`` boundary
        # -- the case a naive prefix match misses). An AGEING glob whose anchor
        # directory is related to the target (equal, above, or below) could
        # cover it, so the verdict is unknown; one anchored elsewhere
        # (``e /var/*/cache``) cannot reach the target and is skipped.
        glob_at = min((i for i, ch in enumerate(rule_path) if ch in "*?["), default=-1)
        if glob_at >= 0:
            anchor = os.path.dirname(rule_path[:glob_at]) or "/"
            related = (
                target == anchor
                or target.startswith(anchor.rstrip("/") + "/")
                or anchor.startswith(target.rstrip("/") + "/")
            )
            if ages and related:
                return None
            continue

        resolved = _realpath_or(rule_path)
        covers = resolved == target or target.startswith(resolved.rstrip("/") + "/")
        # An ignore/exclusion (``x``/``X``) AT OR ABOVE the target can stop a
        # parent ageing rule from cleaning it, so the honest answer is unknown.
        # One strictly BELOW the target excludes a child and does not stop the
        # target's own files being aged.
        if base_type in ("x", "X") and covers:
            return None
        if ages and covers:
            aged = True
    return aged


def _realpath_or(path: str) -> str:
    """``os.path.realpath(path)``, or *path* unchanged when it cannot resolve."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _doctor_temp_directories(issues: list[str]) -> None:
    """Report the temp dir in use: filesystem and ageing policy.

    Shows the directory ``tempfile.gettempdir()`` resolves for THIS doctor run,
    with the filesystem type backing it and whether a ``tmpfiles.d`` rule ages
    it. Warns (never fails — advisory, like the Memory Pressure section) when
    the dir is tmpfs with no swap: a RAM-backed temp dir with nothing to absorb
    a spike throttles under memory pressure before the kernel OOM killer fires.
    Swap is read only for that decision and not re-printed — the Memory Pressure
    section already shows the swap total.

    Space and inode headroom are deliberately NOT measured here — the Runtime
    tmpfs row already warns on them for this directory. With no ``TMPDIR`` set
    the path is the system default, which a gateway installed without a
    ``TMPDIR`` override also uses; a service given a divergent ``TMPDIR`` is not
    visible to this probe, so the row names the directory it measured rather than
    claiming to read the service.

    Linux carries the probe; macOS and Windows print the path informationally
    (filesystem type, swap and tmpfiles are all Linux-specific sources).
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nTemp directories")
    try:
        temp_dir = tempfile.gettempdir()
    except OSError:
        # gettempdir() probes candidate dirs and raises when none is usable —
        # exactly the host that most needs the rest of doctor to keep running.
        print("  ⚠️  could not resolve the temp directory (no usable TMPDIR)")
        return
    print(f"  path: {temp_dir} (resolved by this doctor run)")

    if not sys.platform.startswith("linux"):
        print(
            f"  ⏹ not applicable ({sys.platform} — filesystem type, swap and "
            "tmpfiles ageing are read from Linux procfs/systemd)"
        )
        return

    # Reuse the one mount-table filesystem-type resolver the repo already has
    # (taskq) rather than ship a second. A function-local import keeps this
    # family from binding a project module at module scope (test_cli_doctor_
    # refactor_family_reads). It reads the type for the dir, honouring the
    # topmost mount among stacked ones.
    from kiro_crew.taskq.store import _linux_mount_type

    fs_type = _linux_mount_type(Path(temp_dir))
    if fs_type is not None:
        print(f"  filesystem: {fs_type}")
    else:
        print("  filesystem: ⚠️  could not read the mount table")

    # Swap is read (not re-printed — the Memory Pressure section already shows
    # it) only to decide the tmpfs-with-no-swap warning below.
    swap_kib = _swap_total_kib()
    is_tmpfs = fs_type == "tmpfs"

    aged = _tmpfiles_ages(temp_dir)
    if aged is True:
        print("  ageing: ⚠️  a tmpfiles.d rule deletes files here by age")
    elif aged is False:
        print("  ageing: ✅ no tmpfiles.d age rule covers this directory")
    else:
        print(
            "  ageing: ⏹ could not determine (systemd-tmpfiles unavailable or the "
            "probe failed, or a rule this reader cannot resolve — a glob, a "
            "specifier, or an exclusion — covers this directory)"
        )

    # Advisory warning — a RAM-backed temp dir with no swap release valve.
    if is_tmpfs and swap_kib == 0:
        print(f"  ⚠️  {temp_dir} is tmpfs (RAM-backed) with no swap.")
        print(
            "       Its contents count against RAM, so a large write throttles "
            "the host under memory pressure. Point TMPDIR at a disk-backed path "
            "for whatever writes here."
        )


# ── kiro-cli installer residue ────────────────────────────────────────────────
# kiro-cli runs its auto-update check on STARTUP — the ``app.disableAutoupdates``
# setting is documented as "Disable automatic updates on startup" — and Crew
# spawns a FRESH kiro-cli per session (``AcpRuntime`` is constructed per session
# in ``providers/acp_runtime_start.py`` and ``session.py``, and again per Code Review Sage
# worker). So that check runs once per process START, not once per host per
# release.
#
# On Windows the running executable cannot be replaced, so the downloaded
# installer can never be applied while a Crew ACP child holds the binary — and
# the "update pending" state is not cleared after an upgrade either. Nothing in
# that loop is self-limiting: one installer is left behind per process start, and
# the residue reaches tens of gigabytes.
#
# Crew cannot fix the updater, and must NOT disable updates on the user's behalf:
# ``app.disableAutoupdates`` is a per-user setting shared with their own
# interactive CLI, so setting it silently would suppress their security updates.
# What Crew can do is stop the residue being invisible, since it is Crew's
# per-session spawning that turns a stale flag into tens of gigabytes.
_CLI_INSTALLER_GLOB = "kiro-installer*"

# One file can be a download still in flight; two or more is residue, because a
# failed apply leaves the file behind and the next process start fetches another.
_CLI_INSTALLER_RESIDUE_MIN = 2

# The temp dir is shared with every other process on the host and can hold a very
# large number of entries, so a diagnostic must not walk it unbounded.
# Non-recursive by design: the installer lands at the top level.
_CLI_INSTALLER_SCAN_CAP = 512


def _scan_cli_installer_residue(temp_dir: Path) -> tuple[int, int]:
    """Return ``(count, total_bytes)`` for leftover kiro-cli installers in *temp_dir*.

    Bounded and non-raising: the scan stops at :data:`_CLI_INSTALLER_SCAN_CAP`
    matches, and an entry that vanishes mid-scan — another process cleaning up,
    or the updater itself — is skipped rather than aborting the whole doctor run.
    An unreadable temp dir reports "nothing found" for the same reason.
    """
    count = 0
    total = 0
    try:
        for entry in temp_dir.glob(_CLI_INSTALLER_GLOB):
            try:
                if not entry.is_file():
                    continue
                total += entry.stat().st_size
            except OSError:
                # Raced with a delete, or unreadable: one bad entry must not
                # abort a diagnostic.
                continue
            count += 1
            if count >= _CLI_INSTALLER_SCAN_CAP:
                break
    except OSError:
        return (0, 0)
    return (count, total)


def _doctor_cli_installer_residue(issues: list[str]) -> None:
    """Report leftover kiro-cli auto-update installers piling up in the temp dir.

    Silent on a healthy host — the common case, and every case on a platform that
    can replace a running binary — so a normal doctor run gains no noise. This
    speaks only when residue is actually present, which is why it is not gated on
    ``platform.system() == "Windows"``: the gate is the evidence on disk, so the
    check still fires if this failure mode ever appears on another platform.
    """
    # gettempdir() itself probes candidate directories and raises when none is
    # usable, so it must be inside the guard too: a host with a full or
    # unwritable temp volume is exactly the host most in need of the rest of the
    # doctor run, and must not get a traceback instead of it.
    try:
        temp_dir = Path(tempfile.gettempdir())
    except OSError:
        return
    count, total = _scan_cli_installer_residue(temp_dir)
    if count < _CLI_INSTALLER_RESIDUE_MIN:
        return

    # Capped scans undercount, so say so rather than printing a precise-looking
    # number that is actually a floor. This applies to the SIZE as well: the scan
    # stopped summing at the cap, so the total is a floor exactly as the count is,
    # and rendering it as exact next to a "512+" count would contradict itself.
    capped = count >= _CLI_INSTALLER_SCAN_CAP
    count_label = f"{count}+" if capped else str(count)
    if total >= 1073741824:
        size_label = f"{total / 1073741824:.2f} GiB"
    else:
        size_label = f"{total / 1048576:.1f} MiB"
    if capped:
        size_label = f"≥ {size_label}"

    print("\nkiro-cli installer residue")
    print(f"  files:       ⚠️  {count_label} in {temp_dir}")
    print(f"  reclaimable: {size_label}")
    print("               Auto-update downloads that could not be applied while")
    print("               kiro-cli was running, and are not cleaned up. Crew starts")
    print("               a kiro-cli per session, so one accumulates per start.")
    print(f"               Fix: delete {_CLI_INSTALLER_GLOB} from {temp_dir}, then stop")
    print("               the gateway and run `kiro-cli update` deliberately.")
    print("               To stop the downloads: `kiro-cli settings")
    print("               app.disableAutoupdates true` — note this is per-user, so it")
    print("               also pauses updates for your own interactive kiro-cli.")
    issues.append("kiro-cli installer residue in temp")


def _doctor_agents_janitor(issues: list[str], sweep_backups: bool) -> None:
    """Report aged orphaned atomic-write temps and stale backups in the agents dir.

    The shared kiro agents directory accumulates ``<base>.json.<digits>.tmp``
    orphans and ``*.bak-<digits>`` / ``*.json.bak.<digits>`` backups from the
    several independent writers that install agents there; nothing else removes
    them. ``kirocrew doctor`` REPORTS what a sweep would reclaim but never
    deletes anything itself (``dry_run=True``) — a diagnostic you run *because
    something broke* must not silently unlink files, including recovery backups,
    in the same invocation. Actual deletion is left to the fire-and-forget boot
    sweep, and the report mirrors that sweep's scope: backups are only counted
    when ``agent.sweep_agents_backups`` is enabled (*sweep_backups*), since Kiro
    Crew authors none of them and the boot sweep leaves foreign backups alone by
    default. Advisory only (never appended to ``issues``): reclaimable junk is
    housekeeping, not a setup fault, and the scan is fail-open so it can never
    abort the run.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nAgents Directory")
    agents_dir = cli_doctor._agents_dir()
    result = cli_doctor.sweep_agents_dir(agents_dir, dry_run=True, sweep_backups=sweep_backups)
    if result.removed:
        mib = result.freed_bytes / 1048576
        print(
            f"  janitor:     🧹 {result.removed} stale temp/backup file(s) "
            f"reclaimable ({mib:.1f} MiB) — the gateway sweeps these on boot"
        )
        for name in result.removed_names:
            # ``!r`` on the name: this directory is shared with foreign writers,
            # so a crafted filename could otherwise smuggle a terminal-control
            # (ANSI/OSC) escape sequence straight to the operator's terminal.
            print(f"{render._INDENT}- {name!r}")
    else:
        print("  janitor:     ✅ no stale temp/backup files to reclaim")
    _doctor_skill_view_census(agents_dir)
    _doctor_skill_view_residue(agents_dir)
    _doctor_run_dirs()


# Orphaned sidecars or leftover ``<alias>.lock`` files above which the doctor
# warns. The gateway sweeps both in bounded batches, so steady state is near
# zero; a count this high is a backlog it has not drained yet.
_SKILL_VIEW_RESIDUE_WARN = 500


def _doctor_skill_view_residue(agents_dir: Path) -> None:
    """Report, in one line, the skill-view residue and an external rewriter.

    Advisory and read-only. Two signals the alias census cannot give: files the
    projection left around aliases that are gone (ownership sidecars, and the
    empty ``<alias>.lock`` files a spec-rewriting launcher leaves), and aliases
    whose bytes differ from what the projection recorded -- another program is
    rewriting the agents directory, the precondition of alias growth and of the
    "not installed" failure. A
    rewriter is not a fault by itself; the line says so and names the rollback
    switch in case sessions are failing.
    """
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    counts = acp_driver.skill_view_residue_census(agents_dir)
    orphans = counts.get("orphan_sidecars", 0)
    locks = counts.get("alias_locks", 0)
    rewritten = counts.get("rewritten", 0)
    floor = "+" if counts.get("truncated", 0) else ""
    metadata_dir, _lease_dir = acp_driver.skill_view_sidecar_dirs()
    warn = orphans > _SKILL_VIEW_RESIDUE_WARN or locks > _SKILL_VIEW_RESIDUE_WARN or rewritten
    print(
        f"  skill-view residue: {'⚠️ ' if warn else '✅'} {orphans}{floor} ownership sidecar(s) in"
        f" {metadata_dir}/ with no alias, {locks}{floor} leftover alias .lock file(s),"
        f" {rewritten}{floor} alias(es) rewritten by another program"
    )
    if orphans > _SKILL_VIEW_RESIDUE_WARN or locks > _SKILL_VIEW_RESIDUE_WARN:
        print(
            f"{render._INDENT}The gateway removes the ones it can prove are its own residue"
            f" (this home's sidecars, unheld empty locks) in bounded batches at boot and on"
            f" every spawn; restart it once to drain the backlog."
        )
    churning = acp_driver.skill_view_churning_env_keys(agents_dir)
    if churning:
        print(
            f"{render._INDENT}⚠️ env value(s) differing across one agent's skill views:"
            f" {', '.join(render._safe_display(label) for label in churning)}. If a launcher"
            f" re-stamps one on every launch, add its"
            f" key to KIROCREW_SKILL_VIEW_VOLATILE_ENV so it stops naming a new view per launch."
        )
    if rewritten:
        print(
            f"{render._INDENT}Another program rewrites the specs in {agents_dir} (a sandbox or"
            f" credential launcher does this on every launch). Kiro Crew tolerates it; if"
            f" sessions still fail with 'Agent spec ... is not installed', turn projection"
            f" off (kirocrew config set agent.native_skill_projection false, or"
            f" KIROCREW_NATIVE_SKILL_PROJECTION=0 for the gateway) and report it."
        )


# Unmarked run directories above which the doctor warns. Each is one directory
# holding one small file; the count matters as a listing cost on the workspace
# root, which every derived-cwd spawn's ``mkdir`` re-enumerates.
_RUN_DIR_BACKLOG_WARN = 1000


def _doctor_run_dirs() -> None:
    """Report, in one line, the run directories the gateway's sweep cannot reclaim.

    Advisory and read-only. A subagent, a stateless cron run or a memory
    consolidation call gets a directory under the workspace root that the provider
    marks at first start and reclaims at shutdown; the gateway sweeps what a dead
    predecessor of its own data home left. A memory consolidation folder is never
    marked on the by-name walk (Windows), so the census counts it with the
    unmarked ones. Three figures from one bounded walk, judged by the sweep's own
    rule: directories from builds that wrote no marker (a name is not provenance,
    so the sweep deletes nothing it cannot prove Crew made); marked directories
    this data home cannot act on -- another data home's, an unreadable marker, or
    a gateway the pid ledger still retains entries for; and marked directories
    the rule permits yet the sweep keeps for what they hold beyond Crew's own
    residue -- a folder the by-name walk keeps for kiro-cli's ``.kiro/agents``,
    or any marked folder that gained a file. Named, never done: the doctor
    deletes nothing.
    """
    from kiro_crew.config.loader import workspace_root
    from kiro_crew.session_pid import retained_gateway_pids
    from kiro_crew.session_work_dir import DERIVED_NAME_RE, RUN_DIR_MARKER, count_run_dirs
    from kiro_crew.workspace_cli_settings import CLI_SETTINGS_LOCK_NAME

    try:
        # Resolve only: the default resolver creates the tree, and a read-only
        # report must not leave a workspace behind where no gateway ever ran.
        root = workspace_root(create=False)
    except OSError:
        return
    if not root.is_dir():
        print("  run dirs:    ✅ no workspace root yet, so no run directories")
        return
    try:
        retained = retained_gateway_pids()
    except OSError:
        print("  run dirs:    ⚠️  the session pid ledger cannot be read; census skipped")
        return
    census = count_run_dirs(root, retained_gateway_pids=retained)
    if not census.unmarked and not census.refused and not census.kept:
        print("  run dirs:    ✅ no run directories left behind that the sweep cannot reclaim")
        return
    suffix = "+" if census.floor else ""
    warn = census.unmarked > _RUN_DIR_BACKLOG_WARN or census.refused > 0 or census.kept > 0
    print(
        f"  run dirs:    {'⚠️ ' if warn else '✅'} under {root}: {census.unmarked}{suffix} run"
        f" director(ies) carry no {RUN_DIR_MARKER} marker (left by a build that did not mark that"
        f" kind); {census.refused}{suffix} marked director(ies)"
        f" this data home cannot reclaim (another data home's, an unreadable marker, or a"
        f" gateway the pid ledger still retains); {census.kept}{suffix} marked director(ies) the"
        f" sweep keeps for what they hold beyond .kiro/settings residue"
    )
    if census.unmarked > _RUN_DIR_BACKLOG_WARN:
        print(
            f"{render._INDENT}The gateway reclaims only marked run directories. With the gateway"
            f" stopped, move directories matching {DERIVED_NAME_RE.pattern} that hold nothing"
            f" beyond .kiro/settings/cli.json, .kiro/settings/{CLI_SETTINGS_LOCK_NAME} and an"
            f" empty .kiro/agents out of {root}; a live run recreates its own."
        )


def _doctor_skill_view_census(agents_dir: Path) -> None:
    """Report how many projected skill-view aliases the agents directory holds.

    Advisory and read-only, like the janitor line above it. The count matters
    because kiro-cli enumerates every file in this directory on every startup
    and the projection writes one alias per distinct agent view, shared by
    every spawn of that agent: a backlog from a build that predates the
    lease-based reclaim reached 28k files on one host and made every session
    start crawl. The gateway drains its own home's unreferenced aliases -- the
    whole backlog at boot in lock-bounded batches, a bounded number per spawn
    after that; the report says
    exactly which share that covers -- not aliases another data home owns, not
    lease-named ones while their lease is held -- and refuses to promise any
    drain while a lease record is unreadable, since the reclaim then keeps
    everything. Once the census hit a retention bound its counts are floors and
    the derived ones are not printed at all. The manual fallback is named,
    never performed, and is a move rather than a delete: the doctor cannot
    prove who authored a file that merely carries the prefix, and a move is
    undoable.
    """
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    counts = acp_driver.skill_view_alias_census(agents_dir)
    total = counts.get("total", 0)
    leased = counts.get("leased", 0)
    foreign_unreferenced = counts.get("foreign_home", 0)
    foreign_leased = counts.get("foreign_leased", 0)
    foreign = foreign_unreferenced + foreign_leased
    unreadable = counts.get("unreadable_leases", 0)
    truncated = bool(counts.get("truncated", 0))
    metadata_dir, lease_dir = acp_driver.skill_view_sidecar_dirs()
    alias_glob = f"{cli_doctor.NATIVE_SKILL_ALIAS_PREFIX}*.json"
    # Two Kiro Crew data homes share this directory whenever they share
    # ``~/.kiro``; the remedy must then stop every gateway that uses it, not
    # only the one this doctor speaks for.
    stopped = (
        "with every gateway that uses this agents directory stopped"
        if foreign
        else "with the gateway stopped"
    )

    floor = "+" if truncated else ""
    detail = f"{total}{floor} {alias_glob} alias(es)"
    if total:
        detail += f" ({leased}{floor} named by a lease record"
        # Once a bound was hit "not named" is total minus a floor, which is
        # neither a floor nor a ceiling, so only the measured counts are shown.
        if not truncated:
            detail += f", {total - leased} not"
        if foreign:
            detail += f", {foreign}{floor} owned by another Kiro Crew home"
        detail += ")"
    warn = bool(unreadable) or total > _SKILL_VIEW_BACKLOG_WARN
    print(f"  skill views: {'⚠️ ' if warn else '✅'} {detail}")
    if truncated:
        print(f"{render._INDENT}(floors: the census stopped at its retention bound)")
    if unreadable:
        print(
            f"{render._INDENT}{unreadable} lease record(s) in {lease_dir}/ (or in the legacy"
            f" .kirocrew-skill-projection-leases/ beside the aliases) cannot be read, and"
            f" the reclaim keeps every alias while one exists. {stopped[0].upper()}"
            f"{stopped[1:]}, move that directory out of the way; every"
            f" live projection republishes its own lease."
        )
    if total <= _SKILL_VIEW_BACKLOG_WARN:
        return
    if unreadable:
        drain = " Nothing is reclaimed until the unreadable lease record(s) above are gone."
    elif truncated:
        # Past the lease bound the census did not read every record, and one
        # unreadable record it did not reach would stop the reclaim entirely.
        drain = (
            " Unscanned lease records leave reclaimability unknown: the gateway"
            " reclaims this home's unreferenced aliases a bounded number per spawn"
            " only while every lease record is readable."
        )
    else:
        drain = (
            f" On every spawn the gateway reclaims a bounded number of the"
            f" {total - leased - foreign_unreferenced} this home owns and no lease names."
        )
    if leased - foreign_leased > 0:
        drain += (
            " This home's lease-named aliases are kept while their lease is held; a"
            " crash-stale lease is reclaimed on the next spawn."
        )
    if foreign:
        drain += (
            f" The {foreign}{floor} another Kiro Crew home owns never drain here;"
            f" only that home's gateway reclaims them."
        )
    print(
        f"{render._INDENT}kiro-cli reads every file here on startup, so this many slows every"
        f" session start.{drain}"
    )
    print(
        f"{render._INDENT}The gateway has stopped creating new skill views at this count;"
        f" new spawns run under their authored agent until it drops, so the leak cannot deepen"
        f" while it stands (sessions already projecting keep refreshing their own view)."
        f" The count is every {alias_glob} file here -- kiro-cli reads them"
        f" all at startup, including any written by a different gateway that shares this"
        f" directory -- so the move below clears a share this gateway cannot drain itself."
    )
    print(
        f"{render._INDENT}To clear it at once: {stopped}, move the {alias_glob} files out of"
        f" the agents directory and the {metadata_dir}/ directory out of the way (a move is"
        f" undoable; the doctor never deletes). Every spawn republishes the aliases it needs;"
        f" authored agents keep their own names and are not touched."
    )
