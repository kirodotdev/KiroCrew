"""Say once that this Crew is reachable through the Kiro relay because of its default.

A Crew whose ``tunnel.relay_enabled`` is unset hosts when it has a team key or a
workload identity (:mod:`kiro_crew.remote_control.hosting`), with no ``on`` from
anyone. The first time such a Crew connects to the relay, :func:`announce` writes
one gateway log line and one ``system.agent`` bell note that say who can now reach
it and how to turn it off, then records that it did in
``<data home>/remote-control-notice.json``. A later connect, a restart, or a note the
owner deleted shows nothing again: the record, not the bell, is what keeps it to
once. A Crew that hosts because someone ran ``on`` never gets it.

The record is a courtesy, not a control: nothing reads it to decide whether the
Crew hosts, so it carries no sandbox fence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHANNEL = "system.agent"
SOURCE = "system"
#: Replaces a stored note of the same source, so the bell holds one.
KEY = "remote-control-default-host"
#: The record's file name in the data home.
RECORD_NAME = "remote-control-notice.json"

TITLE = "This Crew is now reachable through the Kiro relay"
TEXT = (
    "Remote Control is on by default because this Crew has a team key or a workload "
    "identity. People the Kiro relay lets open this Crew can now reach it. To turn "
    "it off: kirocrew remote-control off"
)


def record_path() -> Path:
    from kiro_crew.config.loader import config_dir

    return config_dir() / RECORD_NAME


def _shown() -> bool:
    """Whether the note was shown before. BLOCKING. An unreadable record counts as shown."""
    path = record_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        doc = json.loads(raw)
    except ValueError:
        return True
    return not isinstance(doc, dict) or doc.get("default_host_shown") is not False


def _record() -> None:
    """Record that the note was shown. BLOCKING."""
    from kiro_crew.atomic_write import atomic_write

    atomic_write(record_path(), json.dumps({"default_host_shown": True}) + "\n")


def _due() -> bool:
    """Whether to say it now: still hosting only by default, and never said. BLOCKING."""
    from kiro_crew.remote_control import setting

    return setting.by_default() and not _shown()


async def announce(state: Any) -> bool:
    """Log and ring the note if it is due, then record it. True when it was shown.

    Called once by the Remote Control guard, the first time the relay connects.
    Never raises: a notice must not stop the relay.
    """
    try:
        if not await asyncio.to_thread(_due):
            return False
        logger.warning("%s. %s", TITLE, TEXT)
        bus = getattr(state, "notification_bus", None)
        if bus is not None:
            from kiro_crew.notifications.bus import NotificationPayload

            bus.push(
                NotificationPayload(
                    source=SOURCE,
                    channel=CHANNEL,
                    priority="default",
                    title=TITLE,
                    body=TEXT,
                    key=KEY,
                )
            )
            # Read before any await: a later note replaces the state-wide future.
            persisted = getattr(state, "last_notification_persist", None)
            if persisted is not None and not await persisted:
                logger.warning("Remote Control default notice was not stored; it shows next start")
                return False
        await asyncio.to_thread(_record)
        return True
    except Exception:  # noqa: BLE001 - a notice must never stop the relay
        logger.warning("Remote Control default notice failed", exc_info=True)
        return False
