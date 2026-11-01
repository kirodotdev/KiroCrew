"""Slack agent-session events: the native Stop button and user renames.

Two events reach the app once it is a Slack agent and subscribes to them:

* ``agent_session_stopped`` -- a person pressed the Stop button Slack shows on a
  ``processing`` session. :func:`handle_stopped` stops that thread's in-flight
  turn the way the dashboard Stop does: one cooperative cancel through
  ``SessionManager.stop_turn``, which waits for the backend to acknowledge and
  escalates to a reset only when it does not. The stopped turn then posts nothing
  more (:mod:`.fence`), the streams Slack named are sealed, and the session is
  moved off ``processing``, which Slack leaves to the app.
* ``agent_session_title_changed`` -- a person renamed the session in Slack.
  :func:`handle_title_changed` gives the linked dashboard session the same name,
  as a manual title, so auto-titling never overwrites it.

Who may do what (each decision is SEL-audited):

* Both events first pass the per-workspace origin gate a message passes
  (``slack_gate.check_message_origin``) on the envelope ``team_id``, so a
  workspace outside ``slack.allowed_enterprise_ids`` reaches no session.
* Stopping a turn takes the same standing as ``!stop``: the owner or an allowed
  user. A Stop from anyone else stops nothing.
* A rename from anyone but the owner or an allowed user is not mirrored: every
  member of a channel can retitle a Slack session, but only people allowed to use
  the bot may rename its conversation in the dashboard.

A Stop is idempotent: a redelivered event (same ``event_ts``) is dropped, as is a
redelivered rename, and a
second press on a turn that already ended finds it idle and only re-settles the
status. A Stop for a thread this bot has no session in calls nothing at all -- not
even ``setStatus``, which would create a session there.

A Stop with no ``thread_ts`` (a whole-channel session) belongs to no feature
here and is ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.link import canonical_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_lifecycle import (
    STOP_DECLINED_COMPACTING_TEXT,
    compaction_in_flight,
    consume_stop_declined,
    decline_stop,
)
from kiro_crew.slack.agent_sessions import fence
from kiro_crew.slack.agent_sessions.status import clearing_driver, driver_of
from kiro_crew.slack.channel_policy import channel_refusal
from kiro_crew.slack.events_inbound import SeenCache

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)

EVENT_STOPPED = "agent_session_stopped"
EVENT_TITLE_CHANGED = "agent_session_title_changed"
#: The agent-session events this runtime consumes; both have a handler below.
AGENT_SESSION_EVENTS = frozenset({EVENT_STOPPED, EVENT_TITLE_CHANGED})

#: Same cap the ``!title`` command applies to a dashboard title.
_TITLE_MAX = 80

#: Redelivered Stops and renames (Socket Mode retries an unacknowledged envelope)
#: by ``channel|thread_ts|event_ts``, in the socket path's own dedupe cache.
#: Bounded; a retry arrives within seconds.
_SEEN_EVENTS_MAX = 512
_SEEN_EVENTS = SeenCache(_SEEN_EVENTS_MAX)

#: One rename at a time per thread, in arrival order. Each handler takes its
#: thread's lock before its first await, and asyncio locks queue waiters FIFO, so
#: two renames racing through the policy and privacy reads commit in the order
#: they were dispatched. ``[lock, interested handlers]``; an entry is dropped once
#: nobody holds or waits on it.
_TITLE_LOCKS: dict[tuple[str, str], list[Any]] = {}

#: The newest ``event_ts`` of a rename applied (or an echo of ours seen) per
#: ``(channel, thread_ts)``. A rename Slack retries after a newer one was applied
#: (its first delivery went unacknowledged) is dropped instead of overwriting it.
#: Bounded like ``_SEEN_EVENTS``.
_NEWEST_TITLE_TS: OrderedDict[tuple[str, str], float] = OrderedDict()


def _audit(caller: str, operation: str, outcome: str, **kw: Any) -> None:
    try:
        sel().log_api_access(
            caller=caller or "unknown", operation=operation, outcome=outcome, source="slack", **kw
        )
    except Exception:
        logger.warning("SEL write failed for %s", operation, exc_info=True)


def dispatch(orch: GatewayOrchestrator, payload: dict, event: dict) -> None:
    """Origin-check one agent-session event, then handle it off the socket listener.

    Slack puts ``team_id`` on the enclosing ``event_callback`` envelope (the
    event body carries none for a Stop), so the envelope id wins, as on the
    message path. An event with no workspace id at all is refused.
    """
    from kiro_crew.slack import events as _events

    event_type = str(event.get("type") or "")
    if event_type not in AGENT_SESSION_EVENTS:
        return
    if not _feature_on(orch, event):
        # Off means off: the subscription can outlive the switch (an app installed
        # while it was on), and these events were ignored before this runtime existed.
        logger.debug("%s ignored: the Slack feature it belongs to is off", event_type)
        return
    caller = str(event.get("user") or "")
    team_id = str(payload.get("team_id") or event.get("team_id") or event.get("team") or "")
    channel = str(event.get("channel") or "")
    if not team_id:
        logger.warning("%s rejected: no team_id on the event or its envelope", event_type)
        _audit(caller, f"slack.{event_type}", "denied", error="missing_team_id")
        return
    if not _events.current_context().slack_gate.check_message_origin(team_id):
        logger.error("%s rejected: team_id=%s is not an allowed workspace", event_type, team_id)
        _audit(
            caller,
            f"slack.{event_type}",
            "denied",
            resources=f"team_id={team_id} channel={channel}",
            error="enterprise_origin_mismatch",
        )
        return
    handler = handle_stopped if event_type == EVENT_STOPPED else handle_title_changed
    task = asyncio.create_task(handler(orch, event))
    orch._handler_tasks.add(task)
    task.add_done_callback(orch._handler_tasks.discard)


def _feature_on(orch: GatewayOrchestrator, event: dict) -> bool:
    """Thread sessions need ``slack.agent_sessions``; a whole-channel Stop (no
    ``thread_ts``) belongs to no feature here, so it stays ignored."""
    from kiro_crew.slack.features import agent_sessions_enabled

    if event.get("type") == EVENT_STOPPED and not event.get("thread_ts"):
        return False
    if agent_sessions_enabled(getattr(orch, "_cfg", None)):
        return True
    # Off now, but a Stop on a session this process left working (the feature
    # went off mid-turn) still stops the turn and settles the session, or Slack
    # shows a dead Stop on "Working..." until its own timeout.
    return event.get("type") == EVENT_STOPPED and (
        clearing_driver(
            getattr(orch, "slack", None),
            str(event.get("channel") or ""),
            str(event.get("thread_ts") or ""),
        )
        is not None
    )


def _first_delivery(channel: str, thread_ts: str, event_ts: str) -> bool:
    if not event_ts:
        return True
    return not _SEEN_EVENTS.check_and_add(f"{channel}|{thread_ts}|{event_ts}")


def _may_use_bot(user: str) -> bool:
    from kiro_crew.slack import events as _events

    return bool(user) and (_events.is_owner(user) or _events.is_allowed_user(user))


def _entry_thread(kwargs: Any) -> str:
    """The thread a queued Slack entry was sent in, or "" at channel root."""
    return str((kwargs or {}).get("thread_ts") or "")


def _linked_thread(sessions: Any, thread_ts: str | None, flat_key: str) -> bool:
    """Whether *thread_ts* is a thread a dashboard send moved to its own session.

    A self-derived owner (``slack:<thread_ts>``) is not a binding, as in
    :func:`resolve_session`, and nor is the flat DM's own key: a thread bound to the
    DM's session is the DM.
    """
    if not thread_ts or sessions is None:
        return False
    owner = sessions.get_session_for_thread(thread_ts)
    return owner is not None and owner not in (canonical_key(thread_ts), flat_key)


def resolve_session(
    orch: GatewayOrchestrator, channel: str, thread_ts: str | None
) -> tuple[str, str, bool] | None:
    """``(router_key, session_key, flat)`` for the session a thread belongs to.

    *router_key* is what the Slack router tracks the thread's task, queue and
    busy hold under (the flat DM key, else the bare thread ts; a dashboard-linked
    thread of a flat DM keeps the flat key, which the router uses for it too); *session_key* is
    the session the turn RUNS in, which a dashboard send-to-Slack can have moved
    to its own session -- with or without single-session DMs, the binding the
    turn path itself follows (``_resolve_thread_owner``). A self-derived owner
    (``slack:<thread_ts>``) is not a binding. *flat* says the whole DM is the
    session. None when the event addresses no session at all.
    """
    from kiro_crew.slack import events as _events

    flat_key = _events.flat_dm_session_key(
        channel, thread_ts, enabled=_events._dm_single_session_enabled(orch, channel)
    )
    router_key = flat_key or thread_ts
    if not router_key:
        return None
    session_key = router_key
    sessions = orch.sessions
    if thread_ts and sessions is not None:
        owner = sessions.get_session_for_thread(thread_ts)
        if owner is not None and owner != canonical_key(thread_ts):
            session_key = owner
    # A DM thread a dashboard send moved to its own session is that session, not
    # the flat DM: its Stop fences the thread and its rename reaches its title.
    # Its router key stays the flat one: the router runs and queues every thread of
    # a single-session DM under that key, a linked thread's messages included, so a
    # Stop there narrows the hold, the drop and the cancel to the thread itself.
    return router_key, session_key, flat_key is not None and session_key == flat_key


def _is_ours(orch: GatewayOrchestrator, channel: str, thread_ts: str | None, keys: tuple) -> bool:
    router_key, session_key, _flat = keys
    # The live driver, or the one still owing this thread its ``active`` after
    # the feature went off or latched: such a thread is this bot's either way.
    driver = clearing_driver(orch.slack, channel, thread_ts)
    sessions = orch.sessions
    if driver is not None and driver.owns(channel, thread_ts):
        return True
    if router_key in orch._session_tasks:
        return True
    if sessions is None:
        return False
    if sessions.has_session(session_key) or sessions.has_session(router_key):
        return True
    # The thread index outlives a restart, which empties the two maps above while
    # Slack may still show this thread as working.
    return thread_ts is not None and sessions.get_session_for_thread(thread_ts) is not None


async def handle_stopped(orch: GatewayOrchestrator, event: dict) -> str | None:
    """Stop the turn behind a pressed Stop button; returns the stop outcome.

    Outcomes are ``stop_turn``'s (``soft`` / ``hard`` / ``idle`` / ``compacting``)
    plus ``error`` (the session stop raised; the turn's task is cancelled anyway),
    ``denied``, ``duplicate`` and ``not_ours``. Never raises.
    """
    from kiro_crew.slack import events as _events

    channel = str(event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or "") or None
    user = str(event.get("user") or "")
    streams = [str(ts) for ts in (event.get("streaming_message_ts") or []) if ts]
    if not _first_delivery(channel, thread_ts or "", str(event.get("event_ts") or "")):
        logger.debug("agent_session_stopped redelivered for %s/%s; ignored", channel, thread_ts)
        return "duplicate"
    if not _may_use_bot(user):
        _audit(
            user,
            "slack.agent_session_stopped",
            "denied",
            resources=f"{channel}:{thread_ts or ''}",
            error="unauthorized sender",
        )
        return "denied"
    if not channel or orch.sessions is None:
        return None
    keys = resolve_session(orch, channel, thread_ts)
    if keys is None or not _is_ours(orch, channel, thread_ts, keys):
        # Nothing of this bot's runs there: no stop, and no setStatus, which would
        # create a session in a thread the bot never answered.
        _audit(
            user,
            "slack.agent_session_stopped",
            "no_session",
            resources=f"{channel}:{thread_ts or ''}",
        )
        return "not_ours"
    router_key, session_key, flat = keys
    _audit(user, "slack.agent_session_stopped", "allowed", resources=session_key)

    sessions = orch.sessions
    # A repeat press inside the window, while the session's own compaction still
    # holds it, is the forcing second press the decline promised (the rule
    # ``!stop`` and the Stop buttons follow).
    force = consume_stop_declined(session_key, user) and compaction_in_flight(sessions, session_key)
    outcome = "error"
    still_running = False
    if compaction_in_flight(sessions, session_key) and not force:
        outcome = "compacting"
    else:
        kw: dict[str, Any] = {"force": True, "preserve_queue": True} if force else {}
        active_task = orch._session_tasks.get(router_key)
        # A single-session DM's router key also carries its dashboard-linked
        # threads, each its own session. A Stop in one linked thread addresses that
        # thread alone; the DM's own Stop addresses everything but those threads.
        shared_key = not flat and thread_ts is not None and router_key != thread_ts
        addresses: Callable[[Any], bool] | None = None
        if shared_key:
            if active_task is not None and _events.router_task_thread(active_task) != thread_ts:
                active_task = None

            def _in_this_thread(kwargs: Any) -> bool:
                return _entry_thread(kwargs) == thread_ts

            addresses = _in_this_thread
        elif flat:
            # Judged once per thread, at the press: a binding a dashboard send adds
            # or removes while the stop is in flight does not change what it meant.
            linked_at_press: dict[str, bool] = {}

            def _linked(thread: str | None) -> bool:
                if not thread:
                    return False
                if thread not in linked_at_press:
                    linked_at_press[thread] = _linked_thread(sessions, thread, router_key)
                return linked_at_press[thread]

            def _outside_linked_threads(kwargs: Any) -> bool:
                return not _linked(_entry_thread(kwargs))

            addresses = _outside_linked_threads
            if active_task is not None and _linked(_events.router_task_thread(active_task)):
                active_task = None
        # The holds come after everything above that can raise, so a failing
        # lookup leaks none. Nothing above awaits, so the press is judged as before.
        muted = fence.mute_turns(channel, None if flat else thread_ts)
        if flat:
            try:
                linked = [s for s in muted if _linked(s.thread_ts)]
            except Exception:
                fence.unmute(muted)
                raise
            fence.unmute(linked)
            muted = [s for s in muted if s not in linked]
        cancelled = False
        decided = False

        def _cancel_task() -> bool:
            if orch._session_tasks.get(router_key) is active_task:
                orch._session_tasks.pop(router_key, None)
            if active_task is not None and not active_task.done():
                active_task.cancel()
                return True
            return False

        try:
            # The presser's queued messages only: every member of a thread queues
            # under its one key. What was queued at the press is detached into
            # the shared hold (as ``!stop`` does) and ``stop_turn`` keeps what is
            # admitted later. The hold keeps the router from starting a kept
            # entry until the stop, the cancel of the task it ended and the status
            # settle are done: a kept entry dispatched earlier announces its own
            # ``processing``, which the settle's ``active`` would then overwrite.
            owner = _events._entry_owner(user, channel)
            async with _events.slack_stop_in_flight(
                orch,
                router_key,
                owner=owner,
                detach=True,
                addresses=addresses,
            ) as went_through:
                try:
                    outcome = await sessions.stop_turn(
                        session_key, **{**kw, "preserve_queue": True}
                    )
                except Exception:
                    # The session stop failed (a reset that raised); the person
                    # still asked for the turn to end, so its task goes regardless.
                    # Nothing is dropped: the hold puts every entry back.
                    logger.warning("stop_turn failed for agent_session_stopped", exc_info=True)
                    outcome = "error"
                    cancelled = _cancel_task()
                else:
                    if outcome != "compacting":
                        if outcome == "hard" and not force:
                            # An escalated stop reset the session: as ``!stop``'s,
                            # what was queued at the press goes for everybody.
                            went_through(reset=True)
                        elif not (force and outcome == "hard"):
                            # A forced reset is aimed at the compaction, not the
                            # queue, and drops nobody's entries (``!stop``'s rule).
                            went_through()
                        cancelled = _cancel_task()
                still_running = _keep_audible(outcome, muted, cancelled)
                decided = True
                if outcome != "compacting" and not still_running:
                    await _settle(orch, channel, thread_ts, streams)
                    if flat:
                        # A single-session DM's Stop ends the turn replying in
                        # another of its threads too: that thread leaves
                        # ``processing`` as well (linked threads are not in muted),
                        # but only one this bot put into a status -- a setStatus
                        # elsewhere would create a session Slack never showed.
                        for ended in sorted({s.thread_ts for s in muted} - {thread_ts}):
                            # Also when the feature went off or latched meanwhile:
                            # a thread still owed its ``active`` gets it.
                            ended_driver = clearing_driver(orch.slack, channel, ended)
                            if ended_driver is not None and ended_driver.owns(channel, ended):
                                await _settle(orch, channel, ended, [])
        except Exception:
            # The hold's release raised (putting entries back, or the drain) after
            # the stop above decided and settled; the outcome stands. A stop that
            # ended a turn still ends its task; a declined one leaves it running.
            logger.warning("stop hold release failed for agent_session_stopped", exc_info=True)
            if not cancelled and outcome != "compacting":
                cancelled = _cancel_task()
        finally:
            if not decided:
                # Nothing decided (the hold itself failed): the person asked for the
                # turn to end, so it stays silenced rather than coming back.
                fence.release(muted, ended=outcome != "compacting")
    if outcome == "compacting":
        # Nothing was stopped, so the session really is still working and keeps its
        # loader and Stop button. The presser is told privately, and only a warning
        # that landed arms the forcing second press.
        async def _say_declined() -> bool:
            if orch.slack is None:
                return False
            try:
                # Raises when it did not land; returns nothing when it did.
                await orch.slack.post_ephemeral(
                    channel, user, STOP_DECLINED_COMPACTING_TEXT, thread_ts=thread_ts
                )
            except Exception:
                return False
            return True

        await decline_stop(session_key, user, _say_declined)
    try:
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="agent_session_stopped",
            tool_kind="command",
            outcome=outcome,
            metadata={"user": user, "channel": channel},
        )
    except Exception:
        logger.warning("SEL write failed for agent_session_stopped", exc_info=True)
    return outcome


def _keep_audible(outcome: str, muted: Any, cancelled: bool) -> bool:
    """Release this Stop's holds as it decided; True when a turn keeps running there.

    A compaction stopped nothing. On ``idle`` / ``error`` with a held turn and no
    task cancelled, a turn is replying in this thread that neither the session
    stop nor a task cancel reached: one still opening its prompt (cold start,
    context build) that no router task tracks. Silencing it would hide a turn
    that keeps working, so this Stop lets go of it and it keeps its loader --
    audible again unless another Stop still holds or has ended it. Anything else
    ended the turn, which stays silenced whatever an overlapping Stop decides.
    """
    if outcome == "compacting":
        fence.release(muted, ended=False)
        return False
    if outcome in ("idle", "error") and muted and not cancelled:
        fence.release(muted, ended=False)
        return True
    fence.release(muted, ended=True)
    return False


async def _settle(
    orch: GatewayOrchestrator, channel: str, thread_ts: str | None, streams: list[str]
) -> None:
    """Seal the streams Slack stopped and move the session off ``processing``."""
    slack = orch.slack
    if slack is None:
        return
    for stream_ts in streams:
        try:
            await slack.stop_stream(channel, stream_ts)
        except Exception:
            logger.debug("stop_stream failed after agent_session_stopped", exc_info=True)
    driver = clearing_driver(slack, channel, thread_ts)
    if driver is not None and thread_ts:
        try:
            await driver.settle_after_stop(channel, thread_ts)
        except Exception:
            logger.debug("status settle failed after agent_session_stopped", exc_info=True)


async def handle_title_changed(orch: GatewayOrchestrator, event: dict) -> str:
    """Mirror a user's Slack rename onto the linked dashboard session."""
    channel = str(event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or "") or None
    raw = str(event.get("title") or "").strip()
    if not (channel and thread_ts and raw):
        return "ignored"
    async with _rename_turn(channel, thread_ts):
        return await _apply_title_change(orch, event, channel, thread_ts, raw)


@contextlib.asynccontextmanager
async def _rename_turn(channel: str, thread_ts: str) -> AsyncIterator[None]:
    """Hold *thread_ts*'s rename lock; see ``_TITLE_LOCKS``."""
    key = (channel, thread_ts)
    entry = _TITLE_LOCKS.setdefault(key, [LoopBoundLock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0 and _TITLE_LOCKS.get(key) is entry:
            del _TITLE_LOCKS[key]


async def _apply_title_change(
    orch: GatewayOrchestrator, event: dict, channel: str, thread_ts: str, raw: str
) -> str:
    from kiro_crew.messaging import auto_title, privacy_mode

    user = str(event.get("user") or "")
    event_ts = str(event.get("event_ts") or "")
    if not _first_delivery(channel, thread_ts, event_ts):
        # A redelivery: the first copy already consumed its echo or was mirrored.
        return "duplicate"
    driver = driver_of(orch.slack)
    if driver is not None and driver.take_echo(channel, thread_ts, raw[:200]):
        # Our own rename coming back, not a person's; each is matched once.
        _note_title_ts(channel, thread_ts, event_ts)
        return "echo"
    if _older_than_applied(channel, thread_ts, event_ts):
        # A retried rename Slack sent before one already applied here: the newer
        # title stands.
        return "stale"
    if not _may_use_bot(user):
        _audit(
            user,
            "slack.agent_session_title_changed",
            "denied",
            resources=f"{channel}:{thread_ts}",
            error="unauthorized sender",
        )
        return "denied"
    if not (
        await channel_inbound_permitted("slack")
        and not channel_refusal(channel, caller=user, audit=False)
    ):
        # A rename is inbound content changing dashboard metadata, so the
        # ``channels`` policy ceiling and its Slack channel list apply to it like
        # a message. (A Stop is exempt, as ``!stop`` is: cancelling must stay
        # possible on a denied channel.) One audit row, written here.
        _audit(
            user,
            "slack.agent_session_title_changed",
            "denied",
            resources=f"{channel}:{thread_ts}",
            error=channel_refusal(channel, audit=False) or "channels policy denies slack",
        )
        return "denied"
    keys = resolve_session(orch, channel, thread_ts)
    if keys is None or not _is_ours(orch, channel, thread_ts, keys):
        # A thread this bot never answered has no conversation of ours to rename.
        return "ignored"
    if keys[2]:
        # A single-session DM is ONE conversation across all its threads, while
        # Slack titles each thread's session: one thread's name is not the DM's.
        return "ignored"
    session_key = _title_key(keys[1], thread_ts)
    # The durable incognito/temporary record first: after a restart the in-memory
    # trackers start empty, and a rename can arrive before any message restores them.
    await privacy_mode.hydrate_inbound(orch.sessions, session_key)
    again = resolve_session(orch, channel, thread_ts)
    if again is None or _title_key(again[1], thread_ts) != session_key:
        # A dashboard send-to-Slack claimed the thread during the read: the rename
        # was aimed at a conversation that has lost the thread.
        return "ignored"
    if privacy_mode.is_restricted(session_key):
        _audit(
            user,
            "slack.agent_session_title_changed",
            "allowed",
            resources=session_key,
            error="restricted session: not recorded",
        )
        return "restricted"
    conv_log = getattr(orch, "conv_log", None)
    title, _ = redact_exfiltration_urls(raw)
    title, _ = redact_credentials(title)
    title = title[:_TITLE_MAX]
    # A live dashboard slot is authoritative: renaming only the transcript would
    # let that slot's next save write its old title back. The slot rename also
    # marks the title manual and broadcasts it, so it is the whole write.
    try:
        from kiro_crew.dashboard.channel_slots import rename_channel_title_live

        renamed_live = await rename_channel_title_live(
            getattr(orch, "dashboard_state", None), session_key, title
        )
    except Exception:
        logger.warning("could not rename the live slot for %s", session_key, exc_info=True)
        return "failed"
    if not renamed_live:
        if conv_log is None:
            return "ignored"
        if not await _write_title(conv_log, session_key, title):
            return "failed"
    # After the write: a manual mark with no title behind it would stop
    # auto-titling a conversation that never got its name.
    auto_title.mark_titled(session_key, auto_title.TITLE_KIND_MANUAL)
    _audit(user, "slack.agent_session_title_changed", "allowed", resources=session_key)
    _note_title_ts(channel, thread_ts, event_ts)
    return "renamed"


def _ts_value(event_ts: str) -> float | None:
    try:
        value = float(event_ts)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _older_than_applied(channel: str, thread_ts: str, event_ts: str) -> bool:
    """True when *event_ts* predates the newest rename already applied to the thread."""
    value = _ts_value(event_ts)
    newest = _NEWEST_TITLE_TS.get((channel, thread_ts))
    return value is not None and newest is not None and value < newest


def _note_title_ts(channel: str, thread_ts: str, event_ts: str) -> None:
    value = _ts_value(event_ts)
    if value is None:
        return
    key = (channel, thread_ts)
    _NEWEST_TITLE_TS[key] = max(value, _NEWEST_TITLE_TS.pop(key, value))
    while len(_NEWEST_TITLE_TS) > _SEEN_EVENTS_MAX:
        _NEWEST_TITLE_TS.popitem(last=False)


def _title_key(session_key: str, thread_ts: str | None) -> str:
    """The conversation-log key a rename writes: a bare thread ts is ``slack:<ts>``."""
    return canonical_key(session_key) if session_key == thread_ts else session_key


async def _write_title(conv_log: Any, session_key: str, title: str) -> bool:
    """Persist *title* and say whether it landed.

    ``update_metadata_if(require_write=True)`` reports a write a legacy
    message-first transcript skipped, which ``set_title`` swallows; a log without
    it (an older double) falls back to ``set_title``.
    """
    try:
        conditional = getattr(conv_log, "update_metadata_if", None)
        if callable(conditional):
            return bool(
                await asyncio.to_thread(
                    conditional,
                    session_key,
                    # ``title_origin`` "user", as a dashboard rename stores it: a tab
                    # reopened on this transcript must restore the name as final,
                    # not as an auto title its background refresh may replace.
                    {"title": title, "title_origin": "user"},
                    lambda _meta: True,
                    require_existing=True,
                    require_write=True,
                )
            )
        await asyncio.to_thread(conv_log.set_title, session_key, title)
        return True
    except Exception:
        logger.warning("could not record the Slack title for %s", session_key, exc_info=True)
        return False
