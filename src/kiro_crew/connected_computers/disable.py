"""What turning the ``connected_computers`` switch off does on the remote gateway.

Hiding the UI is not enough, so the config watcher calls :func:`apply` on every
``dashboard.feature_previews`` change, and :func:`apply` schedules the work off
the watcher's cycle (an applier is cancelled after 10 s, and a cancel must never
leave a teardown half done):

* **off** -- every connector socket is told to stop and closed; each open
  channel ends as interrupted, so a running call reports its outcome unknown.
  Chat bindings, device records and every file are kept.
* **on** -- nothing starts: new connects are admitted by the gates themselves,
  which read the switch live.

While the switch stays off nothing here runs: :func:`apply` returns before
scheduling anything, and :func:`shutdown` finds no broker to close.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.connected_computers.flag import PREVIEW_NAME, preview_on

logger = logging.getLogger(__name__)

_TASKS: "set[asyncio.Task[Any]]" = set()


def touched(change: Any) -> bool:
    return bool(
        getattr(change, "touched", None)
        and change.touched(
            f"dashboard.feature_previews.{PREVIEW_NAME}", "dashboard.feature_previews"
        )
    )


async def turn_off(reason: str = "connected computers was turned off") -> None:
    from kiro_crew.connected_computers.broker import get_broker

    closed = await get_broker().shutdown_all(reason)
    logger.info("connected computers off: %d connector socket(s) closed", closed)


def apply(change: Any) -> None:
    """Schedule the teardown for a feature-preview change (never blocks)."""
    if not touched(change):
        return
    new_on = preview_on(getattr(change, "new", None) or object())
    old_on = preview_on(getattr(change, "old", None) or object())
    if new_on or new_on == old_on:
        return
    task = asyncio.ensure_future(turn_off())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def shutdown() -> None:
    """Gateway shutdown: close what runs, keep every record and file."""
    from kiro_crew.connected_computers import broker as broker_mod

    try:
        await asyncio.gather(*list(_TASKS), return_exceptions=True)
        if broker_mod._BROKER is not None:
            await broker_mod._BROKER.shutdown_all("the gateway is shutting down")
    except Exception:  # noqa: BLE001 -- shutdown must not fail on an optional feature
        logger.warning("connected computers: shutdown cleanup failed", exc_info=True)
