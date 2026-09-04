"""Tests for the bounded maintenance/cron thread pools.

The key invariant: cron execution and the periodic orphan-reaping sweeps use
*separate* pools, so a burst of long-running concurrent cron jobs can never
occupy all the maintenance threads and starve the sweeps that reap leaked
kiro-cli/MCP children (the documented root cause of the loop wedge).
"""

from __future__ import annotations

import asyncio
import threading
from unittest import mock

import pytest

import kiro_crew.executors as ex


def teardown_function() -> None:
    # Don't leak pools between tests (the module memoizes them process-wide).
    ex.shutdown_maintenance_executor()


def test_maintenance_and_cron_are_distinct_pools() -> None:
    maint = ex.maintenance_executor()
    cron = ex.cron_executor()
    assert maint is not cron, "cron must not share the maintenance pool"


def test_subprocess_pool_is_distinct_from_maintenance_and_cron() -> None:
    # The whole point of the subprocess pool: a teardown call that blocks on a
    # wedged kernel resource (PTY os.close, hung ps/pgrep) must NOT share workers
    # with the orphan-reaping sweep that is the recovery action for the wedge.
    subproc = ex.subprocess_executor()
    assert subproc is not ex.maintenance_executor()
    assert subproc is not ex.cron_executor()


def test_pools_are_memoized() -> None:
    assert ex.maintenance_executor() is ex.maintenance_executor()
    assert ex.subprocess_executor() is ex.subprocess_executor()
    assert ex.cron_executor() is ex.cron_executor()


def test_thread_name_prefixes_distinguish_pools() -> None:
    # Watchdog stack dumps identify the offending pool by thread name.
    assert ex.maintenance_executor()._thread_name_prefix == "mc-maint"
    assert ex.subprocess_executor()._thread_name_prefix == "mc-subproc"
    assert ex.cron_executor()._thread_name_prefix == "mc-cron"


def test_pools_are_bounded() -> None:
    # Bounded so a flood of concurrent work queues rather than spawning
    # unbounded threads.
    assert ex.cron_executor()._max_workers == ex._MAX_CRON_WORKERS
    assert ex.maintenance_executor()._max_workers == ex._MAX_MAINT_WORKERS
    assert ex.subprocess_executor()._max_workers == ex._MAX_SUBPROCESS_WORKERS


def test_shutdown_is_idempotent_and_resets() -> None:
    first = ex.maintenance_executor()
    first_subproc = ex.subprocess_executor()
    first_cron = ex.cron_executor()
    ex.shutdown_maintenance_executor()
    ex.shutdown_maintenance_executor()  # second call must not raise
    # After shutdown a fresh pool is created on next use.
    assert ex.maintenance_executor() is not first
    assert ex.subprocess_executor() is not first_subproc
    assert ex.cron_executor() is not first_cron


def test_pools_execute_work() -> None:
    assert ex.maintenance_executor().submit(lambda: 1 + 1).result(timeout=5) == 2
    assert ex.subprocess_executor().submit(lambda: 4 + 4).result(timeout=5) == 8
    assert ex.cron_executor().submit(lambda: 2 + 3).result(timeout=5) == 5


def test_path_resolve_pool_is_isolated_bounded_named_and_reset() -> None:
    # The sensitive-path gates' realpath used to run inline on the event loop and
    # could block in the kernel on a stalled automount for as long as the mount
    # did.  The caller now bounds its wait, but a timed-out future does NOT free
    # its thread -- so a wedged lstat must only ever be able to starve OTHER
    # path resolution, never the sweeps, teardown, or the default executor.
    pool = ex.path_resolve_executor()
    assert pool is ex.path_resolve_executor()
    for other in (
        ex.maintenance_executor(),
        ex.subprocess_executor(),
        ex.cron_executor(),
        ex.discovery_executor(),
        ex.governance_executor(),
    ):
        assert pool is not other
    assert pool._max_workers == ex._MAX_PATH_RESOLVE_WORKERS
    assert pool._thread_name_prefix == "mc-pathres"
    assert pool.submit(lambda: 6 * 7).result(timeout=5) == 42
    ex.shutdown_maintenance_executor()
    assert ex.path_resolve_executor() is not pool


