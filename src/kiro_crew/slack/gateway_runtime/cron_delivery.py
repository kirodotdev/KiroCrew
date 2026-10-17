"""Deliver a single-agent cron result to where the job belongs.

The duplicate and silent paths, the dashboard tab and bell, the channel the job was
scheduled from, and the Slack post with its uploads; the dedup anchor advances only
on a confirmed delivery.

Orchestrator fields read: ``_owner_id``, ``dashboard_state``, ``sessions``, ``slack``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from kiro_crew.slack.gateway import (
        _SUCCESS_REMINDER_SECS,
        Any,
        CronJob,
        GatewayOrchestrator,
        LLMProvider,
        build_cron_ack_block,
        inject_cron_result_to_dashboard,
        logger,
        prefetch_cron_history,
        redact_credentials,
        redact_exfiltration_urls,
        render_for_slack,
        sel,
    )


async def _deliver_cron_agent_result(
    self: GatewayOrchestrator,
    job: CronJob,
    session_key: str,
    client: LLMProvider,
    result_text: str,
    rh: str,
    _ctx_reading: Any,
    _turn_stats: dict[str, Any] | None,
) -> str:
    """Deliver a single-agent cron result: dedup, silent, dashboard, channel, Slack.

    Runs inside the turn's ``try``, so a delivery error reaches the turn's
    failure handler as before. Returns the result the job stores.
    """
    if rh == job.last_posted_hash:
        job.consecutive_dupes += 1
        # Time-based reminder: re-post after 24h so persistent identical
        # results don't go unnoticed indefinitely.
        if time.time() - job.last_posted_at >= _SUCCESS_REMINDER_SECS:
            # NB: consecutive_dupes is captured here before the reset
            # at the post-delivery state update further below.
            result_text = (
                f"⚠️ Cron '{job.name}' has produced the same result"
                f" {job.consecutive_dupes} times in a row:\n\n{result_text}"
            )
        else:
            logger.info(
                "Cron '%s': duplicate result #%d — suppressing delivery",
                job.name,
                job.consecutive_dupes,
            )
            if self.dashboard_state:
                redacted_for_dash, _ = redact_exfiltration_urls(result_text)
                redacted_for_dash, _ = redact_credentials(redacted_for_dash)
                title = f"🔇 Cron: {job.name} (dup #{job.consecutive_dupes})"
                title, _ = redact_exfiltration_urls(title)
                title, _ = redact_credentials(title)
                self.dashboard_state.notify(
                    "cron",
                    title,
                    redacted_for_dash,
                    meta={"job_id": job.id},
                )

            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_dedup_suppress",
                outcome="suppressed",
                downstream_service="none",
            )
            # Still inject into dashboard slot even when Slack is suppressed
            if (
                self.dashboard_state
                and job.persistent_session
                and not job.hide_in_chat
                and self.dashboard_state.has_slot(f"cron-{job.id}")
            ):
                inject_cron_result_to_dashboard(
                    self.dashboard_state,
                    job,
                    result_text,
                    history=await prefetch_cron_history(self.dashboard_state, job.id),
                    context_reading=_ctx_reading,
                    turn_stats=_turn_stats,
                )
            return result_text

    if job.silent:
        logger.info("Cron job '%s' silent — suppressing auto-delivery", job.name)

        sel().log_tool_invocation(
            session_key=f"cron:{job.id}",
            tool_name="cron_silent_suppress",
            outcome="suppressed",
            downstream_service="none",
        )
        # Still inject into dashboard slot even when silent
        if (
            self.dashboard_state
            and job.persistent_session
            and not job.hide_in_chat
            and self.dashboard_state.has_slot(f"cron-{job.id}")
        ):
            inject_cron_result_to_dashboard(
                self.dashboard_state,
                job,
                result_text,
                history=await prefetch_cron_history(self.dashboard_state, job.id),
                context_reading=_ctx_reading,
                turn_stats=_turn_stats,
            )
        return result_text

    if self.dashboard_state:
        # Inject into slot BEFORE notification so has_slot() is true for notify_meta.
        # hide_in_chat=True keeps the cron out of the active session list — the
        # result still reaches Slack/bell below, and the run stays visible in the
        # History tab via the cron execution-history store (CronHistoryStore, written
        # unconditionally by the executor and surfaced at GET /api/crons/{id}/history).
        # NOTE: the cron:{id} dashboard conversation_log is written ONLY by
        # inject_cron_result_to_dashboard (gated off here for hidden crons), so it is
        # intentionally empty for a hidden cron — it exists solely to feed a dashboard
        # follow-up turn, which a no-slot cron never has. Do NOT rely on cron:{id} for
        # hidden-cron result persistence; get_history() is the source of truth.
        # This is the only slot *creator* site (get_or_create_slot); the dedup/silent
        # paths above only re-inject into an already-existing slot via has_slot(), so
        # they self-no-op when hide_in_chat is True.
        if job.persistent_session and not job.hide_in_chat:
            history = (
                await asyncio.to_thread(
                    self.dashboard_state.conversation_log.read_messages,
                    f"cron:{job.id}",
                )
                if self.dashboard_state.conversation_log
                else []
            )
            inject_cron_result_to_dashboard(
                self.dashboard_state,
                job,
                result_text,
                history=history,
                context_reading=_ctx_reading,
                turn_stats=_turn_stats,
            )
        redacted_for_dash, _ = redact_exfiltration_urls(result_text)
        redacted_for_dash, _ = redact_credentials(redacted_for_dash)
        safe_name, _ = redact_exfiltration_urls(job.name)
        safe_name, _ = redact_credentials(safe_name)
        notify_meta: dict[str, str] = {"job_id": job.id}
        # Gate the slot linkage on not hide_in_chat for parity with the
        # three inject sites above. Without this, a job flipped to
        # hide_in_chat=True that still owns an older cron-{id} slot would
        # keep emitting meta.slot (has_slot stays True) → the notification
        # CTA shows "Continue session" pointing at a slot that has
        # stopped receiving results. Gating here forces the no-slot "View last
        # result" CTA, which lazily rebuilds from CronHistoryStore.
        if (
            job.persistent_session
            and not job.hide_in_chat
            and self.dashboard_state.has_slot(f"cron-{job.id}")
        ):
            notify_meta["slot"] = f"cron-{job.id}"
        self.dashboard_state.notify(
            "cron",
            f"Cron: {safe_name}",
            redacted_for_dash,
            meta=notify_meta,
        )
    # A job belongs to ONE surface: the conversation that scheduled
    # it. Delivering to both would notify an operator twice for one
    # run, which is how notifications become noise people stop
    # reading. So the channel leg is attempted FIRST and Slack stands
    # down only on a CONFIRMED delivery: a predicate saying a channel
    # *would* take it is not the same claim, and standing Slack down
    # on that loses the result outright when the channel send is
    # refused by governance or fails on the wire. An explicit
    # `job.channel` is a destination the user pinned, so it wins over
    # both. Every job that exists today has no channel origin, so
    # this is inert for current installs.
    channel_delivered = False
    if not job.channel:
        try:
            channel_delivered = await self._deliver_cron_to_channel(
                job.session_key,
                f"⏰ Cron: {job.name}\n\n{result_text}",
                actor_key=session_key,
            )
        except Exception:
            # The job SUCCEEDED. Letting a delivery error reach the
            # outer handler would record a failure and march the job
            # toward auto-pause on a messaging fault. Slack still
            # runs below, because nothing was delivered here.
            logger.error(
                "Cron job '%s': channel delivery failed (job succeeded)",
                job.name,
                exc_info=True,
            )
    if channel_delivered:
        # Same dedup contract as the Slack leg: the hash advances
        # once the result reached someone. Without this a Slack-less
        # install never advances it, so the suppression branch can
        # never fire and an unchanged result is re-delivered forever.
        self._record_cron_delivery(job, rh)
        # No reply-anchor write to mirror the Slack branch's
        # set_thread/set_channel below, deliberately. Those two record
        # where a Slack cron post LANDED so a later subagent completion
        # under ``cron:{id}`` can be threaded onto it; the channel leg
        # learns nothing equivalent at send time -- its conversation was
        # already resolved FROM the creating session's own durable
        # origin/mirror link, which every later delivery re-reads.
        # Routing a cron's subagent completions back to the creating
        # channel needs a ``cron:{id}`` -> creating-key edge instead,
        # which is its own change; half of it here would look like
        # parity without being it.
    if self.slack and not channel_delivered:
        try:
            # Retry only open_dm (transient Slack API errors).
            # Delivery (post_blocks/post_message) is NOT retried to avoid duplicates.
            channel = job.channel
            if not channel and (job.created_by or self._owner_id):
                channel = await self._open_dm_with_retry(job.created_by or self._owner_id, job.name)
            if channel:
                # ── Extract embedded local images and upload them
                # natively (mirror the chat renderer's on_done seal).
                # The cron delivery path posts text only; without
                # this an inline ![alt](/abs/path.png) in the reply
                # ships as literal Markdown and Slack shows nothing.
                # within_root is the cron session's resolved cwd
                # (client.cwd — the agent's workspace), which bounds
                # extraction to files the session may read.
                # extract_outbound_with_audit is the SAME admission
                # seal the renderer uses: it writes the mandatory SEL
                # record for both admitted and refused files and folds
                # any refusal note into the text, so a cron's file
                # egress leaves the same audit trail as a chat reply.
                #
                # ``_cron_body`` is a SEPARATE Slack-only local: the
                # extraction strips the local path and may fold in a
                # refusal note, neither of which belongs in the value
                # the callback RETURNS. That return value is the job's
                # stored result and feeds the next run's "[Previous
                # run result]" prompt, so ``result_text`` is left
                # untouched and only the delivered body is rewritten.
                _cron_upload_files: list = []
                _cron_root = getattr(client, "cwd", "") or ""
                _cron_body = result_text
                if _cron_root:
                    from kiro_crew.slack.files import (
                        extract_outbound_with_audit,
                    )

                    _cron_body, _cron_files, _ = await extract_outbound_with_audit(
                        result_text,
                        within_root=_cron_root,
                        audit_caller=session_key or channel or "cron",
                    )
                    _cron_upload_files = list(_cron_files)
                # The caption is redacted-but-not-converted by
                # render_for_slack's header= seam, which also charges
                # it against the limit. Doing it there rather than
                # here is the point: a cron name is LLM-authored (the
                # agent can create crons via cron_add), and the
                # hand-rolled version of this had already forgotten to
                # redact it once.
                parts = render_for_slack(
                    _cron_body,
                    limit=_CRON_MSG_LIMIT,
                    header=f"⏰ *Cron: {job.name}*\n\n",
                )
                # First part as Block Kit message with ack button
                blocks: list[dict] = [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": parts[0]},
                    },
                ] + build_cron_ack_block(job.id)
                parent_ts = await self.slack.post_blocks(channel, blocks, parts[0], job.thread_ts)
                thread_root = job.thread_ts or parent_ts
                # Store the thread so subagents can route replies here
                # -- but CLAIM the thread for inbound routing only when
                # the cron POSTED it itself (no ``job.thread_ts``, so
                # ``thread_root`` is this run's own ``parent_ts``).
                #
                # ``set_thread`` -> ``set_slack_link("cron:<id>", ts)``
                # is a NON-self-derived claim (``cron:`` is not in
                # ``CHANNEL_SESSION_NAMESPACES``), so it EVICTS whatever
                # else owns ``ts`` in ``_thread_to_session``. For an
                # inherited/explicit ``job.thread_ts`` -- a human's own
                # Slack thread, keyed ``slack:<ts>`` -- that eviction is
                # permanent: the self-link that would restore the owner
                # (``transport_dispatch.py``) only fires on an UNCLAIMED
                # thread, so the human's next message in their own thread
                # would run in ``cron:<id>`` forever. A cron must never
                # take over a thread it did not create. The channel bind
                # is harmless (``set_channel`` writes an empty ``ts`` for
                # an unclaimed cron, which never evicts) and overflow
                # parts below still thread under ``thread_root``.
                if thread_root and self.sessions:
                    if not job.thread_ts:
                        await self.sessions.set_thread(session_key, thread_root)
                    else:
                        # An inherited/explicit ``job.thread_ts`` must
                        # not be stored as ``cron:<id>``'s anchor (the
                        # claim above evicts the human owner), and any
                        # anchor a prior top-level run left is cleared
                        # to empty so ``set_channel`` keeps it empty
                        # and the readers resolve the job's own
                        # ``thread_ts`` through ``_cron_outbound_thread``
                        # instead of a stale ts. ``set_thread(key, "")``
                        # is an empty, unindexed anchor and evicts no
                        # owner.
                        await self.sessions.set_thread(session_key, "")
                    await self.sessions.set_channel(session_key, channel)
                # Overflow parts as threaded follow-up messages
                for part in parts[1:]:
                    await self.slack.post_message(channel, part, thread_root)
                # Upload any extracted local images natively into the
                # same thread (bytes travel, not the path — every gate
                # in outbound_files was already applied). The reporting
                # variant posts a redacted note into the thread for any
                # upload Slack refuses, so a failed upload is never a
                # silent loss (the image markup is already stripped).
                if _cron_upload_files:
                    from kiro_crew.slack.files import (
                        upload_outbound_files_reporting,
                    )

                    await upload_outbound_files_reporting(
                        self.slack,
                        channel,
                        thread_root or "",
                        _cron_upload_files,
                    )
                # Dedup state: only advance after confirmed delivery.
                self._record_cron_delivery(job, rh)
            else:
                logger.warning("Cron '%s': no channel resolved, skipping notification", job.name)
        except Exception as slack_exc:
            logger.error(
                "Cron job '%s': Slack delivery failed (job succeeded)",
                job.name,
                exc_info=True,
            )
            if self.dashboard_state:
                exc_msg, _ = redact_exfiltration_urls(str(slack_exc))
                exc_msg, _ = redact_credentials(exc_msg)
                self.dashboard_state.notify(
                    "cron",
                    f"Cron: {job.name}",
                    f"⚠️ Job completed but Slack delivery failed: {exc_msg}",
                    meta={"job_id": job.id},
                )
    return result_text


# Slack Block Kit section.text hard limit is 3000 chars.
# We split cron output at this boundary so each chunk fits in a section block.
_CRON_MSG_LIMIT = 3000
