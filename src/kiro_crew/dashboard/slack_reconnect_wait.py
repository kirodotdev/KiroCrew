"""The Slack client a dashboard send to Slack uses while Slack reconnects.

``POST /api/slack/reconnect`` clears ``DashboardState.slack_client`` for the
attempt and publishes the new client only behind a connected socket
(:mod:`kiro_crew.slack.reconnect`). A dashboard send that finds no client waits
for that attempt through ``DashboardState.slack_reconnect_wait``, which the
gateway owning the Slack socket publishes beside ``slack_reconnect``, and then
takes whatever client the attempt published. A reply, approval card or turn-end
tidy-up owed to a thread the turn had already linked, left with no client, says
so to the owner (:func:`slack_reply_client`); a turn that started in the window of a failed
attempt was never mirrored, so it runs unmirrored, as with no client at all.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def wait_for_slack_reconnect(state: Any) -> None:
    """With no Slack client published, wait for the reconnect in progress to finish.

    Returns at once while a client is published or no reconnect runs. Read
    ``state.slack_client`` afterwards: the client the attempt published, or
    ``None`` when it ended without one.
    """
    wait = getattr(state, "slack_reconnect_wait", None)
    if getattr(state, "slack_client", None) is None and wait is not None:
        await wait()


REPLY_NOTICE = (
    "Slack: reply not delivered",
    "Slack did not reconnect, so a reply was not posted to its Slack thread. "
    "The reply is in the dashboard; reconnect Slack in Settings.",
)
APPROVAL_NOTICE = (
    "Slack: approval not posted",
    "Slack did not reconnect, so an approval request was not posted to its Slack thread. "
    "Answer it in the dashboard; reconnect Slack in Settings.",
)
TURN_END_NOTICE = (
    "Slack: thread not tidied",
    "Slack did not reconnect, so a turn's progress line was left in its Slack thread. "
    "The turn is in the dashboard; reconnect Slack in Settings.",
)


def slack_reply_client(
    state: Any,
    session_key: str,
    channel: str,
    *,
    turn: Any = None,
    notice: tuple[str, str] = REPLY_NOTICE,
) -> Any:
    """The published Slack client a send owed to a linked thread posts with.

    The turn linked its thread through a published client, so none now means a
    Slack reconnect took it and did not publish another: the owner gets one
    dashboard notification, *notice* (title, body), instead of losing the send
    silently, and this returns ``None``. Given its *turn*, the owner is told once
    per turn, however many of its sends find no client.
    """
    client = getattr(state, "slack_client", None)
    if client is not None:
        return client
    if turn is not None:
        if getattr(turn, "_mirror_owner_told", False):
            return None
        turn._mirror_owner_told = True
    logger.warning("Slack did not reconnect: %s for %s", notice[0], session_key)
    state.notify(
        "agent", notice[0], notice[1], meta={"session_key": session_key, "channel": channel}
    )
    return None