def test_governance_pool_is_isolated_bounded_and_reset() -> None:
    # GPT round-7 pass 3: the governance pool (externally-paced inbound channels
    # gate + dashboard governance GETs) must be a DISTINCT, bounded, shutdown-
    # resettable pool so a remote message burst can't occupy the maintenance
    # workers the orphan sweeps need.
    gov = ex.governance_executor()
    # Distinct from every other pool.
    assert gov is not ex.maintenance_executor()
    assert gov is not ex.subprocess_executor()
    assert gov is not ex.cron_executor()
    assert gov is not ex.discovery_executor()
    assert gov is not ex.embed_executor()
    # Memoized, named, bounded.
    assert ex.governance_executor() is gov
    assert gov._thread_name_prefix == "mc-gov"
    assert gov._max_workers == ex._MAX_GOVERNANCE_WORKERS
    # Executes work.
    assert gov.submit(lambda: 7 * 6).result(timeout=5) == 42
    # Shutdown resets it (fresh pool next use).
    ex.shutdown_maintenance_executor()
    assert ex.governance_executor() is not gov


# --- run_in_cron_pool: queue wait must not be charged to the job's timeout ----
#
# The defect these cover: loop.run_in_executor only SUBMITS. With the cron pool
# bounded at _MAX_CRON_WORKERS, a call whose workers are all busy waits in the
# pool's queue having run no code -- but asyncio.wait_for starts counting when
# awaited, so wrapping the whole submit+run in one wait_for spends the job's own
# budget on queue time and then reports the job as having timed out itself.


@pytest.fixture
def single_worker_cron_pool(monkeypatch):
    """Give the cron pool exactly ONE worker, so one blocker starves it.

    The pool is memoized process-wide, so it has to be torn down both before
    (to drop any pool built at the default width) and after (so later tests
    don't inherit a 1-worker pool).
    """
    ex.shutdown_maintenance_executor()
    monkeypatch.setattr(ex, "_MAX_CRON_WORKERS", 1)
    release = threading.Event()
    entered = threading.Event()

    def _occupy() -> None:
        entered.set()
        release.wait(timeout=30)

    ex.cron_executor().submit(_occupy)
    assert entered.wait(timeout=5), "blocker never claimed the only worker"
    try:
        yield release
    finally:
        release.set()
        ex.shutdown_maintenance_executor()


@pytest.mark.asyncio
async def test_queue_wait_is_not_charged_to_the_execution_timeout(single_worker_cron_pool):
    """A FAST call queued behind a busy worker must still run.

    Negative control for the defect: the execution timeout here (0.2s) is far
    shorter than the queue wait (~0.5s), so the pre-fix construct
    ``wait_for(run_in_executor(...), timeout=0.2)`` would kill this call and
    report it as the callable overrunning -- even though the callable is
    instant. Charging queue time to execution is the bug; this asserts it isn't.
    """
    release = single_worker_cron_pool
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, release.set)

    result = await ex.run_in_cron_pool(lambda: "ran", timeout=0.2, queue_timeout=10)

    assert result == "ran"


@pytest.mark.asyncio
async def test_starved_pool_reports_queued_never_ran_not_a_timeout(single_worker_cron_pool):
    """Exhausting the QUEUE budget is reported as its own thing.

    This is the misdiagnosis half: without a distinct error the next pool
    saturation reads as N separate jobs that each overran, which is what makes
    it cost days to spot.
    """
    with pytest.raises(ex.CronQueueTimeout) as excinfo:
        await ex.run_in_cron_pool(lambda: "ran", timeout=30, queue_timeout=0.3)

    assert "never ran" in str(excinfo.value)
    assert excinfo.value.waited >= 0.3


