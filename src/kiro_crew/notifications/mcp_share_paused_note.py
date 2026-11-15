"""One bell note when a shared MCP server that was approved gets paused.

A gateway start refuses a shared server's launch when it differs from every
approved one (``changed_needs_reapproval``). The server then runs one copy per
chat, so nothing in a chat says so. This publishes one ``system.approval`` note
naming the paused servers, with a link to the page where they are approved.

The note carries a ``key``, so the bell holds one such note, and records the
launch each server was refused for. A start that finds the same launches paused
as the stored note publishes nothing: the owner may have read that note, and a
restart is not news. A start that finds nothing paused removes the stored note,
so the same launches can be announced if they pause again; a start with no
shared server runs this too. A different set of launches first removes the
stored note, so the new one rings instead of replacing it quietly. Only a
stored note stops a repeat, so a note the owner deleted is published again at
the next start while the servers stay paused.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

CHANNEL = "system.approval"
#: Replaces the stored note of the same source, so the bell holds one.
KEY = "mcp-share-paused"
#: The note field that records which launches it was published for.
NOTE_KEY = "_mcp_share_paused"
#: The MCP Servers page's Sharing view, with its Review dialog opened.
REVIEW_URL = "/mcp?view=sharing&review=1"


def paused_approved_shares() -> dict[str, str]:
    """Share-list name -> refused launch, for each approved launch that changed. BLOCKING: reads files."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.mcp_gateway.launch_approval import (
        REFUSED_CHANGED,
        refused_servers,
        refused_share_records,
        refused_shares,
    )

    cfg = KiroCrewConfig.load().mcp_gateway
    if not cfg.enabled:
        return {}
    refused = refused_servers()
    reasons = refused_shares(cfg.stub_servers, refused)
    # A server refused again for another launch is a new pause, so the launch
    # (empty when the store lacks it) is part of the identity.
    return {
        name: str(record.get("expected_launch", ""))
        for name, record in sorted(refused_share_records(cfg.stub_servers, refused).items())
        if reasons.get(name) == REFUSED_CHANGED
    }


async def publish(state: Any) -> bool:
    """Publish the note for the servers paused now. True when a note was pushed."""
    from kiro_crew.notifications.bus import NotificationPayload

    paused = await asyncio.to_thread(paused_approved_shares)
    stored = [n for n in getattr(state, "_notification_log", ()) if NOTE_KEY in n]
    if not paused:
        if stored:
            _removed, persisted = await state.dismiss_notification(source="system", key=KEY)
            if not persisted:
                logger.warning("paused-share note: could not remove the resolved note")
        return False
    record = stored[-1].get(NOTE_KEY) if stored else None
    if isinstance(record, dict) and record.get("launches") == paused:
        return False
    if stored:
        # A same-key push at the same priority replaces the old note quietly,
        # so a new pause would ring nothing. Remove the old note first.
        _removed, persisted = await state.dismiss_notification(source="system", key=KEY)
        if not persisted:
            logger.warning("paused-share note: could not remove the earlier note")
            return False
    names = list(paused)
    listed = ", ".join(names)
    note = state.notification_bus.push(
        NotificationPayload(
            source="system",
            channel=CHANNEL,
            # Not the channel's critical default: a paused share blocks no agent
            # turn, each chat starts its own copy. A user override can raise it.
            priority="default",
            kind="agent",
            title="A shared MCP server is paused",
            body=f"Paused: {listed}. Review shared servers to approve the update and share again.",
            url=REVIEW_URL,
            key=KEY,
            i18n={"id": "mcp_share_paused", "params": {"names": listed}},
        )
    )
    # Set before any await: the keyed write copies the note when it runs, so the
    # record is stored with it and a restart does not notify again.
    note[NOTE_KEY] = {"launches": paused}
    # Read before any await: a later note replaces the state-wide future.
    persisted = getattr(state, "last_notification_persist", None)
    if persisted is not None and not await persisted:
        logger.warning("paused-share note: the note was not stored")
        return False
    return True
