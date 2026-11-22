"""Start-up warning for a container memory limit at or under the critical threshold.

The posture's figure (:mod:`kiro_crew.resource_status`) is clamped to the
container's cgroup headroom, so it can never rise above the container's memory
limit, while ``agent.resource_critical_gb`` is an absolute figure. In a container
whose limit is at or under it, that figure never rises above the threshold for
the container's whole life. This module names that once, with the settings to
lower. It changes no threshold and gates nothing.

The RAW ``resource_critical_gb`` is compared, not the value
``resource_status._resolve_thresholds`` clamps to the pressure threshold: the
adaptive spawn gate reads the raw value, so it still pauses when pressure was
lowered and critical was not.
"""

from __future__ import annotations

import asyncio
import logging

from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

_GIB = 1024**3

#: Strong reference to the boot task (the loop holds tasks weakly).
_NOTICE_TASK: asyncio.Task[None] | None = None


def warn_container_limit_at_critical(cfg: KiroCrewConfig) -> None:
    """Log one WARNING when the container memory limit is at or under the critical threshold.

    Blocking cgroup reads: call it off the event loop.
    """
    critical_gb = float(cfg.agent.resource_critical_gb)
    if critical_gb <= 0:
        return
    # Lazy: ``subagent`` is heavy, and this runs off the boot path.
    from kiro_crew.subagent import _container_cgroup_reading

    limit_bytes, _ = _container_cgroup_reading()
    if limit_bytes is None:
        return
    limit_gib = limit_bytes / _GIB
    if limit_gib > critical_gb:
        return
    logger.warning(
        "The container memory limit (%.1f GB) is at or under agent.resource_critical_gb "
        "(%.1f GB), so available memory can never rise above it. For a container this "
        "small, lower agent.resource_critical_gb, agent.resource_pressure_gb and "
        "agent.spawn_min_memory_gb.",
        limit_gib,
        critical_gb,
    )


def start_container_limit_notice(cfg: KiroCrewConfig) -> None:
    """Schedule the notice in the background so it never delays gateway readiness."""
    global _NOTICE_TASK

    async def _run() -> None:
        try:
            await asyncio.to_thread(warn_container_limit_at_critical, cfg)
        except Exception:
            logger.debug("container memory limit notice failed", exc_info=True)

    _NOTICE_TASK = asyncio.create_task(_run(), name="container-limit-notice")
