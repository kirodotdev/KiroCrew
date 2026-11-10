"""What a cron run must clear before, and while, it dispatches.

The bounded fire-time governance gate and the ``run_never_started`` retention
marker it holds across a cancellation, the screen that keeps a job's ``env`` from
choosing its own governance, the first-run tab pre-create, the claim-time re-vet
inside the cron-pool worker with its handoff against an awaiter that gave up, and
the one-shot post-token CONTINUE resume of an agent turn.

The cron callback that sequences them lives in ``cron_fire.py``, its command and
script arms in ``cron_exec.py`` and its agent turns in ``cron_turn.py``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.llm_helpers import annotate_model_fallback
from kiro_crew.subagent import _TRANSIENT_CONTINUE_MSG

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import (
        _SUBPROC_CLEANUP_ALLOWANCE_SECS,
        TOOL_ACTIVITY_ATTR,
        Any,
        Callable,
        CronJob,
        CronQueueTimeout,
        DashboardState,
        acp_error_is_transient,
        asyncio,
        cron_gate_budget,
        effective_wake_budget,
        ensure_cron_slot,
        logger,
        provider_last_turn_usage,
        run_in_cron_gate_pool,
        sel,
        stream_and_collect,
        threading,
        time,
        transient_retry_delay,
        vet_job_at_fire_time,
    )


# Whole-callback transient retries for the cron LLM path (session acquire /
# client creation / context assembly), mirroring the subagent path's budget.
# In-stream transient errors are retried separately by stream_and_collect.
_CRON_TRANSIENT_RETRIES = 2


# Continuation prompt for the one-shot post-token resume below. Reuses the
# subagent path's constant verbatim (gateway.py already imports from
# kiro_crew.subagent) instead of adding a third hand-maintained copy next to
# the dashboard's _POSTTOKEN_RECOVER_MSG — see the PARITY NOTE in
# _on_acp_posttoken_transient (dashboard/chat_turn/acp_recovery.py). The cron
# result is delivered once at the end of the turn, so the preserved partial is
# concatenated with the continuation instead of being re-shown to a live viewer.
_CRON_POSTTOKEN_CONTINUE_MSG = _TRANSIENT_CONTINUE_MSG


def _defer_cron_before_dispatch(job: CronJob, reason: str) -> None:
    """Retain an undispatched run without counting success or failure."""
    job.clear_carried_result()
    job.last_status = "error"
    job.last_error = reason
    job.run_never_started = True


async def _await_cron_fire_time_gate(
    job: CronJob, *, tool_name: str, tool_kind: str
) -> tuple[str | None, bool]:
    """Await the fire-time governance gate, bounded, returning ``(reason, starved)``.

    The gate runs on its OWN pool, and its total wait (queue plus execution) is
    bounded below the wake deadline ``_execute_with_timeout`` armed. The shared
    governance pool is paced by remote senders, so an inbound burst would queue
    ahead of the gate, and a message job has no ``_pool_queue_allowance`` to
    absorb that. If the wake deadline fired first, ``_merge_job_result`` would
    see a finished run and consume a ``delete_after_run`` job that never
    dispatched.

    So starvation surfaces as ``CronQueueTimeout`` first: ``starved`` is
    returned and ``run_never_started`` set, which the delete site honours. Not
    ``fire_time_denied``, which parks an at-job disabled as a policy denial.
    ``record_failure()`` is not called: pool capacity must not auto-pause a job
    that never ran a line.
    """
    budget = cron_gate_budget(effective_wake_budget(job))
    # Retain for the whole await, not only in the handler: an event-loop stall
    # can let ``_execute_with_timeout`` cancel this coroutine AT the await, and
    # no handler runs. ``CancelledError`` is a ``BaseException``, so it escapes
    # the ``except Exception`` below and the marker survives.
    job.run_never_started = True
    try:
        reason = await run_in_cron_gate_pool(vet_job_at_fire_time, job, timeout=budget)
    except CronQueueTimeout as exc:
        # Scoped as close_run's result-less clear (cron_service/execution.py).
        # A message job keeps ``last_result``: it is the "do NOT repeat" context
        # cron_prior_result_context carries, and clearing it would make the next
        # run repeat what was already sent. Command and script jobs discard
        # their prompt, so a carried value could only mislabel this run.
        if job.command or job.script:
            job.clear_carried_result()
        job.last_status = "error"
        # Distinct from the pool-starvation text so the two are not conflated:
        # this run never even reached its own dispatch decision.
        job.last_error = f"fire-time gate {exc}"
        # Already True from above; kept so this handler still reads correctly on
        # its own and a later reordering cannot silently drop the retention.
        job.run_never_started = True
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name=tool_name,
                tool_kind=tool_kind,
                outcome="error",
                error=job.last_error,
            )
        except Exception:
            logger.debug("SEL logging failed in cron fire-time gate starvation path", exc_info=True)
        return None, True
    except Exception:
        # The gate reached its own WORK and failed there -- a failed dispatch
        # DECISION, not a run that never started.  Clearing preserves the very
        # distinction :class:`CronGateWorkTimeout` draws.
        job.run_never_started = False
        raise
    # A verdict came back, so clear: a marker held past it would retain a
    # healthy one-shot. A deny clears too, since ``fire_time_denied`` owns its
    # retention.
    job.run_never_started = False
    return reason, False


#: Env-var names a cron job's own ``env`` map may never deliver to the spawned
#: session, stripped in :func:`cron_job_env_without_reserved`. Reserved names
#: match case-insensitively while allowed keys keep their declared case.
#:
#: ``job.env`` comes from an app manifest's ``crons[].env`` block, which
#: ``apps.manifest.CronEntry.from_dict`` copies verbatim -- keys are stringified,
#: never screened -- so every name here is one whose VALUE decides how the run is
#: governed rather than what it does:
#:
#: * ``KIROCREW_APPROVAL_MODE`` is re-injected by the caller when the job's own
#:   VALIDATED ``approval_mode`` is "auto"; delivered through ``job.env`` instead,
#:   it auto-approves an interactive cron's ``spawn_run`` subagents.
#: * ``KIROCREW_SECURITY_POLICY`` and ``KIROCREW_ADMISSION_POLICY`` name the FILE
#:   the governance and admission ceilings are read from.
#:   ``platform.governance`` resolves that env path as a tier ABOVE the operator's
#:   own ``security_policy.json`` and the two are mutually exclusive
#:   (first-present-wins), so a job-supplied path replaces the operator's ceiling
#:   for the scheduled agent and every MCP server it starts, rather than
#:   tightening it.
#: * ``KIROCREW_HOME`` picks the same ceiling one level up: ``config.paths`` reads
#:   it to resolve the data home, and ``governance._policy_home_path`` resolves
#:   ``security_policy.json`` under that home, so a job-supplied home points the
#:   ceiling read at a directory the job controls. It reaches the child even
#:   though the policy-path pair would not, because ``KIROCREW_HOME`` is absent
#:   from ``sandbox._AGENT_DENIED_ENV_KEYS`` while the
#:   ``KIROCREW_POLICY_*`` fetch family is in it.
#: * ``KIROCREW_PROFILE`` picks WHICH ceiling is composed at all:
#:   ``platform.resolve_profile`` reads it, and a job-supplied ``standalone``
#:   drops the companion edition's overlay, so an operation the enterprise
#:   ceiling denies resolves as permitted. It is forwarded to first-party app
#:   backends on purpose (``apps/backend.py``), which is the gateway's own value
#:   and untouched here.
#:
#: Stripped HERE, at the untrusted-input seam, and deliberately not in the agent
#: spawn's own env scrub: the gateway's ``os.environ`` copy of each of these is
#: the OPERATOR's value, and a child that inherits nothing at all resolves the
#: standalone ungoverned ceiling, which is open where deny-by-default is open.
#: Removing them from every child would therefore drop a real operator ceiling;
#: removing them from ``job.env`` drops only what an app asked for.
#:
#: Reserved names match case-insensitively: ``CronEntry.from_dict`` keeps a
#: manifest key's case verbatim, and a Windows child resolves ``kirocrew_home``
#: and ``KIROCREW_HOME`` as one variable.
_CRON_RESERVED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "KIROCREW_APPROVAL_MODE",
        "KIROCREW_SECURITY_POLICY",
        "KIROCREW_ADMISSION_POLICY",
        "KIROCREW_HOME",
        "KIROCREW_PROFILE",
    }
)


def cron_job_env_without_reserved(job_env: dict[str, str] | None) -> dict[str, str]:
    """Return *job_env* minus :data:`_CRON_RESERVED_ENV_KEYS`.

    A module-level function rather than a comprehension inside the caller's
    closure so the seam has a name a test can drive: the property it carries is
    about an untrusted map reaching a spawned session, and a closure is reachable
    only by standing a whole gateway up.
    """
    return {k: v for k, v in (job_env or {}).items() if k.upper() not in _CRON_RESERVED_ENV_KEYS}


async def _pre_create_cron_slot(dashboard_state: "DashboardState", job: CronJob) -> None:
    """Pre-create the job's first-run dashboard tab, best-effort.

    The tab (via :func:`ensure_cron_slot`) is an amenity, not a precondition,
    and the bind does transcript I/O. ``_execute`` clears ``run_never_started``
    before this callback and never re-arms it, so an error escaping here would
    let the delete site consume a ``delete_after_run`` one-shot that never
    dispatched.

    As in :func:`_await_cron_fire_time_gate`, the marker is armed for exactly
    the await, so a cancel at the await retains the one-shot. An ordinary
    failure is logged and the run proceeds without the tab; delivery's bind
    still creates it later. The marker must be cleared before dispatch, or a
    healthy one-shot is retained. ``record_failure()`` is not called: a tab that
    could not be minted is not a defect of the job.
    """
    job.run_never_started = True
    try:
        await ensure_cron_slot(dashboard_state, job)
    except Exception:
        logger.warning(
            "Cron '%s': first-run tab pre-create failed; running without it",
            job.name,
            exc_info=True,
        )
    job.run_never_started = False


class CronClaimTimeDenied(Exception):
    """Governance refused the job when the worker CLAIMED its execution.

    Distinct from the fire-time deny, which happens before the execution is
    submitted at all.  Deliberately a plain ``Exception``: it must not be caught
    by the :class:`CronQueueTimeout` clause (whose retention semantics are for
    runs that never got a worker) nor by ``asyncio.TimeoutError``, and it must
    be handled BEFORE the generic ``except Exception`` arm, which calls
    ``record_failure()`` and would feed a policy decision into the auto-pause
    counter.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CronClaimAbandoned(Exception):
    """The awaiter gave up before the worker reached the payload, so it must not run.

    Deliberately NOT a :class:`CronClaimTimeDenied` subclass -- an abandoned call
    is a deadline, not a policy decision, and recording it as a denial would
    misreport it.  Deliberately NOT a :class:`CronQueueTimeout` or
    ``asyncio.TimeoutError`` subclass either: by the time this is raised the
    awaiter has already left, so nothing catches it and it exists to say in the
    logs which of the two timeout shapes happened.
    """


