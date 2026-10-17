"""Moving a stale registry checkout aside, keeping it, and sweeping it.

``_move_checkout_aside`` refreshes the retention clock before the rename (a failed
refresh fails closed with the checkout still in place), survives cancellation without
stranding the checkout, and names any aside it cannot move back; a successful install
keeps the moved-aside checkout, and the retained-at report names only a checkout that
stays on disk; the aged sweep removes only expired ``.stale-*`` / ``.partial-*``
children of ``app-sources/``.

``test_external_registry.py`` holds the index cache, fetch and lookup tests and names
every sibling; shared fixtures live in ``external_registry_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401

# Bound every gated-executor worker wait in this module. A test that gates a
# move-aside worker on a threading.Event and fails before signalling the
# release would otherwise leave that worker blocked against tmp_path past
# teardown (hanging xdist worker shutdown or mutating a directory a later test
# already deleted). Generous enough never to false-fire on a loaded shared
# runner.
_WORKER_RELEASE_TIMEOUT = 30.0


async def _bounded_off_loop(loop: asyncio.AbstractEventLoop, event: threading.Event) -> None:
    """Await *event* off the event loop, bounded by ``_WORKER_RELEASE_TIMEOUT``.

    Waiting for a worker's signal on a bare ``threading.Event`` via
    ``run_in_executor`` blocks an executor thread; if the worker never signals
    (an assertion failed before it ran), an unbounded wait would hang the test
    process. The bound turns that into a fast, loud failure instead.
    """
    signalled = await loop.run_in_executor(None, event.wait, _WORKER_RELEASE_TIMEOUT)
    assert signalled, "worker never signalled within the release timeout"


class TestMoveCheckoutAsideCancellationSafety:
    """Regression: GPT 5.6 round-5 finding — a cancellation delivered while
    awaiting ``_move_checkout_aside``'s rename+mtime-refresh must not leave
    the moved-aside checkout silently unaccounted for.

    The old implementation ran the rename and the mtime refresh as TWO
    separate ``asyncio.to_thread`` calls; a cancellation landing between them
    left ``aside`` on disk with the checkout's ORIGINAL mtime — immediately
    eligible for the age-based sweep — and unrecorded, since the assignment
    ``moved_aside = await _move_checkout_aside(...)`` never completed. Both
    calls now run as one thread function (no mtime gap), and a cancellation
    that arrives after that call already completed synchronously undoes the
    rename so the caller's state is unchanged by the attempt.
    """

    @pytest.mark.asyncio
    async def test_cancellation_after_completed_rename_restores_checkout(self, tmp_path):
        """A deterministic, event-gated fake: the rename+refresh is allowed
        to run to completion, a threading.Event confirms that on-disk state,
        and only THEN is the awaiting task cancelled -- never a wall-clock
        sleep racing the interleave."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        rename_completed = threading.Event()
        release_thread = threading.Event()
        real_impl = reg._rename_and_refresh_mtime

        def _gated_impl(d, a):
            # Perform the real rename+utime, signal the test that it landed,
            # then hold the worker thread (the underlying concurrent.futures
            # future stays NOT DONE) until the test has issued the
            # cancellation -- this is what pins the interleave to "cancel
            # arrives strictly after the rename, strictly before the thread
            # call reports its result" instead of leaving it to a race. The
            # wait is BOUNDED: an early test failure that never signals must
            # not leave this worker blocked against tmp_path past teardown.
            real_impl(d, a)
            rename_completed.set()
            assert release_thread.wait(_WORKER_RELEASE_TIMEOUT), "worker was never released"

        log_lines: list[str] = []
        loop = asyncio.get_event_loop()
        with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_gated_impl):
            try:
                task = asyncio.ensure_future(reg._move_checkout_aside(dest, log_lines))
                # Block off-loop until the worker thread's rename+utime has
                # actually landed on disk -- deterministic, no wall-clock sleep.
                await _bounded_off_loop(loop, rename_completed)
                # The underlying thread call is still blocked (has not reported
                # its result yet), so this cancellation is guaranteed to be
                # observed before the coroutine would otherwise receive the
                # completed rename's result.
                task.cancel()
                release_thread.set()
                with pytest.raises(asyncio.CancelledError):
                    # flake-ok: pre-existing await of the cancelled task, whose worker the release_thread.set() just before it releases and whose wait _WORKER_RELEASE_TIMEOUT bounds, moved verbatim; tracked for a de-flake pass
                    await task
            finally:
                # Release the worker no matter how the body exits, so a failed
                # assertion above can never strand the executor thread.
                release_thread.set()

        # The rename had already landed when cancellation arrived, so the
        # helper must have undone it: dest is back with its original
        # contents, and no `.stale-*` sibling is left stranded.
        assert dest.exists()
        assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"
        stranded = list(tmp_path.glob("testapp.stale-*"))
        assert stranded == [], f"expected no stranded aside directory, found {stranded}"

    @pytest.mark.asyncio
    async def test_successful_move_aside_has_no_mtime_gap(self, tmp_path):
        """Control: an un-cancelled move-aside still renames and refreshes
        the mtime, and does so without the window a separate utime call used
        to leave open — the aside's mtime must be fresh (close to now), not
        the checkout's original, possibly-old, mtime."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        old_time = time.time() - 3600
        os.utime(dest, (old_time, old_time))

        log_lines: list[str] = []
        aside = await reg._move_checkout_aside(dest, log_lines)

        assert aside is not None
        assert not dest.exists()
        assert aside.exists()
        # flake-ok: pre-existing 60 s freshness window on the refreshed mtime, moved verbatim; tracked for a de-flake pass
        assert abs(aside.stat().st_mtime - time.time()) < 60, (
            "the aside's mtime must be refreshed to now, not inherited from "
            "the checkout's original (possibly stale) mtime"
        )

    @pytest.mark.asyncio
    async def test_cancellation_before_rename_settles_worker_and_does_not_strand(self, tmp_path):
        """Round-6 regression (GPT 5.6): a cancellation delivered while the
        move-aside worker is dispatched but has NOT yet renamed must still end
        with the checkout accounted for. Cancelling the awaiting task does not
        cancel the executor thread -- it runs on -- so a bare
        ``if aside.exists()`` check races the in-thread rename: a worker past
        dispatch but pre-rename at check time would complete the rename after
        the handler re-raised, stranding the checkout at ``aside`` unrecorded.
        The handler now settles the worker future BEFORE inspecting ``aside``,
        so a rename that lands after the cancellation is undone (or the
        retained path is logged) -- never silently stranded."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        worker_started = threading.Event()
        release_thread = threading.Event()
        rename_done = threading.Event()
        real_impl = reg._rename_and_refresh_mtime

        def _gated_impl(d, a):
            # Signal that the worker is running, then hold it BEFORE the
            # rename -- so at cancellation time ``aside`` does not yet exist.
            # Only after the test releases the thread does the rename land,
            # reproducing the "worker renames after the cancel" interleave. The
            # wait is BOUNDED so an early failure cannot strand the worker past
            # teardown.
            worker_started.set()
            assert release_thread.wait(_WORKER_RELEASE_TIMEOUT), "worker was never released"
            real_impl(d, a)
            rename_done.set()

        log_lines: list[str] = []
        loop = asyncio.get_event_loop()
        with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_gated_impl):
            try:
                task = asyncio.ensure_future(reg._move_checkout_aside(dest, log_lines))
                # Worker is dispatched and blocked strictly BEFORE the rename.
                await _bounded_off_loop(loop, worker_started)
                task.cancel()
                # Let the handler resume and reach its settle-await; a bare
                # check-then-race shape would instead re-raise here without ever
                # waiting for the worker.
                # flake-ok: pre-existing zero-length yield that lets the cancelled task run into its cancellation handler, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0)
                # Now let the worker perform the rename -- AFTER the cancellation.
                release_thread.set()
                with pytest.raises(asyncio.CancelledError):
                    # flake-ok: pre-existing await of the cancelled task, whose worker the release_thread.set() just before it releases and whose wait _WORKER_RELEASE_TIMEOUT bounds, moved verbatim; tracked for a de-flake pass
                    await task
                # Confirm the worker's rename actually landed on disk.
                assert await loop.run_in_executor(
                    None, lambda: rename_done.wait(_WORKER_RELEASE_TIMEOUT)
                )
            finally:
                # Release the worker no matter how the body exits.
                release_thread.set()

        stranded = list(tmp_path.glob("testapp.stale-*"))
        if stranded:
            # If the undo could not run, the retained path MUST be logged so
            # it is never silently unaccounted for.
            assert any(
                "retained at" in line for line in log_lines
            ), f"a stranded aside {stranded} must be logged, not silent"
        else:
            assert dest.exists()
            assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"

    @pytest.mark.asyncio
    async def test_repeated_cancellation_still_settles_and_undoes_or_logs(self, tmp_path):
        """Round-10 regression (GPT 5.6): a SECOND cancellation delivered while
        the handler is awaiting settlement must not skip the undo-or-log.

        The rename lands (aside exists), then the awaiting task is cancelled
        while the worker thread is still held, driving the handler into its
        settle-await. A second cancel is delivered there. The old handler used
        a bare ``await asyncio.wait({worker})`` that re-raised on that second
        cancel and skipped BOTH the aside->dest undo AND the retained-path log
        -- stranding the checkout at an unreported ``.stale-*`` path until the
        sweep deleted it. The handler now absorbs repeated cancels until the
        worker future is done, THEN runs the synchronous undo-or-log, THEN
        re-raises once. So this ends deterministically with either the aside
        restored to dest OR the retained path named in the log -- never a
        silent strand. Event-gated; no wall-clock sleeps."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        rename_completed = threading.Event()
        release_thread = threading.Event()
        real_impl = reg._rename_and_refresh_mtime

        def _gated_impl(d, a):
            # Perform the real rename+utime so ``aside`` exists on disk, signal
            # the test, then hold the worker thread NOT DONE until the test has
            # delivered BOTH cancels -- pinning the "aside exists, worker still
            # settling, second cancel arrives" interleave the fix must survive.
            # BOUNDED so an early failure cannot strand the worker past teardown.
            real_impl(d, a)
            rename_completed.set()
            assert release_thread.wait(_WORKER_RELEASE_TIMEOUT), "worker was never released"

        log_lines: list[str] = []
        loop = asyncio.get_event_loop()
        with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_gated_impl):
            try:
                task = asyncio.ensure_future(reg._move_checkout_aside(dest, log_lines))
                # Block off-loop until the worker's rename+utime has landed on disk.
                await _bounded_off_loop(loop, rename_completed)
                # First cancel: delivered at ``await asyncio.shield(worker)``, so
                # the coroutine enters its CancelledError handler and reaches the
                # settle-await (the worker future is still NOT DONE -- held below).
                task.cancel()
                # flake-ok: pre-existing zero-length yield that lets the cancelled task run into its cancellation handler, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0)
                # Second cancel: delivered while the handler is parked on its
                # settle-await. The fix must absorb this and keep settling; the
                # old bare wait re-raised here and skipped the undo/log.
                task.cancel()
                # flake-ok: pre-existing zero-length yield that lets the cancelled task run into its cancellation handler, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0)
                # Now let the worker finish so the future settles.
                release_thread.set()
                with pytest.raises(asyncio.CancelledError):
                    # flake-ok: pre-existing await of the cancelled task, whose worker the release_thread.set() just before it releases and whose wait _WORKER_RELEASE_TIMEOUT bounds, moved verbatim; tracked for a de-flake pass
                    await task
            finally:
                # Release the worker no matter how the body exits.
                release_thread.set()

        # Repeated cancellation must NOT have stranded the checkout silently.
        stranded = list(tmp_path.glob("testapp.stale-*"))
        if stranded:
            assert any("retained at" in line for line in log_lines), (
                f"a stranded aside {stranded} must be logged after repeated "
                f"cancellation, not silently swept"
            )
        else:
            # The undo ran: the checkout is back in place, unchanged.
            assert dest.exists()
            assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"

    @pytest.mark.asyncio
    async def test_cancel_with_failed_undo_logs_retained_path_durably(self, tmp_path, caplog):
        """Round-14 regression (GPT 5.6, BLOCKING): a cancellation whose undo
        FAILS must record the retained ``.stale-*`` path in a DURABLE sink
        (``logger.warning``), not only in the request-local ``log_lines``.

        A gateway shutdown cancels the update after the move-aside rename landed,
        then the aside->dest undo fails. The request never returns, so its
        ``log_lines`` are discarded — a list-only report would leave the
        age-based sweep to later delete the unnamed recovery checkout. The fix
        emits ``logger.warning`` with the exact retained path before re-raising,
        so the path survives the shutdown in the process log. This asserts the
        durable record specifically, not just the (also-discarded) log line."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        rename_completed = threading.Event()
        release_thread = threading.Event()
        real_impl = reg._rename_and_refresh_mtime

        def _gated_impl(d, a):
            # Land the real rename+utime so ``aside`` exists, signal the test,
            # then hold the worker NOT DONE until the test delivers the cancel.
            real_impl(d, a)
            rename_completed.set()
            assert release_thread.wait(_WORKER_RELEASE_TIMEOUT), "worker was never released"

        real_rename = Path.rename

        def _undo_fails(self, target, *args, **kwargs):
            # Fail ONLY the aside->dest undo (target is the original dest); the
            # dest->aside move-aside already ran inside the worker via real_impl.
            if Path(target) == dest:
                raise OSError("simulated undo failure")
            return real_rename(self, target, *args, **kwargs)

        log_lines: list[str] = []
        loop = asyncio.get_event_loop()
        with caplog.at_level(logging.WARNING, logger=reg.logger.name):
            with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_gated_impl):
                with patch.object(Path, "rename", _undo_fails):
                    try:
                        task = asyncio.ensure_future(reg._move_checkout_aside(dest, log_lines))
                        await _bounded_off_loop(loop, rename_completed)
                        task.cancel()
                        # flake-ok: pre-existing zero-length yield that lets the cancelled task run into its cancellation handler, moved verbatim; tracked for a de-flake pass
                        await asyncio.sleep(0)
                        release_thread.set()
                        with pytest.raises(asyncio.CancelledError):
                            # flake-ok: pre-existing await of the cancelled task, whose worker the release_thread.set() just before it releases and whose wait _WORKER_RELEASE_TIMEOUT bounds, moved verbatim; tracked for a de-flake pass
                            await task
                    finally:
                        release_thread.set()

        # The checkout is genuinely stranded at a .stale-* aside (the undo failed).
        stranded = list(tmp_path.glob("testapp.stale-*"))
        assert stranded, "the undo was supposed to fail, leaving a stranded aside"
        aside_name = stranded[0].name
        # The DURABLE record names the exact retained path. This is the point of
        # the fix: caplog captures logger output that outlives the discarded
        # log_lines, so an incident responder can find the recovery copy.
        durable = [
            rec.getMessage()
            for rec in caplog.records
            if "retained at" in rec.getMessage() and aside_name in rec.getMessage()
        ]
        assert durable, (
            "the retained .stale-* path must be logged durably (logger.warning), "
            f"not only into the request-local log_lines; caplog had: "
            f"{[r.getMessage() for r in caplog.records]}"
        )


