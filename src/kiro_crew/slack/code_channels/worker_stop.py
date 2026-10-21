"""The Workers tab's Stop button: stop ONE worker's turn from the code channel.

The Workers tab (:mod:`.tabs`) is a ``block_kit`` view; a click on one of its
buttons reaches the app as a ``block_actions`` payload whose ``container`` names
the view (`agents.conversations.setView`_). Nothing in the payload is trusted to
pick what is stopped:

1. **The gates every channel button passes** -- the feature on, one clicking
   user, the workspace admitted, the ``channels`` policy, a bound code channel --
   are :func:`.actions.admit`'s, shared with the context bar's own Stop.
2. **The view is ours.** The container's view id must equal the Workers tab this
   gateway recorded for THAT channel (:mod:`.views`); a click from any other view
   -- another channel's tab, an agent's own block_kit view, a forged payload -- is
   refused.
3. **The person may stop here**, by exactly the rule the context bar's Stop
   applies (:func:`.actions._may_stop`: the bot's user check, an allowed user,
   and the channel's origin still admitting the bot). No new rule.
4. **The target is resolved server-side.** The button's value is the row's
   ledger item id, an opaque handle. It is looked up on THIS channel's board, read
   fresh through the board's one scope-checked path (``board.source.read_board``:
   the channel conductor's own ledger, each worker binding back to that conductor
   and item). The worker session stopped is the one that row names; a session key
   in the payload is never read. The conductor's own session is never a target.

Then ``stop_slot_turn`` -- the dashboard Stop's own cancel, as ``session_stop``
uses it -- on that worker's slot only, without escalation, so a double click (or
a redelivery) never turns a cooperative stop into a kill and the conductor keeps
going. Every decision is SEL-audited like the channel's own Stop
(``slack.code_channel.stop``); every refusal is told to the person privately.

.. _agents.conversations.setView: https://docs.slack.dev/reference/methods/agents.conversations.setView
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from kiro_crew.slack.code_channels import actions, surface, views
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.board.render import WORKER_STOP_ACTION, _escape
from kiro_crew.slack.code_channels.surface_store import get_async

logger = logging.getLogger(__name__)

OPERATION = "slack.code_channel.stop"

NOT_THIS_TAB_TEXT = "That button isn't from this channel's Workers tab."
NOT_ON_BOARD_TEXT = "That worker isn't on this channel's board any more."
#: A ledger item id (``it_<hex>``): anything else is not one of ours.
_ITEM_ID = re.compile(r"^it_[0-9a-f]{8,32}$")
#: A second Stop for the same worker this soon is answered without stopping again.
_REPEAT_SECS = 5.0
_recent: dict[tuple[str, str], float] = {}
_MAX_TRACKED = 4096


def _id(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("id")
    return str(value or "") if isinstance(value, (str, int)) else ""


_shape_logged = [False]


def _log_shape(container: dict[str, Any]) -> None:
    """Log the first click's container field NAMES (never values), to pin the body."""
    if not _shape_logged[0]:
        _shape_logged[0] = True
        logger.info("workers tab container keys: %s", sorted(str(k) for k in container))


def parse(payload: dict[str, Any]) -> tuple[actions.Click, str, str]:
    """``(click, container view id, button value)`` from a ``block_actions`` payload."""
    raw_container = payload.get("container")
    container: dict[str, Any] = raw_container if isinstance(raw_container, dict) else {}
    _log_shape(container)
    action = (payload.get("actions") or [{}])[0]
    action = action if isinstance(action, dict) else {}
    channel = (
        _id(payload.get("channel"))
        or _id(container.get("channel_id"))
        or _id(container.get("channel"))
    )
    raw_user = payload.get("user")
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    team = _id(payload.get("team")) or _id(user.get("team_id"))
    view_id = _id(container.get("view_id")) or _id(container.get("tab_id"))
    dedupe = _id(action.get("action_ts")) or _id(payload.get("trigger_id"))
    click = actions.Click(
        channel=channel, user=_id(user), key="worker-stop", team=team, dedupe=dedupe
    )
    return click, view_id, _id(action.get("value"))


