"""Re-consent for apps whose ``sessionApproval`` grant predates the consent record.

The gateway runs :func:`reconcile_after_ready` once, after ``KIROCREW_READY``.
The grant itself is already refused on read (``app_can_manage_session_approvals``),
so nothing waits for this; it makes each such app show its consent step and
stops the code boot started for it while it still read as enabled.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def reconcile_after_ready() -> list[str]:
    """Disable and mark pending every pre-consent ``sessionApproval`` app.

    Per app, under ``app_lifecycle_lock`` so no install, update, uninstall,
    enable or disable of that app interleaves: the same teardown the disable
    route runs (hooks, backend, gateway-owned registrations), then the record
    write. The agent spec is rebuilt once at the end when anything changed.
    Never raises; returns the names marked.
    """
    try:
        return await _reconcile_after_ready()
    except Exception:
        logger.warning("session approval reconcile failed", exc_info=True)
        return []


async def _reconcile_after_ready() -> list[str]:
    from kiro_crew.apps.hooks_integration import stop_retained_startup_hooks  # noqa: PLC0415
    from kiro_crew.apps.manager import (  # noqa: PLC0415 - kept off the import path
        _session_approval_consent_unrecorded_on_disk,
        app_lifecycle_lock,
        get_app,
        mark_session_approval_consent_pending,
        session_approval_consent_candidates,
    )
    from kiro_crew.apps.teardown import teardown_app_runtime  # noqa: PLC0415

    marked: list[str] = []
    for name in await asyncio.to_thread(session_approval_consent_candidates):
        async with app_lifecycle_lock(name):
            # Re-check under the lock: the owner may have consented, reinstalled
            # or uninstalled the app since the listing, and teardown must stop
            # only an app that still qualifies.
            if not await asyncio.to_thread(_session_approval_consent_unrecorded_on_disk, name):
                continue
            info = await asyncio.to_thread(get_app, name)
            if not info:
                continue
            # The disable route's bounded preflight: a startup hook that never
            # exits must not hold this lock forever. Such an app is left for the
            # next start; its grant is still refused on read meanwhile.
            if not await stop_retained_startup_hooks(name, bounded=True):
                logger.warning(
                    "session approval reconcile deferred %s: startup hook still running", name
                )
                continue
            if info.get("enabled"):
                # Teardown first, while the record still reads enabled, matching
                # the disable route: it decides from ``enabled`` whether app code
                # is running and needs stopping.
                teardown = await teardown_app_runtime(name, info)
                for note in (*teardown.warnings, *teardown.failures):
                    logger.warning("session approval reconcile teardown of %s: %s", name, note)
            if await asyncio.to_thread(mark_session_approval_consent_pending, name):
                marked.append(name)
    if marked:
        logger.info("App(s) need session approval consent: %s", marked)
        from kiro_crew.agent import rebuild_agent_config  # noqa: PLC0415 - circular import

        try:
            await asyncio.to_thread(rebuild_agent_config, refresh_forks="defer")
        except Exception:
            logger.warning(
                "agent spec rebuild after session approval reconcile failed", exc_info=True
            )
    return marked
