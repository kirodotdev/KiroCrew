"""Context-bar action clicks: the ``code_channel_action`` event.

The context bar's ``item_type: "action"`` items (:mod:`.workspace.context_bar`)
dispatch to the app instead of linking out: a click arrives as a
``code_channel_action`` event carrying the item's key. Each key runs an EXISTING
path, with that path's own checks and audit -- no action adds a way in:

``create-pr``
    :func:`.commands.run_command` for ``/create-pr`` (allowed user, the channel's
    ORIGIN still admits the bot, not archived, the channel can open a PR).
``stop``
    Slack's own Stop: :func:`~kiro_crew.slack.agent_sessions.session_events.handle_stopped`
    with the channel as the session, so the click is de-duplicated, checked
    (owner or allowed user) and handed to the code channel's stop handler
    exactly like a press of the Stop button Slack shows while a turn runs.

Any other key is refused: the bar offers only these two actions.

Create PR passes the gates every inbound Slack path applies: the feature is on,
the event's workspace is admitted, the ``channels`` policy admits inbound Slack,
and the channel is a bound code channel. Stop goes ahead of them and is asked
exactly what Slack's own Stop is (``session_events.admits``, then
``session_events.handle_stopped``). A person a path refuses is told so privately
(an ephemeral message); every decision is SEL-audited. A redelivered click (same
event id) does nothing.

Slack's reference does not yet document this event's body, so the fields are
read leniently (:func:`parse`): the channel, the person and the item key, each
from the spellings Slack uses on its other events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.store import Binding, store
from kiro_crew.slack.code_channels.workspace.context_bar import ACTION_CREATE_PR, ACTION_STOP

logger = logging.getLogger(__name__)

EVENT = "code_channel_action"
OPERATION = "slack.code_channel.action"

NOT_A_CODE_CHANNEL_TEXT = "That button works only in a Kiro Crew code channel."
NOT_AUTHORIZED_TEXT = "⛔ You are not authorized to use this here."
NOTHING_RUNNING_TEXT = "Nothing is running."
UNKNOWN_ACTION_TEXT = "That button isn't one Kiro Crew offers any more."


@dataclass(frozen=True)
class Click:
    channel: str
    user: str
    key: str
    team: str
    #: What de-duplicates a redelivery ("" when the event carries nothing).
    dedupe: str


def _id(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("id")
    return str(value or "") if isinstance(value, (str, int)) else ""


#: The labels whose first click's field names were logged (one log line each).
_shape_logged: set[str] = set()


def _log_shape(event: dict[str, Any], what: str = "code_channel_action body") -> None:
    """Log the first *what*'s field NAMES (never values), to pin the body once seen."""
    if what not in _shape_logged:
        _shape_logged.add(what)
        logger.info("%s keys: %s", what, sorted(str(k) for k in event))


def parse(payload: dict[str, Any], event: dict[str, Any]) -> Click:
    """The click in *event* (inside the ``event_callback`` *payload*).

    The person is the one user field the body carries. Two that name different
    people make the click unattributable, so it reads as nobody (and every
    check refuses it): an undocumented body is never guessed in a user's favour.
    """
    _log_shape(event)
    channel = _id(event.get("channel")) or _id(event.get("channel_id"))
    users = {u for u in (_id(event.get("user")), _id(event.get("user_id"))) if u}
    user = users.pop() if len(users) == 1 else ""
    key = ""
    for name in ("action_key", "key", "item_key", "action_id"):
        key = _id(event.get(name))
        if key:
            break
    if not key:
        for name in ("action", "item", "context_bar_item"):
            nested = event.get(name)
            if isinstance(nested, dict):
                key = _id(nested.get("key")) or _id(nested.get("action_key"))
                if key:
                    break
    team = _id(payload.get("team_id")) or _id(event.get("team_id")) or _id(event.get("team"))
    dedupe = (
        _id(payload.get("event_id")) or _id(event.get("event_ts")) or _id(event.get("action_ts"))
    )
    return Click(channel=channel, user=user, key=key, team=team, dedupe=dedupe)


_seen: Any = None


def _first_delivery(click: Click, scope: str = EVENT) -> bool:
    """Whether *click* is new: one de-duplication cache for every channel button,
    keyed by *scope* and the click's own delivery id."""
    if not click.dedupe:
        return True
    global _seen
    if _seen is None:
        from kiro_crew.slack.events import SeenCache

        _seen = SeenCache()
    return not _seen.check_and_add(f"{scope}:{click.channel}:{click.dedupe}")


