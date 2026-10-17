"""The owner-notification exit point.

A redacted direct message to the owner: over Slack first, otherwise over the one
channel target that can only be the owner.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
    )


async def _dm_owner(state: DashboardState, text: str) -> None:
    """Best-effort owner notification, Slack first then any live channel.

    The shared owner-notification exit point (currently the
    safety-override-expiry path), so the open_dm → post_message →
    swallow-and-log idiom lives in one place.

    **Slack is not the only place an operator lives.** No-opping without Slack
    would make an expiring unattended grant invisible on a Teams-only,
    Discord-only or Telegram-only install — silence about a security grant
    lapsing is the one outcome this notice exists to prevent. So a Slack DM is
    preferred (it is the owner's direct address), and every registered
    channel transport that advertises a reachable configured target is used as the
    FALLBACK when Slack is absent or could not deliver. Not in addition: an
    operator with Slack should get one notice, not one per channel.

    Defense-in-depth: because this is the single exit point for owner
    notifications and is intended for reuse, ``text`` is passed through
    ``redact_exfiltration_urls()`` then ``redact_credentials()`` (same order as
    the rest of the Slack surface) so a future caller that forwards
    LLM/user-derived content can never leak credentials or exfil URLs, even
    though today's callers only pass static constants.
    """
    safe_text, _ = redact_exfiltration_urls(text)
    safe_text, _ = redact_credentials(safe_text)
    slack_client = state.slack_client
    owner_id = state.owner_id
    if slack_client and owner_id:
        try:
            dm_channel = await slack_client.open_dm(owner_id)
            await slack_client.post_message(dm_channel, safe_text)
            return
        except Exception:
            logger.debug("Owner Slack DM failed; trying the channel transports", exc_info=True)
    await _notify_owner_channels(state, safe_text)


async def _notify_owner_channels(state: DashboardState, safe_text: str) -> None:
    """Deliver an already-redacted owner notice to a channel that can NAME the owner.

    "Reachable" is the transport's OWN answer (`configured_targets` →
    `resolve_configured_target`), so this reaches only destinations that channel
    already authorized — a Teams DM whose route was learned from an allow-listed
    sender, never an address chosen here. Each channel is independent: one that
    cannot deliver must not stop the next.

    **Exactly one candidate across EVERY channel, or nothing.** This notice carries the
    operator's own security state — an expiring unattended auto-approve grant, for
    instance — and there is no channel-neutral owner identity to check it against: Slack
    has an owner id and is preferred above; nothing else does. An allow-list is a list of
    people permitted to TALK to the agent, not a claim that any of them is the operator.

    So the only sound inference is a counting one, and it has to be counted across the
    whole install rather than per channel. Two channels each holding a DIFFERENT single
    identity is two people, and delivering to both hands one of them the other's security
    state — a per-channel "exactly one target" rule misses that entirely. With exactly one
    reachable person in the whole configuration, that person is the operator; with two or
    more, refuse everybody. Same premise as `/sessions`' owner-only rule.

    Counted over ALL configured targets, not just the reachable ones: a three-person
    allow-list where only one route happens to have been learned is still a guess.

    The false negative is deliberate and is the safe direction: the same human configured
    on two channels reads as two candidates and gets no channel notice. The dashboard feed
    carries the same notice unconditionally, so silence here costs a convenience, while
    misdelivery would cost the operator's security state. Positively binding a channel
    identity to the operator is a per-identity authority model that does not exist yet;
    when it does, this becomes a lookup instead of a count.
    """
    candidates: list[tuple[str, Any, Any]] = []
    for channel_type, transport in list(state.channel_transports.items()):
        try:
            if not transport.capabilities.supports_proactive_send:
                continue
            candidates.extend(
                (channel_type, transport, target) for target in transport.configured_targets()
            )
        except Exception:
            logger.debug("Owner notice enumeration failed for %s", channel_type, exc_info=True)
    if len(candidates) != 1:
        if candidates:
            logger.debug(
                "Owner notice skipped: %d channel targets, none positively the owner",
                len(candidates),
            )
        return
    channel_type, transport, target = candidates[0]
    if not target.available:
        return
    try:
        resolved = await transport.resolve_configured_target(target.target_id)
        if not resolved:
            return
        conversation_id, thread_id = resolved
        await transport.send_message(conversation_id, safe_text, thread_id)
    except Exception:
        logger.debug("Owner notice skipped for %s", channel_type, exc_info=True)


def _dispatch_owner_dm(state: DashboardState, text: str) -> None:
    """Fire-and-forget an owner DM without blocking the caller.

    Schedules :func:`_dm_owner` as a tracked background task so a slow or
    unreachable Slack API never stalls the startup / hot path. No-op if there
    is no running loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No running event loop — owner DM skipped")
        return
    task = loop.create_task(_dm_owner(state, text))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
