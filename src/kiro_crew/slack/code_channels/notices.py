"""Tell people once when Slack refuses a code channel's writes, instead of failing silently.

Two refusals switch a channel's context bar, tabs and commands off for good
(`Slack Code authorization model`_): ``not_agent_channel`` (Slack does not hold
the channel as a code channel) and ``not_authorized`` (this app is not the
channel's agent any more). Each is said ONCE per channel, as a dashboard
notification naming the channel, so the bar going stale has a reason someone
can read. ``feature_disabled`` (the workspace has no code channels at all) is
install-wide and already raises its own one-time notification through the
capability latch (``slack.scope_probe.capability_notifier``); the agent's tools
answer each of the three with a plain sentence (:data:`CHANNEL_REFUSALS`).

.. _Slack Code authorization model: https://docs.slack.dev/ai/slack-code/#authorization-model
"""

from __future__ import annotations

import logging

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

#: What each refusal means, in the words the agent and the notification use.
CHANNEL_REFUSALS: dict[str, str] = {
    "not_agent_channel": (
        "Slack says this channel is not a code channel, so Kiro Crew can't set its "
        "context bar, tabs or commands."
    ),
    "not_authorized": (
        "Slack says Kiro Crew is no longer this code channel's agent, so it can't change "
        "the channel's context bar, tabs or commands."
    ),
    "feature_disabled": (
        "Slack code channels are turned off for this workspace, so the channel's context "
        "bar, tabs and commands can't be updated."
    ),
}
#: The refusals that get a per-channel notification (feature_disabled has its own).
NOTIFIED = frozenset({"not_agent_channel", "not_authorized"})

TITLE = "Slack code channel: updates stopped"


async def notify_refusal(channel_id: str, code: str, *, session_key: str = "") -> bool:
    """Notify once for *code* on *channel_id*; True when a notification went out."""
    if code not in NOTIFIED or not channel_id:
        return False
    try:
        record = await get_async(channel_id)
        if record is not None and code in record.notices:
            return False
        state = surface.dashboard_state()
        notify = getattr(state, "notify", None)
        if not callable(notify):
            return False
        from kiro_crew.slack.code_channels.store import store

        binding = store().get_binding(channel_id)
        name = (binding.channel_name if binding is not None else "") or channel_id
        notify(
            "slack",
            TITLE,
            f"#{name}: {CHANNEL_REFUSALS[code]}",
            meta={"channel_id": channel_id, "session_key": session_key, "error": code},
        )
        kept = list(record.notices) if record is not None else []
        await update_async(channel_id, notices=[*kept, code][-8:])
        return True
    except Exception:
        logger.debug("code channel %s: refusal notice failed", channel_id, exc_info=True)
        return False
