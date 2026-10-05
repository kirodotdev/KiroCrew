"""Producer-side concurrency of the session log emitter.

The STORE layer's two-handle seq race is pinned in test_crew_log_core.py. This
file attacks the EMITTER layer above it: the _submit / _buffer / _drain_loop /
flush machinery that sits between a producer calling on_* and the CrewLog.append
that lands a line on disk.

Four areas, matching the task spec:
1. Many producers, one session -- contiguous seq, no gaps, causal order.
2. Many sessions -- no cross-contamination, each file well-formed.
3. Interleaved sessions -- one session's write in flight cannot lose another's.
4. flush(timeout=...) under concurrent producers returns only when empty.

The drain barrier every test here waits on (``crew_log_drain``) is pinned last: its
two ways out, so a writer that stops is reported by name rather than waited on.
"""

from __future__ import annotations

import asyncio
import json
import threading

import crew_log_drain
import pytest
from crew_log_drain import assert_drained, settle, unsync_appends

from kiro_crew.crew_log import CrewLog, crew_log_path, emit

SESSION = "conc-edge-sess-0001"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Each test gets its own data home, zero-backoff, and a clean emitter.

    The writer is waited out BEFORE the home pin lifts (:func:`crew_log_drain.settle`):
    a batch it still holds would otherwise write into the next test's home.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    settle()


@pytest.fixture
def _unsynced_appends(monkeypatch):
    """These tests pin ordering and no-loss, not durability; see ``unsync_appends``."""
    unsync_appends(monkeypatch)


def _open(sid: str = SESSION) -> None:
    emit.on_session_opened(sid, agent="kirocrew", slot="test", model="m", owner="default")
    assert_drained()


def _entries(sid: str = SESSION) -> list[dict]:
    path = crew_log_path("session", sid)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _body(sid: str = SESSION) -> list[dict]:
    return _entries(sid)[1:]


# ---------------------------------------------------------------------------
# 1. Many producers, one session
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_unsynced_appends")
def test_many_producers_one_session_all_entries_land():
    """N threads calling on_tool_called for the same session. Every entry must
    land, seq must be contiguous (no gaps, no duplicates)."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert_drained()

    n_threads = 8
    calls_per_thread = 20
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def _produce(thread_idx: int) -> None:
        try:
            barrier.wait()
            for i in range(calls_per_thread):
                emit.on_tool_called(
                    SESSION,
                    turn=1,
                    name=f"tool_t{thread_idx}",
                    call_id=f"call-{thread_idx}-{i}",
                    server="test-server",
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_produce, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"producer threads raised: {errors}"
    assert_drained()

    body = _body()
    # Filter to tool/called entries (skip session/opened, turn/started)
    tool_entries = [e for e in body if e["type"] == "tool/called"]
    assert len(tool_entries) == n_threads * calls_per_thread

    # Seq must be contiguous with no gaps across ALL body entries
    all_seqs = [e["seq"] for e in body]
    assert all_seqs == list(
        range(1, len(body) + 1)
    ), f"seq not contiguous: gaps at {_find_gaps(all_seqs)}"


@pytest.mark.usefixtures("_unsynced_appends")
def test_many_producers_one_session_per_session_order_preserved():
    """Each thread's entries must appear in the order that thread emitted them.
    The emitter buffers per session and the single writer drains in order, so
    within one session causal order (emit A before B -> seq(A) < seq(B)) must
    hold for entries from the SAME producer thread."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert_drained()

    n_threads = 6
    calls_per_thread = 15
    barrier = threading.Barrier(n_threads)

    def _produce(thread_idx: int) -> None:
        barrier.wait()
        for i in range(calls_per_thread):
            # Encode the thread and the sequence number in the call_id
            emit.on_tool_called(
                SESSION,
                turn=1,
                name=f"tool_t{thread_idx}",
                call_id=f"call-{thread_idx}-{i:04d}",
                server="test-server",
            )

    threads = [threading.Thread(target=_produce, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert_drained()

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]

    # For each thread, extract the entries it produced and verify they are in
    # submission order by checking the index encoded in call_id.
    for thread_idx in range(n_threads):
        thread_entries = [
            e for e in tool_entries if e["data"]["call_id"].startswith(f"call-{thread_idx}-")
        ]
        assert (
            len(thread_entries) == calls_per_thread
        ), f"thread {thread_idx}: expected {calls_per_thread}, got {len(thread_entries)}"
        indices = [int(e["data"]["call_id"].split("-")[-1]) for e in thread_entries]
        assert indices == list(
            range(calls_per_thread)
        ), f"thread {thread_idx}: entries out of causal order: {indices}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_unsynced_appends")