class _ClaimHandoff:
    """Serialises a worker starting its payload against its awaiter giving up.

    A claimed thread cannot be interrupted, and the claim-time vet runs before
    the payload, so the deadline can land mid-vet. The payload would then start
    after the caller's ``finally`` released the overlap guard and run beside the
    next fire, duplicating its side effects.

    The lock makes the outcome deterministic: exactly one of :meth:`claim` and
    :meth:`abandon` sees an unset flag, so a payload either starts (and is
    reported as still running) or never starts.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._abandoned = False
        self._started = False

    def abandon(self) -> bool:
        """Record that the awaiter gave up; True if the payload had already started.

        Idempotent, because the ``finally`` that releases the overlap guard runs
        on every exit path including the ones that already called this.
        """
        with self._lock:
            self._abandoned = True
            return self._started

    def claim(self) -> bool:
        """Ask permission to start the payload.  False means refuse."""
        with self._lock:
            if self._abandoned:
                return False
            self._started = True
            return True


class CronVetOverran(Exception):
    """The claim-time vet spent more than the allowance the deadline carries for it.

    The remaining budget then cannot cover the subprocess bound plus
    :data:`~kiro_crew.cron._SUBPROC_CLEANUP_ALLOWANCE_SECS`, so the deadline
    would fire mid-subprocess, the overlap guard would clear, and the next fire
    would duplicate its side effects. Refusing trades that for a reported
    missed run.

    Not a :class:`CronClaimTimeDenied` subclass (an overrun is not a policy
    decision and must not park an at-job), nor a :class:`CronQueueTimeout`
    subclass (the vet did get a worker slot).
    """

    def __init__(self, elapsed: float, bound: float) -> None:
        super().__init__(f"claim-time vet took {elapsed:.2f}s of a {bound:.2f}s allowance")
        self.elapsed = elapsed
        self.bound = bound


def claim_vet_bound(job: CronJob) -> float:
    """Seconds the claim-time vet may spend before its payload must be refused.

    The same bound the FIRE-time gate is held to for the same
    ``vet_job_at_fire_time`` work, so the two do not drift, and the number
    ``kiro_crew.cron._vet_allowance`` adds to the run deadline.  Read from
    :func:`cron_gate_budget` rather than kept as a literal, which is what makes
    the widened backstop below a guarantee instead of a hope.
    """
    return cron_gate_budget(effective_wake_budget(job))


def _claim_backstop(job: CronJob, subprocess_bound: int) -> float:
    """The inner ``run_in_cron_pool`` bound: subprocess + teardown + vet.

    One budget covers all three serially, so each needs a term.  The teardown
    margin is read from
    :data:`~kiro_crew.cron._SUBPROC_CLEANUP_ALLOWANCE_SECS` rather than repeated
    here as a literal, which keeps it single-sourced, and the
    :func:`claim_vet_bound` term stops the vet spending the teardown's share of
    it.
    """
    return subprocess_bound + _SUBPROC_CLEANUP_ALLOWANCE_SECS + claim_vet_bound(job)


def _vet_at_claim_then(
    handoff: _ClaimHandoff, job: CronJob, fn: Callable[..., Any], *args: Any
) -> Any:
    """Re-vet inside the worker, immediately before the execution it authorises.

    The fire-time gate authorises a run and then the execution is submitted to
    the cron pool, whose queue wait is deliberately NOT charged to the job's
    deadline (that uncharging is this change's sibling and the point of the
    surrounding work).  So the authorisation and the use it authorises are
    separated by a wait bounded only by ``_CRON_QUEUE_WAIT_SECS`` -- and both
    inputs to the decision can change inside it:

    * a script's BODY, because ``run_script_sandboxed``'s launcher re-reads the
      file in the sandboxed child (``open`` + ``compile`` + ``exec``), so the
      bytes that run are whatever is on disk when the worker gets there, not
      the bytes the gate scanned;
    * the governance POLICY, which applies to ``command`` jobs too even though
      a command's text is already captured in ``job.command``.

    Running the vet here closes that window to nil: the queue wait now happens
    BEFORE the decision, and the decision holds at the moment of use.

    This is deliberately an ADDITIONAL vet, not a moved one.  Keeping the
    fire-time gate means a denial is still refused early and cheaply, without
    occupying a cron worker for the queue's duration, and it leaves the gate's
    starvation/retention plumbing (``gate_starved`` ->
    ``run_never_started``) untouched.  The cost is one extra governance
    evaluation per EXECUTED run, plus for a command one extra composition scan of
    its body (length-capped by ``mcp_cron._CRON_MAX_COMMAND_SCAN``) and for a
    script one extra capped body read.
    That work runs inside a worker this job already holds, so unlike gating on
    this pool it puts no policy check behind other jobs' queue -- the property
    the governance-pool split at the call sites protects.  It does count against
    the ``+5s`` backstop the call sites arm, which is why the vet must stay
    short and bounded.

    Consequence for the audit trail, by design: an EXECUTED command/script run
    leaves TWO ``governance_decision`` events per gate (gate time and claim
    time), not one.  They are genuinely distinct decisions -- the second
    is the one that authorised the bytes that ran -- and
    ``vet_job_at_fire_time`` already audits every decision "in its own right so
    the SEL trail shows every permission decision that authorized this
    execution".  A reader counting events per run should expect the pair.

    Because the vet runs BEFORE the payload inside the same budget, the
    caller's deadline can land while the vet is still going -- with the payload
    not yet started.  ``handoff`` is what stops that call dispatching anyway
    once the caller has given up and released its overlap guard; see
    :class:`_ClaimHandoff`.  The check sits immediately before the dispatch and
    nowhere earlier on purpose: the vet itself takes time, so a check made
    before it would be stale by the time it mattered.

    The vet is also BOUNDED here rather than merely asked to "stay short".  The
    caller's backstop carries an allowance for it (:func:`_claim_backstop`), and
    an allowance is only a guarantee if the thing it covers cannot exceed it --
    so a vet that overruns refuses its payload instead of starting one whose
    remaining margin cannot cover the subprocess and its teardown.  Measured
    on ``monotonic`` so a clock adjustment cannot make an overrun look fine.
    """
    started_at = time.monotonic()
    reason = vet_job_at_fire_time(job)
    if reason:
        raise CronClaimTimeDenied(reason)
    elapsed = time.monotonic() - started_at
    bound = claim_vet_bound(job)
    if elapsed > bound:
        raise CronVetOverran(elapsed, bound)
    if not handoff.claim():
        raise CronClaimAbandoned(
            f"cron '{job.name}': awaiter gave up during the claim-time vet; "
            "payload refused rather than run beside the next fire"
        )
    return fn(*args)


# One spelling of the fallback-served warning for every unattended surface: the
# body lives next to TURN_FALLBACK_ATTR in llm_helpers; this module-level name
# serves the cron/heartbeat call sites and their tests.
_annotate_model_fallback = annotate_model_fallback


async def _cron_stream_with_posttoken_resume(
    client: Any,
    message: str,
    *,
    job_name: str,
    directive_refusals: list[str],
    on_resume: Callable[[], None] | None = None,
    **stream_kwargs: Any,
) -> tuple[str, float | None]:
    """Run a cron agent turn, resuming ONCE after a post-token transient error.

    stream_and_collect's in-stream retry stops once tokens have streamed, and
    _cron_callback's whole-callback retry stops once the prompt is dispatched,
    so a transient error after the first token would fail the cycle while the
    live session still holds the turn's context.

    Like chat_runner's ``_posttoken_retry_used`` branch, the streamed partial is
    kept and the SAME session is re-prompted with a continuation instruction
    (never the original message, so completed work is not re-run); the result
    is partial + continuation. One-shot per turn: a transient during the
    continuation propagates. Only ``acp_error_is_transient`` errors qualify,
    and only after tokens streamed.

    Returns ``(text, carried_credits)``. ``carried_credits`` is ``None``
    without a resume; otherwise it is the interrupted prompt's credits,
    snapshotted before ``AcpPromptStats.carry_over()`` zeroes the counter, so
    the usage row bills both prompts.

    Accepted tradeoffs:

    - A side-effecting tool IN FLIGHT when the transient hit may be re-issued
      by the continuation (CONTINUE forbids re-running *completed* tools only).
      On an ``approval_mode == "auto"`` job that re-issue meets no gate and no
      human. Accepted because the window is rare.
    - The resume adds at most one prompt plus one backoff sleep inside the same
      per-wake ``asyncio.wait_for`` deadline; ``CancelledError`` is not caught.

    ``parts`` spans stream_and_collect's internal attempts. That retry fires
    only before any text streams, and we ASSUME (not enforced) a prompt-busy
    error is raised only at submission, so no chunks from an abandoned attempt
    end up in the text.

    A cron turn is never a person's, and no session applier reads its stream,
    so a directive only a person's turn may issue (``set_project``, ...) would
    end with only the tool's own "requested" text. Each one this turn completes
    is refused instead, audited as the chat path's refusal is, and its refusal
    is appended to ``directive_refusals`` for the caller to lead the run's
    result with (``person_only_directive_refuser``, shared with workflow agent
    steps).
    """
    from kiro_crew.dashboard.session_directive_apply import person_only_directive_refuser

    _refuse_person_only = person_only_directive_refuser(
        stream_kwargs.get("session_key", ""), directive_refusals
    )

    parts: list[str] = []
    preserved = ""
    carried_credits: float | None = None
    _resume_used = False  # one-shot, same style as slot._posttoken_retry_used
    msg = message
    while True:
        try:
            text = await stream_and_collect(
                client,
                msg,
                on_chunk=parts.append,
                # The continuation call owns NO further transient budget: its
                # in-stream retry re-sends the prompt whenever no text has
                # streamed, so a mutating tool completed by the continuation
                # followed by a pre-text transient would be re-run by the
                # inner replay — amplifying the one-shot. The first call keeps
                # the default (existing pre-token behaviour, unchanged).
                retry_transient=not _resume_used,
                on_event=_refuse_person_only,
                **stream_kwargs,
            )
            return preserved + text, carried_credits
        except Exception as exc:
            partial = "".join(parts)
            if _resume_used:
                # The continuation's own tag sees only the continuation. The
                # first prompt already produced output and may have run tools,
                # so a caller must never re-send the task after this.
                try:
                    setattr(exc, TOOL_ACTIVITY_ATTR, True)
                except Exception:
                    pass
            if _resume_used or not partial or not acp_error_is_transient(exc):
                raise
            _resume_used = True
            preserved = partial
            parts.clear()
            # Snapshot the interrupted prompt's billing NOW: sending the
            # continuation runs AcpPromptStats.carry_over(), which zeroes the
            # per-turn credit counter, and the caller's single post-turn read
            # would otherwise bill only the continuation.
            carried_credits = provider_last_turn_usage(client).credits
            _delay = transient_retry_delay(1)
            logger.warning(
                "Cron '%s': transient backend error after %d chars streamed — "
                "one-shot CONTINUE re-prompt of live session in %.1fs: %s",
                job_name,
                len(preserved),
                _delay,
                exc,
            )
            # The continuation is a second turn inside this claim. The
            # interrupted prompt's attempt ends here, before the backoff, so
            # what it received inline from a spawn_sub_agents call is revoked
            # and delivered, never confirmed by the continuation's landing.
            if on_resume is not None:
                on_resume()
            await asyncio.sleep(_delay)
            msg = _CRON_POSTTOKEN_CONTINUE_MSG
