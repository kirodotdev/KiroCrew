"""Tests for the Job SDK — app-scoped durable runs.

Covers the runner registry, per-run JSON records, cooperative cancellation via
a threading.Event, the startup reconciliation pass, and the process-wide SDK
registry.

Runs execute on real daemon threads, so terminal state is awaited with a
bounded poll (``_wait_terminal``) rather than a fixed sleep — a fixed sleep is
what makes such a suite flaky on a loaded runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
    UnknownJobKind,
    forget_sdk,
    get_sdk,
    reconcile_all,
    register_sdk,
    registered_apps,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _wait_terminal(sdk: JobSDK, run_id: str, timeout: float = 5.0) -> JobRun:
    """Poll until the run reaches a terminal status, or fail with what we saw."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = sdk.get(run_id)
        if last is not None and last.is_terminal:
            return last
        time.sleep(0.01)
    observed = last.status if last is not None else "<no record>"
    raise AssertionError(f"run {run_id} not terminal within {timeout}s; observed status={observed}")


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
def sdk(tmp_path: Path):
    """A fresh SDK over a tmp data dir, unregistered from the global registry
    on teardown so it never leaks into another test in the session.

    Teardown also SIGNALS and WAITS for any run still executing. Runs are real
    daemon threads: one still alive when pytest removes ``tmp_path`` would
    mkdir and write into the deleted tree at its next progress or terminal
    write, which is a real file mutation racing the fixture. Waiting (bounded,
    and loud on timeout) is the only correct answer -- cancelling the wrapper
    would leave the thread running, which is the defect, not the fix.
    """
    s = JobSDK("test-app", tmp_path)
    yield s
    with s._lock:  # noqa: SLF001 - teardown needs the live table the SDK owns
        live = list(s._live.values())
    for entry in live:
        entry.handle.discarded.set()
        entry.handle.cancelled.set()
    for entry in live:
        entry.thread.join(timeout=5.0)
        assert not entry.thread.is_alive(), (
            "a job worker outlived its test and would write into the removed "
            f"tmp_path: {entry.thread.name}"
        )
    forget_sdk(s.app_name)


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
            "cancellable",
            "created_at",
            "updated_at",
            "finished_at",
            "error",
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
            # Poll the cancel signal cooperatively.
            for _ in range(500):
                if h.cancelled.is_set():
                    return
                time.sleep(0.01)

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
# 5b. cancelling_ids — the read side of "cancel writes nothing"
# ---------------------------------------------------------------------------


