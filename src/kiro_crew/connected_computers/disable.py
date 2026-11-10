"""What turning the ``connected_computers`` switch off (or on) does, on either machine.

Hiding the UI is not enough, so the config watcher calls :func:`apply` on every
``dashboard.feature_previews`` change, and :func:`apply` schedules the work off
the watcher's cycle (an applier is cancelled after 10 s, and a cancel must never
leave a teardown half done):

* **off, remote gateway** -- every connector socket is told to stop and closed;
  each open channel ends as interrupted and its tool server stops. Nothing is
  retried or fabricated. Chat bindings, device records and every file are kept.
* **off, local machine** -- every active grant is revoked in the vault (so a
  later switch-on never silently restores access), each connector and its
  process trees stop, and each remote gateway is told when reachable.
* **on** -- every share saved before it is revoked first (one the switch was
  turned off over while no gateway ran here never comes back), then the local
  controller starts and finds no grant until the user consents again natively.

While the switch stays off nothing here runs: :func:`apply` returns before
scheduling anything, :func:`start_if_enabled` creates no controller and reads
no vault, and :func:`shutdown` finds nothing to stop. New work is refused by the
gates themselves, which read the switch live.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.connected_computers.flag import PREVIEW_NAME, local_switch, preview_on

logger = logging.getLogger(__name__)

_TASKS: "set[asyncio.Task[Any]]" = set()
#: A switch-on made while startup still settles waits for it.
_STARTUP_TASK: asyncio.Task[bool] | None = None


def touched(change: Any) -> bool:
    return bool(
        getattr(change, "touched", None)
        and change.touched(
            f"dashboard.feature_previews.{PREVIEW_NAME}", "dashboard.feature_previews"
        )
    )


async def turn_off(reason: str = "connected computers was turned off") -> None:
    from kiro_crew.connected_computers.broker import get_broker
    from kiro_crew.connected_computers.controller import get_controller

    closed = await get_broker().shutdown_all(reason)
    revoked = await get_controller().off()
    logger.info(
        "connected computers off: %d connector socket(s) closed, %d local grant(s) revoked",
        closed,
        len(revoked),
    )


def apply(change: Any) -> None:
    """Schedule the teardown or start for a feature-preview change (never blocks)."""
    if not touched(change):
        return
    new_on = local_switch(getattr(change, "new", None) or object())
    old_on = local_switch(getattr(change, "old", None) or object())
    if new_on is None or new_on == old_on:
        return
    if new_on:
        # Recovering an unreadable value is not an observed off-to-on transition.
        if old_on is None:
            task = asyncio.ensure_future(_recover_local())
        else:
            task = asyncio.ensure_future(_start_local())
    else:
        task = asyncio.ensure_future(turn_off())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def startup_settled() -> bool:
    """All gateway starts, including direct reconciles, wait for the startup start."""
    return _STARTUP_TASK is None or await asyncio.shield(_STARTUP_TASK)


async def _start_local() -> None:
    """The switch went on while this gateway ran: revoke what was saved, then start.

    Nothing saved can be a share made under this switch-on, since every consent
    is refused while the switch is off; whatever is active was left by an Off
    this gateway never saw, so it is revoked before any connector dials.
    """
    from kiro_crew.connected_computers.controller import get_controller

    if not await startup_settled():
        return
    if await asyncio.to_thread(local_switch) is not True:
        return
    revoked = await get_controller().off()
    if revoked:
        logger.info("connected computers on: %d saved share(s) revoked first", len(revoked))
    get_controller().start()


async def _recover_local() -> None:
    """Resume saved grants after uncertainty without treating it as a fresh opt-in."""
    from kiro_crew.connected_computers.controller import get_controller

    if not await startup_settled() or await asyncio.to_thread(local_switch) is not True:
        return
    controller = get_controller()
    controller.start()
    await controller.reconcile()


async def _settle_startup() -> bool:
    """Settle an admitted startup: revoke on authoritative Off, preserve on unknown."""
    from kiro_crew.connected_computers.controller import get_controller

    try:
        switch = await asyncio.to_thread(local_switch)
        if switch is False:
            await get_controller().off()
        elif switch is True:
            get_controller().start()
        return True
    except Exception:  # noqa: BLE001 -- keep starts blocked without stopping the gateway
        logger.warning("connected computers: local startup did not settle", exc_info=True)
        return False


def start_if_enabled() -> None:
    """Post-bind startup: start the local controller only when the switch is on.

    The switch is read from the live snapshot, in memory. Off (or no snapshot)
    starts nothing and touches no vault; a later switch-on is :func:`apply`'s.
    """
    global _STARTUP_TASK
    from kiro_crew.config import live

    snap = live.snapshot()
    if snap is None or not preview_on(snap):
        return
    _STARTUP_TASK = asyncio.create_task(_settle_startup())
    _TASKS.add(_STARTUP_TASK)
    _STARTUP_TASK.add_done_callback(_TASKS.discard)


async def shutdown() -> None:
    """Gateway shutdown: stop what runs, keep every grant, record and file."""
    import sys

    from kiro_crew.connected_computers import broker as broker_mod

    try:
        # A queued startup must not create a controller after shutdown passed it.
        await asyncio.gather(*list(_TASKS), return_exceptions=True)
        controller_mod = sys.modules.get("kiro_crew.connected_computers.controller")
        if controller_mod is not None and controller_mod._CONTROLLER is not None:
            await controller_mod._CONTROLLER.shutdown()
        if broker_mod._BROKER is not None:
            await broker_mod._BROKER.shutdown_all("the gateway is shutting down")
    except Exception:  # noqa: BLE001 -- shutdown must not fail on an optional feature
        logger.warning("connected computers: shutdown cleanup failed", exc_info=True)
