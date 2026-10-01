"""Process-tree inspection for ACP harness children.

Enumerates a harness root's descendants, records each child's start identity and
executable name so a recycled pid is never mistaken for one of ours, reaps children
that escaped the killed process group, and measures a tree's resident memory. Shared
by ``AcpClient``, ``AcpRuntime`` and the session layer. The POSIX paths short-circuit
on Windows, where the owned-handle drain in ``platform_compat`` is the teardown.

``kiro_crew.acp.client`` and ``kiro_crew.acp.runtime`` re-export the names each of
them defined before the split.
"""

from __future__ import annotations

import logging

# Bound for the spawn audit (``test/test_spawn_audit.py``), which finds a spawn through
# the modules a file imports; the probes that spawn with it read the client's binding.
import subprocess as subprocess_mod  # noqa: F401
import threading

# The helpers read the modules they probe with (``platform_compat``, ``sys``, ``Path``,
# ``subprocess``, ``os``, ``time``) through the facade their code came from, imported
# inside the function: a test that replaces one of those bindings on
# ``kiro_crew.acp.client`` or ``kiro_crew.acp.runtime`` -- to force a platform branch,
# or to keep a sweep off the host's real processes -- reaches the helper at call time,
# as it did when the helper lived there. Both facades are loaded before any helper runs.
# Circular import: each facade imports this module while it loads, so the helpers
# cannot import a facade at the top of this file.

# Logged under the ACP client's name: the escaped-child sweep is part of the client's
# teardown, and operator log filters and level settings key on that name.
logger = logging.getLogger("kiro_crew.acp.client")


def _get_child_pids(parent_pid: int | None, _visited: set[int] | None = None) -> list[int]:
    """Return PIDs of all descendants recursively (best-effort).

    Uses a visited set to prevent infinite loops from PID cycles.
    On Linux, reads /proc/<pid>/task/*/children (kernel-provided, fast).
    Falls back to pgrep -P on other platforms.
    """
    if not parent_pid:
        return []
    if _visited is None:
        _visited = set()
    if parent_pid in _visited:
        return []
    _visited.add(parent_pid)

    direct = _direct_children(parent_pid)
    all_pids = []
    for cpid in direct:
        if cpid not in _visited:
            all_pids.append(cpid)
            all_pids.extend(_get_child_pids(cpid, _visited))
    return all_pids


def _direct_children(pid: int) -> list[int]:
    """Return direct child PIDs. Uses /proc on Linux, pgrep on other POSIX.

    Windows: returns ``[]`` — there is no pgrep, and the tree kill goes through
    ``kill_process_tree`` (``taskkill /T``), which walks descendants itself, so
    the escaped-child sweep this feeds is a POSIX-only concern.
    """
    from kiro_crew.acp.client import Path, platform_compat, subprocess_mod, sys  # noqa: F811

    if platform_compat.IS_WINDOWS:
        return []
    if sys.platform == "linux":
        try:
            children: list[int] = []
            tasks_dir = Path(f"/proc/{pid}/task")
            if tasks_dir.is_dir():
                for tid in tasks_dir.iterdir():
                    cf = tid / "children"
                    if cf.exists():
                        children.extend(int(p) for p in cf.read_text().split() if p.strip())
            if children:
                return children
        except Exception:
            pass  # fall through to pgrep
    try:
        # timeout so a hung pgrep cannot occupy a subprocess_executor worker
        # indefinitely (the ps spawns in _get_start_time/_read_basename already
        # cap at 2s; this path must match or a wedged pgrep starves the pool).
        out = subprocess_mod.check_output(
            ["pgrep", "-P", str(pid)], stderr=subprocess_mod.DEVNULL, timeout=2
        )
        return [int(p) for p in out.decode().split() if p.strip()]
    except Exception:
        return []


