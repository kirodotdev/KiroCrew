"""Subagent orchestration — spawn isolated background agents.

Each subagent gets its own LLM session (via SessionManager) with a
focused system prompt.  Results are announced back to the caller via
a callback.  Max concurrent limit prevents resource exhaustion.

No spawn recursion: subagents cannot spawn other subagents.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.executors import run_in_embed_pool

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.providers.base import LLMProvider

from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context import ContextBuilder, window_for_provider_client
from kiro_crew.context_management import (
    COMPLETION_KEEP_DEFAULT_CHARS,
    apply_completion_keep,
    cap_result_file,
    evict_completed_agents,
)
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    fire_tool_hooks,
    safe_read_file,
)
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    LLMEvent,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import SessionManager
from kiro_crew.session_workspace import result_path as _ws_result_path
from kiro_crew.slack.format import extract_options
from kiro_crew.stats import Stats
from kiro_crew.subagent_cost import (
    append_cost_sample,
    compact_cost_log,
    read_learned_cost,
)
from kiro_crew.subagent_persistence import (
    _agent_dir,
    _cleanup_session_files_sync,
    create_agent_folder,
    list_orphans,
    mark_delivered,
    prune_stale_tombstones,
    update_state,
    write_result_chunk,
    write_tombstone,
)
from kiro_crew.validation import _AGENT_NAME_RE

# Standalone ClaudeCodeProvider removed (KiroACP-only). Name kept as None so the
# legacy isinstance guards short-circuit; the claude-agent-acp seam lives in
# providers.acp.is_claude_backend.
ClaudeCodeProvider = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


_background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks


def _safe_fire(coro: Awaitable[None]) -> None:
    """Schedule a coroutine, preventing GC and logging failures."""

    async def _wrap() -> None:
        try:
            await coro
        except Exception:
            logger.warning("Subagent callback failed", exc_info=True)

    task = asyncio.ensure_future(_wrap())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


_MAX_CONCURRENT = 3


def _validate_agent(requested: str) -> tuple[str, str]:
    """Validate agent name exists in ~/.kiro/agents/.

    Returns (agent_name, error). If agent found, error is empty.
    If not found, agent_name is empty and error explains what happened.
    """
    if not requested:
        return "", ""
    from kiro_crew.aim_agents import list_agents

    known = {a.name for a in list_agents()}
    if requested in known:
        return requested, ""
    available = sorted(known - {"kirocrew", "kirocrew-conductor"})
    logger.warning(
        "Agent %r not found, falling back to kirocrew. Available: %s", requested, available
    )
    return "", ""


def _vet_spawn_governance(parent_session_key: str, agent: str) -> str | None:
    """Return a denial reason if governance forbids spawning, else None.

    Two checks against the parent surface's ceiling ∩ profile:
    1. ``capabilities.spawn`` must be enabled.
    2. if enabled with an ``agents`` scope, the target *agent* must be permitted.

    Best-effort beyond the always-on guards: a ``PlatformCompositionError``
    propagates (fail-closed CPP); any other error returns a denial reason
    (fail-closed) rather than None/no-opinion.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # Gate enabled?  (item ignored when no inner scope — checks ``enabled``.)
        gate = governance_permits(
            "capabilities.spawn", "", session_key=parent_session_key
        )
        if not getattr(gate, "permitted", True):
            return getattr(gate, "reason", "spawn capability disabled")
        # Agent-scope check (capabilities.spawn.scopes.agents).
        if agent:
            scoped = governance_permits(
                "capabilities.spawn",
                f"agents:{agent}",
                session_key=parent_session_key,
            )
            if not getattr(scoped, "permitted", True):
                return f"agent {agent!r} not permitted by spawn policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED: a governance evaluation error must DENY the
        # spawn, not silently permit it (previously returned None = no opinion =
        # allow).  PlatformCompositionError already propagates above; every other
        # error lands here and is audited before denial.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "subagent_spawn",
                session_key=parent_session_key,
                scope="capabilities.spawn",
                failed_closed=True,
            )
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return "subagent spawn denied: governance evaluation failed (fail-closed)"


def _redact(text: str) -> str:
    """Redact credentials and exfiltration URLs from text."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


_MAX_DONE_RESULT_LEN = 50_000  # cap subagent_done payload to avoid bloating WS frames


def _done_result(text: str) -> str:
    """Redact + cap result for inclusion in subagent_done event."""
    if not text:
        return ""
    redacted = _redact(text)
    if len(redacted) <= _MAX_DONE_RESULT_LEN:
        return redacted
    return "…(truncated)\n" + redacted[-_MAX_DONE_RESULT_LEN:]


_TIMEOUT_SECS = 1800  # 30 minutes
_TURN_LIMIT = 100
_REAPER_INTERVAL = 60  # seconds between reaper sweeps
_RESET_TIMEOUT = 30.0  # max seconds for session reset in finally block
_STARTUP_TIMEOUT_SECS = 120  # max seconds a subagent may sit pre-first-turn with no runtime before the startup watchdog reaps it
_ON_DONE_TIMEOUT = 1200.0  # outer cap: max total seconds for semaphore wait + injection
INJECTION_TIMEOUT = 300.0  # inner cap: max seconds for a single stream_and_collect call


def _timeout_context(info: "SubagentInfo", *, include_elapsed: bool = True) -> str:
    """Build a human-readable context string for timeout errors."""
    parts = [f"turn {info.turns}/{info.max_turns}"]
    if info.last_tool:
        parts.append(f"last tool: {_redact(info.last_tool)}")
    if include_elapsed:
        elapsed = info.elapsed if info.elapsed > 0 else (time.time() - info.started)
        parts.append(f"elapsed: {int(elapsed)}s")
    return " | ".join(parts)


def check_memory_available(min_gb: float = 4.0, path: str = "/proc/meminfo") -> tuple[bool, float]:
    """Check if enough memory is available to spawn a subagent.

    Reads /proc/meminfo MemAvailable via ``safe_read_file`` (hooks.py)
    and compares against *min_gb*.
    Returns (ok, available_gb).  On read failure returns (True, -1.0)
    to avoid blocking spawns on non-Linux systems.
    """
    try:
        text = safe_read_file(path)
    except PermissionError:
        logger.warning("Memory check blocked: sensitive path %s", path)
        return (True, -1.0)
    except OSError:
        return (True, -1.0)
    try:
        for line in text.splitlines():
            if line.startswith("MemAvailable:"):
                kb = int(line.split()[1])
                avail = kb / (1024 * 1024)
                return (avail >= min_gb, round(avail, 2))
    except (ValueError, IndexError):
        return (True, -1.0)
    return (True, -1.0)


# Process-subtree RSS readers (relocated from the upstream mcp_gateway pool,
# which is absent in this fork). Pure-stdlib /proc walkers: on non-Linux hosts
# every /proc access raises OSError and these degrade to -1 / [] gracefully.
_RSS_SUBTREE_MAX_PROCS = 256


def _single_proc_rss_kb(pid: int) -> int:
    """RSS (KiB) of a single ``pid`` from /proc/<pid>/status, or -1."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def _proc_children(pid: int) -> list[int]:
    """Direct child PIDs of ``pid`` via /proc/<pid>/task/<tid>/children.

    Uses the kernel-provided children list (CONFIG_PROC_CHILDREN), so no
    ``pgrep``/full-table scan. Returns ``[]`` if the file is unavailable.
    """
    kids: list[int] = []
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return kids
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children", encoding="ascii") as fh:
                kids.extend(int(tok) for tok in fh.read().split())
        except (OSError, ValueError):
            continue
    return kids


def _proc_rss_kb(pid: Optional[int]) -> int:
    """Resident set size (KiB) for ``pid`` **and all its descendants**.

    A subagent's kiro-cli process is frequently a thin launcher whose real
    memory lives in a child process. Counting only ``pid``'s own ``VmRSS``
    under-reports the true footprint, so we sum the whole subtree.

    Returns -1 if ``pid`` is falsy or its own status cannot be read; otherwise
    the summed KiB (descendants that vanish mid-walk are simply skipped, so the
    result degrades gracefully to parent-only when ``children`` is unreadable).
    """
    if not pid:
        return -1
    own = _single_proc_rss_kb(pid)
    if own < 0:
        return -1
    total = own
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _RSS_SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                kb = _single_proc_rss_kb(child)
                if kb > 0:
                    total += kb
                nxt.append(child)
        frontier = nxt
    return total


# Legacy hard-coded concurrent cap; also the lower clamp bound for auto-sizing
# so dynamic sizing never regresses below today's behavior.
_LEGACY_DEFAULT_MAX = 3