class TestMoveAsideRefreshesMtimeBeforeRename:
    """Round-14 regression (GPT 5.6): the move-aside must refresh the retention
    clock BEFORE the rename, so the moved-aside dir never appears under its
    sweep-recognized ``.stale-*`` name carrying a stale (possibly expired) mtime.

    The pre-fix ordering renamed first, then ran ``os.utime`` on the aside. For
    a checkout already older than the retention window, that leaves the aside
    visible under the sweepable name with an expired clock in the window between
    the two syscalls — a CONCURRENT install running the age-based sweep in that
    window deletes the user's recovery copy. Refreshing ``dest`` before the
    rename closes the window: the dir is already fresh the instant it becomes
    observable under the ``.stale-*`` name.
    """

    @pytest.mark.asyncio
    async def test_refresh_precedes_rename_in_invocation_trace(self, tmp_path):
        """The mtime refresh (``os.utime`` on ``dest``) is recorded strictly
        before ``dest`` is renamed aside. Fails against the pre-fix
        rename-then-utime ordering; passes with the refresh-then-rename fix."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        # Source is ALREADY older than the retention window — the exact
        # condition under which a stale-clock aside would be swept immediately.
        expired = time.time() - (reg._STALE_CHECKOUT_RETENTION_DAYS + 1) * 86400
        os.utime(dest, (expired, expired))

        trace: list[str] = []
        real_utime = os.utime
        real_rename = Path.rename

        def _traced_utime(path, *args, **kwargs):
            # Only the retention refresh (targeting the live dest) is of
            # interest; record it and delegate to the real implementation.
            if Path(path) == dest:
                trace.append("utime(dest)")
            return real_utime(path, *args, **kwargs)

        def _traced_rename(self, target, *args, **kwargs):
            if Path(self) == dest:
                trace.append("rename(dest)")
            return real_rename(self, target, *args, **kwargs)

        log_lines: list[str] = []
        with (
            patch.object(reg.os, "utime", side_effect=_traced_utime),
            patch.object(reg.Path, "rename", autospec=True, side_effect=_traced_rename),
        ):
            aside = await reg._move_checkout_aside(dest, log_lines)

        assert aside is not None, f"move-aside should succeed; log: {log_lines!r}"
        # The refresh must be recorded BEFORE the rename.
        assert trace == ["utime(dest)", "rename(dest)"], (
            "the retention clock must be refreshed on dest before the rename; "
            f"observed order was {trace!r}"
        )

    @pytest.mark.asyncio
    async def test_expired_source_carries_fresh_clock_the_instant_it_is_swept_eligible(
        self, tmp_path
    ):
        """On-disk proof: an already-expired checkout, once moved aside, sits at
        the ``.stale-*`` name with a FRESH mtime (well inside the retention
        window), so a concurrent age-based sweep cannot delete it. The sweep's
        own age test is applied to the moved-aside dir to make the point
        concrete."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("recovery copy", encoding="utf-8")
        expired = time.time() - (reg._STALE_CHECKOUT_RETENTION_DAYS + 1) * 86400
        os.utime(dest, (expired, expired))

        log_lines: list[str] = []
        aside = await reg._move_checkout_aside(dest, log_lines)

        assert aside is not None
        assert ".stale-" in aside.name, "the aside must use the sweep-recognized name"
        # Fresh clock: well inside the retention window, so the age-based sweep
        # (cutoff = now - retention window) would NOT delete it.
        cutoff = time.time() - reg._STALE_CHECKOUT_RETENTION_DAYS * 86400
        assert aside.stat().st_mtime >= cutoff, (
            "the moved-aside recovery copy must carry a fresh clock so a "
            "concurrent sweep cannot delete it"
        )
        # And the recovery copy's contents survive intact.
        assert (aside / "marker.txt").read_text(encoding="utf-8") == "recovery copy"


