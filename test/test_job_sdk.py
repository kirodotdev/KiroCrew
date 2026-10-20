"""Tests for the Job SDK — app-scoped durable runs.

Covers the runner registry, per-run JSON records, cooperative cancellation via
a threading.Event, the startup reconciliation pass, and the process-wide SDK
registry.

Runs execute on real daemon threads, so terminal state is awaited with a
bounded poll (``_wait_terminal``) rather than a fixed sleep — a fixed sleep is
what makes such a suite flaky on a loaded runner.

This file pins the lifecycle from registration through ``remove_all_async`` and its disable
trace, and the process-wide registry. The async twins and the defensive invariants live in
``test_job_sdk_invariants.py``; what a runner hands back, with the two axes an interruption
records and the reads of a malformed record, in ``test_job_sdk_returns.py``; the ``start``
params channel in ``test_job_sdk_params.py``; and the ``sdk`` fixture and the bounded waits
in ``job_sdk_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from job_sdk_helpers import (  # noqa: F401 -- the fixture is requested by name
    _wait_terminal,
    _wait_until,
    sdk_fixture,
)

from conftest import requires_symlinks
from kiro_crew.apps import job_sdk
from kiro_crew.apps.job_sdk import (
    CANCELLED,
    DONE,
    FAILED,
    INTERRUPTED,
    RUNNING,
    CleanupResult,
    JobCancelled,
    JobError,
    JobHandle,
    JobRun,
    JobSDK,
    JobStore,
    UnknownJobKind,
    forget_sdk,
    get_sdk,
    reconcile_all,
    register_sdk,
    registered_apps,
)

# ---------------------------------------------------------------------------
# 1. register / kinds / is_cancellable
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_and_kinds(self, sdk: JobSDK) -> None:
        sdk.register("build", lambda h: {"ok": True})
        sdk.register("deploy", lambda h: {"ok": True}, cancellable=True)
        assert sdk.kinds() == ["build", "deploy"]  # sorted

    def test_is_cancellable(self, sdk: JobSDK) -> None:
        sdk.register("plain", lambda h: {})
        sdk.register("stoppable", lambda h: {}, cancellable=True)
        assert sdk.is_cancellable("plain") is False
        assert sdk.is_cancellable("stoppable") is True
        # Unknown kind is not cancellable.
        assert sdk.is_cancellable("nope") is False

    def test_register_empty_kind_raises(self, sdk: JobSDK) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            sdk.register("", lambda h: {})


# ---------------------------------------------------------------------------
# 2. start() on an unregistered kind
# ---------------------------------------------------------------------------


class TestStartUnknown:
    def test_start_unknown_kind_raises(self, sdk: JobSDK) -> None:
        with pytest.raises(UnknownJobKind, match="no registered runner"):
            sdk.start("ghost")


# ---------------------------------------------------------------------------
# 3. Happy run: reaches done; the runner's return value is not recorded
# ---------------------------------------------------------------------------


class TestHappyRun:
    def test_run_reaches_done(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h: {"answer": 42})
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert run.finished_at != ""
        assert run.error == ""

    def test_the_runner_gets_its_handle_and_nothing_else(self, sdk: JobSDK) -> None:
        """P1 has no params channel, so a runner is a one-argument callable.

        Pinned because the signature IS the contract a P2 consumer writes
        against: reintroducing kwargs is a deliberate P2 change, not something
        that should be able to drift back in unnoticed.
        """
        seen: list[object] = []

        def runner(h):
            seen.append(h)

        sdk.register("work", runner)
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert len(seen) == 1 and seen[0].run_id == run_id

    def test_a_returned_value_is_not_persisted_anywhere(self, sdk: JobSDK) -> None:
        """The return value is discarded, and no field appears to hold it.

        Asserting on the SERIALIZED record rather than on an attribute, so this
        fails if a result-shaped field is added back without a design decision.
        """
        sdk.register("work", lambda h: {"secret": "AKIAIOSFODNN7EXAMPLE"})
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(run.to_dict())
        assert set(run.to_dict()) == {
            "run_id",
            "app",
            "kind",
            "status",
            "origin",
            "pid",
            "dedupe_key",
            # The caller's parameter map. Admitted rather than refused because it
            # is a flat map of strings, bounded and checked at the single call
            # that accepts it, so the writer has no payload to reason about. A
            # nested or free-typed field is still a design decision this set
            # forces somebody to make.
            "params",
            "cancellable",
            "created_at",
            "updated_at",
            "finished_at",
            "error",
            # Written only by ``reconcile``, and only for a run whose process is
            # gone. Both are drawn from SDK-owned closed sets (a status constant
            # and a ``CAUSE_*`` constant), so neither is a channel a runner or a
            # caller can reach -- which is why they are here and not a result
            # field wearing a new name.
            "interrupted_from",
            "interrupt_cause",
            # An SDK-minted boolean recording whether the runner reached a
            # ``handle.checkpoint`` -- an OBSERVED fact, not the runner's return
            # value. Set by the SDK from the handle at the terminal write, never
            # supplied by the runner, so like the two above it is a lifecycle fact
            # and not the result payload this guard blocks. Its presence is a
            # deliberate design decision.
            "work_observed",
        }


# ---------------------------------------------------------------------------
# 4. Raising runner -> failed with truncated, recorded error
# ---------------------------------------------------------------------------


class TestFailingRun:
    def test_raising_runner_reaches_failed_with_error(self, sdk: JobSDK) -> None:
        def boom(h, **kw):
            raise RuntimeError("kaboom happened")

        sdk.register("boom", boom)
        run_id = sdk.start("boom")
        run = _wait_terminal(sdk, run_id)
        assert run.status == FAILED
        assert "kaboom happened" in run.error

    def test_error_is_truncated_to_2000_chars(self, sdk: JobSDK) -> None:
        def boom(h, **kw):
            raise RuntimeError("x" * 5000)

        sdk.register("boom", boom)
        run_id = sdk.start("boom")
        run = _wait_terminal(sdk, run_id)
        assert run.status == FAILED
        assert len(run.error) == 2000


# ---------------------------------------------------------------------------
# 5. Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_polling_runner_reaches_cancelled(self, sdk: JobSDK) -> None:
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            h.cancelled.wait()

        sdk.register("loop", runner, cancellable=True)
        run_id = sdk.start("loop")
        assert started.wait(5.0)
        assert sdk.cancel(run_id) is True
        run = _wait_terminal(sdk, run_id)
        assert run.status == CANCELLED

    def test_cancel_unknown_run_id_returns_false(self, sdk: JobSDK) -> None:
        assert sdk.cancel("deadbeef" * 4) is False

    def test_cancel_non_cancellable_run_returns_false(self, sdk: JobSDK) -> None:
        release = threading.Event()
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            release.wait(5.0)
            return {}

        # Registered cancellable=False (the default).
        sdk.register("plain", runner)
        run_id = sdk.start("plain")
        assert started.wait(5.0)
        # Live but not declared cancellable -> False.
        assert sdk.cancel(run_id) is False
        release.set()
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE

    def test_cancel_already_terminal_run_returns_false(self, sdk: JobSDK) -> None:
        sdk.register("quick", lambda h, **kw: {"done": True}, cancellable=True)
        run_id = sdk.start("quick")
        _wait_terminal(sdk, run_id)
        # Popped from _live once terminal, so cancel returns False.
        assert sdk.cancel(run_id) is False


# ---------------------------------------------------------------------------
# 5a. checkpoint - the progress OBSERVATION and the observed-stop cancel
# ---------------------------------------------------------------------------


class TestCheckpoint:
    """The checkpoint channel: one call reports progress AND observes cancel.

    The record STATES observed progress rather than inferring it from the return
    value, and ``cancelled`` names an observed stop rather than a set flag. Both
    come from ``handle.checkpoint``, and the pairing is the point: a checkpoint
    that reports progress necessarily observes the cancel signal at the same call,
    so a progress channel that cannot carry cancellation is not representable.
    """

    def test_checkpoint_records_work_observed_on_done(self, sdk: JobSDK) -> None:
        """A runner that checkpoints and returns leaves ``work_observed`` True."""

        def runner(h, **kw):
            h.checkpoint()
            return {}

        sdk.register("obs", runner)
        run_id = sdk.start("obs")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert run.work_observed is True

    def test_no_checkpoint_leaves_work_observed_false_but_still_done(self, sdk: JobSDK) -> None:
        """Absence of a checkpoint is NOT penalised: the run is still ``done``,
        and the classifier stays the backstop. ``work_observed`` reads False so a
        consumer can tell "did work, observed" from "may have done work"."""

        sdk.register("silent", lambda h, **kw: {})
        run_id = sdk.start("silent")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert run.work_observed is False

    def test_checkpoint_raises_and_records_cancelled_as_observed_stop(self, sdk: JobSDK) -> None:
        """A cancel pending at a checkpoint raises ``JobCancelled``; the run is
        recorded ``cancelled`` because the stack unwound at the checkpoint -- an
        observed stop, not the flag alone. Work done BEFORE the cancel is still
        observed."""
        started = threading.Event()
        ran_on = []

        def runner(h, **kw):
            h.checkpoint()  # progress observed before any cancel
            started.set()
            for _ in range(500):
                # Reaches a checkpoint each iteration; raises once cancel lands.
                h.checkpoint()
                ran_on.append(1)
                h.cancelled.wait()  # block until cancel; checkpoint above raises on it

        sdk.register("stoppable", runner, cancellable=True)
        run_id = sdk.start("stoppable")
        assert started.wait(5.0)
        assert sdk.cancel(run_id) is True
        run = _wait_terminal(sdk, run_id)
        assert run.status == CANCELLED
        # The stop was observed at a checkpoint, so work_observed is True and no
        # error was recorded (a cancel is an outcome, not a fault).
        assert run.work_observed is True
        assert run.error == ""

    def test_checkpoint_after_cancel_does_not_reach_a_false_done(self, sdk: JobSDK) -> None:
        """The concrete hazard for in-thread work: a runner cannot run on to a
        ``done`` after a cancel, because the next checkpoint raises before the
        return."""
        started = threading.Event()
        reached_end = threading.Event()

        def runner(h, **kw):
            started.set()
            h.cancelled.wait()
            # A cancel is now pending. The runner tries to finish anyway; the
            # checkpoint refuses to let it record success.
            h.checkpoint()
            reached_end.set()
            return {}

        sdk.register("racer", runner, cancellable=True)
        run_id = sdk.start("racer")
        assert started.wait(5.0)
        assert sdk.cancel(run_id) is True
        run = _wait_terminal(sdk, run_id)
        assert run.status == CANCELLED
        assert not reached_end.is_set()

    def test_checkpoint_cancel_check_precedes_progress(self) -> None:
        """A checkpoint reached in an already-cancelled run credits no progress:
        the cancel check comes first, so the raise happens before the flag is
        set. Tested on a bare handle to isolate the ordering."""
        run = JobRun(run_id="a" * 32, app="x", kind="k", cancellable=True)
        handle = JobHandle(run)
        handle.cancelled.set()
        with pytest.raises(JobCancelled):
            handle.checkpoint()
        assert handle.work_observed is False

    def test_jobcancelled_is_not_a_joberror(self) -> None:
        """A cancel is the runner's cooperative exit, not an SDK refusal, so it
        must not be caught by handlers that catch ``JobError``; and it is an
        ``Exception`` not a ``BaseException`` so a runner's own ``except
        Exception`` cleanup guard still runs."""
        assert not issubclass(JobCancelled, JobError)
        assert issubclass(JobCancelled, Exception)


# ---------------------------------------------------------------------------
# 5b. the cancelling snapshot — the read side of "cancel writes nothing"
# ---------------------------------------------------------------------------


class TestCancellingIds:
    """A requested cancel must be readable before the worker records it.

    ``cancel`` writes nothing on purpose, so without a derived read the request
    exists only in the response to the cancel call itself. These pin the derived
    read that makes it survive a fresh read of the record.
    """

    def test_request_is_reported_until_the_worker_records_it(self, sdk: JobSDK) -> None:
        started = threading.Event()
        release = threading.Event()

        def runner(h, **kw):
            # A checkpoint deliberately far away: this runner does not poll
            # ``h.cancelled`` until the test lets it. That holds the request
            # window open deterministically rather than racing a worker that
            # would settle before the assertion runs.
            started.set()
            release.wait(5.0)
            return {}

        sdk.register("slow", runner, cancellable=True)
        run_id = sdk.start("slow")
        try:
            assert started.wait(5.0)
            # Nothing has been asked for yet.
            assert sdk.cancelling_and_live_ids()[0] == frozenset()
            assert sdk.cancel(run_id) is True
            # Requested, and readable while the RECORD still says running --
            # which is exactly what a fresh mount has to be able to see.
            assert run_id in sdk.cancelling_and_live_ids()[0]
            record = sdk.get(run_id)
            assert record is not None and record.status == RUNNING
        finally:
            # In FINALLY: an assertion failing above must not park this worker
            # for the rest of the session.
            release.set()
        run = _wait_terminal(sdk, run_id)
        assert run.status == CANCELLED
        # The terminal RECORD lands before the worker drops its live entry
        # (``_execute`` writes, then re-takes the lock to pop), so a read of the
        # record can return terminal while the entry -- and with it the derived
        # cancelling flag -- is still a few instructions from disappearing. That
        # is the documented closing window, not a defect; wait for the drop
        # itself rather than inferring it from the status.
        _wait_until(lambda: run_id not in sdk.cancelling_and_live_ids()[1])
        # The worker recorded the outcome and the live entry is gone, so the
        # status now carries the answer and nothing is pending.
        assert sdk.cancelling_and_live_ids()[0] == frozenset()

    def test_a_refused_cancel_reports_nothing(self, sdk: JobSDK) -> None:
        """The snapshot follows the ACCEPTED request, not the attempt.

        A live run that was never declared cancellable refuses ``cancel``, so no
        event is set and nothing may claim a cancel is under way.
        """
        started = threading.Event()
        release = threading.Event()

        def runner(h, **kw):
            started.set()
            release.wait(5.0)
            return {}

        sdk.register("plain", runner)  # cancellable=False, the default
        run_id = sdk.start("plain")
        try:
            assert started.wait(5.0)
            assert sdk.cancel(run_id) is False
            assert sdk.cancelling_and_live_ids()[0] == frozenset()
        finally:
            release.set()
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE


# ---------------------------------------------------------------------------
# 6. dedupe_key
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_second_start_with_same_key_adopts_run(self, sdk: JobSDK) -> None:
        release = threading.Event()
        started = threading.Event()
        calls = []

        def runner(h, **kw):
            calls.append(1)
            started.set()
            release.wait(5.0)
            return {}

        sdk.register("work", runner)
        first = sdk.start("work", dedupe_key="k1")
        assert started.wait(5.0)
        second = sdk.start("work", dedupe_key="k1")
        assert second == first  # adopted, not a new run
        release.set()
        _wait_terminal(sdk, first)
        assert calls == [1]  # body ran exactly once

    def test_two_starts_without_key_produce_two_ids(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h, **kw: {})
        a = sdk.start("work")
        b = sdk.start("work")
        assert a != b
        _wait_terminal(sdk, a)
        _wait_terminal(sdk, b)


# ---------------------------------------------------------------------------
# 8. get / list_active / list_recent
# ---------------------------------------------------------------------------


class TestReadViews:
    def test_get_missing_returns_none(self, sdk: JobSDK) -> None:
        assert sdk.get("c" * 32) is None

    def test_list_active_excludes_terminal_and_filters_kind(self, sdk: JobSDK) -> None:
        release = threading.Event()

        def blocker(h, **kw):
            release.wait(5.0)
            return {}

        sdk.register("live", blocker)
        sdk.register("fast", lambda h, **kw: {})

        live_id = sdk.start("live")
        fast_id = sdk.start("fast")
        _wait_terminal(sdk, fast_id)

        active = sdk.list_active()
        active_ids = {r.run_id for r in active}
        assert live_id in active_ids
        assert fast_id not in active_ids  # terminal excluded

        # kind filter
        assert [r.run_id for r in sdk.list_active(kind="live")] == [live_id]
        assert sdk.list_active(kind="fast") == []

        release.set()
        _wait_terminal(sdk, live_id)

    def test_list_recent_limit_and_ordering(self, sdk: JobSDK) -> None:
        # Write records directly with controlled, distinct updated_at values.
        # (_now() is second-granularity, so real runs in the same second cannot
        # be ordered by wall clock — the ordering CONTRACT is what we pin here.)
        store = sdk.store
        specs = [
            ("a" * 32, "2026-01-01T00:00:01Z"),
            ("b" * 32, "2026-01-01T00:00:03Z"),  # newest
            ("c" * 32, "2026-01-01T00:00:02Z"),
        ]
        store.dir.mkdir(parents=True, exist_ok=True)
        for rid, ts in specs:
            run = JobRun(run_id=rid, app="test-app", kind="work", status=DONE)
            run.updated_at = ts
            run.created_at = ts
            (store.dir / f"{rid}.json").write_text(json.dumps(run.to_dict(), indent=1))

        recent = sdk.list_recent(limit=2)
        assert len(recent) == 2
        # Most-recently-updated first.
        assert [r.run_id for r in recent] == ["b" * 32, "c" * 32]

    def test_list_recent_limit_zero_returns_empty(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h, **kw: {})
        rid = sdk.start("work")
        _wait_terminal(sdk, rid)
        assert sdk.list_recent(limit=0) == []

    def test_list_recent_kind_filter(self, sdk: JobSDK) -> None:
        sdk.register("a", lambda h, **kw: {})
        sdk.register("b", lambda h, **kw: {})
        a_id = sdk.start("a")
        b_id = sdk.start("b")
        _wait_terminal(sdk, a_id)
        _wait_terminal(sdk, b_id)
        recent_a = sdk.list_recent(kind="a")
        assert [r.run_id for r in recent_a] == [a_id]


class TestLiveness:
    """A durable ``running`` record and a process owning the work are separate facts.

    The ``live`` set answers from the same live-table membership that ``cancel``
    and ``reconcile`` already treat as the authority, so a record whose terminal
    write failed twice and a record minted by another gateway process must both
    be absent from it — that is the whole signal.
    """

    def test_live_while_a_registered_thread_runs(self, sdk: JobSDK) -> None:
        started = threading.Event()
        release = threading.Event()

        def runner(h, **kw):
            started.set()
            release.wait(5.0)
            return {}

        sdk.register("slow", runner)
        run_id = sdk.start("slow")
        try:
            assert started.wait(5.0)
            _, live = sdk.cancelling_and_live_ids()
            assert run_id in live
        finally:
            release.set()
        _wait_terminal(sdk, run_id)
        # The worker recorded the outcome and the live entry is gone. Poll for the
        # live entry rather than reading it once: `_execute` pops it AFTER its
        # terminal write lands, so a terminal record does not yet prove the entry
        # is dropped, and asserting on that instant is a race on a loaded runner.
        _wait_until(lambda: sdk.cancelling_and_live_ids() == (frozenset(), frozenset()))

    def test_stale_running_record_is_not_live(self, sdk: JobSDK) -> None:
        """The issue case: a durable record says ``running``, nothing owns it.

        Written raw with a foreign origin — the shape a record from another
        gateway process has, and the same shape a twice-failed terminal write
        leaves behind (a ``running`` record with no live entry). No thread was
        ever registered for it in this process, so liveness must deny it.
        """
        run = JobRun(
            run_id="e" * 32,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        store = sdk.store
        store.dir.mkdir(parents=True, exist_ok=True)
        (store.dir / f"{run.run_id}.json").write_text(json.dumps(run.to_dict()))
        assert sdk.get(run.run_id) is not None  # durably present…
        _, live = sdk.cancelling_and_live_ids()
        assert run.run_id not in live  # …but nothing owns it

    def test_one_snapshot_keeps_cancelling_within_live(self, sdk: JobSDK) -> None:
        """The two sets describe ONE instant, so cancelling ⊆ live holds.

        Both are derived from the live table under a single lock acquisition;
        taken as two separate snapshots a worker finishing between them could
        report a cancel pending on a run nothing owns.
        """
        started = threading.Event()
        release = threading.Event()

        def runner(h, **kw):
            started.set()
            release.wait(5.0)
            return {}

        sdk.register("slow", runner, cancellable=True)
        run_id = sdk.start("slow")
        try:
            assert started.wait(5.0)
            assert sdk.cancel(run_id) is True
            cancelling, live = sdk.cancelling_and_live_ids()
            assert run_id in cancelling
            assert run_id in live
            assert cancelling <= live
        finally:
            release.set()
        _wait_terminal(sdk, run_id)


# ---------------------------------------------------------------------------
# 9. JobRun.from_dict / JobStore.read / _path / iter_runs
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_from_dict_drops_unknown_keys(self) -> None:
        run = JobRun.from_dict(
            {"run_id": "x1", "app": "a", "kind": "k", "bogus": "nope", "status": DONE}
        )
        assert run.run_id == "x1"
        assert run.status == DONE
        assert not hasattr(run, "bogus")

    def test_from_dict_tolerates_missing_required(self) -> None:
        run = JobRun.from_dict({})
        assert run.run_id == ""
        assert run.app == ""
        assert run.kind == ""

    def test_read_missing_file_returns_none(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        assert store.read("d" * 32) is None

    def test_read_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        store.dir.mkdir(parents=True, exist_ok=True)
        (store.dir / ("abcdef" * 4 + ".json")).write_text("{ not json")
        assert store.read("abcdef" * 4) is None

    def test_read_invalid_run_id_returns_none(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        # ValueError from _path is swallowed into None by read.
        assert store.read("NOT-HEX!") is None

    def test_path_rejects_non_hex_id(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        with pytest.raises(ValueError, match="invalid run id"):
            store._path("zzz")
        with pytest.raises(ValueError, match="invalid run id"):
            store._path("")

    def test_path_rejects_an_all_hex_id_of_the_wrong_length(self, tmp_path: Path) -> None:
        """Length is checked, not just the alphabet.

        A very long all-hex id passed the alphabet check and built a filename
        over the OS limit, so the read raised ENAMETOOLONG -- an ``OSError`` no
        handler names -- and the route answered 500 where an unknown id deserves
        404. Short ids are rejected for the same reason they are not run ids.
        """
        store = JobStore(tmp_path)
        for bad in ("a" * 31, "a" * 33, "a" * 5000, "abcdef"):
            with pytest.raises(ValueError, match="invalid run id"):
                store._path(bad)

    def test_an_overlong_id_reads_as_none_not_an_oserror(self, tmp_path: Path) -> None:
        """The route's 404 path depends on this being None rather than a raise."""
        store = JobStore(tmp_path)
        assert store.read("f" * 5000) is None

    def test_iter_runs_skips_unreadable_file(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        store.dir.mkdir(parents=True, exist_ok=True)
        good = JobRun(run_id="a" * 32, app="x", kind="k", status=DONE)
        store.write(good)
        (store.dir / "corrupt.json").write_text("{ broken")
        runs = list(store.iter_runs())
        assert [r.run_id for r in runs] == ["a" * 32]

    def test_iter_runs_empty_when_dir_absent(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "does-not-exist")
        assert list(store.iter_runs()) == []


class TestRecordReadSurvivesTheWriterRace:
    """Both readers go through the Windows-safe helper and decode UTF-8 explicitly.

    A reader racing the single writer's temp-file-plus-rename is invisible on
    POSIX, where rename is atomic for a reader, and raises ``PermissionError`` on
    Windows -- which is why this reddened only the Windows matrix. These pin the
    helper and the encoding so a later "simplify back to read_text" fails here,
    on Linux, instead of on one Windows shard.
    """

    @staticmethod
    def _spy(monkeypatch) -> list[Path]:
        seen: list[Path] = []
        real = job_sdk.read_bytes_with_retry

        def spy(path):
            seen.append(Path(path))
            return real(path)

        monkeypatch.setattr(job_sdk, "read_bytes_with_retry", spy)
        return seen

    def test_read_routes_through_the_retrying_reader(self, tmp_path: Path, monkeypatch) -> None:
        store = JobStore(tmp_path)
        store.write(JobRun(run_id="b" * 32, app="x", kind="k", status=DONE))
        seen = self._spy(monkeypatch)
        assert store.read("b" * 32) is not None
        assert [p.name for p in seen] == [f"{'b' * 32}.json"]

    def test_iter_runs_routes_through_the_retrying_reader(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        store = JobStore(tmp_path)
        store.write(JobRun(run_id="c" * 32, app="x", kind="k", status=DONE))
        seen = self._spy(monkeypatch)
        assert [r.run_id for r in store.iter_runs()] == ["c" * 32]
        assert [p.name for p in seen] == [f"{'c' * 32}.json"]

    def test_non_ascii_record_round_trips_regardless_of_host_locale(self, tmp_path: Path) -> None:
        """``atomic_write`` emits UTF-8, so the reader must decode UTF-8.

        ``read_text()`` decodes in the host LOCALE, so a redacted string holding a
        non-ASCII character survived only where the console happened to be UTF-8.
        Escapes rather than literals keep this file ASCII while still carrying
        multi-byte content through the round trip.
        """
        text = "resum\u00e9 \u4f5c\u4e1a"
        store = JobStore(tmp_path)
        store.write(JobRun(run_id="d" * 32, app="x", kind="k", status=DONE, error=text))
        got = store.read("d" * 32)
        assert got is not None and got.error == text


# ---------------------------------------------------------------------------
# 10. reconcile()
# ---------------------------------------------------------------------------


class TestReconcile:
    def _write_raw(self, store: JobStore, run: JobRun) -> None:
        store.dir.mkdir(parents=True, exist_ok=True)
        path = store.dir / f"{run.run_id}.json"
        path.write_text(json.dumps(run.to_dict(), indent=1))

    def test_foreign_origin_nonterminal_flips_to_interrupted_known_runner(
        self, sdk: JobSDK
    ) -> None:
        sdk.register("work", lambda h, **kw: {})
        run = JobRun(
            run_id="a" * 32,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        self._write_raw(sdk.store, run)
        flipped = sdk.reconcile()
        assert flipped == 1
        reread = sdk.get(run.run_id)
        assert reread.status == INTERRUPTED
        assert "gateway restarted while this was running" in reread.error

    def test_foreign_origin_unknown_runner_error_names_kind(self, sdk: JobSDK) -> None:
        run = JobRun(
            run_id="b" * 32,
            app="test-app",
            kind="gone-kind",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        self._write_raw(sdk.store, run)
        flipped = sdk.reconcile()
        assert flipped == 1
        reread = sdk.get(run.run_id)
        assert reread.status == INTERRUPTED
        assert "no runner is registered" in reread.error
        assert "gone-kind" in reread.error

    def test_terminal_record_left_alone(self, sdk: JobSDK) -> None:
        run = JobRun(
            run_id="c" * 32,
            app="test-app",
            kind="work",
            status=DONE,
            origin="foreign-origin-token",
        )
        self._write_raw(sdk.store, run)
        assert sdk.reconcile() == 0
        assert sdk.get(run.run_id).status == DONE

    def test_own_origin_record_is_resolved_when_nothing_is_executing_it(self, sdk: JobSDK) -> None:
        """Own origin alone is NOT a reason to spare a record.

        A record this process wrote and then lost -- a terminal write that failed
        twice -- carries this origin while nothing is running it, and sparing it
        would leave exactly the stuck-`running` state the pass exists to clear.
        The live table, not the origin, is what says a run is still executing.
        """
        run = JobRun(
            run_id="d" * 32,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin=job_sdk._ORIGIN,
        )
        self._write_raw(sdk.store, run)
        assert sdk.reconcile() == 1
        assert sdk.get(run.run_id).status == INTERRUPTED

    def test_a_run_this_process_is_actually_executing_is_left_alone(self, sdk: JobSDK) -> None:
        """The other half: a genuinely live run must survive a reconcile.

        Reconciliation runs after the enable loop, and an app's on_startup may
        already have started a run, so this is a real ordering, not a hypothetical.
        """
        release = threading.Event()
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            release.wait(5.0)
            return {}

        sdk.register("work", runner)
        run_id = sdk.start("work")
        assert started.wait(5.0)

        assert sdk.reconcile() == 0
        assert sdk.get(run_id).status == RUNNING

        release.set()
        _wait_terminal(sdk, run_id)


# ---------------------------------------------------------------------------
# 11. remove_all_async
# ---------------------------------------------------------------------------


class TestRemoveAll:
    def test_signals_live_and_deletes_records_idempotent(self, sdk: JobSDK) -> None:
        release = threading.Event()
        started = threading.Event()
        cancelled_seen = threading.Event()

        def runner(h, **kw):
            started.set()
            h.cancelled.wait()
            cancelled_seen.set()
            return {}

        sdk.register("live", runner, cancellable=True)
        run_id = sdk.start("live")
        assert started.wait(5.0)

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup == CleanupResult(1, 0, 0)
        # The live run was signalled to stop.
        assert cancelled_seen.wait(5.0)
        release.set()
        _wait_until(lambda: not any(t.name.startswith("job:") for t in threading.enumerate()))
        # The signalled worker cannot write its record back, so the deletion
        # holds and a second cleanup is a no-op.
        assert sdk.get(run_id) is None
        assert asyncio.run(sdk.remove_all_async()) == CleanupResult(0, 0, 0)

    def test_remove_all_deletes_records_of_finished_runs(self, sdk: JobSDK) -> None:
        """With no live worker to race, remove_all_async deletes the record and
        the second call is a no-op."""
        sdk.register("work", lambda h, **kw: {"ok": True})
        run_id = sdk.start("work")
        _wait_terminal(sdk, run_id)
        # No live worker remains, so nothing can rewrite the file.
        assert asyncio.run(sdk.remove_all_async()) == CleanupResult(1, 0, 0)
        assert sdk.get(run_id) is None
        assert asyncio.run(sdk.remove_all_async()) == CleanupResult(0, 0, 0)

    def test_remove_all_is_not_resurrected_by_worker(self, sdk: JobSDK) -> None:
        """A worker returning AFTER cleanup must not write its record back.

        Regression pin. JobStore.write mkdirs and writes unconditionally, so it
        cannot tell a first write from a resurrection; the guarantee comes from
        remove_all_async marking every live handle discarded BEFORE deleting,
        and both write paths honouring that mark. Without it this run reappears
        on disk as `cancelled` and the second remove_all_async returns 1.
        """
        started = threading.Event()
        removed_done = threading.Event()
        finally_done = threading.Event()

        def runner(h, **kw):
            started.set()
            # Do not exit (and thus do not reach the finally-block write) until
            # remove_all has finished deleting the record — this makes the
            # ordering deterministic rather than timing-dependent.
            removed_done.wait(5.0)
            try:
                return
            finally:
                finally_done.set()

        sdk.register("live", runner, cancellable=True)
        run_id = sdk.start("live")
        assert started.wait(5.0)

        cleanup = asyncio.run(sdk.remove_all_async())
        # Only `removed` is asserted here. Whether this worker is still alive
        # when cleanup returns is a race between its own wait and the join
        # deadline, so pinning it would make this test flaky for a reason that
        # has nothing to do with resurrection. TestCleanupDoesNotBlockTheLoop
        # covers the still-running report deterministically, with a runner that
        # ignores its cancel signal outright.
        assert cleanup.removed == 1
        assert sdk.get(run_id) is None  # deleted at this instant
        removed_done.set()
        # Let the worker's finally-block write complete.
        assert finally_done.wait(5.0)
        _wait_until(lambda: not any(t.name.startswith("job:") for t in threading.enumerate()))
        time.sleep(0.05)
        # The record stays deleted, and idempotency still holds -- a resurrected
        # record would make this second call report 1.
        assert sdk.get(run_id) is None
        assert asyncio.run(sdk.remove_all_async()) == CleanupResult(0, 0, 0)


# ---------------------------------------------------------------------------
# 11a. Disable trace — one SEL line per killed run
# ---------------------------------------------------------------------------


class TestDisableTrace:
    """Disable deletes every record, so the SEL trail is the only place "which
    runs were killed" survives. ``remove_all_async`` emits one
    ``job_killed_on_disable`` entry per formerly-live run (and per stale
    non-terminal record) BEFORE the delete; the count-only summary stays.
    """

    @staticmethod
    def _capture_sel(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        events: list[dict] = []
        monkeypatch.setattr(
            job_sdk,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        return events

    @staticmethod
    def _killed(events: list[dict]) -> list[dict]:
        return [e for e in events if e["operation"] == "jobs.job_killed_on_disable"]

    def test_one_audit_entry_per_killed_live_run(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = self._capture_sel(monkeypatch)
        started = {"alpha": threading.Event(), "beta": threading.Event()}

        def runner(h, **kw):
            started[h.kind].set()
            h.cancelled.wait()

        sdk.register("alpha", runner, cancellable=True)
        sdk.register("beta", runner, cancellable=True)
        a = sdk.start("alpha")
        b = sdk.start("beta")
        assert started["alpha"].wait(5.0)
        assert started["beta"].wait(5.0)

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup.removed == 2

        killed = self._killed(events)
        assert {e["resources"] for e in killed} == {f"{a} kind=alpha", f"{b} kind=beta"}
        assert all(e["outcome"] == "ok" for e in killed)
        # Cooperative workers stopped inside the join, so nothing is flagged.
        assert all("still_running" not in e["resources"] for e in killed)
        # The count-only summary is unchanged and still fires.
        summary = [e for e in events if e["operation"] == "jobs.job_remove_all"]
        assert len(summary) == 1
        assert "removed=2" in summary[0]["resources"]

    def test_worker_that_outlives_the_join_is_flagged_still_running(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = self._capture_sel(monkeypatch)
        # Shrink the bounded join so this test does not pay the full deadline;
        # `_join_workers` reads the module global at call time.
        monkeypatch.setattr(job_sdk, "_CLEANUP_JOIN_SECS", 0.2)
        started = threading.Event()
        stop = threading.Event()
        worker: list[threading.Thread] = []

        def runner(h, **kw):
            worker.append(threading.current_thread())
            started.set()
            # Ignores the cancel signal outright, so cleanup must report it.
            stop.wait(10.0)
            return {}

        sdk.register("stubborn", runner, cancellable=True)
        run_id = sdk.start("stubborn")
        assert started.wait(5.0)

        try:
            cleanup = asyncio.run(sdk.remove_all_async())
            assert cleanup.still_running == 1

            killed = self._killed(events)
            assert len(killed) == 1
            assert killed[0]["resources"] == f"{run_id} kind=stubborn still_running"
            summary = [e for e in events if e["operation"] == "jobs.job_remove_all"]
            assert len(summary) == 1
            assert summary[0]["outcome"] == "partial"
        finally:
            # In a finally so a failed assertion cannot leak the worker into
            # later tests: left running it would write into the removed
            # tmp_path.
            stop.set()
            for t in worker:
                t.join(timeout=5.0)
                assert not t.is_alive(), "the stubborn worker outlived its test"

    def test_same_kind_siblings_are_attributed_per_run_not_per_kind(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Thread names are minted per KIND, so two concurrent runs of one kind
        share a name. The still_running flag must come off each entry's own
        thread: the cooperative sibling of a stubborn run is NOT flagged."""
        events = self._capture_sel(monkeypatch)
        monkeypatch.setattr(job_sdk, "_CLEANUP_JOIN_SECS", 0.5)
        gate = threading.Event()
        stop = threading.Event()
        behavior: dict[str, str] = {}
        both_running = threading.Event()
        worker: list[threading.Thread] = []

        def runner(h, **kw):
            # list.append is atomic under the GIL, so this doubles as the
            # "both workers are up" counter.
            worker.append(threading.current_thread())
            if len(worker) >= 2:
                both_running.set()
            gate.wait(5.0)
            if behavior.get(h.run_id) == "stubborn":
                stop.wait(10.0)  # ignores the cancel signal outright
                return {}
            h.cancelled.wait()

        sdk.register("work", runner, cancellable=True)
        coop = sdk.start("work")
        stubborn = sdk.start("work")
        assert both_running.wait(5.0)
        behavior[stubborn] = "stubborn"
        gate.set()

        try:
            cleanup = asyncio.run(sdk.remove_all_async())
            assert cleanup.still_running == 1

            by_id = {e["resources"].split(" ")[0]: e["resources"] for e in self._killed(events)}
            assert by_id[coop] == f"{coop} kind=work"
            assert by_id[stubborn] == f"{stubborn} kind=work still_running"
        finally:
            stop.set()
            for t in worker:
                t.join(timeout=5.0)
                assert not t.is_alive(), "a worker outlived its test"

    def test_planted_terminal_disk_status_cannot_suppress_kill_line(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record files live in an app-writable directory, so a planted
        ``status=done`` body on a STILL-RUNNING run's record must not veto its
        kill line: whether a live run was killed is decided from in-memory
        state only, never from disk."""
        events = self._capture_sel(monkeypatch)
        started = threading.Event()
        release = threading.Event()
        worker: list[threading.Thread] = []

        def runner(h, **kw):
            worker.append(threading.current_thread())
            started.set()
            while not (h.cancelled.is_set() or release.is_set()):
                h.cancelled.wait(timeout=5.0)  # re-check release every 5 s

        sdk.register("work", runner, cancellable=True)
        run_id = sdk.start("work")
        assert started.wait(5.0)
        try:
            # The plant: the worker is demonstrably still running, but its
            # disk record claims DONE.
            record = sdk.get(run_id)
            assert record is not None
            record.status = DONE
            sdk.store.write(record)

            cleanup = asyncio.run(sdk.remove_all_async())
            assert cleanup.removed == 1
            assert cleanup.still_running == 0
            # The disable's cancel stopped the worker -- exactly a killed run,
            # and the forged disk status must not have silenced it.
            killed = self._killed(events)
            assert len(killed) == 1
            assert killed[0]["resources"] == f"{run_id} kind=work"
        finally:
            # In a finally so a failed assertion cannot leak a non-discarded
            # worker that would write into the removed tmp_path.
            release.set()
            for t in worker:
                t.join(timeout=5.0)
                assert not t.is_alive(), "the worker outlived its test"

    def test_rogue_record_claiming_live_id_cannot_suppress_kill_line(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record FILE whose body claims a live run's id with terminal status
        must not shadow the live run. Live statuses are read by canonical
        filename (``store.read``), never from an id-keyed index over
        ``iter_runs`` -- an index keyed on body ids would let the lying file
        overwrite the live record's entry and suppress its kill line."""
        events = self._capture_sel(monkeypatch)
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            h.cancelled.wait()

        sdk.register("work", runner, cancellable=True)
        run_id = sdk.start("work")
        assert started.wait(5.0)

        rogue = JobRun(run_id=run_id, app="test-app", kind="work", status=DONE)
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        (sdk.store.dir / ("d" * 32 + ".json")).write_text(json.dumps(rogue.to_dict(), indent=1))

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup.removed == 2  # the real record and the rogue file
        killed = self._killed(events)
        # Exactly one line: the live run, unsuppressed. The rogue file gets no
        # stale line either -- its claimed id belongs to a live run.
        assert [e["resources"] for e in killed] == [f"{run_id} kind=work"]

    def test_worker_done_before_cancel_is_not_reported_killed(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that computed DONE before the disable's cancel landed, but
        whose terminal write the discard guard then refused, has already
        audited its own completion -- it must NOT also get a kill line. The
        terminal write is held until the disable marks the handle discarded
        (the exact interleaving under test), then the worker finishes promptly
        so the bounded join observes it cooperative, not stubborn."""
        events = self._capture_sel(monkeypatch)
        started = threading.Event()
        inside_write = threading.Event()
        worker: list[threading.Thread] = []

        def runner(h, **kw):
            worker.append(threading.current_thread())
            started.set()
            return {}  # completes on its own; never sees the cancel signal

        # Patch BEFORE starting the run: the worker heads for the terminal
        # write as soon as the runner returns, so a later patch could race it.
        orig_write = sdk._write_terminal

        def held_write(run, handle):
            inside_write.set()
            # DONE is already assigned in memory here, and no cancel was seen.
            # Wait for the disable to mark the handle discarded, then proceed:
            # the write is refused and the worker exits inside the join.
            handle.discarded.wait(10.0)
            orig_write(run, handle)

        monkeypatch.setattr(sdk, "_write_terminal", held_write)
        sdk.register("work", runner, cancellable=True)
        sdk.start("work")
        assert started.wait(5.0)
        assert inside_write.wait(5.0)

        try:
            cleanup = asyncio.run(sdk.remove_all_async())
            assert cleanup.removed == 1
            assert cleanup.still_running == 0  # it exited inside the join
            killed = self._killed(events)
            assert killed == []  # in-memory DONE: completion already audited
        finally:
            for t in worker:
                t.join(timeout=5.0)
                assert not t.is_alive(), "the held worker outlived its test"

    def test_sel_failure_refuses_deletion_until_trace_is_durable(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kill trace is fail-closed: if SEL cannot durably record which
        runs are being deleted, the records are NOT deleted -- the disable
        reports a failed cleanup, and a retry after SEL recovers emits the
        survivors as ``stale`` lines and only then removes them."""
        stale = JobRun(
            run_id="e" * 32,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        sdk.store.write(stale)

        def broken():
            raise RuntimeError("sel down")

        monkeypatch.setattr(job_sdk, "sel", broken)
        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup.removed == 0
        assert cleanup.failed == 1
        assert not cleanup.is_clean
        assert sdk.store.read(stale.run_id) is not None  # identity preserved

        events = self._capture_sel(monkeypatch)  # SEL recovers
        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup.removed == 1
        killed = self._killed(events)
        assert len(killed) == 1
        assert killed[0]["resources"].endswith("stale")

    def test_stubborn_worker_that_fails_after_deadline_keeps_its_kill_line(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker recorded stubborn at its join deadline can raise AFTERWARDS,
        flipping its in-memory status to FAILED. The deadline observation must
        win: the summary already counted it stubborn, so suppressing its line
        would leave a stubborn count no line identifies."""
        events = self._capture_sel(monkeypatch)
        monkeypatch.setattr(job_sdk, "_CLEANUP_JOIN_SECS", 0.2)
        started = threading.Event()
        stop = threading.Event()
        worker: list[threading.Thread] = []

        def runner(h, **kw):
            worker.append(threading.current_thread())
            started.set()
            stop.wait(10.0)  # ignores the cancel signal through the deadline
            raise RuntimeError("boom after the deadline")

        sdk.register("stubborn", runner, cancellable=True)
        run_id = sdk.start("stubborn")
        assert started.wait(5.0)

        orig_read = sdk.store.read

        def releasing_read(run_id):
            # Runs inside the record scan -- after the join deadline recorded
            # the worker stubborn, before the kill-line loop reads its status:
            # release the worker and wait for it to raise and settle FAILED.
            stop.set()
            for t in worker:
                t.join(timeout=5.0)
            return orig_read(run_id)

        monkeypatch.setattr(sdk.store, "read", releasing_read)

        try:
            cleanup = asyncio.run(sdk.remove_all_async())
            assert cleanup.still_running == 1
            killed = self._killed(events)
            assert len(killed) == 1
            assert killed[0]["resources"] == f"{run_id} kind=stubborn still_running"
        finally:
            stop.set()
            for t in worker:
                t.join(timeout=5.0)
                assert not t.is_alive(), "the stubborn worker outlived its test"

    def test_unreadable_record_is_traced_by_filename_before_deletion(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record file the store cannot parse is still deleted by
        ``remove_all`` -- so its FILENAME must leave a trace line: an identity
        must not vanish just because its record went corrupt."""
        events = self._capture_sel(monkeypatch)
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        corrupt_stem = "f" * 32
        (sdk.store.dir / f"{corrupt_stem}.json").write_text("{not valid json")

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup.removed == 1

        killed = self._killed(events)
        assert len(killed) == 1
        assert killed[0]["resources"] == f"{corrupt_stem!r} unreadable stale"

    def test_run_claimed_but_never_started_is_marked_never_started(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disable can snapshot a run whose thread has not started yet. Its
        record is deleted like the rest, so it MUST get a line -- but no worker
        existed, so the line says ``never_started`` rather than implying a
        kill or a still-running worker."""
        events = self._capture_sel(monkeypatch)
        claimed = threading.Event()
        release = threading.Event()
        orig_persist = sdk._persist

        def held_persist(*args, **kwargs):
            # Hold the starter in the claim->launch window. The initial persist
            # runs AFTER the claim landed in ``_live`` and BEFORE the guarded
            # launch section, and -- unlike ``thread.start``, which runs inside
            # the SDK lock -- it holds no lock, so the disable can proceed and
            # deterministically win the race the old gate-on-Thread.start
            # version only won by timing luck.
            result = orig_persist(*args, **kwargs)
            claimed.set()
            release.wait(10.0)
            return result

        monkeypatch.setattr(sdk, "_persist", held_persist)
        sdk.register("work", lambda h, **kw: {}, cancellable=True)

        def racing_start():
            try:
                sdk.start("work")
            except JobError:
                pass  # the disable won the race and refused the launch -- expected

        starter = threading.Thread(target=racing_start)
        starter.start()
        # The claim (and the record) landed; thread.start has not been reached.
        assert claimed.wait(5.0)

        try:
            cleanup = asyncio.run(sdk.remove_all_async())
            assert cleanup.still_running == 0
            killed = self._killed(events)
            assert len(killed) == 1
            assert killed[0]["resources"].endswith(" never_started")
            assert "still_running" not in killed[0]["resources"]
        finally:
            release.set()
            starter.join(timeout=5.0)
            assert not starter.is_alive(), "the starter thread outlived its test"

    @requires_symlinks
    def test_symlink_record_is_refused_unopened_and_traced_unreadable(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The disable scan walks agent-writable filenames, so a planted
        symlink ``<id>.json`` must be refused WITHOUT following it (a link at
        /dev/zero would hang the read and make disable unreachable) -- and its
        filename still leaves an ``unreadable`` line before deletion."""
        events = self._capture_sel(monkeypatch)
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        target = sdk.store.dir / "target.txt"
        target.write_text("not a record")
        link_stem = "a1" * 16
        (sdk.store.dir / f"{link_stem}.json").symlink_to(target)

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup.removed == 1  # the link itself is unlinked

        killed = self._killed(events)
        assert len(killed) == 1
        assert killed[0]["resources"] == f"{link_stem!r} unreadable stale"

    def test_permission_denied_record_is_classified_unreadable_not_fatal(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable REGULAR record (permission denied, an exhausted
        Windows sharing retry) must not abort the disable: the refused read
        classifies the record unreadable, deletion proceeds, and the filename
        still leaves its line."""
        events = self._capture_sel(monkeypatch)
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        denied_stem = "d" * 32
        (sdk.store.dir / f"{denied_stem}.json").write_text("{}")

        from kiro_crew import platform_compat as _pc

        real_no_reparse = _pc.open_file_no_reparse

        # The refused read is simulated at the seam the record read actually uses.
        # Patching os.open would only cover the POSIX arm of open_file_no_reparse --
        # its Windows arm reaches CreateFileW -- and the scenario in the docstring
        # is a Windows one, so the record would read fine there and be classified
        # by its contents instead of as unreadable.
        def denying_open(path, *args, **kwargs):
            if str(path).endswith(f"{denied_stem}.json"):
                raise PermissionError(13, "denied", str(path))
            return real_no_reparse(path, *args, **kwargs)

        monkeypatch.setattr(_pc, "open_file_no_reparse", denying_open)

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup.removed == 1

        killed = self._killed(events)
        assert len(killed) == 1
        assert killed[0]["resources"] == f"{denied_stem!r} unreadable stale"

    def test_long_kind_cannot_push_the_marker_off_the_audit_line(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_audit`` truncates long lines from the right, so an oversized
        kind must be clipped BEFORE the marker lands -- otherwise the summary
        counts a stubborn run that no line marks ``still_running``."""
        events = self._capture_sel(monkeypatch)
        monkeypatch.setattr(job_sdk, "_CLEANUP_JOIN_SECS", 0.2)
        long_kind = "k" * 300
        started = threading.Event()
        stop = threading.Event()
        worker: list[threading.Thread] = []

        def runner(h, **kw):
            worker.append(threading.current_thread())
            started.set()
            stop.wait(10.0)
            return {}

        sdk.register(long_kind, runner, cancellable=True)
        run_id = sdk.start(long_kind)
        assert started.wait(5.0)

        try:
            cleanup = asyncio.run(sdk.remove_all_async())
            assert cleanup.still_running == 1

            killed = self._killed(events)
            assert len(killed) == 1
            assert killed[0]["resources"] == f"{run_id} kind={'k' * 32} still_running"
        finally:
            stop.set()
            for t in worker:
                t.join(timeout=5.0)
                assert not t.is_alive(), "the stubborn worker outlived its test"

    def test_stale_nonterminal_record_is_traced_and_terminal_is_not(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-terminal record with no live worker (a foreign process's run
        that never got reconciled) vanishes in the same delete, so it gets the
        same line marked ``stale``. A terminal record finished on its own --
        nothing was killed, so it must NOT be traced."""
        events = self._capture_sel(monkeypatch)
        stale = JobRun(
            run_id="b" * 32,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        finished = JobRun(
            run_id="c" * 32,
            app="test-app",
            kind="work",
            status=DONE,
            origin="foreign-origin-token",
        )
        sdk.store.write(stale)
        sdk.store.write(finished)

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup.removed == 2

        killed = self._killed(events)
        assert len(killed) == 1
        # `kind` comes off disk, so it is redacted, bounded, and repr-quoted.
        assert killed[0]["resources"] == f"{stale.run_id} kind='work' stale"


# ---------------------------------------------------------------------------
# 12. Process-wide registry
# ---------------------------------------------------------------------------


class TestProcessRegistry:
    def test_register_get_forget_registered_apps(self, tmp_path: Path) -> None:
        a = JobSDK("app-a", tmp_path / "a")
        b = JobSDK("app-b", tmp_path / "b")
        try:
            register_sdk(a)
            register_sdk(b)
            assert get_sdk("app-a") is a
            assert get_sdk("app-b") is b
            assert "app-a" in registered_apps()
            assert "app-b" in registered_apps()
            forget_sdk("app-a")
            assert get_sdk("app-a") is None
            assert "app-a" not in registered_apps()
        finally:
            forget_sdk("app-a")
            forget_sdk("app-b")

    def test_get_sdk_unknown_returns_none(self) -> None:
        assert get_sdk("never-registered-app") is None

    def test_reconcile_all_sums_across_sdks_and_survives_raise(self, tmp_path: Path) -> None:
        good = JobSDK("good-app", tmp_path / "good")
        bad = JobSDK("bad-app", tmp_path / "bad")

        # good has one foreign non-terminal record to flip.
        good.store.dir.mkdir(parents=True, exist_ok=True)
        run = JobRun(
            run_id="e" * 32,
            app="good-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        (good.store.dir / f"{run.run_id}.json").write_text(json.dumps(run.to_dict(), indent=1))

        # bad raises from reconcile — reconcile_all must survive it.
        def boom() -> int:
            raise RuntimeError("store is broken")

        bad.reconcile = boom  # type: ignore[method-assign]

        try:
            register_sdk(good)
            register_sdk(bad)
            total = reconcile_all()
            assert total == 1  # good's one flip; bad's raise swallowed
        finally:
            forget_sdk("good-app")
            forget_sdk("bad-app")