@pytest.mark.asyncio
async def test_waited_still_covers_the_budget_when_the_timer_fires_early(
    single_worker_cron_pool,
):
    """The reported wait must cover the budget even on a coarse-clock platform.

    ``BaseEventLoop._run_once`` dispatches a timer once
    ``handle._when < time() + _clock_resolution``, so a wake can arrive up to one
    clock resolution EARLY -- about 15.6ms on Windows, ~0 on Linux. A queue phase
    that trusted that wake reported a wait SHORTER than the budget it had just
    exhausted (observed on CI: 0.297 against a 0.3 bound), so the deadline has to
    be re-checked rather than believed.

    Both knobs below are patched to model that platform on any host, because the
    defect is unreachable on a fine-clock one.
    """
    loop = asyncio.get_running_loop()
    win_resolution = 0.015625
    selector = loop._selector
    original_select = selector.select
    loop._clock_resolution = win_resolution
    # Return inside the early-fire window, which is what makes _run_once
    # dispatch the timer before the deadline.
    selector.select = lambda t=None: original_select(
        None if t is None else max(0.0, t - win_resolution)
    )
    try:
        with pytest.raises(ex.CronQueueTimeout) as excinfo:
            await ex.run_in_cron_pool(lambda: "ran", timeout=30, queue_timeout=0.3)
    finally:
        selector.select = original_select

    # The invariant, held by construction rather than by tolerance.
    assert excinfo.value.waited >= 0.3
    # And the classification must still be the queue phase, not an overrun.
    assert "never ran" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_call_claimed_at_the_deadline_is_not_reported_as_never_ran():
    """A worker claiming the call right AT the budget must not read as a queue timeout.

    ``_mark_started`` is delivered by ``call_soon_threadsafe``, which only
    ENQUEUES, so it trails the worker's real claim: ``started`` can still be
    unresolved for a call that is already executing.  Reporting "never ran"
    there is not merely inaccurate -- the caller's overlap guard is released on
    that error, so the next fire starts a second run beside the one still
    executing, and a thread cannot be interrupted to prevent it.
    """
    ex.shutdown_maintenance_executor()
    loop = asyncio.get_running_loop()
    ran = threading.Event()

    def payload() -> str:
        ran.set()
        return "completed"

    # Model the loop's ready-queue lag deterministically, and ONLY for the claim
    # signal -- wrap_future uses call_soon_threadsafe for its own completion
    # plumbing, so deferring everything would just make this test slow.  The
    # real window is the gap between the worker enqueueing _mark_started and the
    # loop running it, which is widest exactly when the loop is saturated -- the
    # same condition under which the pool is saturated and budgets expire.
    real_call_soon_threadsafe = loop.call_soon_threadsafe

    def lagging(cb, *a):  # type: ignore[no-untyped-def]
        if getattr(cb, "__name__", "") == "_mark_started":
            return real_call_soon_threadsafe(loop.call_later, 0.5, cb, *a)
        return real_call_soon_threadsafe(cb, *a)

    loop.call_soon_threadsafe = lagging  # type: ignore[method-assign]
    try:
        result = await ex.run_in_cron_pool(payload, timeout=30, queue_timeout=0.3)
    finally:
        loop.call_soon_threadsafe = real_call_soon_threadsafe  # type: ignore[method-assign]
        ex.shutdown_maintenance_executor()

    assert ran.is_set(), "precondition: a worker must have claimed and run the call"
    assert result == "completed"


@pytest.mark.asyncio
async def test_execution_timeout_is_still_enforced_as_a_backstop():
    """With a free pool, an overrunning callable still trips *timeout*.

    The backstop must survive: without it a wedged worker leaves the job entry
    un-failed forever. It must also NOT be reported as a queue timeout.
    """
    ex.shutdown_maintenance_executor()
    release = threading.Event()
    try:
        with pytest.raises(asyncio.TimeoutError) as excinfo:
            await ex.run_in_cron_pool(lambda: release.wait(5), timeout=0.3, queue_timeout=10)
        assert not isinstance(excinfo.value, ex.CronQueueTimeout)
    finally:
        # Release the worker BEFORE shutting the pool down: shutdown uses
        # wait=False with cancel_futures=True, which drops queued futures but
        # cannot stop a callable a worker has already started.  Without this the
        # thread outlives the test by the remainder of its sleep and leaks into
        # whatever runs next.
        release.set()
        ex.shutdown_maintenance_executor()


@pytest.mark.asyncio
async def test_result_and_exception_pass_through():
    ex.shutdown_maintenance_executor()
    try:
        assert await ex.run_in_cron_pool(lambda: 6 * 7, timeout=5, queue_timeout=5) == 42

        def _boom() -> None:
            raise ValueError("from the worker")

        with pytest.raises(ValueError, match="from the worker"):
            await ex.run_in_cron_pool(_boom, timeout=5, queue_timeout=5)
    finally:
        ex.shutdown_maintenance_executor()


