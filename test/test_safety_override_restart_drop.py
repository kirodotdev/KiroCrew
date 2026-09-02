"""A safety-override grant dropped by a restart must not vanish silently.

Grants live in memory, so a restart ends them -- that is the design and these
tests do not challenge it. What they pin is the NOTICE: a timed grant that still
had time left when the process went down is reported to the operator once, and
the three cases that owe no notice stay quiet.

The whole surface is a pure function of a small file plus the wall clock, so no
restart is simulated: writing the record and then reading it back through
``take_dropped_grant`` IS the restart, from the reader's point of view.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import safety_override as so


@pytest.fixture(autouse=True)
def _isolated_breadcrumb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate the record, and run publishes INLINE.

    Two separate concerns. The path is redirected so no test touches the real
    data home. And ``_enqueue_breadcrumb`` is replaced with direct execution so
    the tests never start the daemon worker: a queued publish would otherwise
    leave a thread running past the test that started it (found in review), and
    inline execution also makes every assertion on the file deterministic instead
    of waiting on a worker.

    Production keeps the worker -- see ``_sync_breadcrumb``; the callers there sit
    on the gateway's event loop.
    """
    monkeypatch.setattr(so, "_breadcrumb_path", lambda: tmp_path / "last_grant.json")
    monkeypatch.setattr(so, "_enqueue_breadcrumb", lambda job: job())
    so.reset_singleton()
    yield tmp_path / "last_grant.json"
    so.reset_singleton()


def _record(path: Path) -> dict:
    """Read the published record."""
    return json.loads(path.read_text(encoding="utf-8"))


def _published(path: Path) -> bool:
    return path.exists()


def _take() -> object:
    return so.take_dropped_grant()


def _write(path: Path, *, source: str, offset_secs: float, permanent: bool = False) -> None:
    """Write a record whose deadline is *offset_secs* from now (negative = past)."""
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": source,
                "expires_at": datetime.now(tz=timezone.utc).timestamp() + offset_secs,
                "permanent": permanent,
            }
        ),
        encoding="utf-8",
    )


class TestTheNoticeIsOwed:
    def test_a_timed_grant_with_time_left_is_reported(self, _isolated_breadcrumb: Path) -> None:
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=3600)
        dropped = so.take_dropped_grant()
        assert dropped is not None
        assert dropped.source == "dashboard"
        # Rounded down by the elapsed microseconds, so allow the boundary.
        assert 3500 <= dropped.remaining_secs <= 3600

    def test_the_notice_names_the_source_and_what_was_lost(self) -> None:
        text = so.describe_dropped_grant(so.DroppedGrant(source="slack", remaining_secs=7200))
        # The operator has to learn three things: it is OFF, how much they lost,
        # and that this is what a restart does -- not a malfunction.
        assert "OFF" in text
        assert "2h" in text
        assert "slack" in text
        assert "restart" in text.lower()

    def test_the_drop_is_audited(self, _isolated_breadcrumb: Path, monkeypatch) -> None:
        calls: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(so, "sel", lambda: _Sel())
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=600)
        assert so.take_dropped_grant() is not None
        ops = [c["operation"] for c in calls]
        assert "safety_override:dropped_by_restart" in ops

    def test_an_audit_failure_still_yields_the_notice(
        self, _isolated_breadcrumb: Path, monkeypatch
    ) -> None:
        # The notice is the point; a broken audit sink must not swallow it.
        class _Boom:
            def log_api_access(self, **kw):
                raise RuntimeError("sel down")

        monkeypatch.setattr(so, "sel", lambda: _Boom())
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=600)
        assert so.take_dropped_grant() is not None


