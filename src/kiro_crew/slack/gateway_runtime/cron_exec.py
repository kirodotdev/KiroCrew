"""Run a command or script cron job and deliver what a script returns.

Both arms hold the overlap guard for their run and release it in one ``finally``,
after abandoning the claim hand-off; both pass the fire-time governance gate and the
claim-time re-vet, and record the run's verdict on the job. A script's
``notify()`` authenticates with the live dashboard's internal secret.

Orchestrator fields read: ``_dashboard_runner``, ``_running_script_ids``, ``cron_svc``,
``dashboard_state``, ``sessions``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import json

    from kiro_crew.slack.gateway import (
        CRON_NOTIFICATION_KIND,
        CronClaimTimeDenied,
        CronJob,
        CronQueueTimeout,
        CronStoreBusy,
        CronStoreUnreadable,
        CronVetOverran,
        GatewayOrchestrator,
        _await_cron_fire_time_gate,
        _claim_backstop,
        _ClaimHandoff,
        _defer_cron_before_dispatch,
        _run_chat,
        _vet_at_claim_then,
        delivery_fingerprint,
        load_window_off_loop,
        logger,
        redact,
        rehydrate_slot_from_history_async,
        run_command_sandboxed,
        run_in_cron_pool,
        run_script_sandboxed,
        runtime_death,
        sel,
        spawn_guarded_turn,
    )


async def _cron_run_command(self: GatewayOrchestrator, job: CronJob) -> str | None:
    """Run a command job: sandboxed shell execution, holding the overlap guard.

    The ``finally`` is the one place the guard is released, after the claim
    hand-off is abandoned. Every exit returns or raises.
    """
    if getattr(self.sessions, "admission_closed", False) is True:
        logger.info("Cron '%s': gateway admission is closed, deferring", job.name)
        _defer_cron_before_dispatch(job, "gateway admission is closed")
        return None
    self._running_script_ids.add(job.id)
    # Bound to the overlap guard's own lifetime, and created HERE rather
    # than at the submit below so the ``finally`` that releases the guard
    # can always reach it -- including on the fire-time deny paths that
    # return before anything is submitted.
    handoff = _ClaimHandoff()
    try:
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_command_exec",
                tool_kind="cron_command",
                outcome="invoked",
            )
        except Exception:
            logger.debug("SEL logging failed in cron command invoked path", exc_info=True)
        # Re-run governance at fire time, not just at cron_add authoring
        # time. A job vetted when it was scheduled can outlive a later
        # policy tightening: the mcp_cron _vet_* gates only run once, at
        # authoring, so a ceiling change has no effect on an
        # already-scheduled job until someone notices and re-authors it.
        # Denial here does not delete the job, so a later policy
        # loosening lets it resume on its own. vet_job_at_fire_time is
        # the shared gate for all three job kinds (command/script/message).
        # Off-loop: the script variant of this gate reads the script
        # file from disk, and governance profile resolution can touch
        # the filesystem too — neither may block the event loop.
        #
        # On the GOVERNANCE pool, deliberately NOT the cron pool. This
        # await sits inside the deadline _execute_with_timeout arms
        # BEFORE the callback runs, so whatever this gate waits for is
        # charged to the job's own execution budget. The cron pool is
        # bounded at _MAX_CRON_WORKERS and its workers are held for a
        # whole job's DURATION, so gating there puts a short policy
        # check behind however many long-running command/script jobs
        # currently occupy it -- and a job whose budget is spent that
        # way is killed having run no code, reported as an overrun,
        # and (if delete_after_run) deleted without ever dispatching.
        # The governance pool holds only short, bounded policy work,
        # which is what makes the residual wait here proportionate.
        #
        # The alternative -- widening every job's deadline by the pool
        # allowance instead -- is the wrong lever twice over: it would
        # delay the wedged-delivery backstop by that allowance for
        # runs that never queue, and it would leave THIS wait
        # unbounded and still misreported, merely later.
        gate_reason, gate_starved = await _await_cron_fire_time_gate(
            job, tool_name="cron_command_exec", tool_kind="cron_command"
        )
        if gate_starved:
            return None
        if gate_reason:
            # Deliberately NOT record_failure(): a governance denial
            # is a policy state, not a job defect. Counting it would
            # auto-pause the job after _AUTO_PAUSE_THRESHOLD fires,
            # and a paused job never fires again — breaking the
            # documented resume-on-policy-loosening semantic.
            # A denial is result-less: without this the run shows a
            # PREVIOUS run's output beside this run's error status.
            job.clear_carried_result()
            job.last_status = "error"
            job.last_error = redact(gate_reason)
            job.fire_time_denied = True
            try:
                sel().log_tool_invocation(
                    session_key=f"cron:{job.id}",
                    tool_name="cron_command_exec",
                    tool_kind="cron_command",
                    outcome="denied",
                )
            except Exception:
                logger.debug(
                    "SEL logging failed in cron command fire-time deny path",
                    exc_info=True,
                )
            await self._alert_cron_failure(job, gate_reason, denied=True)
            return None
        cmd_timeout = job.timeout or 300
        # Queue wait is NOT charged to cmd_timeout: see
        # run_in_cron_pool.  The timeout here is a backstop only --
        # run_command_sandboxed already enforces cmd_timeout on the
        # subprocess itself -- but it must stay, or a wedged worker
        # leaves this entry un-failed forever.
        #
        # Submitted through _vet_at_claim_then for the same reason as
        # the script site: the command TEXT cannot be substituted
        # (it is already captured in job.command), but the governance
        # POLICY it was vetted against can tighten during the queue
        # wait, and the gate above ran before that wait.
        result = await run_in_cron_pool(
            _vet_at_claim_then,
            handoff,
            job,
            run_command_sandboxed,
            job.command,
            cmd_timeout,
            job.id,
            job.secret_env,
            job.secret_env_pin,
            timeout=_claim_backstop(job, cmd_timeout),
        )
        if result.get("status") == "cancelled":
            # User-initiated cancel: CronService.cancel() owns the
            # bookkeeping/history — no failure counting, no delivery.
            return None
        if result.get("status") == "skipped":
            # Overlapping wake refused because this job is already
            # spawning or running. Not a job defect, so no failure
            # counting and no delivery -- counting it would strike a
            # job for a transient scheduling overlap.
            #
            # But returning None alone is NOT neutral: _execute treats
            # any non-"error" last_status as success, so it would set
            # last_status="ok" AND call record_success(), fabricating a
            # run that never happened and refilling the auto-pause
            # budget. The cancelled branch above escapes that only
            # because _execute additionally checks the run claim's
            # cancel flag, and a refused overlap never sets it.
            #
            # So use the established deliberately-neutral shape of the
            # starvation and fire-time-denial paths: last_status="error"
            # to skip the success branch, run_never_started=True as the
            # retention marker, and DELIBERATELY NOT record_failure() --
            # a strike is only ever counted by that explicit call, never
            # by last_status, so this spends no budget and refills none.
            logger.info("Cron '%s': overlapping run refused, skipping", job.name)
            job.clear_carried_result()
            job.last_status = "error"
            job.last_error = "Another run of this job was already starting or running"
            job.run_never_started = True
            return None
        output = result.get("output", "")
        if not output.strip():
            if result.get("status") == "ok":
                # Cleared, not marked: last_status already says the run
                # succeeded, so last_result carries produced text only.
                job.clear_carried_result()
                job.last_status = "ok"
                job.last_error = ""
                job.record_success()
                # Same reason record_success resets consecutive_failures: a run that
                # worked proves the job can work, so the shared-death streak that
                # substitutes for that counter is reset with it. Without this the
                # streak is a LIFETIME total, so after enough deaths the bound is
                # permanently tripped and the exemption silently stops applying.
                runtime_death.clear_shared_deaths(f"cron:{job.id}")
            else:
                # Cleared so displays fall back to last_error below.
                job.clear_carried_result()
                job.last_status = "error"
                job.last_error = f"non-ok status with no output (status={result.get('status')})"
                job.record_failure()
                await self._alert_cron_failure(job, job.last_error)
            return None  # no output = no delivery
        job.set_run_result(redact(output))
        job.last_error = ""
        if result.get("status") == "ok":
            job.last_status = "ok"
            job.record_success()
            # Same reason record_success resets consecutive_failures: a run that
            # worked proves the job can work, so the shared-death streak that
            # substitutes for that counter is reset with it. Without this the
            # streak is a LIFETIME total, so after enough deaths the bound is
            # permanently tripped and the exemption silently stops applying.
            runtime_death.clear_shared_deaths(f"cron:{job.id}")
        else:
            job.last_status = "error"
            job.last_error = f"command failed (exit_code={result.get('exit_code')})"
            job.record_failure()
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_command_exec",
                tool_kind="cron_command",
                outcome=job.last_status,
            )
        except Exception:
            logger.debug("SEL logging failed in cron command result path", exc_info=True)
        if job.last_status == "error":
            # A non-zero exit DOES produce output, and that output is
            # the reason — carry it, not just the exit code.
            await self._alert_cron_failure(job, f"{job.last_error}\n{output}")
        return job.last_result
    except CronQueueTimeout as exc:
        # Pool starvation, not a broken command: every worker was
        # busy for the whole budget so this never started.  Say so,
        # or the next saturation reads as N independent failures.
        job.clear_carried_result()
        job.last_error = str(exc)
        job.last_status = "error"
        # Retention-only marker: a one-shot must not be consumed by a
        # run it never had.  NOT fire_time_denied -- that would also
        # park an at-job disabled and call this a policy denial.
        job.run_never_started = True
        # Deliberately NOT record_failure(): starvation is a fleet
        # state, not a job defect, exactly as a fire-time governance
        # denial is a policy state.  Counting it would auto-pause the
        # job after _AUTO_PAUSE_THRESHOLD starved wakes, and a paused
        # job never fires again -- so a pool that recovers would leave
        # a perfectly healthy job disabled and its work unscheduled.
        # The distinct error text above is what makes the saturation
        # legible; the counter is for runs that actually ran.
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_command_exec",
                tool_kind="cron_command",
                outcome="error",
                error=str(exc),
            )
        except Exception:
            logger.debug(
                "SEL logging failed in cron command queue-timeout path",
                exc_info=True,
            )
        return None
    except asyncio.TimeoutError:
        # Retain the one-shot when the payload never started. This arm
        # is the CLAIM BACKSTOP expiring, and it fires for two
        # different runs: a slow claim-time vet that burned the bound
        # before ``claim()`` was ever granted, and a payload that DID
        # start and then overran. Only the first is a never-started
        # run, and ``abandon()`` is the only thing that can tell them
        # apart -- it returns whether the payload had started, and
        # ``claim()`` refuses once it has been called, so a False here
        # can never become True later. Without this the marker stayed
        # unset and ``_merge_job_result`` consumed a
        # ``delete_after_run`` job that dispatched nothing.
        #
        # Deliberately scoped to THIS arm rather than to the shared
        # ``finally`` below, which also runs on the fire-time deny
        # path: a deny reaches it with the payload equally unstarted,
        # but its retention is owned by ``fire_time_denied``, whose
        # readers park an at-job disabled. Setting this marker there
        # would park a job for a policy decision never made -- the
        # opposite silent failure. ``abandon()`` is documented
        # idempotent, so calling it here and again in the ``finally``
        # is safe.
        started = handoff.abandon()
        job.run_never_started = not started
        job.clear_carried_result()
        job.last_error = f"timeout ({cmd_timeout + 5}s)"
        job.last_status = "error"
        # Count only a run that DISPATCHED. The same reasoning the
        # starvation, gate-deny and vet-overrun arms above already
        # apply: a backstop that expired before ``claim()`` means no
        # line of this job ran, so it is a fleet state rather than a
        # job defect, and counting it auto-pauses at
        # _AUTO_PAUSE_THRESHOLD -- a paused job never fires again, so
        # repeated wedged wakes would permanently disable a healthy
        # job and leave its work unscheduled.
        # ``CronService._execute_with_timeout``'s guard already refuses to count a
        # never-started run, but this arm calls record_failure()
        # DIRECTLY and so never reaches it. A genuine overrun (the
        # payload started, then ran long) still counts, which is what
        # keeps this a discriminator rather than a deletion.
        if started:
            job.record_failure()
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_command_exec",
                tool_kind="cron_command",
                outcome="timeout",
            )
        except Exception:
            logger.debug("SEL logging failed in cron command timeout path", exc_info=True)
        await self._alert_cron_failure(job, f"command {job.last_error}")
        return None
    except CronClaimTimeDenied as exc:
        # Governance refused this run when the worker claimed it --
        # the policy tightened during the queue wait.  Same
        # disposition as the fire-time deny above, deliberately:
        # result-less, keeps the job, and NOT record_failure(),
        # because a policy state must not feed the auto-pause
        # counter.  This clause exists so the run cannot reach the
        # generic arm below, which does count it.
        job.clear_carried_result()
        job.last_status = "error"
        job.last_error = redact(exc.reason)
        job.fire_time_denied = True
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_command_exec",
                tool_kind="cron_command",
                outcome="denied",
            )
        except Exception:
            logger.debug(
                "SEL logging failed in cron command claim-time deny path",
                exc_info=True,
            )
        return None
    except CronVetOverran as exc:
        # The vet outran the allowance the deadline carries for it, so
        # the payload was REFUSED rather than started with a margin
        # that cannot cover its own bound plus teardown.  Nothing
        # ran, so this is retention-shaped like starvation: mark the
        # run never-started and deliberately do NOT record_failure() --
        # a slow governance read is a fleet state, not a job defect,
        # and counting it would auto-pause a healthy job at
        # _AUTO_PAUSE_THRESHOLD.  NOT fire_time_denied: no policy
        # decision was made, and that flag also parks an at-job.
        logger.warning("Cron '%s': %s; payload refused", job.name, exc)
        job.clear_carried_result()
        job.last_error = str(exc)
        job.last_status = "error"
        job.run_never_started = True
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_command_exec",
                tool_kind="cron_command",
                outcome="error",
                error=str(exc),
            )
        except Exception:
            logger.debug("SEL logging failed in cron command vet-overrun path", exc_info=True)
        return None
    except asyncio.CancelledError:
        # The wake deadline cancelled this callback outright, so none of
        # the arms above ran: CancelledError is a BaseException, which
        # the ``except Exception`` below deliberately does not catch.
        # Without this the run reaches cron.py's delete site with
        # neither retention flag set, so a ``delete_after_run`` one-shot
        # is consumed having never executed.
        #
        # ``abandon()`` is the discriminator, exactly as the claim
        # backstop above uses it: it reports whether the payload had
        # already started, and ``claim()`` refuses once abandoned, so a
        # False can never later become True.  Assigning ``not started``
        # rather than setting True unconditionally is what keeps the
        # opposite failure closed -- a payload that DID run reports True,
        # so the marker stays clear and the one-shot is still consumed
        # instead of firing a second time.
        #
        # Deliberately NOT in the shared ``finally`` below, which the
        # fire-time deny path also reaches with the payload equally
        # unstarted: retention there is owned by ``fire_time_denied``,
        # and setting this marker would park an at-job disabled for a
        # policy decision that was never made.
        job.run_never_started = not handoff.abandon()
        raise
    except Exception as exc:
        logger.exception("Command cron '%s' failed: %s", job.name, exc)
        job.clear_carried_result()
        err_str = redact(str(exc))
        job.last_error = err_str[:200]
        job.last_status = "error"
        job.record_failure()
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name="cron_command_exec",
                tool_kind="cron_command",
                outcome="error",
            )
        except Exception:
            logger.debug("SEL logging failed in cron command error path", exc_info=True)
        await self._alert_cron_failure(job, f"{type(exc).__name__}: {exc}")
        return None
    finally:
        # Abandon BEFORE releasing the overlap guard, and do it here
        # rather than in the timeout arm because this ``finally`` is the
        # single place the guard is released -- so it also covers any
        # exit no ``except`` arm above handles.  A no-op on the success
        # path: the payload already ran.  ``abandon()`` is idempotent,
        # so the cancellation arm above having already called it changes
        # nothing observed here.
        handoff.abandon()
        self._running_script_ids.discard(job.id)


async def _deliver_script_result(
    self: GatewayOrchestrator, job: CronJob, message: str, *, remove: bool = False
) -> None:
    """Deliver a script cron result to the originating session. Optionally remove the job."""
    delivered = False
    try:
        if message and not job.silent and self.dashboard_state and job.session_key:
            slot_key = job.session_key.removeprefix("dashboard:")
            slot = self.dashboard_state.get_slot(slot_key)
            if slot is None:
                # Async form: the sync one parses the whole transcript on
                # the loop, which stalls every other session's frames on
                # a large store.
                slot = await rehydrate_slot_from_history_async(self.dashboard_state, slot_key)
            elif getattr(slot, "window_pending", False) is True:
                # A tab restored as a sidebar row: its window loads
                # before the delivery below appends to it, off the loop
                # for the same reason, and the tab is read again after.
                await load_window_off_loop(slot)
                slot = self.dashboard_state.get_slot(slot_key)
            label = redact(job.name)
            if slot:
                wrapped = f'[Cron notification: "{label}"]\n{message}\n[/Cron notification]'
                inject_cls = json.dumps({"cronLabel": label})
                if slot.running:
                    qid = slot.queue_append(wrapped, kind=CRON_NOTIFICATION_KIND)
                    _cls = json.loads(inject_cls)
                    _cls["queue_id"] = qid
                    slot.append("queued", wrapped, json.dumps(_cls))
                else:
                    # `cls` is not persisted for role `inject`, so the label
                    # must also ride in `meta`, which is — otherwise the row
                    # loses its identity on the next rehydrate.
                    slot.append(
                        "inject",
                        wrapped,
                        inject_cls,
                        meta={"injectKind": "cron", "cronLabel": label},
                    )
                    task = spawn_guarded_turn(
                        self.dashboard_state,
                        slot,
                        _run_chat(
                            self.dashboard_state,
                            slot,
                            wrapped,
                            _directive_user_origin=False,
                            # Structural provenance for the session
                            # crew log: the queued twin above carries
                            # CRON_NOTIFICATION_KIND, and this branch is
                            # the same injector dispatching directly.
                            _turn_actor="cron",
                        ),
                    )
                    slot.task = task
                self.dashboard_state.push_slots_update()
            else:
                self.dashboard_state.notify("cron", f"⚡ {label}", message, meta={"job_id": job.id})
        elif message and not job.silent and self.dashboard_state:
            label = redact(job.name)
            self.dashboard_state.notify("cron", f"⚡ {label}", message, meta={"job_id": job.id})
        delivered = True
    except Exception as notify_exc:
        logger.warning("Cron '%s' delivery failed: %s", job.name, notify_exc)
    if remove and delivered and self.cron_svc:
        try:
            await self.cron_svc.remove_job_async(
                job.id,
                actor="cron",
                source="cron",
                one_shot_path="cron_gateway",
            )
        except (CronStoreBusy, CronStoreUnreadable):
            # No caller to retry this fire-and-forget removal, so hand
            # it to the service's deferred-removal queue: the job is
            # disabled in memory immediately (can't re-fire) and the
            # next timer tick drains it from disk under the store lock.
            # No audit here — whichever path lands the removal on disk
            # emits cron.remove: the deferred drain, or the run-merge
            # consume when a delete_after_run job's merge gets there
            # first.
            self.cron_svc.defer_removal(job.id)
            logger.warning(
                "Cron '%s': store busy, queued one-shot removal for " "the next timer tick",
                job.name,
            )


async def _cron_run_script(self: GatewayOrchestrator, job: CronJob) -> str | None:
    """Run a script job: the deterministic, no-LLM path, holding the overlap guard.

    The ``finally`` is the one place the guard is released, after the claim
    hand-off is abandoned. Every exit returns or raises.
    """
    if getattr(self.sessions, "admission_closed", False) is True:
        logger.info("Cron '%s': gateway admission is closed, deferring", job.name)
        _defer_cron_before_dispatch(job, "gateway admission is closed")
        return None
    self._running_script_ids.add(job.id)
    # Bound to the overlap guard's own lifetime, and created HERE rather
    # than at the submit below so the ``finally`` that releases the guard
    # can always reach it -- including on the fire-time deny paths that
    # return before anything is submitted.
    handoff = _ClaimHandoff()
    try:
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name=job.script,
                tool_kind="cron_script",
                outcome="invoked",
            )
        except Exception:
            logger.debug("SEL logging failed in cron script invoked path", exc_info=True)
        # Fire-time governance gate — mirrors the command path above.
        # vet_job_at_fire_time re-runs the capabilities.cron gate AND
        # re-scans the script BODY on the freshly re-resolved path
        # (which also validates the path, making a bare
        # resolve_script_path call here redundant), so a policy
        # tightened after scheduling — or a script file edited on disk
        # after authoring — denies this run. The job is kept: a later
        # policy loosening lets it resume on its own.
        # Off-loop: reads the script body from disk (up to the scan
        # cap) — must not block the event loop on a wedged FS.
        # Governance pool, not the cron pool: see the command site.
        gate_reason, gate_starved = await _await_cron_fire_time_gate(
            job, tool_name="cron_script_exec", tool_kind="cron_script"
        )
        if gate_starved:
            return None
        if gate_reason:
            # No record_failure() — see the command-path deny above:
            # a policy denial must not feed the auto-pause counter.
            # A denial is result-less: without this the run shows a
            # PREVIOUS run's output beside this run's error status.
            job.clear_carried_result()
            job.last_status = "error"
            job.last_error = redact(gate_reason)
            job.fire_time_denied = True
            try:
                sel().log_tool_invocation(
                    session_key=f"cron:{job.id}",
                    tool_name=job.script,
                    tool_kind="cron_script",
                    outcome="denied",
                )
            except Exception:
                logger.debug(
                    "SEL logging failed in cron script fire-time deny path",
                    exc_info=True,
                )
            await self._alert_cron_failure(job, gate_reason, denied=True)
            return None
        # Run in sandboxed subprocess via wrap_argv()
        script_timeout = job.timeout or 30
        # Queue wait is NOT charged to script_timeout: see
        # run_in_cron_pool.  The timeout here is a backstop only --
        # run_script_sandboxed already enforces script_timeout on
        # the subprocess itself -- but it must stay, or a wedged
        # worker leaves this entry un-failed forever.
        #
        # Submitted through _vet_at_claim_then, not bare: the gate
        # above ran before the queue wait, and the launcher re-reads
        # the body from disk in the child, so the gate's scan alone
        # authorises bytes that may be gone by then.  The re-vet
        # runs inside the worker, after the wait, so the decision
        # holds at the moment of use.  It shares the backstop below,
        # which is why it must stay short.
        result = await run_in_cron_pool(
            _vet_at_claim_then,
            handoff,
            job,
            run_script_sandboxed,
            job.script,
            job.id,
            job.message,
            script_timeout,
            job.secret_env,
            job.secret_env_pin,
            delivery_fingerprint(
                job.session_key,
                job.silent,
                job.channel or "",
                job.thread_ts or "",
            ),
            self._live_internal_secret,
            timeout=_claim_backstop(job, script_timeout),
        )
        status = result.get("status", "error")
        if status == "cancelled":
            # User-initiated cancel: CronService.cancel() owns the
            # bookkeeping/history — no failure counting, no delivery.
            return None
        if status == "skipped":
            # Overlapping wake refused because this job is already
            # spawning or running -- no failure counting, no delivery.
            # See the command path for why returning None alone would
            # be recorded as a SUCCESS: last_status="error" skips
            # _execute's success branch, run_never_started=True is the
            # retention marker, and record_failure() is deliberately
            # NOT called so the overlap costs no auto-pause strike.
            logger.info("Cron '%s': overlapping run refused, skipping", job.name)
            job.clear_carried_result()
            job.last_status = "error"
            job.last_error = "Another run of this job was already starting or running"
            job.run_never_started = True
            return None
        if status == "ok":
            job.clear_carried_result()
            job.last_error = ""
            job.last_status = "ok"
            job.record_success()
            # Same reason record_success resets consecutive_failures: a run that
            # worked proves the job can work, so the shared-death streak that
            # substitutes for that counter is reset with it. Without this the
            # streak is a LIFETIME total, so after enough deaths the bound is
            # permanently tripped and the exemption silently stops applying.
            runtime_death.clear_shared_deaths(f"cron:{job.id}")
            try:
                sel().log_tool_invocation(
                    session_key=f"cron:{job.id}",
                    tool_name=job.script,
                    tool_kind="cron_script",
                    outcome="ok",
                )
            except Exception:
                logger.debug("SEL logging failed in cron script ok path", exc_info=True)
            return "ok"
        elif status == "skip":
            # A completed Skip is a successful run that chose no-op —
            # the same "success" outcome as the ok/done/report
            # siblings above. Unlike them it deliberately does NOT
            # call job.record_success() here: CronScheduler._execute
            # is the backstop that resets consecutive_failures (and
            # lifts auto-pause) on every non-error return — Skip
            # included, since this branch returns None without
            # setting last_status="error" — and its reset is guarded
            # by the claim.cancelled cancel-race check. Resetting in
            # this branch would bypass that guard and could re-enable
            # a job cancelled mid-tick.
            # Result-less like the deny paths: a Skip that carried the
            # previous run's output read as though it had produced it.
            job.clear_carried_result()
            try:
                sel().log_tool_invocation(
                    session_key=f"cron:{job.id}",
                    tool_name=job.script,
                    tool_kind="cron_script",
                    outcome="skip",
                )
            except Exception:
                logger.debug("SEL logging failed in cron script skip path", exc_info=True)
            return None
        elif status == "done":
            msg = result.get("message", "")
            script_msg = redact(msg) if msg else ""
            job.set_run_result(script_msg)
            job.last_error = ""
            job.last_status = "ok"
            job.record_success()
            # Same reason record_success resets consecutive_failures: a run that
            # worked proves the job can work, so the shared-death streak that
            # substitutes for that counter is reset with it. Without this the
            # streak is a LIFETIME total, so after enough deaths the bound is
            # permanently tripped and the exemption silently stops applying.
            runtime_death.clear_shared_deaths(f"cron:{job.id}")
            # Deliver Done message and remove job
            await self._deliver_script_result(job, script_msg, remove=True)
            try:
                sel().log_tool_invocation(
                    session_key=f"cron:{job.id}",
                    tool_name=job.script,
                    tool_kind="cron_script",
                    outcome="done",
                )
            except Exception:
                logger.debug("SEL logging failed in cron script done path", exc_info=True)
            return script_msg or "done"
        elif status == "report":
            msg = result.get("message", "")
            script_msg = redact(msg) if msg else ""
            job.set_run_result(script_msg)
            job.last_error = ""
            job.last_status = "ok"
            job.record_success()
            # Same reason record_success resets consecutive_failures: a run that
            # worked proves the job can work, so the shared-death streak that
            # substitutes for that counter is reset with it. Without this the
            # streak is a LIFETIME total, so after enough deaths the bound is
            # permanently tripped and the exemption silently stops applying.
            runtime_death.clear_shared_deaths(f"cron:{job.id}")
            # Deliver Report message (keep job running)
            await self._deliver_script_result(job, script_msg)
            try:
                sel().log_tool_invocation(
                    session_key=f"cron:{job.id}",
                    tool_name=job.script,
                    tool_kind="cron_script",
                    outcome="report",
                )
            except Exception:
                logger.debug("SEL logging failed in cron script report path", exc_info=True)
            return script_msg or "report"
        else:
            err = result.get("error", "unknown error")
            raise RuntimeError(err)
    except CronQueueTimeout as exc:
        # Pool starvation, not a broken script: every worker was
        # busy for the whole budget so this never ran a line.  The
        # distinct text is what makes the next saturation legible
        # instead of looking like N scripts that each overran.
        logger.warning("Script cron '%s' never got a worker slot: %s", job.name, exc)
        job.clear_carried_result()
        job.last_error = str(exc)
        job.last_status = "error"
        # Retention-only marker: see the command path.
        job.run_never_started = True
        # Deliberately NOT record_failure(): see the command path.
        # Starvation means the script never ran a line, so counting it
        # would auto-pause a healthy job after _AUTO_PAUSE_THRESHOLD
        # starved wakes and leave it disabled once the pool recovered.
        # No auto-pause log here either: this path cannot reach the
        # threshold.
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name=job.script,
                tool_kind="cron_script",
                outcome="error",
                error=str(exc),
            )
        except Exception:
            logger.debug(
                "SEL logging failed in cron script queue-timeout path",
                exc_info=True,
            )
        return None
    except asyncio.TimeoutError:
        # See the command path above: this is the claim backstop, and
        # only ``abandon()`` distinguishes a vet that burned the bound
        # before ``claim()`` from a payload that started and overran.
        # Scoped to this arm, not the shared ``finally``, so the
        # fire-time deny path keeps its retention in
        # ``fire_time_denied`` instead of parking an at-job disabled.
        started = handoff.abandon()
        job.run_never_started = not started
        logger.warning("Script cron '%s' timed out after %ds", job.name, script_timeout + 5)
        job.clear_carried_result()
        job.last_error = f"timeout ({script_timeout + 5}s)"
        job.last_status = "error"
        # See the command path: count only a run that DISPATCHED, so a
        # backstop that expired before ``claim()`` cannot auto-pause a
        # job that never ran a line. A genuine overrun still counts.
        if started:
            job.record_failure()
        if job.auto_paused:
            logger.warning(
                "Script cron '%s' auto-paused after %d consecutive errors",
                job.name,
                job.consecutive_failures,
            )
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name=job.script,
                tool_kind="cron_script",
                outcome="error",
                error=f"timeout ({script_timeout + 5}s)",
            )
        except Exception:
            logger.debug("SEL logging failed in cron script timeout path", exc_info=True)
        await self._alert_cron_failure(job, f"script {job.last_error}")
        return None
    except CronClaimTimeDenied as exc:
        # Governance refused this run when the worker claimed it --
        # the body on disk, or the policy, changed during the queue
        # wait.  Same disposition as the fire-time deny above,
        # deliberately: result-less, keeps the job, and NOT
        # record_failure(), because a policy state must not feed the
        # auto-pause counter.  This clause exists so the run cannot
        # reach the generic arm below, which does count it.
        job.clear_carried_result()
        job.last_status = "error"
        job.last_error = redact(exc.reason)
        job.fire_time_denied = True
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name=job.script,
                tool_kind="cron_script",
                outcome="denied",
            )
        except Exception:
            logger.debug(
                "SEL logging failed in cron script claim-time deny path",
                exc_info=True,
            )
        return None
    except CronVetOverran as exc:
        # The vet outran the allowance the deadline carries for it, so
        # the payload was REFUSED rather than started with a margin
        # that cannot cover its own bound plus teardown.  Nothing
        # ran, so this is retention-shaped like starvation: mark the
        # run never-started and deliberately do NOT record_failure() --
        # a slow governance read is a fleet state, not a job defect,
        # and counting it would auto-pause a healthy job at
        # _AUTO_PAUSE_THRESHOLD.  NOT fire_time_denied: no policy
        # decision was made, and that flag also parks an at-job.
        logger.warning("Cron '%s': %s; payload refused", job.name, exc)
        job.clear_carried_result()
        job.last_error = str(exc)
        job.last_status = "error"
        job.run_never_started = True
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name=job.script,
                tool_kind="cron_script",
                outcome="error",
                error=str(exc),
            )
        except Exception:
            logger.debug("SEL logging failed in cron script vet-overrun path", exc_info=True)
        return None
    except asyncio.CancelledError:
        # See the command path above: a wake deadline cancelling this
        # callback runs no ``except`` arm, because CancelledError is a
        # BaseException, so without this the delete site consumes a
        # one-shot that never executed.  ``abandon()`` reports whether
        # the payload had started, so ``not started`` retains only the
        # run that dispatched nothing and leaves a completed run
        # deletable.  Not in the shared ``finally``, whose deny path
        # retention belongs to ``fire_time_denied``.
        job.run_never_started = not handoff.abandon()
        raise
    except Exception as exc:
        logger.exception("Script cron '%s' failed: %s", job.name, exc)
        job.clear_carried_result()
        err_str = redact(str(exc))
        job.last_error = err_str
        job.last_status = "error"
        job.record_failure()
        if job.auto_paused:
            logger.warning(
                "Script cron '%s' auto-paused after %d consecutive errors",
                job.name,
                job.consecutive_failures,
            )
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name=job.script,
                tool_kind="cron_script",
                outcome="error",
                error=err_str,
            )
        except Exception:
            logger.debug("SEL logging failed in cron script error path", exc_info=True)
        # The reason a script cron dies is often environmental (a
        # startup RuntimeError, a missing dependency) and identical on
        # every fire, so it read as an idle job rather than a broken
        # one until this alert carried the reason out.
        await self._alert_cron_failure(job, f"{type(exc).__name__}: {exc}")
        return None
    finally:
        # Abandon BEFORE releasing the overlap guard, and do it here
        # rather than in the timeout arm because this ``finally`` is the
        # single place the guard is released -- so it also covers any
        # exit no ``except`` arm above handles.  A no-op on the success
        # path: the payload already ran.  ``abandon()`` is idempotent,
        # so the cancellation arm above having already called it changes
        # nothing observed here.
        handoff.abandon()
        self._running_script_ids.discard(job.id)


def _live_internal_secret(self: GatewayOrchestrator) -> str:
    """This gateway's LIVE internal-API secret, for script-cron ``notify()``.

    The dashboard mints the secret at startup and the auth middleware
    compares against that same in-memory value (``app["local_secret"]``).
    A script cron's child authenticates ``/api/send-message`` with the
    secret it is handed at spawn, so the in-process scheduler hands it THIS
    value rather than one re-derived from the environment or a per-port
    file — a stale ``KIROCREW_INTERNAL_SECRET`` or ``.secret`` file
    otherwise wins the derivation and 403s every ``notify()``.

    Returns ``""`` when no dashboard has started (``--no-dashboard`` /
    API-only), so ``run_script_sandboxed`` falls back to env/file
    derivation. Read-only and in-process only: the value is never logged or
    returned over HTTP.
    """
    runner = self._dashboard_runner
    if runner is None:
        return ""
    try:
        return runner.app.get("local_secret", "") or ""
    except Exception:
        return ""
