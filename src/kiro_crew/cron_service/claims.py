"""A run's occupancy of its job: the run claim and its fences.

One claim (:class:`_RunClaim`) per job in flight holds every piece of per-run
state -- the cancel and reap markers included, as two flags on the claim;
:class:`RunClaims` is the only place a claim is stored, fenced, taken and
released, so a field added to the claim inherits every fence. The
run-generation counter lives here too, because it is allocated while a claim
is held.

Loop-owned: every claim, fence and release happens on the event loop, await-free.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.cron_service.model import CronJob

logger = logging.getLogger("kiro_crew.cron")


async def _manual_run_refused() -> bool:
    """The coroutine :meth:`CronService.run_job` hands back without a claim.

    ``run_job`` decides synchronously whether the job is already executing; a
    refused trigger still has to return something the caller can ``await`` or
    wrap in a task, so it gets this instead of the claimed body.
    """
    return False


@dataclass(eq=False)
class _RunClaim:
    """One run's claim on its job: every piece of per-run state, in one object.

    Created when the job is claimed (:meth:`RunClaims.claim`) and stored as the
    job's single entry in :attr:`RunClaims.claims`; the claim IS the run's
    identity. ``eq=False`` keeps dataclass identity semantics: two claims are the
    same run only when they are the same object, so a copied or reconstructed
    claim can never pass a fence -- the property the ``(started, trigger)`` meta
    tuple this replaces had only by accident of construction, and every fence
    (:meth:`RunClaims.holds`) relies on.

    A claim's life: ``trigger``, ``claimed_at`` and ``marker_run`` are fixed at
    claim time. ``task`` is the handle the dispatcher tracks (the manual route's
    wrapper, then the run task that replaces it). ``generation``,
    ``started_monotonic`` and ``jitter`` are stamped by ``_run_job_isolated`` as
    the run body reaches them. ``taken`` is set by ``cancel()`` / ``_force_reap``
    when they take over the run's terminal write: from then on the run's own
    fences fail, the reaper skips it and ``running_since`` is silent, while the
    claim stays stored so the job keeps reading as occupied until the teardown
    pops it after its kill awaits. Releasing a claim is popping it -- one
    operation for every field, so a field added here inherits every fence
    instead of needing its own pop at every release site.
    """

    #: ``"manual"`` (``run_job``) or ``"scheduled"`` (the due-scan).
    trigger: str
    #: Epoch time the claim was taken -- the run's ``started_at`` for its
    #: history row and ``running_since``.
    claimed_at: float
    #: Per-run token in the on-disk in-flight marker's file name, so the
    #: finalizer clears only its own run's marker (see ``cron_inflight``).
    marker_run: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: The run generation drawn at the run's first step (:meth:`RunClaims.next_generation`),
    #: stamped on the record the finalizer merges. None until drawn.
    generation: int | None = None
    #: ``time.monotonic()`` when the run body started; the reaper's deadline clock.
    #: None while a manual claim is still parked in its store refresh (the
    #: reaper then measures on the wall clock from ``claimed_at``).
    started_monotonic: float | None = None
    #: Jitter seconds the run slept before executing; the reaper's allowance.
    jitter: float | None = None
    #: The task the dispatcher tracks for this run, for cancel / reap / stop.
    task: asyncio.Task[Any] | None = None
    #: Set by ``cancel()`` / ``_force_reap`` once they own the run's terminal
    #: write. The claim stays stored (the job is still occupied) but is no
    #: longer the run's to release, and the reaper leaves it alone. A lock
    #: with one release: the taker finishes it in a ``finally`` around its
    #: kill awaits, so a teardown that fails still pops the claim it took.
    taken: bool = False
    #: Set by ``cancel()`` on the claim it took, before its kill awaits: this
    #: run was cancelled by the user, so its finalizer writes no terminal row
    #: of its own (``cancel()`` writes the cancelled row) and ``_execute``
    #: leaves ``consecutive_failures`` alone. A flag on the claim cannot
    #: outlive its run or be read by another run of the job, so no site has
    #: to consume it.
    cancelled: bool = False
    #: Set by ``_force_reap`` on the claim it took, before its kill awaits:
    #: the reaper killed this run and writes its terminal row, so the run's
    #: finalizer writes none. Same lifetime as ``cancelled``.
    reaped: bool = False


class RunClaims:
    """Every job's run claim and the generations handed out.

    ``claims`` maps a job id to the claim of the run that occupies the job;
    membership is "the job is running" for the due-scan, the next-wake
    computation, ``run_job`` and the manual-run route, and a claim is released
    by popping it, one operation for every field -- the cancel and reap flags
    (:attr:`_RunClaim.cancelled`, :attr:`_RunClaim.reaped`) included, so they
    go with the run they were set for. ``generations`` is the highest
    run generation this process has allocated per job (see
    :meth:`next_generation`): service state, not job state, because every store
    reload replaces the job objects and a counter kept on one of them would be
    lost with it before the run's merge persisted it.
    """

    def __init__(self) -> None:
        self.claims: dict[str, _RunClaim] = {}
        self.generations: dict[str, int] = {}

    def claim(self, job_id: str, trigger: str) -> _RunClaim:
        """Claim ``job_id`` for a new run and return the claim. Loop-side, await-free.

        The caller has just established that the job is idle in this same loop
        step (``run_job``'s guard, the due-scan's ``not in self.claims`` filter),
        so the claim is stored unconditionally and becomes the job's occupancy at
        once. The claim is handed to the run task rather than read back by it.
        """
        claim = _RunClaim(trigger=trigger, claimed_at=time.time())
        self.claims[job_id] = claim
        return claim

    def holds(self, job_id: str, claim: _RunClaim | None) -> bool:
        """Whether ``claim`` still owns ``job_id``'s run: the stored object, not taken.

        Identity, not equality: ``cancel()`` and the reaper take the stored claim
        when they tear a run down, and any later manual or scheduled claim stores
        a NEW object, so a claim that is not the stored one belongs to someone
        else and is left alone. Every site that releases a job's run state --
        :meth:`release` and the fences in ``_run_job_isolated`` and
        ``_run_claimed_manual`` -- asks this same question, so a newer claim is
        never released by an older run, and a run that ``cancel()`` or the
        reaper has taken releases nothing: they finish it after their kill
        awaits, while its claim still keeps every other claimant out. The cancel
        and reap flags live on the same object, so a finalizer reads only the
        flags set for its own run.
        """
        stored = self.claims.get(job_id)
        return stored is not None and stored is claim and not stored.taken

    def release(self, job_id: str, claim: _RunClaim | None) -> bool:
        """Release ``claim`` -- every field at once -- if it still owns the job."""
        if not self.holds(job_id, claim):
            return False
        del self.claims[job_id]
        return True

    def take(self, job_id: str, expected: _RunClaim | None = None) -> _RunClaim | None:
        """Take over the stored claim for a teardown (``cancel()`` / ``_force_reap``).

        Returns the claim if this caller is the one taking it, None when there
        is none or another teardown already took it. A taken claim stays stored
        so the job keeps reading as occupied through the kill awaits, but the
        run's own fences fail from here on and the reaper skips it;
        :meth:`finish_taken` pops it once the teardown is done killing.
        Only the taker finishes: a caller handed None owns nothing of the run
        and stops there, because pressing on would pop the taker's claim out
        from under its kill awaits and write a second terminal row.

        With ``expected``, only THAT object is taken (identity, like every
        claim fence). A caller that decided on a teardown from a claim it read
        before an await -- the reaper sweep, measuring a snapshot of the claims
        -- names the run it measured; a stored claim that is a different object
        is a replacement run it never measured, and is left untaken. Without
        it -- ``cancel()``, keyed by job like its route, guarding and taking
        in one loop step -- the job's current run is taken, whichever it is.
        """
        claim = self.claims.get(job_id)
        if claim is None or claim.taken:
            return None
        if expected is not None and claim is not expected:
            return None
        claim.taken = True
        return claim

    def finish_taken(self, job_id: str) -> None:
        """Pop the taken claim and cancel its task: the last step of a teardown.

        Only a TAKEN claim is popped. A claim that is not taken belongs to a
        replacement run accepted after this teardown's own claim left the
        store some other way, and is never this teardown's to release.
        Idempotent with ``_run_job_isolated``'s finally.
        """
        stored = self.claims.get(job_id)
        if stored is None or not stored.taken:
            return
        del self.claims[job_id]
        task = stored.task
        if task is not None and not task.done():
            task.cancel()

    def attach_task(self, job_id: str, task: asyncio.Task[Any]) -> None:
        """Track ``task`` as the run that ``CronService.run_job`` just claimed ``job_id`` for.

        The manual-run route wraps the coroutine ``run_job`` returns in a task
        on the same line and hands it in here, still await-free, so ``cancel()``
        can reach a run parked in its store refresh and ``stop()`` can await it.
        The run task ``_run_claimed_manual`` spawns later replaces it. A claim
        that already tracks a task, or no claim at all, is left alone.
        """
        claim = self.claims.get(job_id)
        if claim is not None and claim.task is None:
            claim.task = task

    def discard_finished(self, job_id: str) -> bool:
        """Drop the claim of a run whose task has already finished.

        A claim is released by ``_run_job_isolated``'s ``finally``. A task that
        ends without reaching it leaves the claim stored with nothing left to
        clear it, so the job reads as running for the life of the gateway:
        every manual run of it is refused, and the due-scan, ``_next_wake_secs``
        and ``run_job`` -- which all skip a claimed job -- pass over every
        scheduled fire. The three consumers that gate on "is a run in flight?"
        ask here first: the manual-run route before its 409, the reaper sweep
        before its deadline math, and ``cancel()`` before its guard, so a
        finished task is never mistaken for a live one. A task still running,
        or no tracked task at all, is left untouched -- and so is a claim
        ``cancel()`` or the reaper has taken, whatever its task's state: its
        task ending is the expected effect of that teardown's kill, not an
        orphan, and the teardown pops the claim itself once its kill awaits
        return (:meth:`finish_taken`). Dropping it here would read the job
        idle mid-teardown, admit a replacement run, and let the teardown's
        terminal row outrank that run's generation. Returns True when a stale
        claim was dropped.
        """
        claim = self.claims.get(job_id)
        if claim is None or claim.taken or claim.task is None or not claim.task.done():
            return False
        del self.claims[job_id]
        logger.warning(
            "Cron: dropped stale running markers for job %s -- its task had already finished",
            job_id,
        )
        return True

    def running_since(self, job_id: str) -> float | None:
        """Return the epoch start time of a running job, or None.

        Silent for a run ``cancel()`` or the reaper has taken: its terminal row
        is being written and the badge clears, as it did when they popped the
        run's start stamp ahead of their kill awaits.
        """
        claim = self.claims.get(job_id)
        if claim is None or claim.taken:
            return None
        return claim.claimed_at

    def next_generation(self, job: CronJob) -> int:
        """Allocate the next run generation of ``job``. Loop-side, no store I/O.

        One above both the generation the store holds for the job
        (``job.run_generation``: the last record merged, as of the object's
        last sync) and the highest this process has handed out for it, so two
        runs of one job never share a number -- whatever the clock says
        (Windows' ``time.time()`` ticks every ~15.6 ms, so two claims in one
        tick carry equal timestamps), across the reloads that replace the job
        object before a run's merge lands, and across restarts, which resume
        above the persisted value. Called once per run at its first step (and
        kept on its claim), and by ``cancel()`` / ``_force_reap`` for the
        terminal record they write, each BEFORE the run's claim is released
        and without an ``await`` in between, so a replacement claim always
        draws a higher number. Not a locked store write: the claim sites are
        loop-resident and never take the store lock, and the merge that carries
        the number persists it under the lock anyway.
        """
        generation = max(job.run_generation, self.generations.get(job.id, 0)) + 1
        self.generations[job.id] = generation
        return generation