class TestNoNoticeIsOwed:
    def test_no_record_is_silent(self) -> None:
        assert so.take_dropped_grant() is None

    def test_a_lapsed_grant_is_silent(self, _isolated_breadcrumb: Path) -> None:
        # The clock was taking it anyway, so the restart cost nothing.
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=-1)
        assert so.take_dropped_grant() is None

    def test_a_grant_with_no_expiry_is_silent(self, _isolated_breadcrumb: Path) -> None:
        # A declared grant is re-established from config on this same startup,
        # and an until_shutdown grant is already contracted as ending here.
        _write(_isolated_breadcrumb, source="config", offset_secs=99999, permanent=True)
        assert so.take_dropped_grant() is None

    def test_a_corrupt_record_is_silent_and_removed(self, _isolated_breadcrumb: Path) -> None:
        _isolated_breadcrumb.write_text("{not json", encoding="utf-8")
        assert so.take_dropped_grant() is None
        assert not _isolated_breadcrumb.exists()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are a POSIX shape")
    def test_a_fifo_is_refused_without_hanging(self, _isolated_breadcrumb: Path) -> None:
        # The size bound alone does not cover this: a FIFO reports st_size 0, so
        # it passes a size check and then blocks forever on read, hanging gateway
        # initialization before readiness (found in review). O_NONBLOCK plus an
        # fstat shape check is what refuses it.
        os.mkfifo(_isolated_breadcrumb)
        started = time.monotonic()
        assert so.take_dropped_grant() is None
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"read waited {elapsed:.2f}s on a FIFO"

    @requires_symlinks
    def test_a_symlink_is_refused(self, _isolated_breadcrumb: Path, tmp_path: Path) -> None:
        # O_NOFOLLOW: a link planted here must not redirect the read at another
        # file, even one that would parse.
        real = tmp_path / "elsewhere.json"
        real.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "dashboard",
                    "expires_at": datetime.now(tz=timezone.utc).timestamp() + 3600,
                    "permanent": False,
                }
            ),
            encoding="utf-8",
        )
        os.symlink(real, _isolated_breadcrumb)
        assert so.take_dropped_grant() is None

    def test_an_oversized_record_is_discarded_unread(self, _isolated_breadcrumb: Path) -> None:
        # Anything that can write into the data home could otherwise leave a huge
        # file here and turn every startup into a memory-exhaustion restart loop
        # (found in review). Size is checked before the read, so the bytes are
        # never taken in, and the file is removed so it cannot do it again.
        _isolated_breadcrumb.write_text("x" * (so._BREADCRUMB_MAX_BYTES + 1), encoding="utf-8")
        assert so.take_dropped_grant() is None
        assert not _isolated_breadcrumb.exists()

    def test_a_record_at_the_size_bound_is_still_read(self, _isolated_breadcrumb: Path) -> None:
        # The bound must not reject a legitimate record: pad one with whitespace
        # up to the limit and it still produces its notice.
        payload = json.dumps(
            {
                "version": 1,
                "source": "dashboard",
                "expires_at": datetime.now(tz=timezone.utc).timestamp() + 3600,
                "permanent": False,
            }
        )
        padded = payload + " " * (so._BREADCRUMB_MAX_BYTES - len(payload))
        _isolated_breadcrumb.write_text(padded, encoding="utf-8")
        assert len(padded) == so._BREADCRUMB_MAX_BYTES
        assert so.take_dropped_grant() is not None

    def test_the_notice_fires_at_most_once(self, _isolated_breadcrumb: Path) -> None:
        # Single-shot by construction: the record is consumed, so a restart
        # cannot re-notify on every subsequent startup.
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=3600)
        assert so.take_dropped_grant() is not None
        assert not _isolated_breadcrumb.exists()
        assert so.take_dropped_grant() is None


