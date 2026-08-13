"""Scale plumbing for sub-agent dashboard events (60-100 agents).

At small spawn counts every sub-agent event is forwarded to the dashboard as
an individual WS frame — fine for 3 agents, a socket-saturating flood for
60-100 (each agent streams chunk/tool/stalled deltas concurrently, and every
frame triggers a client dispatch + re-render).

``SubagentEventCoalescer`` sits between the gateway's ``_subagent_event``
callback and ``DashboardState``:

- **Below the activation threshold** (``active_count <= threshold``, default
  8) every event passes through unchanged — small spawns behave byte-identical
  to today.
- **Above the threshold**, high-frequency delta events (``subagent_tool``,
  ``subagent_stalled``, ``subagent_retrying``) are buffered per agent (latest
  state wins) and flushed on a ~1s tick as ONE ``subagent_batch_update`` frame
  ``{updates: [{id, slot, ...merged extras}, ...]}`` broadcast to all clients.
  ``subagent_chunk`` text is buffered per agent (append-concatenated, bounded)
  and flushed on the same tick as ONE ``subagent_batch_chunks`` frame
  ``{chunks: [{id, slot, text}, ...]}`` to subagent subscribers only —
  preserving the existing heavy-data routing split.
- **Lifecycle events are NEVER coalesced**: ``subagent_spawn``,
  ``subagent_done``, ``subagent_recovering``, ``subagent_injection_failed``,
  ``spawn_batch_started``, and ``batch_finished`` always pass through
  immediately (they flip truth values the UI and slots-stream key on).

On the transition back below the threshold any buffered state is flushed so
nothing is stranded. ``close()`` flushes and cancels the tick task.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Delta events eligible for coalescing (latest-state-wins per agent).
_COALESCABLE = frozenset({"subagent_tool", "subagent_stalled", "subagent_retrying"})
# Per-agent buffered chunk text cap: the UI truncates streaming text anyway;
# bounding here keeps one slow-consumer tick from accumulating unbounded text.
_CHUNK_BUF_CAP = 16_000

BroadcastFn = Callable[[str, dict], None]
ActiveCountFn = Callable[[], int]


class SubagentEventCoalescer:
    """Buffer + tick-flush high-frequency subagent events at scale."""

    def __init__(
        self,
        broadcast_all: BroadcastFn,
        broadcast_subscribers: BroadcastFn,
        active_count: ActiveCountFn,
        *,
        threshold: int = 8,
        tick_secs: float = 1.0,
    ) -> None:
        self._broadcast_all = broadcast_all
        self._broadcast_subs = broadcast_subscribers
        self._active_count = active_count
        self._threshold = max(1, threshold)
        self._tick_secs = tick_secs
        # id -> {"slot": str, **latest extras}  (latest state wins)
        self._updates: dict[str, dict] = {}
        # id -> {"slot": str, "text": str}  (append-concatenated)
        self._chunks: dict[str, dict] = {}
        self._tick_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._closed = False

    # ── ingestion ────────────────────────────────────────────────────

    def _active(self) -> int:
        """Active-agent count; non-int (test doubles / errors) fails open to 0
        so behavior degrades to pass-through, never to absorption."""
        try:
            n = self._active_count()
            return n if isinstance(n, int) else 0
        except Exception:
            return 0

    def handle(self, etype: str, payload: dict) -> bool:
        """Route one event. Returns True when the event was ABSORBED into a
        buffer (caller must not forward it), False when the caller should
        forward it immediately (pass-through)."""
        if self._closed:
            return False
        if etype == "subagent_chunk":
            if self._active() <= self._threshold:
                self._flush_if_pending()
                return False
            buf = self._chunks.setdefault(
                payload.get("id", ""), {"slot": payload.get("slot", ""), "text": ""}
            )
            text = buf["text"] + str(payload.get("text", ""))
            if len(text) > _CHUNK_BUF_CAP:
                text = "…(truncated)\n" + text[-(_CHUNK_BUF_CAP - 100):]
            buf["text"] = text
            self._ensure_tick()
            return True
        if etype in _COALESCABLE:
            if self._active() <= self._threshold:
                self._flush_if_pending()
                return False
            entry = self._updates.setdefault(
                payload.get("id", ""), {"slot": payload.get("slot", "")}
            )
            # Latest state wins; merge so a tool update and a stalled update
            # for the same agent ride one entry. Incompatible fields are
            # cleared to mirror the per-event reducer semantics: tool activity
            # means the agent is WORKING again, so a stale buffered `attempt`
            # (retrying) must not survive the merge and leave the row marked
            # retrying after work resumed.
            if etype == "subagent_tool":
                entry.pop("attempt", None)
            entry.update({k: v for k, v in payload.items() if k != "id"})
            self._ensure_tick()
            return True
        # Lifecycle / unknown event: pass through. A done/spawn between ticks
        # must not overtake this agent's buffered deltas — flush first so
        # ordering is preserved (e.g. buffered chunk text lands before done).
        if etype in ("subagent_done", "subagent_spawn"):
            self._flush_if_pending()
        return False

    # ── flushing ─────────────────────────────────────────────────────

    def _ensure_tick(self) -> None:
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.get_event_loop().create_task(self._tick())

    async def _tick(self) -> None:
        try:
            await asyncio.sleep(self._tick_secs)
        except asyncio.CancelledError:
            return
        self._flush_if_pending()

    def _flush_if_pending(self) -> None:
        if self._updates:
            updates = [{"id": aid, **extra} for aid, extra in self._updates.items()]
            self._updates.clear()
            try:
                self._broadcast_all("subagent_batch_update", {"updates": updates})
            except Exception:
                logger.debug("batch_update broadcast failed", exc_info=True)
        if self._chunks:
            chunks = [
                {"id": aid, "slot": buf["slot"], "text": buf["text"]}
                for aid, buf in self._chunks.items()
                if buf["text"]
            ]
            self._chunks.clear()
            if chunks:
                try:
                    self._broadcast_subs("subagent_batch_chunks", {"chunks": chunks})
                except Exception:
                    logger.debug("batch_chunks broadcast failed", exc_info=True)

    def close(self) -> None:
        """Flush remaining state and stop the tick (gateway shutdown)."""
        self._closed = True
        self._flush_if_pending()
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
