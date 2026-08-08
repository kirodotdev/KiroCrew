"""Auto-compaction notice delivery for channel-originated sessions.

``SessionManager`` fires its compact callback for EVERY session it compacts, but
the dashboard's handler only knows how to append to a chat slot — so a session
living on Slack or Discord had its context compacted silently: no notice, and no
explanation for the summarized history the user sees afterwards. This module is
the channel leg of that notice.

Delivery reuses the two existing outbound paths rather than adding a third:

* **Slack** — ``state.slack_client.post_message`` into the thread persisted by
  the inbound leg (``SessionMap.get_slack_link``), the same resolution the
  linked-thread auth-error notice uses. Slack is deliberately absent from the
  ``channel_transports`` registry, so it cannot ride the ladder below.
* **every other channel** — the governed cross-surface ladder
  (``chat_runner._resolve_channel_target``), which vets the send against the
  ``channels`` governance scope, records a SEL decision for grant AND denial,
  and checks ``supports_proactive_send`` before handing off to
  ``Transport.send_message``. The target is the session's ``origin`` link,
  recorded by the transport's inbound path.

Best-effort by construction: the compaction itself already succeeded and the
session keeps running, so a failed or impossible delivery is logged and
swallowed rather than surfaced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.messaging.link import SLACK_NAMESPACE, channel_namespace_of
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_profiles import vet_and_audit

logger = logging.getLogger(__name__)

#: Notices are plain text: a channel session may be read on a client with no
#: markdown rendering, and the dashboard's own notice copy does not transfer
#: (it references UI affordances the channel user does not have).
CHANNEL_COMPACT_NOTICE = (
    "Context reached {pct:.0f}% and was auto-compacted. Earlier turns are now a "
    "summary; this conversation continues where it left off."
)
CHANNEL_COMPACT_FAILED_NOTICE = (
    "Context reached {pct:.0f}% but auto-compact failed. It retries after a "
    "cooldown — send {cmd} to compact now, or {new_cmd} to start fresh."
)

#: Manual fallbacks differ per channel: the bang-prefixed transports own their
#: commands locally, while the reply-token channels use slash commands. Keyed by
#: channel namespace with a conservative default for a transport that has not
#: been checked.
_MANUAL_COMMANDS: dict[str, tuple[str, str]] = {
    "slack": ("`!compact`", "`!new`"),
    "discord": ("`!compact`", "`!new`"),
}
_DEFAULT_COMMANDS = ("/compact", "/new")


def notice_text(namespace: str, pct: float, *, success: bool) -> str:
    """Render the notice for *namespace* at *pct* usage."""
    if success:
        return CHANNEL_COMPACT_NOTICE.format(pct=pct)
    compact_cmd, new_cmd = _MANUAL_COMMANDS.get(namespace, _DEFAULT_COMMANDS)
    return CHANNEL_COMPACT_FAILED_NOTICE.format(pct=pct, cmd=compact_cmd, new_cmd=new_cmd)


async def deliver_channel_compaction_notice(
    state: Any, key: str, pct: float, *, success: bool
) -> None:
    """Post the auto-compact notice into the conversation behind *key*.

    Silent no-op for a key that is not channel-originated (``cron:``,
    ``heartbeat``, ``subagent:`` and friends have no user watching a
    conversation), and for a channel session whose reply target cannot be
    resolved.
    """
    namespace = channel_namespace_of(key)
    if not namespace:
        return
    if namespace == SLACK_NAMESPACE:
        await _deliver_slack(state, key, notice_text(namespace, pct, success=success))
        return
    await _deliver_via_transport(state, key, pct, success=success)


def _channel_egress_permitted(session_key: str, channel_type: str) -> bool:
    """Vet an outbound notice against the ``channels`` governance scope.

    The non-Slack leg inherits this from ``_resolve_channel_target``. Slack is
    deliberately absent from ``channel_transports`` and so never reaches that
    ladder, which would leave its notice as the one unvetted, unaudited egress
    in this module. Fail-closed: a degraded evaluation denies rather than
    degrading to permit, matching the ladder and the other ``channels``-scope
    gates. ``vet_and_audit`` records a SEL decision for both grant and denial.

    Synchronous by design — callers run it through ``asyncio.to_thread`` because
    the gate reads the profile directory.
    """
    try:
        decision = vet_and_audit(
            "channels",
            channel_type,
            session_key=session_key,
            tool_name="chat.compaction_notice",
            fail_closed=True,
        )
    except PlatformCompositionError:
        # An invalid governance ceiling is not an ordinary skip: the ladder
        # deliberately re-raises rather than degrading, and so does this gate.
        raise
    except Exception:
        logger.debug(
            "compact notice: governance check failed for %s; denying (fail-closed)",
            session_key,
            exc_info=True,
        )
        return False
    # Default False: a Decision without ``permitted`` is an unusable answer from
    # a gate and must not read as permission.
    return bool(getattr(decision, "permitted", False))


async def _deliver_slack(state: Any, key: str, text: str) -> None:
    """Post into the Slack thread the session is bound to."""
    client = getattr(state, "slack_client", None)
    sessions = getattr(state, "sessions", None)
    if client is None or sessions is None:
        return
    # Lazy, same reason as the chat_runner import below: chat_utils imports
    # dashboard.state, which imports this module at scope.
    from kiro_crew.dashboard.chat_utils import slack_mirror_is_paused

    # A compaction notice is turn narration, so a muted thread does not get it.
    if slack_mirror_is_paused(state, key):
        return
    try:
        thread_ts, channel_id = sessions.get_slack_link(key)
    except Exception:
        logger.debug("compact notice: slack link lookup failed for %s", key, exc_info=True)
        return
    if not channel_id:
        return
    # Off-loop: the gate walks the profile directory (iterdir + stat, with a
    # possible reload), which is unbounded on slow or networked storage.
    if not await asyncio.to_thread(_channel_egress_permitted, key, SLACK_NAMESPACE):
        return
    try:
        # thread_ts is optional: a session bound to a channel without a thread
        # (post_message treats None as a top-level post) still gets the notice.
        await client.post_message(channel_id, text, thread_ts or None)
    except Exception:
        logger.debug("compact notice: slack delivery failed for %s", key, exc_info=True)


async def _deliver_via_transport(state: Any, key: str, pct: float, *, success: bool) -> None:
    """Send through the governed cross-surface ladder (Discord and friends).

    The notice text is rendered from the RESOLVED channel type rather than the
    key's namespace, so a ``unified:`` DM bucket still quotes the right manual
    command for the channel it actually lives on.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return
    try:
        link = sessions.get_origin_link(key)
    except Exception:
        logger.debug("compact notice: origin lookup failed for %s", key, exc_info=True)
        return
    if link is None:
        return
    # Lazy: chat_runner imports state at module scope, so a top-level import
    # here would close the cycle.
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    # Off-loop: the ladder's governance gate walks the profile directory
    # (iterdir + stat, with a possible reload), which is unbounded on slow or
    # networked storage. A notice is never worth stalling the loop for.
    target = await asyncio.to_thread(_resolve_channel_target, state, key, link)
    if target is None:
        return
    resolved, transport = target
    text = notice_text(resolved.channel_type, pct, success=success)
    try:
        await transport.send_message(resolved.channel_id, text, thread_id=resolved.thread_id)
    except Exception:
        logger.debug(
            "compact notice: %s delivery failed for %s",
            resolved.channel_type,
            key,
            exc_info=True,
        )
