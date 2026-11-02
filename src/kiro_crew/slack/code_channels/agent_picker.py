"""A code channel's agent: the kickoff picker and ``/kirocrew agent <name>``.

Every code channel runs ONE dashboard session (:mod:`.binding`), started on the
resolved default agent: the explicit Slack Code default, else the default
folder's ``default_agent``, else ``kirocrew-conductor`` (the auto "Code channels"
folder's agent). The people in the channel can move that session to another
agent two ways:

* the kickoff message carries a Block Kit ``static_select`` listing the agents
  Settings -> Slack's "Default agent" picker lists (the same function,
  :func:`kiro_crew.dashboard.slack_code_settings._choosable_agent_names`: every
  agent a channel can run), preselected with the agent the session runs;
* ``/kirocrew agent <name>`` in the channel (bare ``/kirocrew agent`` lists them).

**One switch path.** Both doors call :func:`switch_channel_agent`, which runs the
dashboard's own agent-switch transaction
(:func:`kiro_crew.dashboard.chat_api.agent_switch.switch_slot_agent`, the one
behind ``POST /api/chat/slots/{slot}/agent`` and the in-turn ``/agent``): no
store of its own, and the caller is never treated as the dashboard owner, so a
Slack pick gets exactly the permitted-user rules the in-turn command gets. The
model and effort stay what the session was started with.

**A turn in flight.** The switch never tears down a running turn. A pick that
lands while one runs (or while sub-agents run) is refused with a "try again"
reply, and the kickoff is put back on the agent the session runs.

**Who may change it.** The owner (``is_allowed_user``) in a channel whose origin
still admits the bot, the gates every code-channel command applies. Anyone else
is told so ephemerally. Every decision -- refused, unknown agent, switched,
busy, failed -- writes one SEL row (caller ``slack-code-channels``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import bound_slot, code_channels_on, live_config
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)

#: The kickoff picker's action id (routed by ``slack/interactions.py``).
AGENT_SELECT_ACTION_ID = "kc_code_channel_agent"
#: Prefix of the picker block's id. Each render gets a fresh suffix, so Slack
#: re-draws the select from ``initial_option`` instead of keeping a refused pick.
AGENT_BLOCK_PREFIX = "kc_code_channel_agent."
#: Slack's ``static_select`` option cap.
MAX_OPTIONS = 100
#: Slack's ``plain_text`` cap for an option's label.
_OPTION_TEXT_MAX = 75
#: Slack's cap for an option's ``value``; a longer agent name is not offered
#: (one such option would make Slack refuse the whole message).
_OPTION_VALUE_MAX = 150

AUDIT_CALLER = "slack-code-channels"
OPERATION = "slack.code_channel.agent"

PICKER_LABEL = "*Agent* for this channel"
PICKER_PLACEHOLDER = "Choose an agent"
SWITCHED_NOTE = "Agent set to `{agent}` by <@{user}>."
UNKNOWN_AGENT_TEXT = "❌ No agent named `{agent}` is available here. Agents you can pick: {names}"
NO_AGENTS_TEXT = "I couldn't list the agents just now. Try again in a moment."
NO_SESSION_TEXT = "This channel's session isn't open right now. Try again in a moment."
SAME_AGENT_TEXT = "ℹ️ This channel already runs `{agent}`."
SWITCHED_TEXT = "✅ This channel now runs `{agent}`."
BUSY_TEXT = "⏳ A turn is running. Try again to switch to `{agent}` when this turn ends."
SUBAGENTS_BUSY_TEXT = (
    "⏳ Sub-agents are running. Try again to switch to `{agent}` when they finish."
)
FAILED_TEXT = "⚠️ Could not switch to `{agent}`: {reason}"
SWITCH_FAILED_REASON = "the switch failed"
STALE_PICKER_TEXT = (
    "This picker is out of date. Use the channel's latest kickoff message or "
    "`/{command} agent <name>`."
)
CURRENT_TEXT = "This channel runs `{agent}`. Agents you can pick: {names}\nSwitch with `/{command} agent <name>`."
NOT_ALLOWED_TEXT = "⛔ You can't change this channel's agent."

#: The code from ``switch_slot_agent`` that means "a turn holds the session right
#: now": the pick is refused with :data:`BUSY_TEXT`, for the person to retry.
TURN_IN_FLIGHT = "turn_in_flight"
#: The code for "sub-agents still hold the session": also a "try again".
SUBAGENTS_RUNNING = "slot_subagents_running"
#: The code for "a gate closed before the commit" (``still_permitted`` said no).
CALLER_NOT_PERMITTED = "caller_not_permitted"


def _audit(outcome: str, resources: str, error: str = "") -> None:
    surface.audit(AUDIT_CALLER, OPERATION, outcome, resources, error)


# ── The agents ────────────────────────────────────────────────────────────


async def agent_choices(project_dir: str | None) -> list[str] | None:
    """The agents a code channel may run -- the Settings -> Slack picker's list.

    ``None`` when they cannot be listed (the picker is then left off the kickoff
    and a switch is refused, rather than offering nothing or guessing).
    """
    from kiro_crew.dashboard.slack_code_settings import _agent_names

    try:
        return await _agent_names(project_dir)
    except Exception:
        logger.warning("code channel: agents could not be listed", exc_info=True)
        return None


def _names_line(names: list[str]) -> str:
    return ", ".join(f"`{n}`" for n in names) if names else "none"


# ── The kickoff message ───────────────────────────────────────────────────


def _option(name: str) -> dict[str, Any]:
    return {
        "text": {"type": "plain_text", "text": name[:_OPTION_TEXT_MAX]},
        "value": name,
    }


def kickoff_blocks(
    text: str, agents: list[str] | None, selected: str, note: str = ""
) -> list[dict[str, Any]]:
    """The kickoff message: its text, then the agent picker (when agents listed).

    *selected* is the agent the session runs; it is the select's initial option
    when it is one of *agents*. Past Slack's option cap the list is cut, keeping
    *selected* in it.
    """
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if not agents:
        return blocks
    names = [n for n in dict.fromkeys(agents) if 0 < len(n) <= _OPTION_VALUE_MAX]
    if not names:
        return blocks
    if len(names) > MAX_OPTIONS:
        head = names[:MAX_OPTIONS]
        if selected in names and selected not in head:
            head[-1] = selected
        names = head
    select: dict[str, Any] = {
        "type": "static_select",
        "action_id": AGENT_SELECT_ACTION_ID,
        "placeholder": {"type": "plain_text", "text": PICKER_PLACEHOLDER},
        "options": [_option(n) for n in names],
    }
    if selected in names:
        select["initial_option"] = _option(selected)
    blocks.append(
        {
            "type": "section",
            "block_id": f"{AGENT_BLOCK_PREFIX}{time.time_ns()}",
            "text": {"type": "mrkdwn", "text": PICKER_LABEL},
            "accessory": select,
        }
    )
    if note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return blocks


async def refresh_kickoff(binding: Binding, *, note: str = "") -> None:
    """Re-draw *binding*'s kickoff from the session's real agent (never raises)."""
    try:
        slack = surface.slack()
        state = surface.dashboard_state()
        slot = bound_slot(state, binding)
        if slack is None or slot is None or not binding.anchor_ts:
            return
        if not await surface.outbound_permitted():
            return
        from kiro_crew.slack.code_channels.routing import _kickoff_text

        text = _kickoff_text(binding.title, state, str(getattr(slot, "folder_id", "") or ""))
        agents = await agent_choices(getattr(slot, "project", None) or binding.project_dir or None)
        blocks = kickoff_blocks(text, agents, str(getattr(slot, "agent", "") or ""), note)
        await slack.update_message(binding.channel_id, binding.anchor_ts, text=text, blocks=blocks)
    except Exception:
        logger.info("code channel %s: kickoff not updated", binding.channel_id, exc_info=True)


# ── The switch ────────────────────────────────────────────────────────────


async def _switch(
    state: Any, slot: Any, agent: str, still_permitted: Callable[[], bool]
) -> tuple[bool, dict[str, Any]]:
    """The dashboard's agent-switch transaction, as a permitted (non-owner) user.

    *still_permitted* is checked at its commit, so a code-channel gate that
    closes during the transaction's own awaits refuses the switch.
    """
    from kiro_crew.dashboard.chat_handlers import (
        SlotAgentSwitchCaller,
        agent_switch_outcome,
        switch_slot_agent,
    )

    # The slot's own app scope (what its own turn could do), never wider.
    # notice=False: _announce writes this switch's one /agent line (as for_turn
    # and the HTTP announce route do), so the boundary row is not added twice.
    caller = SlotAgentSwitchCaller(
        app=str(getattr(slot, "_app", "") or ""),
        remote=AUDIT_CALLER,
        owner=False,
        notice=False,
        still_permitted=still_permitted,
    )
    resp = await switch_slot_agent(state, slot, slot.key, agent, "template", caller)
    return agent_switch_outcome(resp)


async def _load_window(slot: Any) -> bool:
    """Load a restored slot's pending transcript window with its reads off the loop.

    Every read of the slot's rows on this path (the switch transaction's own
    ``messages`` checks, :func:`_announce`'s append) runs on the gateway loop. A
    restored row that was never opened still has its window on disk, and this is
    not a per-slot route that loads it first, so it is loaded here, as the
    built-in notice that :func:`_switch` turns off would have. False when the load
    failed (the window stays pending).
    """
    from kiro_crew.dashboard.state import load_window_off_loop

    try:
        await load_window_off_loop(slot)
    except Exception:
        logger.warning("code channel: transcript window could not be loaded", exc_info=True)
        return False
    return True


async def _announce(state: Any, slot: Any, agent: str, warning: str) -> None:
    """The dashboard transcript line and live chip update a switch gets.

    The only switch row: :func:`_switch` turns the built-in notice off. It also
    marks a pick made before the first message, which that notice skips. The
    window is loaded off the loop first (a no-op when it already is); a failed
    load omits the row, never the committed switch.
    """
    from kiro_crew.agent_switch_command import switch_announcement
    from kiro_crew.security import redact

    if not await _load_window(slot):
        return
    try:
        append = getattr(slot, "append", None)
        if callable(append):
            append("assistant", redact(switch_announcement(agent, warning)), "msg msg-a")
        broadcast = getattr(state, "broadcast_ws", None)
        if callable(broadcast):
            broadcast("slot_agent_switch", {"slot": slot.key, "agent": str(slot.agent)})
        surface.push_slots(state)
    except Exception:
        logger.debug("code channel agent switch announcement failed", exc_info=True)


@dataclass(frozen=True)
class SwitchResult:
    """What :func:`switch_channel_agent` did, and the reply for the person.

    ``result`` is ``switched``, ``unchanged``, ``busy`` (a turn is running),
    ``unknown_agent``, ``unavailable`` or ``refused``. The kickoff already shows
    the first two, so the select answers only the others.
    """

    result: str
    text: str

    @property
    def shown_on_kickoff(self) -> bool:
        return self.result in ("switched", "unchanged")


#: What the audit row and reply show for a typed name that can't be an agent name.
INVALID_NAME = "<invalid>"
#: The re-check's reason when the channel was rebound to a new session meanwhile.
REBOUND = "session rebound"


def shown_name(agent: str) -> str:
    """*agent* as an audit row or reply may show it: the name, or a placeholder.

    A typed or selected value is free text. Only a string the agent-name
    grammar admits (:func:`~kiro_crew.validation.is_registered_agent_name`: no
    spaces, backticks, ``=`` or markup) is written into a SEL ``key=value`` row
    or echoed back, so it can't forge ``result=`` tokens or Slack markup.
    """
    from kiro_crew.validation import is_registered_agent_name

    return agent if is_registered_agent_name(agent) else INVALID_NAME


#: The re-check's reason when a gate closed after :func:`authorize`.
NO_LONGER_ADMITTED = "the code channel no longer admits the bot"


def _sync_gates(orch: Any, binding: Binding, user: str, team: str) -> bool:
    """The synchronous code-channel gates: workspace, owner, archive fence,
    feature, the channel and its origin. No await, no disk read, so it can run
    inside the switch transaction's commit section (``still_permitted``)."""
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit
    from kiro_crew.slack.handler import is_allowed_user

    cfg = live_config(orch)
    return bool(
        surface.workspace_admitted(team)
        and is_allowed_user(user)
        and binding.channel_id not in surface._archived_here
        and code_channels_on(cfg)
        and channel_and_origin_permit(
            cfg, binding.channel_id, _origin_of(binding.channel_id, binding, None)
        )
    )


