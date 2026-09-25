"""Lightweight, read-only host-resource probe.

Shared by two advisory surfaces:

* the **pressure-gated context line** injected in
  :meth:`kiro_crew.context.ContextManager.build_message` — a compact
  ``[RESOURCES]`` note the gateway adds to a turn ONLY when host memory is
  tight/critical, so the model can pick the lighter path for heavy work. It
  rides the gateway context rail (not an agent tool grant), so it survives
  agent switches, including custom agents.
* the **``resource_status`` pull tool** in ``mcp_core`` — an on-demand probe
  the model can call before a heavy step ("am I clear to run the full suite?").

The advisory surfaces carry no enforcement, no lease, no cross-session
coordination. Two sessions can both read "ample" and both launch heavy work —
the tradeoff of a cheap, zero-tuning guard. One narrow enforcement point sits
on top: :func:`admission_check` gates *background* work admission (scheduled
cron firings, new subagent spawns) while posture is CRITICAL, so the scheduler
stops piling work onto a host that is about to freeze. Direct user chat turns
and the gateway's own operation are never gated, and the gate fails open on an
unknown posture. (A hard, cross-session admission lease is a separate, heavier
design.)

The memory figure reuses :func:`kiro_crew.subagent._available_memory_gb`, the
same cgroup-clamped, container-aware probe that auto-sizes the sub-agent cap, so
the two never disagree. It is imported lazily to keep this module import-cheap
and free of any import cycle (``context`` imports this; ``subagent`` is heavy).

Alongside memory the probe reads one more ceiling: the agent slice's TASK count
against its ``pids.max`` (see :func:`_read_agent_slice_tasks`). The slice's
memory headroom already reaches the posture through the cgroup-clamped memory
probe, while its task headroom reached nothing — a breach there fails ``fork()``
for every agent under that slice at once, yet the count that approaches it was
readable only from ``/sys/fs/cgroup`` by hand. It reaches the pull tool's report,
the injected line, and the diagnostics bundle. It is REPORTED, never gated: the
posture stays a single memory scalar, so :func:`admission_check` and
:func:`prewarm_allowance` behave exactly as before at any task count.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

# Posture labels — coarse buckets, not a continuous score.
POSTURE_AMPLE = "ample"
POSTURE_TIGHT = "tight"
POSTURE_CRITICAL = "critical"
POSTURE_UNKNOWN = "unknown"

# Fallback thresholds (GB) when config is unavailable. Mirror the AgentConfig
# defaults (``resource_pressure_gb`` / ``resource_critical_gb``).
_DEFAULT_PRESSURE_GB = 4.0
_DEFAULT_CRITICAL_GB = 2.0


def _read_available_gb() -> float:
    """Cgroup-clamped available memory (GB), or ``-1.0`` when unreadable.

    Lazily reuses :func:`kiro_crew.subagent._available_memory_gb` (the same
    probe the dynamic sub-agent cap uses) so the container-aware clamping logic
    lives in exactly one place. Any failure degrades to ``-1.0`` → posture
    ``unknown`` → the caller stays silent (fail-open, never a false alarm).
    """
    try:
        from kiro_crew.subagent import _available_memory_gb

        return _available_memory_gb()
    except Exception:  # pragma: no cover - defensive; probe must never raise
        logger.debug("available-memory probe failed", exc_info=True)
        return -1.0


def _read_load_per_cpu(cpu_count: int) -> float | None:
    """1-minute load average normalized per CPU, or ``None`` if unavailable.

    ``os.getloadavg`` is Unix-only and raises ``OSError`` if the load cannot be
    obtained; Windows lacks it entirely (``AttributeError``). Either way the
    caller treats ``None`` as "no load signal" and omits it.
    """
    if cpu_count <= 0:
        return None
    try:
        one_min = os.getloadavg()[0]
    except (OSError, AttributeError, ValueError, IndexError):
        return None
    return round(one_min / cpu_count, 2)


#: Fraction of the agent slice's ``pids.max`` at which its task count reads as
#: tight. Deliberately a constant, not a config key: the ceiling it is measured
#: against is already the operator's knob (``resource_limits.max_total_processes``),
#: and a second knob for the reporting ratio would only let a host silence the
#: reading without raising the ceiling it is about to hit.
_SLICE_TASKS_TIGHT_RATIO = 0.90

#: cgroup v2 files holding a cgroup's live task count and its task ceiling.
_PIDS_CURRENT = "pids.current"
_PIDS_MAX = "pids.max"


def _read_pids_max(path: Path) -> int:
    """A cgroup ``pids.max`` as ``0`` for no ceiling, the value, or ``-1`` unreadable.

    The shared reader (``sandbox.read_cgroup_int``) folds three outcomes into one
    ``None``: the kernel's ``max`` sentinel, an absent file, and unparseable
    content. Every other caller treats all three as "this bound does not
    constrain", which is right for a bound but wrong for a REPORT: a slice torn
    down between the directory check and this read would otherwise be published
    as having no ceiling, which is a reassurance nothing measured. Only the
    literal sentinel earns ``0`` here.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return -1
    if text == "max":
        return 0
    return int(text) if text.isdigit() else -1


