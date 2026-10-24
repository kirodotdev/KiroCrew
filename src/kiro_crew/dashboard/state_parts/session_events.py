"""What the dashboard does when a session or a channel link changes.

The channel-transport registry and its status, the session compact, recycle,
bind and unbind callbacks with the notices they post, and the crew-log class
recording a slot publishes.

Instance fields of ``DashboardState`` this module reads and writes:

Reads only: ``_background_tasks``, ``_slots``, ``channel_transports``, ``sessions``,
``slack_client``, ``slack_socket_connected``.

A function whose first parameter is ``self`` is a ``DashboardState`` method: the
class binds it under the same name (``state_parts/dashboard_state.py``), so it reads
and writes the state's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_compaction_notice import deliver_channel_compaction_notice
from kiro_crew.dashboard.state_parts.transcript_rows import _compaction_keep_record
from kiro_crew.messaging.link import (
    UNBIND_REASON_DASHBOARD_UNLINK,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_ORIGIN_REBIND,
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
    UNBIND_REASON_USER_UNLINK,
    ChannelLink,
    is_channel_session_key,
)
from kiro_crew.messaging.renderer import display_safe
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_COMPACTED,
    COMPACT_OUTCOME_RECYCLED,
    COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE,
    COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState
    from kiro_crew.messaging.transport import MessagingTransport

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


def register_channel_transport(self: DashboardState, transport: "MessagingTransport") -> None:
    """Register a live channel transport for cross-surface mirror delivery.

    Called by each channel's gateway at boot, keyed by ``channel_type`` so
    the dashboard turn path can resolve the transport for a session's
    outbound mirror link and deliver a reply via ``send_message``.
    """
    ct = getattr(transport, "channel_type", "")
    if transport is not None and ct:
        self.channel_transports[ct] = transport
        dispatcher = getattr(transport, "dispatcher", None)
        if dispatcher is not None:
            dispatcher.dashboard_state = self


def get_channel_transport(self: DashboardState, channel_type: str) -> "MessagingTransport | None":
    """Return the registered transport for *channel_type*, or None."""
    return self.channel_transports.get(channel_type)


def channel_status(self: DashboardState) -> dict[str, dict[str, Any]]:
    """Per-channel ``{connected, error}``, keyed by ``channel_type``.

    Read off the same ``<channel>_connected`` / ``<channel>_connect_error``
    attributes each channel's own settings endpoint reports, so one page cannot
    disagree with another about whether a channel came up. A channel with no
    attributes yet reads as not connected with no reason, which is the honest
    answer for one that never started.

    The error string is bounded here as well as at each settings endpoint: this
    payload is polled, and a channel that reconnects in a loop would otherwise
    publish an unbounded reason on every tick.
    """
    # Imported here rather than at module scope: `channels` imports every
    # channel package, and those import this module through the gateway.
    try:
        from kiro_crew.channels import builtin_channel_descriptors

        names = [d.channel_type for d in builtin_channel_descriptors()]
    except Exception:
        logger.debug("channel status: roster unavailable", exc_info=True)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        if name == "slack":
            connected = self.slack_client is not None and self.slack_socket_connected
        else:
            connected = bool(getattr(self, f"{name}_connected", False))
        out[name] = {
            "connected": connected,
            "error": str(getattr(self, f"{name}_connect_error", ""))[:120],
        }
    return out


def wire_session_compact_callback(self: DashboardState) -> None:
    """Register the dashboard's compaction callback on the session manager."""

    async def _on_compacted(
        key: str,
        pct: float,
        *,
        success: bool,
        outcome: str = COMPACT_OUTCOME_COMPACTED,
    ) -> None:
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        slot_key = dashboard_slot_key(key)
        if slot_key:
            # A channel-born session with an open tab is readable on BOTH
            # surfaces, and the user may be looking at either one, so both
            # get the notice: silently summarized history is the confusing
            # outcome this notice exists to prevent.
            if is_channel_session_key(key):
                await self._notify_channel_compaction(key, pct, success=success, outcome=outcome)
        else:
            # No tab to append to, so the notice would be dropped and the
            # user would see summarized history with no explanation. Route
            # it to its own conversation instead.
            await self._notify_channel_compaction(key, pct, success=success, outcome=outcome)
            return
        slot = self.get_slot(slot_key)
        if slot is None:
            return
        if outcome == COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS:
            template = _AUTO_COMPACT_WAITING_NOTICE
        elif outcome == COMPACT_OUTCOME_CANCELLED:
            template = _AUTO_COMPACT_CANCELLED_NOTICE
        elif not success:
            template = _AUTO_COMPACT_FAILED_NOTICE
        elif outcome == COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE:
            template = _AUTO_RESTART_UNCOMPACTABLE_NOTICE
        elif outcome == COMPACT_OUTCOME_RECYCLED:
            template = _AUTO_RECYCLE_NOTICE
        else:
            template = _AUTO_COMPACT_NOTICE
        message = template.format(pct=pct)
        meta: dict[str, Any] = {"kind": "compaction"}
        record = _compaction_keep_record(key)
        if record is not None:
            # The SAME field the two other decision receipts ride
            # (``meta.decisions_strip``), so the reserved-key protection, the
            # history reload and the live websocket door are all the ones already
            # in place. The frontend dispatches on the record's own ``point``, so
            # a reader that predates this one draws nothing rather than guessing.
            meta["decisions_strip"] = record
        try:
            # Tag kind="compaction" so this proactive auto-compact notice
            # (fired at session.autocompact_pct) is skipped by the dashboard's
            # follow-up [OPTIONS:] backward scan — same invariant as
            # chat_utils._append_compaction_notice. meta.kind covers history
            # reload; slot.append carries the meta on the live broadcast too.
            # (Routing through the chat_utils chokepoint would create a
            # state<->chat_utils import cycle; the notice is a hardcoded
            # template with no LLM content, so its redaction pass is moot.)
            slot.append("assistant", message, "msg msg-a", meta=meta)
        except Exception:
            logging.getLogger("kiro_crew.dashboard.state").exception(
                "Failed to append compact notice to slot %s", slot_key
            )
        if success:
            # Reset the context bar — successful compact dropped usage.
            # reset lets the frontend drop its stored token counts too
            # (the "X / Y tokens" tooltip), which do not describe the
            # compacted session.
            try:
                self.broadcast_context_usage(
                    slot_key, {"slot": slot_key, "pct": 0.0, "reset": True}
                )
            except Exception:
                logging.getLogger("kiro_crew.dashboard.state").exception(
                    "Failed to broadcast context_usage for slot %s", slot_key
                )

    self.sessions.set_compact_callback(_on_compacted)

    def _on_compacting_changed(key: str, on: bool) -> None:
        # The slot learns the compaction is RUNNING, not only how it ended:
        # the composer shows it and the Stop button warns on it.
        # Synchronous, from the tick that committed the membership change,
        # so the broadcast that follows agrees with ``is_compacting``.
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        slot_key = dashboard_slot_key(key)
        slot = self.get_slot(slot_key) if slot_key else None
        if slot is None or slot._compacting == on:
            return
        slot._compacting = on
        if not on:
            # The decline marker was this compaction's; the next one, even
            # inside the window, owes its own first refusal.
            slot._stop_declined_at = 0.0
        self.push_slots_update()

    setter = getattr(self.sessions, "set_compacting_callback", None)
    if callable(setter):
        setter(_on_compacting_changed)


