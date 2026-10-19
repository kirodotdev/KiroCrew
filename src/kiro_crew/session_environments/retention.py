"""The sprite, boxd and Fargate branches of archive, reopen, shutdown and the sweep.

``coder/retention.py`` owns the per-session lifecycle lock and generation and
the Coder steps, and calls these under that same lock: each VM provider gets
its own archive, reopen, shutdown and sweep, and one provider's failure never
stops another's. A Fargate task is stopped at archive, kept alive by the
heartbeat, and its owed stops are retried by the sweep.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: boxd machines hibernated at once on shutdown (each is two CLI calls).
_SHUTDOWN_HIBERNATE_PARALLEL = 8
#: provider label -> the last sweep failure logged for it, so the same failure
#: every minute is one warning, not sixty.
_last_vm_sweep_error: dict[str, str] = {}


async def _live_config() -> Any:
    """The live config, or a fresh load before the gateway publishes one."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    return config


async def _section() -> Any:
    return (await _live_config()).session_environments


async def _persistent_environments() -> list[Any]:
    """Every VM provider's environments, so their saved rows are swept even after
    every profile of that provider is removed; none while the developer flag is
    off (no VM call then runs for any row). Read from the one provider table
    (``routing._vm_environments``), never listed here."""
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_environment import session_environments_enabled
    from kiro_crew.session_environments.routing import _vm_environments

    if not session_environments_enabled():
        return []
    section = await _section()
    home = await asyncio.to_thread(data_home)
    return [cls(section, home) for cls in _vm_environments().values()]


async def archive_vms(persistent: list[Any], key: str) -> None:
    """Schedule each VM provider's machine for ``key`` for deletion. Never raises."""
    for provider in persistent:
        try:
            await provider.archive_session(key)
        except Exception:  # noqa: BLE001 -- never surfaces into the close
            logger.warning(
                "Archived session %s machine: could not schedule its deletion",
                provider.label,
                exc_info=True,
            )


async def reopen_vms(persistent: list[Any], key: str) -> Optional[str]:
    """Cancel each VM provider's pending deletion for ``key``; why one still
    stands (for a retryable refusal), or None."""
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    failure: Optional[str] = None
    for provider in persistent:
        try:
            await provider.reopen_session(key)
        except Exception as exc:  # noqa: BLE001 -- the other providers still run
            logger.warning(
                "Reopened session %s machine: could not keep it",
                provider.label,
                exc_info=True,
            )
            reason = str(exc) if isinstance(exc, SessionEnvironmentUnavailable) else ""
            failure = failure or (
                reason
                or f"its {provider.label} machine's deletion could not be cancelled; " "try again"
            )
    return failure


async def _fargate_environments() -> Optional[Any]:
    """The live Fargate environments, or None while the developer flag is off (no
    Fargate registry read or AWS call then runs). Constructing them makes no AWS call."""
    from kiro_crew.config.paths import data_home
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments
    from kiro_crew.session_environment import session_environments_enabled

    if not session_environments_enabled():
        return None
    return FargateSessionEnvironments(await _section(), await asyncio.to_thread(data_home))


async def _fargate_task_recorded(fargate: Any, session_key: str) -> bool:
    """Whether ``session_key`` has a recorded Fargate task; False when unreadable.

    The Fargate registry fails closed, but that must not stall a Coder archive:
    a task it cannot list gets no keep-alive either, so its bootstrap's idle exit
    still stops it.
    """
    if fargate is None:
        return False
    try:
        return bool(await fargate.has_task(session_key))
    except Exception:  # noqa: BLE001 -- one provider's failure never blocks another's
        logger.warning("Archived session: could not read its Fargate task", exc_info=True)
        return False


async def _archive_fargate(fargate: Any, session_key: str) -> None:
    """Stop an archived session's Fargate task now. The caller holds its lifecycle lock.

    A stopped task is gone with its files, so there is no retention window to
    honour; keeping it running would only bill for nothing. Never raises.
    """
    try:
        await fargate.archive_session(session_key)
    except Exception:  # noqa: BLE001 -- the bootstrap's idle exit still stops the task
        logger.warning("Archived session task: could not stop it", exc_info=True)


