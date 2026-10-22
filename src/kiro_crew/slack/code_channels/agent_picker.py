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
FAILED_TEXT = "⚠️ Could not switch to `{agent}`: {reason}"
CURRENT_TEXT = "This channel runs `{agent}`. Agents you can pick: {names}\nSwitch with `/{command} agent <name>`."
NOT_ALLOWED_TEXT = "⛔ You can't change this channel's agent."

#: The code from ``switch_slot_agent`` that means "a turn holds the session right
#: now": the pick is refused with :data:`BUSY_TEXT`, for the person to retry.
TURN_IN_FLIGHT = "turn_in_flight"


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


async def _switch(state: Any, slot: Any, agent: str) -> tuple[bool, dict[str, Any]]:
    """The dashboard's agent-switch transaction, as a permitted (non-owner) user."""
    from kiro_crew.dashboard.chat_handlers import (
        SlotAgentSwitchCaller,
        agent_switch_outcome,
        switch_slot_agent,
    )

    # The slot's own app scope (what its own turn could do), never wider.
    caller = SlotAgentSwitchCaller(
        app=str(getattr(slot, "_app", "") or ""), remote=AUDIT_CALLER, owner=False
    )
    resp = await switch_slot_agent(state, slot, slot.key, agent, "template", caller)
    return agent_switch_outcome(resp)


def _announce(state: Any, slot: Any, agent: str, warning: str) -> None:
    """The dashboard transcript line and live chip update a switch gets."""
    from kiro_crew.agent_switch_command import switch_announcement

    try:
        append = getattr(slot, "append", None)
        if callable(append):
            append("assistant", switch_announcement(agent, warning), "msg msg-a")
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


async def switch_channel_agent(
    binding: Binding, agent: str, user: str, *, via: str
) -> SwitchResult:
    """Move *binding*'s session to *agent* for *user*.

    The caller has already authorized *user* (see :func:`authorize`). Refuses an
    agent the picker would not list, and a pick while a turn runs.
    """
    channel = binding.channel_id
    resources = f"channel={channel} user={user} agent={agent} via={via}"
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
            "unknown_agent", UNKNOWN_AGENT_TEXT.format(agent=agent, names=_names_line(names))
        )
    if str(getattr(slot, "agent", "") or "") == agent:
        _audit("allowed", f"{resources} result=unchanged")
        return SwitchResult("unchanged", SAME_AGENT_TEXT.format(agent=agent))
    ok, body = await _switch(state, slot, agent)
    if ok:
        _audit("allowed", f"{resources} result=switched")
        _announce(state, slot, agent, str(body.get("warning") or ""))
        await refresh_kickoff(binding, note=SWITCHED_NOTE.format(agent=agent, user=user))
        return SwitchResult("switched", SWITCHED_TEXT.format(agent=agent))
    code = str(body.get("code") or "")
    reason = str(body.get("error") or "the switch was refused")
    _audit("error", f"{resources} result=refused code={code}", reason)
    await refresh_kickoff(binding)
    if code == TURN_IN_FLIGHT:
        return SwitchResult("busy", BUSY_TEXT.format(agent=agent))
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
        f"channel={channel} user={user} agent={agent} via=select message={msg_ts}",
        reason,
    )


async def handle_select(orch: Any, payload: dict[str, Any]) -> bool:
    """A pick in the kickoff's agent select; False when it is not ours (gate shut)."""
    if not code_channels_on(live_config(orch)):
        return False
    channel, user, msg_ts, team, agent = _pick_ids(payload)
    resources = f"channel={channel} user={user} agent={agent} via=select"
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or not msg_ts or msg_ts != binding.anchor_ts:
        _audit("denied", f"{resources} message={msg_ts}", "not a code channel kickoff")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
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
    outcome = await switch_channel_agent(binding, agent, user, via="select")
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
) -> None:
    """``/kirocrew agent [<name>]`` in an authorized code channel."""
    command = str(getattr(orch, "slack_command", "") or "kirocrew")
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
    await respond((await switch_channel_agent(binding, name, user, via="command")).text)
