"""What a cron run must clear before, and while, it dispatches.

The bounded fire-time governance gate and the ``run_never_started`` retention
marker it holds across a cancellation, the screen that keeps a job's ``env`` from
choosing its own governance, the first-run tab pre-create, the claim-time re-vet
inside the cron-pool worker with its handoff against an awaiter that gave up, and
the one-shot post-token CONTINUE resume of an agent turn.

The cron callback that sequences them stays in the facade: the usage-row,
runtime-death and dispatch-site audits count its call sites there.

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
# dashboard/chat_runner.py. The cron result is delivered once at the end of
# the turn, so the preserved partial is concatenated with the continuation
# instead of being re-shown to a live viewer.
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

    The gate runs on its OWN pool, never as a bare ``run_in_executor`` on the
    shared governance pool, and its TOTAL wait -- queue plus execution, which is
    why the budget is split across those phases rather than given to each -- is
    bounded below the wake deadline ``_execute_with_timeout`` has already armed.
    Both halves of that matter:

    * the shared governance pool is paced by REMOTE senders, so an inbound burst
      would put an unbounded FIFO backlog ahead of a cron gate; and
    * a message job carries no ``_pool_queue_allowance``, so that whole backlog
      would be charged to its execution budget.  With the wake deadline expiring
      first, ``_execute_with_timeout`` catches the ``TimeoutError`` and returns
      normally, so ``_merge_job_result`` sees an ordinary finished run -- and a
      ``delete_after_run`` job is consumed by a run that never dispatched.

    Bounding the wait below the wake budget makes starvation surface as
    ``CronQueueTimeout`` BEFORE the deadline can fire.  That is what makes the
    retention marker reachable: ``starved`` is reported to the caller and
    ``run_never_started`` is set here, which ``cron.py``'s delete site honours.
    The marker is deliberately not
    ``fire_time_denied`` -- that flag also parks an at-job disabled and records
    the event as a policy denial, and pool capacity is neither.

    ``record_failure()`` is deliberately NOT called, matching both the deny path
    and the command/script starvation handlers: a fleet-capacity state must not
    auto-pause a job that never ran a line.
    """
    budget = cron_gate_budget(effective_wake_budget(job))
    # Default to RETAIN for exactly the duration of the await, not only INSIDE the
    # handler below -- that is, not only when the await raises something that
    # handler catches.  A recoverable event-loop stall can carry wall clock past
    # the gate's own bounds AND the wake deadline, and the ``asyncio.wait_for`` in
    # ``_execute_with_timeout`` then cancels this coroutine AT the await: no
    # handler runs, so a marker armed only there would stay False, that timeout is
    # caught and returns normally, and ``_merge_job_result`` consumes a
    # ``delete_after_run`` job that never dispatched.  Sizing the internal bounds
    # correctly cannot prevent it, because nothing inside the call is scheduled to
    # notice.  ``CancelledError`` is a ``BaseException`` on both interpreters in
    # this matrix, so it escapes the ``except Exception`` below and the marker
    # survives.
    job.run_never_started = True
    try:
        reason = await run_in_cron_gate_pool(vet_job_at_fire_time, job, timeout=budget)
    except CronQueueTimeout as exc:
        # Scoped exactly as _run_job_isolated's own result-less clear
        # (close_run, cron_service/execution.py). For an agent/message job ``last_result`` is the
        # cross-run dedup context build_cron_session_context prepends as "do
        # NOT repeat", and a run starved here produced no result to replace it
        # -- clearing it would make the NEXT run repeat content it had already
        # sent. Command and script jobs still clear: the prompt built for them is
        # discarded, so a carried value could only show a previous run's output
        # beside this run's status. The message fire-time deny path below never
        # clears either, so all three sites agree.
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
    # A verdict came back, so this run reached its dispatch decision.  Clearing is
    # not optional: hold the marker past a verdict and a HEALTHY one-shot is
    # retained instead, so it fires again or never leaves the queue -- the same
    # data-integrity failure pointing the other way.  A DENY clears it too, because
    # its retention is owned by ``fire_time_denied``, whose readers also park an
    # at-job disabled; conflating them would park a job for a policy decision that
    # was never made.
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

    :func:`ensure_cron_slot` gives a first run its tab — and with it its
    session-control caller identity and dashboard-surface routing — before
    dispatch.  But the tab is an amenity of the run, not a precondition, and
    the first bind does transcript I/O.  Awaiting it BARE in the pre-dispatch
    window would let any failure there kill the run the tab was meant to serve —
    and worse than killing it: ``_execute`` clears ``run_never_started``
    before invoking this callback and its ``except`` arm never re-arms it, so
    an error propagating from here would reach ``cron.py``'s delete site with
    the retention marker down, and a ``delete_after_run`` one-shot — the DEFAULT
    shape of an at-scheduled job — would be consumed by a run that never
    dispatched.

    Same shape as :func:`_await_cron_fire_time_gate`, for the same reason:
    the marker is armed for exactly the duration of the await, so the wake
    deadline cancelling this coroutine AT the await leaves it standing
    (``CancelledError`` is a ``BaseException`` on every interpreter in this
    matrix and escapes the ``except Exception`` below) and the one-shot is
    retained.  An ordinary failure is contained instead of propagated: the
    run proceeds without the pre-created tab, losing first-run identity for
    this run only — delivery's own bind still creates the tab afterwards,
    which is exactly the status quo the pre-create improves on.  The clear on
    the linear path is not optional either: dispatch begins after this call,
    and holding the marker past it would retain a HEALTHY one-shot — the
    same data-integrity failure pointing the other way (the fire-time gate
    documents the identical contract).  ``record_failure()`` is deliberately
    not called anywhere here: a tab that could not be minted is not a defect
    of the job.
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

    :func:`run_in_cron_pool` reaches its execution phase only once a worker has
    CLAIMED the call, and a thread cannot be interrupted -- so when that phase
    times out the submitted callable keeps running.  That was tolerable while
    the claimed thread was already inside the sandbox, whose own ``timeout``
    bounds it.  With the claim-time vet the vet runs FIRST, so the deadline can
    land while the payload has not started, and it would then start after the
    caller's ``finally`` released the overlap guard -- running alongside the
    next fire.  ``run_in_cron_pool``'s own docstring names that harm for the
    queue phase ("reporting a queue timeout here would release the caller's
    overlap guard while the command runs, letting the next fire duplicate its
    side effects"); this closes the same hole for a deadline landing mid-vet.

    The lock is what makes the outcome DETERMINISTIC rather than a race.
    Exactly one of :meth:`claim` and :meth:`abandon` observes an unset flag, so
    a payload either starts -- and is reported as still running, which is the
    pre-existing claimed-and-running case -- or never starts at all.  Without
    it ``claim`` could read an unset flag that ``abandon`` sets an instant
    later, and the refusal would silently not happen.
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

    Starting the payload anyway is the harm: the remaining budget does not
    cover the subprocess bound plus
    :data:`~kiro_crew.cron._SUBPROC_CLEANUP_ALLOWANCE_SECS`, so the deadline
    would fire with the subprocess already running -- and a thread cannot be
    interrupted, so the overlap guard would clear while it runs on and the next
    fire would duplicate its side effects.  Refusing before the payload starts
    trades a reported missed run for a silent duplicate execution.

    Deliberately NOT a :class:`CronClaimTimeDenied` subclass -- an overrun is a
    budget fact, not a policy decision, and recording it as a denial would park
    an at-job disabled for a decision never made.  Deliberately NOT a
    :class:`CronQueueTimeout` subclass either: that arm reports "never got a
    worker slot", which a vet that ran for its full bound plainly did.
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
    client: Any, message: str, *, job_name: str, **stream_kwargs: Any
) -> tuple[str, float | None]:
    """Run a cron agent turn, resuming ONCE after a post-token transient error.

    Closes the seam between the two existing transient-retry layers:
    stream_and_collect's in-stream retry stops once tokens have streamed
    (re-running would duplicate the already-emitted output), and
    _cron_callback's whole-callback retry stops once the prompt is dispatched
    (tools may have run). A transient backend error raised AFTER the first
    token therefore failed the whole cycle even though the live session still
    holds the interrupted turn's context.

    Recovery mirrors the dashboard's post-token CONTINUE re-prompt
    (chat_runner's ``_posttoken_retry_used`` branch) and the subagent's
    ``_stream_with_transient_retry`` post-activity arm: the streamed partial is
    preserved, the SAME live session is re-prompted with a continuation
    instruction (never the original message, so completed work is not re-run),
    and the returned result is partial + continuation. The allowance is a
    strict one-shot per turn (``_resume_used``, same style as
    ``_posttoken_retry_used``): a transient error during the continuation
    propagates unchanged, so the unrecovered path records the error exactly as
    before.

    Returns ``(text, carried_credits)``. ``carried_credits`` is ``None`` when
    the turn completed without a resume; on a resumed turn it is the credits
    the INTERRUPTED prompt accumulated — snapshotted before the continuation
    prompt's ``AcpPromptStats.carry_over()`` zeroes the per-turn counter — so
    the caller's usage row can bill both prompts instead of only the
    continuation.

    Eligibility deliberately reuses ``acp_error_is_transient`` — the one
    authoritative classifier — so auth/validation failures and every other
    non-transient error propagate untouched. With NO tokens streamed the error
    also propagates untouched: that window is stream_and_collect's own retry's
    job, and by the time it raises here its budget is spent.

    Inherited tradeoffs, stated for the record (both are the mirrored owner
    decisions from chat_runner/subagent, extended here to the cron surface):

    - A side-effecting tool that was IN FLIGHT (dispatched, no completion)
      when the transient hit may be legitimately re-issued by the continuation
      turn — the CONTINUE instruction forbids re-running *completed* tools
      only. On an ``approval_mode == "auto"`` job that re-issue meets no gate
      and no human, an unattended posture narrower than the live-viewer
      surface the tradeoff is accepted for. Accepted: the window
      is rare (mid-flight tool AND a transient), and failing the whole cycle
      fast is the behaviour the resume exists to remove.
    - The resume adds at most one bounded prompt plus one backoff sleep to the
      cycle's worst case, inside the same per-wake ``asyncio.wait_for``
      deadline. A deadline firing mid-continuation degrades exactly as a
      deadline mid-turn does today (``CancelledError`` is not caught here), so
      no remaining-budget plumbing is added for a one-shot.

    ``parts`` observes chunks across stream_and_collect's internal attempts.
    Its transient retry only fires while no text has streamed, and — a stated
    ASSUMPTION about the provider, not an enforced invariant — a prompt-busy
    error is only raised at prompt submission, before this turn's stream emits
    chunks. Under that assumption the accumulated text never contains chunks
    from an abandoned attempt.
    """
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
                **stream_kwargs,
            )
            return preserved + text, carried_credits
        except Exception as exc:
            partial = "".join(parts)
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
            await asyncio.sleep(_delay)
            msg = _CRON_POSTTOKEN_CONTINUE_MSG