async def _notify_channel_compaction(
    self: DashboardState,
    key: str,
    pct: float,
    *,
    success: bool,
    outcome: str = COMPACT_OUTCOME_COMPACTED,
) -> None:
    """Deliver the auto-compact notice to a channel-originated session.

    Isolated from the dashboard leg: a channel that is unreachable, ungoverned
    or unregistered must not turn a successful compaction into an exception on
    the session manager's background task.
    """
    try:
        await deliver_channel_compaction_notice(self, key, pct, success=success, outcome=outcome)
    except Exception:
        logging.getLogger("kiro_crew.dashboard.state").exception(
            "Failed to deliver channel compact notice for %s", key
        )


def wire_session_bind_listener(self: DashboardState) -> None:
    """Register the crew-log class record for a COMMITTED channel binding.

    The session map sees the binding and nothing else about the session; the
    memory mode and the owning app live on the slot. This is where those halves
    meet, the same division as :meth:`wire_session_unbind_listener`.

    Runs SYNCHRONOUSLY, unlike the unbind notice, and that difference is the point
    rather than an oversight. The notice has to reach a transport, so it hops to
    the gateway loop; this has to be recorded BEFORE anything can be routed through
    the binding, and it is the map's own lock -- held across this call -- that
    guarantees it. Hopping to the loop would put the record after traffic could
    arrive. Every step here is cheap and non-blocking: reading slot attributes,
    one probe of the map (whose lock is reentrant, so the same thread re-enters it
    safely), and an append handed to the crew log's writer without waiting.
    """

    def _on_bind(key: str) -> None:
        slot = self._slots.get(key.partition(":")[2] or key)
        if slot is None:
            # No live slot: nothing is authoring into a crew log under this key
            # right now, so there is no class to record. A later turn opens the log
            # and states the class it finds then.
            return
        self.note_crew_log_class(slot)

    self.sessions.set_bind_listener(_on_bind)


