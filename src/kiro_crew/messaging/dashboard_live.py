"""Show a channel turn in its dashboard tab while it runs.

A conversation started in a channel (a Slack thread) has a dashboard tab bound to
the same session, but its turns run on the CHANNEL path: the transport's
``TurnDriver`` hands every ``OutputEvent`` to the channel's own renderer and the
tab learns of the turn only from the rows the transport writes to the shared
transcript -- the user's message at receipt, the reply at the end. In between the
tab had nothing to draw, so it said "Thinking..." for the whole turn.

Two pieces put the turn on the tab as it happens:

* :class:`DashboardLiveRenderer` is a second renderer for the same turn. It reads
  the events the driver already produces (streamed text, tool calls) plus the raw
  provider events the driver's ``event_tap`` shows it (a tool call's arguments and
  identity, a tool's result), and hands them to a :class:`LiveSink` that projects
  them into the tab. The sink is the dashboard's half (see
  ``dashboard/channel_live.py``); this module never imports the dashboard.
* :class:`TeeRenderer` lets the driver feed both: the channel's renderer FIRST,
  exactly as before, then the live one. The channel's output is unchanged by
  construction -- it receives the same events in the same order from the same
  call -- and nothing the live renderer does is visible to it.

The live renderer is best-effort and fenced: every sink call is guarded, and the
first failure switches it off for the rest of the turn, so a broken dashboard
costs the tab its live view and never the channel turn.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT
from kiro_crew.messaging.renderer import DONE, TEXT_CHUNK, TOOL_CALL, OutputEvent, Renderer

logger = logging.getLogger(__name__)

#: Raw tool-call events kept for the matching ``TOOL_CALL`` output event. The
#: driver dispatches that event right after the tap sees the raw one, so one
#: entry is the steady state; the cap only bounds a stream that announces calls
#: the driver never renders.
_MAX_PENDING_CALLS = 64


class LiveSink(Protocol):
    """Where a channel turn's live output goes: the dashboard's half.

    Every method but :meth:`settle` is synchronous and returns at once: whatever
    a step needs written is the sink's to queue, so the turn never waits on it.
    """

    def on_text(self, text: str) -> None:
        """Streamed assistant text, already redacted by the driver."""

    def on_tool_step(self, raw: Any | None, event: OutputEvent) -> None:
        """A tool call the driver rendered; *raw* is its provider event, if seen."""

    def on_tool_result(self, raw: Any) -> None:
        """A tool's result, as the provider sent it."""

    async def settle(self) -> None:
        """Finish (bounded) or abandon queued writes. Called before the reply is written."""

    def abandon(self) -> None:
        """Write nothing more for this turn."""


class DashboardLiveRenderer(Renderer):
    """Project one channel turn's output into its dashboard tab, best-effort."""

    channel_type = "dashboard"

    def __init__(self, sink: LiveSink, *, capabilities: Any = None) -> None:
        super().__init__(capabilities)
        self._sink = sink
        self._disabled = False
        self._pending_calls: dict[str, Any] = {}

    @property
    def disabled(self) -> bool:
        """Whether a failure switched the live view off for this turn."""
        return self._disabled

    def _fail(self, what: str) -> None:
        if not self._disabled:
            logger.warning(
                "dashboard live view: %s failed; the tab shows the rest of this turn "
                "when it ends",
                what,
                exc_info=True,
            )
        self._disabled = True

    def observe(self, raw: Any) -> None:
        """The driver's ``event_tap``: see each raw provider event."""
        if self._disabled:
            return
        try:
            kind = getattr(raw, "kind", "")
            if kind == EVENT_TOOL_CALL:
                if len(self._pending_calls) >= _MAX_PENDING_CALLS:
                    self._pending_calls.pop(next(iter(self._pending_calls)))
                self._pending_calls[str(getattr(raw, "tool_call_id", "") or "")] = raw
            elif kind == EVENT_TOOL_RESULT:
                self._sink.on_tool_result(raw)
        except Exception:
            self._fail("a provider event")

    async def dispatch(self, event: OutputEvent) -> None:
        if self._disabled:
            return
        try:
            if event.kind == TEXT_CHUNK:
                if event.text:
                    self._sink.on_text(event.text)
            elif event.kind == TOOL_CALL:
                raw = self._pending_calls.pop(str(event.tool_call_id or ""), None)
                self._sink.on_tool_step(raw, event)
            # Everything else is the channel's to render (approval prompts,
            # compaction notices) or arrives on the tab through the transcript
            # the transport writes (the reply, the empty-turn notice).
        except Exception:
            self._fail(f"a {event.kind} event")

    async def settle(self) -> None:
        """Let the sink's queued writes land (bounded) before the reply is written.

        Called by :class:`TeeRenderer` ahead of the channel renderer's ``DONE``
        (which is where a channel may write the reply) and by the transport ahead
        of every other reply write. Never raises; the live view writes nothing
        after it.
        """
        try:
            await self._sink.settle()
        except Exception:
            self._fail("settling")
            self.abandon()

    def abandon(self) -> None:
        """Write nothing more for this turn; for a ``finally``. Never raises."""
        try:
            self._sink.abandon()
        except Exception:
            self._fail("abandoning")

    # The driver reaches this renderer only through ``dispatch``; the handlers
    # exist to satisfy the contract.
    async def on_text_chunk(self, text: str) -> None:
        return None

    async def on_thinking(self, text: str) -> None:
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        return None

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        return None

    async def on_compaction(self, context_usage_pct: float) -> None:
        return None

    async def on_done(self, stop_reason: str = "") -> None:
        return None


class TeeRenderer(Renderer):
    """Feed one turn's events to the channel's renderer, then to a mirror.

    The channel renderer comes first and is awaited exactly as if the driver held
    it directly, so its output and its failures are what they were. The mirror
    (a :class:`DashboardLiveRenderer`) fences its own failures. Only ``dispatch``
    and ``on_turn_start`` are driver-facing; the per-kind handlers delegate to
    the channel renderer so a caller that reaches past ``dispatch`` still gets
    the channel's behaviour.
    """

    def __init__(self, primary: Renderer, mirror: DashboardLiveRenderer) -> None:
        super().__init__(primary.capabilities)
        self.channel_type = primary.channel_type
        self.primary = primary
        self.mirror = mirror

    async def on_turn_start(self) -> None:
        await self.primary.on_turn_start()

    async def dispatch(self, event: OutputEvent) -> None:
        if event.kind == DONE:
            # The channel renderer may write the reply from ``on_done`` (Slack
            # stamps its footer there), so the live view's step writes land, or
            # are dropped, first: a step never follows the reply in the transcript.
            await self.mirror.settle()
        await self.primary.dispatch(event)
        await self.mirror.dispatch(event)

    async def on_text_chunk(self, text: str) -> None:
        await self.primary.on_text_chunk(text)

    async def on_thinking(self, text: str) -> None:
        await self.primary.on_thinking(text)

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        await self.primary.on_tool_call(tool_call_id, title, tool_kind, tool_purpose)

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        await self.primary.on_prompt_choice(
            options, request_id, tool_title, tool_purpose, tool_input
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        await self.primary.on_compaction(context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        await self.primary.on_done(stop_reason)

    async def on_steer_consumed(self, summary: str = "") -> None:
        await self.primary.on_steer_consumed(summary)