async def _still_admitted(orch: Any, binding: Binding, user: str, team: str) -> str | None:
    """The gates again, after the pre-commit awaits; the refusal reason, or None.

    :func:`authorize` ran before the awaits in :func:`_prepare_switch` (agent
    discovery, the off-loop window load); meanwhile the operator may have
    removed the owner, turned the channel, its origin or the feature off,
    denied Slack, archived the channel, or rebound it. The two checks that need
    an await (the stored binding, then the Slack policy) run here last; the
    synchronous ones run here and AGAIN at the switch's commit
    (:func:`_sync_gates` as ``still_permitted``), past every await of its own.
    """
    from kiro_crew.messaging.identity import channel_inbound_permitted

    current = await asyncio.to_thread(store().get_binding, binding.channel_id)
    if current is None or current.session_key != binding.session_key:
        return REBOUND
    if current.archived_at or not await channel_inbound_permitted("slack"):
        return NO_LONGER_ADMITTED
    return None if _sync_gates(orch, binding, user, team) else NO_LONGER_ADMITTED


async def _prepare_switch(
    binding: Binding,
    agent: str,
    shown: str,
    resources: str,
    *,
    user: str,
    team: str,
    orch: Any,
) -> SwitchResult | tuple[Any, Any]:
    """Everything before the commit: an answer, or ``(state, slot)`` to switch."""
    state = surface.dashboard_state()
    slot = bound_slot(state, binding)
    if slot is None:
        _audit("error", resources, "session unavailable")
        return SwitchResult("unavailable", NO_SESSION_TEXT)
    names = await agent_choices(getattr(slot, "project", None) or binding.project_dir or None)
    if names is None:
        _audit("error", resources, "agents could not be listed")
        return SwitchResult("unavailable", NO_AGENTS_TEXT)
    if agent not in names:
        _audit("denied", resources, "unknown agent")
        return SwitchResult(
            "unknown_agent", UNKNOWN_AGENT_TEXT.format(agent=shown, names=_names_line(names))
        )
    if str(getattr(slot, "agent", "") or "") == agent:
        _audit("allowed", f"{resources} result=unchanged")
        return SwitchResult("unchanged", SAME_AGENT_TEXT.format(agent=agent))
    # The switch transaction reads the slot's rows on the loop: load a restored
    # slot's window off it first. Nothing is committed yet, so a failed load is
    # a plain "try again" (the select redraws for "unavailable").
    if not await _load_window(slot):
        _audit("error", resources, "transcript window could not be loaded")
        return SwitchResult("unavailable", NO_SESSION_TEXT)
    refused = await _still_admitted(orch, binding, user, team)
    if refused == REBOUND:
        # The captured slot is stale; the new kickoff already shows its agent.
        _audit("denied", resources, REBOUND)
        return SwitchResult("refused", STALE_PICKER_TEXT.format(command=_command_name(orch)))
    if refused is not None:
        _audit("denied", resources, refused)
        await refresh_kickoff(binding)
        return SwitchResult("refused", NOT_ALLOWED_TEXT)
    return state, slot


