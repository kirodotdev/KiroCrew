"""The sprite and boxd branches of archive, reopen, shutdown and the sweep.

``coder/retention.py`` owns the per-session lifecycle lock and generation and
the Coder steps, and calls these under that same lock: each VM provider gets
its own archive, reopen, shutdown and sweep, and one provider's failure never
stops another's.
"""

from __future__ import annotations

import asyncio
import logging
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
