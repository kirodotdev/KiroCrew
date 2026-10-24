"""The dashboard's half of a channel turn's live view (``messaging/dashboard_live.py``).

A channel turn (a Slack thread answering) runs on the channel path and writes only
two rows to the transcript its dashboard tab is bound to: the user's message at
receipt and the reply at the end. :class:`ChannelLiveSink` puts the rest of a
dashboard turn on the tab while it runs, in the dashboard's own shapes:

* **Tool steps** become the same transcript row a dashboard turn appends
  (``🔧 <title>``, ``msg msg-tool``, the same row meta) and the same ``tool_call``
  frame, both built by the dashboard runner's own helpers
  (``tool_rows._tool_call_ws_payload`` / ``_tool_meta``) so the labels cannot
  drift. The frame is what draws the tool pill and the sidebar's current-step
  line; a result arrives as the same ``tool_result`` frame.
* **Streamed text** goes out as ``chat_chunk`` frames on the slot's own chunk
  counter, so the tab streams the answer instead of "Thinking...".

The tool row is written to the shared TRANSCRIPT, not appended to the tab's
window. The tab's window of a channel conversation has exactly one writer -- the
refresh that copies new transcript lines into it
(``channel_slots.refresh_channel_window``) -- and keeping it that way is what
makes the final refresh land without duplicates: the step is a transcript line
like any other, so the refresh after the reply appends only the reply. Appending
to the window directly would mark the slot dirty, which both defers that refresh
and invites the slot's own save to re-serialize rows the channel path is still
appending to. So the step is appended to disk, then a one-session refresh pulls
it into the window, which broadcasts the row the way any refreshed row is.

The writes run on one background writer, in order, off the turn's path: the
driver never waits on the transcript's file lock. Before the transport writes the
reply it calls :meth:`ChannelLiveSink.settle`, which waits (bounded) for the
writer to finish and otherwise ABANDONS what is left. Abandoning is decided under
the transcript's own append lock (``ConversationLog.atomic_appends``), the lock the
reply is written under, so a step either lands before the reply or not at all --
never below it.

Every method is best-effort for the caller (``DashboardLiveRenderer`` fences
them); none of them touches the channel's own output.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.acp.types import TERMINAL_TOOL_STATUSES
from kiro_crew.dashboard import channel_slots
from kiro_crew.history import mint_row_mid
from kiro_crew.messaging.renderer import OutputEvent

logger = logging.getLogger(__name__)

#: Longest :meth:`ChannelLiveSink.settle` waits for step writes still queued
#: before it abandons them. The reply waits on this, so it stays short.
SETTLE_TIMEOUT_SECS = 5.0


class ChannelLiveSink:
    """Project one channel turn into the dashboard tab bound to its session."""

    def __init__(
        self,
        state: Any,
        session_key: str,
        conversation_log: Any,
        *,
        allowed: Callable[[], bool] = lambda: True,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        self._state = state
        self._session_key = session_key
        self._log = conversation_log
        # Re-asked before every frame and every write: an ``!incognito`` landing
        # mid-turn stops the live view, not only the recording.
        self._allowed = allowed
        # The source fields the transport writes on every row of this turn.
        self._provenance = dict(provenance or {})
        self._writes: list[tuple[str, dict[str, Any]]] = []
        self._writer: asyncio.Task[None] | None = None
        self._abandoned = False
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_again = False

    def _slot(self) -> Any | None:
        if not self._allowed():
            return None
        return channel_slots.linked_channel_slot(self._state, self._session_key)

    def on_text(self, text: str) -> None:
        from kiro_crew.dashboard.chat_utils_completion import chunk_generation

        slot = self._slot()
        if slot is None or not text:
            return
        # The slot's own counter, as the dashboard runner advances it: seq is a
        # per-slot sequence that continues across turns, and the tab's replay
        # floor drops anything at or below the newest seq it has seen.
        slot._chunk_seq += 1
        self._state.broadcast_ws(
            "chat_chunk",
            {
                "slot": slot.key,
                "content": text,
                "seq": slot._chunk_seq,
                "gen": chunk_generation(),
            },
        )

    def on_tool_step(self, raw: Any | None, event: OutputEvent) -> None:
        # Through chat_runner, which composes the chat_turn modules: tool_rows'
        # helpers resolve their shared names only once it has.
        from kiro_crew.dashboard.chat_runner import _tool_call_ws_payload, _tool_meta

        slot = self._slot()
        if slot is None:
            return
        source: Any = raw if raw is not None else _OutputEventAsToolCall(event)
        payload = _tool_call_ws_payload(source)
        payload["slot"] = slot.key
        self._state.broadcast_ws("tool_call", payload)
        if self._log is None or self._abandoned:
            return
        self._writes.append((f"🔧 {payload['tool']}", _tool_meta(source) or {}))
        if self._writer is None or self._writer.done():
            self._writer = asyncio.ensure_future(self._write_steps())

    def on_tool_result(self, raw: Any) -> None:
        from kiro_crew import mcp_apps_render, session_directive
        from kiro_crew.dashboard.chat_utils import _redact_tool_field

        slot = self._slot()
        tcid = getattr(raw, "tool_call_id", "") or ""
        if slot is None or not tcid:
            return
        # The dashboard's own result frame: the output redacted and capped by the
        # same helper, with the in-band markers a dashboard turn consumes before
        # it shows the output taken out rather than shown.
        output = session_directive.strip_marker(getattr(raw, "tool_output", "") or "")
        output = mcp_apps_render.strip_marker(output)
        status = getattr(raw, "tool_status", "") or ""
        self._state.broadcast_ws(
            "tool_result",
            {
                "slot": slot.key,
                "tool_call_id": _redact_tool_field(tcid),
                "output": _redact_tool_field(output),
                "final": bool(getattr(raw, "tool_final", False))
                or status in TERMINAL_TOOL_STATUSES,
            },
        )

    async def settle(self, timeout: float = SETTLE_TIMEOUT_SECS) -> None:
        """Let queued step writes land, then stop writing. Before the reply.

        Waits up to *timeout* for the writer; whatever has not been written by
        then is abandoned, so the reply written next is never followed by a step
        of the same turn. Never raises.
        """
        writer = self._writer
        if writer is not None and not writer.done():
            try:
                await asyncio.wait_for(asyncio.shield(writer), timeout=timeout)
            except Exception:
                logger.debug(
                    "dashboard live view: step writes still pending at the reply for %s",
                    self._session_key,
                )
        self.abandon()

    def abandon(self) -> None:
        """Write nothing more for this turn. Synchronous, so a ``finally`` can call it."""
        self._abandoned = True
        self._writes.clear()

    async def _write_steps(self) -> None:
        while self._writes and not self._abandoned:
            content, meta = self._writes.pop(0)
            try:
                written = await asyncio.to_thread(self._write_step, content, meta)
            except Exception:
                logger.debug(
                    "dashboard live view: step write failed for %s",
                    self._session_key,
                    exc_info=True,
                )
                continue
            if written:
                self._schedule_refresh()

    def _write_step(self, content: str, meta: dict[str, Any]) -> bool:
        # Under the lock the reply is appended under, so the abandon check and
        # the append are one step against it: a write that gets the lock after
        # the reply's sees the abandon and writes nothing.
        with self._log.atomic_appends(self._session_key):
            if self._abandoned or not self._allowed():
                return False
            self._log.append(
                self._session_key,
                "tool",
                content,
                cls="msg msg-tool",
                mid=mint_row_mid(),
                extra_meta=meta,
                **self._provenance,
            )
            return True

    def _schedule_refresh(self) -> None:
        task = self._refresh_task
        if task is not None and not task.done():
            # One refresh at a time; a step that lands while one runs is picked
            # up by one more pass, not by a second concurrent reader.
            self._refresh_again = True
            return
        self._refresh_task = asyncio.ensure_future(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        while True:
            self._refresh_again = False
            try:
                await channel_slots.refresh_linked_channel_slot(self._state, self._session_key)
            except Exception:
                logger.debug(
                    "dashboard live view: step refresh failed for %s",
                    self._session_key,
                    exc_info=True,
                )
            if not self._refresh_again:
                return


class _OutputEventAsToolCall:
    """A rendered tool call standing in for its provider event.

    Used only when the raw event was not seen (a driver without the tap), so the
    tab still gets a step: the driver's derived title, kind and purpose, and no
    arguments or identity, which the dashboard's helpers treat as absent.
    """

    def __init__(self, event: OutputEvent) -> None:
        self.title = event.title
        self.tool_kind = event.tool_kind
        self.is_shell = False
        self.tool_call_id = event.tool_call_id
        self.tool_purpose = event.tool_purpose
        self.tool_input = ""
        self.tool_name = event.tool_name
        self.mcp_server_name = ""