@pytest.mark.asyncio
async def test_positional_args_reach_the_callable():
    ex.shutdown_maintenance_executor()
    try:
        seen: list[tuple] = []
        assert (
            await ex.run_in_cron_pool(
                lambda *a: seen.append(a) or "ok", 1, "two", timeout=5, queue_timeout=5
            )
            == "ok"
        )
        assert seen == [(1, "two")]
    finally:
        ex.shutdown_maintenance_executor()


@pytest.mark.asyncio
async def test_cancelling_the_caller_does_not_leave_the_call_queued(single_worker_cron_pool):
    """A cancelled cron call must not execute later.

    The queue phase shields its started-signal future so it survives a wake and
    can be re-waited. That shield also swallows the cancel, so without an
    explicit handler a caller cancelling this coroutine escapes while the
    submitted call is still sitting in the pool queue -- and it then runs long
    after the caller gave up. A cancelled cron command executing anyway is the
    defect this pins.
    """
    release = single_worker_cron_pool  # the fixture holds the pool's only worker
    ran = threading.Event()

    task = asyncio.create_task(ex.run_in_cron_pool(lambda: ran.set(), timeout=30, queue_timeout=30))
    await asyncio.sleep(0.2)
    assert not ran.is_set(), "precondition: the call must still be queued"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    ex.cron_executor().shutdown(wait=True)  # drain, so a queued call would surface
    assert not ran.is_set(), "a cancelled call executed anyway"


@pytest.mark.asyncio
async def test_cancelling_the_caller_does_not_kill_a_job_already_running():
    """The other direction: cancellation must not abort work already in flight.

    Propagating the cancel to the pool call has to stay a no-op once a worker
    has claimed it -- otherwise closing the queued-call leak would start killing
    jobs mid-execution, which is just as silent and worse.
    """
    ex.shutdown_maintenance_executor()
    entered = threading.Event()
    finished = threading.Event()

    def slow() -> str:
        entered.set()
        threading.Event().wait(0.5)
        finished.set()
        return "done"

    try:
        task = asyncio.create_task(ex.run_in_cron_pool(slow, timeout=30, queue_timeout=30))
        for _ in range(300):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "precondition: the job must have started"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        ex.cron_executor().shutdown(wait=True)
        assert finished.is_set(), "cancellation killed a job that was already running"
    finally:
        ex.shutdown_maintenance_executor()


def test_cron_queue_timeout_is_a_timeout_error() -> None:
    """Fail-safe: a caller that hasn't been taught the queue phase still copes.

    CronQueueTimeout subclasses asyncio.TimeoutError so an un-updated call site
    degrades to its existing timeout handling rather than raising something
    nothing catches. Callers that DO distinguish must order this except first.
    """
    exc = ex.CronQueueTimeout(12.4)
    assert isinstance(exc, asyncio.TimeoutError)
    assert str(exc) == "queued 12s, never ran"
    assert exc.waited == 12.4


def test_a_sub_second_queue_wait_is_not_rendered_as_zero() -> None:
    """A floored message read as "did not wait at all" and lost the diagnostic.

    The whole point of the distinct error is telling an operator how long the job
    sat behind a busy worker, so a real 0.3s wait must not print as ``queued 0s``.
    """
    assert str(ex.CronQueueTimeout(0.297)) == "queued 0.30s, never ran"
    assert str(ex.CronQueueTimeout(0.004)) == "queued 0.00s, never ran"
    # Whole-second rendering is unchanged above the sub-second range.
    assert str(ex.CronQueueTimeout(300.0)) == "queued 300s, never ran"


def test_cron_queue_wait_budget_is_far_larger_than_a_typical_job_timeout() -> None:
    """The queue budget must NOT be the job's own timeout.

    Deriving it from job.timeout would still kill a fast job queued behind a
    wedged worker -- just with a better label. Waiting and then running is the
    correct outcome; this budget only bounds the pathological case.
    """
    assert ex._CRON_QUEUE_WAIT_SECS >= 600


