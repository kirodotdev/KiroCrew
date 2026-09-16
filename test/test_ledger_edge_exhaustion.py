"""Resource-exhaustion edge cases for the session ledger emitter.

ENOSPC, EIO, hung writes, sustained backpressure, oversize entries, and
shutdown-under-load.  Every test synchronises on events and injected delays,
never on wall-clock assertions or ``time.sleep``.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import ledger as lg
from kiro_crew import session_ledger_emit as emit

SESSION = "acp-exhaust-0001"
SESSION_B = "acp-exhaust-0002"


def _pending(job, what: str) -> emit._PendingJob:
    """Wrap *job* in the record the writer's buffer holds."""
    return emit._PendingJob(job=job, what=what)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


def _ledger_path(session_id: str = SESSION) -> Path:
    return lg.ledger_path("session", session_id)


def _entries(session_id: str = SESSION) -> list[dict]:
    path = _ledger_path(session_id)
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


@pytest.mark.parametrize(
    "err",
    [
        pytest.param(OSError(errno.ENOSPC, "No space left on device"), id="ENOSPC"),
        pytest.param(OSError(errno.EIO, "Input/output error"), id="EIO"),
    ],
)
def test_write_error_retains_then_drops_after_attempt_cap(err, monkeypatch, caplog):
    """ENOSPC / EIO on the write (not fsync) is a transient failure: the entry
    is retained and retried, the attempt cap eventually gives up, dropped_writes
    counts it, and a log line names the failure exactly once.
    """
    _open_session()
    assert emit.flush()

    def _raise(self, *a, **kw):
        raise err

    monkeypatch.setattr(lg.Ledger, "append", _raise)

    async def _emit():
        emit.on_turn_started(SESSION, 1, "user")
        assert not emit.flush(
            timeout=0.5
        ), "flush reported quiet while the failed filesystem still owed a loss marker"

    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        asyncio.run(_emit())

    with emit._drained:
        writer_counted_loss = emit._drained.wait_for(
            lambda: emit.dropped_writes() == 2 and emit.buffered_writes() == 0,
            timeout=20.0,
        )
    assert writer_counted_loss, "writer never finished counting the entry and failed marker"
    assert emit.dropped_writes() == 2, "the entry and its failed marker were not counted"
    assert emit.buffered_writes() == 0
    gave_up = [r for r in caplog.records if "gave up on" in r.getMessage()]
    assert len(gave_up) == 1, f"expected one loss report, got {len(gave_up)}"


@pytest.mark.parametrize(
    "err",
    [
        pytest.param(OSError(errno.ENOSPC, "No space left on device"), id="ENOSPC"),
        pytest.param(OSError(errno.EIO, "Input/output error"), id="EIO"),
    ],
)
def test_cleared_error_lands_entries_in_order_with_contiguous_seq(err, monkeypatch):
    """After the disk error clears, retained entries land in order with
    contiguous seq.
    """
    _open_session()
    assert emit.flush()

    real_append = lg.Ledger.append
    failures = {"left": 1}

    def _fail_first(self, *a, **kw):
        if failures["left"]:
            failures["left"] -= 1
            raise err
        return real_append(self, *a, **kw)

    monkeypatch.setattr(lg.Ledger, "append", _fail_first)

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


def test_hung_write_starves_another_session_until_cleared():
    """While session A's write hangs, session B's entries are HELD -- not
    dropped and not written.  This confirms the documented residual: one
    writer thread drains all sessions, so a hung write delays every other
    session.  Bucketing protects order and content, not latency.
    """
    _open_session(SESSION)
    _open_session(SESSION_B)
    assert emit.flush()

    took_it = threading.Event()
    release = threading.Event()

    def _hangs():
        took_it.set()
        release.wait(30.0)

    try:
        emit._buffer(SESSION, _pending(_hangs, "a write that hangs"))
        assert took_it.wait(20.0), "writer never picked up the job"

        for n in range(3):
            emit._buffer(
                SESSION_B,
                _pending(
                    lambda idx=n: lg.Ledger.open(lg.KIND_SESSION, SESSION_B).append(
                        "turn/started", {"turn": idx + 1, "actor": "user"}, src="acp"
                    ),
                    f"B turn {n + 1}",
                ),
            )
        assert emit.buffered_writes() >= 3
        assert emit.dropped_writes() == 0

        b_turns = [e for e in _body(SESSION_B) if e["type"] == "turn/started"]
        assert not b_turns, "B wrote during the hang"
    finally:
        release.set()

    assert emit.drain_for_shutdown(timeout=20.0)
    b_turns = [e["data"]["turn"] for e in _body(SESSION_B) if e["type"] == "turn/started"]
    assert b_turns == [1, 2, 3]
    assert emit.dropped_writes() == 0


