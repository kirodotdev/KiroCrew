"""The runs in flight: their claims, the session keys they register, and a manual run.

A run's claim lives in :class:`~kiro_crew.cron_service.claims.RunClaims`; these are
the service's doors onto it, plus the exact live session keys a reap or cancel ends.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_active_session_keys``, ``_push_refresh``, ``_timeout_alert``.

Instance fields read: ``_active_session_lock``, ``_history``, ``_runs``.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Coroutine

from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.cron_service.claims import _manual_run_refused, _resolve_started, _RunClaim
from kiro_crew.cron_service.store import CronStoreBusy, CronStoreUnreadable

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


# ── Active session tracking ──


def register_active_session_key(self: CronService, job_id: str, session_key: str) -> None:
    """Register one live exact key for ``job_id`` under the run that holds the job's claim.

    Several distinct keys may coexist: a finished stateless run can stay alive
    for pending subagents while a newer run starts, and a sequential job holds
    one key per agent inside one run. Each key is attributed to the run that
    registered it -- the ``_RunClaim`` stored for the job at that moment, the
    run the gateway's cron callback is executing under -- so a reap or cancel
    of that run ends every key it registered and no other run's
    (:meth:`_run_session_keys`). Pop-and-reinsert refreshes recency for
    reaper/cancel targeting and re-attributes a stable key to the run
    registering it now. Re-registering the same exact key is idempotent
    because all claimants share one SessionManager runtime.
    """
    # Loop-owned, read on the loop: both product registrants run inside the
    # cron callback, under the claim ``_run_job_isolated`` holds for the run.
    run = self._claims.get(job_id)
    with self._active_session_lock:
        keys = self._active_session_keys.setdefault(job_id, {})
        keys.pop(session_key, None)
        keys[session_key] = run


def clear_active_session_key(self: CronService, job_id: str, session_key: str) -> None:
    """Retire the exact key whose SessionManager reset completed."""
    with self._active_session_lock:
        keys = self._active_session_keys.get(job_id)
        if not keys:
            return
        keys.pop(session_key, None)
        if not keys:
            self._active_session_keys.pop(job_id, None)


def _run_session_keys(self: CronService, job_id: str, run: _RunClaim) -> list[str]:
    """Every live exact key ``run`` registered, newest first: the keys a reap or cancel of that run ends.

    A key another run registered -- a finished run's session kept alive for
    its pending sub-agents -- is not this run's and is left to its own end.
    A key registered under no claim (no product path registers outside the
    run callback; legacy callers and tests) belongs to whichever run of the
    job is ended next, as the newest key did before keys were attributed.
    Empty when the run registered none: the caller then ends the job's
    stable key.
    """
    with self._active_session_lock:
        keys = self._active_session_keys.get(job_id) or {}
        return [key for key, owner in reversed(keys.items()) if owner is run or owner is None]


def active_session_keys(self: CronService) -> frozenset[str]:
    """Snapshot every exact live key across current and deferred cron runs."""
    with self._active_session_lock:
        return frozenset(
            session_key for keys in self._active_session_keys.values() for session_key in keys
        )


def get_history(self: CronService) -> CronHistoryStore:
    """Public accessor for the history store."""
    return self._history


def is_running(self: CronService, job_id: str) -> bool:
    """Return whether a job is currently executing."""
    return job_id in self._claims


def _claim_run(self: CronService, job_id: str, trigger: str) -> _RunClaim:
    """Claim ``job_id`` for a new run and return the claim (:meth:`RunClaims.claim`).

    The dispatchers' one door: :meth:`run_job` and the due-scan call it in
    the loop step that found the job idle.
    """
    return self._runs.claim(job_id, trigger)


def attach_run_task(self: CronService, job_id: str, task: asyncio.Task[Any]) -> None:
    """Track ``task`` as the run that :meth:`run_job` just claimed ``job_id`` for.

    The manual-run route wraps the coroutine ``run_job`` returns in a task
    on the same line and hands it in here, still await-free, so ``cancel()``
    can reach a run parked in its store refresh and ``stop()`` can await it.
    See :meth:`RunClaims.attach_task`.
    """
    self._runs.attach_task(job_id, task)


def discard_finished_run(self: CronService, job_id: str) -> bool:
    """Drop the claim of a run whose task has already finished (:meth:`RunClaims.discard_finished`).

    Asked first by the three consumers that gate on "is a run in flight?" --
    the manual-run route before its 409, the reaper sweep before its
    deadline math, and :meth:`cancel` before its guard. Returns True when a
    stale claim was dropped.
    """
    return self._runs.discard_finished(job_id)


def running_since(self: CronService, job_id: str) -> float | None:
    """Return the epoch start time of a running job, or None (:meth:`RunClaims.running_since`)."""
    return self._runs.running_since(job_id)


def set_refresh_callback(self: CronService, cb: Any) -> None:
    """Set the dashboard refresh callback."""
    self._push_refresh = cb


def set_timeout_alert(self: CronService, cb: Any) -> None:
    """Set the coroutine function ``cb(job, detail)`` that tells the user a run timed out.

    The gateway sets its cron failure alert here. A run its deadline ended never
    returns to the callback that would have reported it, so without this the
    timeout reaches the history and the log and no one is told.
    """
    self._timeout_alert = cb


def trigger_run(
    self: CronService,
    job_id: str,
    *,
    expected_owner: str | None = None,
    started: asyncio.Future[bool] | None = None,
) -> asyncio.Task[bool] | None:
    """Start a run of ``job_id`` now, off schedule, unless one is in flight.

    The one shape of "run this job now" for every on-loop trigger: the
    manual-run route and ``CronSDK.run_job_async`` both call it, so a change
    to the guard, the claim or the task tracking lands in one place.

    Returns the dispatched wrapper task, or ``None`` when a run of the job
    is already in flight. The caller does not wait for the run; the outcome
    lands in the job's own history.

    Loop-only and await-free from top to bottom, which is what makes the
    check and the claim atomic: no due-scan tick and no second trigger can
    interleave between the guard and the claim ``run_job`` takes while its
    call expression is evaluated. Keep it a plain ``def``; an ``await``
    anywhere in here reopens that window.

    * ``discard_finished_run`` comes first. A run whose task ended without
      reaching ``_run_job_isolated``'s finally leaves its claim stored with
      nothing on that path to release it, and the guard alone would then
      refuse every run of the job until the reaper sweep meets the
      finished task.
    * The guard refuses rather than queues. A second overlapping run would
      orphan the first task's handle, leaving nothing able to track, cancel
      or join it.
    * ``attach_run_task`` on the line after ``create_task``, so ``cancel()``
      can reach a run still parked in its offloaded store refresh and
      ``stop()`` can await it at shutdown.
    * The Schedule page is told a run started, whoever triggered it.

    Ownership and existence checks belong to the caller, before this call.
    A job absent from the store still gets a claim, which
    ``_run_claimed_manual`` releases once its refresh does not find the job.

    ``expected_owner`` is for a caller whose ownership check read the
    cache (the app SDK): it is passed to :meth:`run_job`, which re-checks
    the job's ``created_by`` against it on the job the run actually
    resolves from the refreshed store, and does not run a job whose owner
    differs from it. The dashboard route has no app owner and passes
    nothing.

    ``started`` is for a caller that must report whether the run really
    began, not only whether it was claimed (the app SDK). It is resolved
    ``True`` by the run's own first step, after the store refresh and every
    check on the resolved job have passed and the run has confirmed it
    still holds its claim, and ``False`` when the dispatched task ends
    without the run reaching that step: a job missing from the store, an
    owner mismatch, a store too busy or too unreadable to re-check the
    owner, or a
    ``cancel()`` or ``stop()`` that took the run before its first step. A
    run that starts and is then refused inside its own body (the fire-time
    gate of a disabled app) resolves ``True``, because that refusal is recorded in the job's
    history like any other run outcome. When this returns ``None`` the
    future is left untouched, so the caller decides how to answer.
    """
    self.discard_finished_run(job_id)
    if self.is_running(job_id):
        return None
    if expected_owner is None and started is None:
        coro = self.run_job(job_id)
    else:
        coro = self.run_job(job_id, expected_owner=expected_owner, started=started)
    task = asyncio.create_task(coro)
    self.attach_run_task(job_id, task)
    if started is not None:
        # Every path that ends the task without spawning a run resolves
        # the future False here, including a task cancelled before its
        # body ran, so a caller awaiting it is never left waiting.
        task.add_done_callback(partial(_resolve_started, started, False))
    if self._push_refresh:
        self._push_refresh("crons")
    return task


def run_job(
    self: CronService,
    job_id: str,
    *,
    expected_owner: str | None = None,
    started: asyncio.Future[bool] | None = None,
) -> Coroutine[Any, Any, bool]:
    """Manually trigger a job via _run_job_isolated (records history).

    A plain ``def`` that returns the coroutine, on purpose: the claim on the
    job -- its ``_RunClaim`` with ``trigger="manual"`` -- is taken HERE,
    synchronously, while the call expression is evaluated. It therefore
    exists before the caller's ``asyncio.create_task`` has scheduled
    anything and before the coroutine's first ``await``. The manual-run
    route answers 409 "already running" off the claim and hands the task it
    creates to ``attach_run_task``, and ``cancel()`` reads the same claim: a
    claim taken only inside the coroutine -- after the offloaded store
    refresh, up to a full lock spin later -- leaves a window in which Cancel
    answers "not running" about a run that Run calls "already running", and
    the run then executes anyway. Loop-only and await-free, so the
    check-and-claim stays atomic against the due-scan and a concurrent
    manual trigger; ``_run_claimed_manual`` releases the claim if the store
    does not hold the job. Every caller awaits or schedules the returned
    coroutine at once (the route wraps it in a task on the same line); one
    that dropped it would leave the job claimed.

    ``expected_owner``, when given, must equal the ``created_by`` of the job
    the refreshed store resolves; otherwise the claim is released and the
    run does not start. ``started`` is resolved ``True`` when the run is
    spawned. See :meth:`trigger_run` for both.
    """
    if job_id in self._claims:
        return _manual_run_refused()
    claim = self._claim_run(job_id, "manual")
    return self._run_claimed_manual(job_id, claim, expected_owner, started)


def _audit_owned_run_refusal(self: CronService, owner: str, job_id: str, reason: str) -> None:
    """SEL-audit an owner-checked manual run the store refused to start.

    The caller already audited the dispatch as "ok" off its cached check,
    so the refusal gets its own record under the same operation the SDK
    uses. Contained: the refusal is already enforced, and an audit outage
    must not turn it into a raise.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        seams.sel.sel().log_api_access(
            caller=owner,
            operation="cron_run_job",
            outcome="denied",
            resources=job_id,
            error=reason,
        )
    except Exception:
        logger.warning("SEL audit for a refused manual run failed (job %s)", job_id, exc_info=True)


