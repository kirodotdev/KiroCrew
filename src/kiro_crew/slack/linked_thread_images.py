"""Keep the pictures a Slack message carried when it is routed to a linked dashboard chat.

``slack/events.py`` downloads a message's files to temp files and deletes them when
the message's handler task ends. A message in a thread linked to a dashboard chat is
handed to that chat's turn by ``route_linked_thread``, which starts the turn in the
background (or queues it on a busy slot) and returns, so the handler task deletes
the files before the turn's ``build_prompt_blocks`` reads them.

The event path records the files it downloaded (:func:`set_inbound_files`), and
``route_linked_thread`` calls :func:`store_for_slot` before it hands the message
over. Each picture is copied into the linked session's attachments directory
(:meth:`ConversationLog.store_ingested_images`, the store other channels' rows use)
and its line is rewritten as image markup naming the copy, so the turn and the row
both read a file that is deleted with the session. Files that are not pictures, and
any picture the store refuses, keep their lines as written.

The copy runs off the event loop, so the slot can be closed, deleted or relinked
while it runs. Under the session lock the store refuses a session that was deleted
or is being deleted (the slot's own delete witness), so a copy never recreates a
deleted session's attachments. After the copy, the router's *still_current* check,
read on the loop, covers a close or relink: when it fails, the files are recorded
again and :func:`store_for_slot` answers ``None`` so the router resolves the thread
once more and stores for the slot it lands on. A copy left in the old session is not
removed here: stored names come from the content, so the same file can back a row
another writer committed, and the session's own deletion reclaims it.

The files travel in a context variable, not a parameter: the router is reached from
the native handler, the transport dispatcher and the queue drain, none of which
carries the download list, and each turn task inherits the value from the task that
created it. The recorded files are taken once: a second routing of the same message
in the same task stores nothing more.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, cast

from kiro_crew.dashboard.chat_utils_slots import slot_history_key
from kiro_crew.dashboard.slot_persistence.write_guards import session_was_deleted

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

_INBOUND_FILES: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "slack_linked_thread_inbound_files", default=()
)


def set_inbound_files(paths: Sequence[str]) -> None:
    """Record the temp files the current message's ingest downloaded.

    Set in the task that routes the message; tasks it creates inherit the value.
    """
    _INBOUND_FILES.set(tuple(paths))


async def store_for_slot(
    state: object, slot: _ChatSlot, text: str, *, still_current: Callable[[], bool]
) -> str | None:
    """*text* with each recorded picture stored in *slot*'s session; never raises.

    *state* is the dashboard state the Slack handler holds; only its
    ``conversation_log`` is read. ``None`` means *still_current* failed after the
    copy: the files are recorded again for the next slot.
    """
    files = _INBOUND_FILES.get()
    if not files:
        return text
    _INBOUND_FILES.set(())
    conv_log = getattr(state, "conversation_log", None)
    if conv_log is None:
        return text
    try:
        stored = await asyncio.to_thread(
            conv_log.store_ingested_images,
            slot_history_key(slot),
            text,
            files,
            # Read under the session lock: a session deleted before the copy
            # stores nothing, so its attachments are never recreated.
            refuse=lambda: session_was_deleted(cast("DashboardState", state), slot),
        )
    except Exception:
        # The text as the event path built it: the turn sees the temp paths, as
        # it would with no store at all.
        logger.warning("linked thread: could not store the message's pictures", exc_info=True)
        return text
    if still_current():
        return stored
    _INBOUND_FILES.set(tuple(files))
    return None
