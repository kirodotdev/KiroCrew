"""Goal loops on an app's own chat slots: stopping them when the app is switched off.

Disabling an app stops every AutoNudge loop on a slot stamped with that app, in the
disable request (:func:`stop_app_loops`) and again at the loop's next fire
(:func:`app_disabled_for_slot`), for a disable this process never saw.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The ``stopped_reason`` of a loop stopped because its slot's app was disabled.
APP_DISABLED_REASON = "app_disabled"


def slot_owner_app(slot: Any) -> str:
    """The app stamped on *slot* by the host, or ``""`` for an unscoped slot."""
    owner = getattr(slot, "_app", "") if slot is not None else ""
    return owner if isinstance(owner, str) else ""


def app_disabled_for_slot(slot: Any) -> bool:
    """Whether *slot* belongs to an installed app whose record reads disabled.

    An app with no installed record is not judged here: uninstalling runs
    :func:`stop_app_loops` in its own request.
    """
    from kiro_crew.apps.manager import _read_installed  # noqa: PLC0415

    owner = slot_owner_app(slot)
    if not owner:
        return False
    meta = _read_installed(owner)
    return meta is not None and not meta.enabled


def _audit_stop(app: str, loop: Any) -> None:
    from kiro_crew.sel import sel  # noqa: PLC0415

    try:
        sel().log_tool_invocation(
            session_key=loop.slot_key,
            source=f"app:{app}",
            tool_name="autonudge_stop",
            outcome="success",
            metadata={"loop_id": loop.id, "stopped_reason": APP_DISABLED_REASON},
        )
    except Exception:  # noqa: BLE001 - the stop already happened
        logger.warning("could not audit the app-disable stop of loop %s", loop.id, exc_info=True)


async def stop_loop_if_app_disabled(service: Any, loop: Any, slot: Any) -> bool:
    """Stop *loop* when *slot*'s app reads disabled; returns whether it was refused.

    The fire-time backstop for a disable this process never saw in the request. The
    stop is audited under ``app:<name>`` the same as :func:`stop_app_loops`.
    """
    if not await asyncio.to_thread(app_disabled_for_slot, slot):
        return False
    done = await service.update(loop.id, active=False, stopped_reason=APP_DISABLED_REASON)
    if done is not None:
        await asyncio.to_thread(_audit_stop, slot_owner_app(slot), loop)
    return True


async def stop_app_loops(app: str, *, service: Any = None, state: Any = None) -> int:
    """Stop every active loop on a live slot stamped with *app*; returns how many stopped.

    Never raises: it runs inside the disable teardown, which must complete.
    """
    if not app:
        return 0
    try:
        if service is None:
            from kiro_crew.autonudge import get_instance  # noqa: PLC0415

            service = get_instance()
        if state is None:
            from kiro_crew.slack.handler import get_dashboard_state  # noqa: PLC0415

            state = get_dashboard_state()
        if service is None or state is None:
            return 0
        from kiro_crew.autonudge_service.model import (  # noqa: PLC0415
            is_structured_monitor_loop,
        )

        stopped = 0
        for loop in list(service.list_all()):
            if not loop.active or is_structured_monitor_loop(loop):
                continue
            if slot_owner_app(state.get_slot(loop.slot_key)) != app:
                continue
            try:
                done = await service.update(
                    loop.id, active=False, stopped_reason=APP_DISABLED_REASON
                )
            except Exception:  # noqa: BLE001 - stop the rest regardless
                logger.warning("could not stop loop %s of app %s", loop.id, app, exc_info=True)
                continue
            if done is not None:
                stopped += 1
                await asyncio.to_thread(_audit_stop, app, loop)
        return stopped
    except Exception:  # noqa: BLE001 - the teardown must complete regardless
        logger.warning("could not stop the loops of app %s", app, exc_info=True)
        return 0