def _read_agent_slice_tasks() -> tuple[int, int, int]:
    """Agent-slice task count, its ceiling, and this instance's share of it.

    cgroup v2 ``pids.current`` counts TASKS — threads, not processes — so this is
    the figure that actually reaches ``pids.max``, and a host can sit at 95% of
    the ceiling while its process count looks unremarkable.

    The aggregate is read from the SHARED ``kirocrew-agents.slice``, because that
    is where the ceiling lives: a per-instance child slice carries no ``pids.max``
    of its own, so a per-instance count alone cannot say how close the host is to
    the wall. Reading the shared parent is sound where operating on it would not
    be — a count attributes nothing to anyone, while a kill needs an owner — and
    the instance's own child is read as a separate figure so an install can tell
    its contribution from a co-resident gateway's.

    Returns ``(current, limit, own)``. A figure that cannot be read is ``-1``
    (not Linux, no cgroup v2 delegation, or the slice not currently
    materialized). ``limit`` is ``0`` when the slice has no ceiling at all
    (``pids.max`` holds the kernel's ``max`` sentinel), and ``own`` is ``-1``
    when this install has no per-instance slice to attribute tasks to.
    """
    try:
        # Imported inside the function, like the memory probe above, and for the
        # same reason this module documents as a property: import cheapness.
        # ``sandbox`` is ~12,600 lines, and several module-scope importers of
        # ``resource_status`` -- ``context`` on the per-turn path, ``cron``,
        # ``mcp_tools.spawn``, ``dashboard.cautious_boot`` -- do not import it at
        # all, so hoisting this would put that cost on every one of them. No
        # cycle forces the choice: ``sandbox`` never reads this module.
        from kiro_crew import sandbox

        slice_dir = sandbox._agents_slice_cgroup_dir()
        if slice_dir is None:
            return -1, -1, -1
        current = sandbox.read_cgroup_int(slice_dir / _PIDS_CURRENT)
        limit = _read_pids_max(slice_dir / _PIDS_MAX)
        child_name = sandbox._agents_slice_name()
        own = -1
        if child_name != sandbox._CGROUP_AGENTS_SLICE:
            child = slice_dir / child_name
            if not child.is_dir():
                # systemd releases an empty slice's directory, so an absent
                # child is a true zero rather than a failed read.
                own = 0
            else:
                own_current = sandbox.read_cgroup_int(child / _PIDS_CURRENT)
                own = -1 if own_current is None else own_current
        return (-1 if current is None else current), limit, own
    except Exception:  # pragma: no cover - defensive; the probe must never raise
        logger.debug("agent-slice task probe failed", exc_info=True)
        return -1, -1, -1


