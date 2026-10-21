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
5. **The final gate.** That board read awaits, so everything above is decided
   again by :func:`_final_gate` -- synchronous, from in-memory state, with nothing
   between it and the stop -- and the session the cancel will address (the
   worker slot's running turn, ``turn_stop._cancel_target``) must resolve back to
   that worker's own slot, never the conductor's. That key is passed to the stop.

Then ``stop_slot_turn`` -- the dashboard Stop's own cancel, as ``session_stop``
uses it -- on that worker's slot only, without escalation, so a double click (or
a redelivery) never turns a cooperative stop into a kill and the conductor keeps
going. A stop the session declines (compacting, no active turn) is said as such.
Every decision is SEL-audited like the channel's own Stop
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

logger = logging.getLogger(__name__)

OPERATION = "slack.code_channel.stop"

NOT_THIS_TAB_TEXT = "That button isn't from this channel's Workers tab."
NOT_ON_BOARD_TEXT = "That worker isn't on this channel's board any more."
FAILED_TEXT = "That Stop could not be completed. Nothing was stopped by it; try again."
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
    # The container's own view id, nothing else: a payload without one is not a
    # click on a view of ours.
    view_id = _id(container.get("view_id"))
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

    Never raises: an error is audited against the presser and told to them.
    """
    if not code_channels_on(live_config(orch)):
        return False
    try:
        await _handle(orch, payload)
    except Exception:
        logger.exception("code channel workers-tab stop failed")
        click, _view, value = parse(payload)
        surface.audit(
            click.user,
            OPERATION,
            "error",
            f"channel={click.channel} action=worker-stop item={value[:40] or '-'}",
            "internal error",
        )
        await actions._responder(click.channel, click.user)(FAILED_TEXT)
    return True


async def _handle(orch: Any, payload: dict[str, Any]) -> str:
    click, view_id, value = parse(payload)
    resources = f"channel={click.channel} action=worker-stop item={value[:40] or '-'}"
    if not _first_delivery(click):
        return "duplicate"
    respond = actions._responder(click.channel, click.user)

    async def deny(why: str, text: str, outcome: str = "denied") -> str:
        surface.audit(click.user, OPERATION, outcome, resources, why)
        await respond(text)
        return "denied"

    binding = await actions.admit(click, respond, resources, operation=OPERATION)
    if binding is None:
        return "denied"
    # Asked first only to answer a refused person early; the final gate below asks
    # every one of these again with nothing in between it and the stop. The
    # surface store is loaded OFF the loop here, so both checks read memory only.
    from kiro_crew.slack.code_channels.surface_store import get_async

    await get_async(click.channel)
    if _workers_view(click.channel) != view_id or not view_id:
        return await deny("not this channel's Workers tab", NOT_THIS_TAB_TEXT)
    if not actions._may_stop(orch, binding, click.user):
        return await deny("unauthorized sender", actions.NOT_AUTHORIZED_TEXT)
    if not _ITEM_ID.match(value):
        return await deny("not a work item", NOT_ON_BOARD_TEXT)
    from kiro_crew.slack.code_channels.board import source

    state = surface.dashboard_state()
    if state is None:
        return await deny("no dashboard", NOT_ON_BOARD_TEXT)
    snapshot = await source.read_board(state, binding)
    row = next((r for r in (snapshot.rows if snapshot else ()) if r.item_id == value), None)
    worker = str(getattr(row, "session_key", "") or "")
    if row is None or not worker:
        return await deny("not a worker on this board", NOT_ON_BOARD_TEXT)
    title = _escape(str(getattr(row, "title", "") or "that worker"))
    mark = (click.channel, value)
    now = time.monotonic()
    if now - _recent.get(mark, -_REPEAT_SECS) < _REPEAT_SECS:
        surface.audit(click.user, OPERATION, "allowed", f"{resources} result=repeat")
        await respond(f"Already stopping *{title}*.")
        return "repeat"
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    # ── The final gate: SYNCHRONOUS, and nothing may suspend between it and the
    # stop (the rule session_control.stop_target follows). The board read above
    # awaited, so the binding, the Workers view, the presser's right to stop and
    # the target are all decided again here, from in-memory state.
    verdict = _final_gate(orch, state, binding, click, view_id, worker)
    if isinstance(verdict, str):
        if verdict == "idle":
            surface.audit(click.user, OPERATION, "allowed", f"{resources} result=idle")
            await respond(f"*{title}* isn't running.")
            return "idle"
        text = (
            actions.NOT_AUTHORIZED_TEXT
            if verdict == "unauthorized sender"
            else (
                NOT_THIS_TAB_TEXT
                if verdict == "not this channel's Workers tab"
                else NOT_ON_BOARD_TEXT
            )
        )
        return await deny(verdict, text)
    slot, cancel_key = verdict
    if len(_recent) > _MAX_TRACKED:
        _recent.clear()
    _recent[mark] = now
    # The dashboard Stop's own cancel on the WORKER's slot only, pinned to the
    # session the gate just checked, never escalated: a second click is
    # answered, not turned into a kill.
    result = await stop_slot_turn(
        state, slot, source="slack", escalate=False, cancel_key=cancel_key
    )
    info = str(result.get("info") or "stopping")
    surface.audit(click.user, OPERATION, "allowed", f"{resources} slot={slot.key} result={info}")
    if result.get("compacting"):
        _recent.pop(mark, None)
        await respond(f"*{title}* is compacting its context and can't stop yet. Try again shortly.")
        return "stop:compacting"
    if result.get("already_stopping"):
        from kiro_crew.slack.code_channels.board import stops

        stops.note(slot, click.user)
        await respond(f"Already stopping *{title}*.")
        return "stop:already"
    if info in {"no active turn", "not running"}:
        _recent.pop(mark, None)
        await respond(f"*{title}* isn't running.")
        return "stop:idle"
    await respond(f"Stopping *{title}*. The conductor keeps going.")
    from kiro_crew.slack.code_channels import board
    from kiro_crew.slack.code_channels.board import stops

    # The board and the Workers tab say who stopped it, from the next refresh
    # until the worker's next turn starts.
    stops.note(slot, click.user)
    board.note_session_turn(str(slot.key))
    return f"stop:{info}"


def _workers_view(channel: str) -> str:
    """The Workers tab's view id recorded for *channel*, from the LOADED snapshot.

    Never loads or locks (``SurfaceStore.loaded_get``): it runs on the event
    loop, inside the final gate. :func:`_handle` loads the store off the loop
    first (``get_async``); an unloaded store answers ``""``, which refuses.
    """
    from kiro_crew.slack.code_channels.surface_store import store as surface_store

    ours = views.recorded(surface_store().loaded_get(channel), views.WORKERS_KEY)
    return str(ours.get("view_id") or "") if ours else ""


def _final_gate(
    orch: Any, state: Any, admitted: Any, click: actions.Click, view_id: str, worker: str
) -> tuple[Any, str] | str:
    """``(worker slot, cancel key)`` the stop may land on, or why it may not.

    Synchronous and disk-free, so it sits directly before the stop: the channel's
    binding is still the one admitted (same session and anchor, not archived),
    the click's view is still its recorded Workers tab, the presser still passes
    the context-bar Stop's rule, and the session the cancel will address -- the
    worker slot's running turn, as ``stop_slot_turn`` would resolve it -- belongs
    to that worker's own slot and is not the conductor.
    """
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.chat_api.turn_stop import _cancel_target
    from kiro_crew.slack.code_channels.board import source
    from kiro_crew.slack.code_channels.store import StoreUnavailable, store

    channel = admitted.channel_id
    loaded = store().loaded_bindings()
    current = next((b for b in loaded or () if b.channel_id == channel), None)
    if (
        current is None
        or current.archived_at
        or channel in surface._archived_here
        or (current.session_key, current.anchor_ts) != (admitted.session_key, admitted.anchor_ts)
    ):
        return "the channel's binding changed"
    if not view_id or _workers_view(channel) != view_id:
        return "not this channel's Workers tab"
    if not actions._may_stop(orch, current, click.user):
        return "unauthorized sender"
    conductor = session_ledger.ledger_key(current.session_key)
    slot = source.find_slot(state, worker)
    if slot is None or session_ledger.ledger_key(str(slot.key)) == conductor:
        return "not a worker on this board"
    try:
        born_of = store().worker_conductor(str(slot.key).removeprefix("dashboard_"))
    except StoreUnavailable:
        born_of = ""
    if born_of and session_ledger.ledger_key(born_of) != conductor:
        return "not a worker on this board"
    if not getattr(slot, "running", False):
        return "idle"
    cancel_key = _cancel_target(slot)
    owner = surface.caller_slot_key(state, cancel_key)
    if not cancel_key or owner != slot.key or session_ledger.ledger_key(owner) == conductor:
        return "the cancel target is not that worker"
    return slot, cancel_key


def is_stop_action(action_id: str) -> bool:
    return action_id == WORKER_STOP_ACTION


def reset_for_tests() -> None:
    global _seen
    _seen = None
    _recent.clear()
