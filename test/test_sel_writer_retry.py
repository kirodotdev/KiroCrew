"""The best-effort SEL writer retries a failed append and counts what it loses.

Before this, an ``OSError`` from the append (or from the chain lock) in the
background writer was logged at WARNING and the batch vanished: no retry, and
nothing anywhere said how many audit events were gone. The writer now retries
a transient error with a capped backoff, skips the retry for an errno that
cannot heal, and adds a lost batch to ``dropped_events`` with one ERROR per
streak of failures.

No real waiting: the backoff seam records the requested waits instead.
"""

from __future__ import annotations

import errno
import logging

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.sel import SecurityEvent, SecurityEventLog


@pytest.fixture(autouse=True)
def reset_singleton():
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False
    yield
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False


@pytest.fixture
def waits(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(sel_mod, "_backoff_wait", recorded.append, raising=False)
    return recorded


def _event(event_id: str) -> SecurityEvent:
    return SecurityEvent(
        event_id=event_id,
        timestamp="2026-05-13T00:00:00+00:00",
        event_type="tool_invocation",
        caller_identity="dashboard:abc",
        agent="kirocrew",
        source="dashboard",
        operation="execute_bash",
    )


def _failing_append(monkeypatch, log, errors):
    """Make the append raise each of *errors* in turn, then succeed for real."""
    real = log._append_lines_locked
    calls: list[int] = []

    def append(lines, **kwargs):
        calls.append(len(lines))
        if len(calls) <= len(errors):
            # Raised below the chaining, so the real tip rollback runs.
            raise errors[len(calls) - 1]
        return real(lines, **kwargs)

    monkeypatch.setattr(log, "_append_lines_locked", append)
    return calls


def _error_records(caplog):
    return [r for r in caplog.records if r.levelno == logging.ERROR and r.name == sel_mod.__name__]


def test_transient_error_then_success_appends_the_batch_once(tmp_path, monkeypatch, waits, caplog):
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    calls = _failing_append(monkeypatch, log, [OSError(errno.EIO, "I/O error")])
    batch = [_event("retry-a"), _event("retry-b")]

    log._flush_with_retry(batch)

    assert calls == [2, 2]
    assert waits == [sel_mod._WRITE_BACKOFF_SECS]
    text = log._path.read_text(encoding="utf-8")
    assert text.count("retry-a") == 1 and text.count("retry-b") == 1
    assert log.dropped_events == 0
    assert _error_records(caplog) == []
    assert log.verify_integrity() == (2, 2)


@pytest.mark.parametrize(
    "code", [errno.ENOSYS, errno.EINVAL, errno.EPERM, errno.EACCES, errno.EROFS]
)
def test_permanent_errno_is_not_retried_and_is_counted(tmp_path, monkeypatch, waits, caplog, code):
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    calls = _failing_append(monkeypatch, log, [OSError(code, "nope")] * 10)
    batch = [_event("perm-a"), _event("perm-b"), _event("perm-c")]

    with caplog.at_level(logging.ERROR, logger=sel_mod.__name__):
        log._flush_with_retry(batch)

    assert calls == [3]
    assert waits == []
    assert log.dropped_events == 3
    assert len(_error_records(caplog)) == 1


def test_exhausted_retries_back_off_capped_and_log_one_error_per_streak(
    tmp_path, monkeypatch, waits, caplog
):
    monkeypatch.setattr(sel_mod, "_WRITE_BACKOFF_CAP_SECS", 0.08)
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    calls = _failing_append(monkeypatch, log, [OSError(errno.EIO, "I/O error")] * 100)

    with caplog.at_level(logging.ERROR, logger=sel_mod.__name__):
        log._flush_with_retry([_event("x1")])
        log._flush_with_retry([_event("x2"), _event("x3")])

    per_batch = sel_mod._WRITE_RETRIES + 1
    assert calls == [1] * per_batch + [2] * per_batch
    # Doubling from the base, clamped at the cap, restarting for each batch.
    assert waits == [0.05, 0.08, 0.08] * 2
    assert log.dropped_events == 3
    assert len(_error_records(caplog)) == 1


def test_a_success_ends_the_streak_so_the_next_loss_logs_again(
    tmp_path, monkeypatch, waits, caplog
):
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    perm = OSError(errno.EROFS, "read-only")
    _failing_append(monkeypatch, log, [perm])

    with caplog.at_level(logging.ERROR, logger=sel_mod.__name__):
        log._flush_with_retry([_event("s1")])
        log._flush_with_retry([_event("s2")])
        _failing_append(monkeypatch, log, [perm])
        log._flush_with_retry([_event("s3")])

    assert log.dropped_events == 2
    assert len(_error_records(caplog)) == 2


def test_chain_lock_failure_is_retried_too(tmp_path, monkeypatch, waits):
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    real = log._chain_lock
    attempts: list[int] = []

    def chain_lock(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError(errno.EAGAIN, "busy")
        return real(**kwargs)

    monkeypatch.setattr(log, "_chain_lock", chain_lock)

    log._flush_with_retry([_event("lock-a")])

    assert len(attempts) == 2
    assert log._path.read_text(encoding="utf-8").count("lock-a") == 1
    assert log.dropped_events == 0


def test_a_partly_written_batch_is_never_replayed(tmp_path, monkeypatch, waits, caplog):
    """Disk fills after the first line lands: a replay would duplicate it."""
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    real = log._append_lines_locked
    calls: list[int] = []

    def append(lines, **kwargs):
        calls.append(len(lines))
        real(lines[:1], **kwargs)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(log, "_append_lines_locked", append)

    with caplog.at_level(logging.ERROR, logger=sel_mod.__name__):
        log._flush_with_retry([_event("part-a"), _event("part-b")])

    assert calls == [2]
    assert waits == []
    text = log._path.read_text(encoding="utf-8")
    assert text.count("part-a") == 1 and "part-b" not in text
    # Counted as the whole batch: the writer cannot tell how much landed.
    assert log.dropped_events == 2
    assert len(_error_records(caplog)) == 1


def _stat_fails_for(monkeypatch, target, when):
    """Make ``Path.stat`` raise EIO for *target* while ``when()`` is true."""
    real_stat = type(target).stat

    def stat(self, *args, **kwargs):
        if self == target and when():
            raise OSError(errno.EIO, "I/O error", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(target), "stat", stat)


def test_a_failed_stat_after_a_partial_append_is_never_replayed(tmp_path, monkeypatch, waits):
    """Absent log, a prefix lands, then EIO -- and the stat after it fails too."""
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    assert not log._path.exists()
    real = log._append_lines_locked
    calls: list[int] = []

    def append(lines, **kwargs):
        calls.append(len(lines))
        real(lines[:1], **kwargs)
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(log, "_append_lines_locked", append)
    _stat_fails_for(monkeypatch, log._path, lambda: bool(calls))

    exc = log._flush_batch([_event("blind-a"), _event("blind-b")])
    assert isinstance(exc, sel_mod._MaybeWritten)

    monkeypatch.undo()
    assert log._path.read_text(encoding="utf-8").count("blind-a") == 1


def test_failed_stats_around_the_append_are_never_replayed(tmp_path, monkeypatch, waits):
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    calls = _failing_append(monkeypatch, log, [OSError(errno.EIO, "I/O error")] * 10)
    probing = [True]
    # Both probes fail: two unknowns must not compare equal and allow a replay.
    _stat_fails_for(monkeypatch, log._path, lambda: probing[0])

    log._flush_with_retry([_event("pre-a")])
    probing[0] = False

    assert calls == [1]
    assert waits == []
    assert log.dropped_events == 1


def test_the_background_writer_uses_the_retry(tmp_path, monkeypatch, waits):
    log = SecurityEventLog(base_dir=tmp_path, sync=False)
    calls = _failing_append(monkeypatch, log, [OSError(errno.EIO, "I/O error")])

    try:
        log.log(_event("async-a"))
        log.flush(timeout=10)
    finally:
        # Stop the daemon so it does not outlive this test's directory.
        writer = log._writer
        if writer is not None:
            log._queue.put(None)
            writer.join(timeout=10)

    assert writer is not None and not writer.is_alive()
    assert calls == [1, 1]
    assert log._path.read_text(encoding="utf-8").count("async-a") == 1
    assert log.dropped_events == 0
