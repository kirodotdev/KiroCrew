"""Tell the user a cron run failed, once per distinct failure.

The one spelling of the duplicate check, the Slack-safe fence, the two delivery legs
and the dedup advance, and their two callers: the command, script and denial alert,
and the single-agent turn's failure path, which also charges the run.

Orchestrator fields read: ``_owner_id``, ``dashboard_state``, ``slack``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import socket
    import time

    from kiro_crew.slack.gateway import (
        _CRON_FAILURE_DETAIL_CAP,
        _FAILURE_REMINDER_SECS,
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
        CronJob,
        GatewayOrchestrator,
        _charge_failure,
        _result_hash,
        acp_error_after_tool_activity,
        acp_error_is_session_not_found,
        append_fallback_story,
        channel_namespace_of,
        escape_mrkdwn,
        logger,
        redact,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
    )


def _failure_alert_is_duplicate(self: GatewayOrchestrator, job: CronJob, failure_hash: str) -> bool:
    """Whether this failure repeats the last one inside the reminder window.

    A job that fails identically every minute alerts once per
    ``_FAILURE_REMINDER_SECS`` rather than once per fire. Both surfaces read
    the SAME ``last_failure_hash`` / ``last_failure_at`` pair; a job is
    exactly one kind, so the two writers never interleave on one job.
    """
    return (
        failure_hash == job.last_failure_hash
        and time.time() - job.last_failure_at < _FAILURE_REMINDER_SECS
    )


def _slack_safe_fenced(self: GatewayOrchestrator, text: str) -> str:
    """Make *text* safe to interpolate into a Slack mrkdwn code fence.

    Two hazards, one of which escaping alone does not cover. Slack PARSES
    entity markup, and both halves of a failure alert are attacker-shaped --
    the job name is user-authored and the reason carries subprocess output, so
    a job named ``<!channel>`` would notify a whole channel the moment it
    failed. And three backticks inside the reason would CLOSE the fence early
    and hand the remainder to the parser as markup, which escaping does not
    prevent.

    Slack-facing sinks only. The dashboard bell is not a mrkdwn sink and
    escaping there would render a literal ``&lt;``.
    """
    return escape_mrkdwn(text).replace("```", "'''")


async def _deliver_failure_alert(
    self: GatewayOrchestrator,
    job: CronJob,
    *,
    mrkdwn: str,
    plain: str,
    actor_key: str,
    silent: bool = False,
) -> tuple[bool, bool, bool]:
    """Deliver a failure alert on exactly ONE surface.

    Returns ``(channel_delivered, slack_delivered, slack_failed)``.
    ``slack_failed`` names a real delivery exception, never an unresolved
    channel -- the caller's dedup decision turns on that split.

    The one-surface rule: when the conversation that scheduled the job will
    hear about the failure, the owner DM would be a second alert for one
    event. An explicit ``job.channel`` is a destination the user pinned and
    still wins, so the channel leg is skipped for it -- without that guard the
    channel leg reports a delivery, stands the Slack leg down, and the alert
    lands on the origin conversation instead of the destination the user
    named.

    *mrkdwn* and *plain* are composed by the caller and are deliberately two
    strings: Slack's markup is not another transport's dialect, so a shared
    string would show ``&lt;`` and stray backticks to a channel reader.

    Never raises. Both callers are inside an ``except`` whose exception is the
    real story, and one of them re-raises it.
    """
    channel_delivered = False
    slack_delivered = False
    slack_failed = False
    if not silent and not job.channel:
        try:
            channel_delivered = await self._deliver_cron_to_channel(
                job.session_key, plain, actor_key=actor_key
            )
        except Exception:
            logger.error(
                "Cron '%s': channel failure-alert delivery failed",
                job.name,
                exc_info=True,
            )
    if self.slack and not silent and not channel_delivered:
        try:
            channel = job.channel
            if not channel and (job.created_by or self._owner_id):
                channel = await self._open_dm_with_retry(job.created_by or self._owner_id, job.name)
            if channel:
                await self.slack.post_message(channel, mrkdwn)
                slack_delivered = True
            else:
                logger.warning("Cron '%s': no channel resolved for failure alert", job.name)
        except Exception:
            slack_failed = True
            logger.error(
                "Cron '%s': Slack failure-alert delivery failed",
                job.name,
                exc_info=True,
            )
    return channel_delivered, slack_delivered, slack_failed


def _advance_failure_dedup(
    self: GatewayOrchestrator,
    job: CronJob,
    failure_hash: str,
    *,
    channel_delivered: bool,
    slack_failed: bool,
) -> None:
    """Advance the dedup anchor once the reason actually reached someone.

    "No channel available" counts as delivered -- the bell rang -- so a
    Slack-less install does not re-notify the dashboard on every fire. A
    confirmed channel delivery counts for the same reason: the reason reached
    the user even when the Slack leg threw. Only a REAL Slack exception holds
    the anchor back, so the next identical failure tries again.

    Only the dedup fields move here. ``record_failure()`` has its own owner
    per run and is deliberately not touched.
    """
    if channel_delivered or not slack_failed:
        job.last_failure_hash = failure_hash
        job.last_failure_at = time.time()


async def _alert_cron_failure(
    self: GatewayOrchestrator, job: CronJob, detail: str, *, denied: bool = False
) -> None:
    """Tell the user WHY a script/command cron run failed or was denied.

    The script and command paths signal failure by mutating the job
    (``last_status="error"`` + ``last_error``) and returning normally, so
    they never reached the message path's failure alert below — the reason
    existed only in the gateway log and in a dashboard field nobody is
    watching when the notification they expected simply never arrives. A
    job whose every run dies on a startup-time ``RuntimeError`` therefore
    looked idle rather than broken.

    Deliberately NOT a delivery of the run's *result*: this is a bell plus
    a DM carrying the reason, never an injected turn like
    :func:`_deliver_script_result`. A job failing on its own schedule must
    not spend a model turn per failure, and an injected turn is exactly how
    a failing cron would amplify itself.

    Contract, so the two failure surfaces cannot drift:

    * ``record_failure()`` is NOT called here. Every call site already
      counted the run (or deliberately did not, for a policy denial), and
      the counter has one owner per run — see :func:`_apply_gate_verdict`.
      Callers therefore alert AFTER counting, so ``consecutive_failures``
      reads true if a future body wants it.
    * Dedup reuses the SAME ``last_failure_hash`` / ``last_failure_at``
      fields as the message path. A job is exactly one kind, so the two
      writers never interleave on one job, and a run that fails
      identically every minute alerts once per
      ``_FAILURE_REMINDER_SECS`` instead of once per fire.
    * Never raises. Every call site is inside an ``except`` block whose
      exception is the real story; an alert that failed must not replace
      it. Cancellation still propagates (``CancelledError`` is a
      ``BaseException``).
    """
    try:
        if job.silent:
            # Silent jobs still execute and still count toward auto-pause;
            # only the user-facing surfaces are suppressed.
            return
        text = redact(detail or "")
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
        text = text.strip()[:_CRON_FAILURE_DETAIL_CAP] or "no reason reported"
        # job.name is user-controlled and the reason can carry subprocess
        # output, so both are scrubbed once, ahead of either surface and
        # of either branch below.
        label = redact(job.name)
        label, _ = redact_exfiltration_urls(label)
        label, _ = redact_credentials(label)
        mark = "⛔" if denied else "❌"
        headline = "Blocked by policy" if denied else "Run failed"
        # Denials and failures hash apart so a policy denial does not read
        # as a dup of a same-worded crash (and vice versa).
        fh = _result_hash(f"{'denied' if denied else 'failed'}:{text}")
        if self._failure_alert_is_duplicate(job, fh):
            logger.info(
                "Cron '%s': duplicate failure alert suppressed (%s)",
                job.name,
                "denied" if denied else "failed",
            )
            # Same split the message path draws: the LOCAL bell still
            # rings (marked suppressed, so a user watching the feed sees
            # the job is still down) and only the Slack DM is withheld.
            try:
                if self.dashboard_state:
                    self.dashboard_state.notify(
                        "cron",
                        f"🔇 Cron: {label} (repeat)",
                        f"{mark} Still failing (suppressed — same reason):\n{text}",
                        meta={"job_id": job.id, "failure_hash": fh},
                    )
            except Exception:
                logger.debug(
                    "Dashboard notify failed in cron run-failure suppress path",
                    exc_info=True,
                )
            return
        try:
            if self.dashboard_state:
                self.dashboard_state.notify(
                    "cron",
                    f"Cron: {label}",
                    f"{mark} {headline}:\n{text}",
                    meta={"job_id": job.id, "failure_hash": fh},
                )
        except Exception:
            logger.debug("Dashboard notify failed in cron run-failure alert path", exc_info=True)
        # Name the machine for the same reason the message path does: a
        # laptop and a cloud desktop can both run Kiro Crew, and the
        # alert is the only place the user learns which one failed. Read
        # once, ahead of both delivery legs, so they cannot disagree.
        host = socket.gethostname().split(".")[0]
        # Slack PARSES entity markup and a fence can be closed early by
        # the reason's own backticks; `_slack_safe_fenced` is the one
        # spelling of that hardening. `label` and `text` are already
        # scrubbed above, and the transport leg redacts again at egress.
        safe_label = self._slack_safe_fenced(label)
        safe_text = self._slack_safe_fenced(text)
        msg = (
            f"⏰ *Cron: {safe_label}* {mark} "
            f"_{headline} on {escape_mrkdwn(host)}_\n```{safe_text}```"
        )
        msg, _ = redact_exfiltration_urls(msg)
        msg, _ = redact_credentials(msg)
        # Plain twin for a non-Slack transport: mrkdwn is not another
        # channel's dialect, so a shared string would show `&lt;` and
        # stray backticks there.
        plain = f"⏰ Cron: {label} {mark} {headline} on {host}\n{text}"
        # Silent jobs returned above, so this leg is never suppressed here.
        channel_delivered, slack_delivered, slack_failed = await self._deliver_failure_alert(
            job,
            mrkdwn=msg,
            plain=plain,
            actor_key=f"cron:{job.id}",
        )
        self._advance_failure_dedup(
            job, fh, channel_delivered=channel_delivered, slack_failed=slack_failed
        )
        try:
            # Name every surface the alert actually left on, so the trail
            # does not read "none" for a run answered on Discord.
            surfaces = ["slack"] if slack_delivered else []
            if channel_delivered:
                surfaces.append(channel_namespace_of(job.session_key))
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_run_failure_alert",
                outcome="denied" if denied else "alerted",
                downstream_service=",".join(surfaces) or "none",
            )
        except Exception:
            logger.debug("SEL logging failed in cron run-failure alert path", exc_info=True)
    except Exception:
        logger.warning("Cron '%s': run-failure alert failed", job.name, exc_info=True)


async def _cron_single_failure(
    self: GatewayOrchestrator,
    job: CronJob,
    exc: Exception,
    session_key: str,
    _gate_counted: bool,
    _job_owns_failure: bool,
    _hand_over_streak: bool,
) -> None:
    """Alert a failed single-agent cron run, or suppress a duplicate, and charge it.

    Called from the turn's failure handler, which re-raises *exc* afterwards.
    Every await precedes the charge, so a cancellation mid-alert counts nothing.
    """
    # ── Failure dedup: suppress repeated identical crash notifications ──
    # A chain-exhaustion failure carries the fallback story on the
    # exception (llm_helpers.FALLBACK_STORY_ATTR); append it so the
    # alert names the whole walk, not just the last candidate's
    # error.
    # Redact over the FULL error text BEFORE any truncation — a cap
    # applied first can cut a credential at the boundary, leaving a
    # fragment the redaction regexes cannot match. The story is
    # redacted+capped centrally in fallback_story_of.
    _exc_text = f"{type(exc).__name__}: {exc}"
    _snf_text = str(exc)
    if (
        acp_error_is_session_not_found(exc)
        and SESSION_NOT_FOUND_GIVE_UP_TEXT not in _snf_text
        and SESSION_NOT_FOUND_NOT_REPLAYED_TEXT not in _snf_text
    ):
        # Name the outcome, not just the backend's words: the retry
        # was skipped after a tool ran, or it ran and failed too.
        _exc_text = (
            f"{SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} {_exc_text}"
            if acp_error_after_tool_activity(exc)
            else f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {_exc_text}"
        )
    _exc_text, _ = redact_exfiltration_urls(_exc_text)
    _exc_text, _ = redact_credentials(_exc_text)
    # Delivery detail: the budget trims the ERROR part to leave the
    # story room (a verbose backend error must not evict the walk),
    # floored at half the cap — an oversized story trims its own
    # tail past that point instead of evicting the error.
    exc_detail = append_fallback_story(_exc_text, exc, budget=_CRON_FAILURE_DETAIL_CAP)
    # Dedup hashes the STORY-FREE error text: the walk can differ
    # between two occurrences of the same backend error (a
    # candidate momentarily unadvertised changes `walked`; a
    # skipped walk has no story at all), and a hash keyed on it
    # would miss the duplicate and re-page the user.
    fh = _result_hash(_exc_text)
    # Kept separately from the suppression gate below: `is_dup` still
    # selects the "still failing" wording on an alert that DOES go out
    # because the reminder window has expired.
    is_dup = fh == job.last_failure_hash
    if self._failure_alert_is_duplicate(job, fh):
        # record_failure() is the counter's sole owner: a suppressed
        # duplicate is still a failed run, so it must count toward
        # the auto-pause threshold like every other failure path —
        # unless the gate verdict already counted THIS run, in which
        # case counting again would pause on arithmetic rather than
        # on five distinct failures, or the failure belonged to a
        # shared runtime rather than to this job.
        if not _gate_counted and _job_owns_failure:
            _charge_failure(job, _hand_over_streak)
        if job.auto_paused:
            logger.warning(
                "Cron '%s' auto-paused after %d consecutive failures",
                job.name,
                job.consecutive_failures,
            )
        logger.info(
            "Cron '%s': duplicate failure #%d — suppressing Slack",
            job.name,
            job.consecutive_failures,
        )
        # Dashboard notify is best-effort — never mask the original
        # exception if notification itself fails.
        try:
            if self.dashboard_state and not job.silent:
                title = f"🔇 Cron: {job.name} (dup failure #{job.consecutive_failures})"
                title, _ = redact_exfiltration_urls(title)
                title, _ = redact_credentials(title)
                self.dashboard_state.notify(
                    "cron",
                    title,
                    f"❌ Job failed (suppressed — same error):\n{exc_detail}",
                    meta={"job_id": job.id, "failure_hash": fh},
                )
        except Exception:
            logger.debug("Dashboard notify failed in cron failure suppress path", exc_info=True)
        # SEL logging is best-effort — never mask the original
        # exception if audit logging itself fails.
        try:

            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_failure_dedup_suppress",
                outcome="suppressed",
                downstream_service="none",
            )
        except Exception:
            logger.debug(
                "SEL logging failed in cron failure suppress path",
                exc_info=True,
            )
        return
    # First failure (or fresh failure after reminder window) — alert.
    # Dashboard notify is best-effort — never mask the original
    # exception if notification itself fails.
    try:
        if self.dashboard_state and not job.silent:
            alert_title = f"Cron: {job.name}"
            alert_title, _ = redact_exfiltration_urls(alert_title)
            alert_title, _ = redact_credentials(alert_title)
            # Carry the reason, matching the suppressed-duplicate body
            # below: without it the FIRST alert — the one the user
            # actually reads — was the least informative of the two.
            self.dashboard_state.notify(
                "cron",
                alert_title,
                f"❌ Job failed:\n{exc_detail}",
                meta={"job_id": job.id, "failure_hash": fh},
            )
    except Exception:
        logger.debug("Dashboard notify failed in cron failure alert path", exc_info=True)
    # Include the machine hostname so multi-gateway setups (e.g. a
    # laptop + a cloud desktop both running Kiro Crew) can tell which
    # machine's session failed. This is framework-level: the ❌ DM
    # can fire before any prompt logic runs (e.g. a session-startup
    # credential failure), so the machine name must come from here,
    # not from inside the cron prompt.
    host = socket.gethostname().split(".")[0]
    # Slack PARSES entity markup here, and job.name is user-authored:
    # a job named `<!channel>` notifies the whole channel on failure.
    # Same sink and same source as the script/command alert, so it
    # goes through the one shared spelling of that hardening.
    safe_name = self._slack_safe_fenced(job.name)
    # Carry the reason here too. The dashboard body above and the
    # script/command DM both do, so leaving this one at "check logs"
    # made the DM the only failure surface that still withheld what
    # the caller already knows.
    safe_reason = self._slack_safe_fenced(exc_detail)
    # +1 only when the charge below will actually land. This run's
    # failure is recorded after the awaited Slack attempt, so the
    # display count has to add it explicitly -- but it is now gated on
    # _job_owns_failure, and an exempted run that still claimed
    # "N+1 consecutive failures" would report a number the counter
    # driving auto-pause never reached.
    _display_bump = 1 if (_job_owns_failure and not _gate_counted) else 0
    if is_dup:
        fail_msg = (
            f"⏰ *Cron: {safe_name}* ❌ _Job still failing on {escape_mrkdwn(host)}"
            f" ({job.consecutive_failures + _display_bump} consecutive failures)"
            f" — check logs._\n```{safe_reason}```"
        )
    else:
        fail_msg = (
            f"⏰ *Cron: {safe_name}* ❌ "
            f"_Job failed on {escape_mrkdwn(host)} — check logs._\n"
            f"```{safe_reason}```"
        )
    # Never trust interpolated content (job.name is user-controlled):
    # scrub exfiltration URLs + credentials before it reaches Slack,
    # mirroring the dashboard alert_title redaction above.
    fail_msg, _ = redact_exfiltration_urls(fail_msg)
    fail_msg, _ = redact_credentials(fail_msg)
    # Channel-neutral twin of ``fail_msg``: the same sentence with no
    # mrkdwn. Composed separately rather than reusing the escaped
    # form because Slack's markup is not another channel's dialect --
    # a fence and a ``&lt;`` would reach that reader literally -- and
    # separately from ``render_for_slack`` is how the success leg
    # composes its own channel string too.
    if is_dup:
        channel_fail_msg = (
            f"⏰ Cron: {job.name} ❌ Job still failing on {host}"
            f" ({job.consecutive_failures + _display_bump} consecutive failures)"
            f" — check logs.\n{exc_detail}"
        )
    else:
        channel_fail_msg = (
            f"⏰ Cron: {job.name} ❌ Job failed on {host} — check logs.\n" f"{exc_detail}"
        )
    channel_fail_msg, _ = redact_exfiltration_urls(channel_fail_msg)
    channel_fail_msg, _ = redact_credentials(channel_fail_msg)
    # Silent jobs still execute but suppress notifications (UI bells
    # AND Slack DMs). The failure is still logged at warning level
    # and counted toward auto-pause above — we just skip
    # user-facing noise.
    # One spelling of the one-surface rule and both delivery legs,
    # shared with the script/command alert. `channel_fail_msg` rather
    # than `fail_msg` for the channel leg: that string is mrkdwn, no
    # other transport parses it, and the channel form also carries the
    # repeat-failure wording and both egress redaction passes.
    #
    # Placed before record_failure() deliberately: every await in this
    # handler must precede the counter, so a cancellation mid-alert
    # cannot leave the run counted twice.
    channel_delivered, slack_delivered, slack_failed = await self._deliver_failure_alert(
        job,
        mrkdwn=fail_msg,
        plain=channel_fail_msg,
        actor_key=session_key,
        silent=job.silent,
    )
    # record_failure() is the counter's sole owner: it continues an
    # accumulation another writer (gate verdict, timeout) already
    # built up instead of restarting at 1, and it is deliberately
    # NOT gated on the alert's Slack delivery above — the run
    # failed either way, and a job whose failure alerts also fail
    # must still reach the auto-pause threshold. It runs AFTER the
    # awaited Slack attempt so a timeout cancelling this handler
    # mid-alert cannot leave the run counted here AND again by the
    # timeout handler. For the same reason it defers when the gate
    # verdict already counted THIS run, or when the failure belonged
    # to a runtime this job was sharing rather than to the job.
    if not _gate_counted and _job_owns_failure:
        _charge_failure(job, _hand_over_streak)
    if job.auto_paused:
        logger.warning(
            "Cron '%s' auto-paused after %d consecutive failures",
            job.name,
            job.consecutive_failures,
        )
    # One spelling of the advance rule, shared with the
    # script/command alert: the anchor moves once the reason reached
    # someone, and only a REAL Slack exception holds it back.
    self._advance_failure_dedup(
        job, fh, channel_delivered=channel_delivered, slack_failed=slack_failed
    )
    # The SEL record was nested INSIDE the advance condition before
    # this refactor, so a Slack exception suppressed the audit line as
    # well as the anchor. Preserved verbatim rather than quietly
    # widened -- whether the audit should be unconditional (the
    # script/command alert logs it either way) is a behaviour question,
    # not a consolidation one. Restating the condition is what makes
    # that gating visible instead of implied by indentation.
    if channel_delivered or not slack_failed:
        # SEL logging is best-effort — never mask the original
        # exception if audit logging itself fails.
        try:
            # Name every surface the alert actually left on, so the
            # trail does not read "none" for a crash sent to Discord.
            surfaces = ["slack"] if slack_delivered else []
            if channel_delivered:
                surfaces.append(channel_namespace_of(job.session_key))
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_failure_alert",
                outcome="suppressed" if job.silent else "alerted",
                downstream_service=",".join(surfaces) or "none",
            )
        except Exception:
            logger.debug(
                "SEL logging failed in cron failure alert path",
                exc_info=True,
            )