@dataclass(frozen=True)
class ResourceStatus:
    """A single advisory snapshot of host resource headroom."""

    available_gb: float  # -1.0 when the memory probe is unavailable
    cpu_count: int
    load_per_cpu: float | None  # 1-min loadavg / cpu_count, None if unavailable
    posture: str  # one of the POSTURE_* constants
    pressure_gb: float  # tight threshold in effect
    critical_gb: float  # critical threshold in effect
    # Agent-slice task ceiling (see _read_agent_slice_tasks). Defaulted so every
    # existing construction stays valid and a caller that cannot measure tasks
    # reports "unknown" rather than a fabricated zero.
    slice_tasks: int = -1  # slice pids.current; -1 when unreadable
    slice_tasks_limit: int = -1  # slice pids.max; 0 = no ceiling, -1 unreadable
    slice_tasks_own: int = -1  # this instance's share; -1 when unattributable

    @property
    def under_pressure(self) -> bool:
        """True only for tight/critical — the gate for injecting the context line."""
        return self.posture in (POSTURE_TIGHT, POSTURE_CRITICAL)

    @property
    def slice_tasks_tight(self) -> bool:
        """True when the slice's task count sits in the tight band of its ceiling.

        Requires a real ceiling: a slice with no ``pids.max``, and a host where
        the ceiling cannot be read at all, have no wall to approach. An
        unreadable COUNT needs no branch of its own — it is ``-1``, which cannot
        reach a positive threshold — so the reading it cannot substantiate is
        refused by the comparison itself.

        Independent of ``posture``, which stays a memory-only scalar: a host can
        be tight on tasks and ample on memory, which is the case the memory
        figure cannot express.
        """
        if self.slice_tasks_limit <= 0:
            return False
        return self.slice_tasks >= _SLICE_TASKS_TIGHT_RATIO * self.slice_tasks_limit

    def slice_tasks_text(self) -> str:
        """The task reading both rendered surfaces print, so they cannot disagree.

        Empty when the count is unreadable, which is what keeps the figure out of
        the pull tool's report and off the advisory line on a host with no cgroup
        task ceiling. The diagnostics bundle is separate and carries the ``-1``
        sentinel instead, because a field reader needs the key present to tell
        "not measurable here" from a field this version does not serve.
        """
        if self.slice_tasks < 0:
            return ""
        if self.slice_tasks_limit == 0:
            text = f"{self.slice_tasks} tasks, no ceiling set"
        elif self.slice_tasks_limit < 0:
            text = f"{self.slice_tasks} tasks, ceiling unreadable"
        else:
            pct = round(100 * self.slice_tasks / self.slice_tasks_limit)
            text = f"{self.slice_tasks} of {self.slice_tasks_limit} tasks ({pct}%)"
        if self.slice_tasks_own >= 0:
            text += f", this instance {self.slice_tasks_own}"
        return text

    def _load_suffix(self) -> str:
        return f", load {self.load_per_cpu}/core" if self.load_per_cpu is not None else ""

    def context_line(self) -> str:
        """The compact ``[RESOURCES]`` advisory injected on a pressured turn.

        Returns ``""`` when the line is disabled (``pressure_gb <= 0`` — the
        documented off switch), or when neither ceiling is near: memory is ample
        AND the slice's task count is outside its tight band, so callers can
        append unconditionally. Note this is the OFF switch for the injected line
        only; ``posture`` / ``summary_lines`` still report the true state for the
        pull tool. Kept short on purpose — it costs tokens every pressured turn.

        The task ceiling can raise the line by itself, because a host one fork
        from its ``pids.max`` is not observable in the memory figure at all. It
        does NOT touch ``posture``, so nothing that gates on the posture tier
        changes behaviour with it.
        """
        if self.pressure_gb <= 0:
            return ""  # off switch: disables the line regardless of critical tier
        if not self.under_pressure:
            return self._tasks_only_line()
        gb = f"{self.available_gb:.1f}"
        load = self._load_suffix()
        if self.posture == POSTURE_CRITICAL:
            return (
                f"[RESOURCES] Host memory is CRITICALLY low (~{gb} GB free{load}). "
                "Do NOT start heavy work now (full test suites, large builds, big "
                "parallel sub-agent waves) — it may fail or destabilize other "
                "sessions on this host. Run only the lightest necessary steps, or "
                "wait for memory to free. Call the resource_status tool to re-check."
            ) + self._tasks_clause()
        return (
            f"[RESOURCES] Host memory is tight (~{gb} GB free{load}). Before heavy "
            "work, prefer the lighter path: run targeted tests instead of the full "
            "suite, avoid large parallel sub-agent waves, and serialize or defer "
            "memory-heavy builds/test runs. Call the resource_status tool to "
            "re-check before a heavy step."
        ) + self._tasks_clause()

    def _tasks_clause(self) -> str:
        """Sentence appended to a memory advisory when tasks are ALSO near the cap."""
        if not self.slice_tasks_tight:
            return ""
        return (
            f" The agent slice is also near its task ceiling ({self.slice_tasks_text()}); "
            "past it every agent under that slice fails to fork at once, so close idle "
            "sessions rather than adding more."
        )

    def _tasks_only_line(self) -> str:
        """The advisory for a slice tight on tasks while memory is not the constraint.

        The memory half is stated from the posture rather than assumed: a host
        whose memory probe is unreadable classifies as ``unknown``, which is not
        under pressure, so this line would otherwise report memory as fine on a
        reading it never obtained.
        """
        if not self.slice_tasks_tight:
            return ""
        if self.posture == POSTURE_UNKNOWN:
            memory = "Host memory is unreadable here"
        else:
            memory = "Host memory is fine"
        return (
            f"[RESOURCES] {memory}, but the agent slice is near its task ceiling "
            f"({self.slice_tasks_text()}). Past it every agent under that slice fails to "
            "fork at once. Avoid large parallel sub-agent waves, close idle sessions, and "
            "call the resource_status tool to re-check."
        )

    def summary_lines(self) -> list[str]:
        """Human/agent-readable multi-line report for the pull tool."""
        lines = ["Host resources (advisory — not an enforced limit):"]
        if self.available_gb < 0:
            lines.append("  Available memory: unknown (probe unavailable on this host)")
        else:
            lines.append(
                f"  Available memory: {self.available_gb:.1f} GB "
                f"(tight \u2264 {self.pressure_gb:g} GB, critical \u2264 {self.critical_gb:g} GB)"
            )
        load = f"{self.load_per_cpu}/core" if self.load_per_cpu is not None else "unknown"
        lines.append(f"  CPU cores: {self.cpu_count}   1-min load: {load}")
        lines.append(f"  Posture: {self.posture.upper()}")
        tasks = self.slice_tasks_text()
        if tasks:
            # Omitted, not reported as "unknown", where there is no cgroup task
            # ceiling to approach (macOS, Windows, no cgroup v2 delegation): an
            # unknown figure on those hosts is noise on every call.
            band = " — TIGHT" if self.slice_tasks_tight else ""
            lines.append(f"  Agent slice tasks: {tasks}{band}")
        lines.extend(adaptive_summary_lines())
        return lines


