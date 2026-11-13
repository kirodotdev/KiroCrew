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
    identity. ``eq=False`` keeps identity semantics, so a copied or reconstructed
    claim can never pass a fence (:meth:`RunClaims.holds`).

    ``trigger``, ``claimed_at`` and ``marker_run`` are fixed at claim time; the
    other stamps are set by ``_run_job_isolated`` as the run body reaches them.
    Releasing a claim is popping it, so a field added here inherits every fence
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
    #: The job object this run executes and mutates, stamped with ``started_monotonic``.
    #: Every store reload replaces the registry's job objects, and the runtime-only
    #: markers the run set (``run_never_started``, ``fire_time_denied``) stay on this
    #: one, so the reaper reads whether the payload ever started here and not off the
    #: registry's replacement. None until the run body starts. Left out of the claim's
    #: repr, so a logged or asserted claim does not print the whole job.
    job: CronJob | None = field(default=None, repr=False)
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
    #: Set by ``_execute`` when it resets this run's outcome fields. Until then
    #: the job still holds the previous run's ``last_status`` / ``last_error``,
    #: so a run stopped before this point has no outcome of its own.
    outcome_reset: bool = False


class RunClaims:
    """Every job's run claim and the generations handed out.

    ``claims`` maps a job id to the claim of the run occupying it; membership
    means "the job is running". Releasing pops the claim, so every field,
    the cancel and reap flags included, goes with the run it was set for.

    ``generations`` is the highest run generation allocated per job (see
    :meth:`next_generation`). It lives here, not on the job, because a store
    reload replaces the job objects and would lose the counter before the
    run's merge persisted it. :meth:`release` drops the entry: by then the
    persisted ``job.run_generation`` carries it, and a one-shot's entry would
    otherwise leak.
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

        Identity, not equality: every new claim is a new object, so one that is
        not the stored object belongs to another run. :meth:`release` and the
        fences in ``_run_job_isolated`` and ``_run_claimed_manual`` all ask this,
        so an older run never releases a newer claim. A taken run releases
        nothing: ``cancel()`` or the reaper finishes it after their kill awaits,
        while the stored claim keeps other claimants out.
        """
        stored = self.claims.get(job_id)
        return stored is not None and stored is claim and not stored.taken

    def release(self, job_id: str, claim: _RunClaim | None) -> bool:
        """Release ``claim`` -- every field at once -- if it still owns the job.

        Drops the job's run-generation entry too, so a one-shot (fresh ``uuid4``
        id, fired once) does not leak one int for the gateway's life. Safe because
        :meth:`next_generation` also reads the persisted ``job.run_generation``,
        which the held terminal record (``_hold_run_record``) already carries.
        A taken claim keeps its entry: ``cancel()``/``_force_reap`` draw the
        terminal row's generation after finishing the claim.
        """
        if not self.holds(job_id, claim):
            return False
        del self.claims[job_id]
        self.generations.pop(job_id, None)
        return True

    def take(self, job_id: str, expected: _RunClaim | None = None) -> _RunClaim | None:
        """Take over the stored claim for a teardown (``cancel()`` / ``_force_reap``).

        Returns the claim, or None when there is none or another teardown took
        it. A taken claim stays stored so the job reads as occupied through the
        kill awaits, while the run's own fences fail and the reaper skips it;
        :meth:`finish_taken` pops it afterwards. A caller handed None must stop:
        going on would pop the taker's claim mid-kill and write a second
        terminal row.

        With ``expected``, only that object is taken. The reaper decides from a
        snapshot read before an await, so a different stored object is a
        replacement run it never measured. ``cancel()`` checks and takes in one
        loop step, so it passes no ``expected`` and takes the current run.
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

        A task that ends without reaching ``_run_job_isolated``'s ``finally``
        leaves its claim stored, so the job would read as running for the
        gateway's life. The manual-run route, the reaper sweep and ``cancel()``
        ask here first. A live or untracked task is left untouched, and so is a
        TAKEN claim: dropping it would read the job idle mid-teardown and let
        the teardown's terminal row outrank a replacement run's generation
        (:meth:`finish_taken` pops it). Returns True when a stale claim was dropped.
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

        One above both ``job.run_generation`` and the highest this process handed
        out, so two runs never share a number: not on equal timestamps (Windows'
        ``time.time()`` ticks every ~15.6 ms), across reloads, or across restarts.
        Callers draw it BEFORE releasing the run's claim with no ``await`` in
        between, so a replacement claim always draws higher. No store lock: the
        merge that carries the number persists it under the lock.
        """
        generation = max(job.run_generation, self.generations.get(job.id, 0)) + 1
        self.generations[job.id] = generation
        return generation
