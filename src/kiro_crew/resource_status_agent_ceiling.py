"""Which ceiling the advisory memory figure measured: the host's or the agents'.

The figure :mod:`kiro_crew.resource_status` classifies is the tightest of host
``MemAvailable``, a container limit and the headroom left under
``kirocrew-agents.slice``'s own ceiling (``subagent._available_memory_gb``).
When the slice binds, the figure is what agent processes have left together,
not what the host has free: agents can sit at that ceiling, throttled by the
kernel, while the host still holds the quarter of its RAM the ceiling keeps
back, and the dashboard's memory gauge, which reads host ``MemAvailable``, shows
plenty free. This module reads what an advisory needs to say which of the two it
means. It changes no figure and gates nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: Share of the agent ceiling that shared memory must reach before an advisory
#: names it. Below it ``shmem`` is ordinary process sharing; above it, it is
#: usually files in a tmpfs (``/dev/shm``, a tmpfs ``/tmp``), which stay charged
#: to the slice until they are deleted, and which no reclaim can drop on a host
#: without swap.
_SLICE_SHMEM_NOTABLE_RATIO = 0.10

_GIB = 1024**3


def gb_text(value: float) -> str:
    """*value* GB for an advisory: whole GB from 10 up, one decimal below.

    One decimal below 10 GB, so a host near empty reads "0.3 GB", not "0 GB".
    """
    return f"{value:.0f}" if value >= 10 else f"{value:.1f}"


@dataclass(frozen=True)
class AgentCeiling:
    """The agent slice's ceiling, read when its headroom is the memory figure."""

    ceiling_gb: float  # min(memory.high, memory.max) of kirocrew-agents.slice
    host_available_gb: float  # host MemAvailable, clamped by no cgroup
    shmem_gb: float  # shared memory charged to the slice; 0.0 when unreadable

    @property
    def shmem_notable(self) -> bool:
        """Whether shared memory holds enough of the ceiling to be worth naming."""
        return self.ceiling_gb > 0 and self.shmem_gb >= _SLICE_SHMEM_NOTABLE_RATIO * self.ceiling_gb

    def shmem_sentence(self) -> str:
        """The sentence naming shared memory, or ``""`` when it is not notable."""
        if not self.shmem_notable:
            return ""
        return (
            f"{gb_text(self.shmem_gb)} GB of agent memory is shared memory, such as files in "
            "/dev/shm or a tmpfs /tmp, which stays until those files are deleted."
        )


def read_agent_ceiling() -> AgentCeiling | None:
    """The agent slice's ceiling when its headroom binds the memory figure, else None.

    None when host ``MemAvailable`` or a container limit is the tighter reading,
    off Linux, and whenever an input cannot be read: the advisories then keep
    their host wording. The headroom comes from the same reader the memory
    figure uses (``subagent._agents_slice_available_gb``), so the two agree on
    what the slice has left. It is a second read, taken after the figure, so
    when host and slice headroom sit within what moved between the reads it can
    name either one; it only words the advisory. Never raises.
    """
    if not platform_compat.IS_LINUX:
        return None
    try:
        # Lazy, like resource_status's own slice probes: ``sandbox`` and
        # ``subagent`` are large, and ``resource_status`` is imported on the
        # per-turn context path.
        from kiro_crew import sandbox
        from kiro_crew.subagent import (
            _agents_slice_available_gb,
            _container_cgroup_available_gb,
        )

        host_mib = platform_compat.host_available_mib()
        slice_gb = _agents_slice_available_gb()
        if host_mib <= 0 or slice_gb < 0:
            return None
        host_gb = host_mib / 1024
        if slice_gb >= host_gb:
            return None
        container_gb = _container_cgroup_available_gb()
        if 0 <= container_gb < slice_gb:
            return None
        slice_dir = sandbox._agents_slice_cgroup_dir()
        if slice_dir is None:
            return None
        ceilings = [
            limit
            for limit in (
                sandbox.read_cgroup_int(slice_dir / "memory.high"),
                sandbox.read_cgroup_int(slice_dir / "memory.max"),
            )
            if limit is not None
        ]
        if not ceilings:
            return None
        shmem = sandbox._read_cgroup_counters(slice_dir / "memory.stat").get("shmem", 0)
        return AgentCeiling(
            ceiling_gb=min(ceilings) / _GIB,
            host_available_gb=host_gb,
            shmem_gb=shmem / _GIB,
        )
    except Exception:  # pragma: no cover - defensive; the probe must never raise
        logger.debug("agent-slice ceiling probe failed", exc_info=True)
        return None
