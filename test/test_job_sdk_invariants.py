"""The async twins and the Job SDK's defensive invariants.

``start_async`` and ``cancel_async`` match their sync twins. The invariants cover the
redaction, audit and ``OSError`` fallbacks; the discard guard; the dedupe claim under a
race, after a failed write and after a refused thread start; the scrub of runner text; the
interruption cause and prior status ``reconcile`` records; a reconcile pass
that walks past an unusable record, and ``from_dict``'s coercion of a wrong-typed field; and
a disable that stops its workers, waits out a stubborn one without parking the event loop,
closes the SDK and refuses a launch caught in the ``starting`` window. The lifecycle and the
process-wide registry live in ``test_job_sdk.py``, what a runner hands back in
``test_job_sdk_returns.py``, the ``start`` params channel in ``test_job_sdk_params.py``, and
the ``sdk`` fixture and the bounded waits in ``job_sdk_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from job_sdk_helpers import (  # noqa: F401 -- the fixture is requested by name
    _wait_terminal,
    _wait_until,
    sdk_fixture,
)

from kiro_crew.apps import job_sdk
from kiro_crew.apps.job_sdk import (
    _CLEANUP_JOIN_SECS,
    CANCELLED,
    DONE,
    FAILED,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    STARTING,
    TERMINAL_STATES,
    CleanupResult,
    JobError,
    JobHandle,
    JobRun,
    JobSDK,
    JobStore,
    forget_sdk,
    register_sdk,
)
from kiro_crew.testing.wait import wait_until

# ---------------------------------------------------------------------------
# 7. JobHandle.progress and bounded line tail
# ---------------------------------------------------------------------------


def _handle_over(tmp_path: Path, run: JobRun) -> tuple[JobHandle, JobStore, JobSDK]:
    """A handle plus the REAL SDK whose guarded writer owns the discard check.

    The handle carries no writer: the only mid-life write is the worker's
    terminal one, so the discard check lives entirely in ``JobSDK._persist``.
    Handing back the SDK lets a test exercise that real guard rather than a
    stand-in copy of it.
    """
    sdk = JobSDK("handle-test", tmp_path)
    return JobHandle(run), sdk.store, sdk


# ---------------------------------------------------------------------------
# 13. start_async / cancel_async match sync twins
# ---------------------------------------------------------------------------


class TestAsyncTwins:
    def test_start_async_runs_and_reaches_done(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h: {"async": True})
        run_id = asyncio.run(sdk.start_async("work"))
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE

    def test_cancel_async_matches_sync(self, sdk: JobSDK) -> None:
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            # flake-ok: pre-existing runner parked on its cancel event until the cancel_async under test sets it, moved verbatim; tracked for a de-flake pass
            h.cancelled.wait()

        sdk.register("loop", runner, cancellable=True)
        run_id = sdk.start("loop")
        assert started.wait(5.0)
        assert asyncio.run(sdk.cancel_async(run_id)) is True
        run = _wait_terminal(sdk, run_id)
        assert run.status == CANCELLED

    def test_cancel_async_unknown_returns_false(self, sdk: JobSDK) -> None:
        assert asyncio.run(sdk.cancel_async("f" * 32)) is False


# ---------------------------------------------------------------------------
# Misc invariants
# ---------------------------------------------------------------------------


class TestMisc:
    def test_terminal_states_membership(self) -> None:
        assert DONE in TERMINAL_STATES
        assert FAILED in TERMINAL_STATES
        assert CANCELLED in TERMINAL_STATES
        assert INTERRUPTED in TERMINAL_STATES
        assert RUNNING not in TERMINAL_STATES

    def test_is_terminal_property(self) -> None:
        assert JobRun(run_id="x", app="a", kind="k", status=DONE).is_terminal is True
        assert JobRun(run_id="x", app="a", kind="k", status=RUNNING).is_terminal is False


# ---------------------------------------------------------------------------
# Defensive branches: redaction fallback, audit swallow, OSError paths
# ---------------------------------------------------------------------------


class TestDefensiveBranches:
    def test_redact_falls_back_to_raw_when_security_raises(self, sdk: JobSDK, monkeypatch) -> None:
        """If the redaction chain itself raises, the raw error text survives
        (redaction must never mask the error)."""

        def boom(_text):
            raise RuntimeError("redaction backend down")

        monkeypatch.setattr("kiro_crew.security.pass_compose.compose", boom)

        def failer(h, **kw):
            raise ValueError("SENTINEL-ERR-TEXT")

        sdk.register("boom", failer)
        run_id = sdk.start("boom")
        run = _wait_terminal(sdk, run_id)
        assert run.status == FAILED
        assert "SENTINEL-ERR-TEXT" in run.error

    def test_audit_failure_is_swallowed(self, sdk: JobSDK, monkeypatch) -> None:
        """A SEL audit failure must not fail the job."""
        import kiro_crew.apps.job_sdk as m

        def boom():
            raise RuntimeError("sel down")

        monkeypatch.setattr(m, "sel", boom)
        sdk.register("work", lambda h, **kw: {"ok": True})
        run_id = sdk.start("work")  # _audit("job_start") swallows the raise
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE

    def test_reconcile_survives_write_oserror(self, sdk: JobSDK, monkeypatch) -> None:
        """A write failure during reconcile is logged and skipped, not raised."""
        run = JobRun(
            run_id="a" * 32,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        (sdk.store.dir / f"{run.run_id}.json").write_text(json.dumps(run.to_dict(), indent=1))

        def boom(_run):
            raise OSError("disk full")

        monkeypatch.setattr(sdk.store, "write", boom)
        # Does not raise; the un-writable record is not counted as flipped.
        assert sdk.reconcile() == 0

    def test_remove_all_survives_unlink_oserror(self, tmp_path: Path, monkeypatch) -> None:
        store = JobStore(tmp_path)
        run = JobRun(run_id="b" * 32, app="x", kind="k", status=DONE)
        store.write(run)

        real_unlink = Path.unlink

        def boom(self, *a, **k):
            raise OSError("locked")

        monkeypatch.setattr(Path, "unlink", boom)
        # Counted as a FAILURE, not silently dropped: reporting only successes
        # let a partial delete read as a clean one, so disable would claim the
        # app's runs were gone while records remained.
        assert store.remove_all() == (0, 1)
        monkeypatch.setattr(Path, "unlink", real_unlink)

    def test_remove_all_empty_dir_returns_zero(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "absent")
        assert store.remove_all() == (0, 0)


# ---------------------------------------------------------------------------
# The discard guard — the other half of the resurrection fix
# ---------------------------------------------------------------------------


class TestDiscardGuard:
    """``JobHandle`` has TWO write paths and cleanup must silence both.

    ``TestRemoveAll`` pins the worker's terminal write. This pins the guarded
    writer directly: a write arriving after the record was dropped would
    recreate the file just as surely, because ``JobStore.write`` mkdirs and
    writes unconditionally and cannot tell a first write from a resurrection.
    """

    def test_the_guarded_writer_refuses_a_discarded_handle(self, tmp_path: Path) -> None:
        run = JobRun(run_id="ab" * 16, app="demo", kind="work", status=RUNNING)
        handle, store, sdk = _handle_over(tmp_path, run)

        assert sdk._persist(run, handle) is True  # noqa: SLF001 - the guard under test
        assert store.read(run.run_id) is not None

        assert store.remove_all() == (1, 0)
        handle.discarded.set()

        # The worker still tries to write its outcome; it must not land. The
        # writer refuses under the same lock the discard was set with, so there
        # is no check-then-act window for cleanup to slip through -- and the
        # refusal is a False return, not an exception, so the caller's live-table
        # and dedupe bookkeeping still runs.
        run.status = DONE
        assert sdk._persist(run, handle) is False  # noqa: SLF001 - the guard under test
        assert store.read(run.run_id) is None

    def test_handle_exposes_its_run_id(self, tmp_path: Path) -> None:
        run = JobRun(run_id="cd" * 16, app="demo", kind="work")
        handle, _store, _sdk = _handle_over(tmp_path, run)
        assert handle.run_id == run.run_id

    def test_reconcile_does_not_overwrite_a_run_that_finished_mid_pass(self, sdk: JobSDK) -> None:
        """A REAL interleaving, and the assertion is that the record is UNCHANGED.

        `reconcile` decides from a snapshot `iter_runs` decoded from disk, and writes
        later. A worker that finishes in between has already written its terminal
        record AND left `_live` -- the departure happens after the write -- so the
        `live` check does not close the window. Before the fix this pass wrote
        `interrupted` over a true `done`, with cause `terminal_write_lost`: it
        claimed the terminal write was lost when the write had succeeded and this
        pass had destroyed it.

        The ordering is driven, not mocked: every step of `reconcile` runs for real
        and only the worker's completion is scheduled, using the generator's own
        yield as the suspension point. A mocked ordering would prove the mock.

        The assertion is full-record equality, deliberately. "reconcile returned
        early" is a PROXY for "the record was not corrupted", and substituting a
        proxy for the fact is the defect this whole change set exists to remove --
        so this test must not commit it either.
        """
        release = threading.Event()
        body_entered = threading.Event()

        def runner(handle: JobHandle) -> None:
            body_entered.set()
            release.wait(5)

        sdk.register("work", runner)
        run_id = sdk.start("work")
        assert body_entered.wait(5), "the worker never entered its body"

        wait_until(lambda: sdk.get(run_id).status == RUNNING, timeout=5)
        assert sdk.get(run_id).status == RUNNING

        real_iter = sdk._store.iter_runs  # noqa: SLF001 - driving the interleaving
        finished: list[dict] = []

        def interleaved():
            for run in real_iter():
                if run.run_id == run_id:
                    # The snapshot reconcile will decide from says `running`...
                    assert run.status == RUNNING
                    # ...and now the worker really completes and really leaves _live.
                    release.set()

                    def _settled():
                        r = sdk.get(run_id)
                        return (
                            r
                            if r is not None and r.is_terminal and run_id not in sdk._live
                            else None
                        )  # noqa: SLF001

                    latest = wait_until(
                        _settled, timeout=5, describe=lambda: "the worker did not settle in time"
                    )
                    finished.append(latest.to_dict())
                yield run

        sdk._store.iter_runs = interleaved  # type: ignore[method-assign] # noqa: SLF001
        try:
            sdk.reconcile()
        finally:
            sdk._store.iter_runs = real_iter  # type: ignore[method-assign] # noqa: SLF001

        terminal_record = finished[0]
        assert terminal_record["status"] == DONE, "premise: the run really completed"
        # THE assertion: the record on disk is byte-for-byte what the worker wrote.
        assert sdk.get(run_id).to_dict() == terminal_record


class TestReconcilePoisonRecord:
    """One unusable record must cost only itself.

    Found by a pod end-to-end pass: a hand-written record whose run_id was not
    hex made ``JobStore._path`` raise ``ValueError``, which escaped ``reconcile``
    (it caught only ``OSError``) and abandoned every remaining run of that app.
    The symptom was the exact one the pass exists to clear -- runs stuck at
    ``running`` forever -- and it was invisible except as a logged traceback.
    """

    def test_a_record_with_an_unwritable_id_does_not_abandon_the_rest(
        self, sdk: JobSDK, tmp_path: Path
    ) -> None:
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        # Sorted first, so it is reached BEFORE the healthy record: if it aborts
        # the loop, the sibling below is never reconciled and the test fails.
        (jobs_dir / "0-bad-id.json").write_text(
            json.dumps(
                {
                    "run_id": "not-hex-at-all",
                    "app": "demo",
                    "kind": "work",
                    "status": RUNNING,
                    "origin": "f" * 32,
                }
            )
        )
        healthy_id = "ab" * 16
        (jobs_dir / f"{healthy_id}.json").write_text(
            json.dumps(
                {
                    "run_id": healthy_id,
                    "app": "demo",
                    "kind": "work",
                    "status": RUNNING,
                    "origin": "f" * 32,
                }
            )
        )

        # The poison record is skipped; the healthy one is still resolved.
        assert sdk.reconcile() == 1
        assert sdk.get(healthy_id).status == INTERRUPTED


class TestConcurrentDedupe:
    """Two simultaneous starts with one key must produce ONE run.

    The double-click / two-tabs case is the whole point of `dedupe_key`, and the
    first version checked the key and claimed it in two separate critical
    sections with a disk read in between, so both callers could see no owner and
    both run -- paying the cost twice, which is exactly the hazard dedupe exists
    to remove.
    """

    def test_two_racing_starts_yield_one_run(self, sdk: JobSDK) -> None:
        bodies = threading.Semaphore(0)
        entered: list[str] = []

        def runner(h, **kw):
            entered.append(h.run_id)
            bodies.acquire(timeout=5.0)
            return {}

        sdk.register("work", runner, cancellable=False)

        gate = threading.Barrier(3, timeout=5.0)
        ids: list[str] = []
        errors: list[BaseException] = []

        def racer() -> None:
            try:
                # flake-ok: pre-existing rendezvous of the racing starts, bounded by the Barrier's own 5 s timeout
                gate.wait()
                ids.append(sdk.start("work", dedupe_key="one"))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=racer, name=f"racer-{i}") for i in range(2)]
        for t in threads:
            t.start()
        # flake-ok: pre-existing rendezvous of the racing starts, bounded by the Barrier's own 5 s timeout
        gate.wait()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, errors
        assert len(ids) == 2
        assert ids[0] == ids[1], f"dedupe let both starts win: {ids}"
        # And only one body ever ran. Waited for rather than assumed: the worker's
        # first act is the STARTING -> RUNNING write, so a body enters strictly
        # after a lock acquisition and a small disk write. Reading `entered` the
        # instant both starts returned would only pass while that gap stays short
        # -- a timing assumption, not a property.
        # `ids[0] == ids[1]` above already proves ONE run exists, so nothing else
        # can append here.
        _wait_until(lambda: len(entered) >= 1)
        assert len(entered) == 1, entered

        bodies.release()
        _wait_terminal(sdk, ids[0])


class TestRunnerOutputIsRedacted:
    """Runner-produced text is scrubbed at INGEST, so the record on disk is
    clean too -- not only the HTTP response. A runner that shells out can quote
    back a command line carrying a credential.

    In P1 there is exactly one such field: the error of a failed run. The
    progress and result channels are out of P1, so the surface that needs
    scrubbing is a single string rather than arbitrary nested data.
    """

    def test_a_failed_runners_error_is_scrubbed_on_disk_and_in_the_record(
        self, sdk: JobSDK
    ) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"

        def runner(h):
            raise RuntimeError(f"upload failed: aws_secret_access_key={secret}")

        sdk.register("leaky", runner)
        run_id = sdk.start("leaky")
        run = _wait_terminal(sdk, run_id)

        assert run.status == FAILED
        on_disk = (sdk.store.dir / f"{run_id}.json").read_text()
        assert secret not in on_disk, "the credential reached the record on disk"
        assert secret not in run.error
        # Scrubbed, not blanked: the operator still learns what failed.
        assert "upload failed" in run.error


class TestClaimAndWriteFailurePaths:
    """The two failure paths the atomicity and persistence fixes introduced."""

    def test_a_failed_initial_write_releases_the_dedupe_claim(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the key stays owned by a run that never started, and every
        later start with that key would adopt a run that does not exist."""
        sdk.register("work", lambda h, **kw: {})

        def boom(_run):
            raise OSError("disk full")

        monkeypatch.setattr(sdk.store, "write", boom)
        # The guarded writer turns any write failure into a False return, so
        # start refuses with a JobError rather than leaking the raw OSError --
        # and, crucially, without skipping the claim release below.
        with pytest.raises(JobError):
            sdk.start("work", dedupe_key="k")

        # Claim released: with a working store the same key starts a real run.
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the failing write stub before the same key starts a real run, moved verbatim; tracked for a de-flake pass
        monkeypatch.undo()
        run_id = sdk.start("work", dedupe_key="k")
        assert run_id
        assert _wait_terminal(sdk, run_id).status == DONE

    def test_a_transient_terminal_write_is_retried(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost terminal write leaves the record reading `running` while the
        work is finished, so one retry covers the transient case.

        Targeted by the STATUS being written rather than by call index. An earlier
        form failed the second write and called it the terminal one, but the second
        write is the ``starting`` -> ``running`` transition -- so it never exercised
        a terminal write at all, and it passed only because a failed running
        transition was being ignored. That is the defect this change set fixes, so
        the guard had to stop depending on it.
        """
        real_write = sdk.store.write
        calls: list[str] = []
        failed_once: list[bool] = []

        def flaky(run):
            calls.append(run.status)
            if run.status in job_sdk.TERMINAL_STATES and not failed_once:
                failed_once.append(True)
                raise OSError("transient")
            return real_write(run)

        sdk.register("work", lambda h, **kw: {"ok": True})
        monkeypatch.setattr(sdk.store, "write", flaky)
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert failed_once, "the terminal write was never made to fail"
        assert calls.count(DONE) >= 2, f"the retry did not happen: {calls}"


class TestRecordIsAlwaysWritable:
    """A runner cannot make its own record unserializable.

    `json.dumps` raises TypeError on a set, a Path, or any object, and such an
    exception escaping the terminal write would skip the live-table and
    dedupe-key cleanup that follows it -- leaking a claim no later start could
    release. In P1 that failure is impossible by CONSTRUCTION rather than
    prevented by a sanitizer: every field is minted by the SDK from a str, an int
    or a bool, and the one runner-supplied field is a string. The tests that used
    to feed a Path and a set through the result and params channels went with
    those channels.
    """

    def test_no_runner_can_put_a_non_json_value_in_its_record(self, sdk: JobSDK) -> None:
        """A runner returning unserializable junk still lands a readable record.

        The return value is discarded, so this holds no matter what the runner
        produces -- and it is the property that keeps the claim from leaking,
        because the terminal write cannot raise on serialization.
        """
        sdk.register("weird", lambda h: {"path": Path("/tmp/x"), "s": {1, 2}, "n": 3})
        run_id = sdk.start("weird", dedupe_key="k")
        run = _wait_terminal(sdk, run_id)

        assert run.status == DONE
        # Readable back off disk, so the write really happened.
        assert sdk.get(run_id) is not None
        # Bookkeeping was NOT skipped: the key is free, so a new start with the
        # same key begins a new run rather than adopting a finished one.
        # A terminal record can precede release of the live entry and dedupe key.
        _wait_until(lambda: run_id not in sdk.cancelling_and_live_ids()[1])
        second = sdk.start("weird", dedupe_key="k")
        assert second != run_id
        _wait_terminal(sdk, second)

    def test_every_field_of_a_live_record_is_json_serializable(self, sdk: JobSDK) -> None:
        """Pinned on the record's own shape, so a future field cannot slip in
        holding something ``json.dumps`` refuses.

        One field holds a MAP, and it is admitted only while every key and value
        in it is a string: a map one level deep of strings is what keeps the
        record a flat document, and a nested value would fail here. The branch
        names that field, so the scalar pin below still covers every other one --
        a second field arriving with a map lands on the pin and fails.
        """
        sdk.register("work", lambda h: None)
        run_id = sdk.start("work", params={"account": "123456789012"})
        run = _wait_terminal(sdk, run_id)
        for name, value in run.to_dict().items():
            if name == "params" and isinstance(value, dict):
                assert all(
                    isinstance(k, str) and isinstance(v, str) for k, v in value.items()
                ), f"{name} holds an entry that is not string to string"
                continue
            assert isinstance(value, (str, int, bool)), f"{name} is {type(value).__name__}"


class TestDisableStopsTheWork:
    """Disable must STOP the runs, not merely forget them.

    Signalling alone left the threads running: a disabled app kept doing real,
    side-effecting work with its records already deleted. Cleanup now
    bounded-joins each worker.
    """

    def test_remove_all_waits_for_a_cooperating_worker(self, sdk: JobSDK) -> None:
        started = threading.Event()
        finished = threading.Event()

        def runner(h, **kw):
            started.set()
            # flake-ok: pre-existing runner parked on its cancel event until remove_all_async signals it, moved verbatim; tracked for a de-flake pass
            h.cancelled.wait()
            finished.set()
            return {}

        sdk.register("slow", runner, cancellable=True)
        sdk.start("slow")
        assert started.wait(5.0)

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup == CleanupResult(1, 0, 0)
        # The worker is already done by the time cleanup returns -- that is the
        # difference between stopping the work and forgetting it.
        assert finished.is_set()
        assert not any(t.name.startswith("job:") for t in threading.enumerate())


class TestSanitizeInvariant:
    """Nothing a runner supplied reaches disk unsanitized.

    Four rounds of review each found a different channel the scrub did not cover --
    top-level result values, then ``step`` and nested values, then dict KEYS,
    then ``progress_pct`` past the float guard -- because the writer's backstop
    was a hand-written LIST of fields and a list is a thing to forget. P1 has one
    runner-supplied field instead of five, so the funnel has one input and
    cannot have a member missing. The channels those rounds found are out of P1
    and return in P2 on types that are safe by construction.
    """

    def test_the_writer_scrubs_an_error_set_behind_its_back(self, sdk: JobSDK) -> None:
        """The backstop. A field assigned directly, skipping the ingest site, is
        still scrubbed because the single writer re-scrubs before writing."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        run = JobRun(run_id="ef" * 16, app="test-app", kind="work", status=RUNNING)
        run.error = f"failed with aws_secret_access_key={secret}"
        assert sdk._persist(run) is True  # noqa: SLF001 - the invariant under test
        on_disk = (sdk.store.dir / f"{run.run_id}.json").read_text()
        assert secret not in on_disk
        assert secret not in run.error

    def test_error_is_the_only_runner_supplied_field(self) -> None:
        """The claim the one-line backstop rests on, pinned.

        If a future field lands that a runner or caller can set, this fails and
        whoever added it has to decide how it is sanitized -- which is the review
        conversation the hand-written list kept losing.
        """
        sdk_minted = {
            "run_id",
            "app",
            "kind",
            "status",
            "origin",
            "pid",
            "dedupe_key",
            # Caller-supplied, like ``dedupe_key``, and still not the writer's
            # problem: ``_validated_params`` refuses anything but a bounded flat
            # map of strings the scrub would leave alone, at the single call that
            # accepts one, so what reaches the writer is already sanitized. A
            # field a RUNNER can set still lands in the difference below.
            "params",
            "cancellable",
            "created_at",
            "updated_at",
            "finished_at",
            # Set only by ``reconcile``, from closed sets this module owns: a
            # status constant and a ``CAUSE_*`` constant. No runner and no caller
            # can reach either, so the one-line backstop still has one input.
            "interrupted_from",
            "interrupt_cause",
            # A boolean the SDK sets from the handle at the terminal write: the
            # runner CALLS ``checkpoint`` (a method), it does not SUPPLY this
            # value. A runner cannot write True here without having reached a
            # checkpoint, and cannot write a payload through it -- so it stays a
            # lifecycle fact the one-line backstop need not sanitize.
            "work_observed",
        }
        fields = set(JobRun.__dataclass_fields__)
        assert fields - sdk_minted == {"error"}

    def test_a_write_lost_starting_record_says_the_body_never_ran(self, sdk: JobSDK) -> None:
        """The consequence is DERIVED from the progress axis, not asserted.

        The `starting` -> `running` transition is a precondition for calling the
        runner, so a record still at `starting` whose terminal write was lost
        proves execution never began. Saying "not known" there would be this
        module's own defect: a record declining to state what it knows.
        """
        run_id = uuid.uuid4().hex
        sdk.store.write(
            JobRun(
                run_id=run_id,
                app=sdk.app_name,
                kind="work",
                status=STARTING,
                origin=job_sdk._ORIGIN,
            )
        )
        assert sdk.reconcile() >= 1
        rec = sdk.get(run_id)
        assert rec.interrupt_cause == job_sdk.CAUSE_TERMINAL_WRITE_LOST
        assert rec.interrupted_from == STARTING
        assert "never ran" in rec.error
        assert "precondition" in rec.error
        assert "not known" not in rec.error

    def test_a_write_lost_running_record_still_says_the_outcome_is_unknown(
        self, sdk: JobSDK
    ) -> None:
        """The other side: `running` genuinely leaves the outcome open, so the two
        progress values must not collapse onto one consequence."""
        sdk.register("work", lambda h: None)
        run_id = uuid.uuid4().hex
        sdk.store.write(
            JobRun(
                run_id=run_id,
                app=sdk.app_name,
                kind="work",
                status=RUNNING,
                origin=job_sdk._ORIGIN,
            )
        )
        assert sdk.reconcile() >= 1
        rec = sdk.get(run_id)
        assert rec.interrupt_cause == job_sdk.CAUSE_TERMINAL_WRITE_LOST
        assert "not known" in rec.error
        assert "never ran" not in rec.error

    def test_an_own_origin_record_is_not_blamed_on_a_restart(self, sdk: JobSDK) -> None:
        """The cause must not claim a restart when this process minted the record.

        `origin == _ORIGIN` is positive evidence the gateway did NOT restart, so
        both restart causes are false there however `known` reads. Reachable
        without a restart: `reconcile_all` runs after the app loop, while an app's
        `on_startup` hook can already have started a job in this same process, so
        a job whose terminal write is lost is reconciled by the process that
        minted it.
        """
        sdk.register("work", lambda h: None)
        run_id = uuid.uuid4().hex
        sdk.store.write(
            JobRun(
                run_id=run_id,
                app=sdk.app_name,
                kind="work",
                status=RUNNING,
                origin=job_sdk._ORIGIN,  # minted by THIS process
            )
        )
        assert sdk.reconcile() >= 1
        rec = sdk.get(run_id)
        assert rec.status == INTERRUPTED
        assert rec.interrupt_cause == job_sdk.CAUSE_TERMINAL_WRITE_LOST
        assert rec.interrupted_from == RUNNING
        # The message must not claim a restart, and must not guess the outcome.
        assert "restart" not in rec.error.lower()
        assert "not known" in rec.error

    def test_a_foreign_origin_record_still_reports_the_restart(self, sdk: JobSDK) -> None:
        """The other side, so the new branch cannot swallow the original cause."""
        sdk.register("work", lambda h: None)
        run_id = uuid.uuid4().hex
        sdk.store.write(
            JobRun(
                run_id=run_id,
                app=sdk.app_name,
                kind="work",
                status=RUNNING,
                origin="a-process-that-is-gone",
            )
        )
        assert sdk.reconcile() >= 1
        rec = sdk.get(run_id)
        assert rec.interrupt_cause == job_sdk.CAUSE_PROCESS_GONE
        assert "restarted" in rec.error

    def test_a_foreign_origin_unregistered_kind_still_reports_unregistered(
        self, sdk: JobSDK
    ) -> None:
        """The third arm: the `known` split survives, scoped to foreign origins."""
        run_id = uuid.uuid4().hex
        sdk.store.write(
            JobRun(
                run_id=run_id,
                app=sdk.app_name,
                kind="never-registered",
                status=RUNNING,
                origin="a-process-that-is-gone",
            )
        )
        assert sdk.reconcile() >= 1
        rec = sdk.get(run_id)
        assert rec.interrupt_cause == job_sdk.CAUSE_RUNNER_UNREGISTERED

    def test_the_interruption_fields_can_only_hold_values_this_module_minted(self) -> None:
        """The claim that makes the two new fields SDK-minted rather than a
        payload channel: every value either is written from a module constant, or
        is a status this module defined.

        Pinned as the INVARIANT rather than as one literal expression. An earlier
        form asserted the exact text of the cause conditional, so adding a third
        cause failed it for the wrong reason -- the single assignment site was
        still intact, only the expression had changed.
        """
        source = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted(Path(job_sdk.__file__).parent.glob("job_sdk*.py"))
        )
        # ``interrupted_from`` is only ever assigned the record's own status, and
        # ``interrupt_cause`` only ever a CAUSE_* constant -- one site each.
        assert source.count("run.interrupted_from = ") == 1
        assert "run.interrupted_from = run.status" in source
        assert source.count("run.interrupt_cause = ") == 1
        # The one site assigns a local, so the local's every value must itself be
        # a CAUSE_* constant. Checked inside `reconcile`, where it is composed.
        body = source.split("def reconcile(")[1].split("def _persist(")[0]
        assert "run.interrupt_cause = cause" in body
        cause_writes = [
            ln.strip().split("=", 1)[1].strip()
            for ln in body.splitlines()
            if re.match(r"\s*cause\s*=\s*", ln)
        ]
        assert cause_writes, "the cause local is never assigned -- harness stale"
        for value in cause_writes:
            assert value.startswith("CAUSE_"), f"cause assigned a non-constant: {value!r}"