# ---------------------------------------------------------------------------
# Stale checkout retention on success + aged sweep
# ---------------------------------------------------------------------------


class TestSuccessPathRetainsStaleCheckout:
    """Regression: install success must NOT delete moved-aside checkouts.

    A successful source replacement must not permanently delete the .stale-*
    dir and lose the user's local edits when the install SUCCEEDED; the stale
    dir is retained and its path is surfaced in the install log.
    """

    @pytest.mark.asyncio
    async def test_success_retains_stale_checkout_and_logs_path(self, tmp_path):
        """A successful install from registry retains the .stale-* dir and
        names its path in the log output."""
        import kiro_crew.apps.registry as reg

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        stale_dir = app_sources / "testapp.stale-abcd1234"
        stale_dir.mkdir()
        (stale_dir / "local-edits.txt").write_text("important work", encoding="utf-8")

        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "version": "1.0.0", "resources": "app"}),
            encoding="utf-8",
        )

        async def _fake_clone_build(
            git_url, name, log_lines, *, branch="main", index_originated=False, **kwargs
        ):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
            }

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "resources": "app",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry.app_admission_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
            patch(
                "kiro_crew.apps.manager.register_external_app",
            ),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await reg.install_from_registry("testapp")

        assert result["ok"]
        # The stale directory must still exist — not deleted.
        assert stale_dir.exists(), "Expected .stale-* dir to survive a successful install"
        assert (stale_dir / "local-edits.txt").read_text() == "important work"
        # The log must name the retained path.
        assert str(stale_dir) in result.get("log", "")


