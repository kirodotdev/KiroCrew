"""The SEL row every manual session rename writes.

Split out of :mod:`kiro_crew.dashboard.chat_title`, whose
:func:`~kiro_crew.dashboard.chat_title.apply_manual_title` calls it.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.chat_title")


def record_manual_rename(
    slot: Any, old_title: str, *, route: str, caller: str, source: str = ""
) -> None:
    """Write one ``chat.manual_rename`` row under *caller* for *slot*'s new title.

    *source* is the row's surface; empty lets the SEL infer it from *caller*, which
    is right for a session key and wrong for an app name. The SEL writer redacts
    the titles in metadata. A failed write is logged and swallowed, so a broken
    audit log never fails a rename.
    """
    try:
        sel().log_tool_invocation(
            session_key=caller,
            source=source,
            tool_name="chat.manual_rename",
            outcome="allowed",
            resources=slot.key,
            metadata={"route": route, "old_title": old_title or "", "new_title": slot.title},
        )
    except Exception:  # noqa: BLE001 - an audit failure must not fail the rename
        logger.warning("Rename of slot %s could not be audited", slot.key, exc_info=True)


def record_rename_route_access(caller: str, slot_key: str) -> None:
    """Write the rename route's own ``chat.slot_rename`` API-access row.

    It records the request's ``forwarded`` provenance, which the manual-rename row
    does not carry, so both rows are written for a route rename.
    """
    sel().log_api_access(
        caller=caller,
        operation="chat.slot_rename",
        outcome="allowed",
        source="dashboard",
        resources=slot_key,
    )