def test_stall_is_reported_after_threshold():
    """_note_stall_if_any fires when a write exceeds _WRITE_STALL_SECS,
    and fires only once.
    """
    _open_session()
    assert emit.flush()

    with emit._lock:
        emit._inflight_since = time.monotonic() - emit._WRITE_STALL_SECS - 1.0
        emit._inflight_what = "test stalled write"
        emit._stall_reported = False

    try:
        logged = []
        real_error = logging.Logger.error

        def _spy(self, msg, *a, **kw):
            if "stalled" in str(msg):
                logged.append(msg)
            return real_error(self, msg, *a, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(logging.Logger, "error", _spy)
            emit._note_stall_if_any()

        assert logged, "the stall was not reported"

        # A second call must be silent.
        logged2: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                logging.Logger,
                "error",
                lambda self, msg, *a, **kw: logged2.append(msg) if "stalled" in str(msg) else None,
            )
            emit._note_stall_if_any()
        assert not logged2, "the stall was reported twice"
    finally:
        with emit._lock:
            emit._inflight_since = 0.0
            emit._inflight_what = ""


def test_shutdown_is_bounded_under_a_hung_write():
    """A stuck write makes the shutdown drain REFUSE, not hang.

    The guarantee is in the return value, not in how long the call took: a drain that
    could not finish reports False, and the caller decides what to do about the loss.
    Asserting on elapsed time instead would be a wall-clock race -- on a loaded
    machine the same correct code takes longer, and the test would fail for being
    slow rather than for being wrong.
    """
    _open_session()
    assert emit.flush()

    took_it = threading.Event()
    release = threading.Event()

    def _hangs():
        took_it.set()
        release.wait(300.0)

    try:
        emit._buffer(SESSION, _pending(_hangs, "a write that hangs forever"))
        assert took_it.wait(20.0)
        emit._buffer(SESSION, _pending(lambda: None, "queued behind hang"))

        # Returns rather than blocking on the stuck write, and says it did not finish.
        assert (
            emit.drain_for_shutdown(timeout=1.0) is False
        ), "the drain claimed it finished while a write was still stuck"
        # And the entry behind the hang is still HELD, not discarded, so the loss is
        # the stuck write's alone.
        assert emit.buffered_writes() >= 1, "the entry queued behind the hang was dropped"
    finally:
        release.set()


# ===================================================================== #
# 3.  Buffer under sustained pressure
# ===================================================================== #


def test_no_drops_under_sustained_pressure_and_peak_reflects_truth(monkeypatch, caplog):
    """Produce far more entries than the writer can drain.  Assert the
    never-drop-for-backpressure rule holds, peak_buffered_writes reflects
    the true peak, and the high-water warning fires exactly once.
    """
    _open_session()
    assert emit.flush()

    monkeypatch.setattr(emit, "_PENDING_HIGH_WATER", 4)
    release = threading.Event()
    real_append = lg.Ledger.append

    def _slow(self, *a, **kw):
        release.wait(20.0)
        return real_append(self, *a, **kw)

    monkeypatch.setattr(lg.Ledger, "append", _slow)
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
    _open_session()
    assert emit.flush()

    # A ceiling low enough to cross deterministically. The high-water warning
    # stays well below it so the two thresholds do not collide in this test.
    monkeypatch.setattr(emit, "_PENDING_HIGH_WATER", 2)
    monkeypatch.setattr(emit, "_MAX_PENDING_COUNT", 4)

    release = threading.Event()
    real_append = lg.Ledger.append

    def _slow(self, *a, **kw):
        release.wait(20.0)
        return real_append(self, *a, **kw)

    monkeypatch.setattr(lg.Ledger, "append", _slow)
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


def test_process_byte_ceiling_spans_session_buckets(monkeypatch):
    monkeypatch.setattr(emit, "_MAX_PENDING_BYTES", 10)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_start_drain", lambda: None)
        emit._buffer("bytes-a", emit._PendingJob(lambda: None, "bytes-a", nbytes=4))
        emit._buffer("bytes-b", emit._PendingJob(lambda: None, "bytes-b", nbytes=4))
        emit._buffer("bytes-c", emit._PendingJob(lambda: None, "bytes-c", nbytes=3))

    try:
        assert emit.overflow_writes() == 1, "aggregate session bytes did not hit the global ceiling"
        assert emit._pending_bytes == {"bytes-a": 4, "bytes-b": 4}
        assert emit._pending_total_bytes == 8
    finally:
        emit._drain_loop()


def test_claimed_bytes_still_count_toward_process_ceiling(monkeypatch):
    monkeypatch.setattr(emit, "_MAX_PENDING_BYTES", 10)
    entered = threading.Event()
    release = threading.Event()

    def _block() -> None:
        entered.set()
        release.wait(20.0)

    emit._buffer("claimed", emit._PendingJob(_block, "claimed", nbytes=8))
    try:
        assert entered.wait(5.0), "writer did not claim the first byte-counted job"
        assert emit.buffered_writes() == 0
        assert emit._pending_total_bytes == 8, "claimed batch bytes left the process total"

        emit._buffer("behind", emit._PendingJob(lambda: None, "behind", nbytes=3))
        assert emit.overflow_writes() == 1, "claimed bytes disappeared from the global ceiling"
    finally:
        release.set()
        emit.flush(timeout=20.0)