def _get_start_time(pid: int) -> int | None:
    """Read process start time to detect PID recycling.

    Windows: returns ``None``. It feeds the POSIX-only escaped-child sweep
    (a no-op on win32, where ``taskkill /T`` already walks the tree), so a
    missing start time has no effect there and avoids spawning a failing ``ps``.
    """
    from kiro_crew.acp.client import Path, platform_compat, subprocess_mod, sys  # noqa: F811

    if platform_compat.IS_WINDOWS:
        return None
    try:
        if sys.platform == "linux":
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat.rsplit(")", 1)[1].split()
            return int(fields[19])  # field 22 = starttime
        # macOS: use ps -o lstart= (absolute start timestamp, constant for process lifetime)
        ps_bin = platform_compat.trusted_system_bin("ps")
        if ps_bin is None:
            return None
        out = subprocess_mod.check_output(
            [ps_bin, "-o", "lstart=", "-p", str(pid)], stderr=subprocess_mod.DEVNULL, timeout=2
        )
        return hash(out.strip())  # stable per-process, changes on recycle
    except Exception:
        return None


def _read_basename(pid: int) -> bytes | None:
    """Read the executable basename for a PID (platform-aware).

    POSIX only in practice — the escaped-child sweep that consumes this value is a
    Windows no-op — but the helper still short-circuits on win32 (returning None)
    so a stray future caller doesn't crash trying to invoke ``ps`` / read /proc.
    """
    from kiro_crew.acp.client import Path, platform_compat, subprocess_mod, sys  # noqa: F811

    if platform_compat.IS_WINDOWS:
        return None
    try:
        if sys.platform == "linux":
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if not cmdline_path.exists():
                return None
            cmdline = cmdline_path.read_bytes()
            if not cmdline:
                return None
            return cmdline.split(b"\x00", 1)[0].rsplit(b"/", 1)[-1]
        else:
            ps_bin = platform_compat.trusted_system_bin("ps")
            if ps_bin is None:
                return None
            out = subprocess_mod.check_output(
                [ps_bin, "-o", "comm=", "-p", str(pid)], stderr=subprocess_mod.DEVNULL, timeout=2
            )
            name = out.strip()
            if not name:
                return None
            return name.rsplit(b"/", 1)[-1]
    except Exception:
        return None


# Type alias for child PID records: (start_id, recorded_basename).
#
# The identity half is ``platform_compat.get_process_start_id``, NOT the local
# ``_get_start_time``. That matters on macOS, where ``_get_start_time`` returns
# ``hash(ps -o lstart=)``: ``ps`` reports whole seconds, so two processes started
# in the same second alias to one value and a recycled pid can FALSE-MATCH, and
# ``hash()`` is PYTHONHASHSEED-randomized so the value is not comparable outside
# the interpreter that produced it. ``get_process_start_id`` is microsecond
# libproc on macOS, 100 ns creation FILETIME on Windows and stat field 22 on
# Linux, is stable to persist and compare across processes, and spawns nothing.
# Being a neutral value also lets the pid-lifecycle layer verify these records
# with platform_compat alone, instead of importing this module to do it.
ChildRecord = tuple[str | None, bytes | None]


def _capture_child_records(pids: list[int]) -> dict[int, ChildRecord]:
    """Capture (start_id, basename) for each pid as a ChildRecord map.

    On macOS ``_read_basename`` shells out to ``ps`` (and ``_get_child_pids`` to
    ``pgrep``), which can block during the subprocess spawn (fork/exec). Callers
    running on the event loop MUST invoke this via
    ``run_in_executor(subprocess_executor(), ...)`` so the spawns happen on a
    worker thread and never wedge the loop. Only the basename half needs that:
    the identity half spawns nothing on any platform.
    """
    from kiro_crew.acp.client import platform_compat

    return {p: (platform_compat.get_process_start_id(p), _read_basename(p)) for p in pids}