class TestRetainedAtReportingSkipsRestorableStale:
    """Regression: a restorable (same-repository) move-aside must never be
    reported as "Previous checkout retained at" — the enclosing `finally`
    restores it after `_report_retained_stale_checkouts` runs, so naming its
    (now-deleted) `.stale-*` path in the log is misleading recovery guidance.

    A branch-mismatch move-aside is the SAME repository as the
    active checkout (origin already verified identical), so it is marked
    restorable and the `finally` puts it back on a post-build failure. An
    origin-mismatch move-aside is a DIFFERENT repository and is never
    restorable, so it must keep reporting retained-at and stay on disk.
    """

    @pytest.mark.asyncio
    async def test_restorable_stale_is_restored_and_not_reported_as_retained(self, tmp_path):
        """Branch-mismatch move-aside (restorable) + post-build install
        failure -> checkout restored to app_source_dir, and the returned log
        does not claim "Previous checkout retained at:" for it."""
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        (pkg_dir / "replacement.txt").write_text("freshly fetched", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                "_restorable_stale": [stale_dir],
            }

        class _NotOk:
            ok = False
            name = "testapp"
            message = None
            error = "install failed"

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_NotOk()),
            # The `finally` restores to `app_source_dir(name)`, not to a key on
            # the failure dict (see _restore_moved_aside call site).
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert (pkg_dir / "my-work.txt").read_text(encoding="utf-8") == "important", (
            "the branch-mismatch move-aside is the same repository and must be "
            "restored after the failed install"
        )
        assert not (pkg_dir / "replacement.txt").exists(), "the replacement is discarded"
        assert not stale_dir.exists(), "the restored path no longer exists as a stale sibling"
        assert "Previous checkout retained at:" not in result.get("log", ""), (
            "a restored checkout must not be reported as still retained at a "
            "now-deleted .stale-* path"
        )

    @pytest.mark.asyncio
    async def test_non_restorable_stale_still_reports_retained_and_survives(self, tmp_path):
        """Origin-mismatch move-aside (non-restorable, a different repository)
        + the same post-build install failure -> the retained-at line is
        still present and the `.stale-*` dir survives on disk untouched."""
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        (pkg_dir / "replacement.txt").write_text("freshly fetched", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                # No _restorable_stale: an origin mismatch is a different
                # repository and is never restorable.
            }

        class _NotOk:
            ok = False
            name = "testapp"
            message = None
            error = "install failed"

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_NotOk()),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert stale_dir.exists(), "an origin-mismatched checkout must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").exists()
        assert (pkg_dir / "replacement.txt").exists(), "the freshly cloned tree stays in place"
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")


