"""The Slack session queue around a running turn: queue tags, ``!stop`` holds and the drain.

A message for a busy session key waits in that session's queue, tagged with its channel
and sender (``_queue_tags``, ``_entry_owner``) so that a ``!stop`` drops only its own
sender's entries. While overlapping stops -- ``!stop`` and the Stop buttons, which join
through ``slack_stop_in_flight`` -- are in flight, ``_stops_in_flight`` holds one
``_StopHold`` per key and ``_key_busy`` reads the key as busy; ``_settle_stop_hold`` puts
back what they detached once the last one settles. ``_drain_slack_queue`` then starts the
next entry through ``_dispatch_queued``, which resolves the approval mode
(``_resolve_approval_mode``) and dispatches the turn. ``_spawn_tracked`` keeps a strong
reference to fire-and-forget work, for the slash commands as well.

Split out of ``kiro_crew.slack.events``, which stays the import path and the patch
target: every name here is reached as ``kiro_crew.slack.events.<name>``, and a patch
through that facade reaches this module (``kiro_crew._facade_mirror``). Nothing here
imports the facade. New queue and stop-hold work goes here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from typing import TYPE_CHECKING, Any, AsyncIterator, Coroutine

from kiro_crew.config.loader import ACTIVATION_REVIEW, KiroCrewConfig
from kiro_crew.messaging.link import legacy_key
from kiro_crew.messaging.queue_drain import entries_queued_by, owner_token, tag_entry
from kiro_crew.session import unlink_queued_temp_paths
from kiro_crew.session_lifecycle import hand_queue_to_successor
from kiro_crew.slack.handler import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    handle_message,
    is_yolo_mode,
    slack_cfg,
)
from kiro_crew.slack.transport_dispatch import handle_message_transport
from kiro_crew.start_priority import person_priority

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger("kiro_crew.slack.events")


#: This channel's name in the shared queue-drain contract (``messaging/queue_drain.py``).
#: ONE constant, used both to tag the entries this module produces and to register its
#: drain, because a tag that does not match the registration cannot be woken for its own
#: entries.
_CHANNEL = "slack"


def _entry_owner(sender_id: str, channel: str) -> str:
    """The neutral token naming the principal a Slack message came from.

    ``!stop`` compares it to drop one person's queued messages and leave everybody
    else's: every member of a thread shares that thread's session key and queue, so a
    whole-queue clear there discards messages other members are still owed an answer to.

    The place is the channel, not the thread. A thread-scoped session IS one thread, and
    a single-session DM merges every thread of that 1:1 conversation into one key on
    purpose (``flat_dm_session_key``), so the thread root would only split the DM user's
    own entries away from a ``!stop`` typed at channel root. The sender id alone is not
    enough either: two channels can share a thread timestamp, and the channel is the
    WHERE the sibling transports' ``sender_key`` carries beside the WHO.
    """
    return owner_token(_CHANNEL, (sender_id, channel))


def _queue_tags(sender_id: str, channel: str) -> dict[str, Any]:
    """The channel and owner tags every Slack queue entry carries.

    Attached to the session queue's kwargs and to a pre-session ``_pending_queue``
    entry's kwargs alike, so a ``!stop`` scoped by owner can tell the caller's entries
    from the rest on both.
    """
    return tag_entry({}, _CHANNEL, _entry_owner(sender_id, channel))


class _StopHold:
    """The Slack ``!stop``s in flight on one session key, and what they detached."""

    __slots__ = ("count", "held", "dropping")

    def __init__(self) -> None:
        self.count = 0
        #: One ``(session entries, pending entries)`` pair per stop, in PRESS order. Each
        #: stop detaches everything queued at its press, so press order is arrival order.
        self.held: list[tuple[list[Any], list[Any]]] = []
        #: The owners whose stop went through, each with the index in ``held`` of the
        #: latest snapshot their stop detached. Only entries detached at or before it
        #: are theirs to drop: a message sent after one's own ``!stop`` is newer
        #: intent, even when another member's overlapping stop detached it.
        self.dropping: dict[str, int] = {}


#: Per orchestrator, the stops in flight on each session key. Overlapping stops share
#: one hold, so the queue is put back, and drained, only when the LAST one settles.
_stops_in_flight: weakref.WeakKeyDictionary[Any, dict[str, _StopHold]] = weakref.WeakKeyDictionary()


def _key_busy(orch: GatewayOrchestrator, session_key: str) -> bool:
    """Whether a turn holds *session_key*, or a ``!stop`` on it has not settled yet.

    What every Slack drain and the busy check read instead of ``_session_tasks``
    alone: a queued message started while a stop is in flight is one that stop's
    ``stop_turn`` would then cancel, losing the dequeued entry.
    """
    return session_key in orch._session_tasks or session_key in _stops_in_flight.get(orch, {})


async def _settle_stop_hold(orch: GatewayOrchestrator, session_key: str, hold: _StopHold) -> None:
    """Put back what overlapping stops detached, once the last of them has settled.

    The snapshots are merged in press order, which is arrival order, so a co-tenant's
    messages keep their order however the stops finished. Every stopping member's own
    entries are dropped with their files, the rest go back ahead of anything admitted
    since: to the session, or to the successor a hard stop respawns.
    """
    sessions = orch.sessions
    if sessions is None:
        return
    owned = [(entries_queued_by(owner), upto) for owner, upto in hold.dropping.items()]

    def _dropped(index: int, kwargs: Any) -> bool:
        return any(index <= upto and by(kwargs) for by, upto in owned)

    queued = [(i, entry) for i, (entries, _) in enumerate(hold.held) for entry in entries]
    pending = [(i, entry) for i, (_, entries) in enumerate(hold.held) for entry in entries]
    if hold.dropping:
        # Only once a stop went through: a declined one drops nothing.
        sessions.clear_queue(
            session_key, only=tuple(entry for i, entry in queued if _dropped(i, entry[2]))
        )
    kept = tuple(entry for i, entry in queued if not _dropped(i, entry[2]))
    if kept:
        if sessions.has_session(session_key):
            sessions.restore_queue(session_key, kept)
        else:
            await hand_queue_to_successor(sessions, session_key, kept)
    kept_pending = []
    for i, item in pending:
        if _dropped(i, item[2]):
            # Never reaches _dispatch_queued's cleanup, so its temp files go here.
            unlink_queued_temp_paths(item[2])
        else:
            kept_pending.append(item)
    if kept_pending:
        later = orch._pending_queue.get(session_key) or []
        orch._pending_queue[session_key] = kept_pending + list(later)


@contextlib.asynccontextmanager
async def slack_stop_in_flight(
    orch: GatewayOrchestrator | None, session_key: str
) -> AsyncIterator[None]:
    """Read *session_key* as busy for a Stop that detaches nothing of its own.

    The Stop buttons let ``stop_turn(owned_by=...)`` drop the presser's entries and
    leave everyone else's queued. Without this, the running turn can end while
    ``stop_turn`` awaits the cancel, its turn-end drain starts one of those kept
    entries, and an escalation to a hard reset then aborts it after it left the
    queue. Joining the shared hold keeps every drain off the key until the last
    overlapping Stop -- ``!stop`` included -- settles, then drains once.

    No orchestrator (a handler double, or a call before the gateway wired one) has
    no drain to hold, so nothing is held.
    """
    if orch is None:
        yield
        return
    # The router keys its tasks, holds and drains by the BARE thread ts, while a
    # button can carry the canonical ``slack:<ts>``: a hold under the canonical
    # form would leave the router's busy check blind to it, and would not share
    # an overlapping ``!stop``'s count.
    session_key = legacy_key(session_key) or session_key
    hold = _stops_in_flight.setdefault(orch, {}).setdefault(session_key, _StopHold())
    hold.count += 1
    try:
        yield
    finally:
        hold.count -= 1
        if hold.count <= 0:
            _stops_in_flight.get(orch, {}).pop(session_key, None)
            await _settle_stop_hold(orch, session_key, hold)
            await _drain_slack_queue(orch, session_key)


# Strong references to fire-and-forget asyncio.Tasks scheduled from
# synchronous paths. Python's event loop keeps only *weak* references to
# tasks, so without a strong reference a task can be garbage-collected
# mid-execution. Tasks remove themselves on completion via add_done_callback.
_bg_tasks: set[asyncio.Task[object]] = set()


def _spawn_tracked(
    coro: Coroutine[object, object, object],
    *,
    owner: "GatewayOrchestrator | None" = None,
) -> asyncio.Task[object]:
    """Schedule slash work under both retention and restart ownership."""
    task = asyncio.ensure_future(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_on_tracked_done)
    if owner is not None:
        owner._handler_tasks.add(task)
        task.add_done_callback(owner._handler_tasks.discard)
    return task


def _on_tracked_done(task: asyncio.Task[object]) -> None:
    """Discard a finished tracked task and surface any failure.

    A bare ``discard`` swallows exceptions from the spawned coroutine — a failed
    ``_respond`` POST (expired ``response_url``, network timeout) or a raising slash
    handler would store the exception in the task, which nobody reads and which is
    GC'd with the task, leaving operators blind to dropped slash replies. Log at
    DEBUG (these failures are routine and peer-driven) while still discarding.
    """
    _bg_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Tracked slash task failed: %s", exc)


async def _drain_slack_queue(orch: GatewayOrchestrator, session_key: str) -> None:
    """Start the next queued message when no turn's own tail will drain it.

    One message per call; each dispatched turn calls this again when it ends.

    The session layer's wake (``register_drain``) can name the canonical
    ``slack:<ts>``, while the router registers tasks, stop holds and the
    pre-session stash under the bare ts. Normalized first, so the busy check
    sees them and a queued entry cannot start under a running turn or a Stop.
    """
    session_key = legacy_key(session_key) or session_key
    if _key_busy(orch, session_key) or not orch.sessions:
        return
    _next = orch.sessions.dequeue(session_key)
    if not _next:
        # The pre-session stash, as the turn-end drains read it.
        _pq = orch._pending_queue.get(session_key)
        if not _pq:
            return
        _next = _pq.pop(0)
        if not _pq:
            del orch._pending_queue[session_key]
    task = asyncio.ensure_future(_dispatch_queued(orch, session_key, *_next))
    orch._session_tasks[session_key] = task
    orch._handler_tasks.add(task)

    def _after(done: asyncio.Task) -> None:  # type: ignore[type-arg]
        orch._handler_tasks.discard(done)
        if orch._session_tasks.get(session_key) is done:
            del orch._session_tasks[session_key]
        _spawn_tracked(_drain_slack_queue(orch, session_key))

    task.add_done_callback(_after)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Slack dispatch approval mode: CLI --approval flag wins, else config.

    Normalized to handle_message's auto/interactive contract; reads/yolo are
    gated separately (gateway approval-event path, global YOLO/trust).
    """
    # Runtime YOLO (owner-toggled via the `yolo` slash command, TTL-capped safety_override)
    # auto-approves all tools. The native loop checks is_yolo_mode() inline; the
    # transport TurnDriver only sees this resolved mode, so fold YOLO in here at
    # the single per-message chokepoint (evaluated fresh each message) — both
    # paths then honor the runtime toggle consistently.
    if is_yolo_mode():
        return APPROVAL_AUTO
    mode = orch._approval_mode or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def _dispatch_queued(
    orch: GatewayOrchestrator,
    session_key: str,
    msg_ts: str,
    text: str,
    kwargs: dict,
) -> None:
    """Dispatch a queued message — remove ⏳ reaction and call handle_message."""
    channel = kwargs.get("channel", "")
    thread_ts = kwargs.get("thread_ts")
    from_trusted_bot = bool(kwargs.get("from_trusted_bot", False))
    # The pre-ingestion message the enqueue site captured (see _route_message):
    # the queued ``text`` is the ingested form, which the spool must not record.
    _q_spool_text = kwargs.get("spool_text")
    if not isinstance(_q_spool_text, str):
        _q_spool_text = None
    _q_spool_attachments = int(kwargs.get("spool_attachments") or 0)
    if orch.slack:
        try:
            await orch.slack.remove_reaction(channel, msg_ts, "hourglass_flowing_sand")
        except Exception:
            pass
    # Route the queued follow-up through the SAME gate as the initial message so
    # behavior is consistent mid-conversation: a thread that took the transport
    # path must keep taking it for its queued follow-ups (not silently fall back
    # to native). Review-mode channels stay on native (privacy gate), matching
    # the _route_message gate.
    _activation = slack_cfg(orch).channel_config(channel).activation
    _use_transport = (
        getattr(getattr(slack_cfg(orch), "messaging", None), "use_transport", False) is True
        and _activation != ACTIVATION_REVIEW
    )
    try:
        if _use_transport:
            await handle_message_transport(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                text,
                thread_ts,
                msg_ts,
                kwargs.get("sender_id", ""),
                context_builder=orch.ctx_builder,
                conversation_log=orch.conv_log,
                approval_mode=_resolve_approval_mode(orch),
                agent_override=kwargs.get("agent_override"),
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                cron_service=orch.cron_svc,
                # Live-read per message (parity with native handle_message, which
                # loads config in handler_runtime/turn.py): orch._cfg is captured at
                # startup, so reading it here would make settings-UI toggle saves
                # silently inert until restart.
                reactions_enabled=KiroCrewConfig.load().slack.reactions_enabled,
                show_thinking=KiroCrewConfig.load().slack.show_thinking,
                consolidator=orch.consolidator,
                user_display_name=kwargs.get("user_display_name"),
                # Live gateway state for the session-directive consumer (a
                # monitor directive on a dashboard-owned thread resolves its
                # slot through orch.dashboard_state).
                gateway=orch,
                # Echo-loop guard travels with the queued turn (parity with the
                # immediate dispatch above).
                from_trusted_bot=from_trusted_bot,
                dm_single_session=KiroCrewConfig.load().slack.dm_single_session,
                start_priority=person_priority(not from_trusted_bot),
                spool_text=_q_spool_text,
                spool_attachments=_q_spool_attachments,
            )
            return
        await handle_message(
            orch.slack,  # type: ignore[arg-type]
            orch.sessions,  # type: ignore[arg-type]
            channel,
            text,
            thread_ts,
            msg_ts,
            kwargs.get("sender_id", ""),
            team_id=kwargs.get("team_id", ""),
            approval_mode=_resolve_approval_mode(orch),
            context_builder=orch.ctx_builder,
            cron_service=orch.cron_svc,
            conversation_log=orch.conv_log,
            consolidator=orch.consolidator,
            subagent_manager=orch.subagent_mgr,
            task_runner=orch.task_runner,
            channel_agent=kwargs.get("agent_override"),
            user_display_name=kwargs.get("user_display_name"),
            from_trusted_bot=from_trusted_bot,
            spool_text=_q_spool_text,
            spool_attachments=_q_spool_attachments,
            start_priority=person_priority(not from_trusted_bot),
        )
    finally:
        # The enqueue path deferred temp-image cleanup to here so the queued
        # turn's text could still resolve its image paths (see _route_message).
        # Unlink them now that the turn has consumed them — in finally so a
        # raising turn can't leak the temp files.
        unlink_queued_temp_paths(kwargs)