def adaptive_state() -> dict | None:
    """The adaptive concurrency controller's structured state, or ``None``.

    Read from the gateway-process registry in ``kiro_crew.adaptive.controller``
    (one controller per gateway). ``None`` means no controller is running in
    this process -- the CLI, a subagent process, a test -- and the caller
    omits the section. Never raises.
    """
    try:
        from kiro_crew.adaptive.controller import current_state

        return current_state()
    except Exception:  # pragma: no cover - defensive; the probe must never raise
        logger.debug("adaptive controller state unavailable", exc_info=True)
        return None


def adaptive_exec_cap() -> int:
    """The execution cap IN FORCE in this process, or ``0`` when unknown.

    The number a caller sizing a fan-out needs: ``agent.max_subagents`` is a
    ceiling the adaptive controller may be dispatching 1 at a time under. The
    same registry read as :func:`adaptive_state` -- a dict lookup, no request --
    so it is safe on every session-assembly path; outside the gateway it is 0
    and the caller falls back to the configured ceiling, LABELLED as one. A
    disabled controller leaves the user's max as the cap in force, so that is
    what it reports; a paused dispatch (cap 0) reads as unknown, because "up to
    0" is no fan-out guidance at all.
    """
    state = adaptive_state()
    if not state:
        return 0
    key = "effective_exec_cap" if state.get("enabled", True) else "exec_ceiling"
    cap = state.get(key)
    return cap if isinstance(cap, int) and cap > 0 else 0


