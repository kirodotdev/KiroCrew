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

This is **advisory only**: no enforcement, no lease, no cross-session
coordination. Two sessions can both read "ample" and both launch heavy work —
the tradeoff of a cheap, zero-tuning guard. It makes the agent *smarter* about
when to go light, not *bounded*. (A hard, cross-session admission lease is a
separate, heavier design.)

The memory figure reuses :func:`kiro_crew.subagent._available_memory_gb`, the
same cgroup-clamped, container-aware probe that auto-sizes the sub-agent cap, so
the two never disagree. It is imported lazily to keep this module import-cheap
and free of any import cycle (``context`` imports this; ``subagent`` is heavy).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

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


@dataclass(frozen=True)
class ResourceStatus:
    """A single advisory snapshot of host resource headroom."""

    available_gb: float          # -1.0 when the memory probe is unavailable
    cpu_count: int
    load_per_cpu: float | None   # 1-min loadavg / cpu_count, None if unavailable
    posture: str                 # one of the POSTURE_* constants
    pressure_gb: float           # tight threshold in effect
    critical_gb: float           # critical threshold in effect

    @property
    def under_pressure(self) -> bool:
        """True only for tight/critical — the gate for injecting the context line."""
        return self.posture in (POSTURE_TIGHT, POSTURE_CRITICAL)

    def _load_suffix(self) -> str:
        return f", load {self.load_per_cpu}/core" if self.load_per_cpu is not None else ""

    def context_line(self) -> str:
        """The compact ``[RESOURCES]`` advisory injected on a pressured turn.

        Returns ``""`` when the line is disabled (``pressure_gb <= 0`` — the
        documented off switch) or when not under pressure, so callers can append
        unconditionally. Note this is the OFF switch for the injected line only;
        ``posture`` / ``summary_lines`` still report the true state for the pull
        tool. Kept short on purpose — it costs tokens every pressured turn.
        """
        if self.pressure_gb <= 0:
            return ""  # off switch: disables the line regardless of critical tier
        if not self.under_pressure:
            return ""
        gb = f"{self.available_gb:.1f}"
        load = self._load_suffix()
        if self.posture == POSTURE_CRITICAL:
            return (
                f"[RESOURCES] Host memory is CRITICALLY low (~{gb} GB free{load}). "
                "Do NOT start heavy work now (full test suites, large builds, big "
                "parallel sub-agent waves) — it may fail or destabilize other "
                "sessions on this host. Run only the lightest necessary steps, or "
                "wait for memory to free. Call the resource_status tool to re-check."
            )
        return (
            f"[RESOURCES] Host memory is tight (~{gb} GB free{load}). Before heavy "
            "work, prefer the lighter path: run targeted tests instead of the full "
            "suite, avoid large parallel sub-agent waves, and serialize or defer "
            "memory-heavy builds/test runs. Call the resource_status tool to "
            "re-check before a heavy step."
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


def probe(cfg: object | None = None) -> ResourceStatus:
    """Take an advisory resource snapshot.

    *cfg* is an optional pre-loaded ``KiroCrewConfig``; when omitted it is
    loaded (the loader is fingerprint-cached, so this is cheap). Never raises —
    on any failure it returns an ``unknown`` posture so callers stay silent.
    """
    if cfg is None:
        try:
            from kiro_crew.config.loader import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        except Exception:  # pragma: no cover - defensive
            cfg = None
    pressure_gb, critical_gb = _resolve_thresholds(cfg)
    available_gb = _read_available_gb()
    cpu_count = os.cpu_count() or 1
    load_per_cpu = _read_load_per_cpu(cpu_count)
    posture = _classify(available_gb, pressure_gb, critical_gb)
    return ResourceStatus(
        available_gb=available_gb,
        cpu_count=cpu_count,
        load_per_cpu=load_per_cpu,
        posture=posture,
        pressure_gb=pressure_gb,
        critical_gb=critical_gb,
    )
