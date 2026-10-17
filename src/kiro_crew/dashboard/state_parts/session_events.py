"""What the dashboard does when a session or a channel link changes.

The notices the session compact, recycle, unbind and stuck-turn paths post, and
the crew-log class recording a slot publishes.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.messaging.link import (
    UNBIND_REASON_DASHBOARD_UNLINK,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_ORIGIN_REBIND,
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
)

logger = logging.getLogger("kiro_crew.dashboard.state")


def note_crew_log_class(state: Any, slot: Any) -> None:
    """Record *slot*'s current class in its crew log, if it has moved.

    THE recorder, as the surfaces outside this module call it. Every path that commits a
    change to a session's class reaches it, so there is one place the fact is written and
    one place to read to know when it is written.
    ``test_crew_log_class_recorder.py`` derives the call-site list from the source and
    fails if a new one appears outside it.
    """
    _record_crew_log_class(state, slot)


def _record_crew_log_class(state: Any, slot: Any) -> None:
    """The implementation. Never raises, and the reason is not caution.

    A record is not worth turning an injection or a binding into a failure, and the
    paths that reach here are reached in tests by state DOUBLES that model a slot store
    and nothing else -- so a missing attribute is an ordinary input rather than a bug.
    What makes swallowing safe is the far end: the append is handed to the crew log's
    writer without waiting, a write the writer permanently loses is itself recorded, and
    the class fold reads a dropped write as a hole. A lost record therefore costs a
    cross-session read a refusal, never a silent grant.

    A slot with NO OPEN LOG is the one case that argument does not cover, because nothing
    is handed to the writer at all and so nothing records the loss. It is reachable: an
    idle session can be bound to a channel, route a turn, and be unbound again before its
    first turn opens a log, and a class read from the live slot at that point states
    never-published about a log that holds channel-authored words. So a restriction is
    MARKED on the slot instead of dropped, and the shared derivation folds the mark in
    when the log is finally opened.

    The mark is written BEFORE the append is attempted and is NOT conditioned on it,
    because a session id is not evidence that a log exists: a restored session publishes
    its id while its log is still absent, and the append then finds no log and returns
    having written nothing and recorded no loss. Keying the mark on the restriction itself
    rather than on a prediction about the append covers that ordering and every other
    reason an append can fail to land, including ones not enumerated here. The cost is a
    mark that outlives an append that DID land, which only re-states a restriction the log
    already carries: the fold holds each member at the most restrictive value the log ever
    recorded, so a redundant mark changes no answer.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    try:
        from kiro_crew.dashboard.chat_runner import (
            PENDING_CHANNEL_ATTR,
            _crew_log_class,
            _crew_log_workspace,
        )

        memory, app, channel = _crew_log_class(state, slot)
        if channel:
            setattr(slot, PENDING_CHANNEL_ATTR, True)
        sid = crew_log_emit.session_id_of(getattr(slot, "_acp_client", None))
        if not sid:
            return
        crew_log_emit.on_class_observed(
            sid,
            memory=memory,
            app=app,
            channel=channel,
            workspace=_crew_log_workspace(slot),
        )
    except Exception:
        logger.debug(
            "crew-log class record skipped for %r", getattr(slot, "key", ""), exc_info=True
        )


_AUTO_COMPACT_NOTICE = "🔄 Auto-compacted at {pct:.0f}%."


#: The notice for the arm that REPLACES the session instead of summarizing it. A
#: separate template because ``_AUTO_COMPACT_NOTICE`` would announce a summary that
#: never happened, and the user's next question -- why does the agent not remember
#: this -- is answerable only if the notice said what actually occurred.
#: Both restart notices end in ``_RESTART_MEMORY_TAIL`` because that sentence is
#: what the successor actually does: its first turn is built from a recent excerpt
#: of this transcript (``ContextBuilder`` thread history). Naming the excerpt is
#: the point: a user who believes the context is gone for good has no reason to
#: ask the agent to pick the work back up.
_RESTART_MEMORY_TAIL = (
    "The conversation above is still here, and the agent's next reply starts from a "
    "recent excerpt of it rather than the whole thing."
)