class TestDisableIsTerminalForTheSDK:
    """Cleanup must close the SDK, not merely snapshot what was live.

    If marking and snapshotting were the whole of cleanup's critical section, a
    ``start`` that had already read the runner table could claim AFTER the
    snapshot and spawn a worker cleanup would never see -- a disabled app doing
    real, side-effecting work with its records already deleted. The route guard
    does not cover this: it re-reads the manifest, but the app's own code holds a
    reference to the SDK object and calls it directly.
    """

    def test_start_after_disable_is_refused(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h: None)
        asyncio.run(sdk.remove_all_async())
        with pytest.raises(JobError, match="no longer accepting jobs"):
            sdk.start("work")

    def test_a_start_racing_cleanup_cannot_claim_after_the_snapshot(self, sdk: JobSDK) -> None:
        """The window itself: close, then start, and no worker may exist.

        Asserted on the THREAD table rather than on the refusal, because the
        defect was not a missing error message -- it was app code still running.
        """
        # flake-ok: pre-existing 30 s stand-in runner body that never runs because every start is refused
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing stand-in runner body that the refused starts never call, moved verbatim; tracked for a de-flake pass
        sdk.register("work", lambda h: time.sleep(30))
        asyncio.run(sdk.remove_all_async())
        for _ in range(5):
            with pytest.raises(JobError):
                sdk.start("work")
        assert not [t for t in threading.enumerate() if t.name.startswith("job:")]
        assert sdk.list_active() == []