class TestRetainedAtReportingOnDurableSuccess:
    """Regression: a restorable stale must still be reported as retained on a
    DURABLE SUCCESS exit, where the enclosing ``finally`` never restores it.

    GPT 5.6 round-5 finding: ``_report_retained_stale_checkouts`` filtered out
    every ``_restorable_stale`` entry unconditionally, on the assumption that
    "the enclosing finally restores it" — true only on a FAILURE exit
    (``durable_success`` stays False). On a durable success (a completed
    branch-drift or pinned reinstall), the same filtering silently dropped
    the "Previous checkout retained at" line for the one stale that is
    genuinely never restored on that path — it sits at ``.stale-*`` until
    the age-based sweep deletes it, with neither the user nor the log ever
    told about it. ``filter_restorable=False`` on the success call sites
    fixes this.
    """

    @pytest.mark.asyncio
    async def test_successful_reinstall_reports_retained_restorable_stale(self, tmp_path):
        """A durably successful kirocrew-managed install whose build carried
        a restorable stale (branch drift, same repository) must still log
        "Previous checkout retained at" for it."""
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "version": "1.0.0", "resources": "app"}),
            encoding="utf-8",
        )
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                # Restorable, but this success path's `finally` never reaches
                # the restore branch (durable_success is set True below) —
                # the stale genuinely stays at `.stale-*` and must be named.
                "_restorable_stale": [stale_dir],
            }

        class _Ok:
            ok = True
            name = "testapp"
            message = "installed"
            error = None

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "resources": "app",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_Ok()),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert result["ok"]
        assert stale_dir.exists(), "a durable success never restores the stale"
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", ""), (
            "a restorable stale must still be reported as retained on a "
            "durable-success exit, where it is genuinely never restored"
        )


