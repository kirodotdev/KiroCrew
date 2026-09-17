"""Fan-out edge cases: burst writes from many sessions, a hung writer starving
siblings, the per-session pending ceiling, and shutdown draining many sessions.

All synchronization uses threading.Event / threading.Barrier -- no time.sleep,
no wall-clock assertions.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import pytest

from kiro_crew.crew_log import Ledger, emit, ledger_path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pending(job, what: str) -> emit._PendingJob:
    """Wrap *job* in the record the writer's buffer holds."""
    return emit._PendingJob(job=job, what=what)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sid(n: int) -> str:
    return f"acp-fanout-{n:04d}"


def _entries(session_id: str) -> list[dict]:
    path = ledger_path("session", session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _body(session_id: str) -> list[dict]:
    return _entries(session_id)[1:]


def _open(session_id: str) -> None:
    emit.on_session_opened(
        session_id,
        agent="kirocrew",
        slot="s",
        model="m",
        cwd="/x",
        owner="default",
    )
    emit.flush(timeout=5.0)


# ---------------------------------------------------------------------------
# 1. Burst from N sessions: nothing dropped, accounting honest
# ---------------------------------------------------------------------------


class TestBurstFanout:
    """Many sessions emitting entries concurrently -- simulates N agents/workflows
    completing at once."""

    N_SESSIONS = 20
    ENTRIES_PER = 5

    def test_burst_no_drops_and_accounting_correct(self):
        sessions = [_sid(i) for i in range(self.N_SESSIONS)]
        for sid in sessions:
            _open(sid)

        barrier = threading.Barrier(self.N_SESSIONS)
        errors: list[Exception] = []

        def _blast(sid: str) -> None:
            try:
                barrier.wait(timeout=5.0)
                for turn in range(1, self.ENTRIES_PER + 1):
                    emit.on_turn_started(sid, turn)
                    emit.on_turn_completed(sid, turn, input_tokens=1, output_tokens=1)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_blast, args=(sid,)) for sid in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        assert not errors, f"Producer threads raised: {errors}"

        assert emit.flush(timeout=10.0), "Writer did not drain in time"
        assert emit.dropped_writes() == 0
        assert emit.buffered_writes() == 0

        for sid in sessions:
            body = _body(sid)
            started = [e for e in body if e["type"] == "turn/started"]
            completed = [e for e in body if e["type"] == "turn/completed"]
            assert len(started) == self.ENTRIES_PER, f"{sid}: wrong turn/started count"
            assert len(completed) == self.ENTRIES_PER, f"{sid}: wrong turn/completed count"

    def test_peak_reflects_something(self):
        """peak_buffered_writes() is at least zero and never negative."""
        sid = _sid(50)
        _open(sid)
        for turn in range(1, 5):
            emit.on_turn_started(sid, turn)
        emit.flush(timeout=5.0)
        assert emit.peak_buffered_writes() >= 0
        assert emit.dropped_writes() == 0


# ---------------------------------------------------------------------------
# 2. Hung writer starves siblings -- single writer thread invariant
#
# In test mode (no event loop) _submit takes the INLINE path: each call writes
# directly on its own thread. So a true cross-session starvation (writer thread
# stuck on A, B queued behind it) can only happen on the queued/writer path.
#
# What we test instead: entries for session A that are RETAINED (failed once, put
# back at the front of the bucket) hold back session A's later entries while
# session B's entries land independently. This is the per-session bucketing
# guarantee the design documents.
# ---------------------------------------------------------------------------


class TestSingleWriterHazard:
    """The documented invariant: one writer drains every session, so bucketing
    buys order and content but NOT latency isolation."""

    def test_per_session_bucketing_isolates_failures(self):
        """A transient failure on session A retains A's batch and retries, while
        session B's entries land immediately."""
        sid_a = _sid(100)
        sid_b = _sid(101)
        _open(sid_a)
        _open(sid_b)

        a_fail_count = 0
        original_append = Ledger.append

        def _fail_a_once(self_ledger, entry_type, data, **kw):
            nonlocal a_fail_count
            # Fail the first turn/started for session A by checking the file path
            if (
                entry_type == "turn/started"
                and "fanout-0100" in str(getattr(self_ledger, "_path", ""))
                and a_fail_count == 0
            ):
                a_fail_count += 1
                raise OSError("simulated ENOSPC")
            return original_append(self_ledger, entry_type, data, **kw)

        with patch.object(Ledger, "append", _fail_a_once):
            emit.on_turn_started(sid_a, 1)
            emit.on_turn_started(sid_b, 1)
            emit.on_turn_completed(sid_a, 1, input_tokens=1, output_tokens=1)
            emit.on_turn_completed(sid_b, 1, input_tokens=1, output_tokens=1)

        # After the failure + retry, everything should land
        assert emit.flush(timeout=5.0)
        assert emit.dropped_writes() == 0

        body_a = _body(sid_a)
        body_b = _body(sid_b)
        a_started = [e for e in body_a if e["type"] == "turn/started"]
        b_started = [e for e in body_b if e["type"] == "turn/started"]
        assert len(a_started) == 1, "Session A's retried entry was lost"
        assert len(b_started) == 1, "Session B was affected by A's failure"

    def test_many_sessions_independent_of_one_failing(self):
        """8 sessions are not affected by one session hitting a transient error."""
        failing_sid = _sid(200)
        ok_sids = [_sid(201 + i) for i in range(8)]
        all_sids = [failing_sid] + ok_sids

        for sid in all_sids:
            _open(sid)

        fail_count = 0
        original_append = Ledger.append

        def _fail_first_only(self_ledger, entry_type, data, **kw):
            nonlocal fail_count
            if (
                entry_type == "turn/started"
                and "fanout-0200" in str(getattr(self_ledger, "_path", ""))
                and fail_count < 2
            ):
                fail_count += 1
                raise OSError("simulated EIO")
            return original_append(self_ledger, entry_type, data, **kw)

        with patch.object(Ledger, "append", _fail_first_only):
            for sid in all_sids:
                emit.on_turn_started(sid, 1)
                emit.on_turn_completed(sid, 1, input_tokens=1, output_tokens=1)

        assert emit.flush(timeout=5.0)
        assert emit.dropped_writes() == 0

        for sid in all_sids:
            body = _body(sid)
            started = [e for e in body if e["type"] == "turn/started"]
            assert len(started) >= 1, f"{sid}: turn/started missing"