class TestThisProcessOwnRecordIsNotADropNotice:
    """A reader in the writing process IMAGE must leave the live record alone.

    This is what frees the read from having to run before the startup grant is
    applied -- the constraint that previously forced it onto the pre-bind boot
    path. Identity is an import-time nonce rather than the pid, because both
    restart paths end in ``os.execv``, which PRESERVES the pid (and the process
    start time), so a pid check would swallow the notice on exactly the restarts
    this feature exists for (found in review).
    """

    def test_our_own_live_record_is_not_reported(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        assert _published(_isolated_breadcrumb)
        # Written by THIS image, so it is a live grant's record, not a dropped one.
        assert so.take_dropped_grant() is None

    def test_our_own_live_record_is_not_consumed(self, _isolated_breadcrumb: Path) -> None:
        # Consuming it would delete the record for a grant that is in force, so a
        # LATER restart would have nothing to report.
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        so.take_dropped_grant()
        assert _published(_isolated_breadcrumb)

    def test_a_restart_that_keeps_the_pid_still_reports(self, _isolated_breadcrumb: Path) -> None:
        # THE regression this identity exists for. `reexec_python_module` is
        # os.execv on POSIX: same pid, new process image. Simulated by writing a
        # record with this very pid but a different image token -- exactly what the
        # previous image leaves behind -- which must still be reported.
        _isolated_breadcrumb.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "dashboard",
                    "expires_at": datetime.now(tz=timezone.utc).timestamp() + 3600,
                    "permanent": False,
                    "image": "a-previous-process-image",
                    "pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        dropped = so.take_dropped_grant()
        assert dropped is not None, "an execv restart keeps the pid; the notice must survive it"
        assert dropped.source == "dashboard"
        assert not _isolated_breadcrumb.exists()

    def test_a_consume_serializes_against_a_publish(self, _isolated_breadcrumb: Path) -> None:
        # A publish landing between the consumer's open and its clear would be
        # UNLINKED by that clear, deleting the record for a grant that is live
        # right now (found in review). The consumer therefore holds the publisher's
        # lock across the whole span -- proven by holding it here and showing the
        # consumer waits rather than proceeding.
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=3600)
        finished = threading.Event()

        def _consume() -> None:
            so.take_dropped_grant()
            finished.set()

        so._breadcrumb_io_lock.acquire()
        try:
            threading.Thread(target=_consume, daemon=True).start()
            assert not finished.wait(0.25), "consumer did not wait for the publisher's lock"
        finally:
            so._breadcrumb_io_lock.release()
        assert finished.wait(5.0), "consumer never completed after the lock was released"
        assert not _isolated_breadcrumb.exists()

    def test_a_record_from_another_process_is_reported(self, _isolated_breadcrumb: Path) -> None:
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=3600)
        # _write stamps no image token, so it reads as "not this image" -- the same
        # verdict a previous gateway's record gets.
        dropped = so.take_dropped_grant()
        assert dropped is not None
        assert not _isolated_breadcrumb.exists()


class TestTheCourtesyNeverEndangersTheGrant:
    """The record is a courtesy; the grant is a decision. Failures stay separated."""

    def test_an_enqueue_failure_does_not_fail_the_activation(self, monkeypatch) -> None:
        # The grant is already committed when the publish is dispatched, so an
        # exception escaping the dispatch would report a FAILED activation while
        # tools are in fact auto-approved (found in review). Starting the worker
        # can fail on its own -- a thread quota is a real limit.
        def _boom(job):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(so, "_enqueue_breadcrumb", _boom)
        override = so.safety_override()
        override.adhoc_ttl = 3600
        assert override.activate("dashboard").active is True
        assert override.is_active() is True
        # And the reverse direction: a revocation must still complete.
        override.deactivate("dashboard")
        assert override.is_active() is False

    def test_the_flush_is_bounded_when_a_write_is_stalled(self, monkeypatch) -> None:
        # A restart path calls this. An unbounded wait would freeze a gateway
        # that was trying to re-exec, which is worse than losing the record
        # (found in review). Simulate a publish that never finishes.
        so._breadcrumb_idle.clear()
        try:
            started = time.monotonic()
            drained = so.flush_breadcrumb_writes(0.05)
            elapsed = time.monotonic() - started
            assert drained is False
            assert elapsed < 1.0, f"flush waited {elapsed:.2f}s on a stalled write"
        finally:
            so._breadcrumb_idle.set()


class TestOneRecordPerHomeIsSafe:
    """No sibling gateway can consume this record out from under its writer.

    The record is per data home, which is only safe because a home already has at
    most one gateway: ``gateway_lock`` takes an exclusive advisory flock for the
    process lifetime and REFUSES a second gateway on the same home. Pinned here
    because a reviewer read the single file as shared-gateway state, and the thing
    that makes it not shared is a guard in another module.
    """

    def test_the_record_sits_in_the_directory_the_gateway_lock_guards(self) -> None:
        from kiro_crew import gateway_lock
        from kiro_crew.config.loader import config_dir

        # Built from the module constant rather than from ``_breadcrumb_path()``,
        # which the autouse fixture redirects into tmp_path -- asserting on the
        # patched accessor would only test the fixture.
        real_record = config_dir() / so._BREADCRUMB_FILE
        # Same directory, so the lock that admits one gateway per home is the
        # same boundary that admits one writer of this record.
        assert real_record.parent == config_dir()
        assert (config_dir() / gateway_lock.LOCK_FILENAME).parent == real_record.parent

    def test_a_second_gateway_on_the_same_home_is_refused(self, tmp_path: Path) -> None:
        # The invariant itself, exercised rather than asserted from the docstring:
        # the second acquire raises instead of proceeding alongside the first.
        from kiro_crew.gateway_lock import GatewayLock, GatewayLockError

        first = GatewayLock(tmp_path).acquire()
        try:
            with pytest.raises(GatewayLockError):
                GatewayLock(tmp_path).acquire()
        finally:
            first.release()