class TestStaleCheckoutSweep:
    """Tests for _sweep_stale_checkouts_sync — the aged sweep mechanism."""

    def test_removes_aged_stale_dirs(self, tmp_path):
        """Dirs matching .stale-* older than retention are removed."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        # Create an old .stale-* dir (mtime 30 days ago)
        stale = sources / "myapp.stale-aabbccdd"
        stale.mkdir()
        (stale / "file.txt").write_text("old", encoding="utf-8")
        old_mtime = time.time() - (30 * 86400)
        os.utime(stale, (old_mtime, old_mtime))

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        assert "myapp.stale-aabbccdd" in removed
        assert not stale.exists()

    def test_removes_aged_partial_dirs(self, tmp_path):
        """Dirs matching .partial-* older than retention are removed."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        partial = sources / "myapp.partial-11223344"
        partial.mkdir()
        old_mtime = time.time() - (30 * 86400)
        os.utime(partial, (old_mtime, old_mtime))

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        assert "myapp.partial-11223344" in removed
        assert not partial.exists()

    def test_keeps_fresh_stale_dirs(self, tmp_path):
        """Dirs within the retention window are NOT removed."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        stale = sources / "myapp.stale-freshone1"
        stale.mkdir()
        # mtime = now (just created) — well within retention

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        assert removed == []
        assert stale.exists()

    def test_ignores_non_matching_siblings(self, tmp_path):
        """Normal app dirs and unrelated names are never touched."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        # A normal app directory
        app_dir = sources / "myapp"
        app_dir.mkdir()
        (app_dir / "app.json").write_text("{}", encoding="utf-8")
        # A dir with 'stale' in the name but not matching the pattern
        oddname = sources / "stale-notes"
        oddname.mkdir()
        # Set both old so they'd be swept IF they matched the pattern
        old_mtime = time.time() - (30 * 86400)
        os.utime(app_dir, (old_mtime, old_mtime))
        os.utime(oddname, (old_mtime, old_mtime))

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        assert removed == []
        assert app_dir.exists()
        assert oddname.exists()

    def test_symlink_outside_sources_not_followed(self, tmp_path):
        """A symlink pointing outside app-sources is NOT followed/deleted."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        # Create a target outside sources
        outside = tmp_path / "outside-precious"
        outside.mkdir()
        (outside / "secret.txt").write_text("do not delete", encoding="utf-8")
        # Create a symlink inside sources that looks like a stale checkout
        link = sources / "myapp.stale-symlink1"
        link.symlink_to(outside)
        # Make it old — os.utime(follow_symlinks=False) is unavailable on
        # Windows, so use os.lstat + os.utime on platforms that support it,
        # otherwise skip the mtime aging (the containment check rejects
        # symlinks before the age check anyway).
        old_mtime = time.time() - (30 * 86400)
        if os.utime in os.supports_follow_symlinks:
            os.utime(link, (old_mtime, old_mtime), follow_symlinks=False)
        # On Windows: the symlink resolves outside sources_dir, so the
        # containment check alone is sufficient to prove safety.

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        # The symlink target must NOT be deleted
        assert outside.exists()
        assert (outside / "secret.txt").read_text() == "do not delete"
        # The symlink itself should not appear in removed
        assert "myapp.stale-symlink1" not in removed

    def test_nonexistent_sources_dir(self, tmp_path):
        """A missing app-sources directory returns empty (no crash)."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        removed = _sweep_stale_checkouts_sync(tmp_path / "does-not-exist", time.time())
        assert removed == []

    @pytest.mark.asyncio
    async def test_async_sweep_called_at_install(self, tmp_path):
        """_sweep_stale_checkouts is called at the start of
        install_from_registry (integration coherence check)."""
        import kiro_crew.apps.registry as reg

        sweep_called = []

        async def _tracking_sweep():
            sweep_called.append(True)

        async def _fake_clone_build(
            git_url, name, log_lines, *, branch="main", index_originated=False, **kwargs
        ):
            return {"ok": False, "error": "deliberate failure for test"}

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry.app_admission_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry._sweep_stale_checkouts",
                new=_tracking_sweep,
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await reg.install_from_registry("testapp")

        # Clone failed but sweep must have been called before it.
        assert sweep_called, "Expected _sweep_stale_checkouts to be invoked"
        assert not result["ok"]

    def test_move_aside_with_mtime_refresh_survives_sweep(self, tmp_path):
        """Regression: a checkout moved aside with refreshed mtime is NOT
        immediately swept, even if the original checkout was older than the
        retention window.

        This reproduces the bug where rename() preserves the directory's mtime,
        so a 30-day-old checkout renamed to .stale-* would be sweep-eligible
        on the very next install — defeating the retention promise.
        """
        from kiro_crew.apps.registry import (
            _STALE_CHECKOUT_RETENTION_DAYS,
            _sweep_stale_checkouts_sync,
        )

        sources = tmp_path / "app-sources"
        sources.mkdir()

        # Simulate a checkout that was last modified 30 days ago.
        old_checkout = sources / "myapp"
        old_checkout.mkdir()
        (old_checkout / "user-edits.txt").write_text("precious", encoding="utf-8")
        old_time = time.time() - (30 * 86400)
        os.utime(old_checkout, (old_time, old_time))

        # Simulate the move-aside: rename preserves mtime…
        moved = sources / "myapp.stale-aabb0011"
        old_checkout.rename(moved)
        # …then the fix refreshes mtime to now.
        os.utime(moved)

        # Also create a genuinely aged stale dir (no refresh).
        genuinely_old = sources / "other.stale-cc001122"
        genuinely_old.mkdir()
        very_old = time.time() - ((_STALE_CHECKOUT_RETENTION_DAYS + 1) * 86400)
        os.utime(genuinely_old, (very_old, very_old))

        # Run sweep.
        removed = _sweep_stale_checkouts_sync(sources, time.time())

        # The refreshed dir must survive — its mtime is now < retention.
        assert moved.exists(), "Move-aside dir with refreshed mtime should NOT be swept"
        assert "myapp.stale-aabb0011" not in removed

        # The genuinely old one must still be swept.
        assert not genuinely_old.exists()
        assert "other.stale-cc001122" in removed


