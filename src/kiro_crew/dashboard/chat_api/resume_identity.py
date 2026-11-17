"""Holding a resume's live-slot answer to the transcript identity the caller pinned."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        ResumeOutcome,
        ResumeRefusal,
        slot_history_key,
    )


async def _pinned_live_outcome(
    state: DashboardState, outcome: ResumeOutcome, expected_created_at: str | None
) -> ResumeOutcome:
    """Hold a live-slot answer to the transcript the caller pinned.

    A refusal passes through as it is: an app gets the same isolation 404 with a
    pin as without one, so a pin cannot tell it that a session it does not own is
    open, and a member pin refusal keeps its own reason. Only a live slot whose
    transcript is not the pinned one becomes ``resume_identity_mismatch``.

    The slot's transcript identity is the ``created_at`` of the transcript it
    HOLDS, never ``slot.created_at``: that field is the constructor's clock
    reading, and a save carries the disk line's own ``created_at`` forward
    (``metadata_line.build_full_line``) and records it as
    ``_disk_meta_created_at``. A ``workflow-<run_id>`` slot bound to a closed
    chat's session, or a chat minted by name over an existing transcript, holds a
    transcript whose identity its stamp never matches. The observed value is the
    answer when the slot has one, the same identity ``mergedFrom.createdAt`` is
    recorded from and ``resolve_turn_transcript_identity`` answers with. A slot
    that has neither saved nor hydrated will carry the disk line forward on its
    first save, so its transcript's identity is the one on disk now, read off the
    event loop as resume reads every metadata line. A slot that never writes
    a transcript (a non-persistent memory mode), an unreadable line, a missing
    file and a legacy line with no ``created_at`` all have no identity, which no
    pin can equal.
    """
    if expected_created_at is None or outcome.refusal is not None:
        return outcome
    held_identity = ""
    if outcome.slot is not None:
        held_identity = str(getattr(outcome.slot, "_disk_meta_created_at", "") or "")
        if (
            not held_identity
            and outcome.slot.memory_mode == "persistent"
            and state.conversation_log
        ):
            meta, meta_readable = await asyncio.to_thread(
                state.conversation_log.get_metadata_status, slot_history_key(outcome.slot)
            )
            if meta_readable:
                held_identity = str(meta.get("created_at") or "")
    if held_identity and held_identity == expected_created_at:
        return outcome
    return ResumeOutcome(
        refusal=ResumeRefusal(
            "the requested session is no longer available",
            "resume_identity_mismatch",
            409,
        )
    )
