"""Settle a finished sub-agent's completion once the parent read its result.

``spawn_status`` on a finished run returns that run's whole result to the parent's
own turn, just as a blocking ``spawn_sub_agents`` reply does. Without a mark the
run's completion event then plays as one more parent turn carrying the same
result. :class:`StatusReadAnswer` answers the call with the text it was given and,
once the frame carrying that text was written to the parent's pipe
(:meth:`DeferredTool.on_answered` with ``written`` True), sends the same
``POST /api/spawn/mark-collected`` the blocking tool sends. The gateway applies
its own rules to that mark (only the caller's own children, and only under the
attempt that spawned and read the run), so a mark it does not take leaves the
completion to be delivered: a duplicate at worst, never a loss.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.mcp_shared import DeferredTool
from kiro_crew.platform import redact_log_via_context as redact_log

logger = logging.getLogger(__name__)

#: Per-attempt timeouts, in seconds, for the mark. The same schedule the blocking
#: spawn tool uses: a second, longer attempt covers a loaded gateway, and the
#: worst case stays bounded on the thread that wrote the reply.
MARK_TIMEOUTS: tuple[float, ...] = (5, 10)


def post_mark_collected(ids: list[str], parent_session: str, *, tool: str) -> None:
    """Tell the gateway the parent now holds these children's results.

    A transport failure leaves the outcome unknown, so the mark is sent once
    more; a repeat of a mark that landed changes nothing. A definite refusal is
    not retried. Never raises: a failed mark only costs the duplicate turn.
    """
    body = {"ids": ids, "parent_session": parent_session, "written": True}
    data: dict[str, Any] = {}
    for timeout in MARK_TIMEOUTS:
        try:
            data = mcp_core._post("/api/spawn/mark-collected", body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - a failed mark must not fail the reply
            data = {"error": str(exc), "transport_error": True}
        if not data.get("error") or not data.get("transport_error"):
            break
    if data.get("error"):
        logger.warning(
            "%s: could not mark %d sub-agent(s) collected, so their completion "
            "events will also arrive: %s",
            tool,
            len(ids),
            redact_log(str(data["error"]))[:200],
        )


class StatusReadAnswer(DeferredTool):
    """Answer with ``text`` at once; mark ``agent_id`` collected after the write."""

    def __init__(self, text: str, agent_id: str, parent_session: str) -> None:
        self.text = text
        self.agent_id = agent_id
        self.parent_session = parent_session
        self._settled = False

    def due_at(self) -> float:
        return 0.0

    def step(self) -> str | None:
        return self.text

    def on_settled(self, text: str) -> None:
        self._settled = True

    def on_answered(self, written: bool) -> None:
        # Only a reply that reached the parent's own pipe may settle the
        # completion; a torn write or a pooled backend's answer marks nothing.
        if not written or not self._settled or not self.parent_session:
            return
        self._settled = False
        post_mark_collected([self.agent_id], self.parent_session, tool="spawn_status")


def answer_full_read(text: str, agent_id: str) -> "str | DeferredTool":
    """``text`` as a :class:`StatusReadAnswer` when the caller's session is known."""
    parent_session = mcp_core._resolve_session_key() or ""
    if not parent_session:
        return text
    return StatusReadAnswer(text, agent_id, parent_session)