_seen: Any = None


def _first_delivery(click: actions.Click) -> bool:
    if not click.dedupe:
        return True
    global _seen
    if _seen is None:
        from kiro_crew.slack.events import SeenCache

        _seen = SeenCache()
    return not _seen.check_and_add(f"workers-tab-stop:{click.channel}:{click.dedupe}")


async def handle_stop_click(orch: Any, payload: dict[str, Any]) -> bool:
    """Answer one Workers-tab Stop click; False when code channels are off (not ours).

    Never raises.
    """
    try:
        if not code_channels_on(live_config(orch)):
            return False
        await _handle(orch, payload)
    except Exception:
        logger.exception("code channel workers-tab stop failed")
    return True


async def _handle(orch: Any, payload: dict[str, Any]) -> str:
    click, view_id, value = parse(payload)
    resources = f"channel={click.channel} action=worker-stop item={value[:40] or '-'}"
    if not _first_delivery(click):
        return "duplicate"
    respond = actions._responder(click.channel, click.user)
    binding = await actions.admit(click, respond, resources, operation=OPERATION)
    if binding is None:
        return "denied"
    ours = views.recorded(await get_async(click.channel), views.WORKERS_KEY)
    if ours is None or not view_id or view_id != ours.get("view_id"):
        surface.audit(click.user, OPERATION, "denied", resources, "not this channel's Workers tab")
        await respond(NOT_THIS_TAB_TEXT)
        return "denied"
    if not actions._may_stop(orch, binding, click.user):
        surface.audit(click.user, OPERATION, "denied", resources, "unauthorized sender")
        await respond(actions.NOT_AUTHORIZED_TEXT)
        return "denied"
    if not _ITEM_ID.match(value):
        surface.audit(click.user, OPERATION, "denied", resources, "not a work item")
        await respond(NOT_ON_BOARD_TEXT)
        return "denied"
    from kiro_crew.slack.code_channels.board import source

    state = surface.dashboard_state()
    if state is None:
        await respond(NOT_ON_BOARD_TEXT)
        return "denied"
    snapshot = await source.read_board(state, binding)
    row = next((r for r in (snapshot.rows if snapshot else ()) if r.item_id == value), None)
    worker = str(getattr(row, "session_key", "") or "")
    if row is None or not worker or worker == binding.session_key:
        surface.audit(click.user, OPERATION, "denied", resources, "not a worker on this board")
        await respond(NOT_ON_BOARD_TEXT)
        return "denied"
    title = _escape(str(getattr(row, "title", "") or "that worker"))
    slot = source.find_slot(state, worker)
    if slot is None or not getattr(slot, "running", False):
        surface.audit(click.user, OPERATION, "allowed", f"{resources} result=idle")
        await respond(f"*{title}* isn't running.")
        return "idle"
    mark = (click.channel, value)
    now = time.monotonic()
    if now - _recent.get(mark, -_REPEAT_SECS) < _REPEAT_SECS:
        await respond(f"Already stopping *{title}*.")
        return "repeat"
    if len(_recent) > _MAX_TRACKED:
        _recent.clear()
    _recent[mark] = now
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    # The dashboard Stop's own cancel on the WORKER's slot only, never escalated:
    # a second click is answered, not turned into a kill.
    result = await stop_slot_turn(state, slot, source="slack", escalate=False)
    info = str(result.get("info") or "stopping")
    surface.audit(click.user, OPERATION, "allowed", f"{resources} slot={slot.key} result={info}")
    await respond(f"Stopping *{title}*. The conductor keeps going.")
    from kiro_crew.slack.code_channels import board

    board.note_session_turn(str(slot.key))
    return f"stop:{info}"


def is_stop_action(action_id: str) -> bool:
    return action_id == WORKER_STOP_ACTION


def reset_for_tests() -> None:
    global _seen
    _seen = None
    _recent.clear()