def _is_our_child(
    pid: int, expected_start: str | None = None, expected_basename: bytes | None = None
) -> bool:
    """Verify a PID still belongs to a process we spawned (deny-by-default).

    Compares recorded basename and start id against live values. No hardcoded
    allowlist — any binary recorded at spawn time is automatically supervised.
    Returns False for recycled PIDs or unreadable processes.

    The start id comes from ``platform_compat.get_process_start_id`` so it is
    read the same way it was recorded (see :data:`ChildRecord` for why that is
    not ``_get_start_time``).
    """
    from kiro_crew.acp.client import platform_compat

    try:
        # Start-id check: definitive PID recycling detection (always required)
        actual_start = platform_compat.get_process_start_id(pid)
        if expected_start is None or actual_start is None:
            logger.debug("PID %d start time unavailable — denying (fail-closed)", pid)
            return False
        if actual_start != expected_start:
            logger.debug("PID %d start time mismatch (recycled)", pid)
            return False
        # Basename check: catches recycling to a different binary with same start slot.
        # Deny-by-default: if no basename was recorded (a legacy record predating
        # basename recording), we deny rather than skip the check. Returning False here
        # causes the caller (_kill_escaped_children) to SKIP the kill — we won't
        # SIGKILL a process we can't positively confirm is ours. This is the safe
        # direction: avoids killing a recycled PID that belongs to another user.
        # Trade-off: legacy in-memory records (no basename) are left alive until
        # the session restarts and re-records them with basenames.
        if expected_basename is None:
            logger.debug("PID %d has no recorded basename — denying (fail-closed)", pid)
            return False
        actual_basename = _read_basename(pid)
        if actual_basename is None:
            return False  # process gone
        if actual_basename != expected_basename:
            logger.debug(
                "PID %d basename mismatch (recorded=%r, actual=%r)",
                pid,
                expected_basename,
                actual_basename,
            )
            return False
        return True
    except Exception:
        return False


def _kill_escaped_children(child_pids: dict[int, int | None] | dict[int, ChildRecord]) -> None:
    """SIGKILL descendants that survived killpg (different PGID). Kills leaf-first.

    POSIX-only sweep: it cleans up children that reparented out of the killed
    process group (e.g. MCP servers). On Windows there are no process groups —
    ``kill_process_tree`` already used ``taskkill /T`` to walk the whole child
    tree — so there is nothing left to sweep, and the POSIX signal APIs are
    unavailable there. No-op on win32.
    """
    from kiro_crew.acp.client import platform_compat

    if platform_compat.IS_WINDOWS:
        return
    for cpid in reversed(list(child_pids.keys())):
        try:
            if not platform_compat.pid_exists(cpid):
                continue  # gone — nothing to sweep
            record = child_pids.get(cpid)
            # Support both the legacy (int|None) and current (tuple) record shapes.
            # A legacy int is a pre-neutral-start-id reading and can never equal a
            # ``get_process_start_id`` value, so it is carried as unproven rather
            # than compared: _is_our_child then denies, which is the same outcome a
            # mismatch would produce, and keeps this branch honest about what it can
            # actually verify.
            expected_start: str | None = None
            expected_basename: bytes | None = None
            if isinstance(record, tuple):
                expected_start, expected_basename = record
            if not _is_our_child(
                cpid, expected_start=expected_start, expected_basename=expected_basename
            ):
                logger.debug("Skipping PID %d — not our process (recycled?)", cpid)
                continue
            platform_compat.kill_pid(cpid, platform_compat.SIGKILL)
            logger.debug("Killed escaped child PID %d", cpid)
        except (ProcessLookupError, OSError):
            pass