async def switch_channel_agent(
    binding: Binding, agent: str, user: str, *, via: str, team: str, orch: Any
) -> SwitchResult:
    """Move *binding*'s session to *agent* for *user*.

    The caller has already authorized *user* (see :func:`authorize`). Refuses an
    agent the picker would not list, and a pick while a turn runs.
    """
    channel = binding.channel_id
    shown = shown_name(agent)
    resources = f"channel={channel} user={user} agent={shown} via={via}"
    try:
        prepared = await _prepare_switch(
            binding, agent, shown, resources, user=user, team=team, orch=orch
        )
    except asyncio.CancelledError:
        # Nothing committed: put the select back before unwinding.
        await asyncio.shield(refresh_kickoff(binding))
        raise
    if isinstance(prepared, SwitchResult):
        return prepared
    state, slot = prepared
    try:
        # The synchronous gates run again inside the transaction, at its commit.
        ok, body = await _switch(state, slot, agent, lambda: _sync_gates(orch, binding, user, team))
    except asyncio.CancelledError:
        # Put the select back on the agent the session runs before unwinding.
        await asyncio.shield(refresh_kickoff(binding))
        raise
    except Exception:
        # switch_slot_agent rolls its own commit back and re-raises (e.g. a
        # failed session reset): answer, audit and redraw instead of leaving
        # the select on a pick that did not happen.
        logger.warning("code channel %s: agent switch raised", channel, exc_info=True)
        _audit("error", f"{resources} result=failed", "switch raised")
        await refresh_kickoff(binding)
        return SwitchResult("refused", FAILED_TEXT.format(agent=agent, reason=SWITCH_FAILED_REASON))
    if ok:
        _audit("allowed", f"{resources} result=switched")
        await _announce(state, slot, agent, str(body.get("warning") or ""))
        await refresh_kickoff(binding, note=SWITCHED_NOTE.format(agent=agent, user=user))
        return SwitchResult("switched", SWITCHED_TEXT.format(agent=agent))
    from kiro_crew.security import redact

    code = str(body.get("code") or "")
    if code == CALLER_NOT_PERMITTED:
        # A gate closed during the transaction's own awaits (_sync_gates).
        _audit("denied", f"{resources} result=refused code={code}", NO_LONGER_ADMITTED)
        await refresh_kickoff(binding)
        return SwitchResult("refused", NOT_ALLOWED_TEXT)
    # The dashboard's error text can carry raw exception text: redact it before
    # it leaves the host for Slack (as _announce does).
    reason = redact(str(body.get("error") or "the switch was refused"))
    _audit("error", f"{resources} result=refused code={code}", reason)
    await refresh_kickoff(binding)
    if code == TURN_IN_FLIGHT:
        return SwitchResult("busy", BUSY_TEXT.format(agent=agent))
    if code == SUBAGENTS_RUNNING:
        return SwitchResult("busy", SUBAGENTS_BUSY_TEXT.format(agent=agent))
    return SwitchResult("refused", FAILED_TEXT.format(agent=agent, reason=reason))