def test_cron_gate_pool_is_isolated_from_the_externally_paced_governance_pool() -> None:
    """The fire-time gate must not queue behind remote inbound traffic.

    Both pools do governance work, so sharing one looks natural -- but they are
    paced by different things. The governance pool takes one submit per inbound
    message across five transports plus the dashboard GETs, so a remote burst
    puts an unbounded FIFO backlog ahead of anything else on it. A cron gate is
    awaited INSIDE the job's already-armed wake deadline, so a backlog it did not
    cause is charged to the job's own budget. Here a cron gate queues only behind
    another cron gate, which the schedule paces.
    """
    gate = ex.cron_gate_executor()
    assert gate is not ex.governance_executor()
    assert gate is not ex.cron_executor()
    assert gate is not ex.maintenance_executor()
    assert gate is not ex.subprocess_executor()
    # Memoized, named, bounded.
    assert ex.cron_gate_executor() is gate
    assert gate._max_workers == ex._MAX_CRON_GATE_WORKERS
    worker = gate.submit(threading.current_thread).result(timeout=5)
    assert worker.name.startswith("mc-crongate"), f"gate ran on {worker.name!r}"
    # Reset by the shared shutdown, or a later test inherits a saturated pool.
    ex.shutdown_maintenance_executor()
    assert ex.cron_gate_executor() is not gate


def test_the_gate_budget_is_capped_below_the_wake_budget() -> None:
    """The gate's bound must fire BEFORE the deadline it is awaited inside.

    This is the one place a queue bound is derived from the caller's own clock,
    and the inversion is deliberate: for the execution pool, waiting past the
    budget and then running is correct, but a gate verdict arriving after the
    wake deadline is worthless -- the deadline kills the run either way, and the
    caller can then no longer tell starvation from an overrun. That ambiguity is
    precisely the state in which a one-shot is consumed by a run that never
    dispatched, so the cap is what keeps the retention marker reachable.

    The 1s case is the one that mattered and an earlier revision of this test
    excused it as an accepted residual. It is not acceptable: a bare
    ``max(1.0, ...)`` floor returns 1.0 there, so the gate and the wake deadline
    expire together and the OUTER one wins -- exactly the ambiguity above, at the
    budget where a one-shot is most likely to be starved.
    """
    # Strictly below, for EVERY positive budget including the degenerate ones.
    for budget in (0.5, 1, 2, 3, 4, 8, 60, 1800, 86400):
        gate = ex.cron_gate_budget(budget)
        assert gate < budget, f"gate {gate}s could outlive a {budget}s deadline"
    # But not collapsed to a vanishing window either, which is the opposite error:
    # dropping the floor outright hands a 2s job a 0.5s gate and times it out on a
    # HEALTHY pool, turning a job that would have run into a never-started one.
    for budget in (2, 3, 4):
        assert ex.cron_gate_budget(budget) == ex._CRON_GATE_MIN_SECS
    # A sub-floor budget keeps a usable fraction rather than the floor.
    assert ex.cron_gate_budget(1) == pytest.approx(1 * ex._CRON_GATE_HEADROOM)
    # Degenerate input is defined, not an exception or a negative wait.
    assert ex.cron_gate_budget(0) == 0.0
    # And it never grows without limit: a long-running job still gets a prompt
    # verdict instead of a gate allowed to sit for hours.
    assert ex.cron_gate_budget(86400) == float(ex._CRON_GATE_WAIT_SECS)


@pytest.mark.asyncio
async def test_both_gate_bounds_reach_the_retention_handler() -> None:
    """Either gate bound expiring must be catchable as CronQueueTimeout.

    ``run_in_cron_pool`` raises ``CronQueueTimeout`` for its QUEUE phase but a
    plain ``asyncio.TimeoutError`` for its EXECUTION phase. The gate callers key
    the ``run_never_started`` retention marker off ``except CronQueueTimeout``, so
    a gate that got a worker and then overran its own bound bypassed that handler
    entirely and its one-shot was deleted as though the run had happened.

    Both bounds mean the same thing for a gate -- no verdict was reached -- so
    both must reach the handler. Fails pre-fix on the execution bound with a bare
    ``TimeoutError`` that is not a ``CronQueueTimeout``.
    """
    ex.shutdown_maintenance_executor()
    release = threading.Event()
    try:
        # EXECUTION bound: pool is free, so the call IS claimed and only its own
        # bound can fire.
        with pytest.raises(ex.CronQueueTimeout) as ei:
            await ex.run_in_cron_gate_pool(release.wait, 5.0, timeout=0.3)
        assert isinstance(ei.value, ex.CronGateTimeout)
        # Distinct wording: this call did get a worker, so "queued" would lie.
        assert "queued" not in str(ei.value), str(ei.value)

        # QUEUE bound: saturate so nothing is claimed. Still the base type.
        release.set()
        ex.shutdown_maintenance_executor()
        held = threading.Event()
        for _ in range(ex._MAX_CRON_GATE_WORKERS):
            ex.cron_gate_executor().submit(held.wait, 10.0)
        await asyncio.sleep(0.2)
        try:
            with pytest.raises(ex.CronQueueTimeout) as ei2:
                await ex.run_in_cron_gate_pool(lambda: None, timeout=0.3)
            assert not isinstance(ei2.value, ex.CronGateTimeout)
            assert "never ran" in str(ei2.value)
        finally:
            held.set()
    finally:
        release.set()
        ex.shutdown_maintenance_executor()


