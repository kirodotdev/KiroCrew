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

from kiro_crew.dashboard.slack_egress import channel_egress_permitted
from kiro_crew.messaging.link import SLACK_NAMESPACE, channel_namespace_of

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


async def _deliver_slack(state: Any, key: str, text: str) -> None:
    """Post into the Slack thread the session is bound to.

    Vetted by the shared ``channel_egress_permitted`` gate and NOT by the full
    hardened chain, deliberately. Adopting the chain here would widen this
    surface's refusal set -- a notice would newly be refused when no authority
    names the recipient, or on a mid-send rebind -- and that is a behaviour
    change to a surface this change does not otherwise need to touch. It belongs
    in its own change, where the widened refusal is the subject under review
    rather than a rider, alongside the three other proactive sends
    ``_deliver_slack_governed``'s docstring defers for the same reason.

    So this keeps the pre-existing posture: governance-vetted, audited, and
    sending on the link it read. The gate is the EXTRACTED shared one rather
    than a local copy, which is the part worth keeping -- a duplicated
    fail-closed gate is how the two copies drift.
    """
    client = getattr(state, "slack_client", None)
    sessions = getattr(state, "sessions", None)
    if client is None or sessions is None:
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
    if not await asyncio.to_thread(
        channel_egress_permitted, key, SLACK_NAMESPACE, tool_name="chat.compaction_notice"
    ):
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
    # ORIGIN then MIRROR, through the ONE spelling of that ladder rather than a
    # third hand-written copy of it. Origin alone is written by exactly one
    # channel -- Discord's resume path -- so a notice for a Telegram or WeCom
    # conversation, which binds a MIRROR on its first turn, would be computed and
    # then dropped; the conversation gets summarized silently, and on a compaction
    # FAILURE the operator never sees the "run /compact or /new" line that is the
    # whole point of the notice.
    #
    # ``skip_paused`` is passed EXPLICITLY even though False is the default,
    # because the pause posture is the thing that diverges between callers and a
    # default read from elsewhere hides it. A notice is pause-BLIND on purpose: a
    # paused mirror still stops receiving turns, and the compaction that just
    # happened is precisely what the operator needs told about. Only the note
    # mirror opts into skipping, where a paused row means the user asked for
    # silence on that conversation.
    #
    # LAZY BECAUSE HOISTING RAISES AT IMPORT TIME: module scope here fails with
    # `ImportError: cannot import name 'DashboardState' from partially initialized
    # module 'kiro_crew.dashboard.state'`, via handlers.messaging ->
    # dashboard/handlers/__init__.py -> handlers_system -> dashboard.state. The
    # `top-level-imports` rule exempts a circular import. Measuring this needs the
    # HOIST direction -- importing handlers.messaging first succeeds, so a probe that
    # way reports no cycle and proves nothing.
    from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link

    try:
        snapshot = snapshot_channel_link(state, key, skip_paused=False)
    except Exception:
        logger.debug("compact notice: link lookup failed for %s", key, exc_info=True)
        return
    if snapshot is None:
        return
    link, _is_origin = snapshot
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