# ── Authorization (shared by the select and the command) ───────────────────


async def authorize(
    orch: Any, binding: Binding, user: str, team: str, resources: str
) -> str | None:
    """None when *user* may change *binding*'s agent; else the refusal to show.

    The gates every code-channel command applies, in its order, each refusal
    SEL-audited: the workspace, the owner, the code channel and its origin both
    still admitting the bot, an archived channel.
    """
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit
    from kiro_crew.slack.handler import is_allowed_user

    if not surface.workspace_admitted(team):
        _audit("denied", f"{resources} team_id={team}", "enterprise_origin_mismatch")
        return ""
    if not is_allowed_user(user):
        _audit("denied", resources, "unauthorized user")
        return NOT_ALLOWED_TEXT
    origin = _origin_of(binding.channel_id, binding, None)
    if not channel_and_origin_permit(live_config(orch), binding.channel_id, origin):
        _audit(
            "denied",
            f"{resources} origin={origin}",
            "the code channel or its origin does not admit the bot",
        )
        return NOT_ALLOWED_TEXT
    if await surface.is_archived(binding):
        _audit("denied", resources, "archived")
        from kiro_crew.slack.code_channels.commands import ARCHIVED_TEXT

        return ARCHIVED_TEXT
    return None


async def _ephemeral(channel: str, user: str, text: str) -> None:
    slack = surface.slack()
    if slack is None or not (channel and user and text):
        return
    try:
        await slack.post_ephemeral(channel, user, text)
    except Exception:
        logger.debug("code channel ephemeral failed", exc_info=True)


