"""The code channel's "hand-off is off" notice and its Turn-on button.

``slack.code_channel_dispatch`` is an opt-in (default off). When a code channel's
own session asks to hand off work while it is off, the dispatch exemption refuses
(``DISPATCH_OFF_REFUSAL``, which names the Settings control) and calls
:func:`notice_soon`, which posts ONE short notice in that channel with a
**Turn on hand-off for code channels** button.

**Once per channel per refusal state.** A channel that has been shown the notice is
not shown it again while the switch stays off; the exemption calls :func:`forget`
the next time it finds the switch on, so a later switch-off gets a fresh notice.
The record is in memory: a gateway restart may post one more.

**The button** (:func:`handle_handoff_click`) is the owner's alone -- anyone else
gets an ephemeral refusal -- and asks for one confirm (Slack's own ``confirm``
dialog). Only the click on the CURRENT notice this process posted counts, and only
once: the notice's ts is registered when it is posted, and a button any other
message wears -- an agent's copy (agents may not post or edit in the reserved
``kc_code_channel_`` action ids at all), an older notice, a replay -- is refused as
stale. It writes the setting through ``save_code_channel_dispatch``, the same
locked, validated, SEL-audited ``config.json`` write the Settings card's save
performs (no new store; the switch's default stays off), then edits the notice to
:data:`DONE_TEXT`. It never re-runs the refused tool call: the conductor's next
turn (the owner asking again, or saying "retry") is what dispatches. Every
decision is SEL-audited with the channel id.

The notice is posted only where an ordinary message to the channel may go
(workspace, channel and origin admission, channels governance). Its text is fixed
and carries nothing from the session, so it publishes no dispatch data.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.store import store

logger = logging.getLogger(__name__)

HANDOFF_ACTION_ID = "kc_code_channel_enable_handoff"
OPERATION = "slack.code_channel.handoff"

NOTICE_TEXT = (
    "This channel's conductor tried to start helper agents for this work, but hand-off "
    "is turned off for code channels."
)
BUTTON_TEXT = "Turn on hand-off for code channels"
CONFIRM_TITLE = "Turn on hand-off?"
CONFIRM_TEXT = (
    "Every code channel's conductor will be able to start helper agents for its task "
    "when you message it. You can turn it off again in Settings → Channels → Slack → "
    'Code channels → "Let the conductor hand off work".'
)
DONE_TEXT = "Hand-off is on. Send your request again."
NOT_OWNER_TEXT = ":no_entry: Only {owner} can turn on hand-off."
NOT_ALLOWED_TEXT = ":no_entry: You can't change hand-off from this channel."
FAILED_TEXT = "I couldn't turn on hand-off: {reason}"
PINNED_TEXT = (
    "Hand-off for code channels is set in `{path}`, so it can't be turned on from "
    "Slack. Change it in that file, then ask the conductor again."
)
STALE_TEXT = (
    "That notice is out of date, so it can't change the setting. Ask the conductor "
    "again, or use Settings → Channels → Slack → Code channels."
)

#: Channel id -> the CURRENT notice's ts: a ``pending:<n>`` token while it is being posted, _UNKNOWN when
#: Slack's answer was lost, _USED once its button has been pressed. Only a click on
#: the registered ts counts, once -- a button any other message carries (an agent's
#: copy, an older notice, a replay) is refused as stale.
_noticed: dict[str, str] = {}
_UNKNOWN = "?"
_USED = "!"
#: A reservation while a post is in flight: one token per attempt, so an attempt
#: superseded by a forget and a newer refusal can only release or fill its own.
_PENDING = "pending:"
_attempts = itertools.count(1)
_locks: dict[str, asyncio.Lock] = {}


def _lock(channel_id: str) -> asyncio.Lock:
    lock = _locks.get(channel_id)
    if lock is None:
        lock = _locks[channel_id] = asyncio.Lock()
    return lock


def notice_blocks(channel_id: str) -> list[dict[str, Any]]:
    """The notice: one line and the owner's button (with Slack's confirm)."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": NOTICE_TEXT}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": HANDOFF_ACTION_ID,
                    "value": channel_id,
                    "text": {"type": "plain_text", "text": BUTTON_TEXT},
                    "confirm": {
                        "title": {"type": "plain_text", "text": CONFIRM_TITLE},
                        "text": {"type": "plain_text", "text": CONFIRM_TEXT},
                        "confirm": {"type": "plain_text", "text": "Turn on"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                }
            ],
        },
    ]


