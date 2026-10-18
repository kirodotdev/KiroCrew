"""Charge a runtime death only to whoever caused it.

A death on a shared runtime is a process event: the session that saw it did not
necessarily cause it. The cron gate verdict, the cron failure attribution with
the substitute streak's hand-over, the charge itself, and the death arms of a
completion's injection all ask ``runtime_death.caused_by_this_session`` here,
beside ``note_shared_death``.

Orchestrator fields read: ``sessions``, ``subagent_mgr``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.slack.gateway import (
        _AUTO_PAUSE_THRESHOLD,
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
        AcpError,
        AcpProcessDied,
        CronJob,
        GatewayOrchestrator,
        PromptBusyExhaustedError,
        SubagentInfo,
        _GateTally,
        acp_error_after_tool_activity,
        acp_error_is_session_not_found,
        logger,
        runtime_death,
        stream_and_collect,
    )


def _apply_gate_verdict(job: CronJob, tally: _GateTally) -> bool:
    """Record a finished cron run's success or failure from its gate outcomes.

    Shared by both cron agent paths so their verdicts cannot drift. Mutating
    ``last_status`` is how the non-raising cron paths signal failure:
    ``CronScheduler._execute`` keeps an explicit "error" rather than
    overwriting it with "ok".

    Returns whether this call counted a failure, because a run must increment
    ``consecutive_failures`` **at most once**. On the single-agent path the
    delivery work that follows (dashboard broadcast, Slack post) runs inside a
    ``try`` whose handler counts too, so a blocked turn whose delivery then
    failed would otherwise reach the auto-pause threshold in three runs rather
    than five — pausing on arithmetic instead of on evidence.

    Three outcomes, not two. A run with every call refused is a failure and
    is counted. A run with SOME call refused keeps ``last_status = "error"``
    with the refusal as its reason but is counted in neither direction: the
    approved calls prove the job can work, so it must not march toward
    auto-pause, and the lost call means it did not succeed, so it must not
    read as ``ok`` and must not reset a failure streak or the failure-alert
    dedup the way a success does. Only a run with no refusal at all records a
    success.
    """
    if tally.all_blocked:
        # Nothing the model attempted was permitted, so the run accomplished
        # nothing however plausible its reply reads. A success resets
        # consecutive_failures and clears auto_paused, so recording one here
        # would keep a structurally-failing job firing on its schedule forever.
        job.last_status = "error"
        job.last_error = tally.refusal_summary()
        job.record_failure()
        if job.auto_paused:
            logger.warning(
                "Cron '%s': auto-paused after %d consecutive failures",
                job.name,
                job.consecutive_failures,
            )
        return True
    if tally.partially_blocked:
        # The run did work AND lost work. "error" is the one non-ok status the
        # row, cron_list and the history record understand, and the reason
        # names the refused call. Neither counter moves: not record_failure(),
        # because the approved calls are evidence the job can work; not
        # record_success(), because that would reset a failure streak and the
        # failure-alert dedup on a run that did not fully succeed.
        job.last_status = "error"
        job.last_error = tally.refusal_summary()
        return False
    # Clear failure dedup on any success, regardless of whether the success
    # result itself is a dup. A successful run means the job recovered — next
    # failure should always alert fresh. record_success() owns the reset now, so
    # every kind's success path gets it rather than only this one.
    job.record_success()
    # Same reason record_success resets consecutive_failures: a run that
    # worked proves the job can work, so the shared-death streak that
    # substitutes for that counter is reset with it. Without this the
    # streak is a LIFETIME total, so after enough deaths the bound is
    # permanently tripped and the exemption silently stops applying.
    runtime_death.clear_shared_deaths(f"cron:{job.id}")
    return False


def _cron_failure_attribution(job: CronJob, _run_provider: object | None) -> tuple[bool, bool]:
    """Whether a failed cron run is its job's own failure, and whether its streak hands over.

    Returns ``(job_owns_failure, hand_over_streak)`` for :func:`_charge_failure`.
    """
    # Was this run's failure this JOB's, or its runtime's? A cron
    # runtime hosts the job's own sub-agents, so a death there is a
    # process event several accounts witness -- and five of them
    # auto-pause a job that has done nothing wrong, which is the
    # threshold this guard protects. Read the record the death wrote
    # once, through the provider the manager still holds for this key
    # (never a pid, which a session must not be able to name).
    # Unattributable, or a runtime this job was alone on: counted
    # exactly as before.
    _job_owns_failure = runtime_death.caused_by_this_session(_run_provider)
    # Set only when the substitute bound reaches its limit below, and
    # consumed only beside record_failure(), so the three steps of the
    # hand-over stay contiguous.
    _hand_over_streak = False

    if not _job_owns_failure:
        # Not charging is not the same as never charging. A job whose
        # shared runtime dies every run would otherwise re-fire on
        # every tick forever, because record_failure() is the only
        # actuator auto-pause has. So the exemption is bounded by the
        # job's own threshold, counted against the shared runtime
        # rather than against the job -- the same substitute bound the
        # chat runner and the taskrunner use.
        # Keyed to the JOB, never to this run's session key. The
        # counter this substitutes for -- job.consecutive_failures --
        # lives on the job, while a non-persistent job's session key
        # is `cron:{id}:{uuid}`, fresh every run: a streak keyed there
        # would never reach 2, so the bound would never fire and the
        # exemption would be unbounded for exactly the jobs that most
        # need it. A substitute bound is keyed to whatever owns the
        # counter it replaces.
        _shared_streak = runtime_death.note_shared_death(f"cron:{job.id}")
        if _shared_streak >= _AUTO_PAUSE_THRESHOLD:
            # At the limit the substitute bound HANDS OVER its
            # accumulated value to the counter it stood in for,
            # instead of adding a single charge to a counter still at
            # zero. Adding one would deliver twice the bound this
            # claims: the exemption spends the first
            # _AUTO_PAUSE_THRESHOLD deaths, and the job's own counter
            # would then need that many charges again, so a job whose
            # shared runtime dies every run keeps firing for about
            # twice as many runs as a job that was never exempted.
            # The hand-over leaves the one charge below to land ON the
            # threshold, so record_failure() stays the sole owner of
            # both the counter and the pause, and the alert's
            # displayed count is the number that actually paused it.
            #
            # The transfer itself is NOT performed here. It is a
            # three-step move -- raise the counter, forget the streak,
            # charge the failure -- and every await in this handler
            # precedes the counter for the reason the comment beside
            # record_failure() gives. Splitting the move across those
            # awaits leaves a window: `cancel()` is a plain
            # ``task.cancel()`` that charges nothing, so a cancellation
            # during the failure alert would leave the counter at
            # threshold-1 with the streak already erased, and the next
            # failure would pause a job that had not reached the
            # threshold. So the decision is recorded now and the move
            # happens beside record_failure(), where the three steps
            # are contiguous and cannot be torn apart.
            _hand_over_streak = True
            logger.warning(
                "Cron '%s': the runtime it shares has died %d times running — "
                "handing the streak to the job's own counter so it pauses now",
                job.name,
                _shared_streak,
            )
            _job_owns_failure = True
        else:
            logger.warning(
                "Cron '%s': the runtime it shares died (%d running) — recording the "
                "error but not counting it toward auto-pause",
                job.name,
                _shared_streak,
            )
    return _job_owns_failure, _hand_over_streak


def _charge_failure(job: CronJob, _hand_over_streak: bool) -> None:
    """Charge this run's failure, performing any pending hand-over first.

    The two are one indivisible move. The hand-over raises the
    job's counter to one below the threshold so that the charge
    below lands ON it, and forgets the streak whose value it just
    transferred -- and none of those three steps may be separated
    from the others by an await. ``cancel()`` is a plain
    ``task.cancel()`` which, unlike the wake-budget timeout,
    charges nothing on its way out, so a torn move would leave the
    counter and the streak disagreeing with no writer left to
    reconcile them.
    """
    if _hand_over_streak:
        job.consecutive_failures = max(job.consecutive_failures, _AUTO_PAUSE_THRESHOLD - 1)
        runtime_death.clear_shared_deaths(f"cron:{job.id}")
    job.record_failure()


async def _inject_with_retry(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    client,
    msg: str,
    parent_key: str,
    label: str,
) -> str | None:
    """Retry stream_and_collect up to 3 times on AcpError.

    Cancels any orphaned prompt between attempts so the next
    retry doesn't hit 'Prompt already in progress'. A live backend
    that lost the parent's session is answered once by a reset and a
    fresh claim, which re-loads the same backend id; the caller's
    single ``release(parent_key)`` balances against that claim.
    """
    _snf_reloaded = False
    attempt = 0
    while True:
        try:
            response = await stream_and_collect(client, msg, retry_transient=False)
            # The parent's turn consumed the report: the positive
            # acknowledgement an owed expiry report is cleared on.
            info._report_delivered = True
            return response
        except PromptBusyExhaustedError:
            # Provider is dead after exhausting prompt-busy retries.
            # Reset session + notify, same as TimeoutError path.
            logger.error(
                "Subagent %s: provider dead after prompt-busy retries (%s)",
                info.id,
                label,
            )
            # Same ownership question as the AcpProcessDied arm below:
            # the provider being dead is a PROCESS fact, and on a
            # shared runtime the process that died was carrying the
            # parent and its co-tenants too. Resetting the parent for
            # a death it did not cause takes their sessions with it.
            if runtime_death.caused_by_this_session(client):
                try:
                    assert self.sessions is not None
                    await self.sessions.reset(parent_key)
                except Exception:
                    logger.debug(
                        "Failed to reset %s after busy exhaustion",
                        parent_key,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "Subagent %s: the SHARED runtime died — leaving parent %s "
                    "to re-acquire on its own next turn",
                    info.id,
                    parent_key,
                )
            if self.subagent_mgr:
                self.subagent_mgr.notify_injection_failed(
                    info,
                    reason="provider dead after prompt-busy retries",
                )
            return None
        except AcpProcessDied:
            logger.warning(
                "Subagent %s: ACP process died during %s injection",
                info.id,
                label,
            )
            # The parent is reset only when the death was the
            # PARENT's runtime ending. A sub-agent runs on its
            # parent's process, so this handler also catches the
            # child's own death -- and resetting the parent for that
            # tears down a healthy conversation, plus every other
            # tenant's session with it, over a child that failed.
            # A single-tenant runtime is reset exactly as before.
            if runtime_death.caused_by_this_session(client):
                try:
                    assert self.sessions is not None
                    await self.sessions.reset(parent_key)
                except Exception:
                    logger.debug(
                        "Failed to reset %s after process death",
                        parent_key,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "Subagent %s: the SHARED runtime died — leaving parent %s "
                    "to re-acquire on its own next turn",
                    info.id,
                    parent_key,
                )
            if self.subagent_mgr:
                self.subagent_mgr.notify_injection_failed(
                    info,
                    reason="ACP process died",
                )
            return None
        except AcpError as exc:
            if acp_error_is_session_not_found(exc):
                if acp_error_after_tool_activity(exc):
                    raise AcpError(f"{SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} {exc}") from exc
                if _snf_reloaded:
                    raise AcpError(f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {exc}") from exc
                _snf_reloaded = True
                logger.warning(
                    "Subagent %s: backend lost parent session %s during %s "
                    "injection — re-loading it on a fresh runtime",
                    info.id,
                    parent_key,
                    label,
                )
                assert self.sessions is not None
                await self.sessions.reset(parent_key)
                client, _is_new, _resumed = await self.sessions.get_or_create(parent_key)
                continue
            if attempt == 2:
                raise
            logger.warning(
                "Subagent %s %s injection attempt %d failed, retrying",
                info.id,
                label,
                attempt + 1,
            )
            try:
                assert self.sessions is not None
                # Fenced to this task's lease: a turn that claimed the key
                # after a reset is the parent's own and must run on.
                await self.sessions.cancel_current(parent_key, owner_task=asyncio.current_task())
            except Exception:
                logger.debug(
                    "Failed to cancel parent prompt for %s",
                    info.id,
                    exc_info=True,
                )
            await asyncio.sleep(2**attempt)
            attempt += 1
