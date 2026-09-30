"""Where an unattended result is routed.

A job belongs to the conversation that scheduled it: the origin key recovered from
a run's session key, the channel conversation behind it (origin link, mirror link,
or a direct session's stored target), the channel leg that hands a result to that
conversation, the dedup anchor a confirmed delivery advances, the OPTIONS
bookkeeping for a posted control, the bounded DM open, and whether a job is
silent.

Every delivery leg that renders or redacts a result before it leaves the process
stays in the facade (``_deliver_channel_reply``, ``_deliver_cron_response``,
``_deliver_result`` with the heartbeat Slack rendering, the failure alerts):
``security_posture`` names ``slack/gateway.py`` as the sink for cron and
notification posts.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import (
        CHANNEL_SESSION_NAMESPACES,
        CHAT_TYPE_DIRECT,
        DM_SCOPE_UNIFIED,
        SLACK_NAMESPACE,
        ChannelLink,
        CronJob,
        GatewayOrchestrator,
        PostedOptions,
        asyncio,
        channel_namespace_of,
        logger,
        open_dm_with_retry,
        parse_session_key,
        remember_slack_options,
        time,
        vet_and_audit,
    )


async def _open_dm_with_retry(
    self: GatewayOrchestrator, user_id: str, job_name: str, max_attempts: int = 3
) -> str | None:
    """Retry open_dm to handle transient Slack API errors (shared impl)."""
    if self.slack is None:
        return None
    return await open_dm_with_retry(
        self.slack,
        user_id,
        context=f"Cron '{job_name}'",
        max_attempts=max_attempts,
    )


def _record_cron_delivery(self: GatewayOrchestrator, job: CronJob, result_hash: str) -> None:
    """Advance the dedup anchor after a CONFIRMED delivery, on any surface.

    Delivery-agnostic on purpose, and that is the whole point: this triple is
    the state the duplicate-suppression read consults, so a surface that
    delivers a result without advancing it can never suppress the next
    identical one. Leaving it to the Slack branch alone left every
    channel-delivered cron with ``last_posted_hash == ""`` forever, so an
    unchanged-output job spammed the chat on every tick where Slack posted
    once and then went quiet for ``_SUCCESS_REMINDER_SECS``, and the "same
    result N times in a row" reminder could never fire there at all.

    Call it only where delivery is CONFIRMED. In particular the
    dashboard-notification-only path must NOT: the bell is passive and the
    operator may never open it, so counting it as delivered would suppress a
    result nobody has seen.
    """
    job.last_posted_hash = result_hash
    job.consecutive_dupes = 0
    job.last_posted_at = time.time()


def _remember_options(
    self: GatewayOrchestrator,
    session_key: str,
    channel: str,
    ts: str,
    choices: list[str],
    blocks: list[dict],
    text: str,
) -> None:
    """Record an OPTIONS control posted into *session_key*'s Slack thread.

    Lets that session's next turn strike the control through, so a delivery
    which ended on a question stops inviting an answer once the conversation
    has moved past it. Best-effort: losing the record only means the control
    is left live.
    """
    if not ts or not choices:
        return
    try:

        remember_slack_options(
            self.dashboard_state,
            session_key,
            PostedOptions(
                channel=channel,
                ts=ts,
                choices=tuple(choices),
                blocks=tuple(blocks),
                text=text,
            ),
        )
    except Exception:
        logger.debug("Failed to record OPTIONS control for %s", session_key, exc_info=True)


def _channel_reply_link(
    self: GatewayOrchestrator, parent_key: str
) -> tuple[ChannelLink, bool] | None:
    """Resolve the non-Slack channel conversation behind *parent_key*.

    Returns ``(link, needs_dm_resolution)``, or None when no safe target
    exists. Resolution ladder, most explicit first:

    1. the session's **origin link** — the conversation's real send target,
       recorded by a transport's inbound dispatch (e.g. Discord);
    2. a non-Slack **mirror link** (e.g. a Telegram ``/link`` binding),
       which also carries a forum Topic thread id;
    3. the session's stored channel value — a *session-attribution* id,
       not a postable conversation, so it is accepted ONLY when it names a
       direct (1:1) peer: for a canonical channel key the stored
       ``"{namespace}:{user_id}"`` must match the key's own namespace and
       the key's chat_type must be direct; for a ``unified:`` DM bucket
       (direct-only by construction — forum routes never collapse into it)
       the stored namespace must be a registered non-Slack channel. The id
       is the peer's USER id, so the caller must resolve the postable
       conversation through ``transport.resolve_configured_target``
       (``needs_dm_resolution=True``). Group/forum sessions never take
       this rung: their stored value carries the sender's user id, and
       sending there would leak the conversation into a private DM.

    Returns None for Slack, dashboard, and unrecognized keys so callers
    keep their existing delivery for those.
    """
    namespace = channel_namespace_of(parent_key)
    if not namespace or namespace == SLACK_NAMESPACE or self.sessions is None:
        return None
    for getter in (self.sessions.get_origin_link, self.sessions.get_mirror_link):
        try:
            link = getter(parent_key)
        except Exception:
            link = None
        if link is not None and link.channel_id and link.channel_type != SLACK_NAMESPACE:
            return link, False
    try:
        stored = self.sessions.get_channel(parent_key)
    except Exception:
        stored = None
    if not stored:
        return None
    channel_type, sep, peer_id = stored.partition(":")
    if not (sep and channel_type and peer_id) or channel_type == SLACK_NAMESPACE:
        return None
    if namespace == DM_SCOPE_UNIFIED:
        # A unified bucket carries no chat_type of its own; validate the
        # stored namespace against the registered channel set instead.
        if channel_type not in CHANNEL_SESSION_NAMESPACES or channel_type == DM_SCOPE_UNIFIED:
            return None
    else:
        parsed = parse_session_key(parent_key)
        if parsed is None or parsed.chat_type != CHAT_TYPE_DIRECT or channel_type != namespace:
            return None
    return ChannelLink(channel_type, channel_id=peer_id), True


def _cron_origin_key(self: GatewayOrchestrator, parent_key: str) -> str:
    """The session key the cron job behind *parent_key* was created from.

    ``parent_key`` is ``cron:{job_id}`` or ``cron:{job_id}:{run_id}``, so the
    job id is the second colon-separated segment in both spellings. A cron
    key of its own carries no channel namespace, so it can never name the
    surface the job belongs to; the creating session's key can, which is why
    the job records it.

    Returns ``""`` when no job is known or its origin is unusable. The field
    round-trips through ``cron.json`` without coercion, so a hand-edited or
    corrupt store can hand back a non-string, and an origin that is not a
    session key must degrade to "no channel" rather than raise on a
    delivery path.
    """
    if not parent_key.startswith("cron:") or self.cron_svc is None:
        return ""
    parts = parent_key.split(":", 2)
    if len(parts) < 2:
        return ""
    job = self.cron_svc.get_job(parts[1])
    origin = job.session_key if job else ""
    return origin if isinstance(origin, str) else ""


async def _deliver_cron_to_channel(
    self: GatewayOrchestrator, origin_key: str, text: str, *, actor_key: str
) -> bool:
    """Deliver cron output to the non-Slack channel that owns *origin_key*.

    A job belongs to the conversation that scheduled it, so an unattended
    run reaches the surface its owner actually watches instead of Slack
    alone. The send itself reuses the shared transport leg, which already
    redacts through the canonical egress shim (credentials AND exfiltration
    URLs, so a second pass here would only double-scrub) and chunks to the
    transport's own message ceiling.

    Two profiles govern one send, tightest-wins. ``_deliver_channel_reply``
    vets *origin_key*, whose surface is the DESTINATION conversation, so this
    vets *actor_key* (``cron:{job_id}``) as well: cron is the unattended
    surface an operator restricts hardest, and evaluating only the
    destination would let a cron-surface ``channels`` denial stop applying
    the moment cron routed through a channel it does not itself own. Both
    gates are the same audited, fail-closed seam, so a denial at either end
    refuses the send and lands on the SEL trail.

    Returns False for a Slack, dashboard, or unresolvable origin: those keep
    the Slack leg and the dashboard bell as their delivery, which is every job
    an install carries today. When it DOES deliver, it is the only leg: the
    callers stand their Slack leg down, because one run notifying one operator
    twice is how notifications become noise. An explicit ``job.channel`` is a
    destination the user pinned and takes precedence over both.
    """
    if not origin_key or not text.strip():
        return False
    resolved = self._channel_reply_link(origin_key)
    if resolved is None:
        return False
    channel_type = resolved[0].channel_type
    try:
        # Off-loop: resolving the active profile walks the profile directory
        # (iterdir + stat, with a possible reload), unbounded on slow or
        # networked storage.
        decision = await asyncio.to_thread(
            vet_and_audit,
            "channels",
            channel_type,
            session_key=actor_key,
            tool_name="cron.channel_delivery",
            # An egress on a network surface, so a degraded evaluation must
            # DENY rather than degrade-to-permit.
            fail_closed=True,
        )
    except Exception:
        # Fail closed on the way out too: an unusable answer from a gate is
        # not permission, and cron has no operator to ask.
        logger.exception(
            "Cron %s: governance evaluation failed for %s; refusing delivery",
            actor_key,
            channel_type,
        )
        return False
    # Default False, not True: a Decision without ``permitted`` is an
    # unusable answer from a gate, and must not read as permission.
    if not getattr(decision, "permitted", False):
        logger.info(
            "Cron %s: delivery to %s denied by the cron surface's policy",
            actor_key,
            channel_type,
        )
        return False
    return await self._deliver_channel_reply(
        origin_key, text, resolved_link=resolved, caller="cron"
    )


def _cron_job_is_silent(self: GatewayOrchestrator, parent_key: str) -> bool:
    """Return True if *parent_key* maps to a cron job marked silent.

    ``_deliver_cron_response`` routes a cron
    session's post-subagent-completion turn to its channel and to Slack,
    gated on ``info.silent``, the *sub-agent's* flag. That flag is never set from
    the parent cron's ``silent`` setting (``spawn`` defaults it False and
    the spawn queue tuple doesn't carry it), so a silent cron's subagent
    completions still reached Slack. The cron job's own ``silent`` flag is
    the source of truth, so resolve it here. ``parent_key`` is
    ``cron:{job_id}`` or ``cron:{job_id}:{run_id}``.
    """
    if not parent_key.startswith("cron:") or self.cron_svc is None:
        return False
    parts = parent_key.split(":", 2)
    if len(parts) < 2:
        return False
    job = self.cron_svc.get_job(parts[1])
    return bool(job and job.silent)