@pytest.mark.asyncio
async def test_a_timeout_from_the_gates_own_work_is_not_a_never_started_run() -> None:
    """The opposite error to the one above, and it is why the handler stays narrow.

    ``asyncio.TimeoutError`` IS ``builtins.TimeoutError`` on 3.11+, so a socket or
    subprocess timeout raised INSIDE a governance check is type-identical to the
    gate's own bound expiring. Widening the caller's handler to every
    ``asyncio.TimeoutError`` -- the obvious fix for the case above -- would mark
    that never-started and retain a one-shot whose gate DID reach its work.

    So the gate's own timeout is wrapped in the worker thread before the bound can
    see it, and stays a ``TimeoutError`` (callers that handle timeouts are
    unaffected) while NOT being a ``CronQueueTimeout``.
    """
    ex.shutdown_maintenance_executor()

    def _gate_that_times_out_internally() -> None:
        raise TimeoutError("governance store read timed out")

    try:
        with pytest.raises(asyncio.TimeoutError) as ei:
            await ex.run_in_cron_gate_pool(_gate_that_times_out_internally, timeout=5.0)
        assert isinstance(ei.value, ex.CronGateWorkTimeout)
        assert not isinstance(
            ei.value, ex.CronQueueTimeout
        ), "a failed dispatch DECISION was reported as a run that never started"
        assert "governance store read timed out" in str(ei.value)
    finally:
        ex.shutdown_maintenance_executor()


@pytest.mark.asyncio
async def test_the_gate_spends_its_single_budget_once_across_both_phases() -> None:
    """The gate's queue and execution bounds must SUM to within its own budget.

    ``run_in_cron_pool`` treats *timeout* and *queue_timeout* as two independent
    budgets -- it charges *timeout* to EXECUTION alone and takes *queue_timeout*
    as a separate bound -- so handing the same value to both let the gate run for
    twice the budget it was given. That budget is already
    ``cron_gate_budget(...)``, sized to land strictly BELOW the job's wake
    deadline, so spending it twice put the gate's total ABOVE that deadline and
    the deadline won the race. A 0.6s queue wait followed by a 0.4s run reached
    neither 0.75s phase bound, so nothing raised, the caller's ``except
    CronQueueTimeout`` never ran, and a ``delete_after_run`` one-shot was
    consumed by a run that never dispatched -- the exact regression this module's
    queue accounting exists to prevent.

    Observed at the enforcing call rather than recomputed here. Re-deriving the
    split from the same constants would measure the arithmetic and pass whatever
    the call site actually arms.
    """
    budget = 1.0
    seen: dict[str, float | None] = {}

    async def _spy(func, /, *args, timeout, queue_timeout=None, executor=None):
        seen["timeout"] = timeout
        seen["queue_timeout"] = queue_timeout
        return "verdict"

    with mock.patch.object(ex, "run_in_cron_pool", _spy):
        assert await ex.run_in_cron_gate_pool(lambda: "verdict", timeout=budget) == "verdict"

    assert seen["queue_timeout"] is not None, "the gate must bound its own queue phase"
    total = seen["queue_timeout"] + seen["timeout"]
    assert total <= budget, (
        f"gate arms {seen['queue_timeout']}s queue + {seen['timeout']}s execution "
        f"= {total}s total against a {budget}s budget it must stay inside"
    )


