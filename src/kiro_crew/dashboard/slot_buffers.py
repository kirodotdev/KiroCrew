"""Live delivery and deferred-context buffers for dashboard chat slots."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

from kiro_crew.sel import sel


def _apply_message_patch(slot: Any, message: dict, content: str | None, meta: dict | None) -> dict:
    """Write a resolved row's new content/meta and mark the slot for persistence."""
    if content is not None:
        message["content"] = content
        slot.invalidate_source_links()
    if meta is not None:
        message["meta"] = meta
    slot._dirty = True
    return message


class SlotBufferCoordinator:
    """Operate on the current facade-owned slot containers without aliasing them."""

    @staticmethod
    def push_wire_frame(slot: Any, cls: str, content: str) -> None:
        slot._pending.append({"role": cls, "content": content, "cls": cls, "ts": ""})
        slot.event.set()

    @staticmethod
    def drain(slot: Any) -> list[dict[str, str]]:
        pending = slot._pending[:]
        slot._pending.clear()
        slot.event.clear()
        return pending

    @staticmethod
    def pending_has_consumer(slot: Any) -> bool:
        return slot._pending_consumers > 0 or slot._has_reader

    @staticmethod
    def retry_deferred_release(slot: Any) -> int:
        if not slot._pending_release_deferred:
            return 0
        return slot.release_pending_chunks()

    @staticmethod
    @contextlib.contextmanager
    def pending_consumer(slot: Any) -> Iterator[None]:
        slot._pending_consumers += 1
        try:
            yield
        finally:
            slot._pending_consumers = max(0, slot._pending_consumers - 1)
            slot._retry_deferred_release()

    @staticmethod
    def release_pending_chunks(slot: Any) -> int:
        # A live SSE/OpenAI reader owns these rows until it detaches.  Remember a
        # refused release so the final detaching consumer can reclaim them.
        if slot.pending_has_consumer:
            slot._pending_release_deferred = True
            return 0
        slot._pending_release_deferred = False
        before = len(slot._pending)
        if not before:
            return 0
        slot._pending = [message for message in slot._pending if message.get("role") != "chunk"]
        return before - len(slot._pending)

    @staticmethod
    def purge_chunks(slot: Any) -> int:
        slot.messages = [message for message in slot.messages if message.get("role") != "chunk"]
        return slot.release_pending_chunks()

    @staticmethod
    def append_pending_context(
        slot: Any,
        entry: dict[str, Any],
        *,
        max_pending_context: int,
        entry_expired: Callable[[dict[str, Any], float], bool],
    ) -> None:
        now = time.time()
        if entry_expired(entry, now):
            return
        slot._pending_context[:] = [
            current for current in slot._pending_context if not entry_expired(current, now)
        ]
        while len(slot._pending_context) >= max_pending_context:
            slot._pending_context.pop(0)
        slot._pending_context.append(entry)

    @staticmethod
    def drop_foreign_authorized_notes(
        slot: Any,
        *,
        authorized_elsewhere: Callable[[object, str], bool],
        logger: logging.Logger,
    ) -> int:
        # Local import avoids a module cycle: chat_utils imports the state facade.
        from kiro_crew.dashboard.chat_utils import effective_session_key

        live_session = effective_session_key(slot)
        kept_context = [
            entry
            for entry in slot._pending_context
            if not authorized_elsewhere(entry, live_session)
        ]
        dropped = len(slot._pending_context) - len(kept_context)
        if dropped:
            slot._pending_context[:] = kept_context

        kept_messages = [
            message
            for message in slot.messages
            if not authorized_elsewhere(message.get("meta"), live_session)
        ]
        if len(kept_messages) != len(slot.messages):
            dropped += len(slot.messages) - len(kept_messages)
            slot.messages[:] = kept_messages
        if dropped:
            sel().log_api_access(
                caller="dashboard",
                operation="note_rebind_drop",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key} dropped={dropped}",
                error="slot was rebound to another session after the note was written",
            )
            logger.warning(
                "Slot %s dropped %d note item(s): authorized elsewhere, slot now routes to %s",
                slot.key,
                dropped,
                live_session,
            )
        return dropped

    @staticmethod
    def deferred_context_count(slot: Any) -> int:
        return sum(1 for note in slot._deferred_notes if note.get("context") is not None)

    @staticmethod
    def flush_deferred_notes(slot: Any, *, logger: logging.Logger) -> int:
        """Flush held notes in order, restoring the unwritten suffix on failure."""
        if not slot._deferred_notes:
            return 0
        from kiro_crew.dashboard.chat_utils import effective_session_key

        held = slot._deferred_notes[:]
        slot._deferred_notes.clear()
        live_session = effective_session_key(slot)
        written = 0
        for index, note in enumerate(held):
            authorized_session = note.get("session")
            if authorized_session is not None and authorized_session != live_session:
                sel().log_api_access(
                    caller="dashboard",
                    operation="note_flush",
                    outcome="denied",
                    source="app_isolation",
                    resources=f"slot={slot.key}",
                    error="slot was rebound to another session while the note was held",
                )
                logger.warning(
                    "Slot %s dropped a held note: authorized for %s, slot now routes to %s",
                    slot.key,
                    authorized_session,
                    live_session,
                )
                continue

            # Pop is a retry marker: if the visible row fails after the context
            # was queued, the restored note must not enqueue that context twice.
            context = note.pop("context", None)
            try:
                if context is not None:
                    context["noteSession"] = live_session
                    slot.append_pending_context(context)
                slot.append(
                    role="inject",
                    content=note["content"],
                    cls=note["cls"],
                    broadcast=True,
                    meta={"noteSession": live_session},
                )
            except Exception:
                # New arrivals stay after this older, unwritten suffix.
                slot._deferred_notes[:0] = held[index:]
                raise
            written += 1
            # BOTH halves are committed now, which is the whole reason the mirror waits
            # for the flush: at POST it would assert content the session never received.
            mirror = note.get("mirror")
            if mirror is not None:
                try:
                    mirror()
                except Exception:
                    # Never load-bearing: the note is written either way, and the
                    # suffix-restore above must not fire for a delivery failure.
                    logger.warning(
                        "held note mirror dispatch failed for slot %s", slot.key, exc_info=True
                    )
        return written

    @staticmethod
    def mark_permission_resolved(slot: Any, approval_id: str, decision: str) -> None:
        for message in slot.messages:
            if message.get("role") != "permission":
                continue
            try:
                cls_data = json.loads(message.get("cls", ""))
                if isinstance(cls_data, dict) and cls_data.get("request_id") == approval_id:
                    cls_data["resolved"] = decision
                    message["cls"] = json.dumps(cls_data)
                    return
            except (json.JSONDecodeError, TypeError):
                pass

    @staticmethod
    def update_message(
        slot: Any,
        ts: str,
        *,
        content: str | None,
        meta: dict | None,
        mid: str | None = None,
    ) -> dict | None:
        # `mid` is the row's server-minted identity, stamped once per row by
        # _ChatSlot.append. Prefer it: `ts` is NOT an identity -- an explicitly
        # supplied one is preserved verbatim for a row replayed from a channel
        # transcript, and a coarse OS clock stamps two same-tick rows identically
        # -- so a ts lookup resolves the FIRST match and can patch the wrong row.
        # `ts` remains the fallback for a legacy row written before the id existed,
        # where it is the only handle available.
        if mid:
            for message in slot.messages:
                if (message.get("meta") or {}).get("mid") == mid:
                    return _apply_message_patch(slot, message, content, meta)
            return None
        if not ts:
            return None
        for message in slot.messages:
            if message.get("ts") != ts:
                continue
            return _apply_message_patch(slot, message, content, meta)
        return None