def _get_rss_mb(pid: int) -> float | None:
    """Get resident set size (RSS) of a process in MiB, or None if unavailable.

    Linux: reads /proc/<pid>/status. macOS (no /proc): shells out to
    ``ps -o rss= -p <pid>`` (ps reports RSS in KiB on both platforms).
    Windows: WorkingSetSize through the ``platform_compat`` shim, since no
    ``ps`` is resolvable there. Returns None on any failure (missing /proc,
    permission error, process gone, ps not found) so callers can treat
    "unknown" the same as "not over threshold" rather than raising.
    """
    from kiro_crew.acp.runtime import platform_compat, subprocess, sys

    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # Format: "VmRSS:\t   123456 kB"
                        parts = line.split()
                        return int(parts[1]) / 1024.0
        except (OSError, IndexError, ValueError):
            return None
        return None

    if platform_compat.IS_WINDOWS:
        # Windows ships no `ps` in the fixed system directories the POSIX
        # fallback below resolves through (trusted_system_bin ignores PATH on
        # purpose), so that fallback can only ever answer None here. Read
        # WorkingSetSize via GetProcessMemoryInfo through the shim instead.
        # The watchdog's RSS-recycle ceiling does not depend on this branch —
        # _get_rss_tree_mb serves Windows from proc_rss_tree_mb_for_pid and
        # never calls this function — so this keeps a direct single-pid read
        # honest for a direct caller.
        rss = platform_compat.proc_rss_bytes_for_pid(pid)
        return None if rss is None else rss / (1024.0 * 1024.0)

    # macOS / other: no /proc, fall back to ps (mirrors the sysctl/ps pattern
    # used elsewhere in this codebase for darwin system info).
    ps_bin = platform_compat.trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        out = (
            subprocess.check_output([ps_bin, "-o", "rss=", "-p", str(pid)], timeout=2)
            .decode()
            .strip()
        )
        return int(out) / 1024.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _own_children(pid: int) -> list[int]:
    """Direct children of *pid*, asked of the kernel one thread at a time."""
    from kiro_crew.acp.runtime import os

    kids: list[int] = []
    try:
        entries = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return kids
    for tid in entries:
        try:
            with open(f"/proc/{pid}/task/{tid}/children") as f:
                tokens = f.read().split()
        except OSError:
            continue
        for tok in tokens:
            try:
                kids.append(int(tok))
            except ValueError:
                continue
    return kids


def _iter_descendant_pids(
    pid: int,
    max_depth: int | None = None,
    *,
    children: "dict[int, list[int]] | None" = None,
) -> list[int]:
    """Return ``[pid, *descendants]`` (Linux only), best-effort.

    Walks ``/proc/<pid>/task/<tid>/children`` breadth-first. Returns ``[pid]``
    when the interface is unavailable. Used so RSS accounting can cover a
    sandbox launcher's exec'd child — see _get_rss_tree_mb().

    ``max_depth`` bounds the walk in generations below *pid*: ``None`` is the
    whole subtree, ``0`` is *pid* alone, ``1`` adds its direct children. The queue
    carries each pid's own depth rather than the loop tracking a level, so a
    process reachable at two depths is counted once, at whichever it is reached
    first — the same single-visit rule the unbounded walk has.

    ``children`` supplies a parent map (``platform_compat.proc_child_map``) to
    read the edges from instead of asking the kernel per process. Same walk and
    same rules; only where an edge comes from changes. It is for a caller that
    needs MANY roots' trees in one pass: the kernel route costs one read per
    thread of every process visited, which a per-root caller pays again on every
    root, while one map answers all of them. A map that is missing a process
    yields the root alone for it, exactly as an unreadable ``children`` file does.
    """
    order: list[int] = []
    visited: set[int] = set()
    queue: list[tuple[int, int]] = [(pid, 0)]
    while queue:
        p, depth = queue.pop()
        if p in visited:
            continue
        visited.add(p)
        order.append(p)
        if max_depth is not None and depth >= max_depth:
            continue
        for cpid in children.get(p, ()) if children is not None else _own_children(p):
            if cpid not in visited:
                queue.append((cpid, depth + 1))
    return order


#: A whole-machine process table: ``(children_by_ppid, rss_kib_by_pid)``.
_ProcessTable = tuple[dict[int, list[int]], dict[int, int]]

