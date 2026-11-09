"""The pending-approval note the notification bridge carries (RFC bridge actions, B4p).

When a tool-approval card opens, this publishes one ``system.approval`` note naming
the request, so the bridge has something to carry to a chat app. It is behind the
default-off feature preview ``dashboard.feature_previews.bridge_approvals``: with the
preview off nothing is published.

The note is bridge-only. It goes straight to the notification bridge and never to
the dashboard's own sink, so the bell keeps ringing only when an approval expires
(the dashboard already shows a pending approval inline). It carries no buttons:
acting on it from a chat app is a later phase.

A tool-approval note is told apart from the sealed-config review, the other
``system.approval`` producer, by its producer meta key (:data:`PRODUCER`), never by
its channel. When the card was already posted into a linked Slack thread, the note
says so (:data:`SLACK_CARD_KEY`), and the Slack sink reuses that card instead of
posting a second copy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: Hand-off tasks kept alive when the state holds no background-task set.
_HANDOFF_TASKS: "set[asyncio.Task[None]]" = set()

#: The feature preview that turns the note on (``dashboard.feature_previews``).
PREVIEW = "bridge_approvals"

#: The producer meta key, and the value that marks a pending tool approval.
PRODUCER_KEY = "producer"
PRODUCER = "tool_approval"

#: The meta key naming the request the note is about.
REQUEST_KEY = "approval_request_id"

#: The meta key set when the card is already posted in a linked Slack thread.
SLACK_CARD_KEY = "approval_slack_card"


def enabled(cfg: Any) -> bool:
    """Whether the preview is on in *cfg*. Off when unread."""
    from kiro_crew.edition_locks import feature_previews

    previews = feature_previews(getattr(getattr(cfg, "dashboard", None), "feature_previews", None))
    return isinstance(previews, Mapping) and previews.get(PREVIEW) is True


def is_pending_approval_note(note: Mapping[str, Any]) -> bool:
    """Whether *note* is this producer's pending tool-approval note."""
    return note.get("channel") == "system.approval" and note.get(PRODUCER_KEY) == PRODUCER


def publish_pending_approval(
    state: Any,
    cfg: Any,
    *,
    slot_key: str,
    session_key: str,
    request_id: str,
    tool_title: str,
    session_title: str = "",
    slack_card: bool = False,
) -> dict[str, Any] | None:
    """Hand one pending-approval note to the notification bridge. Never raises.

    Returns the note handed over, or None when the preview is off, the gateway has
    no bridge, or the note could not be built.
    """
    try:
        if not enabled(cfg) or getattr(state, "notification_bridge", None) is None:
            return None
        from kiro_crew.notifications.bus import NotificationBus, payload_from_legacy

        where = f" in {session_title}" if session_title else ""
        meta: dict[str, Any] = {
            PRODUCER_KEY: PRODUCER,
            REQUEST_KEY: str(request_id),
            "slot": slot_key,
            "session_key": session_key,
        }
        if slack_card:
            meta[SLACK_CARD_KEY] = True
        payload = payload_from_legacy(
            "approval",
            "Approval needed",
            f"{tool_title or 'A tool'} is waiting for approval{where}.",
            meta,
        )
        # The bridge is the only sink: the dashboard bell never sees this note.
        return NotificationBus(sink=state._bridge_note).push(payload)
    except Exception:
        logger.warning("pending-approval bridge note failed", exc_info=True)
        return None


def schedule_pending_approval(
    state: Any, decision: "asyncio.Future[Any]", **fields: Any
) -> "asyncio.Task[None] | None":
    """Start the hand-off of one open approval as its own task, and return it.

    The permission prompt calls this and never awaits the result: it registered its
    approval and must reach the ``try`` that cleans it up with no new await in
    between, or a cancel landing there would strand the approval. The task reads
    the live config off the loop, sends nothing for an approval answered in the
    meantime, and is held until done (the gateway's background set, else this
    module's) with any failure logged. *fields* are
    :func:`publish_pending_approval`'s keywords.
    """
    try:
        task = asyncio.get_running_loop().create_task(_hand_off(state, decision, fields))
    except RuntimeError:
        return None  # No running loop: nothing can bridge.
    tasks = getattr(state, "_background_tasks", None)
    if not isinstance(tasks, set):
        tasks = _HANDOFF_TASKS
    # Held until done: the loop keeps only a weak reference to a task.
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_failure)
    return task


async def _hand_off(state: Any, decision: "asyncio.Future[Any]", fields: dict[str, Any]) -> None:
    from kiro_crew.config import KiroCrewConfig

    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        return  # No config, no preview: nothing is bridged.
    if decision.done():
        return  # Answered while the config was read: nothing is pending to send.
    publish_pending_approval(state, cfg, **fields)


def _log_failure(task: "asyncio.Task[None]") -> None:
    """Log a hand-off that raised, so a failure is never silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("pending-approval bridge hand-off failed", exc_info=exc)