def done_blocks() -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":white_check_mark: {DONE_TEXT}"}}
    ]


def pinned_blocks(text: str) -> list[dict[str, Any]]:
    """The notice once a click found ``config.local.json`` pinning the switch: no button."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": NOTICE_TEXT}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]},
    ]


async def _overlay_pin_path() -> str:
    """``config.local.json``'s path (home as ``~``) when it pins the switch, else ``""``."""
    try:
        from kiro_crew.dashboard.slack_code_settings import (
            _dispatch_overlay_state,
            _overlay_display_path,
        )

        if await asyncio.to_thread(_dispatch_overlay_state) != "pinned":
            return ""
        return _overlay_display_path()
    except Exception:
        return ""


def forget(channel_id: str) -> None:
    """The switch is on again: the next switch-off may notice *channel_id* afresh."""
    _noticed.pop(channel_id, None)


def clear() -> None:
    """Forget every notice (tests)."""
    _noticed.clear()


def notice_soon(channel_id: str) -> bool:
    """Post the notice in *channel_id* once per refusal state; True when one started.

    Called from the exemption's synchronous judgement, so the post is a task on
    the running loop. Nothing starts without a loop.
    """
    if not channel_id or channel_id in _noticed:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    token = f"{_PENDING}{next(_attempts)}"
    _noticed[channel_id] = token
    try:
        loop.create_task(_post(channel_id, token))
    except Exception:  # pragma: no cover - no task: let the next refusal try
        _release(channel_id, token)
        return False
    return True


def _release(channel_id: str, token: str) -> None:
    """Nothing was posted: let the next refusal try again (only THIS attempt's hold)."""
    if _noticed.get(channel_id) == token:
        _noticed.pop(channel_id, None)


async def _post(channel_id: str, token: str) -> None:
    resources = f"channel={channel_id}"
    slack = surface.slack()
    if slack is None:
        _release(channel_id, token)
        return
    if not await surface.outbound_permitted():
        surface.audit(
            "system", f"{OPERATION}.notice", "denied", resources, "channels governance policy"
        )
        _release(channel_id, token)
        return
    try:
        binding = await asyncio.to_thread(store().get_binding, channel_id)
    except Exception:
        binding = None
    if binding is None or await surface.is_archived(binding):
        surface.audit(
            "system", f"{OPERATION}.notice", "denied", resources, "not a live code channel"
        )
        _release(channel_id, token)
        return
    # Posted only where an ordinary message to this channel may go: its workspace
    # admitted, and the channel and its origin conversation admitting the bot --
    # the gates the click handler (and the message path) apply.
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit

    origin = _origin_of(channel_id, binding, None)
    if not surface.workspace_admitted(str(getattr(binding, "team_id", "") or "")) or not (
        channel_and_origin_permit(live_config(None), channel_id, origin)
    ):
        surface.audit(
            "system",
            f"{OPERATION}.notice",
            "denied",
            f"{resources} origin={origin}",
            "channel, origin or workspace does not admit the bot",
        )
        _release(channel_id, token)
        return
    if _noticed.get(channel_id) != token:
        # Superseded while the checks awaited (forgotten, or a newer attempt holds
        # the channel): that attempt posts, not this one.
        surface.audit("system", f"{OPERATION}.notice", "denied", resources, "superseded")
        return
    try:
        ts = str(await slack.post_blocks(channel_id, notice_blocks(channel_id), NOTICE_TEXT) or "")
    except Exception:
        # The outcome is UNKNOWN (Slack may have posted it and lost the answer), so
        # the channel stays noticed for this switch-off: a retry could post twice.
        # Its button is not registered, so a click on it is refused as stale.
        logger.warning(
            "code channel %s: hand-off notice outcome unknown", channel_id, exc_info=True
        )
        if _noticed.get(channel_id) == token:
            _noticed[channel_id] = _UNKNOWN
        surface.audit("system", f"{OPERATION}.notice", "error", resources, "post outcome unknown")
        return
    if not ts:
        _release(channel_id, token)  # refused outright: the next refusal may try again
        surface.audit("system", f"{OPERATION}.notice", "error", resources, "post refused")
        return
    if _noticed.get(channel_id) == token:
        _noticed[channel_id] = ts
    surface.audit("system", f"{OPERATION}.notice", "allowed", f"{resources} message={ts}")