def adaptive_summary_lines(state: dict | None = None) -> list[str]:
    """Effective caps and controller state, for the ``resource_status`` tool.

    Empty when no controller runs here. Otherwise: the live execution cap
    against the user's ceiling, the spawn-gate capacity, whether dispatch is
    paused or probing, and the last decision's action and reason -- what the
    dashboard's resources popover and ``kirocrew doctor`` show as "effective
    concurrency vs user max and the current pressure reason". When the state
    carries ``recent_decisions``, the last five cap changes follow, newest
    last, so a low cap can be traced to the samples that cut it.
    """
    if state is None:
        state = adaptive_state()
    if not state:
        return []
    lines = ["Adaptive concurrency (enforced beneath the user cap):"]
    if not state.get("enabled", True):
        lines.append(
            f"  Disabled (agent.adaptive_concurrency=false); execution cap = user max "
            f"{state.get('exec_ceiling')}"
        )
        return lines
    mode = state.get("mode", "aimd")
    exec_cap = state.get("effective_exec_cap")
    ceiling = state.get("exec_ceiling")
    gate_cap = state.get("spawn_gate_capacity")
    gate_ceiling = state.get("gate_ceiling")
    status = "paused" if state.get("paused") else "active"
    if state.get("probing"):
        status = "probing"
    lines.append(
        f"  Mode: {mode}   Execution cap: {exec_cap}/{ceiling}   "
        f"MCP spawn gate: {gate_cap}/{gate_ceiling}   Dispatch: {status}"
    )
    # The growth regime: without it "4/64" reads as an unexplained throttle.
    # The cap climbs toward the user's ceiling on clean samples; a low one is
    # earned headroom not yet spent, never a static host prediction.
    if state.get("enabled", True) and "slow_start" in state:
        growth = "slow start (x2/window)" if state.get("slow_start") else "+1 per window"
        lines.append(f"  Growth toward ceiling: {growth}")
    last = state.get("last") or {}
    if last:
        signals = ",".join(last.get("signals") or []) or "none"
        lines.append(
            f"  Last decision: {last.get('action')} ({last.get('reason')}); signals: {signals}"
        )
        throttled = last.get("throttled_providers") or []
        if throttled:
            lines.append(
                f"  Provider throttling (scoped, not a host signal): {', '.join(throttled)}"
            )
    recent = state.get("recent_decisions")
    if isinstance(recent, list) and recent:
        lines.append("  Recent cap changes (newest last):")
        for entry in recent[-5:]:
            lag = entry.get("loop_lag_ms")
            lag_text = "-" if lag is None else f"{lag}"
            at = entry.get("at")
            # The same ``%H:%M:%S`` local-time stamp gateway.log carries, so
            # the line can be matched against the log without conversion.
            at_text = (
                time.strftime("%H:%M:%S", time.localtime(at))
                if isinstance(at, (int, float))
                else "--:--:--"
            )
            lines.append(
                f"    {at_text} {entry.get('action')} -> exec {entry.get('exec_cap')} "
                f"gate {entry.get('gate_cap')} lag {lag_text}ms ({entry.get('reason')})"
            )
    counts = state.get("counts") or {}
    if counts:
        lines.append("  Decisions: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return lines


def _resolve_thresholds(cfg: object | None) -> tuple[float, float]:
    """Return (pressure_gb, critical_gb) from config, falling back to defaults."""
    agent = getattr(cfg, "agent", None)
    pressure = getattr(agent, "resource_pressure_gb", _DEFAULT_PRESSURE_GB)
    critical = getattr(agent, "resource_critical_gb", _DEFAULT_CRITICAL_GB)
    try:
        pressure = float(pressure)
        critical = float(critical)
    except (TypeError, ValueError):
        pressure, critical = _DEFAULT_PRESSURE_GB, _DEFAULT_CRITICAL_GB
    # Invariant: critical must not exceed pressure. `_classify` tests critical
    # first, so an inverted config (e.g. critical=8, pressure=4) would make the
    # `tight` tier unreachable and report CRITICAL at high free memory. Clamp
    # rather than reject so a misconfig degrades sensibly. Only meaningful when
    # pressure is enabled (pressure<=0 disables the line anyway).
    if pressure > 0 and critical > pressure:
        critical = pressure
    return pressure, critical


def _classify(available_gb: float, pressure_gb: float, critical_gb: float) -> str:
    """Map available memory + thresholds to a posture bucket.

    A threshold of ``0`` disables its bucket: with ``pressure_gb == 0`` no
    positive memory reading can ever be ``<=`` it, so the posture is always
    ``ample`` and the context line never injects (the documented off switch).
    An unreadable probe (``available_gb < 0``) is ``unknown`` → fail-open.
    """
    if available_gb < 0:
        return POSTURE_UNKNOWN
    if critical_gb > 0 and available_gb <= critical_gb:
        return POSTURE_CRITICAL
    if pressure_gb > 0 and available_gb <= pressure_gb:
        return POSTURE_TIGHT
    return POSTURE_AMPLE


def _load_config() -> object | None:
    """The ``KiroCrewConfig`` every threshold reader here resolves against.

    Fingerprint-cached by the loader, so calling it per probe is cheap. Returns
    ``None`` on any failure so the caller falls back to the shipped defaults.
    """
    try:
        return KiroCrewConfig.load()
    except Exception:  # pragma: no cover - defensive
        return None


def probe(cfg: object | None = None) -> ResourceStatus:
    """Take an advisory resource snapshot.

    *cfg* is an optional pre-loaded ``KiroCrewConfig``; when omitted it is
    loaded (the loader is fingerprint-cached, so this is cheap). Never raises —
    on any failure it returns an ``unknown`` posture so callers stay silent.
    """
    if cfg is None:
        cfg = _load_config()
    pressure_gb, critical_gb = _resolve_thresholds(cfg)
    available_gb = _read_available_gb()
    cpu_count = os.cpu_count() or 1
    load_per_cpu = _read_load_per_cpu(cpu_count)
    posture = _classify(available_gb, pressure_gb, critical_gb)
    slice_tasks, slice_tasks_limit, slice_tasks_own = _read_agent_slice_tasks()
    return ResourceStatus(
        available_gb=available_gb,
        cpu_count=cpu_count,
        load_per_cpu=load_per_cpu,
        posture=posture,
        pressure_gb=pressure_gb,
        critical_gb=critical_gb,
        slice_tasks=slice_tasks,
        slice_tasks_limit=slice_tasks_limit,
        slice_tasks_own=slice_tasks_own,
    )


# ── pytest-xdist auto-worker cap ─────────────────────────────────────────────
#
# The one place resource awareness SHAPES agent work instead of only advising
# on it. pytest-xdist resolves ``-n auto`` / ``-n logical`` to the CPU count,
# ignoring memory entirely — on a many-core host each worker's interpreter +
# fixtures cost ~1 GB, so one full-suite run claims 12-16 GB and two concurrent
# agent runs can exhaust an unswapped host. xdist honors the
# ``PYTEST_XDIST_AUTO_NUM_WORKERS`` environment variable when resolving *auto*
# (https://pytest-xdist.readthedocs.io/en/stable/distribution.html), so seeding
# it into every agent child env caps ONLY auto resolution: explicit ``-n N``,
# non-xdist runs, and venvs without xdist are untouched.

#: Env var pytest-xdist consults when resolving ``-n auto`` / ``-n logical``.
XDIST_AUTO_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"

#: Platform-aware assumed steady-state memory of one xdist worker (GB). A
#: full-suite worker holds ~1.5 GB on Linux but 14.9-16.1 GB on macOS, so no
#: single fixed number is correct for every host. macOS is sized to that
#: footprint; Linux/Windows to the collection floor plus headroom. Deliberately
#: a constant, not a config key -- ``xdist_auto_cap`` is the single operator
#: knob. Sized identically to the local ``-n auto`` budget (``xdist_budget``)
#: so the two never disagree.
_MACOS_XDIST_PER_WORKER_GB = 16.0
_DEFAULT_XDIST_PER_WORKER_GB = 3.0


def _resolve_xdist_per_worker_gb() -> float:
    """Per-worker reservation (GB): the platform-aware built-in.

    Sized identically to :func:`xdist_budget._gib_per_worker`
    so the local ``-n auto`` hook and this agent-spawn cap size a worker
    identically.
    """
    return _MACOS_XDIST_PER_WORKER_GB if sys.platform == "darwin" else _DEFAULT_XDIST_PER_WORKER_GB


#: Fraction of *currently available* memory one test run may claim. Half, so
#: two concurrent agent sessions sizing themselves at the same instant cannot
#: jointly commit more than what was free.
_XDIST_MEMORY_SHARE = 0.5


def compute_xdist_auto_workers(
    available_gb: float,
    cpu_count: int,
    *,
    per_worker_gb: float | None = None,
    share: float = _XDIST_MEMORY_SHARE,
) -> int:
    """Memory-aware worker count for ``-n auto``: never more than the CPUs,
    never more than ``available_gb * share / per_worker_gb``, never below 1.

    ``per_worker_gb`` defaults to the platform-aware, config-overridable
    reservation (see :func:`_resolve_xdist_per_worker_gb`); pass an explicit
    value only in tests. The floor of 1 keeps a low-memory host running the
    suite serially rather than failing the run; the CPU ceiling means this can
    only tighten xdist's own default, never exceed it.
    """
    if per_worker_gb is None or per_worker_gb <= 0:
        per_worker_gb = _resolve_xdist_per_worker_gb()
    cpus = max(1, cpu_count)
    by_memory = int(max(0.0, available_gb) * share / per_worker_gb)
    return max(1, min(cpus, by_memory))


def _xdist_cap_config() -> int:
    """Resolve ``resource_limits.xdist_auto_cap`` from the raw config.

    Semantics: ``-1`` (default) = auto-compute from available memory;
    ``0`` = disabled, inject nothing (passthrough to xdist's own default);
    ``N > 0`` = fixed cap. Junk values fall back to the default. Reads the
    shared ``resource_limits`` block through ``ResourceLimitsConfig.from_raw``,
    the single validated parse site every other consumer of that block also
    goes through; the import is function-level to avoid an import cycle through
    the config loader.
    """
    try:
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
        if rl.xdist_auto_cap is not None:
            return rl.xdist_auto_cap
    except Exception:  # pragma: no cover - defensive; config must never break a spawn
        logger.debug("xdist auto cap: config unavailable, using default", exc_info=True)
    return -1


def inject_xdist_auto_cap(env: MutableMapping[str, str]) -> None:
    """Seed ``PYTEST_XDIST_AUTO_NUM_WORKERS`` into an agent child environment.

    Called at the agent spawn boundary (``acp/client.py`` / ``acp/runtime.py``)
    after the child env has been assembled. Only affects how pytest-xdist
    resolves ``-n auto`` — see the section comment above. Never overrides a
    value already present (operator/user wins), never raises, and injects
    nothing when disabled via config or when the memory probe is unavailable.
    """
    if env.get(XDIST_AUTO_ENV):
        return  # already set by the operator/user — respect it
    cap = _xdist_cap_config()
    if cap == 0:
        return  # disabled: leave xdist's own auto resolution untouched
    if cap > 0:
        env[XDIST_AUTO_ENV] = str(cap)
        return
    available_gb = _read_available_gb()
    if available_gb < 0:
        return  # probe unavailable — fail open to xdist's default
    env[XDIST_AUTO_ENV] = str(compute_xdist_auto_workers(available_gb, os.cpu_count() or 1))


@dataclass(frozen=True)
class AdmissionDecision:
    """Typed verdict from :func:`admission_check`.

    ``reason`` is a caller-embeddable, human-readable explanation — non-empty
    exactly when ``admitted`` is False, so refusal surfaces (spawn errors, cron
    deferral logs) can relay it verbatim.
    """

    admitted: bool
    posture: str  # POSTURE_* observed at decision time
    available_gb: float  # -1.0 when the memory probe is unavailable
    reason: str = ""


def _gate_enabled(cfg: object | None) -> bool:
    """Whether the admission gate is switched on (``agent.admission_gate``)."""
    agent = getattr(cfg, "agent", None)
    enabled = getattr(agent, "admission_gate", True)
    return enabled if isinstance(enabled, bool) else True


def admission_check(cfg: object | None = None) -> AdmissionDecision:
    """Decide whether new *background* work may start right now.

    The single enforcement point layered on the advisory posture tier: a
    CRITICAL posture refuses; every other posture — ample, tight, and unknown —
    admits. Callers on the two gated paths (scheduled cron firings, new
    subagent spawns) consult this once per admission decision; it reuses the
    same cheap :func:`probe` the advisory surfaces use (fingerprint-cached
    config, one memory read, and a handful of single-value cgroup reads for the
    slice's task figure) and never scans processes. The task figure is reported,
    not gated: the verdict below turns on the memory posture alone, so no task
    count can refuse work here.

    Fail-open by construction: an unreadable memory probe classifies as
    ``unknown`` (admitted), a disabled gate (``agent.admission_gate: false``)
    always admits, and any unexpected error admits — the gate must never be
    the thing that strands the scheduler. *cfg* is an optional pre-loaded
    ``KiroCrewConfig``; never raises.
    """
    try:
        if cfg is None:
            try:
                cfg = KiroCrewConfig.load()
            except Exception:  # pragma: no cover - defensive
                # Fail-open, not fall-back: probing with default thresholds
                # (and the gate's default-on) could DEFER work because the
                # config was unreadable — the documented contract is that
                # only a genuine critical posture refuses.
                return AdmissionDecision(admitted=True, posture=POSTURE_UNKNOWN, available_gb=-1.0)
        status = probe(cfg)
        if not _gate_enabled(cfg):
            return AdmissionDecision(
                admitted=True, posture=status.posture, available_gb=status.available_gb
            )
        if status.posture == POSTURE_CRITICAL:
            return AdmissionDecision(
                admitted=False,
                posture=status.posture,
                available_gb=status.available_gb,
                reason=(
                    f"host memory is critical (~{status.available_gb:.1f} GB free, "
                    f"critical \u2264 {status.critical_gb:g} GB) — retry when memory frees"
                ),
            )
        return AdmissionDecision(
            admitted=True, posture=status.posture, available_gb=status.available_gb
        )
    except Exception:
        logger.debug("admission check failed — admitting (fail-open)", exc_info=True)
        return AdmissionDecision(admitted=True, posture=POSTURE_UNKNOWN, available_gb=-1.0)


# Pre-warmed (eager spawn) session population, derived from host memory.
#
# Each speculative session is one full kiro-cli process plus its own MCP
# servers, held live and unclaimed until a real turn arrives or the idle
# sweep / prefetch TTL fires. The allowance is the per-host answer to "how
# many of those may sit idle": none when the host is already in the critical
# band, one in the tight band, and the historical fixed cap of three above
# it. The bands ARE the advisory posture: the allowance is keyed by the
# bucket ``_classify`` returns for the same reading and the same
# ``_resolve_thresholds(cfg)`` result the ``[RESOURCES]`` line uses, so a
# host tuned via ``agent.resource_pressure_gb`` / ``agent.resource_critical_gb``
# gets a matching allowance, and the pre-warm population shrinks in step with
# the posture rather than on a second, disagreeing scale. ``unknown`` (an
# unreadable probe) keeps the fixed cap: a host the probe cannot measure is
# never made worse by it.
PREWARM_MAX_LIVE = 3
_PREWARM_BY_POSTURE: dict[str, int] = {
    POSTURE_CRITICAL: 0,
    POSTURE_TIGHT: 1,
    POSTURE_AMPLE: PREWARM_MAX_LIVE,
    POSTURE_UNKNOWN: PREWARM_MAX_LIVE,
}


def prewarm_allowance(available_gb: float | None = None, cfg: object | None = None) -> int:
    """How many pre-warmed agent sessions the host can afford to hold idle.

    *available_gb* is the cgroup-clamped available memory; when omitted it is
    read via the same probe every other surface here uses. *cfg* is an
    optional pre-loaded ``KiroCrewConfig``; when omitted it is loaded the way
    :func:`probe` loads it, so the bands follow the configured thresholds. An
    unreadable probe (``< 0``) returns :data:`PREWARM_MAX_LIVE` — the pre-fix
    behaviour, so a host the probe cannot measure is never made worse by it.
    Never raises.
    """
    try:
        if available_gb is None:
            available_gb = _read_available_gb()
        if cfg is None:
            cfg = _load_config()
        pressure_gb, critical_gb = _resolve_thresholds(cfg)
        posture = _classify(available_gb, pressure_gb, critical_gb)
        return _PREWARM_BY_POSTURE.get(posture, PREWARM_MAX_LIVE)
    except Exception:  # pragma: no cover - defensive; must never raise
        logger.debug("prewarm allowance probe failed — using the fixed cap", exc_info=True)
        return PREWARM_MAX_LIVE


# Cached-verdict layer for callers that must never block: the sync spawn path
# runs on the gateway event loop, so it reads the last off-thread verdict
# instead of probing inline. Freshness window sized to the posture's own rate
# of change (memory exhaustion develops over tens of seconds, not millis).
_CACHED_TTL_SECS = 5.0
_cached_decision: AdmissionDecision | None = None
_cached_at: float = 0.0
_cache_refresh_inflight = threading.Lock()


def _refresh_cached_decision() -> None:
    global _cached_decision, _cached_at
    try:
        _cached_decision = admission_check()
        _cached_at = time.monotonic()
    finally:
        _cache_refresh_inflight.release()


def cached_admission_check() -> AdmissionDecision:
    """Non-blocking admission verdict for event-loop hot paths.

    Returns the last off-thread verdict while it is fresh; when stale, kicks
    one background refresh (non-blocking dedupe) and returns the previous
    verdict — or a fail-open admit before the first refresh completes. The
    caller's thread never performs config or procfs I/O, which is what keeps
    the sync spawn path safe to call from the gateway event loop. The
    trade-off is bounded staleness (:data:`_CACHED_TTL_SECS` plus one refresh
    latency), acceptable because the gate is advisory pressure-shedding, not
    a correctness barrier.
    """
    if _cached_decision is None or time.monotonic() - _cached_at >= _CACHED_TTL_SECS:
        if _cache_refresh_inflight.acquire(blocking=False):
            try:
                threading.Thread(
                    target=_refresh_cached_decision,
                    name="admission-refresh",
                    daemon=True,
                ).start()
            except RuntimeError:
                # Thread exhaustion: this runs on the spawn path, so it must
                # neither raise nor retain the refresh lock. Fail-open below.
                _cache_refresh_inflight.release()
                logger.debug("admission refresh thread could not start", exc_info=True)
    if _cached_decision is not None:
        return _cached_decision
    return AdmissionDecision(
        admitted=True,
        posture=POSTURE_UNKNOWN,
        available_gb=-1.0,
        reason="no cached verdict yet — admitting (fail-open)",
    )
