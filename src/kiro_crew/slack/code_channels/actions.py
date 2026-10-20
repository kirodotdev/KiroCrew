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

Before any of them the click passes the gates every inbound Slack path applies:
the feature is on, the event's workspace is admitted, the ``channels`` policy
admits inbound Slack, and the channel is a bound code channel. A person a path
refuses is told so privately (an ephemeral message); every decision is
SEL-audited. A redelivered click (same event id) does nothing.

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


_shape_logged = [False]


def _log_shape(event: dict[str, Any]) -> None:
    """Log the first click's field NAMES (never values), to pin the body once seen."""
    if not _shape_logged[0]:
        _shape_logged[0] = True
        logger.info("code_channel_action body keys: %s", sorted(str(k) for k in event))


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


def _first_delivery(click: Click) -> bool:
    if not click.dedupe:
        return True
    global _seen
    if _seen is None:
        from kiro_crew.slack.events import SeenCache

        _seen = SeenCache()
    return not _seen.check_and_add(f"{EVENT}:{click.channel}:{click.dedupe}")


def _responder(channel: str, user: str) -> Callable[[str], Awaitable[None]]:
    async def _respond(text: str) -> None:
        slack = surface.slack()
        if slack is None or not (channel and user):
            return
        try:
            await slack.post_ephemeral(channel, user, text)
        except Exception:
            logger.debug("code channel action reply failed", exc_info=True)

    return _respond


_tasks: set[asyncio.Future[Any]] = set()


def dispatch(orch: Any, payload: dict[str, Any], event: dict[str, Any]) -> None:
    """Handle one ``code_channel_action`` off the socket listener."""
    task = asyncio.ensure_future(handle(orch, payload, event))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for dispatched clicks (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def handle(orch: Any, payload: dict[str, Any], event: dict[str, Any]) -> str:
    """Answer one click; returns what happened (for tests and logs). Never raises."""
    try:
        return await _handle(orch, payload, event)
    except Exception:
        logger.exception("code channel action failed")
        return "error"


async def _handle(orch: Any, payload: dict[str, Any], event: dict[str, Any]) -> str:
    if not code_channels_on(live_config(orch)):
        return "off"
    click = parse(payload, event)
    resources = f"channel={click.channel} action={click.key[:64] or '-'}"
    if not _first_delivery(click):
        return "duplicate"
    if not click.user:
        surface.audit("", OPERATION, "denied", resources, "no single clicking user")
        return "denied"
    if not surface.workspace_admitted(click.team):
        surface.audit(
            click.user,
            OPERATION,
            "denied",
            f"{resources} team_id={click.team}",
            "enterprise_origin_mismatch",
        )
        return "denied"
    from kiro_crew.messaging.identity import channel_inbound_permitted

    if not await channel_inbound_permitted("slack"):
        surface.audit(click.user, OPERATION, "denied", resources, "channels governance policy")
        return "denied"
    respond = _responder(click.channel, click.user)
    binding = await asyncio.to_thread(store().get_binding, click.channel) if click.channel else None
    if binding is None:
        surface.audit(click.user, OPERATION, "denied", resources, "not a bound code channel")
        await respond(NOT_A_CODE_CHANNEL_TEXT)
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
    if click.key == ACTION_STOP:
        return await _stop(orch, binding, click, respond)
    surface.audit(click.user, OPERATION, "denied", resources, "unknown action")
    await respond(UNKNOWN_ACTION_TEXT)
    return "unknown"


def _may_stop(orch: Any, binding: Binding, user: str) -> bool:
    """The checks Slack's Stop runs for a code channel, asked first only to answer
    a refused person privately: the stop itself checks them again."""
    from kiro_crew.slack.agent_sessions.session_events import _may_use_bot
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit
    from kiro_crew.slack.handler import is_allowed_user

    # session_events.handle_stopped's own check, then the code channel's stop
    # handler's (routing.handle_session_stopped).
    if not _may_use_bot(user) or not is_allowed_user(user):
        return False
    origin = _origin_of(binding.channel_id, binding, None)
    return channel_and_origin_permit(live_config(orch), binding.channel_id, origin)


def _running(binding: Binding) -> bool:
    from kiro_crew.slack.code_channels.binding import bound_slot

    slot = bound_slot(surface.dashboard_state(), binding)
    return bool(slot is not None and getattr(slot, "running", False))


async def _stop(
    orch: Any, binding: Binding, click: Click, respond: Callable[[str], Awaitable[None]]
) -> str:
    from kiro_crew.slack.agent_sessions import session_events

    if not _may_stop(orch, binding, click.user):
        surface.audit(
            click.user,
            OPERATION,
            "denied",
            f"channel={click.channel} action=stop",
            "unauthorized sender",
        )
        await respond(NOT_AUTHORIZED_TEXT)
        return "denied"
    was_running = _running(binding)
    stop_event = {
        "type": session_events.EVENT_STOPPED,
        "channel": click.channel,
        "user": click.user,
        # Its own de-duplication key, always prefixed: a context-bar click is never
        # mistaken for (or swallowed as) a press of Slack's own Stop button, which
        # leaves the status to Slack (routing.handle_session_stopped).
        "event_ts": f"{EVENT}:{click.dedupe or time.monotonic_ns()}",
    }
    outcome = await session_events.handle_stopped(orch, stop_event)
    if not was_running and outcome == "channel":
        await respond(NOTHING_RUNNING_TEXT)
    return f"stop:{outcome}"
