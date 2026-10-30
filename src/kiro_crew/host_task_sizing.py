"""Task and thread-pool ceilings sized from the host, for when config leaves them unset.

The kernel's ``pids`` controller counts TASKS, so every thread an agent tree
starts counts against the agents slice's ``TasksMax``. Runtimes that size their
pools by core count turn a many-core host into a task-hungry one: on a 128-vCPU
host one kiro-cli ACP child runs about 277 threads (tokio and rayon each start
one worker per core), and a ``creds_agent`` MCP server, a tokio binary,
about 138. With both pools at 4 the same processes run 29 and 14. A fixed slice
ceiling sized for a laptop then fills at a few dozen sessions, and the kernel
refuses the next ``clone()`` in whichever runtime asks.

Two values are derived here, each used only when config leaves it unset:

* :func:`size_slice_tasks` -- ``TasksMax`` for the shared agents slice
  (``resource_limits.max_total_processes``).
* :func:`size_thread_pool` -- the default for ``TOKIO_WORKER_THREADS`` and
  ``RAYON_NUM_THREADS`` in spawned kiro-cli children
  (``agent.child_env_defaults``), set only where the agents slice's task
  ceiling is in force (:func:`task_ceiling_in_force`); elsewhere there is no
  ceiling to protect and each runtime keeps its own pool sizes. Both variables
  are honored by kiro-cli-chat, and ``TOKIO_WORKER_THREADS`` by ``creds_agent``,
  which inherits it through the MCP tree. ``GOMAXPROCS`` is not set: neither
  binary is a Go program.

Python MCP servers are not covered here because ``kirocrew mcp-*`` already
caps OpenBLAS and OpenMP at one thread in-process (``cli.main`` and
``mcp_entry``); numpy alone would otherwise start 64 threads per server.

The host is read once per process (:func:`host_task_facts`) and then fixed,
like :mod:`kiro_crew.session_start_sizing`. Every function is pure over a
:class:`HostTaskFacts` so the formulas are testable without the host.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The fixed slice ceiling used before host sizing. Still the floor of the
#: derived value, so no host gets a smaller ceiling than it ran with before
#: unless its own user or kernel limits are smaller still.
SLICE_TASKS_FLOOR = 32768
#: The per-scope ceiling (``sandbox_cgroup._CGROUP_DEFAULT_MAX_PROCESSES``,
#: pinned by a test). The slice keeps room for one scope at this ceiling.
SCOPE_TASKS = 8192
#: Resident memory of a small agent runtime (one kiro-cli and its MCP tree).
#: Measured across 35 live scopes on a 128-vCPU host: median 789 MB, mean
#: 1.9 GB. The low end is used so the count of runtimes that fit is an upper
#: bound.
RUNTIME_MEMORY_MB = 512
#: Tasks budgeted per runtime. The same scopes averaged 203 tasks with the
#: thread pools capped (about 840 uncapped); 512 is 2.5 times the capped mean.
RUNTIME_TASKS = 512
#: Share of RAM the slice's ``MemoryMax`` defaults to; the same value as
#: ``sandbox_slice._CGROUP_TOTAL_MEMORY_FRACTION`` (pinned by a test).
SLICE_MEMORY_SHARE = 0.80
#: Share of the tightest user or kernel task ceiling the slice may take. The
#: rest stays with the gateway, the user's own shells and other services.
SLICE_SHARE_OF_HOST = 0.5
#: Worker threads per pool in a spawned kiro-cli. Measured on a 128-vCPU host,
#: kiro-cli-chat runs about 21 threads plus its tokio and rayon pools, and an
#: ACP agent mostly waits on the network, so four workers per pool is plenty.
THREAD_POOL_CAP = 4
#: The pool variables a host default is filled in for.
THREAD_POOL_ENV = ("TOKIO_WORKER_THREADS", "RAYON_NUM_THREADS")

_PROC_SYS_KERNEL = Path("/proc/sys/kernel")
#: The user manager's cgroup and its ancestors, nearest first; ``{uid}`` is
#: filled at read time. A module attribute so tests can point it elsewhere.
_USER_CGROUP_CHAIN = (
    "/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service",
    "/sys/fs/cgroup/user.slice/user-{uid}.slice",
    "/sys/fs/cgroup/user.slice",
)


@dataclass(frozen=True)
class HostTaskFacts:
    """What bounds task creation on this host. ``None`` means unread or unbounded."""

    cpus: int | None
    mem_bytes: int | None
    pid_max: int | None
    threads_max: int | None
    user_tasks_max: int | None

    def host_task_budget(self) -> int | None:
        """The tightest known task ceiling above the agents slice, or ``None``."""
        known = [v for v in (self.pid_max, self.threads_max, self.user_tasks_max) if v]
        return min(known) if known else None

    def describe(self) -> str:
        mem = "unknown" if self.mem_bytes is None else f"{self.mem_bytes / 2**30:.0f}GiB"
        user = "max" if self.user_tasks_max is None else str(self.user_tasks_max)
        return (
            f"cpus={self.cpus} mem={mem} pid_max={self.pid_max} "
            f"threads-max={self.threads_max} user TasksMax={user}"
        )


def _read_int(path: Path) -> int | None:
    """A positive integer from a one-line kernel file; ``None`` for ``max`` or unread."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdigit() and int(text) > 0 else None


def _read_user_tasks_max(uid: int) -> int | None:
    """The smallest ``pids.max`` on the user manager's cgroup and its ancestors."""
    values = [_read_int(Path(p.format(uid=uid)) / "pids.max") for p in _USER_CGROUP_CHAIN]
    known = [v for v in values if v is not None]
    return min(known) if known else None


def _read_mem_bytes() -> int | None:
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    return total if total > 0 else None