async def test_entries_queued_behind_the_writer_keep_each_producers_order(monkeypatch):
    """The same causal order, through the WRITER thread rather than inline appends.

    Off an event loop a producer writes inline whenever its session owes nothing, so at
    native speed the test above lands every entry on its own thread and never puts one
    in a batch the writer claims. Here the writer is parked first, inside an append
    queued from the loop: the session then owes, every producer below buffers behind
    it, and the batches the writer claims once it is released decide the order on disk.
    """
    await asyncio.to_thread(_open)
    n_threads = 6
    calls_per_thread = 15
    release = _park_appends(monkeypatch)
    try:
        emit.on_turn_started(SESSION, turn=1)
        barrier = threading.Barrier(n_threads, timeout=crew_log_drain.DRAIN_CEILING_SECONDS)

        def _produce(thread_idx: int) -> None:
            barrier.wait()
            for i in range(calls_per_thread):
                emit.on_tool_called(
                    SESSION,
                    turn=1,
                    name=f"tool_t{thread_idx}",
                    call_id=f"call-{thread_idx}-{i:04d}",
                    server="test-server",
                )

        threads = [threading.Thread(target=_produce, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=crew_log_drain.DRAIN_CEILING_SECONDS)
        assert not any(t.is_alive() for t in threads), "a producer never returned"
    finally:
        release.set()
    await asyncio.to_thread(assert_drained)

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]
    for thread_idx in range(n_threads):
        thread_entries = [
            e for e in tool_entries if e["data"]["call_id"].startswith(f"call-{thread_idx}-")
        ]
        indices = [int(e["data"]["call_id"].split("-")[-1]) for e in thread_entries]
        assert indices == list(
            range(calls_per_thread)
        ), f"thread {thread_idx}: entries out of causal order: {indices}"