async def _run_claimed_manual(
    self: CronService,
    job_id: str,
    claim: _RunClaim,
    expected_owner: str | None = None,
    started: asyncio.Future[bool] | None = None,
) -> bool:
    """Body of :meth:`run_job`, entered with the claim already taken."""
    # Refresh the store off the loop, then resolve + spawn on the loop.
    #
    # The locked _sync() + snapshot runs in a worker thread (_synced_snapshot
    # via asyncio.to_thread) so a manual trigger never pays the whole-file
    # read_bytes() + blake2b hash of crons.json on the event loop.
    # _claims is loop-owned; the claim was taken on the loop before this
    # coroutine started, so a cancel() that lands while the refresh is in
    # flight finds the run and cancels THIS task.
    #
    # One residual, benign race remains against the batch-remove worker: it
    # may delete the job on its own thread in the instant between our
    # snapshot and our spawn, so a manual run can execute a just-removed job
    # ONCE (non-destructive — it is not persisted, and the next scan won't
    # see it). The batch-remove worker holds the SAME flock, so a lock-held
    # claim could not observe a delete mid-way regardless. Degrades to the
    # in-memory snapshot under lock contention.
    try:
        if expected_owner is None:
            snapshot = await asyncio.to_thread(self._synced_snapshot, True)
        else:
            # The owner re-check below must read the store, not the cache
            # the caller's own check already used, so a contended lock or
            # an unreadable store fails the run here (the claim is released
            # below) instead of degrading to that cache.
            snapshot = await asyncio.to_thread(self._synced_snapshot, True, strict=True)
    except BaseException as exc:
        if expected_owner is not None and isinstance(exc, (CronStoreBusy, CronStoreUnreadable)):
            # Both strict-refresh failures are refusals of a dispatch the
            # caller already audited "ok" off its cached check, so each one
            # gets its own record naming which of the two it was.
            self._audit_owned_run_refusal(
                expected_owner,
                job_id,
                f"{'store busy' if isinstance(exc, CronStoreBusy) else 'store unreadable'}"
                ": owner could not be re-checked",
            )
        # No run will consume the claim: release it unless cancel() already
        # took it (the fence fails) or a newer claim has replaced it. A
        # cancel flag cancel() set goes with the claim.
        self._runs.release(job_id, claim)
        raise
    if not self._runs.holds(job_id, claim):
        # cancel() took the claim while the refresh was in flight and is
        # still tearing down: it takes the claim first and cancels the
        # tracked task only after its process-kill / session-reset awaits,
        # so this coroutine can resume inside that gap. Dispatching here
        # would start the very run cancel() is about to report cancelled.
        return False
    job = next((j for j in snapshot if j.id == job_id), None)
    if not job:
        self._runs.release(job_id, claim)
        return False
    if expected_owner is not None and job.created_by != expected_owner:
        # The caller's ownership check read the cache, which can be up to a
        # poll interval stale; this is the job the run would actually
        # execute, read under the store lock. A job id that now names a
        # job someone else owns is refused here, at the last point before
        # dispatch, so a stale cache can never start another owner's job.
        self._runs.release(job_id, claim)
        logger.warning(
            "Cron: manual run of %s refused: the store now records owner %r, "
            "the trigger expected %r",
            job_id,
            job.created_by,
            expected_owner,
        )
        self._audit_owned_run_refusal(expected_owner, job_id, "ownership mismatch in store")
        return False
    # ``started`` is resolved by the run itself, once its first step has
    # confirmed it still holds the claim. A task cancelled before that
    # step (stop() at shutdown, cancel()) runs nothing and writes no
    # history, so resolving here, at spawn, would report a start that
    # never happened; trigger_run's done callback answers False instead.
    if started is None:
        run = self._run_job_isolated(job, claim)
    else:
        run = self._run_job_isolated(job, claim, started)
    task = asyncio.create_task(run)
    claim.task = task
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise  # outer coroutine was cancelled, propagate
    finally:
        # Backstop for a run whose finally was cut short, idempotent with
        # it and fenced the same way: the wrapper resumes one loop
        # iteration after the run ends, and a claim that is not this
        # wrapper's by then belongs to a replacement run.
        if task.done():
            self._runs.release(job.id, claim)
    return True