def _read_cpus() -> int | None:
    """Cores an agent runtime will size its pools by.

    The affinity mask, not the gateway's own cgroup ``cpu.max``: agent scopes
    live in the agents slice, outside the gateway's unit, so a quota on the
    gateway (a pod runs with ``CPUQuota=200%``) does not reach them, and tokio
    and rayon in the child count the affinity cores.
    """
    try:
        from kiro_crew.cpu_affinity import affinity_cpu_count

        count = affinity_cpu_count()
    except Exception:
        logger.debug("cpu probe failed", exc_info=True)
        count = None
    return count if count else os.cpu_count()


def probe_host_task_facts() -> HostTaskFacts:
    """Read the host now. File I/O: call off the event loop."""
    if not sys.platform.startswith("linux"):
        return HostTaskFacts(_read_cpus(), _read_mem_bytes(), None, None, None)
    return HostTaskFacts(
        cpus=_read_cpus(),
        mem_bytes=_read_mem_bytes(),
        pid_max=_read_int(_PROC_SYS_KERNEL / "pid_max"),
        threads_max=_read_int(_PROC_SYS_KERNEL / "threads-max"),
        user_tasks_max=_read_user_tasks_max(os.getuid()),
    )


_lock = threading.Lock()
_cached: HostTaskFacts | None = None


def host_task_facts() -> HostTaskFacts:
    """This process's one host reading, probed on first use and then fixed."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = probe_host_task_facts()
        return _cached


def max_runtimes(facts: HostTaskFacts, slice_memory_mb: int | None = None) -> int | None:
    """How many agent runtimes the slice's memory ceiling can hold; pure.

    *slice_memory_mb* is the slice ``MemoryMax`` in force; unset, it is
    :data:`SLICE_MEMORY_SHARE` of RAM, the module default. ``None`` when
    neither is known.
    """
    if slice_memory_mb is None:
        if facts.mem_bytes is None:
            return None
        slice_memory_mb = int(facts.mem_bytes * SLICE_MEMORY_SHARE) // 2**20
    return max(1, slice_memory_mb // RUNTIME_MEMORY_MB)


def size_slice_tasks(facts: HostTaskFacts, slice_memory_mb: int | None = None) -> int:
    """``TasksMax`` for the agents slice on a host with *facts*; pure.

    ``max_runtimes * 512 + 8192``: every runtime the slice's
    memory can hold gets :data:`RUNTIME_TASKS`, and one tree may still grow to
    its whole scope ceiling (a parallel build). Legitimate load therefore hits
    ``MemoryMax`` before ``TasksMax``, and only a tree averaging well past the
    measured cost (a thread leak, a fork bomb) reaches the task ceiling.
    Capped at half the tightest user or kernel task ceiling
    (:data:`SLICE_SHARE_OF_HOST`), but never below :data:`SLICE_TASKS_FLOOR`
    unless that ceiling itself is lower, in which case it is the whole ceiling.
    With the memory unknown the floor applies.
    """
    runtimes = max_runtimes(facts, slice_memory_mb)
    value = SLICE_TASKS_FLOOR
    if runtimes is not None:
        value = max(value, runtimes * RUNTIME_TASKS + SCOPE_TASKS)
    budget = facts.host_task_budget()
    if budget is None:
        return value
    return min(max(SLICE_TASKS_FLOOR, min(value, int(budget * SLICE_SHARE_OF_HOST))), budget)


def size_thread_pool(facts: HostTaskFacts) -> int | None:
    """Default worker count for each pool in :data:`THREAD_POOL_ENV`; pure.

    ``min(cpus, 4)``. ``None`` when the core count is unknown, in which case
    no default is set and each runtime keeps its own.
    """
    if not facts.cpus or facts.cpus < 1:
        return None
    return min(facts.cpus, THREAD_POOL_CAP)


def task_ceiling_in_force() -> bool:
    """Whether agent spawns on this host run under the agents slice's ``TasksMax``.

    The same capability check both cgroup layers gate on
    (``sandbox._probe_cgroup_scope``: Linux, cgroup v2, the ``pids`` and
    ``memory`` controllers delegated, ``systemd-run``, a reachable user bus).
    Where it fails, ``cgroup_scope_argv`` returns the argv unwrapped and
    ``ensure_agents_slice_limits`` skips the slice, so no task ceiling exists for
    a capped pool to protect: macOS, Windows, and Linux without the slice.
    ``False`` when the check itself fails. The probe is cached with a TTL and may
    shell out on a miss, so call off the event loop.
    """
    try:
        from kiro_crew import sandbox

        available, _reason = sandbox._probe_cgroup_scope()
    except Exception:
        logger.debug("task ceiling probe failed", exc_info=True)
        return False
    return bool(available)


def thread_pool_env_defaults(
    facts: HostTaskFacts | None = None, *, ceiling_in_force: bool | None = None
) -> dict[str, str]:
    """The host-derived ``agent.child_env_defaults`` entries.

    Empty when the pools cannot be sized, and empty where no task ceiling is in
    force (:func:`task_ceiling_in_force`): the cap exists only to keep agent
    trees inside the agents slice's ``TasksMax``, so a host without that slice
    keeps each runtime's own pool sizes. *ceiling_in_force* overrides the
    reading for a caller that already has one.
    """
    in_force = task_ceiling_in_force() if ceiling_in_force is None else ceiling_in_force
    if not in_force:
        return {}
    pool = size_thread_pool(facts if facts is not None else host_task_facts())
    if pool is None:
        return {}
    return {name: str(pool) for name in THREAD_POOL_ENV}


def _reset_for_tests() -> None:
    global _cached
    with _lock:
        _cached = None