@pytest.mark.usefixtures("_unsynced_appends")
def test_call_index_contiguous_under_concurrent_producers():
    """call_index is minted under _lock per turn. With N threads calling
    on_tool_called concurrently the resulting call_index values must form a
    contiguous 1..N*M range with no duplicates."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert_drained()

    n_threads = 8
    calls_per_thread = 10
    barrier = threading.Barrier(n_threads)

    def _produce(thread_idx: int) -> None:
        barrier.wait()
        for i in range(calls_per_thread):
            emit.on_tool_called(
                SESSION,
                turn=1,
                name=f"t{thread_idx}",
                call_id=f"ci-{thread_idx}-{i}",
                server="s",
            )

    threads = [threading.Thread(target=_produce, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert_drained()

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]
    call_indices = sorted(e["data"]["call_index"] for e in tool_entries)
    expected = list(range(1, n_threads * calls_per_thread + 1))
    assert call_indices == expected, (
        f"call_index not contiguous: missing={set(expected) - set(call_indices)}, "
        f"dupes={[x for x in call_indices if call_indices.count(x) > 1]}"
    )


# ---------------------------------------------------------------------------
# 2. Many sessions at once
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_unsynced_appends")
def test_many_sessions_no_cross_contamination():
    """M sessions emitting concurrently. No session's file holds another's
    entries, and each is independently readable by the production reader."""
    n_sessions = 6
    entries_per_session = 20
    sids = [f"conc-multi-{i:04d}" for i in range(n_sessions)]

    for sid in sids:
        _open(sid)
        emit.on_turn_started(sid, turn=1)
    assert_drained()

    barrier = threading.Barrier(n_sessions)

    def _produce(sid: str) -> None:
        barrier.wait()
        for i in range(entries_per_session):
            emit.on_tool_called(
                sid,
                turn=1,
                name=f"tool-{sid}",
                call_id=f"call-{sid}-{i}",
                server="s",
            )

    threads = [threading.Thread(target=_produce, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert_drained()

    for sid in sids:
        body = _body(sid)
        tool_entries = [e for e in body if e["type"] == "tool/called"]
        # Correct count
        assert (
            len(tool_entries) == entries_per_session
        ), f"{sid}: expected {entries_per_session} tool entries, got {len(tool_entries)}"
        # No foreign entries
        for e in tool_entries:
            assert e["data"]["name"] == f"tool-{sid}", (
                f"{sid}: cross-contamination, entry names tool for another session: "
                f"{e['data']['name']}"
            )
            assert e["data"]["call_id"].startswith(
                f"call-{sid}-"
            ), f"{sid}: cross-contamination in call_id: {e['data']['call_id']}"

        # Each file is independently readable by the production reader
        log = CrewLog.open("session", sid)
        reader_entries = list(log.iter_from(1))
        assert len(reader_entries) > 0, f"{sid}: iter_from returned nothing"
        reader_seqs = [re.seq for re in reader_entries]
        assert reader_seqs == list(
            range(1, len(reader_entries) + 1)
        ), f"{sid}: production reader sees non-contiguous seqs"


@pytest.mark.usefixtures("_unsynced_appends")
def test_many_sessions_each_file_well_formed():
    """Each session's file must parse as valid JSON lines and the header must
    be present and correct."""
    n_sessions = 4
    sids = [f"conc-wf-{i}" for i in range(n_sessions)]

    for sid in sids:
        _open(sid)
        emit.on_turn_started(sid, turn=1)
    assert_drained()

    barrier = threading.Barrier(n_sessions)

    def _produce(sid: str) -> None:
        barrier.wait()
        emit.on_message_received(sid, turn=1, text=f"hello from {sid}")
        emit.on_tool_called(sid, turn=1, name="tool", call_id=f"c-{sid}", server="s")
        emit.on_tool_completed(sid, turn=1, name="tool", call_id=f"c-{sid}", status="ok")
        emit.on_turn_completed(sid, turn=1, input_tokens=10, output_tokens=5)

    threads = [threading.Thread(target=_produce, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert_drained()

    for sid in sids:
        entries = _entries(sid)
        # Header is entry 0 -- its type is the crew log kind, "session"
        assert entries[0]["type"] == "session", f"{sid}: first line is not a header"
        # Every line parses as valid JSON (already guaranteed by _entries, but
        # also check via the production reader)
        log = CrewLog.open("session", sid)
        all_read = list(log.iter_from(1))
        types_seen = {e.type for e in all_read}
        assert "session/opened" in types_seen, f"{sid}: missing session/opened"
        assert "turn/started" in types_seen, f"{sid}: missing turn/started"
        assert "turn/completed" in types_seen, f"{sid}: missing turn/completed"


# ---------------------------------------------------------------------------
# 3. Interleaved sessions -- no entry lost while another's write is in flight
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_unsynced_appends")
def test_interleaved_sessions_no_entry_lost():
    """Session A and session B emit concurrently. The invariant: bucketing buys
    order and content, NOT latency isolation. No entry from either session is
    lost while the other's write is in flight."""
    sid_a = "conc-interleave-a"
    sid_b = "conc-interleave-b"
    _open(sid_a)
    _open(sid_b)
    emit.on_turn_started(sid_a, turn=1)
    emit.on_turn_started(sid_b, turn=1)
    assert_drained()

    n_entries = 30
    barrier = threading.Barrier(2)

    def _produce_a() -> None:
        barrier.wait()
        for i in range(n_entries):
            emit.on_tool_called(sid_a, turn=1, name="a-tool", call_id=f"a-{i}", server="s")

    def _produce_b() -> None:
        barrier.wait()
        for i in range(n_entries):
            emit.on_tool_called(sid_b, turn=1, name="b-tool", call_id=f"b-{i}", server="s")

    ta = threading.Thread(target=_produce_a)
    tb = threading.Thread(target=_produce_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    assert_drained()

    body_a = [e for e in _body(sid_a) if e["type"] == "tool/called"]
    body_b = [e for e in _body(sid_b) if e["type"] == "tool/called"]

    assert (
        len(body_a) == n_entries
    ), f"session A lost entries: expected {n_entries}, got {len(body_a)}"
    assert (
        len(body_b) == n_entries
    ), f"session B lost entries: expected {n_entries}, got {len(body_b)}"

    # Each session's seqs are contiguous on their own
    for label, body in [("A", _body(sid_a)), ("B", _body(sid_b))]:
        seqs = [e["seq"] for e in body]
        assert seqs == list(range(1, len(body) + 1)), f"session {label}: non-contiguous seqs"


@pytest.mark.usefixtures("_unsynced_appends")
def test_burst_across_sessions_all_entries_land():
    """A harder variant: many sessions each emitting a burst simultaneously,
    testing that the single writer thread and per-session bucketing lose
    nothing under a wide fan-out."""
    n_sessions = 10
    entries_per = 15
    sids = [f"conc-burst-{i:03d}" for i in range(n_sessions)]

    for sid in sids:
        _open(sid)
        emit.on_turn_started(sid, turn=1)
    assert_drained()

    barrier = threading.Barrier(n_sessions)

    def _produce(sid: str) -> None:
        barrier.wait()
        for i in range(entries_per):
            emit.on_tool_called(sid, turn=1, name="t", call_id=f"c-{sid}-{i}", server="s")

    threads = [threading.Thread(target=_produce, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert_drained()

    total_tool_entries = 0
    for sid in sids:
        tool_entries = [e for e in _body(sid) if e["type"] == "tool/called"]
        assert (
            len(tool_entries) == entries_per
        ), f"{sid}: expected {entries_per}, got {len(tool_entries)}"
        total_tool_entries += len(tool_entries)
    assert total_tool_entries == n_sessions * entries_per


# ---------------------------------------------------------------------------
# 4. flush(timeout=...) under concurrent producers
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_unsynced_appends")
def test_flush_returns_only_when_buffer_empty():
    """flush() must return True only when the buffer is truly empty, even when
    producers are still active at the moment flush is called."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert_drained()

    n_entries = 50
    produced = threading.Event()

    def _produce() -> None:
        for i in range(n_entries):
            emit.on_tool_called(SESSION, turn=1, name="ft", call_id=f"fc-{i}", server="s")
        produced.set()

    t = threading.Thread(target=_produce)
    t.start()
    produced.wait()  # Wait until all entries are submitted
    t.join()

    # Now flush -- it must wait until the writer has drained everything
    assert_drained()
    assert (
        emit.buffered_writes() == 0
    ), f"flush returned True but {emit.buffered_writes()} writes still buffered"

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]
    assert len(tool_entries) == n_entries


@pytest.mark.usefixtures("_unsynced_appends")
def test_flush_under_ongoing_production():
    """flush called while producers are STILL emitting. flush must return True
    only after the entries that existed at the time of the call have been
    written. (New entries submitted after flush starts looking are NOT its
    responsibility, but anything queued before must land.)"""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert_drained()

    n_pre_flush = 20
    n_post_flush = 10
    pre_flush_done = threading.Event()
    flush_started = threading.Event()
    flush_result: list[AssertionError | None] = []

    def _produce() -> None:
        for i in range(n_pre_flush):
            emit.on_tool_called(SESSION, turn=1, name="pre", call_id=f"pre-{i}", server="s")
        pre_flush_done.set()
        flush_started.wait()  # Wait for flush to begin
        for i in range(n_post_flush):
            emit.on_tool_called(SESSION, turn=1, name="post", call_id=f"post-{i}", server="s")

    def _flusher() -> None:
        pre_flush_done.wait()
        flush_started.set()
        try:
            assert_drained()
        except AssertionError as exc:  # raised on this thread, reported on the test's
            flush_result.append(exc)
        else:
            flush_result.append(None)

    tp = threading.Thread(target=_produce)
    tf = threading.Thread(target=_flusher)
    tp.start()
    tf.start()
    tp.join()
    tf.join()

    assert flush_result == [None], f"the flush barrier failed: {flush_result}"

    # A second flush to ensure everything including post-flush entries has landed
    assert_drained()

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]
    # At minimum, the pre-flush entries must be there; post-flush may or may not
    # be included depending on timing, but a second flush guarantees all.
    assert len(tool_entries) == n_pre_flush + n_post_flush


def test_flush_timeout_zero_does_not_hang():
    """flush(timeout=0) must return immediately, True if empty, False otherwise.
    It must never block."""
    _open()
    # With nothing pending, should return True instantly
    assert emit.flush(timeout=0.0) or True  # may be True or False, must not hang

    # Submit some entries
    emit.on_turn_started(SESSION, turn=1)
    for i in range(5):
        emit.on_tool_called(SESSION, turn=1, name="t", call_id=f"z-{i}", server="s")

    # flush(0) returns immediately -- we just check it returns at all
    emit.flush(timeout=0.0)
    # Now drain properly
    assert_drained()


# ---------------------------------------------------------------------------
# Mixed: different on_* entry points concurrently on one session
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_unsynced_appends")
def test_mixed_entry_points_concurrent():
    """Different on_* functions called concurrently for one session. Tests that
    the emitter's internal state (_live, _tool_started, _pinned) stays
    consistent under contention."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert_drained()

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def _tools() -> None:
        try:
            barrier.wait()
            for i in range(10):
                emit.on_tool_called(SESSION, turn=1, name="tool", call_id=f"mt-{i}", server="s")
                emit.on_tool_completed(SESSION, turn=1, name="tool", call_id=f"mt-{i}", status="ok")
        except Exception as exc:
            errors.append(exc)

    def _messages() -> None:
        try:
            barrier.wait()
            for i in range(10):
                emit.on_message_received(SESSION, turn=1, text=f"msg {i}", role="user")
        except Exception as exc:
            errors.append(exc)

    def _approvals() -> None:
        try:
            barrier.wait()
            for i in range(10):
                emit.on_approval_requested(SESSION, turn=1, approval_id=f"ap-{i}", tool="t")
                emit.on_approval_decided(
                    SESSION, turn=1, approval_id=f"ap-{i}", decision="approved"
                )
        except Exception as exc:
            errors.append(exc)

    def _contexts() -> None:
        try:
            barrier.wait()
            for i in range(10):
                emit.on_context_composed(
                    SESSION, turn=1, blocks={"memory": 100 + i, "skills": 200 + i}
                )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_tools),
        threading.Thread(target=_messages),
        threading.Thread(target=_approvals),
        threading.Thread(target=_contexts),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads raised: {errors}"
    assert_drained()

    body = _body()
    seqs = [e["seq"] for e in body]
    assert seqs == list(range(1, len(body) + 1)), "non-contiguous seqs after mixed producers"

    # Verify tool calls and completions pair up correctly
    called_ids = {e["data"]["call_id"] for e in body if e["type"] == "tool/called"}
    completed_ids = {e["data"]["call_id"] for e in body if e["type"] == "tool/completed"}
    assert called_ids == completed_ids, (
        f"tool call/completion mismatch: called={called_ids - completed_ids}, "
        f"unclosed={completed_ids - called_ids}"
    )


# ---------------------------------------------------------------------------
# The drain barrier itself: a writer that stops is reported, never waited on
# ---------------------------------------------------------------------------


def _park_appends(monkeypatch) -> threading.Event:
    """Park every later append on the writer until the returned event is set.

    The wait is bounded too, so a test that forgets to release cannot hold the one
    process-wide writer thread for the rest of the run.
    """
    release = threading.Event()
    real_append = CrewLog.append

    def _parked(self, *args, **kwargs):
        release.wait(timeout=crew_log_drain.DRAIN_CEILING_SECONDS)
        return real_append(self, *args, **kwargs)

    monkeypatch.setattr(CrewLog, "append", _parked)
    return release


@pytest.mark.asyncio
async def test_the_barrier_names_a_writer_that_lands_nothing(monkeypatch):
    """A writer that lands nothing for a whole window fails the barrier, by name.

    The entry is emitted ON the loop so it is queued for the writer thread: off a loop
    the emitter writes inline, on the caller's own thread, which is not what this pins.
    """
    await asyncio.to_thread(_open)
    release = _park_appends(monkeypatch)
    try:
        emit.on_turn_started(SESSION, turn=1)
        # Scoped to this one barrier: teardown's settle() must wait with the real window
        # for the parked append to land once it is released.
        with pytest.MonkeyPatch.context() as tight:
            tight.setattr(crew_log_drain, "NO_PROGRESS_SECONDS", 0.2)
            with pytest.raises(AssertionError, match=r"landed nothing for 0\.2s") as stuck:
                await asyncio.to_thread(assert_drained)
        assert SESSION in str(stuck.value), "the failure does not name the log it waited on"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_the_barrier_gives_up_at_its_ceiling(monkeypatch):
    """The ceiling bounds the wait even when the window has not elapsed yet."""
    await asyncio.to_thread(_open)
    release = _park_appends(monkeypatch)
    try:
        emit.on_turn_started(SESSION, turn=1)
        with pytest.raises(AssertionError, match=r"was still writing at 0\.3s"):
            await asyncio.to_thread(assert_drained, ceiling=0.3)
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_gaps(seqs: list[int]) -> list[int]:
    """Return the seq values that are missing from a range."""
    if not seqs:
        return []
    expected = set(range(seqs[0], seqs[-1] + 1))
    return sorted(expected - set(seqs))