#: How long one ``ps -A`` snapshot may be reused.
#:
#: This exists because the snapshot is WHOLE-MACHINE while its consumer asks
#: per-pid. ``session_memory._blocking_sample`` samples every live runtime pid in
#: one pass, so an uncached snapshot enumerated every process on the host once
#: PER SESSION — 8 sessions on a host with ~150 MCP processes meant 8 full
#: process-table walks every 5s, serialized in one worker. Measured cost on a
#: typical Mac (875 procs): ~33ms per ``ps -Ao``, so 8 walks ≈ 272ms duty cycle
#: per 5s poll — linear amplification that wastes a thread worker and grows with
#: session count (macOS only: the Linux branch above uses ``/proc`` directly and
#: never spawns anything).
#:
#: One second is chosen against the two consumers, not arbitrarily: the Sessions
#: panel polls at 5s and the watchdog's RSS ceiling is a multi-GB threshold
#: checked on a timer, so neither can tell a 1s-old measurement from a fresh
#: one — while a sampling pass over N pids completes well inside the window and
#: therefore pays for exactly one snapshot.
_PS_TABLE_TTL_S = 1.0

_ps_table_lock = threading.Lock()
#: ``(monotonic_taken_at, table)``, or None before the first snapshot. A cached
#: FAILURE is not stored — a transient ``ps`` error must not pin every caller to
#: the single-pid fallback for a whole second.
_ps_table_cache: tuple[float, _ProcessTable] | None = None


def _reset_ps_table_cache() -> None:
    """Drop the memoized process table. Test seam: the cache is keyed on wall
    time only, so a test that fakes ``ps`` output would otherwise inherit the
    previous test's snapshot."""
    global _ps_table_cache
    with _ps_table_lock:
        _ps_table_cache = None


def _ps_process_table() -> _ProcessTable | None:
    """One ``ps -Ao pid=,ppid=,rss=`` snapshot as a parent map + RSS map.

    Memoized for :data:`_PS_TABLE_TTL_S` so a caller that needs the tree for many
    pids pays for ONE process-table walk rather than one per pid. Returns None
    when ``ps`` is unavailable or fails, so callers fall back to a single-pid
    read instead of reporting a phantom-empty tree.

    The snapshot is taken under the lock rather than merely published under it:
    concurrent first-callers would otherwise each spawn ``ps`` before any of them
    stored a result, which is the exact amplification this cache exists to
    remove.
    """
    from kiro_crew.acp.runtime import platform_compat, subprocess, time

    global _ps_table_cache
    with _ps_table_lock:
        cached = _ps_table_cache
        if cached is not None and (time.monotonic() - cached[0]) < _PS_TABLE_TTL_S:
            return cached[1]
        ps_bin = platform_compat.trusted_system_bin("ps")
        if ps_bin is None:
            return None
        try:
            out = (
                subprocess.check_output([ps_bin, "-Ao", "pid=,ppid=,rss="], timeout=2)
                .decode()
                .strip()
            )
        except (OSError, subprocess.SubprocessError):
            return None
        children: dict[int, list[int]] = {}
        rss_kib: dict[int, int] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                cpid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(cpid)
            rss_kib[cpid] = rss
        table: _ProcessTable = (children, rss_kib)
        _ps_table_cache = (time.monotonic(), table)
        return table


def _rss_tree_mb_for_pids(pids: list[int]) -> float | None:
    """Sum RSS (MiB) over pids already walked (Linux), or None if none answered.

    Split out of _get_rss_tree_mb so a caller that ALREADY holds the descendant
    set can sum it without walking again. Nearly all of the cost is the walk,
    not the sum: measured over 245 live session trees of 2 to 31 processes, the
    walk took a median 10.3ms while summing RSS over the set it returned took
    0.8ms. So a caller that needs both the set and the total, and cannot hand
    the set over, walks twice and roughly doubles its own cost.
    """
    total = 0.0
    found = False
    for p in pids:
        r = _get_rss_mb(p)
        if r is not None:
            total += r
            found = True
    return total if found else None