async def close_fargate_forwards() -> None:
    """End this gateway's own Fargate SSM forward children; never touches a task.

    Runs on every shutdown, whatever ``stop_on_shutdown`` says: the switch is
    about the tasks, and these are the gateway's processes, which would
    otherwise outlive it holding their loopback ports. A gateway that never
    loaded the Fargate launcher opened no forward, so shutdown does not load it.
    """
    launch = sys.modules.get("kiro_crew.fargate_session.launch")
    if launch is None:
        return
    await launch.close_forwards()


async def count_fargate_for_shutdown(counts: dict[str, int]) -> None:
    """Count every live Fargate task in ``counts["skipped"]`` and end its forwards.

    The task IS the workspace, so shutdown leaves it running (its row and secret
    survive for reconnect). Raises when the registry cannot be read (it fails
    closed); the forwards are ended either way.
    """
    try:
        environments = await _fargate_environments()
        if environments is None:
            return
        live_tasks = len(await asyncio.to_thread(environments.registry.all))
        # Read before adding: "+=" would load the count before the await and
        # lose a concurrent provider's increment.
        counts["skipped"] += live_tasks
    finally:
        # Its SSM forwards are this gateway's own processes, though: end
        # them whether or not the registry could be counted.
        await close_fargate_forwards()


async def keep_alive_once() -> int:
    """Touch every live Fargate task once; a failure is logged, never raised."""
    try:
        fargate = await _fargate_environments()
        if fargate is None:
            return 0
        return int(await fargate.keep_alive())
    except Exception:  # noqa: BLE001 -- the heartbeat outlives any one pass
        logger.warning("Retention sweep: Fargate keep-alive failed", exc_info=True)
        return 0


async def retry_fargate_stops() -> int:
    """Retry the Fargate stops still owed; how many tasks stopped. Never raises."""
    try:
        fargate = await _fargate_environments()
        if fargate is None:
            return 0
        stopped: int = await fargate.retry_stops()
        return stopped
    except Exception:  # noqa: BLE001 -- one provider's failure never blocks another's
        logger.warning("Retention sweep: Fargate session tasks failed", exc_info=True)
        return 0


async def hibernate_boxd_for_shutdown(counts: dict[str, int]) -> None:
    """Hibernate every owned boxd machine, counting each in ``counts["boxd"]``.

    A sprite needs no call: it idles by itself. Raises only when the boxd CLI
    itself cannot be reached; each machine's own failure is logged and skipped.
    """
    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.config.paths import data_home

    environments = BoxdSessionEnvironments(await _section(), await asyncio.to_thread(data_home))
    rows = await asyncio.to_thread(environments.registry.all)
    if not rows:
        return  # no CLI call, so a gateway without boxd pays nothing
    cli = await environments.cli()
    # In parallel, a few at a time: one by one, the later machines would
    # outlast the shutdown budget and stay running.
    gate = asyncio.Semaphore(_SHUTDOWN_HIBERNATE_PARALLEL)

    async def hibernate(row: Any) -> None:
        async with gate:
            try:
                record = await cli.get(row.name)
                if record is None:
                    return
                environments.check_identity(row, record)
                await cli.hibernate(row.name)
                counts["boxd"] += 1
            except Exception as exc:  # noqa: BLE001 -- boxd idles it on its own
                logger.info("Shutdown: boxd hibernate failed (%s); boxd idles it", exc)

    await asyncio.gather(*(hibernate(row) for row in rows))


async def _sweep_persistent() -> int:
    deleted = 0
    for provider in await _persistent_environments():
        try:
            deleted += await provider.sweep_due()
            _last_vm_sweep_error.pop(provider.label, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- one provider never stops the others
            message = str(exc)
            if _last_vm_sweep_error.get(provider.label) != message:
                logger.warning("Archived session %s sweep failed: %s", provider.label, message)
            _last_vm_sweep_error[provider.label] = message
    return deleted
