"""The delivery-debt notice of ``SlackRenderer``: disclosing answer text Slack refused.

Moved verbatim from :mod:`kiro_crew.slack.renderer`, which composes it into
``SlackRenderer`` and owns ``_stream_debt``.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.slack.handler import DELIVERY_DEBT_NOTICE

logger = logging.getLogger(__name__)


class StreamDebtNotice:
    """Settles one renderer's delivery debt on the message that lost the text."""

    slack: Any
    channel: str
    thread_ts: str | None
    _stream_debt: bool

    async def _settle_stream_debt(self, ts: str) -> None:
        """Disclose answer text Slack refused for good, on the message that lost it.

        Called at every point a stream is abandoned, so the notice goes out while
        an append can still reach the message the hole is in. Clearing the debt is
        part of settling it: a second notice on a later message would report a gap
        the reader has already been shown.

        Sent directly rather than through ``_append_stream``: the notice is not
        answer text, so it must not enter the delivery ledger, which the rescue
        replays to the next turn as text the reader already established.

        ``append_stream`` reports a refusal by RETURNING False -- the client turns
        every exception into that return -- so the return value is the whole
        signal, and leaving it unread hides the very loss this notice exists to
        disclose. The two refusals that takes fall inside one Slack rate-limit or
        outage window, so they are correlated rather than independent. On refusal
        post a separate message, which does not depend on the stream that just
        refused and, sent directly, stays out of the ledger too.
        """
        if not self._stream_debt:
            return
        self._stream_debt = False
        notice_ok = False
        try:
            notice_ok = await self.slack.append_stream(self.channel, ts, DELIVERY_DEBT_NOTICE)
        except Exception:
            logger.warning("Slack: appending the delivery-debt notice failed")
        if not notice_ok:
            try:
                await self.slack.post_message(self.channel, DELIVERY_DEBT_NOTICE, self.thread_ts)
            except Exception:
                logger.warning(
                    "Slack: the delivery-debt notice reached neither the "
                    "stream nor a separate message"
                )