def test_the_process_byte_total_never_counts_a_job_it_did_not_add():
    """The inline path runs a job without buffering it, so its bytes are not in
    the process total. Releasing them anyway drove the total NEGATIVE, which
    loosens the ceiling instead of enforcing it -- and a retained inline job is
    entering the total for the first time, so it must be added exactly once.

    Mutation guard: dropping the ``job.counted`` test in ``_drop`` reddens the
    first assertion at -5; dropping the ``not job.counted`` filter in ``_retain``
    reddens the third by double-counting to 12.
    """
    emit.reset_caches()
    never_buffered = emit._PendingJob(lambda: None, "never buffered", nbytes=5)
    emit._drop("inline-drop", [never_buffered])
    assert (
        emit._pending_total_bytes == 0
    ), f"released bytes the total never held: {emit._pending_total_bytes}"

    emit.reset_caches()
    retained = emit._PendingJob(lambda: None, "inline retained", nbytes=6)
    emit._retain("inline-retain", [retained])
    assert (
        emit._pending_total_bytes == 6
    ), f"an inline job entering memory was miscounted: {emit._pending_total_bytes}"
    emit._retain("inline-retain", [retained])
    assert (
        emit._pending_total_bytes == 6
    ), f"an already-counted job was added twice: {emit._pending_total_bytes}"


def test_process_byte_total_returns_to_zero_after_normal_drain():
    emit._buffer(SESSION, emit._PendingJob(lambda: None, "normal drain", nbytes=7))
    assert emit.flush(timeout=20.0)
    assert emit._pending_total_bytes == 0, "landed bytes leaked from the process total"


def test_a_ceiling_above_the_load_sheds_nothing(monkeypatch):
    """Mutation guard: with the ceiling raised above the load, the same flood
    overflows zero. Proves the counter tracks the cap, not the mere act of
    buffering -- raise the bound and the red assertion above goes green.
    """
    _open_session()
    assert emit.flush()

    monkeypatch.setattr(emit, "_MAX_PENDING_COUNT", 100_000)

    release = threading.Event()
    real_append = lg.Ledger.append

    def _slow(self, *a, **kw):
        release.wait(20.0)
        return real_append(self, *a, **kw)

    monkeypatch.setattr(lg.Ledger, "append", _slow)
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
        assert emit.buffered_writes() > 0 or emit._draining

    asyncio.run(_emit())
    assert emit.drain_for_shutdown(timeout=20.0)
    assert emit.buffered_writes() == 0
    types = [e["type"] for e in _body()]
    assert "turn/started" in types
    assert "turn/completed" in types
    assert emit.dropped_writes() == 0


def test_shutdown_deadline_exceeded_reports_loss(caplog):
    """When the drain deadline is exceeded, drain returns False and a
    warning says how many are still buffered.
    """
    _open_session()
    assert emit.flush()

    took_it = threading.Event()
    release = threading.Event()

    def _stuck():
        took_it.set()
        release.wait(300.0)

    try:
        emit._buffer(SESSION, _pending(_stuck, "write that never finishes"))
        assert took_it.wait(20.0)

        for n in range(5):
            emit._buffer(SESSION, _pending(lambda: None, f"queued {n}"))

        with caplog.at_level(logging.WARNING, logger=emit.logger.name):
            drained = emit.drain_for_shutdown(timeout=0.5)

        assert drained is False
        warnings = [r for r in caplog.records if "did not finish" in r.getMessage()]
        assert warnings, "shutdown did not warn about unfinished entries"
    finally:
        release.set()


def test_a_retained_batch_is_written_at_shutdown_not_abandoned(monkeypatch):
    """A batch in retry backoff lands at shutdown instead of being lost
    because the schedule outlives the process.
    """
    _open_session()
    assert emit.flush()

    allow = threading.Event()
    real_append = lg.Ledger.append

    def _gated(self, *a, **kw):
        if not allow.is_set():
            raise OSError("disk refusing")
        return real_append(self, *a, **kw)

    monkeypatch.setattr(emit, "_retry_delay", lambda _: 5.0)
    monkeypatch.setattr(lg.Ledger, "append", _gated)
    emit.on_turn_started(SESSION, 1, "user")
    assert not emit.flush(timeout=0.5), "the gated store did not retain"
    assert emit.buffered_writes() >= 1

    allow.set()
    assert emit.drain_for_shutdown(timeout=20.0)

    assert "turn/started" in [e["type"] for e in _body()]
    assert emit.dropped_writes() == 0