def wire_session_unbind_listener(self: DashboardState) -> None:
    """Register the channel notice for a removed inbound resume binding.

    The session map audits every removal itself; what it cannot do is reach
    the conversation, because that means resolving a transport. This is where
    those halves meet. Called from async gateway startup, which is what makes
    the loop capture below correct: the listener itself runs on whatever thread
    performed the clear, so the loop has to be bound here.
    """
    loop = asyncio.get_event_loop()

    def _on_unbind(key: str, link: ChannelLink, reason: str) -> None:
        if reason == UNBIND_REASON_USER_UNLINK:
            # The in-channel unlink command has already replied in this very
            # conversation, so a notice here would be an echo of it.
            return
        if loop.is_closed():
            # The gateway is shutting down; there is nothing left to deliver
            # on. The SEL event already recorded the removal.
            logger.debug("Gateway loop closed; dropping inbound-unbind notice for %s", key)
            return
        try:
            # ``call_soon_threadsafe`` rather than a call-time
            # ``get_running_loop``: SessionMap is synchronous and a clear can
            # arrive on a worker thread, where there is no running loop and the
            # notice would be dropped. The loop captured at wire time is the
            # gateway's own. Stays SYNC and returns at once — the map holds its
            # lock across this call.
            loop.call_soon_threadsafe(self._spawn_unbind_notice, key, link, reason)
        except RuntimeError:
            # Raced a shutdown between the is_closed check and the call.
            logger.debug("Gateway loop gone; dropping inbound-unbind notice for %s", key)

    self.sessions.set_unbind_listener(_on_unbind)


def _spawn_unbind_notice(self: DashboardState, key: str, link: ChannelLink, reason: str) -> None:
    """Start the notice task on the gateway loop, retaining a strong reference.

    Runs ON the loop (``call_soon_threadsafe`` target), so creating the task is
    safe here. Tracked in ``_background_tasks`` for the same reason
    :meth:`_spawn_ws_send` does it: the loop holds only a weak reference, so an
    untracked task can be collected mid-send and the notice silently vanishes.
    """
    task = asyncio.ensure_future(self._notify_inbound_unbind(key, link, reason))
    self._background_tasks.add(task)
    task.add_done_callback(self._on_unbind_notice_done)


def _on_unbind_notice_done(self: DashboardState, task: "asyncio.Task") -> None:  # type: ignore[type-arg]
    """Release the finished notice task and consume any exception it stored.

    ``_notify_inbound_unbind`` swallows its own delivery failures, so an
    exception here is unexpected; reading it keeps asyncio from logging a bare
    "exception was never retrieved" at GC time.
    """
    self._background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("inbound-unbind notice task failed: %s", exc)


async def _notify_inbound_unbind(
    self: DashboardState, key: str, link: ChannelLink, reason: str
) -> None:
    """Tell the conversation behind *link* that it is detached.

    Rides the governed cross-surface ladder rather than the transport directly,
    so the send is capability-checked and governance-vetted like every other
    outbound notice. Best-effort: the binding is already gone and audited, so an
    unreachable, ungoverned or unregistered channel is logged and dropped
    rather than raised on a background task.
    """
    # Lazy: chat_runner imports this module at scope, so a top-level import
    # here would close the cycle.
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    try:
        # Off-loop: the ladder's governance gate walks the profile directory,
        # which is unbounded on slow storage.
        target = await asyncio.to_thread(_resolve_channel_target, self, key, link)
        if target is None:
            # The docstring's "logged and dropped" promise, kept: an
            # unreachable, ungoverned or unregistered channel means the user
            # was NOT told their conversation lost its way back, and a silent
            # return here leaves that undeliverable notice invisible to the
            # operator too (the SEL event records the removal, not the
            # delivery failure).
            logging.getLogger("kiro_crew.dashboard.state").warning(
                "inbound-unbind notice for %s undeliverable: no governed %s transport",
                key,
                link.channel_type,
            )
            return
        resolved, transport = target
        notice = _INBOUND_UNBIND_NOTICE.format(
            title=self._unbind_notice_title(key),
            why=_INBOUND_UNBIND_WHY.get(reason, _INBOUND_UNBIND_WHY_DEFAULT),
        )
        # The title is user-controlled (a rename, or an LLM-authored one), so
        # the rendered notice goes through the SHARED outbound display sink —
        # display canonicalization, exfiltration URLs, credentials, then
        # mention defang — rather than a second copy of that order here.
        await transport.send_message(
            resolved.channel_id,
            display_safe(notice),
            thread_id=resolved.thread_id,
        )
    except Exception:
        logging.getLogger("kiro_crew.dashboard.state").warning(
            "Failed to deliver inbound-unbind notice for %s", key, exc_info=True
        )