# ---------------------------------------------------------------------------
# 3. Per-session pending ceiling
# ---------------------------------------------------------------------------


class TestNothingIsShed:
    """No depth of backlog makes this module discard an entry.

    A hole in this log may not be VOLUNTARY. Several subsystems read it to decide
    what happened, and an entry discarded while the process is healthy reads exactly
    like a fact that never occurred -- nothing recovers it and no reader detects it.
    A crash-shaped loss is a different thing: the repair names and closes what a kill
    left behind. So the backlog is held however deep it gets, and the cost of a
    filesystem that stops answering is memory, up to and including the process dying
    with every session's unwritten entries in it.
    """

    def test_no_ceiling_constant_exists_to_shed_against(self):
        for name in ("_MAX_PENDING_PER_SESSION", "_MAX_PENDING_BYTES_PER_SESSION"):
            assert not hasattr(
                emit, name
            ), f"{name} is back: a ceiling trades a possible loss for a guaranteed one"

    def test_a_deep_backlog_is_held_entirely(self):
        """Well past any former ceiling, with the writer occupied, nothing is lost."""
        took_it = threading.Event()
        release = threading.Event()

        def _hangs():
            took_it.set()
            release.wait(30.0)

        try:
            sid = _sid(900)
            emit._buffer(sid, _pending(_hangs, "a write that never returns"))
            assert took_it.wait(20.0), "the writer never picked up the hanging job"
            for n in range(2000):
                emit._buffer(sid, _pending(lambda: None, f"append {n}"))

            assert (
                emit.buffered_writes() >= 2000
            ), f"only {emit.buffered_writes()} of 2000 appends are held"
            assert (
                emit.dropped_writes() == 0
            ), f"{emit.dropped_writes()} append(s) were discarded while healthy"
        finally:
            release.set()
            assert emit.drain_for_shutdown(timeout=20.0)


class TestShutdownDrainsAll:

    def test_shutdown_covers_all_sessions(self):
        n = 12
        sessions = [_sid(400 + i) for i in range(n)]
        for sid in sessions:
            _open(sid)

        for sid in sessions:
            emit.on_turn_started(sid, 1)
            emit.on_turn_completed(sid, 1, input_tokens=1, output_tokens=1)
            emit.on_turn_started(sid, 2)
            emit.on_turn_completed(sid, 2, input_tokens=2, output_tokens=2)

        result = emit.drain_for_shutdown(timeout=10.0)
        assert result, "drain_for_shutdown returned False"
        assert emit.dropped_writes() == 0
        assert emit.buffered_writes() == 0

        for sid in sessions:
            body = _body(sid)
            started = [e for e in body if e["type"] == "turn/started"]
            completed = [e for e in body if e["type"] == "turn/completed"]
            assert len(started) == 2, f"{sid}: expected 2 turn/started"
            assert len(completed) == 2, f"{sid}: expected 2 turn/completed"

    def test_shutdown_after_burst_nothing_lost(self):
        sessions = [_sid(500 + i) for i in range(10)]
        for sid in sessions:
            _open(sid)

        barrier = threading.Barrier(len(sessions))
        errors: list[Exception] = []

        def _blast(sid: str) -> None:
            try:
                barrier.wait(timeout=5.0)
                for turn in range(1, 4):
                    emit.on_turn_started(sid, turn)
                    emit.on_turn_completed(sid, turn, input_tokens=1, output_tokens=1)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_blast, args=(sid,)) for sid in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
        assert not errors

        result = emit.drain_for_shutdown(timeout=10.0)
        assert result
        assert emit.dropped_writes() == 0

        for sid in sessions:
            body = _body(sid)
            started = [e for e in body if e["type"] == "turn/started"]
            completed = [e for e in body if e["type"] == "turn/completed"]
            assert len(started) == 3, f"{sid}: expected 3 turn/started"
            assert len(completed) == 3, f"{sid}: expected 3 turn/completed"

    def test_shutdown_with_retried_entries_drains_them(self):
        sid = _sid(600)
        _open(sid)

        call_count = 0
        original_append = Ledger.append

        def _fail_first_then_succeed(self_ledger, entry_type, data, **kw):
            nonlocal call_count
            if entry_type == "turn/started":
                call_count += 1
                if call_count == 1:
                    raise OSError("simulated ENOSPC")
            return original_append(self_ledger, entry_type, data, **kw)

        with patch.object(Ledger, "append", _fail_first_then_succeed):
            emit.on_turn_started(sid, 1)
            emit.on_turn_completed(sid, 1, input_tokens=1, output_tokens=1)
            assert emit.drain_for_shutdown(
                timeout=10.0
            ), "the shutdown drain did not finish the retried entry"

        body = _body(sid)
        started = [e for e in body if e["type"] == "turn/started"]
        assert len(started) >= 1, "Retried entry was lost at shutdown"
