"""Wire the host memory guard to the dashboard (rfc-host-memory-guard.md §4.4).

Two seams: the resource-pressure notifier's guard trigger schedules a guard pass on
the session manager, and each tree the guard handles posts one notice in the chat
that started it and, after a stop by a bound worker, one ``question`` report on that
worker's item, which wakes its conductor through the ordinary report wake. A chat
with no dashboard tab that lives on a channel gets the notice in its channel
conversation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from kiro_crew import session_memory_guard
from kiro_crew.dashboard.chat_compaction_notice import deliver_channel_text
from kiro_crew.messaging.link import is_channel_session_key

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")


def wire_memory_guard(self: DashboardState) -> None:
    """Connect the notifier's guard trigger and the guard's notice seam."""
    sessions = self.sessions
    notifier = getattr(self, "resource_pressure_notifier", None)
    if notifier is not None:
        notifier.memory_guard = lambda status, psi, trigger: session_memory_guard.request(
            sessions, status, psi, trigger
        )

    tasks: set[asyncio.Task[Any]] = set()

    def _spawn(coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _notify_channel(key: str, message: str) -> None:
        try:
            await deliver_channel_text(self, key, message, tool_name="chat.memory_guard_notice")
        except Exception:
            logger.exception("Failed to deliver memory-guard notice to channel chat %s", key)

    def _on_memory_guard_stop(key: str, message: str, summary: str | None) -> None:
        from kiro_crew import session_ledger
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        slot_key = dashboard_slot_key(key)
        slot = self.get_slot(slot_key) if slot_key else None
        if slot is not None:
            try:
                # Same delivery as the leftover-memory notice: a turn may be
                # streaming, so finalize its text so far as a segment first.
                cut = getattr(slot, "_steer_segment_cut", None)
                if callable(cut):
                    cut(interrupted=False)
                    self.broadcast_ws("chat_segment", {"slot": slot.key})
                slot.append(
                    "assistant",
                    message,
                    "msg msg-a",
                    meta={"kind": "compaction", "notice": "memory_guard"},
                )
            except Exception:
                logger.exception("Failed to append memory-guard notice to slot %s", slot_key)
        elif is_channel_session_key(key):
            _spawn(_notify_channel(key, message))
        if summary is None:
            return
        from kiro_crew.dashboard.memory_guard_report import record_guard_report

        _spawn(record_guard_report(self, session_ledger.ledger_key(slot_key or key), summary))

    sessions.on_memory_guard_stop = _on_memory_guard_stop
