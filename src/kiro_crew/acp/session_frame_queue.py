"""A session's frame queue that keeps one copy of a re-sent state frame while parked.

A KAS engine re-sends a session's whole MCP status and its whole ``configOptions``
whenever something changes, idle sessions included, and an idle session reads its
queue only when its next turn starts. Left alone, those frames pile up for as
long as the session stays idle.

:meth:`SessionFrameQueue.drop_superseded_by` removes the unread frame a new one
makes redundant, so a parked queue holds at most one of each. It runs only on a
queue marked parked (``SessionFrameQueue.parked``): a queue is born unparked, the
session start parks it once it has read what it counts, and a turn unparks it for
as long as it runs. The turn loop and the session start count frames by the
queue's length, and neither runs on a parked queue. Who reads a parked queue's
frames, and why dropping one changes nothing they keep:

* the pre-turn drain feeds every between-turns frame to the sign-in tracker
  (``_note_mcp_sign_in_status``) and absorbs no ``configOptions``. The tracker
  reads only each server's name, status, ``failedAuthorization`` and
  ``errorMessage``, and reading two snapshots that agree on all of those leaves
  it where reading the newer one alone does. So a status frame is dropped only
  for a newer one that agrees on every field except the four lists a server
  carries (``tools``, ``prompts``, ``resources``, ``resourceTemplates``).
* the session's MCP report already folded every frame when the reader loop
  received it (``McpSessionReport.fold_frame``), so it never reads the queue.

A frame is never moved past another kind of frame: the search for the frame to
drop stops at the first one that is neither a status nor an options update.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, METHOD_SESSION_UPDATE, JsonRpcMessage

#: The lists a status entry carries that no reader of a parked queue compares.
_SERVER_LISTS = frozenset({"tools", "prompts", "resources", "resourceTemplates"})
_STATUS = "status"
_OPTIONS = "options"


def _kind(msg: Any) -> str | None:
    """Which re-sent state *msg* is, or ``None`` for any other frame."""
    if not isinstance(msg, JsonRpcMessage) or msg.id is not None:
        return None
    params = msg.params
    if not isinstance(params, dict):
        return None
    if msg.method == METHOD_KAS_MCP_STATUS:
        return _STATUS if isinstance(params.get("servers"), list) else None
    if msg.method == METHOD_SESSION_UPDATE:
        update = params.get("update")
        if isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update":
            return _OPTIONS
    return None


def _status_view(params: dict[str, Any]) -> dict[str, Any]:
    """*params* with each server's lists left out: what the sign-in tracker reads."""
    servers = [
        (
            {k: v for k, v in entry.items() if k not in _SERVER_LISTS}
            if isinstance(entry, dict)
            else entry
        )
        for entry in params["servers"]
    ]
    return {**params, "servers": servers}


def _supersedes(new: JsonRpcMessage, old: JsonRpcMessage, kind: str) -> bool:
    if new.params.get("sessionId") != old.params.get("sessionId"):
        return False
    if kind == _OPTIONS:
        # Each update replaces the whole set, and a parked queue's reader drops
        # them all unread.
        return True
    return _status_view(new.params) == _status_view(old.params)


class SessionFrameQueue(asyncio.Queue):  # type: ignore[type-arg]
    """One session's frame queue (``AcpRuntime._session_queues``).

    Born read, not parked: the session start counts its frames by the queue's
    length until it returns. The start parks it when it returns, a turn unparks
    it on entry (``AcpRuntime.mark_turn_active``) and parks it again on exit.
    """

    def __init__(self) -> None:
        super().__init__()
        #: Whether nothing counts this queue's frames, so a superseded one may go.
        self.parked = False

    def drop_superseded_by(self, msg: JsonRpcMessage) -> bool:
        """Remove the unread frame *msg* makes redundant; whether one was removed.

        Call it right before queueing *msg*, and only on a parked queue.
        """
        kind = _kind(msg)
        if kind is None:
            return False
        frames = self._queue  # type: ignore[attr-defined]
        for index in range(len(frames) - 1, -1, -1):
            old = frames[index]
            old_kind = _kind(old)
            if old_kind is None:
                return False
            if old_kind != kind or old.params.get("sessionId") != msg.params.get("sessionId"):
                continue
            # Only the nearest frame of the same kind is compared: one that
            # differs is a state the reader must still pass through, so nothing
            # older than it may go either.
            if not _supersedes(msg, old, kind):
                return False
            del frames[index]
            # The frame leaves unread: it is not owed a task_done().
            self._unfinished_tasks -= 1  # type: ignore[attr-defined]
            return True
        return False


def drop_superseded_if_parked(queue: Any, msg: JsonRpcMessage) -> bool:
    """The reader loop's step before it queues *msg*: drop what it supersedes, if parked.

    Synchronous on purpose. The caller queues *msg* right after with no await in
    between, so a turn cannot begin between the parked check and the frame
    landing: whatever it reads, it reads after both.
    """
    if not isinstance(queue, SessionFrameQueue) or not queue.parked:
        return False
    return queue.drop_superseded_by(msg)


def set_parked(queue: Any, parked: bool) -> None:
    """Mark *queue* parked (nothing counts its frames) or read; any other queue is left."""
    if isinstance(queue, SessionFrameQueue):
        queue.parked = parked
