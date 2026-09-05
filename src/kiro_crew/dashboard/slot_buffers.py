"""Live delivery and deferred-context buffers for dashboard chat slots."""

from __future__ import annotations

import contextlib
import json
import logging
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
            # HELD, NOT DESTROYED. This slot may not inject content stamped for
            # another session, but discarding it deletes a durable copy the API
            # already acknowledged: `pending_context` is slot-owned, so the next save
            # writes this slot's (now shorter) queue and the stored copy goes with it.
            # That is unrecoverable, and it fires on a spelling this hydration merely
            # could not PROVE belongs here -- a folded transcript stem is ambiguous by
            # construction, because `_safe_key` maps every separator onto `_` and
            # leaves a literal `_` alone, so `discord:crew_agent:direct:user_1` cannot
            # be told from a key that really carried underscores there. Holding the
            # entries lets `export_pending_context` write them back unchanged, so the
            # copy survives until a live binding resolves the key and the session they
            # were stamped for can claim them.
            _foreign = [entry for entry in slot._pending_context if entry not in kept_context]
            slot._ctx_held_foreign = [
                *(getattr(slot, "_ctx_held_foreign", None) or []),
                *_foreign,
            ]
            slot._pending_context[:] = kept_context
            # A DESTRUCTIVE MUTATION THE EXPORT'S GENERATION CHECK MUST SEE: a save that
            # snapshotted before this transfer would otherwise commit the pre-move state.
            slot._pending_context_gen = getattr(slot, "_pending_context_gen", 0) + 1

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