def _responder(channel: str, user: str) -> Callable[[str], Awaitable[None]]:
    async def _respond(text: str) -> None:
        slack = surface.slack()
        if slack is None or not (channel and user):
            return
        if not await surface.outbound_permitted() or surface.archived_now(channel):
            # The channels policy or an archive: no reply. Never the stop fence:
            # this answers a person's own click, not work admitted before a Stop.
            return
        try:
            await slack.post_ephemeral(channel, user, text)
        except Exception:
            logger.debug("code channel action reply failed", exc_info=True)

    return _respond


_tasks: set[asyncio.Future[Any]] = set()


def dispatch(orch: Any, payload: dict[str, Any], event: dict[str, Any]) -> None:
    """Handle one ``code_channel_action`` off the socket listener.

    The click was acknowledged to Slack already, so Slack never retries it: the
    task is registered with the gateway's handler tasks (``orch._handler_tasks``,
    as the agent-session events are), so an automatic update's restart counts it
    and waits for it instead of losing the click.
    """
    task = asyncio.ensure_future(handle(orch, payload, event))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    handler_tasks = getattr(orch, "_handler_tasks", None)
    if isinstance(handler_tasks, set):
        handler_tasks.add(task)
        task.add_done_callback(handler_tasks.discard)


async def drain() -> None:
    """Wait for dispatched clicks (tests, shutdown)."""
    # Only the running tasks: a finished one leaves the set on a later loop pass,
    # and gathering only finished tasks never yields (3.12+), so this would spin.
    while pending := [task for task in _tasks if not task.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


async def handle(orch: Any, payload: dict[str, Any], event: dict[str, Any]) -> str:
    """Answer one click; returns what happened (for tests and logs). Never raises."""
    try:
        return await _handle(orch, payload, event)
    except Exception:
        logger.exception("code channel action failed")
        return "error"


async def _handle(orch: Any, payload: dict[str, Any], event: dict[str, Any]) -> str:
    click = parse(payload, event)
    if click.key == ACTION_STOP:
        # The same Stop as Slack's button: exactly the checks its Stop asks, none
        # in front of them (who may press, the feature or a running session, the
        # workspace, the channel's own rule) and none left out.
        return await _stop(orch, click)
    if not code_channels_on(live_config(orch)):
        return "off"
    resources = f"channel={click.channel} action={click.key[:64] or '-'}"
    if not _first_delivery(click):
        return "duplicate"
    respond = _responder(click.channel, click.user)
    binding = await admit(click, respond, resources)
    if binding is None:
        return "denied"
    # The gates above were read before the binding's read: asked again live, right
    # before the click acts (a person's click is not held back by Slack's Stop).
    refusal = await surface.act_refusal(click.channel, binding, team=click.team, stop=False)
    if refusal:
        surface.audit(click.user, OPERATION, "denied", resources, f"refused: {refusal}")
        if refusal == "channel_off":
            await respond(NOT_AUTHORIZED_TEXT)
        return "denied"
    if click.key == ACTION_CREATE_PR:
        from kiro_crew.slack.code_channels import commands

        # The command's own gates and audit row, exactly as /create-pr.
        await commands.run_command(
            orch,
            command=commands.CREATE_PR,
            channel=click.channel,
            user=click.user,
            team=click.team,
            text="",
            respond=respond,
            via="action",
        )
        return "create-pr"
    surface.audit(click.user, OPERATION, "denied", resources, "unknown action")
    await respond(UNKNOWN_ACTION_TEXT)
    return "unknown"


async def admit(
    click: Click,
    respond: Callable[[str], Awaitable[None]],
    resources: str,
    *,
    operation: str = OPERATION,
) -> Binding | None:
    """The gates every channel button passes before its own path; the binding, or None.

    One clicking user, the event's workspace admitted, the ``channels`` policy
    admitting inbound Slack, and a bound code channel. Each refusal is SEL-audited
    under *operation*; the last is also told to the person privately. Shared by the
    context bar's actions and the Workers tab's Stop (:mod:`.worker_stop`), so
    neither adds a rule of its own.
    """
    if not click.user:
        surface.audit("", operation, "denied", resources, "no single clicking user")
        return None
    if not surface.workspace_admitted(click.team):
        surface.audit(
            click.user,
            operation,
            "denied",
            f"{resources} team_id={click.team}",
            "enterprise_origin_mismatch",
        )
        return None
    from kiro_crew.messaging.identity import channel_inbound_permitted
    from kiro_crew.slack.channel_policy import channel_refusal

    channel = click.channel
    # The pair every Slack inbound gate makes (events.py, interactions.dispatch):
    # the channels ceiling and the per-channel one (allowed_channel_ids, audited
    # at the policy seam), both before any side effect.
    if not (
        await channel_inbound_permitted("slack")
        and not channel_refusal(channel, operation=operation, caller=click.user)
    ):
        surface.audit(click.user, operation, "denied", resources, "channels governance policy")
        return None
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None:
        surface.audit(click.user, operation, "denied", resources, "not a bound code channel")
        await respond(NOT_A_CODE_CHANNEL_TEXT)
        return None
    return binding


_RESOLVE: Any = object()


def _may_stop(orch: Any, binding: Binding, user: str, *, cfg: Any = _RESOLVE) -> bool:
    """The checks Slack's Stop runs for a code channel (``session_events.handle_stopped``
    then ``routing.stop_from_channel``), for the Workers tab's Stop (:mod:`.worker_stop`).

    *cfg* is the live config the caller already resolved (the gateway's one
    config object, updated in place), for a caller that must not resolve it here.
    """
    from kiro_crew.slack.agent_sessions.session_events import _may_use_bot
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit
    from kiro_crew.slack.handler import is_allowed_user

    # session_events.handle_stopped's own check, then the code channel's stop
    # handler's (routing.handle_session_stopped).
    if not _may_use_bot(user) or not is_allowed_user(user):
        return False
    origin = _origin_of(binding.channel_id, binding, None)
    live = live_config(orch) if cfg is _RESOLVE else cfg
    return channel_and_origin_permit(live, binding.channel_id, origin)


async def _stop(orch: Any, click: Click) -> str:
    """Stop the channel's session as Slack's own Stop does, through the same path.

    The click becomes the ``agent_session_stopped`` Slack's button sends, asked
    :func:`.session_events.admits` (what Slack's Stop event is asked on arrival)
    and handed to :func:`.session_events.handle_stopped` (who may press, then
    ``routing.stop_from_channel``). Only the private reply is the click's own.
    """
    from kiro_crew.slack.agent_sessions import session_events

    if not _first_delivery(click):
        return "duplicate"
    stop_event = {
        "type": session_events.EVENT_STOPPED,
        "channel": click.channel,
        "user": click.user,
        # Its own de-duplication key, always prefixed: a context-bar click is never
        # mistaken for (or swallowed as) a redelivered press of Slack's own Stop
        # button; both then stop the session the same way (routing._stop_bound).
        "event_ts": f"{EVENT}:{click.dedupe or time.monotonic_ns()}",
    }
    if not session_events.admits(orch, {"team_id": click.team}, stop_event):
        return "stop:not-admitted"
    was_running = _anything_running(click.channel)
    outcome = await session_events.handle_stopped(orch, stop_event)
    respond = _responder(click.channel, click.user)
    if outcome == "denied":
        await respond(NOT_AUTHORIZED_TEXT)
    elif outcome == "not_ours":
        await respond(NOT_A_CODE_CHANNEL_TEXT)
    elif outcome == "channel" and not was_running:
        await respond(NOTHING_RUNNING_TEXT)
    return f"stop:{outcome}"


def _anything_running(channel: str) -> bool:
    """Whether Stop has something to stop in *channel*, read with no await.

    The channel's session, one of its helper agents (``stop_helpers``' own reads),
    or a live or queued sub-agent of either.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.slack.code_channels import routing, stop_helpers

    if routing.channel_session_running(channel):
        return True
    binding = store().get_binding_if_loaded(channel)
    if binding is None:
        return False
    state = routing._dashboard_state()
    helpers = stop_helpers._live(state, stop_helpers._helper_keys(binding.session_key))
    children = getattr(getattr(state, "subagents", None), "has_live_or_queued_children", None)
    for slot in (routing.bound_slot(state, binding), *helpers):
        if slot is None:
            continue
        if getattr(slot, "running", False) is True:
            return True
        if callable(children) and children(effective_session_key(slot)) is True:
            return True
    return False