_AUTO_RECYCLE_NOTICE = (
    "♻️ Compaction didn't succeed at {pct:.0f}%, so the session was restarted "
    "instead. " + _RESTART_MEMORY_TAIL
)


#: The same restart, for a backend that never had a compaction to attempt. Only this
#: one may name the missing capability: the notice above is reached by kiro-cli and
#: opencode sessions whose compaction merely failed, and telling those users their
#: backend cannot compact would be false.
_AUTO_RESTART_UNCOMPACTABLE_NOTICE = (
    "♻️ Context reached {pct:.0f}% and this backend cannot compact at all, so "
    "the session was restarted. " + _RESTART_MEMORY_TAIL
)


#: A user Stop ended the compaction turn. The only Stop that reaches a compacting
#: session is the FORCED one, and a forced stop restarts the session, so the
#: notice says so with the restart notices' own verb and memory tail; what tells
#: it apart from them is the actor it leads with: nothing failed, the user ended
#: it.
_AUTO_COMPACT_CANCELLED_NOTICE = (
    "⏹ Your forced Stop ended the compaction (condensing the conversation to free space) "
    "that started at {pct:.0f}% of the context limit, so the session was restarted. "
    + _RESTART_MEMORY_TAIL
)


_AUTO_COMPACT_WAITING_NOTICE = (
    "⏸ Context reached {pct:.0f}%. The session is waiting for its sub-agents "
    "to finish before it restarts. Your messages are kept."
)


_AUTO_COMPACT_FAILED_NOTICE = (
    "⚠ Auto-compact failed at {pct:.0f}% — will retry after cooldown. "
    "You can run `/compact` manually."
)


_SESSION_RECYCLED_NOTICE = (
    "♻️ This session was recycled by the watchdog ({reason}). "
    "Conversation history is preserved — your next message starts a fresh process."
)


#: Sent to a conversation that just lost its inbound resume binding, so the next
#: message landing in a brand-new session is explained rather than mysterious.
#: ``!sessions`` is Discord's command and Discord is the only transport that binds
#: inbound, so the instruction is reachable wherever this notice can arrive.
_INBOUND_UNBIND_NOTICE = (
    '🔗 This conversation was detached from session "{title}" — {why}. '
    "Run `!sessions` to reattach."
)


#: Human phrasing per audited reason, so the notice never shows an audit token.
#: The two vocabularies stay separate on purpose: a reason can be renamed or split
#: without rewriting user copy, and this copy can be reworded without touching the
#: trail. An unmapped reason falls back to the generic phrase rather than leaking
#: through as a raw token.
_INBOUND_UNBIND_WHY: dict[str, str] = {
    UNBIND_REASON_DASHBOARD_UNLINK: "someone unlinked it from the dashboard",
    UNBIND_REASON_ORIGIN_REBIND: "this conversation was relinked to a new session",
    UNBIND_REASON_SESSION_DESTROYED: "that session was deleted",
    UNBIND_REASON_ENTRY_DELETED: "that session's record was removed",
    UNBIND_REASON_PRUNED_STALE: "that session's record was no longer on disk",
    UNBIND_REASON_UNSPECIFIED: "the link was cleared",
}


_INBOUND_UNBIND_WHY_DEFAULT = "the link was cleared"


#: Shown when the out-of-band watchdog finds a turn whose consumer stopped
#: pulling events. Deliberately describes the observation rather than promising a
#: remedy: nothing is cancelled or retried, because what the turn is blocked on
#: is not knowable from the loop that noticed. Stating a duration is the point —
#: it is what distinguishes this from a turn that is merely slow.
_STUCK_TURN_NOTICE = (
    "⏳ This turn has produced nothing for {minutes} min and is not waiting on an "
    "approval — it may be stuck. Nothing has been cancelled. Press Stop to end it "
    "and try again."
)


def stuck_turn_notice(parked_secs: float) -> str:
    """Render the stuck-turn notice for a park of ``parked_secs``.

    Module-level and pure so the rounding is testable without standing up a whole
    ``DashboardState``. Floors at 1 minute: the hook's threshold is minutes-scale,
    so "0 min" would only ever read as a bug to the person seeing it.
    """
    return _STUCK_TURN_NOTICE.format(minutes=max(1, int(parked_secs // 60)))