class TestCancellingIds:
    """A requested cancel must be readable before the worker records it.

    ``cancel`` writes nothing on purpose, so the request used to exist only in
    the response to the cancel call itself. These pin the derived read that makes
    it survive a fresh read of the record instead.
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
            assert sdk.cancelling_ids() == frozenset()
            assert sdk.cancel(run_id) is True
            # Requested, and readable while the RECORD still says running --
            # which is exactly what a fresh mount has to be able to see.
            assert run_id in sdk.cancelling_ids()
            record = sdk.get(run_id)
            assert record is not None and record.status == RUNNING
        finally:
            # In FINALLY: an assertion failing above must not park this worker
            # for the rest of the session.
            release.set()
        run = _wait_terminal(sdk, run_id)
        assert run.status == CANCELLED
        # The worker recorded the outcome and the live entry is gone, so the
        # status now carries the answer and nothing is pending.
        assert sdk.cancelling_ids() == frozenset()

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
            assert sdk.cancelling_ids() == frozenset()
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
# 7. JobHandle.progress and bounded line tail
# ---------------------------------------------------------------------------


def _handle_over(tmp_path: Path, run: JobRun) -> tuple[JobHandle, JobStore, JobSDK]:
    """A handle plus the REAL SDK whose guarded writer owns the discard check.

    The handle no longer carries a writer: with the progress channel gone the
    only mid-life write is the worker's terminal one, so the discard check lives
    entirely in ``JobSDK._persist``. Handing back the SDK lets a test exercise
    that real guard rather than a stand-in copy of it.
    """
    sdk = JobSDK("handle-test", tmp_path)
    return JobHandle(run), sdk.store, sdk


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
            for _ in range(500):
                if h.cancelled.is_set():
                    cancelled_seen.set()
                    return
                time.sleep(0.01)
            release.wait(5.0)
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
            for _ in range(500):
                if h.cancelled.is_set():
                    return
                time.sleep(0.01)

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
        import kiro_crew.security as security

        def boom(_text):
            raise RuntimeError("redaction backend down")

        monkeypatch.setattr(security, "redact_credentials", boom)

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
                gate.wait()
                ids.append(sdk.start("work", dedupe_key="one"))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=racer, name=f"racer-{i}") for i in range(2)]
        for t in threads:
            t.start()
        gate.wait()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, errors
        assert len(ids) == 2
        assert ids[0] == ids[1], f"dedupe let both starts win: {ids}"
        # And only one body ever ran. Waited for rather than assumed: the worker's
        # first act is now the STARTING -> RUNNING write, so a body enters strictly
        # after a lock acquisition and a small disk write. This assertion used to
        # read `entered` the instant both starts returned, which was only ever true
        # because that gap was short -- a latent timing assumption, not a property.
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
    progress and result channels this class used to cover are out of P1, so the
    surface that needs scrubbing is a single string rather than arbitrary nested
    data.
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
        monkeypatch.undo()
        run_id = sdk.start("work", dedupe_key="k")
        assert run_id
        assert _wait_terminal(sdk, run_id).status == DONE

    def test_a_transient_terminal_write_is_retried(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost terminal write leaves the record reading `running` while the
        work is finished, so one retry covers the transient case."""
        real_write = sdk.store.write
        calls: list[int] = []

        def flaky(run):
            calls.append(1)
            # Fail only the terminal write (the initial one is call 1).
            if len(calls) == 2:
                raise OSError("transient")
            return real_write(run)

        sdk.register("work", lambda h, **kw: {"ok": True})
        monkeypatch.setattr(sdk.store, "write", flaky)
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert len(calls) >= 3, f"the retry did not happen: {len(calls)} write(s)"


class TestRecordIsAlwaysWritable:
    """A runner cannot make its own record unserializable.

    `json.dumps` raises TypeError on a set, a Path, or any object, and that
    exception used to escape the terminal write and skip the live-table and
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
        second = sdk.start("weird", dedupe_key="k")
        assert second != run_id
        _wait_terminal(sdk, second)

    def test_every_field_of_a_live_record_is_json_serializable(self, sdk: JobSDK) -> None:
        """Pinned on the record's own shape, so a future field cannot slip in
        holding something ``json.dumps`` refuses."""
        sdk.register("work", lambda h: None)
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        for name, value in run.to_dict().items():
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
            while not h.cancelled.is_set():
                time.sleep(0.01)
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

    Four review rounds each found a different channel the scrub did not cover --
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
            "cancellable",
            "created_at",
            "updated_at",
            "finished_at",
        }
        fields = set(JobRun.__dataclass_fields__)
        assert fields - sdk_minted == {"error"}


class TestDisableIsTerminalForTheSDK:
    """Cleanup must close the SDK, not merely snapshot what was live.

    Marking and snapshotting used to be the whole of cleanup's critical section,
    so a ``start`` that had already read the runner table could claim AFTER the
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
        sdk.register("work", lambda h: time.sleep(30))
        asyncio.run(sdk.remove_all_async())
        for _ in range(5):
            with pytest.raises(JobError):
                sdk.start("work")
        assert not [t for t in threading.enumerate() if t.name.startswith("job:")]
        assert sdk.list_active() == []


class TestDedupeKeyIsNotLogged:
    """A caller-supplied dedupe key must not reach the gateway log.

    The adoption line used to quote it. The gateway log is durable and is served
    by ``/api/logs``, and a dedupe key is whatever the caller chose -- an account
    id, a path, or a credential -- so quoting it turned a de-duplication aid into
    an output boundary nobody had classified as one.
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
                    await asyncio.sleep(0.02)
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
        monkeypatch.undo()

        # No run is left claiming to be active, and the key is free again.
        assert sdk.list_active() == []
        again = sdk.start("work", dedupe_key="k")
        assert _wait_terminal(sdk, again).status == DONE