def _available_memory_gb() -> float:
    """Effective available memory (GB), dispatched per operating system.

    Each OS reports "available" memory through a different, non-portable
    interface, so the probe is a small per-platform branch. Every branch
    returns a best-effort available-GB figure, or ``-1.0`` when this platform
    has no probe yet / the read failed — in which case the caller
    (``compute_max_subagents``) fails open to the legacy default cap.

        • Linux  — ``/proc/meminfo`` ``MemAvailable`` (via ``check_memory_available``),
                   then clamped by cgroup headroom so a container's limit binds.
        • macOS  — reclaimable memory via Mach ``host_statistics64`` (ctypes,
                   in-process, no subprocess); see ``_macos_available_memory_gb``.
                   No cgroups.
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
    # Unsupported platform (e.g. Windows): no probe yet → fail open.
    return -1.0


def _macos_vm_reclaimable_pages() -> Optional[int]:  # pragma: no cover
    """Reclaimable memory in **pages** via Mach ``host_statistics64``, or ``None``.

    macOS-only. Excluded from coverage because the Linux CI fleet cannot execute
    the Mach path; validated live against ``vm_stat`` on Apple silicon (matches
    within live-fluctuation noise). Reads in-process through ``ctypes`` /
    ``libSystem`` — **no subprocess** — so it is safe on the gateway event loop
    and passes the spawn-audit guard.

    Reclaimable ≈ ``free + inactive + speculative + purgeable`` page classes:
    memory that can back a new allocation without swapping (the closest analogue
    to Linux ``MemAvailable``). Wired/active/compressed pages are excluded.
    Returns ``None`` on any failure (non-macOS ``libSystem`` absent, non-zero
    ``kern_return_t``) so the caller falls back to the legacy default.
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    except OSError:
        return None  # not macOS / libSystem unavailable

    natural_t = ctypes.c_uint  # natural_t is 32-bit on macOS
    u64 = ctypes.c_uint64

    # Leading fields of vm_statistics64_data_t (<mach/vm_statistics.h>) in
    # declaration order, so the byte layout matches what the kernel fills. Only
    # free/inactive/speculative/purgeable are read, but the full struct is
    # declared so the element count handed to host_statistics64 is exact.
    class _VMStatistics64(ctypes.Structure):
        _fields_ = [
            ("free_count", natural_t),
            ("active_count", natural_t),
            ("inactive_count", natural_t),
            ("wire_count", natural_t),
            ("zero_fill_count", u64),
            ("reactivations", u64),
            ("pageins", u64),
            ("pageouts", u64),
            ("faults", u64),
            ("cow_faults", u64),
            ("lookups", u64),
            ("hits", u64),
            ("purges", u64),
            ("purgeable_count", natural_t),
            ("speculative_count", natural_t),
            ("decompressions", u64),
            ("compressions", u64),
            ("swapins", u64),
            ("swapouts", u64),
            ("compressor_page_count", natural_t),
            ("throttled_count", natural_t),
            ("external_page_count", natural_t),
            ("internal_page_count", natural_t),
            ("total_uncompressed_pages_in_compressor", u64),
        ]

    HOST_VM_INFO64 = 4  # flavor selector for host_statistics64

    try:
        libc.mach_host_self.restype = ctypes.c_uint
        libc.host_statistics64.restype = ctypes.c_int
        libc.host_statistics64.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(_VMStatistics64),
            ctypes.POINTER(ctypes.c_uint),
        ]
        stats = _VMStatistics64()
        count = ctypes.c_uint(ctypes.sizeof(_VMStatistics64) // ctypes.sizeof(ctypes.c_int))
        kern_return = libc.host_statistics64(
            libc.mach_host_self(),
            HOST_VM_INFO64,
            ctypes.byref(stats),
            ctypes.byref(count),
        )
    except (AttributeError, OSError, ValueError):
        return None
    if kern_return != 0:  # non-zero kern_return_t → failure
        return None

    return (
        stats.free_count
        + stats.inactive_count
        + stats.speculative_count
        + stats.purgeable_count
    )


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
    avail_gb = pages * page_size / (1024 ** 3)
    return round(avail_gb, 2) if avail_gb > 0 else -1.0


# Values at/above this are the kernel's "no limit" sentinel (PAGE_COUNTER_MAX).
_CGROUP_UNLIMITED = 1 << 62


def _read_int_file(path: str) -> int | None:
    """Read a single integer from *path*; None on absence/garbage. 'max' → None."""
    try:
        with open(path, encoding="ascii") as fh:
            txt = fh.read().strip()
    except OSError:
        return None
    if txt == "max":  # cgroup v2 unlimited sentinel
        return None
    try:
        return int(txt)
    except ValueError:
        return None


def _cgroup_available_gb() -> float:
    """Container memory headroom (GB) = limit − current, or -1.0 if unlimited/unknown.

    Reads cgroup v2 (``memory.max``/``memory.current``) then v1
    (``memory.limit_in_bytes``/``memory.usage_in_bytes``). A sentinel-large
    limit means unlimited. Returns -1.0 on unconstrained / non-Linux hosts so
    the caller ignores the clamp (``dynamic-subagent-sizing.md`` §9).
    """
    # cgroup v2
    limit = _read_int_file("/sys/fs/cgroup/memory.max")
    if limit is not None:
        if limit >= _CGROUP_UNLIMITED:
            return -1.0
        current = _read_int_file("/sys/fs/cgroup/memory.current") or 0
        return max(0.0, (limit - current) / (1024 ** 3))
    # cgroup v1
    limit = _read_int_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if limit is not None:
        if limit >= _CGROUP_UNLIMITED:
            return -1.0
        current = _read_int_file("/sys/fs/cgroup/memory/memory.usage_in_bytes") or 0
        return max(0.0, (limit - current) / (1024 ** 3))
    return -1.0  # no cgroup memory controller


def compute_max_subagents(cfg: KiroCrewConfig) -> int:
    """Compute the concurrent sub-agent cap from host memory and CPU.

    Memory- and CPU-symmetric: each resource yields a candidate count from a
    buffered budget divided by a per-agent cost, and the tighter one binds.
    The result is clamped to ``[3, hard_cap]`` — never below the legacy
    default (the per-spawn ``spawn_min_memory_gb`` gate is the real-time
    memory guard), never above the absolute ``subagent_auto_max`` (which
    stands in for the unmodeled LLM-provider concurrency limit).

    Per-agent costs come from the learned cost store (``read_learned_cost``);
    when no learned value exists yet, the configured first-boot fallbacks
    (``subagent_cost_gb`` / ``subagent_cpu_cost_cores``) are used. Fails open to
    the legacy default when memory can't be read (e.g. non-Linux hosts).

    See ``dynamic-subagent-sizing.md`` §3.
    """
    agent = cfg.agent
    # Hard floor of 3 (``_LEGACY_DEFAULT_MAX``): the auto-sized cap never drops
    # below today's behavior even if ``subagent_auto_max`` is somehow < 3 (the
    # config loader clamps it up to 3, but defend here too so the runtime cap is
    # guaranteed >= 3). ``subagent_auto_max`` is the upper ceiling.
    hard_cap = max(_LEGACY_DEFAULT_MAX, agent.subagent_auto_max)
    lo = _LEGACY_DEFAULT_MAX

    avail_gb = _available_memory_gb()
    if avail_gb <= 0:
        # Memory unreadable (non-Linux / read error) — fail open.
        logger.info(
            "dynamic subagent cap = %d (memory unreadable; fail-open to legacy default)",
            lo,
        )
        return lo

    buf = 1.0 - agent.subagent_mem_buffer_pct / 100.0
    mem_cost = read_learned_cost("mem_gb") or agent.subagent_cost_gb or 0.5
    cpu_cost = read_learned_cost("cpu_cores") or agent.subagent_cpu_cost_cores or 1.0
    pool_size = cfg.session.pool_size

    mem_term = math.floor((avail_gb * buf - pool_size * mem_cost) / mem_cost)
    cpu_count = os.cpu_count() or 1
    cpu_term = math.floor((cpu_count * buf) / cpu_cost)

    candidate = min(mem_term, cpu_term)
    result = max(lo, min(candidate, hard_cap))

    # Name the active bound for an explainable startup log (§5.2).
    if candidate >= hard_cap:
        reason = "hard_cap"
    elif candidate <= lo:
        reason = "floor"
    elif mem_term <= cpu_term:
        reason = "mem_term"
    else:
        reason = "cpu_term"
    logger.info(
        "dynamic subagent cap = %d (%s; mem_term=%d, cpu_term=%d, floor=%d, hard_cap=%d)",
        result,
        reason,
        mem_term,
        cpu_term,
        lo,
        hard_cap,
    )
    return result


def resolve_max_subagents(cfg: KiroCrewConfig) -> int:
    """Resolve the effective cap: explicit value when > 0, else auto-compute.

    ``agent.max_subagents == 0`` is the "auto" sentinel that triggers
    :func:`compute_max_subagents`. See ``dynamic-subagent-sizing.md`` §5.1.
    """
    try:
        configured = int(cfg.agent.max_subagents)
    except (AttributeError, TypeError, ValueError):
        configured = _LEGACY_DEFAULT_MAX
    if configured > 0:
        # An explicit pin below the legacy floor (1 or 2) would silently disable
        # auto-sizing AND run below today's default; floor it to 3. 0 stays the
        # auto sentinel. The config loader and dashboard API also enforce this;
        # defend here so a directly-constructed config can't drop the runtime cap
        # below the floor.
        return max(configured, _LEGACY_DEFAULT_MAX)
    return compute_max_subagents(cfg)


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_CPU_SUBTREE_MAX_PROCS = 256


def _parse_cpu_jiffies(stat: bytes) -> int:
    """Sum utime+stime (clock ticks) from raw ``/proc/<pid>/stat`` bytes.

    Splits after the final ``)`` so a ``comm`` containing spaces/parens is
    handled. utime/stime are fields 14/15 (1-indexed) → indices 11/12 of the
    post-comm tokens. Returns 0 on any parse error.
    """
    try:
        rparen = stat.rindex(b")")
        fields = stat[rparen + 2:].split()
        return int(fields[11]) + int(fields[12])
    except (ValueError, IndexError):
        return 0


def _proc_cpu_jiffies(pid: int) -> int:
    """utime+stime (clock ticks) for a single pid, 0 on error."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return _parse_cpu_jiffies(fh.read())
    except OSError:
        return 0


def _subtree_cpu_jiffies(pid: int) -> int:
    """Sum utime+stime across ``pid`` and its descendants (clock ticks).

    Walks the same kernel children list as ``pool._proc_rss_kb`` so the CPU
    subtree matches the RSS subtree.
    """
    total = _proc_cpu_jiffies(pid)
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _CPU_SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                total += _proc_cpu_jiffies(child)
                nxt.append(child)
        frontier = nxt
    return total


def validate_cwd(cwd: str, allowed_roots: list[str]) -> tuple[str, str]:
    """Validate a caller-supplied ``cwd`` for ``spawn_run``.

    Resolves symlinks and verifies the path is an existing directory under at
    least one entry in ``allowed_roots``. Empty ``allowed_roots`` disables the
    feature — any non-empty ``cwd`` is rejected.

    Args:
        cwd: Caller-supplied absolute path (may contain ``~``).
        allowed_roots: Permitted root paths from config (may contain ``~``).

    Returns:
        ``(resolved_cwd, error)``. On success ``error`` is empty and
        ``resolved_cwd`` is the canonical absolute path (realpath-resolved).
        On failure ``error`` is a reason string and ``resolved_cwd`` is empty.
    """
    if not cwd:
        return ("", "")
    if not allowed_roots:
        return ("", "cwd override is disabled (subagent_cwd_allowed_roots is empty)")
    try:
        expanded = os.path.expanduser(cwd)
        if not os.path.isabs(expanded):
            return ("", "cwd must be an absolute path")
        resolved = os.path.realpath(expanded)
    except (OSError, ValueError) as exc:
        return ("", f"cwd resolution failed: {exc}")
    if not os.path.isdir(resolved):
        return ("", "cwd does not exist or is not a directory")
    resolved_roots = [os.path.realpath(os.path.expanduser(r)) for r in allowed_roots]
    for root in resolved_roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return (resolved, "")
    return ("", f"cwd is not under any allowed root: {allowed_roots}")


_SYSTEM_PREFIX = (
    "You are a focused sub-agent. Complete the following task concisely. "
    "Do NOT create other agents. Report your result directly.\n"
    "IMPORTANT: Do NOT narrate your own process, failures, retries, or "
    "orchestration decisions. The user does not care how you got the answer. "
    "Do NOT include [OPTIONS: ...] tags. Do NOT use the AskUserQuestion tool. "
    "Only output meaningful, actionable results. Never output greetings or filler.\n\n"
)


@dataclass
class SubagentInfo:
    """Metadata for a running subagent."""

    id: str
    task: str
    started: float = field(default_factory=time.time)
    done: bool = False
    result: str = ""
    result_path: str = ""
    result_truncated: bool = False  # completion-event copy dropped content → summary+path
    error: str = ""
    parent_session_key: str = ""
    agent: str = ""
    approval_mode: str = ""  # "auto" to skip tool approvals in the subagent session
    silent: bool = False  # suppress completion notification (dashboard + Slack)
    turns: int = 0
    last_tool: str = ""
    max_turns: int = 0
    reaped: bool = False
    streaming_text: str = ""
    elapsed: float = 0.0
    _raw_task: str = ""  # unredacted task for kiro-cli execution prompt
    # CC-specific overrides (ignored for ACP)
    model: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    bare: bool = False
    # Optional subprocess cwd override. When set, the subagent kiro-cli/claude-code
    # process launches here instead of the default ``subagent_<id>`` sandbox, so
    # cwd-relative resource globs (``.kiro/steering/**/*.md``, ``AGENTS.md``,
    # ``CLAUDE.md``) resolve against this directory. Validated on spawn against
    # ``AgentConfig.subagent_cwd_allowed_roots``.
    cwd: str = ""
    _pid: int | None = None  # PID of kiro-cli child process, for tombstone diagnostics
    # Wall-clock (time.time) when _run_inner actually began executing. Distinct
    # from ``started`` (set at registration): a subagent may sit in ``_agents``
    # awaiting spawn approval for an arbitrary time before execution begins. The
    # startup watchdog measures from THIS timestamp so it never reaps an agent
    # that is merely waiting for approval. None until execution starts.
    _exec_started: float | None = None
    # Learned-cost high-water marks (dynamic-subagent-sizing.md §4.1), sampled
    # periodically by the reaper loop and folded into the cost store at exit.
    peak_rss_gb: float = 0.0
    peak_cpu_cores: float = 0.0
    _cpu_jiffies_prev: int = 0  # last subtree utime+stime sample (clock ticks)
    _cpu_sample_ts: float = 0.0  # monotonic time of the last CPU sample
    # Session sharing — when True, this subagent runs as a session on the
    # parent's shared AcpRuntime instead of its own process. Cleanup skips
    # release/reset (no entry in SessionManager) and instead calls shutdown()
    # on the _shared_provider directly.
    _session_sharing: bool = False
    _shared_provider: Any = None  # AcpSessionProvider when _session_sharing=True


# Callback: (subagent_info) -> None
AnnounceCallback = Callable[[SubagentInfo], Awaitable[None]]

# Event callback: (event_type, info, extra_data) -> None
SubagentEventCallback = Callable[[str, "SubagentInfo", dict], Awaitable[None]]


class ToolApprovalCallback(Protocol):
    async def __call__(self, event: LLMEvent, parent_session_key: str = "") -> bool:
        pass


class SpawnApprovalCallback(Protocol):
    async def __call__(
        self, request_id: str, description: str, parent_session_key: str = ""
    ) -> bool:
        pass


class SubagentManager:
    """Spawn and track isolated background agents."""

    def __init__(
        self,
        sessions: SessionManager,
        ctx_builder: ContextBuilder,
        on_done: AnnounceCallback | None = None,
        max_concurrent: int = _MAX_CONCURRENT,
        default_turn_limit: int = _TURN_LIMIT,
        default_timeout: int = _TIMEOUT_SECS,
        startup_timeout: int = _STARTUP_TIMEOUT_SECS,
        on_tool_approval: ToolApprovalCallback | None = None,
        on_tool_approval_factory: (
            Callable[["SubagentInfo"], Callable[[LLMEvent], Awaitable[bool]]] | None
        ) = None,
        on_spawn_approval: SpawnApprovalCallback | None = None,
        is_yolo: Callable[[], bool] | None = None,
        on_event: SubagentEventCallback | None = None,
        completion_keep: str = "head",
        completion_keep_chars: int = COMPLETION_KEEP_DEFAULT_CHARS,
    ):
        self._sessions = sessions
        self._ctx_builder = ctx_builder
        self._on_done = on_done
        self._max_concurrent = max_concurrent
        self._default_turn_limit = default_turn_limit
        self._default_timeout = default_timeout if default_timeout > 0 else _TIMEOUT_SECS
        self._startup_deadline = startup_timeout if startup_timeout > 0 else _STARTUP_TIMEOUT_SECS
        self._on_tool_approval = on_tool_approval  # fallback for non-auto sessions
        self._on_tool_approval_factory = on_tool_approval_factory
        self._on_spawn_approval = on_spawn_approval
        self._is_yolo = is_yolo
        self._on_event = on_event
        self._completion_keep = completion_keep
        self._completion_keep_chars = completion_keep_chars
        self._running_count = 0
        self._last_spawn_ts: float = 0.0  # monotonic time of the last actual start (stagger gate)
        self.hook_store: Any = None  # Optional ScriptHookStore, set by server.py
        self._agents: dict[str, SubagentInfo] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # Queued spawns store the FULL spawn() kwarg set (not just a 5-tuple), so a
        # drained spawn preserves approval_mode / silent / model / allowed_tools / bare —
        # dropping them made a queued headless/auto spawn hit the deny-by-default gate and
        # a queued silent spawn emit output. See _drain_queue.
        self._queue: list[dict[str, Any]] = []
        self._reaper_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Cache global approval_mode at init to avoid disk I/O on every
        # parentless spawn (cron, webhooks).
        try:
            self._global_approval_mode = KiroCrewConfig.load().agent.approval_mode
        except Exception:
            logger.warning(
                "Failed to load KiroCrewConfig for approval_mode; defaulting to interactive",
                exc_info=True,
            )
            self._global_approval_mode = ""
        # Retention window (seconds) for a delivered subagent's result.txt before
        # the reaper prunes it — the parent's grace window to read the full
        # transcript (spawn_status / read / grep) after the completion event.
        try:
            self._result_ttl_secs = int(
                KiroCrewConfig.load().agent.subagent_result_ttl_secs
            )
        except Exception:
            self._result_ttl_secs = 3600
        # Spawn stagger interval — bounds the cold-start ramp rate so a high cap
        # never bursts (dynamic-subagent-sizing.md §5.3).
        try:
            self._spawn_stagger_secs = max(
                0.0, float(KiroCrewConfig.load().agent.subagent_spawn_stagger_secs)
            )
        except Exception:
            self._spawn_stagger_secs = 2.0

    def update_completion_keep(self, mode: str, max_chars: int) -> None:
        """Update the live completion-keep mode and char budget.

        Called from ``api_kirocrew_config_patch`` after the user changes
        ``agent.completion_keep`` or ``agent.completion_keep_chars`` from
        the Settings UI. The values are read once per subagent at
        completion time (``apply_completion_keep`` call site), so swapping
        them here takes effect for the next subagent to finish — including
        ones already running. No torn-read possible under asyncio: both
        reads happen in the same synchronous block.

        ``mode`` is validated by ``_validated_completion_keep`` at config
        load; this setter is intentionally permissive about ``max_chars``
        so the loader / handler stays the validation choke-point.
        """
        self._completion_keep = mode
        self._completion_keep_chars = max_chars

    @staticmethod
    async def _approve_and_log(
        client,
        request_id: str | int,
        session_key: str,
        event: LLMEvent,
        *,
        metadata: dict | None = None,
    ) -> None:
        await client.approve_tool(request_id)
        sel().log_tool_invocation(
            session_key=session_key,
            source="subagent",
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome="auto_approved" if metadata and metadata.get("reason") else "approved",
            request_id=request_id,
            metadata=metadata,
        )

    @staticmethod
    async def _reject_and_log(
        client,
        request_id: str | int,
        session_key: str,
        event: LLMEvent,
        *,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        await client.reject_tool(request_id)
        sel().log_tool_invocation(
            session_key=session_key,
            source="subagent",
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome="denied" if error else "rejected",
            request_id=request_id,
            error=error or "",
            metadata=metadata,
        )

    def start_reaper(self) -> None:
        """Start the periodic reaper loop.  Call once after the event loop is running."""
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())
            # One-shot orphan reconciliation on startup
            self._reconcile_task = asyncio.create_task(self._reconcile_orphans())

    async def _reconcile_orphans(self) -> None:
        """Scan for orphaned agent folders from a prior gateway run.

        For each orphan (folder with state.json but no tombstone.json
        and not tracked in ``_agents``):
        - PID alive → SIGKILL, tombstone (gateway_restart)
        - PID dead + result → tombstone (gateway_restart, delivered)
        - PID dead + no result → tombstone (gateway_restart, notification_pending)
        """
        try:

            orphans = list_orphans()
            if not orphans:
                return
            logger.info("Reconciling %d orphaned subagent(s)", len(orphans))
            processed = 0
            for state in orphans:
                agent_id = state.get("id", "")
                if not agent_id or agent_id in self._agents:
                    continue  # tracked in current run, skip
                try:
                    pid = state.get("pid")
                    has_result = False
                    try:

                        rp = _agent_dir(agent_id) / "result.txt"
                        has_result = rp.exists() and rp.stat().st_size > 0
                    except OSError:
                        pass

                    recovery = "undeliverable"
                    if pid and self._is_pid_alive(pid):
                        # Use pid_recorded_at (when PID was actually written) instead of
                        # started (folder creation time) to avoid false negatives under load
                        pid_recorded_at = state.get("pid_recorded_at", state.get("started", 0))
                        if self._is_orphan_process(pid, pid_recorded_at):
                            self._kill_orphan_pid(pid)
                            try:
                                sel().log_tool_invocation(
                                    session_key=f"subagent:{agent_id}",
                                    source="subagent",
                                    tool_name="orphan_reconcile_kill",
                                    outcome="killed",
                                    metadata={"subagent_id": agent_id, "pid": pid},
                                )
                            except Exception:
                                logger.debug("SEL audit failed for orphan %s", agent_id)
                        recovery = "result_available" if has_result else "notification_pending"
                    elif has_result:
                        recovery = "result_available"
                    else:
                        recovery = "notification_pending"

                    try:
                        write_tombstone(
                            agent_id,
                            cause="gateway_restart",
                            recovery_action=recovery,
                            pid=pid,
                            turns=state.get("turns", 0),
                            last_tool=state.get("last_tool", ""),
                        )
                    except Exception:
                        logger.debug("Failed to tombstone orphan %s", agent_id, exc_info=True)

                    # Clean up session files for the orphaned agent
                    session_id = state.get("session_id", "")
                    if session_id:
                        try:
                            provider = state.get("provider", "acp")
                            cwd = state.get("cwd", "")
                            _cleanup_session_files_sync(session_id, provider, cwd=cwd)
                        except Exception:
                            logger.debug(
                                "Session cleanup failed for orphan %s", agent_id, exc_info=True
                            )

                    logger.info(
                        "Reconciled orphan %s: recovery=%s, pid=%s, has_result=%s",
                        agent_id, recovery, pid, has_result,
                    )
                    # Notify user about the orphaned agent
                    try:
                        await self._notify_orphan(agent_id, state, recovery, has_result)
                    except Exception:
                        logger.debug("Notification failed for orphan %s", agent_id, exc_info=True)
                except Exception:
                    logger.warning("Failed to reconcile orphan %s", agent_id, exc_info=True)

                # Rate limit: yield to event loop every 50 entries
                processed += 1
                if processed % 50 == 0:
                    await asyncio.sleep(0)
        except Exception:
            logger.warning("Orphan reconciliation failed", exc_info=True)

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a PID is still running."""
        # os.kill(pid, 0) would terminate the process on Windows — probe instead.
        return platform_compat.pid_exists(pid)

    @staticmethod
    def _is_orphan_process(pid: int, spawned_at: float) -> bool:
        """Check if PID belongs to the original subagent (not a recycled PID).

        Compares /proc/{pid} creation time against the recorded spawn time.
        Returns False if the process was created after the agent was spawned
        (indicating PID reuse).
        """
        try:
            proc_stat = os.stat(f"/proc/{pid}")
            # Process was created before or around the time we spawned the agent
            return proc_stat.st_ctime <= spawned_at + 2.0
        except (FileNotFoundError, OSError):
            return False

    @staticmethod
    def _kill_orphan_pid(pid: int) -> None:
        """Best-effort SIGKILL of an orphaned process."""
        try:
            platform_compat.kill_pid(pid, platform_compat.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    async def _notify_orphan(
        self, agent_id: str, state: dict, recovery: str, has_result: bool
    ) -> None:
        """Notify user about an orphaned subagent.

        1. Try session injection if parent session still exists
        2. Fall back to Slack DM via send_message MCP tool
        """
        task_preview = (state.get("task", "") or "")[:100]
        parent_session = state.get("parent_session", "")

        result_path = str(_agent_dir(agent_id) / "result.txt")

        if has_result:
            msg = (
                f"[Subagent completion event]\n"
                f"Agent `{agent_id}` ⚠️ orphaned by gateway restart\n"
                f"Task: {task_preview}\n"
                f"Result saved at: `{result_path}`\n"
                f"Use the read tool to retrieve it."
            )
        else:
            msg = (
                f"[Subagent completion event]\n"
                f"Agent `{agent_id}` ❌ lost to gateway restart\n"
                f"Task: {task_preview}\n"
                f"No result was captured before the restart."
            )

        # Redact before any delivery path (injection or Slack DM)
        msg = _redact(msg)

        # Try session injection first
        if parent_session.startswith("dashboard:"):
            try:
                injected = await self._try_inject_orphan_notification(parent_session, msg)
                if injected:
                    # Update tombstone recovery_action
                    try:
                        write_tombstone(
                            agent_id,
                            cause="gateway_restart",
                            recovery_action="delivered",
                            pid=state.get("pid"),
                            turns=state.get("turns", 0),
                            last_tool=state.get("last_tool", ""),
                        )
                    except Exception:
                        pass
                    return
            except Exception:
                logger.debug("Injection failed for orphan %s", agent_id, exc_info=True)

        # Fallback: Slack DM
        try:
            await self._send_orphan_slack_dm(msg)
        except Exception:
            logger.debug("Slack DM fallback failed for orphan %s", agent_id, exc_info=True)

    async def _try_inject_orphan_notification(self, parent_session: str, msg: str) -> bool:
        """Try to inject a message into the parent dashboard session.

        Returns True if injection succeeded.
        """
        # This hooks into the existing dashboard session injection mechanism.
        # For now, return False to always fall through to Slack DM.
        # Full injection requires access to the dashboard slot, which is
        # wired up at a higher level (gateway.py). This will be connected
        # when the notification plumbing is integrated.
        return False

    async def _send_orphan_slack_dm(self, msg: str) -> None:
        """Send orphan notification via Slack DM (best-effort).

        TODO: Wire into the gateway's Slack client once available at this layer.
        Currently logs the notification for debugging.
        """
        logger.warning("Orphan notification (Slack DM pending): %s", msg[:200])

    def _live_shared_count(self, pid: int | None) -> int:
        """Count live session-shared subagents sharing runtime *pid* (>= 1).

        Used to average the shared AcpRuntime's measured RSS/CPU across the
        sessions currently running inside it, so each shared subagent is charged
        an empirical per-session share rather than the whole process.
        """
        if not pid:
            return 1
        n = sum(
            1
            for a in self._agents.values()
            if not a.done and a._session_sharing and a._pid == pid
        )
        return n if n > 0 else 1

    def _sample_live_costs(self) -> None:
        """Sample high-water RSS/CPU for each live agent (reaper-loop piggyback).

        Updates per-run peaks on ``SubagentInfo`` (dynamic-subagent-sizing.md
        §4.1). RSS is the subtree VmRSS in GB; CPU is cores used since the last
        sample = Δ(utime+stime jiffies) / (CLK_TCK × Δt). The first sample only
        seeds the CPU baseline (no delta yet). Best-effort: a dead/unreadable
        pid is simply skipped.
        """
        now = time.monotonic()
        for info in list(self._agents.values()):
            if info.done or not info._pid:
                continue
            # Session-shared subagents run inside the parent's AcpRuntime process;
            # every sharing subagent reports the SAME runtime PID, so naive
            # per-PID sampling would attribute the whole shared process to each
            # of them. Instead attribute the runtime's measured RSS/CPU divided
            # by the number of concurrently-live shared sessions on that PID — an
            # empirical per-session average, not a guessed constant
            # (dynamic-subagent-sizing.md §session-sharing cost model).
            if info._session_sharing:
                shared_n = self._live_shared_count(pid_owner := info._pid)
                rss_kb = _proc_rss_kb(pid_owner)
                if rss_kb > 0 and shared_n > 0:
                    gb = (rss_kb / (1024 * 1024)) / shared_n
                    if gb > info.peak_rss_gb:
                        info.peak_rss_gb = gb
                jiffies = _subtree_cpu_jiffies(pid_owner)
                if info._cpu_sample_ts > 0.0 and jiffies >= info._cpu_jiffies_prev and shared_n > 0:
                    dt = now - info._cpu_sample_ts
                    if dt > 0:
                        cores = ((jiffies - info._cpu_jiffies_prev) / (_CLK_TCK * dt)) / shared_n
                        if cores > info.peak_cpu_cores:
                            info.peak_cpu_cores = cores
                info._cpu_jiffies_prev = jiffies
                info._cpu_sample_ts = now
                continue
            pid = info._pid
            rss_kb = _proc_rss_kb(pid)
            if rss_kb > 0:
                gb = rss_kb / (1024 * 1024)
                if gb > info.peak_rss_gb:
                    info.peak_rss_gb = gb
            jiffies = _subtree_cpu_jiffies(pid)
            if info._cpu_sample_ts > 0.0 and jiffies >= info._cpu_jiffies_prev:
                dt = now - info._cpu_sample_ts
                if dt > 0:
                    cores = (jiffies - info._cpu_jiffies_prev) / (_CLK_TCK * dt)
                    if cores > info.peak_cpu_cores:
                        info.peak_cpu_cores = cores
            info._cpu_jiffies_prev = jiffies
            info._cpu_sample_ts = now

    def _record_cost(self, info: SubagentInfo) -> None:
        """Persist this run's high-water RSS/CPU to the learned-cost store."""
        if info.peak_rss_gb <= 0 and info.peak_cpu_cores <= 0:
            return  # never sampled (e.g. finished before the first reaper sweep)
        try:
            append_cost_sample(info.agent, info.peak_rss_gb, info.peak_cpu_cores)
        except Exception:
            logger.debug("Failed to record subagent cost for %s", info.id, exc_info=True)

    async def _reaper_loop(self) -> None:
        """Periodically force-kill subagents that exceed the timeout.

        Defense-in-depth: catches cases where ``asyncio.wait_for`` in
        ``_run()`` fails to fire (event-loop saturation, orphaned tasks,
        or ``reset()`` hanging in the finally block).
        """
        try:
            compact_cost_log()  # startup FIFO trim (§4.2)
        except Exception:
            logger.debug("Reaper: startup cost-log compaction failed", exc_info=True)
        while True:
            await asyncio.sleep(_REAPER_INTERVAL)
            now = time.time()
            self._sample_live_costs()
            try:
                compact_cost_log()  # periodic FIFO trim (also bounds a long-running gateway)
            except Exception:
                logger.debug("Reaper: cost-log compaction failed", exc_info=True)
            for agent_id, info in list(self._agents.items()):
                if info.done:
                    continue
                elapsed = now - info.started
                # Startup watchdog: a subagent that entered execution but is
                # still on turn 0 with no runtime PID after the startup window
                # is wedged in startup (e.g. a hung provider/ACP handshake that
                # never launches the child process). Reap it fast with a clear
                # "failed to start" error instead of burning the full deadline
                # and surfacing a misleading 30-minute turn-0 timeout.
                if self._is_startup_stalled(info, now):
                    logger.warning(
                        "Reaper: subagent %s failed to start within %ds "
                        "(turn 0, no runtime launched), force-killing",
                        agent_id,
                        self._startup_deadline,
                    )
                    try:
                        await self._force_reap(
                            agent_id,
                            info,
                            now - (info._exec_started or now),
                            reason="startup_timeout",
                        )
                    except Exception:
                        logger.exception("Reaper: failed to reap %s", agent_id)
                    continue
                if elapsed <= self._default_timeout:
                    continue
                logger.warning(
                    "Reaper: subagent %s exceeded %ds (ran %.0fs), force-killing",
                    agent_id,
                    self._default_timeout,
                    elapsed,
                )
                try:
                    await self._force_reap(agent_id, info, elapsed)
                except Exception:
                    logger.exception("Reaper: failed to reap %s", agent_id)

            # Prune stale tombstoned folders (>7 days old)
            try:
                pruned = await asyncio.get_running_loop().run_in_executor(
                    maintenance_executor(),
                    prune_stale_tombstones,
                    7,
                    self._result_ttl_secs,
                )
                if pruned:
                    logger.info("Reaper: pruned %d stale tombstone(s)", pruned)
            except Exception:
                logger.debug("Reaper: tombstone pruning failed", exc_info=True)

    def _is_startup_stalled(self, info: SubagentInfo, now: float) -> bool:
        """True if a subagent is wedged in startup and should be reaped early.

        A subagent qualifies only once it has actually entered execution
        (``_exec_started`` set by ``_run_inner``) yet has launched no runtime
        (``_pid is None``) and produced no turn (``turns == 0``) within
        ``_startup_deadline`` seconds. Keying on ``_exec_started`` — not the
        registration timestamp ``started`` — means an agent merely awaiting
        spawn approval (never entered ``_run_inner``) is never caught here.
        """
        exec_started = info._exec_started
        if exec_started is None:
            return False
        return (
            info.turns == 0 and info._pid is None and (now - exec_started) > self._startup_deadline
        )

    async def _force_reap(
        self, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
    ) -> None:
        """Kill a subagent's session process and mark it done."""
        session_key = f"subagent:{agent_id}"

        if info._session_sharing:
            # Session-sharing subagent: NEVER SIGKILL the shared runtime —
            # the parent session owns it and other co-tenants may be active.
            # Conservative approach: shut down only this subagent's provider
            # handle, leaving the shared runtime intact.
            runtime_pid = info._pid
            logger.info(
                "Reaper: conservative shutdown for session-sharing %s — "
                "runtime pid=%s kept alive (shared runtime, never SIGKILL)",
                agent_id, runtime_pid,
            )
            try:
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="subagent",
                    tool_name="smart_hard_kill",
                    outcome="conservative-shutdown",
                    resources=f"runtime_pid={runtime_pid}",
                    metadata={
                        "subagent_id": agent_id,
                        "runtime_pid": runtime_pid,
                        "decision": "session-sharing-never-kill",
                    },
                )
            except Exception:
                logger.debug("SEL audit for conservative shutdown failed", exc_info=True)
            # Shutdown the shared provider handle only
            try:
                if info._shared_provider:
                    await info._shared_provider.shutdown()
            except Exception:
                logger.debug(
                    "Reaper: shared session shutdown failed for %s", agent_id, exc_info=True
                )
        else:
            # Kill the process FIRST so the pipe unblocks, then cancel the task.
            try:
                await asyncio.wait_for(self._sessions.reset(session_key), timeout=_RESET_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Reaper: reset hung for %s, attempting SIGKILL", agent_id)
                await self._sigkill_session(session_key)
            except Exception:
                logger.exception("Reaper: reset failed for %s", agent_id)

        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()

        freed_slot = False
        if not info.done:
            info.done = True
            if not info.error:
                if reason == "startup_timeout":
                    info.error = f"Failed to start within {self._startup_deadline}s (no runtime launched, no turn produced) [{_timeout_context(info, include_elapsed=False)}]"
                else:
                    info.error = f"Reaped after {int(elapsed)}s (exceeded {self._default_timeout}s deadline) [{_timeout_context(info, include_elapsed=False)}]"
            self._running_count = max(0, self._running_count - 1)
            freed_slot = True
            Stats().inc_subagent_failed()
            self._write_tombstone(info, reason or "reaped")
            self._record_cost(info)
        info.reaped = True
        # A reap/cancel frees a slot but — unlike normal completion (the `if not
        # info.reaped` finally block in _run) — does NOT otherwise pump the queue.
        # Without this, queued spawns sit stranded until an unrelated agent finishes
        # or a new spawn arrives. Drain here so a freed slot is used immediately.
        if freed_slot:
            self._drain_queue()

        try:
            sel().log_tool_invocation(
                session_key=session_key,
                source="subagent",
                tool_name="reaper_force_kill",
                outcome="reaped",
                metadata={
                    "subagent_id": agent_id,
                    "session_key": session_key,
                    "elapsed": int(elapsed),
                },
            )
        except Exception:
            logger.exception("Reaper: SEL audit failed for %s", agent_id)

        try:
            self._sessions.release(session_key, cleanup=True)
        except Exception:
            logger.warning("Reaper: release failed for %s", agent_id, exc_info=True)

        # Fire WS event immediately so Activity Viewer updates
        # before the slow _on_done path (stream_and_collect).
        info.elapsed = elapsed
        await self._fire_event(
            "subagent_done",
            info,
            {
                "elapsed": elapsed,
                "error": _redact(info.error) if info.error else None,
                "task": _redact(info.task),
                "agent": _redact(info.agent),
                "result": _done_result(info.result),
            },
        )

        if self._on_done:
            try:
                await asyncio.wait_for(self._on_done(info), timeout=_ON_DONE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error(
                    "Reaper: completion injection timed out for %s after %.0fs",
                    agent_id,
                    _ON_DONE_TIMEOUT,
                )
                try:
                    await self._sessions.reset(info.parent_session_key)
                except Exception:
                    logger.debug(
                        "Reaper: failed to reset parent session %s",
                        info.parent_session_key,
                        exc_info=True,
                    )
                self.notify_injection_failed(
                    info, reason=f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s (reaper)"
                )
            except Exception:
                logger.exception("Reaper: announce failed for %s", agent_id)

        # Truncate retained text AFTER _on_done to preserve full output for result injection
        if len(info.streaming_text) > 10_000:
            info.streaming_text = info.streaming_text[:10_000] + "\n…(truncated)"

    async def _sigkill_session(self, session_key: str) -> None:
        """Best-effort SIGKILL when graceful reset hangs.

        Uses killpg to kill the entire process group, then sweeps
        escaped children in different PGIDs (MCP servers).

        Async so the Windows ``taskkill`` spawn offloads to
        :func:`kiro_crew.executors.subprocess_executor` via
        :func:`platform_compat.kill_process_tree_async` / ``kill_pid_async``
        instead of blocking the reaper loop's event loop for the duration of
        ``taskkill.exe``.
        """
        try:
            # circular import: subagent → acp.client → session → subagent
            from kiro_crew.acp.client import (
                _capture_child_records,
                _get_child_pids,
                _is_our_child,
                _kill_escaped_children,
            )

            session = self._sessions._sessions.get(session_key)
            if not session:
                return
            client = getattr(session.provider, "_client", None)
            raw_pid = getattr(client, "_pid", None) if client else None
            pid = raw_pid if isinstance(raw_pid, int) else None
            if not pid:
                return
            # Snapshot child tree before killing — children in different
            # PGIDs survive killpg. macOS pgrep/ps spawns are offloaded to
            # subprocess_executor to keep the reaper loop responsive
            loop = asyncio.get_running_loop()
            raw_children = getattr(client, "_child_pids", None)
            child_pids: dict = (
                dict(raw_children) if isinstance(raw_children, dict) else {}
            )
            fresh = await loop.run_in_executor(subprocess_executor(), _get_child_pids, pid)
            new_pids = [p for p in fresh if p not in child_pids]
            if new_pids:
                child_pids.update(
                    await loop.run_in_executor(
                        subprocess_executor(), _capture_child_records, new_pids
                    )
                )
            # Validate PID hasn't been recycled before killing.
            original_start = getattr(client, "_start_time", None)
            if original_start is None:
                logger.debug("Reaper: PID %d already dead for %s", pid, session_key)
                await loop.run_in_executor(
                    subprocess_executor(), _kill_escaped_children, child_pids
                )
                return
            if not await loop.run_in_executor(
                subprocess_executor(), _is_our_child, pid, original_start
            ):
                logger.warning("Reaper: PID %d recycled for %s, skipping killpg", pid, session_key)
                stored = dict(raw_children) if isinstance(raw_children, dict) else {}
                await loop.run_in_executor(
                    subprocess_executor(), _kill_escaped_children, stored
                )
                return
            # Kill the entire process group first
            logger.warning(
                "Reaper: killpg for PID %d (%d children) for %s",
                pid,
                len(child_pids),
                session_key,
            )
            try:
                # Async variants offload Windows taskkill to
                # subprocess_executor so the reaper loop never blocks the
                # event loop on taskkill.exe.
                await platform_compat.kill_process_tree_async(
                    pid, platform_compat.SIGKILL
                )
            except ValueError:
                # Guard refused the pid outright (non-int/reserved) — nothing
                # safe to signal. Mirrors CronService._sigkill_session so a
                # broadcast-guard refusal is a clean log line, not the noisy
                # generic `except Exception` traceback below.
                logger.error(
                    "Reaper: kill guard refused pid %r for %s", pid, session_key
                )
            except (ProcessLookupError, OSError):
                try:
                    await platform_compat.kill_pid_async(
                        pid, platform_compat.SIGKILL
                    )
                except (ProcessLookupError, OSError):
                    pass
            # Sweep children that escaped to different PGIDs
            await loop.run_in_executor(
                subprocess_executor(), _kill_escaped_children, child_pids
            )
        except Exception:
            logger.exception("Reaper: SIGKILL failed for %s", session_key)

    def notify_injection_failed(
        self, info: SubagentInfo, reason: str = "delivery timed out"
    ) -> None:
        """Notify UI and queue failure for LLM when injection times out.

        Appends a synthetic error to the dashboard slot (UI) and queues a
        failure message into ``slot._pending_subagent_failures`` so the LLM
        learns about the failure on the next ``_run_chat`` turn and can read
        the result from disk if needed.
        """
        try:
            parent_key = info.parent_session_key
            if not parent_key.startswith("dashboard:"):
                return
            slot_name = parent_key.removeprefix("dashboard:")

            # Build failure message the LLM will see on next turn
            task_preview = _redact((info.task or "")[:100])
            result_hint = ""
            if info.result_path:
                try:
                    size = os.path.getsize(info.result_path)
                    size_str = f"{size:,} bytes"
                except OSError:
                    size_str = ""
                result_hint = (
                    f"\nResult saved at: {info.result_path}"
                    + (f" ({size_str})" if size_str else "")
                    + "\nUse the read tool to retrieve it if needed."
                )
            failure_msg = (
                f"[Subagent completion event]\n"
                f"Agent `{info.id}` ❌ {reason}\n"
                f"Task: {task_preview}\n"
                f"The agent finished but result delivery timed out.{result_hint}"
            )

            # Queue for LLM context drain on next _run_chat
            if self._on_event:
                _task = asyncio.ensure_future(
                    self._fire_event(
                        "subagent_injection_failed",
                        info,
                        {
                            "error": reason,
                            "slot": slot_name,
                            "failure_msg": failure_msg,
                        },
                    )
                )
                _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except Exception:
            logger.debug("notify_injection_failed failed for %s", info.id, exc_info=True)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        return self._running_count

    def running_agents_for(self, parent_key: str) -> list[dict]:
        """Return summary dicts for agents belonging to *parent_key*."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        def _r(s: str) -> str:
            s, _ = redact_exfiltration_urls(s)
            s, _ = redact_credentials(s)
            return s

        return [
            {
                "id": a.id,
                "task": _r(a.task[:80]),
                "agent": _r(a.agent),
                "turns": a.turns,
                "last_tool": _r(a.last_tool),
                "startedAt": a.started,
            }
            for a in self._agents.values()
            if not a.done and a.parent_session_key == parent_key
        ]

    def spawn(
        self,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        max_turns: int = 0,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        bare: bool = False,
        cwd: str = "",
        approval_mode: str | None = None,
        silent: bool = False,
    ) -> SubagentInfo | None:
        """Spawn a subagent for *task*.

        Approval priority (first match wins):

        1. YOLO mode → immediate execution
        2. ``approval_mode="auto"`` from caller → immediate execution
        3. ``auto_approve_subagent_spawn`` config → auto-approved execution
        4. ``on_spawn_approval`` callback → interactive approval
        5. Otherwise → rejected

        When ``approval_mode="auto"`` is set, it has two effects:
        - Skips the spawn approval gate (this method)
        - Sets the subagent's session-level tool approval policy to
          "auto" in ``_run_inner()``, meaning all tool calls within
          the subagent are auto-approved for its entire lifetime.

        This dual behavior is intentional for headless callers (e.g.
        Mochi bg agent) that have no UI to respond to approval prompts.
        The parameter is only accepted via the internal ``POST /api/spawn``
        endpoint (requires X-Internal-Secret), not from LLM tool calls.

        Args:
            task (str): The prompt/task description for the subagent.
            parent_session_key (str): Session key of the caller.
            agent (str): Agent name override (default: "kirocrew").
            model (str): Model override for CC provider (ignored for ACP).
            allowed_tools (list): Tool allowlist for CC provider (ignored for ACP).
            bare (bool): Launch CC in bare mode (ignored for ACP).
            cwd (str): Optional absolute path where the subagent subprocess
                launches instead of the default ``subagent_<id>`` sandbox.
                Validated against ``AgentConfig.subagent_cwd_allowed_roots``;
                rejected spawns return a done ``SubagentInfo`` with ``error``
                set. Enables cwd-relative resource globs (``AGENTS.md``,
                ``.kiro/steering``, ``CLAUDE.md``) to resolve correctly.
            approval_mode (str | None): "auto" to skip spawn gate and
                set session-level auto-approve.  Only honored from
                authenticated internal callers (X-Internal-Secret).
            silent (bool): Suppress completion notifications.

        Returns:
            SubagentInfo | None: Agent metadata, or None if at capacity.
        """
        # --- Task guard: refuse empty/whitespace-only tasks (defense in depth).
        # The HTTP handler (api_spawn) and MCP tool schemas validate too, but
        # direct Python callers reach this choke point unvalidated. An empty
        # task produces a useless subagent and a blank Activity card. Must run
        # BEFORE the redaction below, which would raise on a None task. ---
        if not task or not task.strip():
            logger.warning(
                "Subagent spawn refused: empty task (parent=%s)", parent_session_key
            )
            # Audit is best-effort: the rejection must be returned even if
            # SEL is unavailable (a graceful refusal must not become an
            # unhandled exception in api_spawn / MCP tool callers).
            try:
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_empty_task",
                    metadata={"agent": agent},
                )
            except Exception:
                logger.debug("SEL audit failed for empty-task rejection", exc_info=True)
            return SubagentInfo(
                id=uuid.uuid4().hex[:8],
                task="",
                agent=agent,
                done=True,
                error="spawn refused: task must be a non-empty string",
            )

        # --- Redact task once for all SubagentInfo storage (raw task kept for kiro-cli prompt) ---
        _redacted_task = redact_credentials(redact_exfiltration_urls(task)[0])[0]

        # --- Memory guard: refuse to spawn if system memory is critically low ---
        try:
            min_mem = KiroCrewConfig.load().agent.spawn_min_memory_gb
        except Exception:
            min_mem = 4.0
        mem_ok, avail_gb = check_memory_available(min_gb=min_mem)
        if not mem_ok:
            logger.warning(
                "Subagent spawn refused: only %.2f GB available (min %.1f GB required)",
                avail_gb, min_mem,
            )
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="refused_low_memory",
                metadata={
                    "available_gb": avail_gb,
                    "min_gb": min_mem,
                    "task": _redacted_task[:120],
                },
            )
            info = SubagentInfo(
                id=uuid.uuid4().hex[:8],
                task=_redacted_task,
                agent=agent,
                done=True,
                error=f"spawn refused: only {avail_gb:.1f} GB memory available (need {min_mem:.0f} GB)",
            )
            return info

        # --- CWD validation: reject bad paths before consuming a slot ---
        resolved_cwd = ""
        if cwd:
            try:
                allowed_roots = KiroCrewConfig.load().agent.subagent_cwd_allowed_roots
            except Exception:
                # Fail closed: if config is unavailable, treat cwd override as
                # disabled. Defaulting to the permissive default here would
                # silently re-enable the feature for admins who set
                # subagent_cwd_allowed_roots=[] to disable it.
                allowed_roots = []
            resolved_cwd, cwd_err = validate_cwd(cwd, allowed_roots)
            if cwd_err:
                logger.warning("Subagent spawn refused: invalid cwd %r: %s", cwd, cwd_err)
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_invalid_cwd",
                    metadata={"cwd": cwd[:200], "reason": cwd_err, "task": _redacted_task[:120]},
                )
                info = SubagentInfo(
                    id=uuid.uuid4().hex[:8],
                    task=_redacted_task,
                    agent=agent,
                    done=True,
                    error=f"spawn refused: {cwd_err}",
                )
                return info

        # --- Governance: spawn capability gate (blast-radius containment) ---
        # A policy/profile may disable sub-agent spawning entirely, or bound it
        # to named agents (capabilities.spawn.scopes.agents).  Resolved against
        # the PARENT surface so a per-app/per-surface profile contains what it
        # can spawn — even if the kiro side would allow it.
        gov_spawn_err = _vet_spawn_governance(parent_session_key, agent)
        if gov_spawn_err:
            logger.warning("Subagent spawn refused by governance: %s", gov_spawn_err)
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="denied",
                error=gov_spawn_err,
                metadata={"agent": agent, "task": _redacted_task[:120]},
            )
            return SubagentInfo(
                id=uuid.uuid4().hex[:8],
                task=_redacted_task,
                agent=agent,
                done=True,
                error=f"spawn refused by governance: {gov_spawn_err}",
            )

        now = time.monotonic()
        should_queue, slot_free = self._should_stagger_queue(now)
        if should_queue:
            self._queue.append(
                {
                    "task": task,
                    "parent_session_key": parent_session_key,
                    "agent": agent,
                    "max_turns": max_turns,
                    "model": model,
                    "allowed_tools": allowed_tools,
                    "bare": bare,
                    "cwd": resolved_cwd,
                    "approval_mode": approval_mode,
                    "silent": silent,
                }
            )
            logger.info(
                "Subagent queued (%d running, %d queued, slot_free=%s)",
                self._running_count,
                len(self._queue),
                slot_free,
            )
            # If a slot is free, no running agent will trigger the drain on
            # completion — schedule the staggered pump at the interval boundary
            # so the queued spawn still launches.
            if slot_free:
                delay = max(0.0, self._spawn_stagger_secs - (now - self._last_spawn_ts))
                try:
                    asyncio.get_event_loop().call_later(delay, self._drain_queue)
                except RuntimeError:
                    pass  # no running loop (sync/test context)
            info = SubagentInfo(id=f"q{len(self._queue)}", task=_redacted_task, agent=agent)
            return info

        if agent:
            agent, err = _validate_agent(agent)
            if err:
                info = SubagentInfo(
                    id=uuid.uuid4().hex[:8], task=_redacted_task, agent="", done=True, error=err
                )
                return info

        agent_id: str = uuid.uuid4().hex[:8]
        info = SubagentInfo(
            id=agent_id,
            task=_redacted_task,
            parent_session_key=parent_session_key,
            agent=agent,
            approval_mode=approval_mode or "",
            silent=silent,
            max_turns=max_turns,
            model=model or "",
            allowed_tools=list(allowed_tools) if allowed_tools else [],
            bare=bare,
            cwd=resolved_cwd,
        )
        info._raw_task = task  # unredacted prompt for kiro-cli execution
        self._agents[agent_id] = info
        self._running_count += 1
        self._last_spawn_ts = time.monotonic()  # stagger gate: one start per interval

        # Check parent session trust (approval_policy="auto") set by dashboard trust toggle.
        parent_trusted = (
            parent_session_key
            and self._sessions.get_approval_policy(parent_session_key) == "auto"
        )

        if self._is_yolo and self._is_yolo():
            self._tasks[agent_id] = asyncio.create_task(self._run(info))
            self._log_spawned(info)
        elif approval_mode == "auto":
            self._tasks[agent_id] = asyncio.create_task(self._run(info))
            self._log_spawned(info)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="auto_approved_spawn",
                metadata={"subagent_id": agent_id, "reason": "approval_mode_auto"},
            )
        elif parent_trusted:
            self._tasks[agent_id] = asyncio.create_task(self._run(info))
            self._log_spawned(info)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="auto_approved_spawn",
                metadata={"subagent_id": agent_id, "reason": "parent_trusted"},
            )
        elif self._ctx_builder and self._ctx_builder.hooks:
            if self._ctx_builder.hooks.auto_approve_subagent_spawn is True:
                self._tasks[agent_id] = asyncio.create_task(self._run(info))
                self._log_spawned(info)
                sel().log_tool_invocation(
                    session_key=info.parent_session_key,
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="auto_approved_spawn",
                    metadata={"subagent_id": agent_id, "reason": "tool_calls_gated"},
                )
            elif self._on_spawn_approval:
                self._tasks[agent_id] = asyncio.create_task(self._spawn_with_approval(info))
            else:
                info.done = True
                info.error = "spawn rejected: no approval mechanism configured"
                self._running_count -= 1
                self._drain_queue()
                sel().log_tool_invocation(
                    session_key=info.parent_session_key,
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_spawn",
                    metadata={"subagent_id": agent_id, "reason": "no_approval_mechanism"},
                )
                return info
        elif self._on_spawn_approval:
            self._tasks[agent_id] = asyncio.create_task(self._spawn_with_approval(info))
        else:
            info.done = True
            info.error = "spawn rejected: no approval mechanism configured"
            self._running_count -= 1
            self._drain_queue()
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="rejected",
                metadata={"subagent_id": agent_id, "reason": "no approval mechanism"},
            )
            logger.warning("Subagent %s rejected: no approval callback", agent_id)
            if self._on_done:
                self._tasks[agent_id] = asyncio.ensure_future(self._safe_announce(info))

        return info

    async def _safe_announce(self, info: SubagentInfo) -> None:
        """Notify completion callback with error handling.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        assert self._on_done is not None
        try:
            await self._on_done(info)
        except Exception:
            logger.exception("Subagent announce failed for %s", info.id)

    def _should_stagger_queue(self, now: float) -> tuple[bool, bool]:
        """Decide whether a spawn arriving at *now* must be queued.

        Returns ``(should_queue, slot_free)``. A spawn is queued when either no
        slot is free (at capacity) OR a spawn started within the stagger window
        (``subagent_spawn_stagger_secs``) — so the initial fill never bursts and
        no two agents start within the interval (dynamic-subagent-sizing.md §5.3).
        """
        slot_free = self._running_count < self._max_concurrent
        too_soon = (now - self._last_spawn_ts) < self._spawn_stagger_secs
        return (not slot_free or too_soon, slot_free)

    def _drain_queue(self) -> None:
        """Spawn the next queued task if a slot is available and the stagger
        interval has elapsed.

        This is the single staggered pump: at most one start per
        ``subagent_spawn_stagger_secs`` (dynamic-subagent-sizing.md §5.3). If a
        slot is free but a spawn started too recently, it reschedules itself at
        the interval boundary rather than bursting.
        """
        if not self._queue or self._running_count >= self._max_concurrent:
            return
        elapsed = time.monotonic() - self._last_spawn_ts
        if elapsed < self._spawn_stagger_secs:
            # Too soon since the last start — reschedule at the boundary.
            try:
                asyncio.get_event_loop().call_later(
                    self._spawn_stagger_secs - elapsed, self._drain_queue
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
            return
        params = self._queue.pop(0)
        logger.info(
            "Draining queue: spawning '%s' (%d left)", str(params.get("task", ""))[:40], len(self._queue)
        )
        # spawn() re-checks the gate; since elapsed >= stagger and a slot is
        # free, it starts immediately and updates _last_spawn_ts. Forward the FULL
        # kwarg set so approval_mode / silent / model / allowed_tools / bare survive
        # the queue round-trip.
        self.spawn(**params)
        if self._queue and self._running_count < self._max_concurrent:
            try:
                asyncio.get_event_loop().call_later(
                    self._spawn_stagger_secs, self._drain_queue
                )
            except RuntimeError:
                pass

    async def _spawn_with_approval(self, info: SubagentInfo) -> None:
        """Request approval before starting the subagent.

        If approval is denied the subagent is marked as done with an
        error and the running count is decremented without executing.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        assert self._on_spawn_approval is not None
        request_id: str = f"spawn:{info.id}"
        try:
            from kiro_crew.security import (
                redact_credentials,
                redact_exfiltration_urls,
            )

            task_safe, _ = redact_exfiltration_urls(info.task)
            task_safe, _ = redact_credentials(task_safe)
            task_preview: str = task_safe[:80]
            approved: bool = await self._on_spawn_approval(
                request_id, f"spawn_run({task_preview})", info.parent_session_key
            )
        except Exception:
            logger.exception("Spawn approval failed for %s", info.id)
            approved = False

        if not approved:
            info.done = True
            info.error = "spawn rejected"
            self._running_count -= 1
            self._drain_queue()
            self._tasks.pop(info.id, None)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="rejected",
                metadata={"subagent_id": info.id},
            )
            logger.info("Subagent %s spawn rejected", info.id)
            if self._on_done:
                await self._safe_announce(info)
            return

        self._log_spawned(info)
        await self._run(info)

    def _log_spawned(self, info: SubagentInfo) -> None:
        """Record spawn metrics and audit log entry.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        # Persist agent folder to disk for orphan recovery
        try:

            create_agent_folder(
                info.id,
                task=info.task,
                agent=info.agent,
                parent_session=info.parent_session_key,
                max_turns=info.max_turns,
            )
        except Exception:
            logger.warning("Failed to create agent folder for %s", info.id, exc_info=True)

        Stats().inc_subagent_spawned()
        sel().log_tool_invocation(
            session_key=info.parent_session_key,
            source="subagent",
            tool_name="spawn_run",
            outcome="spawned",
            metadata={
                "subagent_id": info.id,
                "agent": info.agent or "kirocrew",
                "cwd": info.cwd,
            },
        )
        logger.info("Subagent %s spawned: %s", info.id, info.task[:80])

    @property
    def running(self) -> list[SubagentInfo]:
        """Return currently running (not done) subagents."""
        return [a for a in self._agents.values() if not a.done]

    @property
    def all_agents(self) -> list[SubagentInfo]:
        """Return all tracked subagents (running and done)."""
        return list(self._agents.values())

    def get(self, agent_id: str) -> SubagentInfo | None:
        """Get agent info by ID."""
        return self._agents.get(agent_id)

    @property
    def count(self) -> int:
        return len(self.running)

    async def _run(self, info: SubagentInfo) -> None:
        """Execute a subagent task in its own session."""
        session_key = f"subagent:{info.id}"
        try:
            await asyncio.wait_for(self._run_inner(info, session_key), timeout=self._default_timeout)
        except asyncio.TimeoutError:
            if not info.reaped:
                info.error = f"Timed out after {self._default_timeout // 60} minutes [{_timeout_context(info)}]"
                info.done = True
                Stats().inc_subagent_failed()
                self._write_tombstone(info, "timeout")
            logger.warning("Subagent %s timed out", info.id)
        except asyncio.CancelledError:
            if not info.reaped:
                info.done = True
                info.error = "cancelled"
                Stats().inc_subagent_failed()
                self._write_tombstone(info, "cancelled")
            logger.info("Subagent %s cancelled", info.id)
        except Exception as exc:
            if not info.reaped:
                info.error = str(exc)
                info.done = True
                Stats().inc_subagent_failed()
                self._write_tombstone(info, "error")
            logger.exception("Subagent %s failed", info.id)
        finally:
            if not info.reaped:
                # Fire WS event immediately so Activity Viewer updates
                # before the slow reset + on_done path.
                info.elapsed = time.time() - info.started
                self._record_cost(info)
                await self._fire_event(
                    "subagent_done",
                    info,
                    {
                        "elapsed": info.elapsed,
                        "error": _redact(info.error) if info.error else None,
                        "task": _redact(info.task),
                        "agent": _redact(info.agent),
                        "result": _done_result(info.result),
                    },
                )
                try:
                    if info._session_sharing:
                        # Session-sharing subagents: destroy the session handle
                        # (unregister from shared runtime). Don't kill the runtime.
                        # Skip when the reaper already tore it down (info.reaped).
                        if info._shared_provider and not info.reaped:
                            await info._shared_provider.shutdown()
                    else:
                        self._sessions.release(session_key, cleanup=True)
                except Exception:
                    logger.warning("Subagent %s: release failed", info.id, exc_info=True)
                self._running_count -= 1
                self._drain_queue()
                if not info._session_sharing:
                    try:
                        await asyncio.wait_for(
                            self._sessions.reset(session_key), timeout=_RESET_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Subagent %s: reset timed out, force-killing", info.id)
                        await self._sigkill_session(session_key)
                        try:
                            sel().log_tool_invocation(
                                session_key=session_key,
                                source="subagent",
                                tool_name="run_finally_force_kill",
                                outcome="sigkill",
                                metadata={"subagent_id": info.id},
                            )
                        except Exception:
                            logger.exception("Subagent %s: SEL audit failed", info.id)
                    except Exception:
                        logger.exception("Subagent %s: reset failed", info.id)
            self._tasks.pop(info.id, None)

        if self._on_done and not info.reaped:
            try:
                await asyncio.wait_for(self._on_done(info), timeout=_ON_DONE_TIMEOUT)
                # Retain result.txt for a TTL grace window instead of deleting it
                # now, so the parent can read the full transcript (spawn_status /
                # read / grep) after the completion event. A "delivered" tombstone
                # excludes it from orphan reconciliation; the reaper prunes it after
                # agent.subagent_result_ttl_secs (default 1h).
                if not info.error:
                    try:
                        mark_delivered(info.id)
                    except Exception:
                        logger.debug("Failed to mark subagent %s delivered", info.id, exc_info=True)
                    # Clean up workspace result file (agent-{id}.md in parent session dir)
                    try:
                        parent_key = info.parent_session_key
                        if parent_key.startswith("dashboard:"):
                            slot_key = parent_key.removeprefix("dashboard:")
                            _ws_result_path(slot_key, info.id).unlink(missing_ok=True)
                    except Exception:
                        logger.debug(
                            "Failed to clean workspace result for %s", info.id, exc_info=True
                        )
            except asyncio.TimeoutError:
                logger.error(
                    "Subagent %s: completion injection timed out after %.0fs",
                    info.id,
                    _ON_DONE_TIMEOUT,
                )
                # Kill the parent session's kiro-cli process so the next
                # agent's injection gets a clean provider instead of hitting
                # "Prompt already in progress" on the stuck one.
                try:
                    await self._sessions.reset(info.parent_session_key)
                except Exception:
                    logger.debug(
                        "Failed to reset parent session %s after injection timeout",
                        info.parent_session_key,
                        exc_info=True,
                    )
                self.notify_injection_failed(
                    info,
                    reason=f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s (queue + injection)",
                )
            except Exception:
                logger.exception("Subagent announce failed for %s", info.id)

    async def _fire_event(self, etype: str, info: SubagentInfo, extra: dict | None = None) -> None:
        if self._on_event:
            try:
                await self._on_event(etype, info, extra or {})
            except Exception:
                logger.warning("on_event failed for %s/%s", etype, info.id, exc_info=True)

    @staticmethod
    def _write_tombstone(info: SubagentInfo, cause: str) -> None:
        """Best-effort tombstone write for abnormal exits."""
        try:

            write_tombstone(
                info.id,
                cause=cause,
                recovery_action="pending",
                pid=info._pid,
                turns=info.turns,
                last_tool=info.last_tool,
            )
        except Exception:
            logger.debug("Failed to write tombstone for %s", info.id, exc_info=True)

    async def _run_inner(self, info: SubagentInfo, session_key: str) -> None:
        """Inner execution — called within timeout wrapper."""
        # Mark the real start of execution BEFORE any await so the startup
        # watchdog measures from here, not from registration (which may include
        # an arbitrary spawn-approval wait). Must be the first statement.
        info._exec_started = time.time()
        # Inherit approval policy from parent session; yolo/trust overrides
        parent_policy = self._sessions.get_approval_policy(info.parent_session_key)
        # Explicit approval_mode from spawn caller (e.g. Mochi bg agent)
        if not parent_policy and info.approval_mode == "auto":
            parent_policy = "auto"
            sel().log_api_access(
                caller=info.parent_session_key or f"subagent:{info.id}",
                operation="subagent.approval_mode_auto_policy",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id}",
            )
        if not parent_policy and self._is_yolo and self._is_yolo():
            parent_policy = "auto"
            sel().log_api_access(
                caller=info.parent_session_key,
                operation="subagent.yolo_policy_fallback",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id}",
            )
        if not parent_policy and self._global_approval_mode == "auto":
            # Apply global config as fallback only when parent is absent or
            # confirmed garbage-collected (no longer in session store).
            # If parent session still exists but returned no policy, deny by
            # default — the session is alive and intentionally non-auto.
            if not info.parent_session_key:
                _parent_gone = True  # no_parent
            elif self._sessions.has_session(info.parent_session_key) is False:
                _parent_gone = True  # parent_gc
            else:
                _parent_gone = False  # parent alive or store error → deny
                sel().log_api_access(
                    caller=f"subagent:{info.id}",
                    operation="subagent.config_policy_fallback",
                    outcome="denied",
                    source="subagent",
                    resources=f"subagent_id={info.id},reason=parent_alive_or_store_error",
                )
            if _parent_gone:
                parent_policy = "auto"
                _reason = "parent_gc" if info.parent_session_key else "no_parent"
                sel().log_api_access(
                    caller=f"subagent:{info.id}",
                    operation="subagent.config_policy_fallback",
                    outcome="ok",
                    source="subagent",
                    resources=f"subagent_id={info.id},reason={_reason}",
                )
        # auto_approve_subagent_tools auto-approves tool calls inside
        # subagents (separate from the spawn gate, deny-by-default).
        if not parent_policy and self._ctx_builder and self._ctx_builder.hooks:
            if self._ctx_builder.hooks.auto_approve_subagent_tools is True:
                parent_policy = "auto"
                sel().log_api_access(
                    caller=info.parent_session_key or f"subagent:{info.id}",
                    operation="subagent.auto_approve_subagent_tools_policy",
                    outcome="ok",
                    source="subagent",
                    resources=f"subagent_id={info.id}",
                )
        # Inherit agent from parent session when not explicitly specified
        agent = info.agent or self._sessions.get_agent(info.parent_session_key)
        if not info.agent and agent:
            sel().log_api_access(
                caller=f"subagent:{info.id}",
                operation="subagent.agent_inheritance",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id},inherited_agent={agent}",
            )
        extra_kwargs: dict[str, Any] = {}
        if info.model:
            extra_kwargs["model"] = info.model
        if info.bare:
            extra_kwargs["bare"] = True
        if info.allowed_tools:
            extra_kwargs["allowed_tools"] = info.allowed_tools
        if info.cwd:
            extra_kwargs["cwd"] = info.cwd

        # ── Session sharing: reuse parent's shared AcpRuntime ──
        # When enabled and eligible, subagents get a session on the parent's
        # companion AcpRuntime (~200ms startup, ~0 memory) instead of spawning
        # a fresh kiro-cli process (~3-5s, ~400MB).
        use_session_sharing = self._should_use_session_sharing(info)
        if use_session_sharing:
            try:
                client = await self._create_shared_session(info, session_key, agent)
            except Exception as exc:
                # Fallback: shared runtime unavailable (dead, spawn failed, etc.)
                # Revert to legacy per-process path transparently.
                logger.warning(
                    "Subagent %s: session sharing failed (%s), falling back to dedicated process",
                    info.id, exc,
                )
                info._session_sharing = False
                info._shared_provider = None
                use_session_sharing = False
                client, is_new, _resumed = await self._sessions.get_or_create(
                    session_key, agent=agent or None, approval_policy=parent_policy,
                    **extra_kwargs,
                )
                is_cc = self._is_cc_provider(client)
            else:
                is_new = True
                _resumed = False
                is_cc = False
        else:
            client, is_new, _resumed = await self._sessions.get_or_create(
                session_key, agent=agent or None, approval_policy=parent_policy,
                **extra_kwargs,
            )
            # Detect CC provider to skip permission event loop
            is_cc = self._is_cc_provider(client)
        # Intentionally check info.agent (not resolved `agent`) so only
        # explicitly requested agents skip _SYSTEM_PREFIX (defense-in-depth).
        named_agent = bool(info.agent and _AGENT_NAME_RE.fullmatch(info.agent))
        raw_task = info._raw_task or info.task
        message = raw_task if named_agent else (_SYSTEM_PREFIX + raw_task)
        # Scale the injected-context budget to this subagent's model window (a
        # subagent can be pinned to a smaller model). Resolved from the live
        # client; None ⇒ 1M reference.
        _sub_window = window_for_provider_client(client)
        # Off-loop: build_message embeds the episodic query (blocking urllib).
        full_message, _ = await run_in_embed_pool(
            self._ctx_builder.build_message,
            message, is_new, session_key, provider_type="claude_code" if is_cc else "acp",
            model_window=_sub_window,
        )

        result_text = ""
        turns = 0
        turn_limit = info.max_turns or self._default_turn_limit or _TURN_LIMIT
        # Reports inherited agent (not just info.agent) so telemetry shows
        # the actual agent used for this subagent session.
        await self._fire_event(
            "subagent_spawn", info, {"task": _redact(info.task), "agent": agent or ""}
        )
        # Stream results to disk for orchestrated chat.

        # Record PID for orphan recovery
        try:

            pid = self._sessions.get_pid(session_key)
            if pid:
                info._pid = pid  # make available for _write_tombstone
                update_state(info.id, pid=pid, pid_recorded_at=time.time())
        except Exception:
            logger.debug("Failed to record PID for %s", info.id, exc_info=True)

        # Record session_id and provider type for session file cleanup
        try:
            session_id = client.session_id if hasattr(client, "session_id") else ""
            provider_type = "claude_code" if is_cc else "acp"
            state_update: dict[str, object] = {
                "session_id": session_id,
                "provider": provider_type,
            }
            # Store CWD for CC cleanup (needed to derive project-key path).
            # info.cwd is only set when a caller passes an explicit cwd
            # override (disabled by default), so for the common case derive
            # the project dir from the provider's own work dir — that is the
            # same path sent as ACP `cwd`, hence the encoded project key under
            # ~/.claude/projects. Without this, CC cleanup is skipped (no cwd)
            # and the transcript leaks.
            if is_cc:
                cc_cwd = info.cwd
                if not cc_cwd:
                    inner = getattr(client, "client", None)
                    work_dir = getattr(inner, "_work_dir", None)
                    if work_dir:
                        cc_cwd = str(work_dir)
                if cc_cwd:
                    state_update["cwd"] = cc_cwd
            update_state(info.id, **state_update)
        except Exception:
            logger.debug("Failed to record session_id for %s", info.id, exc_info=True)

        _rp = _agent_dir(info.id) / "result.txt"
        info.result_path = str(_rp)
        # Cache tool names by tool_call_id so PostToolUse can recover the tool name
        # when EVENT_TOOL_RESULT arrives (which only carries tool_call_id and output).
        # Mirrors kiro_crew.dashboard.chat_runner._pending_tools.
        _pending_tools: dict[str, str] = {}
        async for event in client.stream(full_message):
            if event.kind == EVENT_TEXT_CHUNK:
                result_text += event.text
                write_result_chunk(info.id, event.text)
                redacted = _redact(event.text)
                info.streaming_text += redacted
                if len(info.streaming_text) > 50_000:
                    info.streaming_text = "…(truncated)\n" + info.streaming_text[-40_000:]
                await self._fire_event("subagent_chunk", info, {"text": redacted})
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Both kiro-cli and claude-agent-acp surface tool calls via
                # session/request_permission. Run them through the same hook
                # → parent_policy → interactive callback pipeline so the
                # approve / reads / trust / yolo protocol applies uniformly.
                turns += 1
                info.turns = turns
                info.last_tool = event.title or ""
                # Persist turn state for orphan recovery diagnostics
                try:
                    update_state(info.id, turns=turns, last_tool=event.title or "")
                except Exception:
                    pass
                await self._fire_event(
                    "subagent_tool",
                    info,
                    {"tool": _redact(event.title or ""), "tool_kind": event.tool_kind},
                )
                if turns > turn_limit:
                    info.result = result_text or "_Partial output._"
                    info.error = f"turn_limit:{turn_limit}"
                    info.done = True
                    Stats().inc_subagent_failed()
                    logger.warning("Subagent %s hit turn limit (%d)", info.id, turn_limit)
                    self._write_tombstone(info, "turn_limit")
                    return
                tool_result = self._ctx_builder.hooks.on_tool_call(
                    event.title,
                    session_key=session_key,
                    agent=info.agent or "",
                    tool_kind=event.tool_kind,
                    raw_params=event.raw_tool_params,
                    command=event.shell_command,
                    is_shell=event.is_shell,
                )
                if tool_result.action == TOOL_DENY:
                    await self._reject_and_log(
                        client, event.request_id, session_key, event, error="hook_deny"
                    )
                    continue
                if tool_result.action == TOOL_AUTO_APPROVE:
                    await self._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id, "reason": "hook_auto_approve"},
                    )
                    continue
                if parent_policy == "auto":
                    await self._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id, "reason": "parent_policy_auto"},
                    )
                    continue
                if self._on_tool_approval_factory:
                    approve_cb = self._on_tool_approval_factory(info)
                    approved = await approve_cb(event)
                    if not approved:
                        await self._reject_and_log(
                            client,
                            event.request_id,
                            session_key,
                            event,
                            metadata={"subagent_id": info.id, "reason": "factory_rejected"},
                        )
                        continue
                    await self._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id},
                    )
                elif self._on_tool_approval:
                    approved = await self._on_tool_approval(event, info.parent_session_key)
                    if not approved:
                        await self._reject_and_log(client, event.request_id, session_key, event)
                        continue
                    await self._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id},
                    )
                else:
                    # No callback, no auto policy — deny by default
                    await self._reject_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id, "reason": "no_policy_deny_default"},
                    )
                    continue
            elif event.kind == EVENT_TOOL_CALL:
                # Fire PreToolUse hooks for auto-approved tools (informational only)
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="subagent",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="auto_approved",
                    metadata={"subagent_id": info.id},
                )
                # Cache tool name so PostToolUse can recover it on EVENT_TOOL_RESULT.
                # Strip "Running: " prefix to match the name passed to PreToolUse hooks.
                _raw = event.title or ""
                if _raw.startswith("Running: "):
                    _raw = _raw[9:]
                if event.tool_call_id:
                    _pending_tools[event.tool_call_id] = _raw
                await fire_tool_hooks(
                    self.hook_store,
                    event.title,
                    event.tool_input,
                    subagent_id=info.id,
                    parent_session_key=info.parent_session_key or None,
                    agent_role=info.agent or None,
                )
            elif event.kind == EVENT_TOOL_RESULT:
                # Fire PostToolUse hooks (parity with chat_runner). Until this
                # branch existed, hooks registered for subagent-spawned tools
                # received PreToolUse but never PostToolUse — losing the
                # tool_response payload.
                if self.hook_store is not None:
                    try:
                        _tool_name = _pending_tools.pop(event.tool_call_id, "")
                        _out = _redact((event.tool_output or "")[:2000])
                        await self.hook_store.fire(
                            HOOK_EVENT_POST_TOOL_USE,
                            tool_name=_tool_name,
                            tool_response={"output": _out},
                            subagent_id=info.id,
                            parent_session_key=info.parent_session_key or None,
                            agent_role=info.agent or None,
                        )
                    except Exception:
                        logger.debug(
                            "PostToolUse hook error in subagent", exc_info=True,
                        )
            elif event.kind == EVENT_COMPLETE:
                break

        # Strip [OPTIONS: ...] tags and redact sensitive content
        cleaned, _ = extract_options(result_text) if result_text else (result_text, [])
        if cleaned:
            from kiro_crew.security import (
                redact_credentials,
                redact_exfiltration_urls,
            )

            cleaned, _ = redact_exfiltration_urls(cleaned)
            cleaned, _ = redact_credentials(cleaned)
        info.result = cleaned or "_No response._"
        # Cap disk file and trim memory — gateway decides how much to show based on mode.
        if info.result_path:
            cap_result_file(Path(info.result_path))
        # Flag whether the completion-event copy will drop content, so the gateway
        # emits a summary + result_path pointer (read on demand) instead of a lossy
        # blob. The full transcript stays in result.txt for the TTL grace window.
        info.result_truncated = (
            self._completion_keep_chars > 0
            and len(info.result) > self._completion_keep_chars
        )
        info.result = apply_completion_keep(
            info.result,
            self._completion_keep,
            self._completion_keep_chars,
        )
        evict_completed_agents(self._agents)
        info.done = True
        self._sessions.record_success(session_key)
        Stats().inc_subagent_completed()
        logger.info("Subagent %s completed", info.id)

    def _should_use_session_sharing(self, info: SubagentInfo) -> bool:
        """Decide whether a subagent should use the shared-runtime path.

        All must hold: session_sharing config True; parent session exists and
        is ACP/kiro-backed (not CC); not a CC-specific spawn (model/allowed_tools/bare).
        """
        try:
            cfg = KiroCrewConfig.load()
            if not cfg.agent.session_sharing:
                return False
        except Exception:
            return False
        if info.model or info.allowed_tools or info.bare:
            return False
        if not info.parent_session_key:
            return False
        return self._sessions.is_session_sharing_eligible(info.parent_session_key)

    async def _create_shared_session(
        self, info: SubagentInfo, session_key: str, agent: str
    ) -> "LLMProvider":
        """Create a subagent session on the parent's AcpRuntime.

        The parent session (provider=kiro) runs on an AcpRuntime via
        AcpSessionProvider. Subagents create additional sessions on that SAME
        runtime — one process hosts everything. Falls back to
        get_subagent_runtime() (companion runtime) if the parent doesn't use
        AcpSessionProvider. Marks info._session_sharing=True so cleanup calls
        provider.shutdown() instead of SessionManager.release/reset.
        """
        runtime = self._get_parent_runtime(info.parent_session_key)
        if runtime is None:
            runtime = await self._sessions.get_subagent_runtime(info.parent_session_key)

        cwd = info.cwd or str(getattr(self._sessions, "_pool_cwd", ""))
        handle = await runtime.create_session(
            cwd=cwd or None,
            agent=agent or None,
        )
        provider = AcpSessionProvider(handle, runtime)
        info._session_sharing = True
        info._shared_provider = provider
        if runtime.pid:
            info._pid = runtime.pid
            update_state(info.id, pid=runtime.pid, pid_recorded_at=time.time())
        logger.info(
            "Subagent %s using session sharing on runtime PID %s (session %s, key %s)",
            info.id, runtime.pid, handle.session_id, session_key,
        )
        return provider

    def _get_parent_runtime(self, parent_session_key: str) -> "AcpRuntime | None":
        """Extract the AcpRuntime from the parent session's provider.

        Returns the runtime if the parent uses AcpSessionProvider (kiro unified
        path), or None if the parent uses AcpClient (CC or legacy).
        """
        provider = self._sessions.get_provider(parent_session_key)
        if provider is None:
            return None
        inner = getattr(provider, "client", None) or getattr(provider, "_client", None)
        if isinstance(inner, AcpSessionProvider):
            return inner._runtime
        return None

    @staticmethod
    def _is_cc_provider(provider: object) -> bool:
        """Check if a provider routes to Claude Code.

        Matches both the (dead) standalone ``ClaudeCodeProvider`` and the
        real default backend ``AcpProvider(acp_backend="claude")``.  The
        latter is what ``_sessions.get_or_create`` actually returns for the
        ``claude_code`` provider, so detecting it here is what makes the
        session-file cleanup target ``~/.claude`` instead of ``~/.kiro``.
        """
        if ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
            return True
        # circular import: providers.acp participates in a providers -> session
        # cycle (see session.py), so keep this off the module top.
        from kiro_crew.providers.acp import is_claude_backend
        return is_claude_backend(provider)

    async def cancel(self, agent_id: str) -> bool:
        """Cancel a single running subagent. Returns True if found and cancelled."""
        info = self._agents.get(agent_id)
        if not info or info.done:
            return False
        info.error = "Cancelled by user"
        await self._force_reap(agent_id, info, time.time() - info.started)
        return True

    async def cancel_all(self) -> None:
        """Cancel all running subagents and wait for cleanup."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            self._reaper_task = None
        tasks_to_await: list[asyncio.Task] = []  # type: ignore[type-arg]
        for agent_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                tasks_to_await.append(task)
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        self._tasks.clear()