def _pick_ids(payload: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """``(channel, user, message ts, team, agent)`` of a picker ``block_actions``."""
    channel = str((payload.get("channel") or {}).get("id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    msg_ts = str((payload.get("message") or {}).get("ts") or "")
    team = str(
        (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    )
    action = (payload.get("actions") or [{}])[0] or {}
    agent = str(((action.get("selected_option") or {}).get("value")) or "").strip()
    return channel, user, msg_ts, team, agent


def audit_refused_pick(payload: dict[str, Any], reason: str) -> None:
    """The picker's audit row for a pick refused before it reached this module."""
    channel, user, msg_ts, _team, agent = _pick_ids(payload)
    _audit(
        "denied",
        f"channel={channel} user={user} agent={shown_name(agent)} via=select message={msg_ts}",
        reason,
    )


def _command_name(orch: Any) -> str:
    return str(getattr(orch, "slack_command", "") or "kirocrew")


async def handle_select(orch: Any, payload: dict[str, Any]) -> bool:
    """A pick in the kickoff's agent select; False when it is not ours (gate shut)."""
    if not code_channels_on(live_config(orch)):
        return False
    channel, user, msg_ts, team, agent = _pick_ids(payload)
    resources = f"channel={channel} user={user} agent={shown_name(agent)} via=select"
    # The workspace first, before any side effect (as wrapup's archive click).
    if not surface.workspace_admitted(team):
        _audit("denied", f"{resources} team_id={team}", "enterprise_origin_mismatch")
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or not msg_ts:
        _audit("denied", f"{resources} message={msg_ts}", "not a code channel kickoff")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    if msg_ts != binding.anchor_ts:
        # A kickoff a rebind replaced: point at the live one.
        _audit("denied", f"{resources} message={msg_ts}", "stale kickoff")
        await _ephemeral(channel, user, STALE_PICKER_TEXT.format(command=_command_name(orch)))
        return True
    refusal = await authorize(orch, binding, user, team, resources)
    if refusal is not None:
        await _ephemeral(channel, user, refusal)
        if refusal:
            # Put the select back on the agent the session really runs.
            await refresh_kickoff(binding)
        return True
    if not agent:
        _audit("denied", resources, "no agent chosen")
        return True
    outcome = await switch_channel_agent(binding, agent, user, via="select", team=team, orch=orch)
    if not outcome.shown_on_kickoff:
        # The kickoff shows a switch; anything else is said here.
        await _ephemeral(channel, user, outcome.text)
        if outcome.result in ("unknown_agent", "unavailable"):
            await refresh_kickoff(binding)
    return True


async def handle_command(
    orch: Any,
    binding: Binding,
    user: str,
    arg: str,
    respond: Callable[[str], Awaitable[None]],
    *,
    team: str,
) -> None:
    """``/kirocrew agent [<name>]`` in an authorized code channel."""
    command = _command_name(orch)
    name = arg.strip()
    if not name:
        slot = bound_slot(surface.dashboard_state(), binding)
        names = await agent_choices(
            (getattr(slot, "project", None) if slot is not None else None)
            or binding.project_dir
            or None
        )
        _audit("allowed", f"channel={binding.channel_id} user={user} via=command result=listed")
        if names is None:
            await respond(NO_AGENTS_TEXT)
            return
        current = str(getattr(slot, "agent", "") or "") if slot is not None else ""
        await respond(
            CURRENT_TEXT.format(
                agent=current or "the default agent", names=_names_line(names), command=command
            )
        )
        return
    outcome = await switch_channel_agent(binding, name, user, via="command", team=team, orch=orch)
    await respond(outcome.text)