def _unbind_notice_title(self: DashboardState, key: str) -> str:
    """Name the detached session the way the user saw it, falling back to *key*.

    A title only exists while a slot is displaying the session; the raw key
    still identifies it, so nothing beyond the in-memory slot is worth a lookup.
    """
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    slot_key = dashboard_slot_key(key)
    slot = self.get_slot(slot_key) if slot_key else None
    if slot is None:
        return key
    return slot.display_title or key


def wire_session_recycle_callback(self: DashboardState) -> None:
    """Register the dashboard's recycle-notification callback.

    Fired when the watchdog recycles a session (e.g. RSS threshold). Posts a
    notice into the slot so the user understands why their session reset.
    """

    async def _on_recycled(key: str, *, reason: str) -> None:
        from kiro_crew.dashboard.chat_handlers import _has_conversation
        from kiro_crew.dashboard.chat_utils import (
            _broadcast_expired_oauth_banners,
            dashboard_slot_key,
        )

        # A channel-born session's key is the channel's own even while its
        # tab is open, so ask which tab displays it rather than reading the
        # key's prefix — otherwise that tab resets with no explanation.
        slot_key = dashboard_slot_key(key)
        if not slot_key:
            return
        slot = self.get_slot(slot_key)
        if slot is None:
            return
        message = _SESSION_RECYCLED_NOTICE.format(reason=reason)
        try:
            # A tab starts its agent before the first message (eager spawn),
            # and that idle tree can already sit over the RSS ceiling, so the
            # watchdog can recycle a session nobody has talked to yet. The
            # notice explains a reset of a conversation; on a tab with none it
            # only makes a fresh "+ New" chat look broken. The banner
            # verdict below still goes out: the recycle did end that child.
            #
            # Tag kind="compaction" so the dashboard's follow-up [OPTIONS:]
            # backward scan skips this proactive system notice, matching the
            # auto-compact notice invariant. `notice` marks it as borrowing
            # the tag: it reports no compaction, so `is_turn_interrupted`
            # must not read it as a `/compact` request's result.
            if _has_conversation(slot):
                slot.append(
                    "assistant",
                    message,
                    "msg msg-a",
                    meta={"kind": "compaction", "notice": "session_recycled"},
                )
        except Exception:
            logging.getLogger("kiro_crew.dashboard.state").exception(
                "Failed to append recycle notice to slot %s", slot_key
            )
        # The recycle ended the child that owned any open MCP OAuth
        # banner's loopback listener; push the read gate's verdict so an
        # open tab withdraws the dead Authorize link without waiting for
        # its next refetch.
        try:
            _broadcast_expired_oauth_banners(self, slot)
        except Exception:
            logging.getLogger("kiro_crew.dashboard.state").exception(
                "Failed to broadcast OAuth banner expiry for slot %s", slot_key
            )

    self.sessions.set_recycle_callback(_on_recycled)

    def _on_stuck_turn(key: str, parked_secs: float) -> None:
        """Surface a stuck turn in the chat where it is happening.

        Same delivery choice as the recycle notice above, for the same
        reason: the person who needs to know is whoever is watching that
        session, so the notice goes to that transcript rather than to a DM or
        a global feed. A WARNING in the journal is not reaching a user.

        Sync, unlike ``_on_recycled``: the hook that fires this is not
        awaiting anything, and appending to a slot needs no I/O.
        """
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        # A channel-born session's key is the channel's own even while its tab
        # is open, so ask which tab displays it (see _on_recycled).
        slot_key = dashboard_slot_key(key)
        if not slot_key:
            return
        slot = self.get_slot(slot_key)
        if slot is None:
            return
        message = stuck_turn_notice(parked_secs)
        try:
            # kind="compaction" for the same reason as the recycle notice: it
            # keeps the dashboard's follow-up [OPTIONS:] backward scan from
            # treating a proactive system notice as the turn's own output.
            # `notice` marks the borrowed tag: a stuck turn is the OPPOSITE
            # of a completed one, so `is_turn_interrupted` must not read
            # this row as a `/compact` request's result.
            slot.append(
                "assistant",
                message,
                "msg msg-a",
                meta={"kind": "compaction", "notice": "stuck_turn"},
            )
        except Exception:
            logging.getLogger("kiro_crew.dashboard.state").exception(
                "Failed to append stuck-turn notice to slot %s", slot_key
            )

    self.sessions.on_stuck_turn = _on_stuck_turn