async def _ephemeral(channel: str, user: str, text: str) -> None:
    slack = surface.slack()
    if slack is None or not (channel and user):
        return
    try:
        await slack.post_ephemeral(channel, user, text)
    except Exception:
        logger.debug("code channel ephemeral failed", exc_info=True)


def _payload_ids(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    channel = str((payload.get("channel") or {}).get("id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    msg_ts = str((payload.get("message") or {}).get("ts") or "")
    actions = payload.get("actions") or [{}]
    value = str((actions[0] or {}).get("value") or "")
    return channel, user, msg_ts, value


async def handle_handoff_click(orch: Any, payload: dict[str, Any]) -> bool:
    """A Turn-on-hand-off click; False when it is not ours to answer (the gate is shut)."""
    if not code_channels_on(live_config(orch)):
        return False
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit
    from kiro_crew.slack.handler import is_owner

    channel, user, msg_ts, value = _payload_ids(payload)
    resources = f"channel={channel} message={msg_ts}"
    team = str(
        (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    )
    if not surface.workspace_admitted(team):
        surface.audit(
            user, OPERATION, "denied", f"{resources} team_id={team}", "enterprise_origin_mismatch"
        )
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or value != channel:
        surface.audit(user, OPERATION, "denied", resources, "not a bound code channel")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    # The owner's alone: the switch widens what every code channel's conductor may
    # do, which is the owner's choice exactly as it is on the Settings card.
    if not is_owner(user):
        surface.audit(user, OPERATION, "denied", resources, "not the owner")
        await _ephemeral(channel, user, NOT_OWNER_TEXT.format(owner=surface.owner_name()))
        return True
    origin = _origin_of(channel, binding, None)
    if not channel_and_origin_permit(live_config(orch), channel, origin):
        surface.audit(
            user,
            OPERATION,
            "denied",
            f"{resources} origin={origin}",
            "channel or origin does not admit the bot",
        )
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    if not await surface.outbound_permitted():
        surface.audit(user, OPERATION, "denied", resources, "channels governance policy")
        return True
    from kiro_crew.dashboard.handlers.messaging import save_code_channel_dispatch

    async with _lock(channel):
        # Only the gateway's CURRENT notice, once: a button another message wears
        # (an agent-posted copy -- whose label and confirm the agent chose -- an
        # older notice, a replayed payload) is not consent to this setting.
        issued = _noticed.get(channel, "")
        if (
            not msg_ts
            or not issued
            or issued in (_UNKNOWN, _USED)
            or issued.startswith(_PENDING)
            or issued != msg_ts
        ):
            surface.audit(user, OPERATION, "denied", resources, "not the current hand-off notice")
            await _ephemeral(channel, user, STALE_TEXT)
            return True
        _noticed[channel] = _USED
        try:
            refusal = await save_code_channel_dispatch(
                surface.dashboard_state(), value=True, caller=user, source="slack"
            )
        except Exception:
            # Not a decision: the button must not die with it.
            logger.warning("code channel %s: hand-off save failed", channel, exc_info=True)
            refusal = "the setting could not be saved"
        if refusal:
            pinned_at = await _overlay_pin_path()
            if pinned_at:
                # The button can only fail again until that file changes: swap it
                # for where the setting lives (the switch stays off).
                surface.audit(user, OPERATION, "error", resources, refusal)
                text = PINNED_TEXT.format(path=pinned_at)
                # Said once: on the notice itself, or privately when it cannot be edited.
                slack = surface.slack()
                shown = False
                if slack is not None and msg_ts:
                    try:
                        await slack.update_message(channel, msg_ts, text, pinned_blocks(text))
                        shown = True
                    except Exception:
                        logger.warning(
                            "code channel %s: hand-off notice not updated", channel, exc_info=True
                        )
                if not shown:
                    await _ephemeral(channel, user, text)
                return True
            _noticed[channel] = msg_ts  # nothing changed: the notice still stands
            surface.audit(user, OPERATION, "error", resources, refusal)
            await _ephemeral(channel, user, FAILED_TEXT.format(reason=refusal))
            return True
        surface.audit(user, OPERATION, "allowed", f"{resources} result=on")
        slack = surface.slack()
        if slack is not None and msg_ts:
            try:
                await slack.update_message(channel, msg_ts, DONE_TEXT, done_blocks())
            except Exception:
                logger.warning(
                    "code channel %s: hand-off notice not updated", channel, exc_info=True
                )
                await _ephemeral(channel, user, DONE_TEXT)
    return True
