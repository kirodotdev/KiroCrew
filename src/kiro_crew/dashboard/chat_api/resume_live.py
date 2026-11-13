"""Answering a resume that an open slot already satisfies."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _RESUME_APP_NOT_FOUND,
        DashboardState,
        ResumeOutcome,
        ResumeRefusal,
        _collapse_wire_rows,
        _history_key_for,
        _live_child_instance,
        _prepare_messages,
        _reconcile_slot_window,
        channel_slot_name,
        deny_app_slot_session_access,
        effective_session_key,
        is_channel_session_key,
        load_window_off_loop,
        members_mod,
        queue_view,
        sel,
        slot_history_key,
        transcripts_share_file,
    )


def _resume_session_identity(state: DashboardState, history_key: str) -> str:
    """The session a transcript runs under, spelled as a slot spells its own.

    Counterpart to :func:`effective_session_key`, for the caller that holds a
    history key and no slot. A channel-born transcript's session is the
    channel's own, read from the session map because ``history._safe_key``
    folds every ``:`` to ``_`` irreversibly — ``discord_a_b_c`` cannot be
    unfolded by guessing, and a guess would name a session the channel never
    reads. An unmapped channel key falls back to the dashboard spelling, the
    same "leave it unbound" outcome the restore path takes.
    """
    if is_channel_session_key(history_key) and state.sessions:
        real_key = state.sessions.channel_key_for_stem(channel_slot_name(history_key))
        if isinstance(real_key, str) and is_channel_session_key(real_key):
            return real_key
    return _history_key_for(history_key)


async def _live_slot_for_resume(
    state, request_app: str, history_key: str, name: str, caller_label: str = ""
) -> "ResumeOutcome | None":
    """Answer a resume that a live slot already satisfies, else return None.

    Returns the app-isolation 404 refusal when the caller's app does not own
    the slot, the ``member_pin_mismatch`` 409 when a member thread's stored pin is
    not dispatchable (``caller_label`` is what SEL records), otherwise the dedup
    outcome carrying the EXISTING slot. Called on
    BOTH sides of the threaded transcript read: that await lets a concurrent
    resume publish the slot in between, and ``get_or_create_slot`` would then
    hand it back having never applied this ownership gate for the second
    caller's app.
    """
    canonical = _resume_session_identity(state, history_key)
    existing = state._slots.get(name)
    if not existing:
        for slot in state._slots.values():
            if effective_session_key(slot) == canonical or transcripts_share_file(
                slot_history_key(slot), history_key
            ):
                existing = slot
                break
    if existing:
        if (
            deny_app_slot_session_access(request_app, existing, existing.key, "slot_resume")
            is not None
        ):
            return ResumeOutcome(refusal=_RESUME_APP_NOT_FOUND)
        if (
            existing.mode == members_mod.DM_SLOT_MODE
            and not members_mod.is_dispatchable_member_name(existing.agent)
        ):
            sel().log_api_access(
                caller=caller_label,
                operation="chat_resume",
                outcome="denied",
                source="member_pin",
                resources=f"slot={existing.key}",
                error="stored member pin is not dispatchable",
            )
            return ResumeOutcome(
                refusal=ResumeRefusal(
                    "this thread's crew name cannot be dispatched", "member_pin_mismatch", 409
                )
            )
        return ResumeOutcome(slot=existing, already_live=True)
    return None


async def _live_slot_resume_payload(state, existing) -> dict:
    """The resume endpoint's dedup body: the already-open slot's live window."""
    # The live slot a resume resolves to can be a restored sidebar row, under a
    # name the per-slot route's own lookup did not match (``dashboard:<key>``):
    # its window loads here, with the read off the loop, before anything reads it.
    await load_window_off_loop(existing)
    await _reconcile_slot_window(state, existing)
    window = _collapse_wire_rows(existing.messages)
    total = len(window)
    recent = window[-200:] if total > 200 else window
    next_before = (getattr(existing, "_disk_older_count", 0) or 0) + (total - len(recent))
    # The page slot detail serves: none of the rows the tab's display floor hides.
    from kiro_crew.dashboard.display_floor import clip_page_off_loop

    recent, next_before, floor_reached = await clip_page_off_loop(
        state.conversation_log, slot_history_key(existing), recent, next_before
    )
    prepared = _prepare_messages(
        recent,
        existing.running,
        live_child=_live_child_instance(state, existing),
        workspace=existing.workspace,
    )
    return {
        "ok": True,
        "key": existing.key,
        "history_key": slot_history_key(existing),
        "messages": prepared,
        "queue": queue_view(existing._queue),
        "total": total,
        "has_more": next_before > 0 and not floor_reached,
        "next_before": next_before,
        "memory_mode": existing.memory_mode,
        "mode": existing.mode,
        "surface": existing.mode,
        "folder_id": existing.folder_id,
    }
