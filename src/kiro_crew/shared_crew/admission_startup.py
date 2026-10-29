"""Gateway start and stop checks for fair admission.

:func:`startup_check` probes containment before any runtime starts;
:func:`shutdown_cleanup` releases principal slices at stop. Both act on the
service :mod:`admission_gate` installs, which stays their import path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from kiro_crew.shared_crew import containment as _c

logger = logging.getLogger("kiro_crew.shared_crew.admission_gate")


async def startup_check() -> _c.ContainmentReport | None:
    """Probe containment at gateway start; in hard-limit mode a missing piece is fatal.

    Returns the report (None with admission off). With ``hard_limits`` on and a
    prerequisite missing, logs the machine reason at ERROR: from then on every
    admission and every runtime start is refused (``runtime_start``'s backstop),
    so nothing runs without its limits.
    """
    from kiro_crew.shared_crew import admission_gate as _g

    try:
        service = await _g.current_service()
        if not service.enabled:
            if not service.limits.hard_limits:
                return None
            report = _c.admission_off_report()
            _c.publish_report(report)
            logger.error(
                "shared crew: hard_limits is on but admission_enabled is off; "
                "no runtime will start until admission is enabled or hard_limits is off"
            )
            return report
        report = await service.containment_report(refresh=True)
    except Exception as exc:  # noqa: BLE001 -- fail closed below, never stop the gateway
        logger.warning("shared crew: startup containment check raised", exc_info=True)
        report = _c.failed_report(
            hard_limits=_g._configured_hard_limits(),
            detail=f"startup containment check raised {type(exc).__name__}",
        )
        _c.publish_report(report if report.hard_limits else None)
    if report.refusal_reason is not None:
        logger.error(
            "shared crew: hard-limit mode is on and containment is unavailable (%s); "
            "no work will be admitted and no runtime will start until it is fixed",
            report.refusal_reason,
        )
    elif not report.available:
        logger.warning(
            "shared crew: per-person containment is unavailable (%s); limits are "
            "admission counts only",
            report.reason,
        )
    return report


#: Sweeps the gateway's stop makes while a runtime scope still empties.
SHUTDOWN_SWEEP_ROUNDS = 8
SHUTDOWN_SWEEP_INTERVAL_SECONDS = 0.5


async def shutdown_cleanup(
    *,
    rounds: int = SHUTDOWN_SWEEP_ROUNDS,
    interval: float = SHUTDOWN_SWEEP_INTERVAL_SECONDS,
    pause: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> int:
    """Release owned principal slices at stop, then the empty instance slice.

    Retry populated slices for at most *rounds* while stopped runtimes empty.
    Keep any still occupied for cleanup at the next gateway start.
    """
    from kiro_crew.shared_crew import admission_gate as _g

    service = _g._SERVICE
    if service is None or service._slices is None:
        return 0
    released = 0
    for attempt in range(max(1, rounds)):
        try:
            released += await service.sweep_slices(force=True)
        except Exception:  # noqa: BLE001 -- cleanup never blocks the stop
            logger.debug("shared crew: slice cleanup at stop failed", exc_info=True)
            return released
        if not _g._runtime.principal_slices_left(service._slices) or attempt + 1 >= rounds:
            break
        await pause(interval)
    try:
        released += await asyncio.to_thread(
            _g._runtime.release_instance_slice,
            service._slices,
            populated=service._slices.populated,
        )
    except Exception:  # noqa: BLE001 -- cleanup never blocks the stop
        logger.debug("shared crew: instance slice release at stop failed", exc_info=True)
    return released


__all__ = [
    "SHUTDOWN_SWEEP_INTERVAL_SECONDS",
    "SHUTDOWN_SWEEP_ROUNDS",
    "shutdown_cleanup",
    "startup_check",
]