class TestDedupeKeyIsNotLogged:
    """A caller-supplied dedupe key must not reach the gateway log.

    The gateway log is durable and is served by ``/api/logs``, and a dedupe key is
    whatever the caller chose -- an account id, a path, or a credential -- so
    quoting it in the adoption line would turn a de-duplication aid into an output
    boundary nobody has classified as one.
    """

    def test_the_adoption_line_names_the_run_not_the_key(
        self, sdk: JobSDK, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"
        gate = threading.Semaphore(0)
        sdk.register("slow", lambda h: gate.acquire(timeout=10))

        first = sdk.start("slow", dedupe_key=secret)
        with caplog.at_level(logging.INFO, logger="kiro_crew.apps.job_sdk"):
            second = sdk.start("slow", dedupe_key=secret)
        assert second == first, "the second start should have adopted the first"

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in logged, "the dedupe key reached the gateway log"
        # Still useful: the line has to identify WHAT was adopted.
        assert first in logged and "slow" in logged

        gate.release()
        _wait_terminal(sdk, first)


class TestEnableReconcilesStaleRecords:
    """A mid-life enable must reconcile, not wait for the next gateway start.

    The boot-time pass runs ONCE, after the enable loop, so an app enabled later
    never got one -- and reconciliation is only decidable after the app's startup
    hook registers its runners, since "no runner for this kind" is one of the two
    outcomes it reports. Without a pass here a record left non-terminal by a
    previous process stayed ``running`` until the next restart, and
    ``list_active`` kept reporting work that had already stopped: the exact
    symptom this SDK exists to remove.
    """

    def test_on_app_enable_flips_a_foreign_running_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from kiro_crew.apps import hooks_integration as hi

        sdk = JobSDK("enable-app", tmp_path)
        sdk.register("work", lambda h: None)
        # Written by a process that is gone: a foreign origin with a live status.
        stale = JobRun(
            run_id="1a" * 16,
            app="enable-app",
            kind="work",
            status=RUNNING,
            origin="a-process-that-no-longer-exists",
        )
        sdk.store.write(stale)
        assert sdk.get(stale.run_id).status == RUNNING

        async def _no_crons(*_a, **_k):
            return 0

        monkeypatch.setattr(hi, "app_execution_denied", lambda *a, **k: "")
        monkeypatch.setattr(hi, "register_app_crons_with_service", _no_crons)
        monkeypatch.setattr(hi, "sel", lambda: MagicMock())
        monkeypatch.setattr(hi, "_publish_hook_health", lambda *a, **k: None)
        monkeypatch.setattr(hi, "_lifecycle_dispatcher", None)
        monkeypatch.setattr(hi, "_route_registry", None)
        monkeypatch.setattr(
            hi,
            "_build_app_context_from_info",
            lambda *a, **k: SimpleNamespace(job=sdk),
        )

        app_info = {"manifest": {"backend": {"hooks": {"on_startup": "app.py:start"}}}}
        result = asyncio.run(hi.on_app_enable("enable-app", app_info))

        assert sdk.get(stale.run_id).status == INTERRUPTED
        assert "1" in result.get("job_reconcile", ""), result

    def test_a_reconcile_failure_does_not_fail_the_enable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enable must survive it: a stale record is worth less than the app."""
        from types import SimpleNamespace

        from kiro_crew.apps import hooks_integration as hi

        sdk = JobSDK("enable-app", tmp_path)

        def boom() -> int:
            raise OSError("the runs directory is unreadable")

        monkeypatch.setattr(sdk, "reconcile", boom)

        async def _no_crons(*_a, **_k):
            return 0

        monkeypatch.setattr(hi, "app_execution_denied", lambda *a, **k: "")
        monkeypatch.setattr(hi, "register_app_crons_with_service", _no_crons)
        monkeypatch.setattr(hi, "sel", lambda: MagicMock())
        monkeypatch.setattr(hi, "_publish_hook_health", lambda *a, **k: None)
        monkeypatch.setattr(hi, "_lifecycle_dispatcher", None)
        monkeypatch.setattr(hi, "_route_registry", None)
        monkeypatch.setattr(
            hi,
            "_build_app_context_from_info",
            lambda *a, **k: SimpleNamespace(job=sdk),
        )

        app_info = {"manifest": {"backend": {"hooks": {"on_startup": "app.py:start"}}}}
        result = asyncio.run(hi.on_app_enable("enable-app", app_info))
        assert result["job_reconcile"].startswith("failed:")


class TestNonObjectRecordCostsOnlyItself:
    """A file holding valid JSON that is not an OBJECT must not strand the scan.

    ``[]``, ``"x"`` and ``5`` all survive ``json.loads``, and then ``.items()``
    raises ``AttributeError`` -- which neither reader's handler named, so it
    escaped as a 500 from the route and abandoned the whole reconciliation pass,
    stranding every later record of that app. Refused as ``ValueError`` at
    ``from_dict``, which is the failure both readers already treat as "this one
    record is unusable".
    """

    def test_from_dict_refuses_a_non_object_body(self) -> None:
        for body in ([], "x", 5, None, 1.5):
            with pytest.raises(ValueError, match="not an object"):
                JobRun.from_dict(body)  # type: ignore[arg-type]

    def test_read_returns_none_for_a_non_object_record(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        store.dir.mkdir(parents=True, exist_ok=True)
        (store.dir / ("9a" * 16 + ".json")).write_text("[]")
        assert store.read("9a" * 16) is None

    def test_iter_runs_skips_it_and_keeps_going(self, tmp_path: Path) -> None:
        """The blast radius: the GOOD record after it must still be yielded."""
        store = JobStore(tmp_path)
        store.dir.mkdir(parents=True, exist_ok=True)
        # Named to sort BEFORE the good one, so a raise would hide the good one.
        (store.dir / ("0b" * 16 + ".json")).write_text('"just a string"')
        good = JobRun(run_id="ff" * 16, app="x", kind="k", status=DONE)
        store.write(good)
        assert [r.run_id for r in store.iter_runs()] == [good.run_id]

    def test_reconcile_is_not_abandoned_by_one(self, tmp_path: Path) -> None:
        sdk = JobSDK("poison-app", tmp_path)
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        (sdk.store.dir / ("0c" * 16 + ".json")).write_text("[1, 2, 3]")
        stale = JobRun(
            run_id="ee" * 16,
            app="poison-app",
            kind="work",
            status=RUNNING,
            origin="a-process-that-is-gone",
        )
        sdk.store.write(stale)
        # The unusable file must not stop the stale one from being resolved.
        assert sdk.reconcile() == 1
        assert sdk.get(stale.run_id).status == INTERRUPTED


class TestDisableCleansUpRegardlessOfTheGrant:
    """Teardown asks the REGISTRY, not the manifest.

    Gating cleanup on the `jobs` grant meant revoking the grant and then
    disabling took the one path that skips it: the SDK stays registered from the
    enable that DID have the grant, its workers keep executing, and the lookup
    entry is dropped afterwards -- so nothing can reach them again.
    """

    def test_cleanup_runs_for_an_app_whose_grant_was_revoked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps import hooks_integration as hi

        sdk = JobSDK("revoked-app", tmp_path)
        started = threading.Event()
        sdk.register("work", lambda h: (started.set(), h.cancelled.wait(10)), cancellable=True)
        sdk.start("work")
        assert started.wait(5.0)

        register_sdk(sdk)
        try:
            monkeypatch.setattr(hi, "get_sdk", lambda name: sdk if name == "revoked-app" else None)
            monkeypatch.setattr(hi, "sel", lambda: MagicMock())

            result: dict = {}
            # No manifest and no grant are consulted at all: the helper takes only
            # the app name, which is the point of the fix.
            asyncio.run(hi._cleanup_app_jobs("revoked-app", result))

            assert sdk.list_active() == []
            assert not [t for t in threading.enumerate() if t.name.startswith("job:")]
            assert "job_cleanup" in result
        finally:
            forget_sdk("revoked-app")


class TestStartingStateClosesTheLaunchWindow:
    """The interval between CLAIMING a run and LAUNCHING it is real and named.

    It contains a disk write, and three defects came from code with no way to name
    it: cleanup joined a thread that was never started (RuntimeError, which
    escaped the disable so the records were never deleted); a start that had
    already claimed launched into an app being disabled; and the record said
    `running` while no worker existed. `_Live.started` and the `STARTING` status
    make each decidable, and both transitions happen under the one lock.
    """

    def test_cleanup_does_not_crash_on_an_unstarted_entry(self, sdk: JobSDK) -> None:
        """The exact crash: join() on a thread that was never started.

        Driven by putting the state in directly, because that IS the state cleanup
        snapshotted -- and the assertion is that the records still get deleted,
        since the real damage was the RuntimeError escaping so `remove_all` never
        ran and a disabled app kept its records AND its worker.
        """
        run = JobRun(run_id="7a" * 16, app=sdk.app_name, kind="work", status=STARTING)
        sdk.store.write(run)
        never_started = threading.Thread(target=lambda: None, name="job:never-started")
        sdk._live[run.run_id] = job_sdk._Live(  # noqa: SLF001 - the state under test
            handle=JobHandle(run), thread=never_started, started=False
        )

        result = asyncio.run(sdk.remove_all_async())

        assert result.removed == 1, "the record was not deleted -- cleanup did not finish"
        assert result.still_running == 0, "an unstarted thread cannot be stubborn"
        assert result.is_clean
        assert sdk.get(run.run_id) is None

    def test_a_disable_landing_inside_the_window_refuses_the_launch(self, sdk: JobSDK) -> None:
        """The window `_closed` alone cannot cover: this start claimed FIRST.

        Cleanup is triggered from inside the initial record write, which is
        precisely where the window is, so no sleep is involved and the ordering is
        not left to the scheduler.
        """
        ran: list[str] = []
        sdk.register("work", lambda h: ran.append(h.run_id))

        real_persist = sdk._persist  # noqa: SLF001 - driving the real writer
        fired: list[bool] = []

        def persist_then_disable(run, handle=None):
            ok = real_persist(run, handle)
            if not fired:
                fired.append(True)
                # The app is disabled while this start sits between its claim and
                # its launch. Its live entry is already in the table.
                asyncio.run(sdk.remove_all_async())
            return ok

        sdk._persist = persist_then_disable  # type: ignore[method-assign]
        with pytest.raises(JobError, match="stopped accepting jobs"):
            sdk.start("work")

        assert ran == [], "a worker was launched for an app that was being disabled"
        assert not [t for t in threading.enumerate() if t.name.startswith("job:")]
        assert sdk.list_active() == []

    def test_the_first_write_says_starting_not_running(self, sdk: JobSDK) -> None:
        """Observed at the write itself, because that is where the lie was.

        Asserting only what the WORKER sees cannot detect a regression: if the
        initial status went back to `running`, the worker would still report
        `running` and such a test would stay green while the record was again
        claiming a worker existed during the initial disk write.
        """
        statuses: list[str] = []
        real_persist = sdk._persist  # noqa: SLF001 - observing the real writer

        def record_status(run, handle=None):
            statuses.append(run.status)
            return real_persist(run, handle)

        sdk._persist = record_status  # type: ignore[method-assign]
        sdk.register("work", lambda h: None)
        run_id = sdk.start("work")
        _wait_terminal(sdk, run_id)

        assert statuses[0] == STARTING, f"the first write claimed {statuses[0]!r}"
        assert RUNNING in statuses, "the worker never recorded that it was running"
        assert statuses[-1] == DONE

    def test_the_record_says_starting_until_the_worker_says_running(self, sdk: JobSDK) -> None:
        """The record must not assert a worker exists before one does."""
        seen: list[str] = []
        release = threading.Semaphore(0)

        def runner(h):
            # By now the worker has completed the STARTING -> RUNNING transition.
            got = sdk.get(h.run_id)
            seen.append(got.status if got else "<gone>")
            release.acquire(timeout=5.0)

        sdk.register("work", runner)
        run_id = sdk.start("work")
        _wait_until(lambda: bool(seen))
        assert seen == [RUNNING]

        release.release()
        assert _wait_terminal(sdk, run_id).status == DONE

    def test_starting_is_not_terminal_so_reconcile_resolves_it(self, tmp_path: Path) -> None:
        """A process that died mid-launch leaves `starting` behind; it must not
        be mistaken for a finished run."""
        assert STARTING not in TERMINAL_STATES
        sdk = JobSDK("mid-launch", tmp_path)
        sdk.store.write(
            JobRun(
                run_id="7b" * 16,
                app="mid-launch",
                kind="work",
                status=STARTING,
                origin="a-process-that-is-gone",
            )
        )
        assert sdk.reconcile() == 1
        assert sdk.get("7b" * 16).status == INTERRUPTED


class TestForeignRecordFieldsAreCoerced:
    """A wrong-TYPED field must not abort the scan either.

    A record whose `error` is a number reached `_persist`, where the slice after
    the redaction raised `TypeError` outside that method's own try -- the same
    blast radius as a non-object body, one level in. Coerced at `from_dict`, the
    single point foreign data enters, so every consumer downstream gets the type
    it is written against.
    """

    def test_wrong_typed_fields_fall_back_to_defaults(self) -> None:
        run = JobRun.from_dict(
            {
                "run_id": "8a" * 16,
                "app": "x",
                "kind": "k",
                "error": 12345,  # not a string
                "pid": "not-a-number",
                "cancellable": "yes",  # not a bool
                "status": ["running"],  # not a string
            }
        )
        assert run.error == ""
        assert run.pid == 0
        assert run.cancellable is False
        assert run.status == QUEUED

    def test_a_true_bool_is_not_accepted_as_a_pid(self) -> None:
        """bool IS an int subclass, so an int check alone would take True."""
        assert JobRun.from_dict({"run_id": "8b" * 16, "pid": True}).pid == 0

    def test_reconcile_survives_a_numeric_error_field(self, tmp_path: Path) -> None:
        sdk = JobSDK("typed-app", tmp_path)
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        (sdk.store.dir / ("8c" * 16 + ".json")).write_text(
            json.dumps(
                {
                    "run_id": "8c" * 16,
                    "app": "typed-app",
                    "kind": "work",
                    "status": RUNNING,
                    "origin": "gone",
                    "error": 999,
                }
            )
        )
        assert sdk.reconcile() == 1
        assert sdk.get("8c" * 16).status == INTERRUPTED


class TestCleanupDoesNotBlockTheLoop:
    """An async SDK method must never park the event loop.

    `remove_all_async` bounded-joins workers, and doing that inline made every
    disable stall the whole gateway for the deadline -- the exact hazard
    CronSDK's docstring spells out. The join runs on a worker thread now.
    """

    def test_the_loop_keeps_running_while_cleanup_waits(self, sdk: JobSDK) -> None:
        started = threading.Event()
        stop = threading.Event()
        # Held so this deliberately uncooperative worker can be joined at the end.
        # It is the one test that WANTS a thread to outlive cleanup's deadline, so
        # it is also the one that has to clean up after itself -- left running it
        # would still hold the SDK's lock and write files while later tests ran.
        worker: list[threading.Thread] = []

        def runner(h, **kw):
            worker.append(threading.current_thread())
            started.set()
            # Ignores the cancel signal, so cleanup must wait out its deadline.
            stop.wait(_CLEANUP_JOIN_SECS + 2.0)
            return {}

        sdk.register("stubborn", runner, cancellable=True)
        sdk.start("stubborn")
        assert started.wait(5.0)

        async def drive() -> int:
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)  # flake-ok: measures free-loop time per tick
                    ticks += 1

            spin = asyncio.ensure_future(ticker())
            try:
                result = await sdk.remove_all_async()
            finally:
                spin.cancel()
            # The worker never cooperated, so cleanup reports it rather than
            # claiming a clean teardown.
            assert result.still_running == 1
            assert not result.is_clean
            return ticks

        ticks = asyncio.run(drive())
        stop.set()
        # A blocking join would have starved the ticker for the whole deadline.
        # flake-ok: pre-existing floor of 5 loop ticks over the 5 s cleanup deadline that only a parked loop misses
        assert ticks > 5, f"the loop was parked during cleanup (only {ticks} ticks)"
        # Released, then actually waited for: the assertion above is about the
        # LOOP not stalling, which says nothing about the thread still running.
        for t in worker:
            t.join(timeout=5.0)
            assert not t.is_alive(), "the stubborn worker outlived its test"


class TestThreadStartFailure:
    """A refused thread must not leave a claimed run nothing will finish."""

    def test_a_refused_thread_leaves_no_ghost(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sdk.register("work", lambda h, **kw: {}, cancellable=False)

        def refuse(self):  # noqa: ANN001 - patching threading.Thread.start
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", refuse)
        with pytest.raises(JobError):
            sdk.start("work", dedupe_key="k")
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that restores Thread.start before the same key starts again, moved verbatim; tracked for a de-flake pass
        monkeypatch.undo()

        # No run is left claiming to be active, and the key is free again.
        assert sdk.list_active() == []
        again = sdk.start("work", dedupe_key="k")
        assert _wait_terminal(sdk, again).status == DONE
