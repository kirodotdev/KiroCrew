"""How ``SlackRenderer`` delivers a reply that has no stream to ride.

A top-level reply in a single-session DM, or one whose stream Slack refused,
shows a placeholder and is sealed into it with ``chat.update``. Two things set
that path apart from the stream, and both are handled here:

* Grammar. The stream's ``markdown_text`` renders Markdown. A ``text`` field is
  mrkdwn, and ``chat.update`` reads it under ``parse=client``, so the model's
  links and emphasis would show raw. The placeholder and every reply part go out
  as one ``markdown`` block instead, which renders the way the stream does, with
  the same text kept in ``text`` for notifications.
* Delivery. The edit carries the whole answer, and Slack can refuse any edit
  (``edit_window_closed``, ``cant_update_message``, ``ratelimited``, ...). A
  refused edit posts the reply as its own message, so it never takes the answer
  with it. Only a refusal does, read by the rule proactive sends use
  (``_slack_rejected_outright``, plus ``_slack_rate_limited``): after a
  transport error, a 5xx or an ``internal_error`` / ``fatal_error`` answer the
  edit may have landed, and posting after it would show the answer twice.
"""

from __future__ import annotations

import logging

from slack_sdk.errors import SlackApiError

from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)


def markdown_blocks(text: str) -> list[dict]:
    """*text* as one Block Kit ``markdown`` block."""
    return [{"type": "markdown", "text": text}]


async def post_markdown(
    slack: SlackClientOps, channel: str, text: str, thread_ts: str | None
) -> str:
    """Post *text* as a ``markdown`` block; returns the new message's ts."""
    return await slack.post_blocks(channel, markdown_blocks(text), text, thread_ts)


async def seal_placeholder(
    slack: SlackClientOps, channel: str, ts: str, text: str, thread_ts: str | None
) -> None:
    """Replace placeholder *ts* with *text*, or post *text* if Slack refuses the edit.

    Raises when the edit fails any other way, or the post fails too.
    """
    try:
        await slack.update_message(channel, ts, text, markdown_blocks(text))
    except SlackApiError as exc:
        from kiro_crew.dashboard.handlers.messaging import (
            _slack_rate_limited,
            _slack_rejected_outright,
        )

        if not (_slack_rejected_outright(exc) or _slack_rate_limited(exc)):
            raise
        logger.warning("Slack refused the reply edit; posting the reply instead", exc_info=True)
        await post_markdown(slack, channel, text, thread_ts)
