"""The context a dashboard turn's input carries: the execution context's memory-mode
fold, the pending context drain, the folder-steering gate and the appended-context split."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _CONTEXT_FRAME_CONTRACT,
        _ChatSlot,
        _MemoryUnavailable,
        canonical_memory_mode,
        context_entry_expired,
        logger,
        read_session_execution,
        stricter_memory_mode,
        tighten_live_session_execution,
    )


def _folder_steering_turn(
    slot: Any,
    execution_context: Any,
    *,
    context_is_new: bool,
    provider_has_history: bool,
    needs_reinjection: bool,
) -> bool:
    """Whether this turn must resolve the folder's steering directories.

    A template chat reads the folder tree only on the two turns that carry
    session-start context: a fresh provider session (not a resumed one, which
    already holds its original injection) and a reinjection after compaction.
    Warm template turns never touch it.

    A V2 MEMBER chat is different. ``build_message`` rebuilds the member's
    essentials envelope on EVERY turn, and that envelope declares itself the
    complete replacement for all prior snapshots ("do not keep applying removed
    sources"). Folder steering rides inside that envelope, so a warm member turn
    that passed no directories would hand the model a snapshot that silently
    withdraws the folder's guides. Every turn that rebuilds the envelope resolves.
    """
    if (context_is_new and not provider_has_history) or needs_reinjection:
        return True
    return bool(
        getattr(slot, "mode", "") == "member"
        and getattr(slot, "agent", "")
        and execution_context is not None
        and getattr(execution_context, "member_id", None)
    )


def _read_and_tighten_turn_execution(
    conversation_log: Any, session_key: str, transcript_key: str | None = None
):
    """Fold the transcript privacy line into the live turn carrier off-loop.

    ``read_session_execution`` deliberately serves a live carrier without file I/O
    because synchronous callers also use it on the event loop. Turn admission has
    already moved to a worker thread, so this is the one read-back that can safely
    compare the live carrier with the line and republish only a stricter mode.
    """
    execution = read_session_execution(session_key)
    if execution is None:
        return None
    if conversation_log is None:
        # No transcript store at all (history disabled, or a state built without
        # one): there is no line on disk to fold, so the carrier stands as read.
        return execution
    # The carrier is addressed by the SESSION key; the privacy line lives on the
    # TRANSCRIPT, which for an unbound channel-born slot is a different file
    # (``slot_history_key`` vs ``effective_session_key``). Read the line by the
    # transcript key, or the fold finds no line there and tightens nothing.
    metadata, readable = conversation_log.get_metadata_status(transcript_key or session_key)
    if not readable:
        # An unreadable line is a transient read failure (fd exhaustion, a
        # sharing violation while another writer's replace lands), not a mode
        # -- and it leaves this turn with NO contract to run under. Neither
        # extreme is right: tightening to Temporary would turn one failed read
        # into a permanent ratchet on a persistent chat (everything this helper
        # publishes only ever narrows), while proceeding on the carrier as read
        # would let a line another writer already tightened -- Temporary over
        # this process's persistent carrier -- go unseen for a whole turn, with
        # memory injection and memory writes still enabled under the looser
        # mode. So the turn is refused, retryably: the same answer the save
        # gives when it meets an unreadable line ("deferred for retry"), and the
        # same card every other memory-unavailable turn shows. Nothing ran,
        # nothing was written; the next turn re-reads the line.
        raise _MemoryUnavailable(
            "memory_unavailable: this conversation's privacy line could not be read "
            "just now; nothing was sent -- try again in a moment"
        )
    if "memory_mode" not in metadata:
        return execution
    retained_mode = stricter_memory_mode(
        canonical_memory_mode(metadata.get("memory_mode")), execution.memory_mode
    )
    if retained_mode == execution.memory_mode:
        return execution
    tightened_live = tighten_live_session_execution(session_key, retained_mode, expected=execution)
    return tightened_live or execution.with_mode(retained_mode)


def drain_pending_context(slot: "_ChatSlot") -> str:
    """Drain ``slot._pending_context`` into a prepend-ready context prefix.

    Returns the concatenated ``[Background context from "<source>"] … [End of
    background context]`` blocks (empty string when there is nothing to inject)
    and clears the queue. Expired entries (``maxAge`` elapsed) are discarded.

    Each frame carries an explicit silent-consumption contract line
    (``_CONTEXT_FRAME_CONTRACT``) between the opening delimiter and the
    content. The endpoint's promise is *silent* background context, and the
    frame has to say so: without the contract, on a fresh session whose visible
    message is one short line, the agent recites the injected feature-request
    workflow verbatim as its reply — surfacing internal instructions in the
    transcript on every click of the header button. The contract is part of the
    frame, not any producer's payload, so every producer (app-kit context
    inject, artifact companion, Slack thread backfill, feature-request seed)
    is covered without each having to remember to say "don't echo this".

    Extracted from ``_run_chat`` so the entry contract — the ``content`` /
    ``source`` keys and the delimiter frame — is pinned by a unit test and
    shared by every producer (app-kit context inject, Slack thread backfill),
    rather than duplicated inline where a key rename could silently break a
    consumer while its producer's own tests stay green.
    """
    # A note's halves resolve their destination here, not at the POST, so a slot
    # rebound since the write must not hand its content to the new session.
    slot.drop_foreign_authorized_notes()
    if not slot._pending_context:
        return ""
    now = time.time()
    ctx_parts: list[str] = []
    for entry in slot._pending_context:
        if context_entry_expired(entry, now):
            continue  # expired — silently discard
        # `or "app"` (not a dict default): api_chat_slot_context always writes
        # the key — as "" when the caller omitted it — so a plain .get() default
        # never fires and the header would render [Background context from ""],
        # an unattributed block under a "not authored by the user" claim.
        source = entry.get("source") or "app"
        ctx_parts.append(
            f'[Background context from "{source}"]\n'
            f"{_CONTEXT_FRAME_CONTRACT}\n"
            f'{entry["content"]}\n'
            f"[End of background context]\n"
        )
    slot._pending_context.clear()
    return "\n".join(ctx_parts) + "\n" if ctx_parts else ""


def _detach_appended_context(original: str, expanded: str) -> tuple[str, str]:
    """Return ``(original request, generated context)`` for append-only transforms.

    ``$skill`` and theme-persona producers return one concatenated
    string. The provider prompt now needs generated bytes BEFORE the request, so
    split them at their shared append-only seam. If a future producer stops
    preserving the original prefix, fail safe: keep the original as the request
    tail and move the whole transformed value into generated context.
    """
    if expanded == original:
        return original, ""
    if expanded.startswith(original):
        return original, expanded[len(original) :]
    logger.warning("generated request context stopped honoring append-only contract")
    return original, expanded