def _get_rss_tree_mb(
    pid: int, max_depth: int | None = None, *, pids: list[int] | None = None
) -> float | None:
    """Sum RSS (MiB) of *pid* and its descendants, or None if unavailable.

    ``max_depth`` bounds the sum in generations below *pid*, for a host that
    declares one through ``SpawnPlan.rss_depth``. ``None``, the default, is
    the whole subtree and is what every kiro-family host uses.

    Windows answers None for any bounded request rather than a subtree total. The
    bound is not available there: the tree is summed through
    ``proc_rss_tree_mb_for_pid``, whose lineage-VALIDATED walk returns a flat set
    of genuine descendants with no generation attached, and the naive parent-map
    walk that would carry depth is the unsafe one that walk exists to avoid.
    Answering with the subtree instead would judge a bounded host's ceiling
    against an unbounded measurement — and for a host that declares a bound
    because its subtree is dominated by a per-session fleet, that reads as a leak
    on the first session and recycles a healthy process. None is the "unknown, do
    not judge" answer this probe's caller already handles, so the age ceiling
    still governs while the RSS ceiling abstains.

    On Linux the kirocrew-lite background runtime is spawned through the
    namespace sandbox launcher, which ``fork()``s: ``self._pid`` is the
    launcher parent (small, stable, blocked in ``waitpid``) while the real
    kiro-cli that accumulates multi-GB RSS is a child. Measuring only
    ``self._pid`` therefore misses the growth entirely, so we sum the whole
    descendant tree.

    On macOS the tree is walked too, and it is NOT redundant: kiro-cli spawns
    MCP-server / tool children there exactly as it does on Windows (see that
    branch's note), so measuring only ``pid`` under-reports a session's real
    footprint and blinds the watchdog's leak ceiling. The macOS tree is NOT "just
    the process itself" — believing otherwise is what makes the per-pid
    whole-machine snapshot look free.

    ``pids`` lets a caller that has ALREADY walked the descendants hand the set
    over so it is not walked a second time. Linux only, because that is the one
    branch whose total is reached from a pid list at all.
    """
    from kiro_crew.acp.runtime import platform_compat, sys

    if sys.platform == "linux":
        if pids is None:
            pids = _iter_descendant_pids(pid, max_depth)
        return _rss_tree_mb_for_pids(pids)

    if platform_compat.IS_WINDOWS:
        if max_depth is not None:
            # See the docstring: no depth-carrying validated walk exists here, and
            # a subtree total would be judged against a bounded host's ceiling.
            return None
        # Windows spawns kiro-cli WITHOUT a launcher fork, but it still spawns
        # MCP-server / tool children that can leak. Sum the tree via
        # proc_rss_tree_mb_for_pid, which enumerates descendants through
        # descendant_termination_handles — the lineage-VALIDATED walk (exact
        # creation/exit-time edge checks across two snapshots). A raw Toolhelp
        # parent-map walk is unsafe here: th32ParentProcessID is never cleared
        # when a parent dies and Windows recycles PIDs, so it would sum unrelated
        # subtrees rooted at a recycled PID into a kill/health decision. The
        # validated walk always counts the root, so an unreadable descendant
        # (another session / higher integrity) narrows the total rather than
        # producing a phantom-low tree attached to a recycled root.
        return platform_compat.proc_rss_tree_mb_for_pid(pid)

    # macOS / other: sum the descendant subtree rooted at pid off a SHARED
    # whole-machine snapshot (ps reports RSS in KiB). The snapshot is memoized in
    # _ps_process_table, so sampling N pids costs one process-table walk, not N.
    table = _ps_process_table()
    if table is None:
        return _get_rss_mb(pid)
    children, rss_kib = table
    if pid not in rss_kib:
        return None
    total_kib = 0
    visited: set[int] = set()
    queue: list[tuple[int, int]] = [(pid, 0)]
    while queue:
        p, depth = queue.pop()
        if p in visited:
            continue
        visited.add(p)
        total_kib += rss_kib.get(p, 0)
        if max_depth is not None and depth >= max_depth:
            continue
        queue.extend((c, depth + 1) for c in children.get(p, []))
    return total_kib / 1024.0