@pytest.mark.asyncio
async def test_the_gate_keeps_most_of_its_budget_for_a_claimed_call() -> None:
    """Splitting the budget must not starve EXECUTION -- the opposite failure.

    Bounding the total is only half the requirement. Hand execution "whatever the
    queue left over" and a gate a worker has ALREADY claimed -- one that would
    have answered in milliseconds -- gets a near-zero bound and raises
    ``CronGateTimeout``. That is a ``CronQueueTimeout`` subclass, so it reaches
    the retention handler and the one-shot is RETAINED and never fires.
    Over-deletion would become silent non-dispatch, which is why the execution
    share is a fixed floor rather than a remainder. The queue bound must stay
    positive too, or starvation stops being detectable before the total expires.
    """
    budget = 1.0
    seen: dict[str, float | None] = {}

    async def _spy(func, /, *args, timeout, queue_timeout=None, executor=None):
        seen["timeout"] = timeout
        seen["queue_timeout"] = queue_timeout
        return None

    with mock.patch.object(ex, "run_in_cron_pool", _spy):
        await ex.run_in_cron_gate_pool(lambda: None, timeout=budget)

    assert (
        seen["timeout"] >= budget / 2
    ), f"a claimed gate is left only {seen['timeout']}s of its {budget}s budget to answer in"
    assert seen["queue_timeout"] > 0, "starvation must still be detectable before the total"

    # End to end, with the real pool: a claimed gate that never answers has to
    # burn a real share of the budget before bailing, not be cut off at once.
    ex.shutdown_maintenance_executor()
    release = threading.Event()
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ex.CronQueueTimeout):
            await ex.run_in_cron_gate_pool(release.wait, 30.0, timeout=budget)
        elapsed = loop.time() - started
        assert elapsed >= budget / 2, f"claimed gate gave up after only {elapsed:.3f}s"
    finally:
        release.set()
        ex.shutdown_maintenance_executor()


# --- configure_default_executor: naming ----------------------------------------
#
# The default executor is the pool asyncio.to_thread and run_in_executor(None, ...)
# route onto.  The loop itself uses it for getaddrinfo (DNS).  These tests cover
# the thread naming (`mc-default-*`) -- sizing is left to Python's default.


@pytest.mark.asyncio
async def test_configure_default_executor_installs_named_pool() -> None:
    """configure_default_executor installs a pool with mc-default-* threads."""
    ex.configure_default_executor()

    loop = asyncio.get_running_loop()
    thread_name = await loop.run_in_executor(None, lambda: threading.current_thread().name)

    assert thread_name.startswith(
        "mc-default"
    ), f"Expected thread name starting with 'mc-default', got {thread_name!r}"


@pytest.mark.asyncio
async def test_configure_default_executor_creates_fresh_pool_per_call() -> None:
    """Each call to configure_default_executor creates a fresh pool for the loop.

    The per-loop design means the loop owns the pool and shuts it down on close,
    removing the need for process-global tracking and private-attribute probes.
    """
    ex.configure_default_executor()
    loop = asyncio.get_running_loop()
    first_executor = loop._default_executor

    ex.configure_default_executor()
    second_executor = loop._default_executor

    # Each call creates a fresh pool
    assert first_executor is not second_executor


def test_second_loop_gets_configured_executor() -> None:
    """A second event loop gets a configured mc-default executor, not anonymous.

    Uses explicit loop management (new_event_loop + run_until_complete + close)
    rather than asyncio.run to avoid a false positive from test_spawn_audit's
    subprocess-spawn detector, which matches asyncio.run as base=asyncio attr=run.

    This test pins the per-loop design: configure_default_executor creates a
    fresh pool for each loop, and each loop gets mc-default threads.
    """
    results: list[str] = []

    async def capture_thread_name() -> None:
        ex.configure_default_executor()
        loop = asyncio.get_running_loop()
        thread_name = await loop.run_in_executor(None, lambda: threading.current_thread().name)
        results.append(thread_name)

    # First event loop
    loop1 = asyncio.new_event_loop()
    try:
        loop1.run_until_complete(capture_thread_name())
    finally:
        loop1.close()

    # Second event loop -- the per-loop design gives it its own fresh pool
    loop2 = asyncio.new_event_loop()
    try:
        loop2.run_until_complete(capture_thread_name())
    finally:
        loop2.close()

    assert len(results) == 2
    assert results[0].startswith(
        "mc-default"
    ), f"First loop: expected 'mc-default*', got {results[0]!r}"
    assert results[1].startswith(
        "mc-default"
    ), f"Second loop: expected 'mc-default*', got {results[1]!r}"