class TestOrderingAgainstConcurrentTransitions:
    """A delayed publish must not resurrect a grant the operator revoked."""

    def test_a_late_publish_cannot_undo_a_revocation(self, _isolated_breadcrumb: Path) -> None:
        # Reproduces the interleaving directly: activation snapshots state, a
        # deactivation lands, and only THEN does the activation's publish run.
        # Because the publish derives its content from live state rather than
        # from the activation's locals, it writes the revocation's truth.
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        override.deactivate("dashboard")
        # A stale sync arriving after the revocation must clear, not write.
        override._sync_breadcrumb()
        assert not _published(_isolated_breadcrumb)
        assert _take() is None

    def test_an_older_generation_does_not_overwrite_a_newer_publish(
        self, _isolated_breadcrumb: Path
    ) -> None:
        # The generation counter is what orders two overlapping transitions: a
        # publish holding a stale generation returns without touching the file.
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        assert _published(_isolated_breadcrumb)
        override._breadcrumb_published_gen += 5  # pretend a newer publish landed
        _isolated_breadcrumb.unlink()
        override._sync_breadcrumb()
        assert not _published(_isolated_breadcrumb)


class TestTheGrantLifecycleMaintainsTheRecord:
    def test_activating_records_the_grant(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        assert _published(_isolated_breadcrumb)
        record = _record(_isolated_breadcrumb)
        assert record["source"] == "dashboard"
        assert record["permanent"] is False
        # Wall clock, because the reader is a different process: the in-memory
        # deadline is time.monotonic() and means nothing after a restart.
        assert record["expires_at"] > datetime.now(tz=timezone.utc).timestamp()

    def test_an_explicit_deactivate_leaves_no_notice(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        override.deactivate("dashboard")
        assert _take() is None

    def test_a_natural_expiry_leaves_no_notice(
        self, _isolated_breadcrumb: Path, monkeypatch
    ) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 1
        override.activate("dashboard")
        # Walk the monotonic clock past the deadline and let is_active() reap it.
        real_monotonic = so.time.monotonic
        monkeypatch.setattr(so.time, "monotonic", lambda: real_monotonic() + 10.0)
        assert override.is_active() is False
        assert _take() is None

    def test_a_renew_moves_the_recorded_deadline(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 60
        override.activate("dashboard")
        first = _record(_isolated_breadcrumb)["expires_at"]
        override.adhoc_ttl = 7200
        assert override.renew("dashboard").renewed is True
        second = _record(_isolated_breadcrumb)["expires_at"]
        # Otherwise a restart after a renewal reports the stale remaining time.
        assert second > first

    def test_a_declared_grant_records_itself_as_no_expiry(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.activate_declared()
        record = _record(_isolated_breadcrumb)
        assert record["permanent"] is True
        # And therefore owes no notice on the next startup.
        assert _take() is None

    def test_a_write_failure_never_breaks_the_grant(self, tmp_path: Path, monkeypatch) -> None:
        # A breadcrumb is a courtesy; a grant is a decision. The courtesy failing
        # must not fail the decision -- and must never fail a DEACTIVATION,
        # where raising would leave auto-approval on.
        #
        # The unwritable path stays UNDER tmp_path: atomic_write() does
        # parent.mkdir(parents=True), so a path outside the fixture's workspace
        # would be CREATED by this test and survive teardown (found in review).
        # A file standing where a directory must be makes the write fail without
        # letting anything escape tmp_path.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(so, "_breadcrumb_path", lambda: blocker / "last_grant.json")
        override = so.safety_override()
        override.adhoc_ttl = 3600
        assert override.activate("dashboard").active is True
        override.deactivate("dashboard")
        assert override.is_active() is False