class TestMoveAsideUtimeFailureUndoesRename:
    """Round-11 regression (GPT 5.6): a failed mtime refresh must NOT forfeit
    stale-checkout retention.

    ``_rename_and_refresh_mtime`` refreshes the checkout's mtime FIRST, then
    renames it aside, so the moved-aside dir gets the full retention window and
    never appears under its ``.stale-*`` name carrying a stale clock. If the
    ``utime`` fails and the failure is swallowed, the checkout would end up
    aside with its original (possibly already-expired) mtime and the next
    install's age-based sweep would delete the user's recovery copy. The refresh
    is not best-effort: it fails CLOSED before anything moves — the utime error
    re-raises while ``dest`` is still at its original path, so
    ``_move_checkout_aside`` returns None and the caller fails closed with
    ``stale_clone_not_removed``. The checkout stays in place, never stranded
    with an expired clock.
    """

    @pytest.mark.asyncio
    async def test_utime_failure_undoes_the_move_aside(self, tmp_path):
        """``os.utime`` raising OSError leaves ``dest`` in place with no
        ``.stale-*`` sibling, and the move-aside returns None (fail closed).

        With the pre-rename refresh the failure fires before ``dest`` is moved,
        so there is nothing to undo — but the observable contract is unchanged:
        the checkout stays put and no aside is handed back."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("timestamps not permitted on this filesystem")

        log_lines: list[str] = []
        with patch("kiro_crew.apps.registry.os.utime", side_effect=_boom):
            aside = await reg._move_checkout_aside(dest, log_lines)

        # Fail closed: no aside path handed back.
        assert aside is None
        # The refresh failed before the rename, so dest never moved: it is
        # still in place with its original contents.
        assert dest.exists()
        assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"
        # No stranded .stale-* sibling holding an expired clock.
        stranded = list(tmp_path.glob("testapp.stale-*"))
        assert stranded == [], f"a .stale-* sibling was stranded: {stranded}"

    @pytest.mark.asyncio
    async def test_utime_failure_makes_install_fail_closed(self, tmp_path):
        """End to end: a branch-drift install whose ``os.utime`` fails during
        move-aside refuses with ``stale_clone_not_removed`` and leaves the old
        checkout in place, rather than moving it aside with an expired mtime."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app-sources" / "drift-app"
        (dest / ".git").mkdir(parents=True)
        # Different branch than requested → triggers the reconvergence move-aside.
        (dest / ".git" / "HEAD").write_text("ref: refs/heads/old-branch\n", encoding="utf-8")

        git_url = "https://example.com/drift-app.git"

        def _boom(*args, **kwargs):
            raise OSError("timestamps not permitted on this filesystem")

        with (
            patch("kiro_crew.apps.registry.os.utime", side_effect=_boom),
            # Origin verified identical → the origin gate does not move aside;
            # only the branch-drift reconvergence below does, which is where the
            # utime failure must fail the whole install closed.
            patch.object(reg, "_clone_origin_url", new=AsyncMock(return_value=git_url)),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=dest),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
        ):
            result = await reg._clone_build_app_locked(
                git_url,
                "drift-app",
                [],
                branch="main",
                commit="",
                pending_cleanup=[],
                restorable_stale=[],
            )

        assert result["ok"] is False
        # The machine slug lives in `code`; `error` is a human sentence the
        # install banner renders verbatim, so the raw slug must never appear
        # there (AppDetailPage renders result.error).
        assert result["code"] == "stale_clone_not_removed"
        assert result["error"] != "stale_clone_not_removed"
        assert "could not be" in result["error"]
        # The old checkout is left in place (fail closed), not stranded aside.
        assert (dest / ".git").is_dir()
        stranded = list((tmp_path / "app-sources").glob("drift-app.stale-*"))
        assert stranded == [], f"a .stale-* sibling was stranded with an expired clock: {stranded}"


