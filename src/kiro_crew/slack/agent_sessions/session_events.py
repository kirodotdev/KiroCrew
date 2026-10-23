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

A whole-channel session (a code channel) carries no ``thread_ts``. Those Stops go
to the handlers registered with :func:`register_channel_stop_handler`; with none
claiming the channel, the Stop is resolved like any other.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.link import canonical_key
from kiro_crew.messaging.queue_drain import entries_queued_by
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_lifecycle import (
    STOP_DECLINED_COMPACTING_TEXT,
    compaction_in_flight,
    consume_stop_declined,
    decline_stop,
)
from kiro_crew.slack.agent_sessions import fence
from kiro_crew.slack.agent_sessions.status import driver_of

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
#: by ``(channel, thread_ts, event_ts)``. Bounded; a retry arrives within seconds.
_SEEN_STOPS: OrderedDict[tuple[str, str, str], None] = OrderedDict()
_SEEN_STOPS_MAX = 512

#: ``async (orch, event, user_id) -> bool``: handle a whole-channel Stop, True when
#: the channel was the handler's to stop.
ChannelStopHandler = Callable[["GatewayOrchestrator", dict, str], Awaitable[bool]]
_channel_stop_handlers: list[ChannelStopHandler] = []


def register_channel_stop_handler(callback: ChannelStopHandler) -> None:
    """Route Stops on whole-channel sessions (no ``thread_ts``) to *callback*.

    For the code-channel runtime, whose channel IS the session. Called with the
    orchestrator, the raw event and the presser AFTER the origin and permission
    checks above have passed; it returns True when the channel was its own (it
    then owns the stop and the status), False to let the Stop resolve normally.
    Register at import time. Idempotent per callback.
    """
    if callback not in _channel_stop_handlers:
        _channel_stop_handlers.append(callback)


def unregister_channel_stop_handler(callback: ChannelStopHandler) -> None:
    if callback in _channel_stop_handlers:
        _channel_stop_handlers.remove(callback)


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
        # while it was on, or code channels subscribing the Stop event), and these
        # events were ignored before this runtime existed.
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
    ``thread_ts``) belongs to code channels and needs ``slack.code_channels``."""
    from kiro_crew.slack.features import agent_sessions_enabled, code_channels_enabled

    cfg = getattr(orch, "_cfg", None)
    if event.get("type") == EVENT_STOPPED and not event.get("thread_ts"):
        return code_channels_enabled(cfg)
    return agent_sessions_enabled(cfg)


def _first_delivery(channel: str, thread_ts: str, event_ts: str) -> bool:
    if not event_ts:
        return True
    key = (channel, thread_ts, event_ts)
    if key in _SEEN_STOPS:
        return False
    _SEEN_STOPS[key] = None
    while len(_SEEN_STOPS) > _SEEN_STOPS_MAX:
        _SEEN_STOPS.popitem(last=False)
    return True


def _may_use_bot(user: str) -> bool:
    from kiro_crew.slack import events as _events

    return bool(user) and (_events.is_owner(user) or _events.is_allowed_user(user))


def resolve_session(
    orch: GatewayOrchestrator, channel: str, thread_ts: str | None
) -> tuple[str, str, bool] | None:
    """``(router_key, session_key, flat)`` for the session a thread belongs to.

    *router_key* is what the Slack router tracks the thread's task, queue and
    busy hold under (the flat DM key, else the bare thread ts); *session_key* is
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
    return router_key, session_key, flat_key is not None


def _is_ours(orch: GatewayOrchestrator, channel: str, thread_ts: str | None, keys: tuple) -> bool:
    router_key, session_key, _flat = keys
    driver = driver_of(orch.slack)
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
    ``denied``, ``duplicate``, ``not_ours`` and ``channel`` (a registered
    whole-channel handler took it). Never raises.
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
    if thread_ts is None:
        for claim in list(_channel_stop_handlers):
            try:
                if await claim(orch, event, user):
                    _audit(user, "slack.agent_session_stopped", "allowed", resources=channel)
                    return "channel"
            except Exception:
                logger.warning("channel stop handler failed for %s", channel, exc_info=True)
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
    outcome: str
    still_running = False
    if compaction_in_flight(sessions, session_key) and not force:
        outcome = "compacting"
    else:
        muted = fence.mute_turns(channel, None if flat else thread_ts)
        kw: dict[str, Any] = {"force": True, "preserve_queue": True} if force else {}
        active_task = orch._session_tasks.get(router_key)
        cancelled = False

        def _cancel_task() -> bool:
            if orch._session_tasks.get(router_key) is active_task:
                orch._session_tasks.pop(router_key, None)
            if active_task is not None and not active_task.done():
                active_task.cancel()
                return True
            return False

        try:
            # The presser's queued messages only: every member of a thread queues
            # under its one key. The hold keeps the router from starting a kept
            # entry until the stop, and the cancel of the task it ended, settle.
            async with _events.slack_stop_in_flight(orch, router_key):
                outcome = await sessions.stop_turn(
                    session_key,
                    owned_by=entries_queued_by(_events._entry_owner(user, channel)),
                    **kw,
                )
                if outcome != "compacting":
                    cancelled = _cancel_task()
        except Exception:
            # The session stop failed (a reset that raised); the person still asked
            # for the turn to end, so its task goes regardless.
            logger.warning("stop_turn failed for agent_session_stopped", exc_info=True)
            outcome = "error"
            cancelled = _cancel_task()
        if outcome == "compacting":
            fence.unmute(muted)
        elif outcome in ("idle", "error") and muted and not cancelled:
            # A turn is replying in this thread, but neither the session stop nor a
            # task cancel reached it: one still opening its prompt (cold start,
            # context build) that no router task tracks. Silencing it would hide a
            # turn that keeps working, so it stays audible and keeps its loader.
            fence.unmute(muted)
            still_running = True
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
    elif not still_running:
        await _settle(orch, channel, thread_ts, streams)
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
    driver = driver_of(slack)
    if driver is not None and thread_ts:
        try:
            await driver.settle_after_stop(channel, thread_ts)
        except Exception:
            logger.debug("status settle failed after agent_session_stopped", exc_info=True)


async def handle_title_changed(orch: GatewayOrchestrator, event: dict) -> str:
    """Mirror a user's Slack rename onto the linked dashboard session."""
    from kiro_crew.messaging import auto_title, privacy_mode

    channel = str(event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or "") or None
    user = str(event.get("user") or "")
    raw = str(event.get("title") or "").strip()
    if not (channel and thread_ts and raw):
        return "ignored"
    if not _first_delivery(channel, thread_ts, str(event.get("event_ts") or "")):
        # A redelivery: the first copy already consumed its echo or was mirrored.
        return "duplicate"
    driver = driver_of(orch.slack)
    if driver is not None and driver.take_echo(channel, thread_ts, raw[:200]):
        # Our own rename coming back, not a person's; each is matched once.
        return "echo"
    if not _may_use_bot(user):
        _audit(
            user,
            "slack.agent_session_title_changed",
            "denied",
            resources=f"{channel}:{thread_ts}",
            error="unauthorized sender",
        )
        return "denied"
    if not await channel_inbound_permitted("slack"):
        # A rename is inbound content changing dashboard metadata, so the
        # ``channels`` policy ceiling applies to it like a message. (A Stop is
        # exempt, as ``!stop`` is: cancelling must stay possible on a denied channel.)
        _audit(
            user,
            "slack.agent_session_title_changed",
            "denied",
            resources=f"{channel}:{thread_ts}",
            error="channels policy denies slack",
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
    return "renamed"


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
                    {"title": title},
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
