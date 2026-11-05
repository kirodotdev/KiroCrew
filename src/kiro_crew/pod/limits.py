"""Per-pod memory and CPU ceilings: parsing, host caps and the env-file keys.

A pod records its ceilings in its env file as ``MEMORY=`` and ``CPU=``, beside
``PORT=``, so they survive a restart and every reader agrees on them. The values
are stored in systemd's own spelling (``16G``, ``800%``) because the Linux
backend renders them straight into the pod's managed drop-in as ``MemoryMax=``
and ``CPUQuota=``. A pod whose env file carries neither key keeps the template
unit's defaults, :data:`DEFAULT_MEMORY` and :data:`DEFAULT_CPU`.

The ceilings are capped against the host so a typo cannot hand one pod the whole
machine. Memory is capped at the same fraction of physical RAM the shared agents
slice uses for its aggregate ``memory.max`` (``sandbox_slice``), because a pod
asking for more than every agent on the host may claim together has no sane
reading. CPU is capped at one full core per host CPU (``N * 100%``), the most a
quota can usefully grant.

Leaf module: imported by the boot path, the CLI and the unit renderer, so it pulls
in nothing from :mod:`kiro_crew.pod.runtime`. Errors are :class:`LimitError`, a
``ValueError``, which callers convert to their own refusal type.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: Template-unit defaults, rendered by ``pod/unit.py``. A pod with no ``MEMORY=``
#: or ``CPU=`` key boots under these.
DEFAULT_MEMORY = "4G"
DEFAULT_CPU = "200%"

#: Env-file keys. Short like ``PORT`` and ``SEED``; the value is systemd syntax.
MEMORY_KEY = "MEMORY"
CPU_KEY = "CPU"

#: Smallest ceilings accepted. A gateway plus one agent does not boot in less
#: than half a gigabyte, and a quota below a tenth of a core starves the health
#: check long before it limits anything useful.
MIN_MEMORY_BYTES = 512 * 1024**2
MIN_CPU_PERCENT = 10

_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_MEMORY_RE = re.compile(r"^([1-9][0-9]{0,8})([KMGT])$")
_CPU_PERCENT_RE = re.compile(r"^([1-9][0-9]{0,5})%$")
_CPU_CORES_RE = re.compile(r"^([1-9][0-9]{0,3})$")


class LimitError(ValueError):
    """A memory or CPU value was malformed, too small, or above the host cap."""


@dataclass(frozen=True)
class PodLimits:
    """A pod's effective ceilings in systemd spelling, and whether each is a default."""

    memory: str
    cpu: str
    memory_default: bool
    cpu_default: bool

    def as_json(self) -> dict[str, str]:
        return {"memory": self.memory, "cpu": self.cpu}

    def label(self) -> str:
        return f"mem={self.memory} cpu={self.cpu}"


def memory_bytes(value: str) -> int:
    """Bytes named by a systemd-style size (``512M``, ``16G``). Raises LimitError."""
    match = _MEMORY_RE.match(value.strip().upper())
    if not match:
        raise LimitError(
            f"memory {value!r} is not a size: use a whole number with K, M, G or T, "
            "for example 16G or 8192M"
        )
    return int(match.group(1)) * _UNITS[match.group(2)]


def cpu_percent(value: str) -> int:
    """Quota percent named by ``800%`` or a bare core count ``8``. Raises LimitError."""
    raw = value.strip()
    match = _CPU_PERCENT_RE.match(raw)
    if match:
        return int(match.group(1))
    match = _CPU_CORES_RE.match(raw)
    if match:
        return int(match.group(1)) * 100
    raise LimitError(
        f"cpu {value!r} is not a quota: use a percentage such as 800% (eight cores) "
        "or a whole core count such as 8"
    )


def _format_bytes(n: int) -> str:
    for suffix in ("T", "G", "M", "K"):
        if n % _UNITS[suffix] == 0:
            return f"{n // _UNITS[suffix]}{suffix}"
    return f"{n // _UNITS['K']}K"


def host_memory_cap_bytes() -> int | None:
    """Largest ``MemoryMax`` a pod may ask for, or None when RAM cannot be read."""
    from kiro_crew import platform_compat
    from kiro_crew.sandbox_slice import _CGROUP_TOTAL_MEMORY_FRACTION

    total_mib = platform_compat.host_total_mib()
    if total_mib <= 0:
        return None
    return int(total_mib * _CGROUP_TOTAL_MEMORY_FRACTION) * 1024**2


def host_cpu_cap_percent() -> int | None:
    """Largest ``CPUQuota`` a pod may ask for, or None when the CPU count is unknown.

    ``os.cpu_count`` rather than the caller's affinity mask: the quota applies to
    a unit the user service manager starts, outside whatever cpuset the invoking
    shell runs in.
    """
    count = os.cpu_count()
    return count * 100 if count else None


def normalize_memory(value: str) -> str:
    """Validate *value* against the floor and the host cap; return systemd spelling."""
    n = memory_bytes(value)
    if n < MIN_MEMORY_BYTES:
        raise LimitError(f"memory {value!r} is below the {_format_bytes(MIN_MEMORY_BYTES)} floor")
    cap = host_memory_cap_bytes()
    if cap is not None and n > cap:
        raise LimitError(
            f"memory {value!r} is above this host's pod cap of {_format_bytes(cap)} "
            "(80% of physical RAM, the same ceiling the agents slice uses)"
        )
    return _format_bytes(n)


def normalize_cpu(value: str) -> str:
    """Validate *value* against the floor and the host cap; return ``N%``."""
    pct = cpu_percent(value)
    if pct < MIN_CPU_PERCENT:
        raise LimitError(f"cpu {value!r} is below the {MIN_CPU_PERCENT}% floor")
    cap = host_cpu_cap_percent()
    if cap is not None and pct > cap:
        raise LimitError(f"cpu {value!r} is above this host's cap of {cap}% ({cap // 100} CPUs)")
    return f"{pct}%"


def from_env(env_data: dict[str, str]) -> PodLimits:
    """The ceilings a pod's env file asks for, defaults filling any gap.

    Re-validated on every read, because the env file is hand-editable and the
    result is rendered into a unit file: a value systemd rejects would fail the
    start with an opaque parse error, and an over-cap value written by hand would
    otherwise slip past the check ``pod up`` made. Raises :class:`LimitError`.
    """
    raw_mem = env_data.get(MEMORY_KEY, "").strip()
    raw_cpu = env_data.get(CPU_KEY, "").strip()
    return PodLimits(
        memory=normalize_memory(raw_mem) if raw_mem else DEFAULT_MEMORY,
        cpu=normalize_cpu(raw_cpu) if raw_cpu else DEFAULT_CPU,
        memory_default=not raw_mem,
        cpu_default=not raw_cpu,
    )


def describe(env_data: dict[str, str]) -> PodLimits:
    """Like :func:`from_env` for display: a bad hand-edit reads as its raw text.

    ``pod ls`` and ``pod status`` must not fail because one env file holds a value
    the next start will refuse; they show what is written so the operator can see
    it, and the start path reports the error.
    """
    try:
        return from_env(env_data)
    except LimitError:
        return PodLimits(
            memory=env_data.get(MEMORY_KEY, "").strip() or DEFAULT_MEMORY,
            cpu=env_data.get(CPU_KEY, "").strip() or DEFAULT_CPU,
            memory_default=not env_data.get(MEMORY_KEY, "").strip(),
            cpu_default=not env_data.get(CPU_KEY, "").strip(),
        )
