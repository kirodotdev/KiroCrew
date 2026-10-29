"""What a runner hands back, and what a record says of an interrupted or unreadable run.

``register`` refuses coroutine and generator runners, a partial or an async ``__call__``
included, and still accepts an ordinary callable. A runner that hands back an undriven
coroutine, generator or future fails its run rather than reaching ``done``, a settled future
counts by its outcome, and work handed back as a pending pool future is stopped, with a
record that claims the stop only when it happened. ``reconcile`` records an interruption's
prior status and its cause as two fields, and ``read`` answers ``None`` for a malformed
record while a permission or I/O error escapes. The lifecycle and the process-wide registry
live in ``test_job_sdk.py``, the async twins and the defensive invariants in
``test_job_sdk_invariants.py``, the ``start`` params channel in ``test_job_sdk_params.py``,
and the ``sdk`` fixture and the bounded waits in ``job_sdk_helpers.py``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import os
import threading
import uuid
from pathlib import Path

import pytest
from job_sdk_helpers import (  # noqa: F401 -- the fixture is requested by name
    _wait_terminal,
    sdk_fixture,
)

from kiro_crew.apps import job_sdk
from kiro_crew.apps.job_sdk import (
    DONE,
    FAILED,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    STARTING,
    JobHandle,
    JobRun,
    JobSDK,
    UnknownJobKind,
)
from kiro_crew.testing.wait import wait_until

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Whether a 0o000 mode actually denies THIS process. Root reads straight through
#: it, so the tests that provoke a real ``PermissionError`` from the filesystem
#: have nothing to provoke and must skip rather than assert a false negative.
_MODE_BITS_BITE = hasattr(os, "geteuid") and os.geteuid() != 0


class TestCoroutineRunnerIsRefusedAtRegistration:
    """An ``async def`` runner would report DONE having executed nothing.

    ``_execute`` calls ``fn(handle)`` on a worker thread and discards the return
    value by design. Calling a coroutine function returns a coroutine object
    instead of doing the work, so the body never runs, the object is dropped
    un-awaited, and the terminal write says ``done``. Refused at ``register``,
    where the answer is still cheap.
    """

    def test_a_coroutine_function_is_refused(self, sdk: JobSDK) -> None:
        async def runner(handle: JobHandle) -> None:  # pragma: no cover - never called
            raise AssertionError("the body of a refused runner must never execute")

        with pytest.raises(ValueError, match="coroutine function"):
            sdk.register("async-work", runner)

    def test_the_refused_kind_is_not_registered(self, sdk: JobSDK) -> None:
        """The refusal must leave NO runner behind.

        Validating after the table write would register the kind and then raise,
        leaving a kind whose every ``start`` records a done run that did nothing --
        the exact outcome the refusal exists to prevent.
        """

        async def runner(handle: JobHandle) -> None:  # pragma: no cover - never called
            raise AssertionError("unreachable")

        with pytest.raises(ValueError):
            sdk.register("async-work", runner)
        assert sdk.kinds() == []
        assert sdk.is_cancellable("async-work") is False
        # And the kind is genuinely absent, not merely missing from the listing.
        with pytest.raises(UnknownJobKind):
            sdk.start("async-work")

    def test_an_async_partial_and_an_async_lambda_alias_are_refused(self, sdk: JobSDK) -> None:
        """``iscoroutinefunction`` unwraps ``functools.partial``, so this holds."""
        import functools

        async def runner(handle: JobHandle) -> None:  # pragma: no cover - never called
            raise AssertionError("unreachable")

        with pytest.raises(ValueError, match="coroutine function"):
            sdk.register("partial-async", functools.partial(runner))
        aliased = runner
        with pytest.raises(ValueError, match="coroutine function"):
            sdk.register("aliased-async", aliased)
        assert sdk.kinds() == []

    def test_a_callable_object_with_an_async_call_is_refused(self, sdk: JobSDK) -> None:
        """The shape a check written against bare functions passes through.

        ``inspect`` reports the INSTANCE as an ordinary object, so
        ``iscoroutinefunction(instance)`` is False while invoking it still returns
        a coroutine, which is why the check goes through ``__call__`` too.
        """

        class AsyncRunner:
            async def __call__(self, handle: JobHandle) -> None:  # pragma: no cover
                raise AssertionError("the body of a refused runner must never execute")

        instance = AsyncRunner()
        # The premise: the instance alone does not look like a coroutine function.
        assert inspect.iscoroutinefunction(instance) is False
        assert inspect.iscoroutinefunction(instance.__call__) is True
        with pytest.raises(ValueError, match="coroutine function"):
            sdk.register("async-call", instance)
        assert sdk.kinds() == []

    def test_generator_shapes_are_refused_too(self, sdk: JobSDK) -> None:
        """Same defect, two more spellings.

        Calling a generator or async-generator function returns a lazy object and
        runs nothing, so the record would say ``done`` for a body that never
        executed -- identical to the coroutine case, and identically silent.
        """

        def sync_gen(handle: JobHandle):  # pragma: no cover - never invoked
            yield 1

        async def async_gen(handle: JobHandle):  # pragma: no cover - never invoked
            yield 1

        with pytest.raises(ValueError, match="generator function"):
            sdk.register("sync-gen", sync_gen)
        with pytest.raises(ValueError, match="async generator function"):
            sdk.register("async-gen", async_gen)
        assert sdk.kinds() == []

    def test_ordinary_callables_are_still_accepted(self, sdk: JobSDK) -> None:
        """The refusal must not narrow what a real app can register."""
        import functools

        def plain(handle: JobHandle) -> None:
            return None

        class Callable_:
            def __call__(self, handle: JobHandle) -> None:
                return None

        sdk.register("plain", plain)
        sdk.register("lambda", lambda h: None)
        sdk.register("partial", functools.partial(plain))
        sdk.register("object", Callable_())
        assert sdk.kinds() == ["lambda", "object", "partial", "plain"]

    def test_a_sync_runner_that_drives_its_own_loop_still_works(self, sdk: JobSDK) -> None:
        """The refusal names ``asyncio.run`` as the supported path, so prove it."""
        seen: list[str] = []

        async def body() -> None:
            seen.append("ran")

        sdk.register("wrapped", lambda h: asyncio.run(body()))
        run_id = sdk.start("wrapped")
        assert _wait_terminal(sdk, run_id).status == DONE
        assert seen == ["ran"]


# ---------------------------------------------------------------------------
# An interruption records TWO facts, and the message is derived from them
# ---------------------------------------------------------------------------


class TestInterruptionRecordsBothAxes:
    """``reconcile`` must not overwrite ``status`` and pick ``error`` from the
    runner table alone: that writes a run whose worker thread never started a
    record claiming it "was running", and a consumer then cannot tell a run that
    may have committed side effects from one that provably has not.

    The two facts are independent because they describe different times: how far
    the run got (past) and whether the kind can be serviced now (present).
    """

    @staticmethod
    def _seed(sdk: JobSDK, status: str, kind: str) -> str:
        run_id = uuid.uuid4().hex
        sdk.store.write(
            JobRun(
                run_id=run_id,
                app=sdk.app_name,
                kind=kind,
                status=status,
                origin="a-process-that-is-gone",
            )
        )
        return run_id

    @pytest.mark.parametrize("status", [QUEUED, STARTING, RUNNING])
    def test_the_prior_status_is_preserved(self, sdk: JobSDK, status: str) -> None:
        sdk.register("work", lambda h: None)
        run_id = self._seed(sdk, status, "work")
        assert sdk.reconcile() == 1
        run = sdk.get(run_id)
        assert run is not None
        assert run.status == INTERRUPTED
        assert run.interrupted_from == status

    def test_the_cause_distinguishes_a_lost_process_from_a_missing_runner(
        self, sdk: JobSDK
    ) -> None:
        sdk.register("known", lambda h: None)
        known = self._seed(sdk, RUNNING, "known")
        gone = self._seed(sdk, RUNNING, "vanished")
        assert sdk.reconcile() == 2
        assert sdk.get(known).interrupt_cause == job_sdk.CAUSE_PROCESS_GONE
        assert sdk.get(gone).interrupt_cause == job_sdk.CAUSE_RUNNER_UNREGISTERED

    def test_the_two_axes_are_independent(self, sdk: JobSDK) -> None:
        """Every (prior status, cause) combination is separately observable.

        This is the assertion the old single-string form could not satisfy: it
        produced ONE record shape for all three prior statuses, so a consumer had
        no way back to what actually happened.
        """
        sdk.register("known", lambda h: None)
        for status in (QUEUED, STARTING, RUNNING):
            for kind in ("known", "vanished"):
                self._seed(sdk, status, kind)
        assert sdk.reconcile() == 6
        pairs = {(r.interrupted_from, r.interrupt_cause) for r in sdk.store.iter_runs()}
        assert pairs == {
            (status, cause)
            for status in (QUEUED, STARTING, RUNNING)
            for cause in (job_sdk.CAUSE_PROCESS_GONE, job_sdk.CAUSE_RUNNER_UNREGISTERED)
        }

    def test_a_run_that_never_started_is_not_described_as_running(self, sdk: JobSDK) -> None:
        """The one behavioural claim about the message, as opposed to its wording.

        A ``queued`` or ``starting`` record provably ran no line of the runner's
        body, so a message asserting it was running is false -- which is what the
        old form wrote for every prior status.
        """
        sdk.register("work", lambda h: None)
        never = [self._seed(sdk, status, "work") for status in (QUEUED, STARTING)]
        did = self._seed(sdk, RUNNING, "work")
        assert sdk.reconcile() == 3
        for run_id in never:
            assert "was running" not in sdk.get(run_id).error
        assert "was running" in sdk.get(did).error

    def test_the_message_still_names_the_restart(self, sdk: JobSDK) -> None:
        """Every consumer of a reconciled record reads ``error`` for the cause,
        so the message must remain recognisable for all six combinations."""
        sdk.register("known", lambda h: None)
        for status in (QUEUED, STARTING, RUNNING):
            for kind in ("known", "vanished"):
                self._seed(sdk, status, kind)
        assert sdk.reconcile() == 6
        for run in sdk.store.iter_runs():
            assert "restart" in run.error.lower()

    def test_an_unregistered_kind_is_still_named_in_the_message(self, sdk: JobSDK) -> None:
        run_id = self._seed(sdk, RUNNING, "vanished")
        assert sdk.reconcile() == 1
        assert "vanished" in sdk.get(run_id).error

    def test_both_fields_survive_a_round_trip_through_disk(self, sdk: JobSDK) -> None:
        """The fields are only useful if a LATER process can read them back --
        which is the whole point, since the writer is a process that has died."""
        sdk.register("work", lambda h: None)
        run_id = self._seed(sdk, STARTING, "work")
        assert sdk.reconcile() == 1
        raw = json.loads((sdk.store.dir / f"{run_id}.json").read_text(encoding="utf-8"))
        assert raw["interrupted_from"] == STARTING
        assert raw["interrupt_cause"] == job_sdk.CAUSE_PROCESS_GONE
        assert JobRun.from_dict(raw).interrupted_from == STARTING
        assert JobRun.from_dict(raw).interrupt_cause == job_sdk.CAUSE_PROCESS_GONE

    def test_a_run_that_was_never_interrupted_carries_neither_field(self, sdk: JobSDK) -> None:
        """The fields are set ONLY by reconcile, so a normal run must leave them
        empty -- a consumer switching on them must not see a stale value."""
        sdk.register("work", lambda h: None)
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert run.interrupted_from == ""
        assert run.interrupt_cause == ""

    def test_composition_tolerates_a_status_or_cause_it_does_not_know(self) -> None:
        """The pass that clears stuck ``running`` records must not be stoppable by
        a record written by a newer build, so neither lookup may raise."""
        assert job_sdk._interrupt_error(job_sdk.CAUSE_PROCESS_GONE, "from-the-future", "k")
        assert job_sdk._interrupt_error("cause-from-the-future", RUNNING, "k")
        assert "was running" in job_sdk._interrupt_error("cause-from-the-future", RUNNING, "k")

    def test_the_message_is_composed_at_exactly_one_site(self) -> None:
        """A ratchet, not a behaviour check: the value of the table is that adding
        a cause edits one place. A second composition site would let the two
        drift, which is the failure the lookup table replaced -- and a behaviour
        test cannot see a second site that happens to agree today.
        """
        source = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted(Path(job_sdk.__file__).parent.glob("job_sdk*.py"))
        )
        # One definition, one call. `reconcile` is the only caller.
        assert source.count("_interrupt_error(") == 2
        # The templates exist only in the table: no other line builds a sentence.
        assert source.count("the gateway restarted while this") == 2
        # And the table is read only inside the composition helper.
        assert source.count("_INTERRUPT_MESSAGE") == 3  # the def, plus .get + fallback


# ---------------------------------------------------------------------------
# An unreadable record is absent, not a crash
# ---------------------------------------------------------------------------


class TestAMalformedRecordIsAbsentAnUnreadableOneIsNot:
    """``read`` answers ``None`` for a record whose bytes it cannot make sense of,
    and lets an environmental fault out.

    The distinction is what ``None`` claims. A missing file, a bad path, or bytes
    that are not the UTF-8 JSON this store writes establish that there is no such
    record. A ``PermissionError`` establishes nothing of the kind: on Windows it is
    the transient sharing violation a concurrent ``os.replace`` produces, which
    ``read_bytes_with_retry`` is built to outlast, and the record is present and
    intact throughout. Answering "absent" there is the same unverified assertion
    this change set exists to remove, so those escape to the caller.
    """

    @staticmethod
    def _unreadable(sdk: JobSDK) -> tuple[str, Path]:
        run_id = uuid.uuid4().hex
        sdk.store.write(JobRun(run_id=run_id, app=sdk.app_name, kind="k", status=DONE))
        path = sdk.store.dir / f"{run_id}.json"
        return run_id, path

    @pytest.mark.skipif(
        not _MODE_BITS_BITE,
        reason="root reads through a 0o000 mode, so the PermissionError cannot be provoked",
    )
    def test_a_mode_that_blocks_the_read_is_not_reported_as_absent(self, sdk: JobSDK) -> None:
        """The record is intact the whole time -- only the mode changed."""
        run_id, path = self._unreadable(sdk)
        os.chmod(path, 0o000)
        try:
            with pytest.raises(PermissionError):
                sdk.get(run_id)
        finally:
            os.chmod(path, 0o600)
        # Restoring the mode restores the record, which is the proof that "absent"
        # would have been a lie: nothing about the record ever changed.
        assert sdk.get(run_id).status == DONE

    def test_a_permission_error_escapes_rather_than_reading_as_absent(self, sdk: JobSDK) -> None:
        """An on-loop read must let a PermissionError out, not swallow it."""
        run_id, _ = self._unreadable(sdk)
        raised: list[str] = []

        def boom(path):
            raised.append(str(path))
            raise PermissionError(13, "Permission denied")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(job_sdk, "read_bytes_with_retry", boom)
            with pytest.raises(PermissionError):
                sdk.get(run_id)
        assert raised, "the patched reader was never reached"

    def test_an_environmental_oserror_escapes_too(self, sdk: JobSDK) -> None:
        """``EIO`` is a fact about the host, not about whether this run exists."""
        run_id, _ = self._unreadable(sdk)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                job_sdk,
                "read_bytes_with_retry",
                lambda path: (_ for _ in ()).throw(OSError(5, "I/O error")),
            )
            with pytest.raises(OSError):
                sdk.get(run_id)

    def test_bytes_that_are_not_utf_8_read_as_absent(self, sdk: JobSDK) -> None:
        """The other half: damage the decode cannot survive IS absence."""
        run_id, path = self._unreadable(sdk)
        path.write_bytes(b"\xff\xfe not utf-8")
        assert sdk.get(run_id) is None

    def test_the_three_absence_cases_answer_none(self, sdk: JobSDK) -> None:
        """The cases where ``None`` is the honest answer, kept while the tuple narrowed."""
        assert sdk.get(uuid.uuid4().hex) is None  # FileNotFoundError
        assert sdk.get("not-a-run-id") is None  # ValueError from _path
        for exc in (FileNotFoundError(), NotADirectoryError()):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    job_sdk,
                    "read_bytes_with_retry",
                    lambda path, e=exc: (_ for _ in ()).throw(e),
                )
                assert sdk.get(uuid.uuid4().hex) is None

    def test_a_damaged_record_does_not_stop_reconciliation(self, sdk: JobSDK) -> None:
        """Why the scan keeps its own broader guard: a record ``get`` now raises on
        is one the reconcile pass must still walk past."""
        if not _MODE_BITS_BITE:
            pytest.skip("root reads through a 0o000 mode")
        sdk.register("work", lambda h: None)
        stale = uuid.uuid4().hex
        sdk.store.write(
            JobRun(
                run_id=stale,
                app=sdk.app_name,
                kind="work",
                status=RUNNING,
                origin="a-process-that-is-gone",
            )
        )
        _, path = self._unreadable(sdk)
        os.chmod(path, 0o000)
        try:
            assert sdk.reconcile() == 1
            assert sdk.get(stale).status == INTERRUPTED
        finally:
            os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
class TestAnUndrivenResultIsFailedNotDone:
    """The safety property, as opposed to the registration guard's friendly error.

    ``register`` refuses callable SHAPES that cannot do work when called, but a
    shape check is a proxy and cannot see every route to the same outcome. The
    fact -- "did this runner do the work" -- is only available from what the call
    handed back, which is where this checks it.
    """

    def test_a_sync_runner_returning_a_coroutine_is_failed(self, sdk: JobSDK) -> None:
        """The form NO registration check can see.

        `def run(h): return _do_it(h)` over an `async def _do_it` is an ordinary
        refactor. The wrapper is a plain function, so every shape predicate says
        it is fine; it runs far enough to construct the coroutine, hands it back,
        and the work never happens.
        """
        ran: list[str] = []

        async def _do_it(handle: JobHandle) -> None:  # pragma: no cover - never awaited
            ran.append("body")

        def wrapper(handle: JobHandle):
            return _do_it(handle)

        # The premise: this defeats the registration guard entirely.
        assert job_sdk._lazy_call_shape(wrapper) == ""
        sdk.register("wrapped", wrapper)
        run = _wait_terminal(sdk, sdk.start("wrapped"))
        assert run.status == FAILED
        # Pinned on the FACT the record must convey, not on a particular word. An
        # earlier form asserted "await" appeared in the message, which broke when
        # the reason was reworded to name the protocol rather than the type -- the
        # same literal-string brittleness this change set keeps running into.
        assert "coroutine" in run.error
        assert "must not return" in run.error
        # It must NOT claim the body never ran: that is only knowable for THIS
        # shape, and the rule is stated for all suspendables uniformly.
        assert "never ran" not in run.error
        assert run.kind in run.error
        assert ran == [], "the coroutine body must not have run"

    def test_a_runner_returning_an_async_generator_is_failed(self, sdk: JobSDK) -> None:
        async def _gen(handle: JobHandle):  # pragma: no cover - never driven
            yield 1

        def wrapper(handle: JobHandle):
            return _gen(handle)

        sdk.register("agen", wrapper)
        run = _wait_terminal(sdk, sdk.start("agen"))
        assert run.status == FAILED
        assert "async generator" in run.error

    def test_a_runner_returning_a_future_is_failed(self, sdk: JobSDK) -> None:
        """A PENDING future: `isawaitable` is the predicate, not `iscoroutine`.

        Pending is the half that means the work did not happen -- something else
        was going to complete this future and nothing on this thread ever will.
        """

        def wrapper(handle: JobHandle):
            loop = asyncio.new_event_loop()
            try:
                return loop.create_future()
            finally:
                loop.close()

        sdk.register("fut", wrapper)
        run = _wait_terminal(sdk, sdk.start("fut"))
        assert run.status == FAILED

    def test_a_runner_returning_a_completed_future_is_done(self, sdk: JobSDK) -> None:
        """The mirror, and the same rule as a returned plain generator.

        A DONE future is evidence the work happened; the object is then just a
        discarded return value like any other. Flagging it would record `failed`
        for a run that did everything asked of it -- the same unverified assertion
        this change set exists to remove, pointed the other way.
        """
        did_work: list[str] = []

        def wrapper(handle: JobHandle):
            did_work.append(handle.run_id)
            loop = asyncio.new_event_loop()
            try:
                fut = loop.create_future()
                fut.set_result({"ok": True})
                return fut
            finally:
                loop.close()

        sdk.register("donefut", wrapper)
        run = _wait_terminal(sdk, sdk.start("donefut"))
        assert run.status == DONE, run.error
        assert did_work, "the runner body never ran"

    def test_a_thread_pool_future_was_never_in_scope(self, sdk: JobSDK) -> None:
        """`concurrent.futures.Future` is not awaitable, so it never reached the
        check at all -- worth pinning, because the reported form named a sync
        runner returning one and that shape was never affected."""
        import concurrent.futures

        cf: concurrent.futures.Future = concurrent.futures.Future()
        cf.set_result(1)
        assert not inspect.isawaitable(cf)
        assert job_sdk._undriven_result(cf) == ""

    def test_an_ordinary_runner_is_still_done(self, sdk: JobSDK) -> None:
        """The check must not fail a runner that did its work and returned data."""
        sdk.register("plain", lambda h: {"rows": 3})
        assert _wait_terminal(sdk, sdk.start("plain")).status == DONE

    def test_a_returned_suspended_generator_is_failed(self, sdk: JobSDK) -> None:
        """Began and did not finish, which is not the same as finished.

        An earlier revision allowed this on the grounds that the runner's body had
        run. It had run PARTWAY: nothing in the SDK drives what a runner returns, so
        a suspended frame never resumes and the work is incomplete. Recording `done`
        would assert a completion nobody observed.
        """
        reached: list[str] = []

        def wrapper(handle: JobHandle):
            def steps():
                reached.append("first")
                yield 1
                reached.append("second")  # pragma: no cover - never resumed
                yield 2

            gen = steps()
            next(gen)  # begins the body, stops at the first yield
            return gen

        sdk.register("gen-suspended", wrapper)
        run = _wait_terminal(sdk, sdk.start("gen-suspended"))
        assert run.status == FAILED
        assert "must not return" in run.error
        # And the proof it really was incomplete: the rest never ran.
        assert reached == ["first"]

    def test_a_returned_never_started_generator_is_failed(self, sdk: JobSDK) -> None:
        """Failed, and with the shared verdict rather than one of its own.

        A never-started generator is a distinct fact, so a distinct message
        ("never began") would be defensible. The state is not consulted, so this
        case carries the one suspendable verdict instead -- the cost of the
        tightening, recorded here rather than left for a reader to infer.
        """

        def wrapper(handle: JobHandle):
            return (n for n in range(3))  # never stepped

        sdk.register("gen-unstarted", wrapper)
        run = _wait_terminal(sdk, sdk.start("gen-unstarted"))
        assert run.status == FAILED
        assert "must not return" in run.error

    def test_a_returned_drained_generator_is_failed_and_this_reverses_an_earlier_call(
        self, sdk: JobSDK
    ) -> None:
        """THE INVERSION. An earlier revision of this same change set asserted DONE here.

        The reversal is deliberate and this test is its record. `CLOSED` was read as
        "ran to completion", which is true of this generator -- the body really did run.
        But `CLOSED` equally covers a suspendable closed before it ever began, and the
        two are indistinguishable from the object, so allowing the state meant recording
        a completion nobody observed for the OTHER case. A rule that cannot tell the
        cases apart must not claim to.

        So the work here genuinely happened and the run is still FAILED. That is the
        cost of the tightening, stated rather than hidden: the SDK refuses the object
        because it cannot interpret it, and the reason must not pretend the body never
        ran, because here it did.
        """
        drained = []

        def wrapper(handle: JobHandle):
            gen = (drained.append(n) or n for n in range(3))
            list(gen)  # drained: frame is now None, body really ran
            return gen

        sdk.register("gen-drained", wrapper)
        run = _wait_terminal(sdk, sdk.start("gen-drained"))
        assert run.status == FAILED
        assert drained == [0, 1, 2], "premise: the body DID run"
        assert "must not return" in run.error
        assert "never ran" not in run.error, "must not claim what is false here"

    def test_every_suspendable_state_reaches_the_same_verdict(self) -> None:
        """Completeness by state-INDEPENDENCE, which is a stronger claim than a map.

        Enumerating the language's frame states and asserting a verdict for each
        breaks the moment a future Python adds a state. The rule reads no state at
        all, so the property to pin is that no state -- the two CLOSED histories
        included -- can reach an allow.
        """

        def gen_fn():
            yield 1
            yield 2

        created = gen_fn()
        suspended = gen_fn()
        next(suspended)
        drained = gen_fn()
        list(drained)
        closed_unstarted = gen_fn()
        closed_unstarted.close()

        cases = {
            inspect.GEN_CREATED: created,
            inspect.GEN_SUSPENDED: suspended,
            "GEN_CLOSED (drained)": drained,
            "GEN_CLOSED (closed unstarted)": closed_unstarted,
        }
        verdicts = set()
        for label, obj in cases.items():
            reason = job_sdk._undriven_result(obj)
            assert reason != "", f"{label} reached an allow"
            assert "never ran" not in reason, f"{label} claims execution it cannot know"
            verdicts.add(reason)

        # The two CLOSED histories are the whole reason for the rule: they differ in
        # what happened and are identical to the classifier, so they MUST NOT be
        # distinguished by the record.
        assert job_sdk._undriven_result(drained) == job_sdk._undriven_result(closed_unstarted)
        assert len(verdicts) == 1, f"state still leaks into the verdict: {verdicts}"
        created.close()
        suspended.close()

    def test_an_unsettled_future_discloses_that_the_work_may_still_run(self) -> None:
        """The record must warn the owner about work the SDK could NOT stop.

        An unsettled future can stand for work already executing in a pool this SDK
        does not own. Marking the run terminal releases the dedupe key, so an owner
        who retries can start a second run overlapping work that never stopped.
        `failed` is the true verdict about the RUN -- the runner returned before
        finishing, violating its contract -- but the reason has to name what the SDK
        cannot control, since the owner reading it is the only party who can judge
        whether retrying is safe.

        The FIXTURE is the point. A bare `concurrent.futures.Future()` is PENDING and
        therefore stoppable, so asserting "may still be running" against one claims
        it about work that provably has not started, and passes only because the SDK
        never looks. The un-stoppable case is work that is genuinely RUNNING, which
        is what this builds.
        """
        entered = threading.Event()
        release = threading.Event()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def slow() -> str:
            entered.set()
            release.wait(5)
            return "done"

        try:
            pool_future = pool.submit(slow)
            assert entered.wait(5), "premise: the work must be executing, not queued"
            verdict = job_sdk._undriven_result(pool_future)
            assert "not settled" in verdict
            # The disclosure, pinned on the FACTS it must convey rather than wording.
            assert "may still be running" in verdict
            assert "cannot stop" in verdict
            assert "overlap" in verdict
            assert "cannot overlap" not in verdict, "must not claim a stop it did not make"
        finally:
            release.set()
            # flake-ok: pre-existing pool join after the release; every task it waits on is bounded by its own 5 s wait
            pool.shutdown(wait=True)

    def test_the_states_are_reachable_and_deliberately_not_distinguished(self) -> None:
        """The states are real; the verdict ignores them ON PURPOSE.

        Asserting three DISTINCT verdicts, with an allow for closed, would be the
        wrong shape: a verdict that varies by state implies the state is
        informative, and for CLOSED it is not.
        """

        created = (x for x in [1, 2])
        suspended = (x for x in [1, 2])
        next(suspended)
        closed = (x for x in [1, 2])
        list(closed)

        # The states really are reachable and really do differ...
        assert inspect.getgeneratorstate(created) == inspect.GEN_CREATED
        assert inspect.getgeneratorstate(suspended) == inspect.GEN_SUSPENDED
        assert inspect.getgeneratorstate(closed) == inspect.GEN_CLOSED
        assert closed.gi_frame is None, "closed is the state where the frame is gone"

        # ...and none of that reaches the record.
        verdicts = {job_sdk._undriven_result(g) for g in (created, suspended, closed)}
        assert len(verdicts) == 1, f"state leaked into the verdict: {verdicts}"
        assert job_sdk._undriven_result(closed) != "", "closed was allowed until round 9"

    def test_a_failed_running_transition_does_not_run_the_body(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The transition is a precondition, not a notification.

        If the store cannot record that the run is running, the body must not run:
        a record left at `starting` while the runner commits side effects is one
        `reconcile` later reads as "never began", and that claim cannot be
        corrected after the fact because the knowledge dies with the process.
        """
        ran: list[str] = []
        real_write = sdk.store.write

        def refuse_running(run):
            if run.status == RUNNING:
                raise OSError("the store is unavailable")
            return real_write(run)

        sdk.register("work", lambda h, **kw: ran.append(h.run_id))
        monkeypatch.setattr(sdk.store, "write", refuse_running)
        run = _wait_terminal(sdk, sdk.start("work"))
        assert run.status == FAILED
        assert not ran, "the body ran even though the running transition was lost"
        assert "never started" in run.error

    def test_a_failed_running_transition_never_reports_never_began(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: no surviving record can claim a run did not begin while
        its side effects exist. Since the body is refused, `starting` is true."""
        real_write = sdk.store.write

        def refuse_running(run):
            if run.status == RUNNING:
                raise OSError("the store is unavailable")
            return real_write(run)

        sdk.register("work", lambda h, **kw: {"ok": True})
        monkeypatch.setattr(sdk.store, "write", refuse_running)
        run = _wait_terminal(sdk, sdk.start("work"))
        # It is recorded as failed, not left for reconcile to describe.
        assert run.status == FAILED
        assert run.interrupted_from == ""
        assert run.interrupt_cause == ""

    def test_every_result_shape_in_the_table_has_a_verdict(self) -> None:
        """A TABLE of factory -> expected verdict, asserted by protocol.

        A table rather than a case per type, because appending a branch per reported
        shape is the failure mode this design replaced -- and because the protocol
        checks mean most new shapes need no row at all. Each row names which QUESTION
        the object answers, so the verdict follows from the answer rather than from
        the type.
        """
        loop = asyncio.new_event_loop()

        def a_coroutine():
            async def _c():  # pragma: no cover - never driven, by design
                return 1

            return _c()

        def an_async_gen():
            async def _a():  # pragma: no cover - never driven, by design
                yield 1

            return _a()

        def unstarted_gen():
            return (x for x in [1, 2])

        def started_gen():
            g = (x for x in [1, 2])
            next(g)
            return g

        def drained_gen():
            g = (x for x in [1, 2])
            list(g)
            return g

        def unsettled_future():
            return loop.create_future()

        def cancelled_future():
            f = loop.create_future()
            f.cancel()
            return f

        def failed_future():
            f = loop.create_future()
            f.set_exception(ValueError("inner op"))
            return f

        def settled_future():
            f = loop.create_future()
            f.set_result({"ok": True})
            return f

        def unsettled_pool_future():
            return concurrent.futures.Future()

        def cancelled_pool_future():
            f: concurrent.futures.Future = concurrent.futures.Future()
            f.cancel()
            return f

        def failed_pool_future():
            f: concurrent.futures.Future = concurrent.futures.Future()
            f.set_exception(ValueError("inner op"))
            return f

        def settled_pool_future():
            f: concurrent.futures.Future = concurrent.futures.Future()
            f.set_result(1)
            return f

        def bare_awaitable():
            class BareAwaitable:
                def __await__(self):  # pragma: no cover - never driven
                    yield

            return BareAwaitable()

        # (question answered, factory, expected fragment) -- "" means a completed unit
        table: list[tuple[str, object, str]] = [
            # Q1: has its body begun?
            ("suspendable: never began (coroutine)", a_coroutine, "must not return"),
            ("suspendable: never began (generator)", unstarted_gen, "must not return"),
            ("suspendable: suspended partway", started_gen, "must not return"),
            # A drained generator is refused too; see the state-independence test.
            ("suspendable: closed, drained", drained_gen, "must not return"),
            # Q2: is it settled? -- asyncio's flavour...
            ("Q2 unsettled", unsettled_future, "not settled"),
            ("Q2 cancelled", cancelled_future, "cancelled future"),
            ("Q2 settled badly", failed_future, "ran and raised"),
            ("Q2 settled well", settled_future, ""),
            # ...and the thread-pool flavour, which is NOT awaitable. These four are
            # the round the type table missed entirely, and they needed no new row:
            # they answer Q2, so the same code path classifies them.
            ("Q2 unsettled (thread pool)", unsettled_pool_future, "not settled"),
            ("Q2 cancelled (thread pool)", cancelled_pool_future, "cancelled future"),
            ("Q2 settled badly (thread pool)", failed_pool_future, "ran and raised"),
            ("Q2 settled well (thread pool)", settled_pool_future, ""),
            # Answers neither question.
            ("neither, no loop here to drive it", an_async_gen, "must not return"),
            ("neither, but still needs driving", bare_awaitable, "must not return"),
        ]
        try:
            for label, factory, expected in table:
                result = factory()  # type: ignore[operator]
                verdict = job_sdk._undriven_result(result)
                if expected == "":
                    assert verdict == "", f"{label}: expected a completed unit, got {verdict!r}"
                else:
                    assert expected in verdict, f"{label}: {verdict!r} does not say {expected!r}"

            # The three settled outcomes must be told APART, not merely all failed.
            reasons = {
                job_sdk._undriven_result(f())
                for f in (unsettled_future, cancelled_future, failed_future)
            }
            assert len(reasons) == 3, f"the settled outcomes were flattened: {reasons}"
        finally:
            loop.close()

    def test_the_two_protocol_questions_cover_the_space(self) -> None:
        """The claim the design rests on, pinned: any object needing to be driven
        answers at least one of the two questions.

        This is what makes the classifier bounded. A future reports settledness; a
        suspendable reports whether its body has begun; an `__await__` object wraps
        one of those. So a lazy type nobody enumerated cannot slip through as a
        silent `done` -- which is exactly how `concurrent.futures.Future` did.
        """
        loop = asyncio.new_event_loop()
        try:

            async def coro_fn():  # pragma: no cover - never driven
                return 1

            def gen_fn():  # pragma: no cover - never driven
                yield 1

            cf: concurrent.futures.Future = concurrent.futures.Future()
            c = coro_fn()
            g = gen_fn()
            for obj in (c, g, loop.create_future(), cf):
                answers_settled = callable(getattr(obj, "done", None))
                answers_begun = any(is_kind(obj) for _n, is_kind in job_sdk._SUSPENDABLE_KINDS)
                assert answers_settled or answers_begun, (
                    f"{type(obj).__name__} answers neither protocol question, so it "
                    "would be recorded done -- the classifier is not bounded"
                )
                assert job_sdk._undriven_result(obj) != "", f"{type(obj).__name__} slipped through"
            c.close()
            g.close()
            cf.cancel()
        finally:
            loop.close()

    def test_the_unstarted_check_does_not_rely_on_f_lasti(self) -> None:
        """Version guard, because the obvious implementation is a trap.

        `f_lasti` is -1 for an unstarted frame up to 3.10 and an instruction offset
        (0) from 3.11, so a `< 0` test matches NOTHING on 3.12 and every `async def`
        runner would be recorded `done` again. Pinned on the value itself so the
        trap cannot be reintroduced by someone simplifying the protocol table.
        """

        async def coro_fn():  # pragma: no cover - never driven
            return 1

        c = coro_fn()
        try:
            assert inspect.getcoroutinestate(c) == inspect.CORO_CREATED
            # The exact-state helper says "unstarted"; f_lasti may or may not.
            assert job_sdk._undriven_result(c) != ""
            lasti = c.cr_frame.f_lasti
            assert isinstance(lasti, int)  # the attribute exists; its MEANING varies
        finally:
            c.close()

    def test_a_valid_result_is_not_failed_closed(self) -> None:
        """The positive direction, because a classifier with only negative tests
        eventually fails closed on something legitimate -- and that error looks like
        a real failure, so it is harder to notice than the one it replaced."""
        for ok in ({"ok": True}, None, 0, "", [1, 2], object(), 3.5, True, b"bytes"):
            assert job_sdk._undriven_result(ok) == "", f"{ok!r} was wrongly flagged"

    def test_classifying_a_cancelled_future_does_not_raise(self) -> None:
        """`exception()` RAISES `CancelledError` on a cancelled future, and that is
        a `BaseException` -- so the wrong check order would escape `_execute`'s
        `except Exception` rather than being recorded. Pinned because the safe
        order is invisible in the code once it is correct."""
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            fut.cancel()
            with pytest.raises(asyncio.CancelledError):
                fut.exception()  # the trap this ordering exists to avoid
            assert not issubclass(asyncio.CancelledError, Exception)
            # The classifier asks cancelled() first, so it answers instead of raising.
            assert "cancelled future" in job_sdk._undriven_result(fut)
        finally:
            loop.close()

    def test_a_runner_returning_a_failed_future_is_failed_not_done(self, sdk: JobSDK) -> None:
        """End to end: a settled-but-failed future is a failure, not a success.

        `done()` answers "is it settled", not "did it succeed"; an earlier revision
        of this change set conflated the two and recorded `done`.
        """

        def wrapper(handle: JobHandle):
            loop = asyncio.new_event_loop()
            try:
                fut = loop.create_future()
                fut.set_exception(ValueError("the inner operation failed"))
                return fut
            finally:
                loop.close()

        sdk.register("failedfut", wrapper)
        run = _wait_terminal(sdk, sdk.start("failedfut"))
        assert run.status == FAILED
        assert "ValueError" in run.error
        assert "ran and raised" in run.error
        # And it must NOT claim the body never ran, because it did.
        assert "never ran" not in run.error

    def test_a_runner_returning_a_cancelled_future_is_failed(self, sdk: JobSDK) -> None:
        """The third outcome, kept distinct from the other two."""

        def wrapper(handle: JobHandle):
            loop = asyncio.new_event_loop()
            try:
                fut = loop.create_future()
                fut.cancel()
                return fut
            finally:
                loop.close()

        sdk.register("cancelledfut", wrapper)
        run = _wait_terminal(sdk, sdk.start("cancelledfut"))
        assert run.status == FAILED
        assert "cancelled future" in run.error
        assert "never completed" in run.error

    def test_a_pending_future_is_retired_by_cancel_not_close(self) -> None:
        """Futures have no `close`, so the hygiene path must reach for `cancel`.

        Pinned directly on the observable rather than through a run, because the
        earlier docstring asserted futures were closable and nothing failed -- the
        claim was wrong and untested at the same time.
        """
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            assert not hasattr(fut, "close"), "if futures gain close(), revisit the order"
            job_sdk._close_quietly(fut)
            assert fut.cancelled(), "a pending future was left un-retired"
        finally:
            loop.close()

    def test_a_coroutine_is_retired_by_close(self) -> None:
        """The other arm of the same loop, so neither method can quietly go away."""

        async def never_awaited():  # pragma: no cover - never driven, by design
            return 1

        coro = never_awaited()
        job_sdk._close_quietly(coro)
        # A closed coroutine refuses to be started, which is what silences the
        # "was never awaited" warning at collection time.
        with pytest.raises(RuntimeError):
            coro.send(None)

    def test_the_undriven_failure_writes_once_through_the_guarded_path(self, sdk: JobSDK) -> None:
        """One-writer-per-run is load-bearing, so neither new branch may add a
        write site: each sets fields and lets the existing terminal write persist
        them, exactly like the `except` branch."""
        source = Path(job_sdk.__file__).read_text(encoding="utf-8")
        body = source.split("def _execute(")[1].split("def _write_terminal(")[0]
        # `_persist` appears once (the STARTING -> RUNNING write); the terminal
        # write goes through `_write_terminal`, which owns the retry + discard check.
        assert body.count("self._persist(") == 1
        assert body.count("self._write_terminal(") == 1

    def test_an_undriven_coroutine_is_closed_so_it_does_not_warn(self, sdk: JobSDK) -> None:
        """The record already says what happened; a garbage-collector warning
        fired later points at the wrong place."""
        closed: list[bool] = []

        class Probe:
            def __await__(self):  # pragma: no cover - never awaited
                yield

            def close(self) -> None:
                closed.append(True)

        sdk.register("probe", lambda h: Probe())
        assert _wait_terminal(sdk, sdk.start("probe")).status == FAILED
        assert closed == [True]


# ── 8. stopping work the runner spawned outside the worker thread ──


class TestStoppingWorkTheRunnerHandedBack:
    """Why handed-back work is stoppable at all.

    The SDK cannot stop work a runner spawned onto a thread it does not own -- unless
    the runner hands that work back, which is what submitting to a pool and returning
    the unwaited `concurrent.futures.Future` does. For one instant `_execute` holds
    the only reference to that work.

    It already ASKED that reference to stop before this change, as hygiene, and threw
    the answer away -- so the record warned about work the next line had guaranteed
    would never run. These tests pin both directions: the record must say a stop
    happened when it did, and must never say one happened when it did not.
    """

    def test_work_the_runner_spawned_is_stopped_and_the_record_says_so(self, sdk: JobSDK) -> None:
        """The losing branch, end to end: spawned outside the thread, then stopped.

        A one-worker pool is occupied, so the runner's own submission sits PENDING and
        the runner returns it unwaited. Two things must hold together: the spawned work
        never runs, and the record does not warn the owner about work that cannot
        overlap a retry.
        """
        entered = threading.Event()
        release = threading.Event()
        ran: list[str] = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def occupy() -> None:
            entered.set()
            release.wait(5)

        def spawned() -> str:
            ran.append("spawned")
            return "spawned"

        def wrapper(handle: JobHandle):
            pool.submit(occupy)
            assert entered.wait(5), "premise: the single worker must be busy"
            # Queued behind `occupy`, so PENDING, and handed back undriven.
            return pool.submit(spawned)

        try:
            sdk.register("poolspawn", wrapper)
            run = _wait_terminal(sdk, sdk.start("poolspawn"))
            assert run.status == FAILED, run.error
            assert "not settled" in run.error
            assert ran == [], f"the spawned work ran despite the stop: {ran}"
            assert "may still be running" not in run.error, (
                "the record warns the owner about work this SDK stopped, so work that "
                "provably never started reads as work a retry could overlap"
            )
            assert (
                "cannot stop" not in run.error
            ), "the record claims it cannot stop work it did stop"
            assert "stopped it" in run.error, "the record does not say the stop happened"
            assert (
                "cannot overlap" in run.error
            ), "the record does not tell the owner a retry is safe"
        finally:
            release.set()
            # flake-ok: pre-existing pool join after the release; every task it waits on is bounded by its own 5 s wait
            pool.shutdown(wait=True)
        assert ran == [], "the cancelled work ran once the pool drained"

    def test_work_already_executing_is_never_claimed_stopped(self, sdk: JobSDK) -> None:
        """The direction that matters more: a stop must not be reported when it failed.

        Work already running in a pool cannot be stopped from here. Reporting success
        would be worse than reporting failure -- the owner would believe the work is
        over and proceed -- so this run keeps the disclosure verbatim.
        """
        entered = threading.Event()
        release = threading.Event()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def slow() -> str:
            entered.set()
            release.wait(5)
            return "done"

        def wrapper(handle: JobHandle):
            future = pool.submit(slow)
            assert entered.wait(5), "premise: the work must be executing, not queued"
            return future

        try:
            sdk.register("poolrunning", wrapper)
            run = _wait_terminal(sdk, sdk.start("poolrunning"))
            assert run.status == FAILED, run.error
            assert "may still be running" in run.error
            assert "cannot stop" in run.error
            assert (
                "cannot overlap" not in run.error
            ), "a stop was claimed for work that is still executing"
            assert (
                "stopped it" not in run.error
            ), "a stop was claimed for work that is still executing"
        finally:
            release.set()
            # flake-ok: pre-existing pool join after the release; every task it waits on is bounded by its own 5 s wait
            pool.shutdown(wait=True)

    def test_the_stop_answers_two_ways_one_case_per_answer(self) -> None:
        """One control per branch, so True is not inferred from the others.

        True is the only answer that licenses the record to claim a stop, so each
        way of reaching False needs its own case.
        """
        entered = threading.Event()
        release = threading.Event()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def occupy() -> None:
            entered.set()
            release.wait(5)

        try:
            pool.submit(occupy)
            assert entered.wait(5), "premise: the single worker must be busy"
            pending = pool.submit(lambda: 1)
            assert job_sdk._stop_returned_work(pending) is True
            assert pending.cancelled(), "True was returned without cancelling"

            release.set()
            running_entered = threading.Event()
            running_release = threading.Event()

            def slow() -> int:
                running_entered.set()
                running_release.wait(5)
                return 1

            running = pool.submit(slow)
            assert running_entered.wait(5), "premise: the work must be executing"
            assert job_sdk._stop_returned_work(running) is False
            running_release.set()

            settled: concurrent.futures.Future = concurrent.futures.Future()
            settled.set_result(1)
            assert job_sdk._stop_returned_work(settled) is False
        finally:
            release.set()
            # flake-ok: pre-existing pool join after the release; every task it waits on is bounded by its own 5 s wait
            pool.shutdown(wait=True)

    def test_a_callback_raising_baseexception_cannot_corrupt_the_record(self, sdk: JobSDK) -> None:
        """The stop runs app code on this thread, so it must not lose the record.

        `Future.cancel` invokes done callbacks inline and `_invoke_callbacks` catches
        only `Exception`, so a callback raising `SystemExit` escapes `cancel()` --
        measured on 3.10, 3.11 and 3.12. Unfenced, that escape passes `_execute`'s
        `except JobCancelled` and `except Exception`, leaves the status at RUNNING,
        and the `finally` persists a `running` record while dropping the live entry
        and the dedupe key. The run then reads as active with nothing owning it, and
        nothing revisits it until a restart makes its origin foreign.
        """
        entered = threading.Event()
        release = threading.Event()
        ran: list[str] = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def occupy() -> None:
            entered.set()
            release.wait(5)

        def hostile(_future: concurrent.futures.Future) -> None:
            raise SystemExit("a done callback that does not play nicely")

        def wrapper(handle: JobHandle):
            pool.submit(occupy)
            assert entered.wait(5), "premise: the single worker must be busy"
            future = pool.submit(lambda: ran.append("spawned"))
            future.add_done_callback(hostile)
            return future

        try:
            sdk.register("hostilecb", wrapper)
            run_id = sdk.start("hostilecb")

            def _terminal():
                r = sdk.get(run_id)
                return r if r is not None and r.is_terminal else None

            run = wait_until(_terminal, timeout=5.0, describe=lambda: "the record vanished")
            assert run is not None, "the record vanished"
            assert run.status == FAILED, (
                f"a done callback raising BaseException left the record at "
                f"{run.status!r} with no worker owning it, so the run reads as "
                f"active forever and only a restart resolves it"
            )
            # The fence under-claims rather than guessing, so the owner gets the
            # hedged disclosure even though the state did transition.
            assert "may still be running" in run.error
            assert "cannot overlap" not in run.error
        finally:
            release.set()
            # flake-ok: pre-existing pool join after the release; every task it waits on is bounded by its own 5 s wait
            pool.shutdown(wait=True)
        assert ran == [], "the cancelled work ran once the pool drained"

    def test_a_truthy_cancel_is_not_accepted_as_a_stop(self) -> None:
        """Why the answer is keyed on the TYPE and not on having a `cancel` method.

        Measured identically on 3.10, 3.11 and 3.12: an `asyncio.Task` whose coroutine
        suppresses `CancelledError` answers `cancel()` with True and then finishes and
        returns a value. A duck-typed check would read that True as a stop and record
        success for work that is still going. So both asyncio carriers, and anything
        merely shaped like a future, answer False and keep the honest disclosure.
        """
        kept_going: list[str] = []

        async def main() -> object:
            async def suppress() -> str:
                try:
                    # flake-ok: pre-existing 3 s coroutine body that the task's cancel interrupts at once
                    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing coroutine body that the cancel interrupts, moved verbatim; tracked for a de-flake pass
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    kept_going.append("ran on past the cancel")
                    await asyncio.sleep(0)
                    return "finished anyway"
                return "not cancelled"

            task = asyncio.ensure_future(suppress())
            # flake-ok: pre-existing 50 ms pause that lets the task reach its sleep before the cancel, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms pause before the cancel, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.05)
            assert task.cancel() is True, "premise: asyncio reports the cancel taken"
            # flake-ok: pre-existing await of the task cancelled just above, whose coroutine returns right after the cancel
            return await task

        assert asyncio.run(main()) == "finished anyway"
        assert kept_going == ["ran on past the cancel"]

        loop = asyncio.new_event_loop()
        try:
            unsettled = loop.create_future()
            assert job_sdk._stop_returned_work(unsettled) is False
            assert not unsettled.cancelled(), "an asyncio future was touched off-loop"
            verdict = job_sdk._undriven_result(unsettled)
            assert "may still be running" in verdict
            assert "cannot overlap" not in verdict
            unsettled.cancel()
        finally:
            loop.close()

        class Lookalike:
            def done(self) -> bool:
                return False

            def cancel(self) -> bool:
                return True

        assert job_sdk._stop_returned_work(Lookalike()) is False
        assert "may still be running" in job_sdk._undriven_result(Lookalike())