class TestMoveAsideUndoFailureReportsRetainedPath:
    """Round-12b + round-14 regression (GPT 5.6): a checkout ever left stranded
    at a ``.stale-*`` aside path MUST have that exact path named in the log,
    rather than left for the age-based sweep to delete an unreported recovery
    copy.

    The mtime refresh happens BEFORE the rename (so the moved-aside dir
    never appears under its sweepable name with a stale clock), which means a
    ``utime`` failure now fails closed while ``dest`` is still at its original
    path — there is no rename to undo, nothing is stranded, and the honest
    report names ``dest`` (see :class:`TestMoveAsideUtimeFailureUndoesRename`).
    The :class:`_MoveAsideUndoFailed` retained-path contract is preserved as
    the caller's handler for any residual stranding path: when it fires, the
    exact ``aside`` path it carries is what the log names.
    """

    @pytest.mark.asyncio
    async def test_move_aside_undo_failed_handler_names_retained_aside(self, tmp_path):
        """The caller's ``_MoveAsideUndoFailed`` handler names the exact
        stranded ``.stale-*`` path. Drive the contract directly: the worker
        raises ``_MoveAsideUndoFailed`` carrying an on-disk aside (the residual
        strand shape), and ``_move_checkout_aside`` must fail closed (return
        None) and log a "Previous checkout retained at: <aside>" line naming
        that exact path — never a generic dest-only line that would leave the
        strand unreported."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        # A stranded aside actually present on disk, as the contract describes.
        aside = dest.with_name("testapp.stale-deadbeef")
        aside.mkdir()
        (aside / "marker.txt").write_text("original", encoding="utf-8")

        def _raise_undo_failed(d, a):
            raise reg._MoveAsideUndoFailed(aside, OSError("undo could not land"))

        log_lines: list[str] = []
        with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_raise_undo_failed):
            result = await reg._move_checkout_aside(dest, log_lines)

        # Failed closed.
        assert result is None
        # The exact retained path is named, not swept unreported.
        retained_lines = [ln for ln in log_lines if "Previous checkout retained at" in ln]
        assert retained_lines, f"the retained aside must be named; got {log_lines!r}"
        assert str(aside) in retained_lines[0], "the log must name the true on-disk aside path"

    @pytest.mark.asyncio
    async def test_utime_failure_before_rename_names_dest_not_retained(self, tmp_path):
        """Round-14 ordering: ``utime`` runs on ``dest`` BEFORE the rename, so a
        failure fires while the checkout is still at ``dest`` — nothing is moved,
        nothing is stranded, and the honest report is the generic
        "Could not move aside ... at <dest>" line with no "retained at" line."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")
        old_time = time.time() - 3600
        os.utime(dest, (old_time, old_time))

        real_utime = os.utime

        def _fake_utime(path, *args, **kwargs):
            # The refresh now targets dest (pre-rename); fail it there.
            if Path(path) == dest:
                raise OSError("simulated utime failure")
            return real_utime(path, *args, **kwargs)

        log_lines: list[str] = []
        with patch.object(reg.os, "utime", side_effect=_fake_utime):
            aside = await reg._move_checkout_aside(dest, log_lines)

        assert aside is None
        # dest is still in place with its original contents; nothing stranded.
        assert dest.exists()
        assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob("testapp.stale-*")) == []
        assert any("Could not move aside" in ln for ln in log_lines)
        assert not any(
            "retained at" in ln for ln in log_lines
        ), "a fail-closed pre-rename refresh strands nothing, so no retained line is owed"
