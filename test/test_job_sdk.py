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
import concurrent.futures
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import requires_symlinks
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


#: Whether a 0o000 mode actually denies THIS process. Root reads straight through
#: it, so the tests that provoke a real ``PermissionError`` from the filesystem
#: have nothing to provoke and must skip rather than assert a false negative.
_MODE_BITS_BITE = hasattr(os, "geteuid") and os.geteuid() != 0


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
            # Written only by ``reconcile``, and only for a run whose process is
            # gone. Both are drawn from SDK-owned closed sets (a status constant
            # and a ``CAUSE_*`` constant), so neither is a channel a runner or a
            # caller can reach -- which is why they are here and not a result
            # field wearing a new name.
            "interrupted_from",
            "interrupt_cause",
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
# 5b. the cancelling snapshot — the read side of "cancel writes nothing"
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
        # The worker recorded the outcome and the live entry is gone.
        assert sdk.cancelling_and_live_ids() == (frozenset(), frozenset())

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
# 11a. Disable trace — one SEL line per killed run (issue #7583)
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
            for _ in range(500):
                if h.cancelled.is_set():
                    return
                time.sleep(0.01)

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
            for _ in range(500):
                if h.cancelled.is_set():
                    return
                time.sleep(0.01)

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
            for _ in range(500):
                if h.cancelled.is_set() or release.is_set():
                    return
                time.sleep(0.01)

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
            for _ in range(500):
                if h.cancelled.is_set():
                    return
                time.sleep(0.01)

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
        real_open = os.open

        def denying_open(path, flags, *args, **kwargs):
            if str(path).endswith(f"{denied_stem}.json"):
                raise PermissionError(13, "denied", str(path))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", denying_open)

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

        deadline = time.monotonic() + 5
        while sdk.get(run_id).status != RUNNING and time.monotonic() < deadline:
            time.sleep(0.01)
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
                    end = time.monotonic() + 5
                    while time.monotonic() < end:
                        latest = sdk.get(run_id)
                        if latest.is_terminal and run_id not in sdk._live:  # noqa: SLF001
                            finished.append(latest.to_dict())
                            break
                        time.sleep(0.01)
                    assert finished, "the worker did not settle in time"
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
            # Set only by ``reconcile``, from closed sets this module owns: a
            # status constant and a ``CAUSE_*`` constant. No runner and no caller
            # can reach either, so the one-line backstop still has one input.
            "interrupted_from",
            "interrupt_cause",
        }
        fields = set(JobRun.__dataclass_fields__)
        assert fields - sdk_minted == {"error"}

    def test_a_write_lost_starting_record_says_the_body_never_ran(self, sdk: JobSDK) -> None:
        """The consequence is DERIVED from the progress axis, not asserted.

        Round 1 made the `starting` -> `running` transition a precondition for calling
        the runner, so a record still at `starting` whose terminal write was lost
        proves execution never began. Saying "not known" there would be this module's
        own defect: a record declining to state what it knows.
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
        source = Path(job_sdk.__file__).read_text(encoding="utf-8")
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
        a coroutine. Two reviewers found this gap in the first version of the
        guard, which is why the check goes through ``__call__`` too.
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
    """``reconcile`` used to overwrite ``status`` and pick ``error`` from the
    runner table alone, so a run whose worker thread was never started was
    written a record claiming it "was running", and a consumer could not tell a
    run that may have committed side effects from one that provably had not.

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
        source = Path(job_sdk.__file__).read_text(encoding="utf-8")
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
        """Pins the direction #7703 depends on: an on-loop read lets it out."""
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
        """Still failed, but no longer for a reason of its own.

        Earlier rounds gave this state a distinct message ("never began") because it is
        a distinct fact. The state is no longer consulted, so it now shares the one
        suspendable verdict -- the cost of the tightening, recorded here rather than
        left for a reader to infer from a deleted assertion.
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

        The old form enumerated the language's frame states and asserted a verdict for
        each, so a future Python adding a state broke it. The rule no longer reads state
        at all, so the property to pin is that no state -- including the two CLOSED
        histories that motivated the change -- can reach an allow.
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
        """The record must warn the owner, because the SDK cannot stop the work.

        An unsettled future stands for a thread this SDK does not own. Marking the run
        terminal releases the dedupe key, so an owner who retries can start a second
        run overlapping work that never stopped. `failed` is the true verdict about the
        RUN -- the runner returned before finishing, violating its contract -- but the
        reason has to name what the SDK cannot control, since the owner reading it is
        the only party who can judge whether retrying is safe.
        """
        pool_future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            verdict = job_sdk._undriven_result(pool_future)
            assert "not settled" in verdict
            # The disclosure, pinned on the FACTS it must convey rather than wording.
            assert "may still be running" in verdict
            assert "cannot stop" in verdict
            assert "overlap" in verdict
        finally:
            pool_future.cancel()

    def test_the_states_are_reachable_and_deliberately_not_distinguished(self) -> None:
        """The states are real; the verdict ignores them ON PURPOSE.

        This test asserted the opposite until round 9 -- that all three produced
        DISTINCT verdicts, and that closed produced an allow. Both assertions are now
        inverted, because a verdict that varies by state implies the state is
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
            # Was "" (allowed) until round 9; see the inversion test for why.
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
