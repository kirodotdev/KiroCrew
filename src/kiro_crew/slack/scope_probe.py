"""Tracked-channel history-readability probe.

Slack grants OAuth scopes only at install time: an install created before the
app manifest gained ``groups:history``/``message.groups`` keeps its old grant
set even after the manifest is fixed, so a tracked private channel delivers no
message events and the silence is indistinguishable from "nobody wrote
anything". This probe gives that dead state a trace: one cheap
``conversations.history(limit=1)`` capability check per tracked channel,
emitting a log warning (and a dashboard notification) for every channel the
bot token cannot read.

Callers dispatch :func:`warn_unreadable_tracked_channels` as a deferred task
(``asyncio.create_task``) — never on the gateway boot path — at gateway
startup after the Slack socket connects, and whenever a channel is added to
tracking dynamically. Best-effort by design: only definitive Slack error
codes are reported; transient network failures stay silent so a flaky link
never produces a false "reinstall your app" alarm.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

# Slack error codes that definitively mean the bot token cannot read the
# channel's history under its current OAuth grant:
# - ``missing_scope``: the install predates a required scope (for a private
#   channel that is ``groups:history`` on a pre-manifest-fix install).
# - ``channel_not_found``: the token cannot see the channel at all (a private
#   channel outside the grant's visibility looks nonexistent to the API).
UNREADABLE_ERRORS = frozenset({"missing_scope", "channel_not_found"})

_REINSTALL_HINT = (
    "the Slack app install likely predates the current OAuth scopes "
    "(groups:history / message.groups) — reinstall the Slack app from the "
    "bundled manifest to restore private-channel message delivery."
)


async def warn_unreadable_tracked_channels(
    slack: "SlackClientOps",
    channel_ids: Iterable[str],
    notify: Callable[..., None] | None = None,
) -> dict[str, str]:
    """Probe each tracked channel's history readability; warn on failures.

    Returns ``{channel_id: error_code}`` for every channel the bot token
    cannot read. When *notify* (``DashboardState.notify``-compatible) is
    given and at least one channel is unreadable, one aggregated dashboard
    notification is pushed. Never raises — safe to run fire-and-forget.
    """
    unreadable: dict[str, str] = {}
    for cid in sorted({c for c in channel_ids if c}):
        err = await slack.probe_channel_history(cid)
        if err in UNREADABLE_ERRORS:
            unreadable[cid] = err
            # The rule keys on the word "token" in the format string; the call
            # logs only a channel ID, a Slack error code, and a static hint —
            # no credential is in scope here.
            logger.warning(  # nosemgrep: python-logger-credential-disclosure
                "Tracked Slack channel %s is not readable by the bot token (%s) "
                "— messages there will NOT be delivered. If this is a private "
                "channel, %s",
                cid,
                err,
                _REINSTALL_HINT,
            )
    if unreadable and notify is not None:
        channels = ", ".join(sorted(unreadable))
        try:
            notify(
                "agent",
                "Slack: tracked channel(s) unreadable",
                (
                    f"The Slack bot token cannot read tracked channel(s) "
                    f"{channels} — messages there will NOT be delivered. "
                    f"If these are private channels, {_REINSTALL_HINT}"
                ),
                meta={"channels": sorted(unreadable)},
            )
        except Exception:
            logger.debug("scope-probe notification failed", exc_info=True)
    return unreadable
