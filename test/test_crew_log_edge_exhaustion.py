"""Resource-exhaustion edge cases for the session log emitter.

ENOSPC, EIO, hung writes, sustained backpressure, oversize entries, and
shutdown-under-load.  Every test synchronises on events and injected delays,
never on wall-clock assertions or ``time.sleep``.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import threading
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import emit, lease
from kiro_crew.crew_log.writer import WriterLimits

SESSION = "acp-exhaust-0001"
SESSION_B = "acp-exhaust-0002"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()
    # A write lease is released when the handle that adopted it is dropped, and
    # nothing in this file may keep one alive past its own test: the holder table
    # is process-global, so a handle this test retains is a lease the next test on
    # this worker -- or the ``TestThisProcessDoesNotRetainAMemberLogsWriteLease``
    # pin in ``test_eventlog_hooks.py`` -- observes as held. Read immediately after
    # the drain: release rides the handle's refcount, so a lease still held here is
    # a retention, not a frame that has not finished unwinding. The table is
    # process-wide, so the key it names may belong to an EARLIER file on this
    # worker (the macOS run of this PR caught ``test_crew_log_core.py``'s
    # chmod-refusal test that way); the path in the message says whose it is.
    assert not lease._held, f"a lease outlived its test on this worker: {sorted(lease._held)}"


#: The write errors the disk-failure tests inject, as errno values. Built into an
#: ``OSError`` inside each test rather than parametrized as instances: an instance in
#: a parametrize list lives for the module, ``raise err`` attaches a ``__traceback__``
#: to it, and that traceback's frames hold the ``CrewLog`` handle whose lease release is
#: a ``weakref.finalize`` -- so the lease stays held for the life of the worker.
_WRITE_ERRNOS = [
    pytest.param(errno.ENOSPC, id="ENOSPC"),
    pytest.param(errno.EIO, id="EIO"),
]


def _write_error(code: int) -> OSError:
    return OSError(code, os.strerror(code))


def _log_path(session_id: str = SESSION) -> Path:
    return lg.crew_log_path("session", session_id)


def _entries(session_id: str = SESSION) -> list[dict]:
    path = _log_path(session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _body(session_id: str = SESSION) -> list[dict]:
    return _entries(session_id)[1:]


def _open_session(session_id: str = SESSION) -> None:
    emit.on_session_opened(
        session_id,
        agent="kirocrew",
        slot="chat-x",
        model="claude-opus-5",
        cwd="/tmp",
        owner="default",
    )


# ===================================================================== #
# 1.  ENOSPC and EIO on the WRITE path (not fsync)
# ===================================================================== #


@pytest.mark.parametrize("code", _WRITE_ERRNOS)
def test_cleared_error_lands_entries_in_order_with_contiguous_seq(code, monkeypatch):
    """After the disk error clears, retained entries land in order with
    contiguous seq.
    """
    _open_session()
    assert emit.flush()

    real_append = lg.CrewLog.append
    failures = {"left": 1}

    def _fail_first(self, *a, **kw):
        if failures["left"]:
            failures["left"] -= 1
            raise _write_error(code)
        return real_append(self, *a, **kw)

    monkeypatch.setattr(lg.CrewLog, "append", _fail_first)

    async def _emit():
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        assert emit.flush(timeout=20.0)

    asyncio.run(_emit())
    assert failures["left"] == 0, "the injected error never fired"
    body = _body()
    types = [e["type"] for e in body]
    assert types == ["session/opened", "turn/started", "tool/called", "turn/completed"]
    seqs = [e["seq"] for e in body]
    assert seqs == sorted(seqs), f"seq is not monotonic: {seqs}"
    for i in range(1, len(seqs)):
        assert seqs[i] == seqs[i - 1] + 1, f"gap at {seqs[i - 1]}→{seqs[i]}"
    assert emit.dropped_writes() == 0


# ===================================================================== #
# 2.  A write that HANGS
# ===================================================================== #


# ===================================================================== #
# 3.  Buffer under sustained pressure
# ===================================================================== #


def test_no_drops_under_sustained_pressure_and_peak_reflects_truth(monkeypatch, caplog):
    """Produce far more entries than the writer can drain.  Assert the
    never-drop-for-backpressure rule holds, peak_buffered_writes reflects
    the true peak, and the high-water warning fires exactly once.
    """
    emit.reset_caches(limits=WriterLimits(pending_high_water=4))
    _open_session()
    assert emit.flush()

    release = threading.Event()
    real_append = lg.CrewLog.append

    def _slow(self, *a, **kw):
        release.wait(20.0)
        return real_append(self, *a, **kw)

    monkeypatch.setattr(lg.CrewLog, "append", _slow)
    count = 30

    async def _flood():
        for n in range(count):
            emit.on_tool_called(SESSION, 1, name="fs_read", call_id=f"tc-{n}")
        release.set()
        assert emit.flush(timeout=20.0)

    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        asyncio.run(_flood())

    assert emit.dropped_writes() == 0
    assert emit.peak_buffered_writes() >= 4

    calls = [e for e in _body() if e["type"] == "tool/called"]
    assert len(calls) == count
    ids = [e["data"]["call_id"] for e in calls]
    assert ids == [f"tc-{n}" for n in range(count)]

    hw = [r for r in caplog.records if "buffered" in r.getMessage() and "mark" in r.getMessage()]
    assert len(hw) == 1, f"high-water warning fired {len(hw)} times, expected 1"


# ===================================================================== #
# 3b.  Hard memory ceiling: overflow is rejected at the tail and counted
# ===================================================================== #


def test_buffer_overflow_is_rejected_at_the_tail_and_counted(monkeypatch, caplog):
    """Hold the writer and flood past the hard count ceiling. Assert the
    overflow is rejected at the tail (not shed from the head), counted in
    overflow_writes, the queued prefix keeps its order, and the loss is
    named exactly once at error level.
    """
    # A ceiling low enough to cross deterministically. The high-water warning
    # stays well below it so the two thresholds do not collide in this test.
    emit.reset_caches(limits=WriterLimits(pending_high_water=2, max_pending_count=4))
    _open_session()
    assert emit.flush()

    release = threading.Event()
    real_append = lg.CrewLog.append

    def _slow(self, *a, **kw):
        release.wait(20.0)
        return real_append(self, *a, **kw)

    monkeypatch.setattr(lg.CrewLog, "append", _slow)
    count = 10  # 4 fit under the ceiling, 6 overflow

    async def _flood():
        for n in range(count):
            emit.on_tool_called(SESSION, 1, name="fs_read", call_id=f"tc-{n}")
        # The ceiling holds while the writer is blocked: only the first entries fit.
        assert emit.buffered_writes() <= 4
        assert emit.overflow_writes() >= count - 4
        release.set()
        assert emit.flush(timeout=20.0)

    with caplog.at_level(logging.ERROR, logger=emit.logger.name):
        asyncio.run(_flood())

    # The counter moved past what the cap allowed: the rejection is observable,
    # not a constant nobody reads.
    assert emit.overflow_writes() >= count - 4
    assert emit.buffered_writes() == 0

    # The prefix that fit kept its order -- the tail was rejected, the head was not
    # shed, so the log is a faithful run from the start rather than holed in the
    # middle.
    calls = [e for e in _body() if e["type"] == "tool/called"]
    ids = [e["data"]["call_id"] for e in calls]
    assert ids == [f"tc-{n}" for n in range(len(ids))]
    assert len(ids) <= 4

    over = [r for r in caplog.records if "buffer full" in r.getMessage()]
    assert len(over) == 1, f"overflow reported {len(over)} times, expected 1"


def test_a_ceiling_above_the_load_sheds_nothing(monkeypatch):
    """Mutation guard: with the ceiling raised above the load, the same flood
    overflows zero. Proves the counter tracks the cap, not the mere act of
    buffering -- raise the bound and the red assertion above goes green.
    """
    emit.reset_caches(limits=WriterLimits(max_pending_count=100_000))
    _open_session()
    assert emit.flush()

    release = threading.Event()
    real_append = lg.CrewLog.append

    def _slow(self, *a, **kw):
        release.wait(20.0)
        return real_append(self, *a, **kw)

    monkeypatch.setattr(lg.CrewLog, "append", _slow)
    count = 10

    async def _flood():
        for n in range(count):
            emit.on_tool_called(SESSION, 1, name="fs_read", call_id=f"tc-{n}")
        release.set()
        assert emit.flush(timeout=20.0)

    asyncio.run(_flood())

    assert emit.overflow_writes() == 0
    assert emit.dropped_writes() == 0
    calls = [e for e in _body() if e["type"] == "tool/called"]
    assert len(calls) == count


# ===================================================================== #
# 4.  At and over MAX_ENTRY_BYTES (64 KiB)
# ===================================================================== #


def test_oversize_raw_entry_is_refused_immediately_not_retried(caplog):
    """A raw entry over the ceiling is a REFUSAL: dropped immediately,
    not retried forever.
    """
    _open_session()
    assert emit.flush()

    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        emit.on_tool_called(SESSION, 1, name="x" * (70 * 1024), call_id="tc-big")

    assert emit.dropped_writes() == 1
    assert emit.buffered_writes() == 0

    # A good write after it still works.
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert _body()[-1]["type"] == "turn/completed"


def test_oversize_body_is_chunked_not_refused():
    """A message body over the ceiling is split into chunks."""
    _open_session()
    huge = "y" * (lg.MAX_ENTRY_BYTES + 5000)
    emit.on_message_sent(SESSION, 1, step=1, text=huge)
    assert emit.flush()

    chunks = [e for e in _body() if e["type"] == "message/chunk"]
    sent = [e for e in _body() if e["type"] == "message/sent"]
    assert chunks, "the oversize body was not chunked"
    assert sent, "the message was lost"
    assert sent[-1]["data"]["chunks"] == [c["seq"] for c in chunks]
    assert "".join(c["data"]["delta"] for c in chunks) == huge
    assert emit.dropped_writes() == 0


def test_body_exactly_at_ceiling_fits_on_one_line():
    """A body that fits with envelope headroom is not split."""
    _open_session()
    body = "a" * (lg.MAX_ENTRY_BYTES - emit._ENVELOPE_HEADROOM - 200)
    emit.on_message_sent(SESSION, 1, step=1, text=body)
    assert emit.flush()

    chunks = [e for e in _body() if e["type"] == "message/chunk"]
    sent = [e for e in _body() if e["type"] == "message/sent"]
    assert not chunks
    assert sent and sent[-1]["data"]["text"] == body


# ===================================================================== #
# 5.  Shutdown / SIGTERM-ish teardown with entries still buffered
# ===================================================================== #


def test_shutdown_drains_buffered_entries_within_deadline():
    """Entries emitted on the event loop are buffered; the drain writes them."""
    _open_session()
    assert emit.flush()

    async def _emit():
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")

    asyncio.run(_emit())
    assert emit.drain_for_shutdown(timeout=20.0)
    assert emit.buffered_writes() == 0
    types = [e["type"] for e in _body()]
    assert "turn/started" in types
    assert "turn/completed" in types
    assert emit.dropped_writes() == 0


def test_a_retained_batch_is_written_at_shutdown_not_abandoned(monkeypatch):
    """A batch in retry backoff lands at shutdown instead of being lost
    because the schedule outlives the process.
    """
    _open_session()
    assert emit.flush()

    allow = threading.Event()
    real_append = lg.CrewLog.append

    def _gated(self, *a, **kw):
        if not allow.is_set():
            raise OSError("disk refusing")
        return real_append(self, *a, **kw)

    monkeypatch.setattr(emit, "_retry_delay", lambda _: 5.0)
    monkeypatch.setattr(lg.CrewLog, "append", _gated)
    emit.on_turn_started(SESSION, 1, "user")
    assert not emit.flush(timeout=0.5), "the gated store did not retain"
    assert emit.buffered_writes() >= 1

    allow.set()
    assert emit.drain_for_shutdown(timeout=20.0)

    assert "turn/started" in [e["type"] for e in _body()]
    assert emit.dropped_writes() == 0
